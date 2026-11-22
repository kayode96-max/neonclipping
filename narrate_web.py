"""Web hooks for the narrated pipeline: POST /narrate, GET /narrate/{job}, GET /narrate-file?path=..., page at /narrate-ui."""
import threading, uuid, time, os
from pathlib import Path
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, FileResponse

JOBS = {}
OUT_ROOT = Path(os.environ.get('NEONCLIPPING_NARRATE_OUT', '/workspace/gta6_out')).resolve()

def attach(app):
    @app.post('/narrate')
    def narrate_start(b: dict):
        src = str(b.get('source') or '').strip()
        if not src: raise HTTPException(400, 'Paste a video link or a local file path.')
        if any(j['status'] == 'running' for j in JOBS.values()): raise HTTPException(409, 'A narrated job is already running.')
        args = [src, '--out', str(OUT_ROOT / time.strftime('%Y%m%d_%H%M%S')), '--shorts', str(int(b.get('shorts', 3))),
                '--tone', str(b.get('tone') or 'hype'), '--voice', str(b.get('voice') or 'en-US-GuyNeural'),
                '--audio', 'mute' if b.get('audio') == 'mute' else 'duck', '--duck', str(float(b.get('duck', .15))),
                '--fill', str(float(b.get('fill', 1.0))), '--min-len', str(float(b.get('min_len', 25))), '--max-len', str(float(b.get('max_len', 50)))]
        if b.get('long'): args.append('--long')
        if b.get('tts') == 'elevenlabs': args += ['--tts', 'elevenlabs']
        job = uuid.uuid4().hex[:12]; JOBS[job] = {'status': 'running', 'log': [], 'results': []}
        def work():
            import narrate
            try:
                JOBS[job]['results'] = narrate.main(args, log=lambda *a: JOBS[job]['log'].append(' '.join(map(str, a))))
                JOBS[job]['status'] = 'done'
            except Exception as e:
                JOBS[job].update(status='error', error=str(e)[:800])
        threading.Thread(target=work, daemon=True).start()
        return {'job': job}

    @app.get('/narrate/{job}')
    def narrate_status(job: str):
        if job not in JOBS: raise HTTPException(404, 'Unknown job')
        return JOBS[job]

    @app.get('/narrate-file')
    def narrate_file(path: str):
        p = Path(path).resolve()
        if OUT_ROOT not in p.parents or not p.is_file(): raise HTTPException(404, 'Not found')
        return FileResponse(p)

    @app.get('/narrate-ui')
    def narrate_ui():
        return HTMLResponse(PAGE)

PAGE = '''<!doctype html><html><head><meta charset="utf-8"><title>NeonClipping - Narrated</title>
<style>body{background:#0d0a1a;color:#eee;font-family:system-ui,sans-serif;max-width:900px;margin:30px auto;padding:0 16px}
h1{color:#ff4fd8;text-shadow:0 0 12px #ff4fd8}label{display:block;margin:10px 0 4px;color:#43ffef}input,select{background:#1b1530;color:#fff;border:1px solid #553;padding:8px;border-radius:6px;width:100%;box-sizing:border-box}
.row{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}button{margin-top:16px;background:#ff4fd8;color:#000;font-weight:700;border:0;padding:12px 24px;border-radius:8px;cursor:pointer}
pre{background:#000;padding:10px;border-radius:6px;max-height:260px;overflow:auto;font-size:12px}.clip{display:flex;gap:16px;margin:16px 0;background:#1b1530;padding:12px;border-radius:8px}
video{max-height:420px;border-radius:6px}.meta{white-space:pre-wrap;font-size:13px}a{color:#43ffef}</style></head><body>
<h1>Narrated clips</h1><p><a href="/">&larr; back to NeonClipping</a> &middot; Gemini writes the voiceover, the game audio is ducked, captions follow the narration.</p>
<label>Video link or local file</label><input id="source" placeholder="https://www.youtube.com/watch?v=...">
<div class="row"><div><label>Shorts (9:16)</label><input id="shorts" type="number" value="3" min="0" max="8"></div>
<div><label>Min length (s)</label><input id="min_len" type="number" value="25"></div><div><label>Max length (s)</label><input id="max_len" type="number" value="50"></div></div>
<div class="row"><div><label>Tone</label><select id="tone"><option>hype</option><option>storyteller</option><option>news</option></select></div>
<div><label>Voice</label><select id="voice"><option>en-US-GuyNeural</option><option>en-US-ChristopherNeural</option><option>en-US-AndrewMultilingualNeural</option><option>en-US-EricNeural</option><option>en-GB-RyanNeural</option><option>en-NG-AbeoNeural</option></select></div>
<div><label>Game audio</label><select id="audio"><option value="duck">Ducked under voice</option><option value="mute">Muted</option></select></div></div>
<div class="row"><div><label>Game audio level (0-1)</label><input id="duck" type="number" step="0.05" value="0.15"></div>
<div><label>Fill (1 = full screen)</label><input id="fill" type="number" step="0.05" value="1"></div>
<div><label><input id="long" type="checkbox" style="width:auto"> Also make long-form 16:9</label></div></div>
<button onclick="go()">Make narrated clips</button><pre id="log"></pre><div id="out"></div>
<script>
async function go(){const v=id=>document.getElementById(id).value;const body={source:v('source'),shorts:+v('shorts'),min_len:+v('min_len'),max_len:+v('max_len'),tone:v('tone'),voice:v('voice'),audio:v('audio'),duck:+v('duck'),fill:+v('fill'),long:document.getElementById('long').checked};
const r=await fetch('/narrate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const j=await r.json();if(!r.ok){alert(j.detail);return}poll(j.job)}
async function poll(job){const r=await fetch('/narrate/'+job);const j=await r.json();document.getElementById('log').textContent=j.log.join('\\n')+(j.error?'\\nERROR: '+j.error:'');
if(j.status==='running'){setTimeout(()=>poll(job),2000);return}
document.getElementById('out').innerHTML=(j.results||[]).map(c=>`<div class="clip"><video controls src="/narrate-file?path=${encodeURIComponent(c.file)}"></video><div class="meta"><b>${c.title}</b>\\n\\n${c.description||''}\\n\\n${(c.youtube_hashtags||[]).join(' ')}\\n${c.tiktok_caption?'TikTok: '+c.tiktok_caption+' '+(c.tiktok_hashtags||[]).join(' '):''}\\n<a href="/narrate-file?path=${encodeURIComponent(c.file)}" download>Download MP4</a></div></div>`).join('')}
</script></body></html>'''
