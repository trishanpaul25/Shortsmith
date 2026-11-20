@echo off
cd /d "%~dp0"
where ffmpeg >nul 2>nul || echo WARNING: ffmpeg was not found. Install it first: winget install ffmpeg  (then reopen this window)
if not exist venv (
  echo Setting up for the first time. This can take a few minutes...
  python -m venv venv
)
call venv\Scripts\activate
pip install -r requirements.txt -q
python app.py
pause
