#!/usr/bin/env bash
cd "$(dirname "$0")"
command -v ffmpeg >/dev/null || echo "WARNING: ffmpeg was not found. Install it first (Mac: brew install ffmpeg, Linux: sudo apt install ffmpeg)."
if [ ! -d venv ]; then
  echo "Setting up for the first time. This can take a few minutes..."
  python3 -m venv venv
fi
source venv/bin/activate
pip install -r requirements.txt -q
python app.py
