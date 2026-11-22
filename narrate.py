"""Narrated clips: Gemini watches the footage, writes a voiceover, edge-tts (or ElevenLabs) speaks it,
the game audio is ducked under the voice, captions follow the narration, and the result is exported as
full-screen 9:16 shorts (smart crop that follows the action) and/or a 16:9 long-form video with chapters.

CLI:
  python narrate.py URL_OR_FILE --shorts 3 --long
  python narrate.py --help

Env: GEMINI_API_KEY (required), NEONCLIPPING_GEMINI_MODEL (optional), ELEVENLABS_API_KEY (optional, --tts elevenlabs).
Captions come from the TTS word boundaries. This pipeline does not run faster-whisper.
On a small ARM board (or with --low-power / NEONCLIPPING_LOW_POWER=1) shorts are 720x1280 @30fps
and long-form is 1280x720, encoded with h264_v4l2m2m when it works, otherwise libx264 ultrafast.
"""
import argparse, asyncio, base64, hashlib, json, math, os, re, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path
import envload
envload.load()

ROOT = Path(__file__).resolve().parent
CACHE = Path(os.environ.get('NEONCLIPPING_DATA_DIR', str(ROOT / 'data'))) / 'narrate'
GEMINI_MODELS = [m for m in [os.environ.get('NEONCLIPPING_GEMINI_MODEL'), 'gemini-2.5-flash', 'gemini-3.8-flash', 'gemini-flash-latest',
                             'gemini-3.5-flash', 'gemini-3-flash-preview', 'gemini-2.5-flash-lite', 'gemini-flash-lite-latest'] if m]
WPS = 2.5                    # narration words per second
DEFAULT_VOICE = 'en-US-GuyNeural'
TONES = {
    'hype': 'High-energy GTA commentary YouTuber. Punchy, excited, fun, slangy but clean, short sentences, reacts to what happens like a hype caster. Builds anticipation for GTA 6.',
    'storyteller': 'Cinematic storyteller. Vivid, dramatic, narrates the scene like a crime-movie trailer voiceover, builds tension and intrigue.',
    'news': 'Gaming news presenter. Clear, informative, confident; points out details, locations, characters and what they reveal about the game.',
}
LOG = print

# ---------------------------------------------------------------- helpers
def run(cmd, **kw):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)
    if r.returncode: raise RuntimeError(f'{Path(str(cmd[0])).name} failed: {r.stderr[:600]} ... {r.stderr[-400:]}')
    return r.stdout

