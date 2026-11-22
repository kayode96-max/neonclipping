#!/usr/bin/env bash
# Start NeonClipping from this folder. Install first with ./install.sh
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo "No .venv in this folder. Run ./install.sh first."
  exit 1
fi
exec .venv/bin/python app.py
