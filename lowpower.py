"""Low-power profile for small ARM boards (Arduino UNO Q, Raspberry Pi).

Auto-on for aarch64 / 32-bit ARM with 4 or fewer cores, or 4 GB-class RAM.
Force with NEONCLIPPING_LOW_POWER=1 (or narrate.py --low-power). Force off with
NEONCLIPPING_LOW_POWER=0 (or --no-low-power). Desktops are unchanged: every helper
below returns the caller's own default when the profile is off.
"""
import os
import platform

_ARM = ('aarch64', 'arm64', 'armv7l', 'armv8l')


def enabled():
    flag = os.environ.get('NEONCLIPPING_LOW_POWER', '').strip().lower()
    if flag in ('1', 'true', 'yes', 'on'):
        return True
    if flag in ('0', 'false', 'no', 'off'):
        return False
    if platform.machine().lower() not in _ARM:
        return False
    cores = os.cpu_count() or 1
    ram = _ram_bytes()
    return cores <= 4 or (ram > 0 and ram <= 5 * 1024 ** 3)


def _ram_bytes():
    try:
        with open('/proc/meminfo', encoding='utf-8') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def threads():
    """ffmpeg / x264 thread cap. 0 means do not pass -threads (desktop default)."""
    if not enabled():
        return 0
    return max(1, min(2, os.cpu_count() or 1))


def thread_args():
    n = threads()
    return ['-threads', str(n)] if n else []


def workers(default):
    """Cap a thread pool so a 2 GB board does not swap."""
    if not enabled():
        return default
    return 1


def x264_preset(requested):
    if enabled():
        return 'ultrafast'
    return requested


def asr_model(language=None):
    """Local speech model when YouTube captions are missing. int8 is chosen by the loader."""
    if language in ('hi', 'hinglish'):
        return 'base'
    return 'tiny'


def cap_download(height, touched=False):
    """720p instead of the 1080p default. An explicit other height, or source_touched, is kept."""
    try:
        height = int(height)
    except (TypeError, ValueError):
        height = 1080
    if enabled() and not touched and height == 1080:
        return 720
    return height


def tune_output(body):
    """720p30 instead of the factory 1080p / source-fps defaults. A touched choice is kept."""
    body = dict(body or {})
    if not enabled() or body.get('output_touched'):
        return body
    try:
        res = int(body.get('resolution') or 1080)
    except (TypeError, ValueError):
        res = 1080
    try:
        fps = int(body.get('fps') or 0)
    except (TypeError, ValueError):
        fps = 0
    if res == 1080:
        body['resolution'] = 720
    if fps == 0:
        body['fps'] = 30
    return body


def short_size():
    return (720, 1280) if enabled() else (1080, 1920)


def long_size():
    return (1280, 720) if enabled() else (1920, 1080)


def short_fps(requested):
    return 30 if enabled() and requested is None else (60 if requested is None else requested)


def gemini_limits(duration, kind):
    """(step_seconds, jpeg_width, jpeg_q) for one batched ffmpeg grab. Desktop values stay wide."""
    if kind == 'overview':
        step, width, q = max(2.0, duration / 150), 384, 5
        if enabled():
            step, width, q = max(4.0, duration / 32), 256, 8
        return step, width, q
    step = 1.5 if duration <= 70 else max(1.5, duration / 45)
    width, q = 512, 5
    if enabled():
        step, width, q = max(3.0, duration / 10), 256, 8
    return step, width, q