def probe(path):
    d = json.loads(run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type,width,height,r_frame_rate:format=duration', '-of', 'json', path]))
    v = next((s for s in d['streams'] if s['codec_type'] == 'video'), {})
    n, _, den = (v.get('r_frame_rate') or '30/1').partition('/')
    return {'duration': float(d['format']['duration']), 'w': v.get('width'), 'h': v.get('height'),
            'fps': float(n) / float(den or 1), 'audio': any(s['codec_type'] == 'audio' for s in d['streams'])}

def split_jpegs(blob):
    """Split a concatenated ffmpeg mjpeg pipe into individual JPEG bytes."""
    out = []; i = 0
    while True:
        a = blob.find(b'\xff\xd8', i)
        if a < 0: break
        b = blob.find(b'\xff\xd9', a + 2)
        if b < 0: break
        out.append(blob[a:b + 2]); i = b + 2
    return out

def frames(src, start, end, step, width=512, quality=5):
    """[(t_relative, jpeg_bytes)] every `step` seconds. One ffmpeg process, cached under data/narrate/frames."""
    import lowpower
    dur = max(0.1, end - start)
    src = Path(src)
    try: stamp = src.stat().st_mtime_ns
    except OSError: stamp = 0
    key = hashlib.sha1(f'{src.resolve()}|{stamp}|{start:.2f}|{end:.2f}|{step:.3f}|{width}|{quality}'.encode()).hexdigest()[:20]
    cache = CACHE / 'frames' / f'{key}.jpgbin'
    blob = b''
    if cache.exists():
        try: blob = cache.read_bytes()
        except OSError: blob = b''
    if not blob:
        cmd = ['ffmpeg', '-v', 'error', *lowpower.thread_args(), '-ss', f'{start:.2f}', '-t', f'{dur:.2f}', '-i', str(src),
               '-vf', f'fps={1 / step:.6f},scale={width}:-2', '-q:v', str(quality), '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1']
        blob = subprocess.run(cmd, capture_output=True).stdout
        if blob:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_bytes(blob)
            except OSError: pass
    out = []
    for i, jpg in enumerate(split_jpegs(blob)):
        t = round(i * step, 1)
        if t < dur - .2: out.append((t, jpg))
    return out

def slug(text, n=50):
    return (re.sub(r'[^a-zA-Z0-9]+', '_', text).strip('_')[:n] or 'video').lower()

# ---------------------------------------------------------------- Gemini
class GeminiError(RuntimeError): pass
DEAD = set()          # models this key cannot use (404), skipped for the rest of the run
GOOD = []             # last model that answered goes first next time

def gemini(parts, schema_hint=True, temperature=.8, models=None):
    key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    if not key: raise GeminiError('GEMINI_API_KEY is not set.')
    body = {'contents': [{'role': 'user', 'parts': parts}],
            'generationConfig': {'temperature': temperature, **({'responseMimeType': 'application/json'} if schema_hint else {})}}
    ck = CACHE / 'gemini' / (hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest() + '.json')
    if ck.exists():
        try: return json.loads(ck.read_text(encoding='utf-8'))['answer']
        except (ValueError, KeyError): pass
    last = ''
    for attempt in range(4):
        order = GOOD + [m for m in (models or GEMINI_MODELS) if m not in GOOD]
        for model in [m for m in order if m not in DEAD]:
            b = dict(body, generationConfig=dict(body['generationConfig'], thinkingConfig={'thinkingLevel': 'low'})) if model.startswith('gemini-3') else body
            req = urllib.request.Request(f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
                                         json.dumps(b).encode(), {'Content-Type': 'application/json', 'x-goog-api-key': key})
            try:
                d = json.load(urllib.request.urlopen(req, timeout=180))
                text = ''.join(p.get('text', '') for p in d['candidates'][0]['content']['parts'] if not p.get('thought'))
                if model not in GOOD: GOOD[:] = [model]
                LOG(f'   (Gemini model: {model})') if len(GOOD) == 1 and not getattr(gemini, '_said', None) == model else None; gemini._said = model
                if schema_hint:
                    m = re.search(r'\{.*\}|\[.*\]', text, re.S); text = json.loads(m.group(0) if m else text)
                ck.parent.mkdir(parents=True, exist_ok=True); ck.write_text(json.dumps({'model': model, 'answer': text}), encoding='utf-8')
                return text
            except urllib.error.HTTPError as e:
                try: msg = json.load(e).get('error', {}).get('message', '')
                except Exception: msg = str(e)
                last = f'{model}: HTTP {e.code} {msg[:160]}'
                if e.code == 404: DEAD.add(model)
                if e.code == 400 and 'thinking' in msg.lower() and model.startswith('gemini-3'): DEAD.add(model)
                if e.code in (401, 403): raise GeminiError('Gemini rejected the API key: ' + msg[:200])
                if e.code == 429 and 'quota' in msg.lower() and 'retry' not in msg.lower(): last = f'{model}: quota exhausted ({msg[:160]})'
            except (KeyError, IndexError, ValueError, TimeoutError, urllib.error.URLError) as e:
                last = f'{model}: {type(e).__name__} {str(e)[:160]}'
            LOG('   Gemini retry -', last)
        time.sleep(4 * (attempt + 1))
    raise GeminiError('Gemini failed: ' + last)

def img_parts(fr):
    parts = []
    for t, jpg in fr:
        parts += [{'text': f'[t={t:.1f}s]'}, {'inline_data': {'mime_type': 'image/jpeg', 'data': base64.b64encode(jpg).decode()}}]
    return parts

def overview_and_moments(src, meta, count, min_len, max_len, title=''):
    """Gemini looks at the whole source (frames every 2-6 s) and proposes moments worth narrating."""
    import lowpower
    dur = meta['duration']; step, width, quality = lowpower.gemini_limits(dur, 'overview')
    fr = frames(src, 0, dur, step, width, quality)
    prompt = (f'These are frames sampled every {step:.1f}s from a video{(" titled " + json.dumps(title)) if title else ""} (duration {dur:.1f}s). '
              f'It is GTA / Grand Theft Auto 6 footage. Give a short overview of what the video shows (setting, characters, story beats), then pick the {count} best, '
              f'most visually exciting or story-rich moments to turn into narrated vertical shorts. Each moment must be {min_len:.0f}-{max_len:.0f} seconds long, '
              f'within 0-{dur:.1f}s, should start on a clean visual beat, and moments should not overlap much. Also split the WHOLE video into 3-6 chronological '
              'chapters for a long-form breakdown video. Skip rating cards (ESRB/PEGI), black screens and logo-only frames when choosing moment and chapter starts. Return JSON: {"overview": str, "moments": [{"start": float, "end": float, "why": str}], '
              '"chapters": [{"start": float, "end": float, "title": str}]}')
    return gemini([{'text': prompt}] + img_parts(fr), temperature=.4)

def write_script(src, start, end, tone, overview, kind='short', extra=''):
    import lowpower
    dur = end - start; words = max(8, int((dur - 1.2) * WPS))
    step, width, quality = lowpower.gemini_limits(dur, 'clip')
    fr = frames(src, start, end, step, width, quality)
    prompt = (f'You are writing a voiceover for a {"vertical YouTube Short / TikTok" if kind == "short" else "segment of a long-form YouTube breakdown video"} '
              f'made from GTA 6 footage. Tone: {TONES.get(tone, tone)}\nContext about the whole video: {overview}\n{extra}\n'
              f'The frames below are sampled from this {dur:.1f}-second clip with their timestamps. First describe what happens as a scene timeline. '
              f'Then write the narration: about {words} words (it is spoken at ~{WPS} words/sec and MUST fit in {dur - 1.2:.0f} seconds; never exceed {int(words * 1.1)} words). '
              'Open with a scroll-stopping hook in the first sentence. Describe/react to what is actually on screen in order, roughly in sync with the timeline. '
              'Do not invent facts that are not visible or widely known; no emojis, no stage directions, no hashtags in the narration. '
              'Also write: a short on-screen hook (max 6 words, ALL CAPS friendly), a YouTube Shorts title (max 70 chars, curiosity-driven, may include #Shorts at the end), '
              'a TikTok caption (max 150 chars), a YouTube description (2-3 sentences + call to action), 8-12 YouTube hashtags/tags and 5-8 TikTok hashtags. '
              'Return JSON: {"timeline": [{"t": float, "what": str}], "narration": str, "hook": str, "title": str, "tiktok_caption": str, '
              '"description": str, "youtube_tags": [str], "youtube_hashtags": [str], "tiktok_hashtags": [str]}')
    out = gemini([{'text': prompt}] + img_parts(fr))
    out['narration'] = re.sub(r'\s+', ' ', re.sub(r'[\*\[\]#]', '', str(out.get('narration', '')))).strip()
    return out

# ---------------------------------------------------------------- TTS
def tts(text, path, voice=DEFAULT_VOICE, rate='+8%', engine='edge'):
    """-> list of word dicts {start,end,text} relative to the audio start. Writes mp3 to path."""
    if engine == 'elevenlabs' and os.environ.get('ELEVENLABS_API_KEY'):
        return _eleven(text, path, voice)
    import edge_tts
    async def go():
        words = []
        com = edge_tts.Communicate(text, voice, rate=rate, boundary='WordBoundary')
        with open(path, 'wb') as f:
            async for ch in com.stream():
                if ch['type'] == 'audio': f.write(ch['data'])
                elif ch['type'] == 'WordBoundary':
                    s = ch['offset'] / 1e7; words.append({'start': s, 'end': s + ch['duration'] / 1e7, 'text': ch['text']})
        return words
    for attempt in range(3):
        try: words = asyncio.run(go()); break
        except Exception as e:
            if attempt == 2: raise RuntimeError(f'edge-tts failed: {e}')
            time.sleep(3)
    return attach_punct(words, text)

def attach_punct(words, text):
    """edge-tts word boundaries drop punctuation; put it back so caption groups break on sentences."""
    toks = text.split(); j = 0
    for w in words:
        core = re.sub(r'\W', '', w['text']).lower()
        for k in range(j, min(j + 4, len(toks))):
            if re.sub(r'\W', '', toks[k]).lower().startswith(core[:4]) or core.startswith(re.sub(r'\W', '', toks[k]).lower()[:4]):
                w['text'] = toks[k]; j = k + 1; break
    return words

def _eleven(text, path, voice):
    vid = voice if re.fullmatch(r'[A-Za-z0-9]{16,}', voice or '') else os.environ.get('ELEVENLABS_VOICE_ID', 'pNInz6obpgDQGcFmaJgB')  # "Adam"
    req = urllib.request.Request(f'https://api.elevenlabs.io/v1/text-to-speech/{vid}/with-timestamps',
                                 json.dumps({'text': text, 'model_id': 'eleven_multilingual_v2'}).encode(),
                                 {'Content-Type': 'application/json', 'xi-api-key': os.environ['ELEVENLABS_API_KEY']})
    d = json.load(urllib.request.urlopen(req, timeout=180))
    Path(path).write_bytes(base64.b64decode(d['audio_base64']))
    al = d['alignment']; words = []; cur = ''; st = None
    for ch, a, b in zip(al['characters'], al['character_start_times_seconds'], al['character_end_times_seconds']):
        if ch.isspace():
            if cur: words.append({'start': st, 'end': prev, 'text': cur}); cur = ''
        else:
            if not cur: st = a
            cur += ch; prev = b
    if cur: words.append({'start': st, 'end': prev, 'text': cur})
    return words

def voiceover(text, path, max_len, voice, engine, rate=8):
    """TTS that fits max_len seconds: speeds the voice up a little (up to +30%) when the script runs long."""
    for r in (rate, rate + 10, rate + 22):
        words = tts(text, path, voice, f'{r:+d}%', engine)
        d = probe(path)['duration']
        if d <= max_len or engine == 'elevenlabs': return words, d
        LOG(f'   voice {d:.1f}s > {max_len:.1f}s, speeding up')
    return words, d

# ---------------------------------------------------------------- smart crop
def crop_track(src, start, end, crop_w, src_w, src_h, fps=4):
    """x offset (pixels) of a crop_w-wide window over time, following motion + people, smoothed. -> [(t, x)]"""
    import lowpower
    import numpy as np
    cheap = lowpower.enabled()
    if cheap: fps = min(fps, 2)
    sw = 320; sh = max(2, int(src_h * sw / src_w) // 2 * 2); scale = src_w / sw
    raw = subprocess.run(['ffmpeg', '-v', 'error', *lowpower.thread_args(), '-ss', f'{start:.2f}', '-t', f'{end - start:.2f}', '-i', str(src),
                          '-vf', f'fps={fps},scale={sw}:{sh}', '-f', 'rawvideo', '-pix_fmt', 'gray', 'pipe:1'], capture_output=True).stdout
    n = len(raw) // (sw * sh)
    if n < 2: return [(0, (src_w - crop_w) / 2)]
    fr = np.frombuffer(raw[:n * sw * sh], np.uint8).reshape(n, sh, sw).astype(np.float32)
    if not cheap:
        import cv2
    cols = np.arange(sw, dtype=np.float32); centre = sw / 2
    xs = []; prev_c = centre
    for i in range(n):
        diff = np.abs(fr[i] - fr[i - 1]) if i else np.zeros_like(fr[0])
        cut = diff.mean() > 40                          # scene cut: motion is meaningless, reset to centre bias
        if cheap:
            energy = np.zeros(sw) if cut else diff.sum(0)          # column motion on the 320px proxy; no OpenCV
        else:
            sal = cv2.GaussianBlur(np.abs(cv2.Laplacian(fr[i], cv2.CV_32F)), (0, 0), 6)    # detail/edges
            energy = (0 if cut else diff.sum(0) * 2.0) + sal.sum(0) * .15
        prior = np.exp(-((cols - centre) / (sw * .35)) ** 2)        # prefer the middle of the frame
        w = energy * prior
        c = float((w * cols).sum() / max(w.sum(), 1e-6)) if w.sum() > 0 else centre
        c = .5 * c + .5 * centre if cut else c
        xs.append(c); prev_c = c
    xs = np.array(xs)
    k = max(3, int(fps * 3)) | 1                       # smooth over ~3 s, then limit pan speed
    pad = np.pad(xs, k // 2, mode='edge'); xs = np.convolve(pad, np.ones(k) / k, 'valid')
    half = crop_w / scale / 2; xs = np.clip(xs, half, sw - half)
    maxstep = sw * .05 / fps                           # at most ~5% of the width per second
    for i in range(1, len(xs)): xs[i] = xs[i - 1] + np.clip(xs[i] - xs[i - 1], -maxstep, maxstep)
    return [(i / fps, float(min(max(0, (x - half) * scale), src_w - crop_w))) for i, x in enumerate(xs)]

def piecewise(track, every=1.0):
    """ffmpeg expression linearly interpolating x(t) through keyframes (t relative to the trimmed input)."""
    keys = [track[0]]
    for t, x in track[1:]:
        if t - keys[-1][0] >= every: keys.append((t, x))
    if keys[-1] != track[-1]: keys.append(track[-1])
    if len(keys) == 1: return f'{keys[0][1]:.1f}'
    expr = f'{keys[-1][1]:.1f}'
    for (t0, x0), (t1, x1) in reversed(list(zip(keys, keys[1:]))):
        expr = f'if(lt(t\\,{t1:.2f})\\,{x0:.1f}+({x1 - x0:.1f})*(t-{t0:.2f})/{max(t1 - t0, .01):.2f}\\,{expr})'
    return expr

# ---------------------------------------------------------------- render
def captions_ass(words, folder, name, hook, width, height, caption_y, size, hook_seconds=3.0, font='Anton'):
    """GTA Neon captions (cyan words, magenta active word, pop-in) timed to the voiceover, plus a big hook title for the first seconds."""
    sys.path.insert(0, str(ROOT))
    import captions
    base = min(size, 140)            # the shared caption writer caps sizes at 140; scale up afterwards
    app = {'caption_size': base, 'caption_font': font, 'caption_max_words': 3}
    ass, _ = captions.write(words, 'gta', Path(folder), name, '', caption_y, 0, width, height, 'pop_in_line', app)
    path = Path(folder) / ass
    text = path.read_text(encoding='utf-8').replace('\\bord4\\3c&H00560C31&\\shad5', '\\bord10\\3c&H00300818&\\shad7')
    text = text.replace(f'\\fs{base}\\', f'\\fs{size}\\')
    fixed = []
    for line in text.split('\n'):                       # shrink caption lines that would run off the screen
        if line.startswith('Dialogue: 1,') and f'\\fs{size}\\' in line:
            visible = re.sub(r'\{[^}]*\}', '', line.split(',,0,0,0,,', 1)[-1])
            est = len(visible) * size * .43       # Anton is condensed: ~0.43 em per uppercase char
            if est > width * .9: line = line.replace(f'\\fs{size}\\', f'\\fs{int(size * width * .9 / est)}\\')
        fixed.append(line)
    text = '\n'.join(fixed)
    if hook.strip():
        import textwrap
        lines = textwrap.wrap(captions.safe(hook).upper(), width=13 if width < height else 24)[:3]
        fs = int(height * (.11 if width < height else .14)); y0 = int(height * (.14 if width < height else .15))
        for k, line in enumerate(lines):
            colour = '&H0000E5FF&' if k == 0 else '&H00FFFFFF&'     # first line GTA yellow, rest white
            text += (f'\nDialogue: 3,0:00:00.00,{captions.stamp(hook_seconds, True)},Main,,0,0,0,,{{\\an5\\pos({width // 2},{y0 + int(k * fs * 1.08)})'
                     f'\\fn{font}\\fs{fs}\\c{colour}\\bord10\\3c&H00000000&\\shad7\\4c&H99000000&'
                     f'\\fscx80\\fscy80\\t(0,160,\\fscx106\\fscy106)\\t(160,260,\\fscx100\\fscy100)\\fad(0,250)}}{line}')
    path.write_text(text, encoding='utf-8')
    return path

def render(src, start, end, voice_mp3, words, out, *, vertical=True, hook='', audio_mode='duck', duck=.15, fps=60, fill=1.0,
           voice_delay=.35, crf=21, maxrate='9M', title_card=None, speed=1.0, darken=0.0):
    """One narrated clip. Original audio muted or ducked to `duck` under the voice; captions follow the narration."""
    import lowpower, encoders
    src = Path(src).resolve(); voice_mp3 = Path(voice_mp3).resolve()
    meta = probe(src); W, H = lowpower.short_size() if vertical else lowpower.long_size()
    flags = 'bilinear' if lowpower.enabled() else 'lanczos'
    dur = end - start; out = Path(out); work = out.with_suffix('')
    vdur = dur / speed
    ws = [{'start': w['start'] + voice_delay, 'end': w['end'] + voice_delay, 'text': w['text']} for w in words]
    ass = captions_ass(ws, out.parent, out.stem, hook, W, H, int(H * (.70 if vertical else .85)), 205 if vertical else 175, 3.0)
    if title_card:
        import captions, textwrap
        card_text = '\\N'.join(textwrap.wrap(captions.safe(title_card).upper(), 16)[:3])
        extra = [f'Dialogue: 2,0:00:00.00,{captions.stamp(vdur, True)},Main,,0,0,0,,{{\\an5\\pos({W // 2},{H // 2})\\fnAnton\\fs{int(H * .16)}\\bord8\\shad6\\c&H00FFFFFF&\\3c&H00560C31&\\4c&H00FFCC00&\\fad(300,300)}}{card_text}']
        ass.write_text(ass.read_text(encoding='utf-8') + '\n' + '\n'.join(extra), encoding='utf-8')
    sw, sh = meta['w'], meta['h']
    if vertical:
        vh = int(H * fill) // 2 * 2
        cw = int(round(sh * (W / vh))) // 2 * 2            # crop width keeping the output aspect of the video area
        cw = min(cw, sw)
        x = piecewise(crop_track(src, start, end, cw, sw, sh)) if cw < sw else '0'
        vf = f'[0:v]setpts=(PTS-STARTPTS)/{speed},crop={cw}:{sh}:{x}:0,scale={W}:{vh}:flags={flags},setsar=1'
        if fill < .999:
            blur = '8:1' if lowpower.enabled() else '20:2'
            vf = (f'[0:v]setpts=(PTS-STARTPTS)/{speed},split=2[b][f];[b]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur={blur},eq=brightness=-.12[bg];'
                  f'[f]crop={cw}:{sh}:{x}:0,scale={W}:{vh}:flags={flags}[fg];[bg][fg]overlay=0:(H-h)/2,setsar=1')
    else:
        vf = f'[0:v]setpts=(PTS-STARTPTS)/{speed},scale={W}:{H}:force_original_aspect_ratio=increase:flags={flags},crop={W}:{H},setsar=1'
    if darken: vf += f',eq=brightness=-{darken}'
    vf += f',fps={fps},ass=filename={ass.name}[v]'
    d_ms = int(voice_delay * 1000)
    vo = f'[1:a]aresample=48000,adelay={d_ms}|{d_ms},apad,atrim=0:{vdur:.3f}[vo]'
    norm = 'alimiter=limit=0.95' if lowpower.enabled() else 'loudnorm=I=-14:TP=-1.5:LRA=11'   # loudnorm is a second full pass
    if audio_mode == 'mute' or not meta['audio']:
        af = vo + f';[vo]{norm},aresample=48000[a]'
    else:
        atempo = f',atempo={speed}' if speed != 1 else ''
        # game audio sits at `duck` under the voice and comes back up (to ~2.5x duck) in the gaps
        af = (vo + f';[vo]asplit[vo1][sc];[0:a]aresample=48000{atempo},asetpts=PTS-STARTPTS,volume={min(1, duck * 2.5):.3f}[g];'
              '[g][sc]sidechaincompress=threshold=0.02:ratio=8:attack=15:release=350:makeup=1[gd];'
              f'[gd][vo1]amix=inputs=2:duration=first:normalize=0,afade=t=out:st={max(0, vdur - .4):.2f}:d=0.4,{norm},aresample=48000[a]')
    fc = vf + ';' + af
    # One graph: trim, crop, scale, captions and the audio mix. No intermediate re-encode.
    limit = ['-filter_threads', str(lowpower.threads())] if lowpower.threads() else []
    prefix = ['ffmpeg', '-y', '-v', 'error', *lowpower.thread_args(), *limit,
              '-ss', f'{start:.3f}', '-t', f'{dur:.3f}', '-i', src, '-i', voice_mp3, '-filter_complex', fc,
              '-map', '[v]', '-map', '[a]', '-t', f'{vdur:.3f}']
    suffix = ['-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '160k', '-ar', '48000', '-movflags', '+faststart', out]
    errors = []
    # Desktop stays libx264 medium. A small board tries the V4L2 hardware encoder, then ultrafast x264.
    choices = encoders.h264(cq=crf, x264_preset='medium') if lowpower.enabled() else [['-c:v', 'libx264', '-crf', str(crf), '-preset', 'medium']]
    for encoder in choices:
        rate = ['-maxrate', maxrate, '-bufsize', '12M'] if encoder[1] == 'libx264' else []
        try:
            run(prefix + encoder + rate + suffix, cwd=str(out.parent))
            break
        except RuntimeError as e:
            errors.append(str(e)[:240]); out.unlink(missing_ok=True)
    else:
        raise RuntimeError('encode failed: ' + ' | '.join(errors)[-800:])
    return out

# ---------------------------------------------------------------- source
def get_source(arg, height=None):
    import lowpower
    if height is None: height = 720 if lowpower.enabled() else 1080
    p = Path(arg)
    if p.exists(): return p.resolve(), p.stem
    CACHE.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT))
    import ytdl
    key = hashlib.sha1(f'{arg}|{height}'.encode()).hexdigest()[:12]; target = CACHE / f'src_{key}_{height}.mp4'; info_f = CACHE / f'src_{key}_{height}.json'
    legacy = CACHE / f'src_{hashlib.sha1(arg.encode()).hexdigest()[:12]}.mp4'
    if height >= 1080 and not target.exists() and legacy.exists():
        info_legacy = legacy.with_suffix('.json')
        if info_legacy.exists(): return legacy, json.loads(info_legacy.read_text()).get('title', '')
    if target.exists() and info_f.exists(): return target, json.loads(info_f.read_text())['title']
    fmt = f'bv*[height<={height}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={height}]+ba/b[height<={height}]/b'
    opts = {'format': fmt, 'merge_output_format': 'mp4', 'outtmpl': str(CACHE / f'src_{key}_{height}.%(ext)s'), 'noplaylist': True, 'quiet': True, 'noprogress': True, 'no_warnings': True}
    if lowpower.enabled(): opts['concurrent_fragment_downloads'] = 2
    with ytdl.YDL(opts) as y:
        info = y.extract_info(arg, download=True)
    if not target.exists():
        cand = sorted(CACHE.glob(f'src_{key}_{height}.*'), key=lambda f: f.stat().st_size)
        cand = [c for c in cand if c.suffix in ('.mp4', '.mkv', '.webm')]
        run(['ffmpeg', '-y', '-v', 'error', '-i', cand[-1], '-c', 'copy', target])
    info_f.write_text(json.dumps({'title': info.get('title', ''), 'url': arg}))
    return target, info.get('title', '')

# ---------------------------------------------------------------- pipelines
def save_meta(path, meta):
    path.with_suffix('.json').write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8')
    def tags(xs):
        seen = []
        for x in xs or []:
            t = '#' + str(x).lstrip('#').replace(' ', '')
            if t.lower() not in [k.lower() for k in seen]: seen.append(t)
        return ' '.join(seen)
    for k in ('youtube_hashtags', 'tiktok_hashtags'): meta[k] = tags(meta.get(k)).split() if meta.get(k) else meta.get(k)
    if meta.get('tiktok_caption'): meta['tiktok_caption'] = re.sub(r'\s*#\w+', '', meta['tiktok_caption']).strip()   # hashtags are listed once, below
    lines = [f"TITLE: {meta['title']}", '', 'YOUTUBE DESCRIPTION:', meta.get('description', ''), '', tags(meta.get('youtube_hashtags')), '',
             'YOUTUBE TAGS: ' + ', '.join(meta.get('youtube_tags') or []), '']
    if meta.get('tiktok_caption') is not None:
        lines += ['TIKTOK CAPTION:', f"{meta.get('tiktok_caption', '')} {tags(meta.get('tiktok_hashtags'))}", '']
    if meta.get('chapters'):
        lines += ['CHAPTERS:'] + [f"{c['stamp']} {c['title']}" for c in meta['chapters']] + ['']
    lines += ['NARRATION:', meta.get('narration', '')]
    path.with_suffix('.txt').write_text('\n'.join(lines), encoding='utf-8')

def make_shorts(src, title, plan, a, outdir):
    meta = probe(src); results = []
    for i, m in enumerate(plan['moments'][:a.shorts]):
        start = max(0, float(m['start'])); end = min(meta['duration'], float(m['end']))
        if end - start < 8: continue
        LOG(f'[short {i + 1}] {start:.1f}-{end:.1f}s: writing narration')
        s = write_script(src, start, end, a.tone, plan.get('overview', ''), 'short')
        name = f'short_{i + 1:02d}_{slug(s.get("title", "gta6"), 40)}'
        mp3 = outdir / f'{name}.voice.mp3'
        LOG(f'[short {i + 1}] voiceover ({len(s["narration"].split())} words)')
        words, vlen = voiceover(s['narration'], mp3, end - start - .6, a.voice, a.tts)
        end = min(meta['duration'], max(end, start + vlen + .9))        # never cut the voice off
        import lowpower
        LOG(f'[short {i + 1}] rendering {end - start:.1f}s {lowpower.short_size()[0]}x{lowpower.short_size()[1]}')
        out = render(src, start, end, mp3, words, outdir / f'{name}.mp4', vertical=True, hook=s.get('hook', ''), audio_mode=a.audio,
                     duck=a.duck, fps=a.fps, fill=a.fill)
        mp3.unlink(missing_ok=True)
        info = {'file': str(out), 'type': 'short', 'source': title, 'source_range': [round(start, 2), round(end, 2)], **s,
                'voice': a.voice, 'tone': a.tone}
        save_meta(out, info); results.append(info)
    return results

def stamp(t):
    t = int(t); return f'{t // 60}:{t % 60:02d}' if t < 3600 else f'{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}'

def make_long(src, title, plan, a, outdir):
    meta = probe(src); dur = meta['duration']
    chapters = [c for c in plan.get('chapters') or [] if float(c['end']) - float(c['start']) >= 8]
    if not chapters:
        n = max(3, min(6, int(dur // 40))); chapters = [{'start': dur * k / n, 'end': dur * (k + 1) / n, 'title': f'Part {k + 1}'} for k in range(n)]
    if a.long_minutes and dur > a.long_minutes * 60:             # long source: keep the best chapters only
        chapters = chapters[:max(2, int(a.long_minutes * 60 // 45))]
    parts = []; tmp = outdir / '_long_parts'; tmp.mkdir(exist_ok=True)
    ov = plan.get('overview', '')
    LOG('[long] writing intro/outro')
    io = gemini([{'text': f'Write the intro and outro voiceover for a long-form YouTube breakdown video about this GTA 6 footage ("{title}"). '
                          f'Tone: {TONES.get(a.tone, a.tone)} Overview: {ov}\nChapters: {json.dumps([c["title"] for c in chapters])}\n'
                          'Intro: 30-40 words, hook the viewer and say what we will break down. Outro: 25-35 words, wrap up and ask viewers to like, subscribe and comment their favourite moment. '
                          'Also write a YouTube title (max 90 chars), a description (3-5 sentences, no timestamps), 10-15 tags and 3-5 hashtags, and a 2-5 word title-card text. '
                          'Return JSON: {"intro": str, "outro": str, "title": str, "description": str, "youtube_tags": [str], "youtube_hashtags": [str], "card": str}'}])
    # intro over a slowed, darkened opening shot with a title card; outro over the last shot
    mo = plan.get('moments') or [{'start': dur * .3, 'end': dur * .3 + 9}]
    i0 = float(mo[0]['start']); o0 = float(mo[-1]['start'])
    segs = [('intro', i0, min(dur, i0 + 9), io.get('intro', ''), io.get('card', 'GTA 6'), .6, .18)]
    for k, c in enumerate(chapters):
        segs.append((f'ch{k + 1}', float(c['start']), min(dur, float(c['end'])), None, None, 1.0, 0))
    segs.append(('outro', o0, min(dur, o0 + 8), io.get('outro', ''), 'LIKE + SUBSCRIBE', .6, .18))
    t = 0.0; chap_meta = []; files = []
    for k, (tag, st, en, text, card, speed, dark) in enumerate(segs):
        if text is None:
            LOG(f'[long] chapter {tag}: {st:.1f}-{en:.1f}s narration')
            s = write_script(src, st, en, a.tone, ov, 'long', f'This is the chapter "{chapters[k - 1]["title"]}". Do not greet the viewer or say goodbye; flow on from the previous chapter.')
            text = s['narration']; ctitle = chapters[k - 1]['title']
        else: ctitle = 'Intro' if tag == 'intro' else 'Outro'
        mp3 = tmp / f'{tag}.mp3'
        vis = (en - st) / speed
        words = tts(text, mp3, a.voice, '+8%', a.tts); vlen = probe(mp3)['duration']
        if vlen + .9 > vis:                    # stretch slow-motion intro/outro or extend the chapter to fit the voice
            if speed < 1: speed = max(.35, (en - st) / (vlen + 1.0))
            else: en = min(dur, st + vlen + .9)
            vis = (en - st) / speed
            if vlen + .5 > vis: words, vlen = voiceover(text, mp3, vis - .5, a.voice, a.tts)
        f = render(src, st, en, mp3, words, tmp / f'{k:02d}_{tag}.mp4', vertical=False, audio_mode=a.audio, duck=a.duck, fps=a.long_fps,
                   title_card=card, speed=speed, darken=dark, crf=23, maxrate='3M')
        chap_meta.append({'stamp': stamp(t), 'start': round(t, 2), 'title': ctitle, 'narration': text}); t += probe(f)['duration']; files.append(f)
    name = f'long_{slug(io.get("title", title), 50)}'
    out = outdir / f'{name}.mp4'
    lst = tmp / 'list.txt'; lst.write_text(''.join(f"file '{f.name}'\n" for f in files))
    ffmeta = tmp / 'chapters.txt'
    ffmeta.write_text(';FFMETADATA1\n' + ''.join(f'[CHAPTER]\nTIMEBASE=1/1000\nSTART={int(c["start"] * 1000)}\nEND={int((chap_meta[i + 1]["start"] if i + 1 < len(chap_meta) else t) * 1000)}\ntitle={c["title"]}\n'
                                                for i, c in enumerate(chap_meta)))
    run(['ffmpeg', '-y', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', lst, '-i', ffmeta, '-map_metadata', '1', '-map_chapters', '1', '-c', 'copy', '-movflags', '+faststart', out], cwd=str(tmp))
    desc = io.get('description', '') + '\n\nChapters:\n' + '\n'.join(f"{c['stamp']} {c['title']}" for c in chap_meta)
    info = {'file': str(out), 'type': 'long', 'source': title, 'title': io.get('title', title), 'description': desc,
            'youtube_tags': io.get('youtube_tags', []), 'youtube_hashtags': io.get('youtube_hashtags', []), 'chapters': chap_meta,
            'narration': '\n\n'.join(f"[{c['stamp']} {c['title']}] {c['narration']}" for c in chap_meta), 'voice': a.voice, 'tone': a.tone}
    save_meta(out, info)
    if not a.keep_parts:
        for f in tmp.iterdir(): f.unlink()
        tmp.rmdir()
    return info

def rerender(json_path, a):
    """Re-voice and re-render a short from its saved .json (same narration, range and metadata), no Gemini call. Use after changing
    the voice, audio mix, fill or caption look."""
    info = json.loads(Path(json_path).read_text(encoding='utf-8'))
    if info.get('type') != 'short': raise SystemExit('Only shorts can be re-rendered from JSON for now.')
    src, _ = get_source(a.source); start, end = info['source_range']; out = Path(info['file'])
    mp3 = out.with_suffix('.voice.mp3')
    words, vlen = voiceover(info['narration'], mp3, end - start - .6, a.voice, a.tts)
    end = min(probe(src)['duration'], max(end, start + vlen + .9))
    render(src, start, end, mp3, words, out, vertical=True, hook=info.get('hook', ''), audio_mode=a.audio, duck=a.duck, fps=a.fps, fill=a.fill)
    mp3.unlink(missing_ok=True)
    info.update(source_range=[round(start, 2), round(end, 2)], voice=a.voice); save_meta(out, info)
    return info

def parser():
    p = argparse.ArgumentParser(description='Narrated GTA clips: Gemini script + TTS voiceover + captions, 9:16 shorts and 16:9 long-form.')
    p.add_argument('source', help='YouTube/Twitch/Kick URL or a local video file')
    p.add_argument('--out', default='', help='output folder (default: data/narrate-out inside this repo)')
    p.add_argument('--shorts', type=int, default=3, help='number of vertical shorts (0 = none)')
    p.add_argument('--min-len', type=float, default=25); p.add_argument('--max-len', type=float, default=50)
    p.add_argument('--long', action='store_true', help='also make a 16:9 long-form video with intro, outro and chapters')
    p.add_argument('--long-minutes', type=float, default=10, help='rough cap on long-form length for long sources')
    p.add_argument('--tone', default='hype', help='hype | storyteller | news | free text')
    p.add_argument('--voice', default=DEFAULT_VOICE, help='edge-tts voice (e.g. en-US-GuyNeural, en-US-ChristopherNeural, en-GB-RyanNeural) or ElevenLabs voice id')
    p.add_argument('--tts', default='edge', choices=['edge', 'elevenlabs'])
    p.add_argument('--audio', default='duck', choices=['duck', 'mute'], help='original game audio: ducked under the voice or muted')
    p.add_argument('--duck', type=float, default=.15, help='game audio level under the voice (0-1)')
    p.add_argument('--fill', type=float, default=1.0, help='share of the 9:16 height filled by the video (1 = full screen, .85 = light blurred bars)')
    p.add_argument('--fps', type=int, default=None, help='short frame rate (default 60, or 30 with --low-power)')
    p.add_argument('--long-fps', type=int, default=None, help='long-form frame rate (default 30)')
    p.add_argument('--low-power', action='store_true', help='720x1280 @30fps, ultrafast / v4l2m2m, fewer Gemini frames')
    p.add_argument('--no-low-power', action='store_true', help='keep desktop quality even on a small ARM board')
    p.add_argument('--keep-parts', action='store_true')
    p.add_argument('--plan', help='reuse a saved plan.json (moments + chapters) instead of asking Gemini again')
    p.add_argument('--rerender', metavar='SHORT.json', nargs='+', help='re-voice/re-render saved shorts (no Gemini); source must be the same video')
    return p

def main(argv=None, log=None):
    global LOG
    if log: LOG = log
    a = parser().parse_args(argv)
    if a.low_power and a.no_low_power: raise SystemExit('Use only one of --low-power and --no-low-power.')
    if a.low_power: os.environ['NEONCLIPPING_LOW_POWER'] = '1'
    elif a.no_low_power: os.environ['NEONCLIPPING_LOW_POWER'] = '0'
    import lowpower
    if a.fps is None: a.fps = lowpower.short_fps(None)
    if a.long_fps is None: a.long_fps = 30
    outdir = Path(a.out) if a.out else ROOT / 'data' / 'narrate-out'
    outdir.mkdir(parents=True, exist_ok=True)
    a.out = str(outdir)
    if lowpower.enabled():
        sw, sh = lowpower.short_size(); lw, lh = lowpower.long_size()
        LOG(f'Low-power profile: shorts {sw}x{sh}@{a.fps}fps, long {lw}x{lh}@{a.long_fps}fps, download 720p, x264 ultrafast / v4l2m2m, no Whisper')
    if a.rerender:
        return [LOG('Re-rendered', rerender(j, a)['file']) or j for j in a.rerender]
    LOG('Getting source'); src, title = get_source(a.source)
    meta = probe(src); LOG(f'Source: {title!r} {meta["duration"]:.1f}s {meta["w"]}x{meta["h"]}')
    LOG('Gemini: watching the whole video and picking moments')
    plan = json.loads(Path(a.plan).read_text()) if a.plan else overview_and_moments(src, meta, max(1, a.shorts), a.min_len, a.max_len, title)
    (outdir / 'plan.json').write_text(json.dumps(plan, indent=2), encoding='utf-8')
    results = []
    if a.shorts: results += make_shorts(src, title, plan, a, outdir)
    if a.long: results.append(make_long(src, title, plan, a, outdir))
    LOG('Done:'); [LOG('  ', r['file'], '|', r['title']) for r in results]
    return results

if __name__ == '__main__':
    try: main()
    except GeminiError as e: print('ERROR:', e, file=sys.stderr); sys.exit(2)
