#!/usr/bin/env bash
# Install NeonClipping in this folder. Does not download the app and does not create another NeonClipping directory.
# System packages (Debian / Ubuntu / Arduino UNO Q): ffmpeg python3 python3-pip python3-venv
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"

say() { printf '\n==> %s\n' "$1"; }

if [[ ! -f "$ROOT/app.py" || ! -f "$ROOT/requirements.txt" ]]; then
  echo "Run ./install.sh from the cloned NeonClipping folder (app.py is missing)."
  exit 1
fi

say "Installing in $ROOT"

pick_python() {
  local c
  for c in python3.12 python3.11 python3.13 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)' 2>/dev/null; then
      printf '%s' "$c"
      return 0
    fi
  done
  return 1
}

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1 || ! pick_python >/dev/null; then
  if command -v apt-get >/dev/null 2>&1; then
    echo "Installing system packages: ffmpeg python3 python3-pip python3-venv"
    if ! sudo -n true 2>/dev/null; then
      echo "Need sudo once for: sudo apt-get install -y ffmpeg python3 python3-pip python3-venv"
    fi
    sudo apt-get update
    sudo apt-get install -y ffmpeg python3 python3-pip python3-venv
  else
    echo "Install these, then run ./install.sh again:"
    echo "  Debian/Ubuntu: sudo apt-get install -y ffmpeg python3 python3-pip python3-venv"
    echo "  macOS: brew install ffmpeg python@3.12"
    exit 1
  fi
fi

PY="$(pick_python || true)"
if [[ -z "$PY" ]]; then
  echo "Need Python 3.11, 3.12 or 3.13. On Debian: sudo apt-get install -y python3 python3-pip python3-venv"
  exit 1
fi
if ! "$PY" -c 'import venv' 2>/dev/null; then
  echo "The venv module is missing. On Debian: sudo apt-get install -y python3-venv"
  exit 1
fi

say "Python packages ($("$PY" --version)) into $ROOT/.venv"
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  "$PY" -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/python" -m pip install --upgrade pip
"$ROOT/.venv/bin/python" -m pip install -r "$ROOT/requirements.txt"

echo
echo "Installed in $ROOT"
echo "Run the app:   ./run.sh"
echo "Narrated clip: .venv/bin/python narrate.py <url> --shorts 3 --long"
echo "Put keys in .env (GEMINI_API_KEY for narrated clips). Nothing was copied to another folder."
