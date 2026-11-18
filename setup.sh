#!/bin/bash
# One-time setup: creates the virtualenv and installs dependencies.
set -e
cd "$(dirname "$0")"

if ! command -v ffmpeg >/dev/null; then
  echo "ffmpeg not found. Install it first:  brew install ffmpeg"
  exit 1
fi

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
echo
echo "Done. Start the web UI with:  ./run.sh"
