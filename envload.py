"""Load this folder's .env into the environment. Existing variables win. Empty values are skipped so code defaults stay in effect. Never writes a file."""
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parent


def _parse(text):
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].strip()
        if '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load(path=None):
    path = Path(path) if path else ROOT / '.env'
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return False
    try:
        from io import StringIO
        from dotenv import dotenv_values
        parsed = dotenv_values(stream=StringIO(text))
    except ImportError:
        parsed = _parse(text)
    if not parsed:
        parsed = _parse(text)
    for key, value in parsed.items():
        if not key or value is None or value == '' or key in os.environ:
            continue
        os.environ[key] = value
    return True
