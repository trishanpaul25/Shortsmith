"""Runs the real website on port 5055 with fake yt-dlp and fake Whisper, for browser tests."""
import os, sys, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ["PYTHONPATH"] = str(HERE / "fake_yt_dlp") + os.pathsep + os.environ.get("PYTHONPATH", "")
os.environ["FAKE_LOG"] = tempfile.mktemp()
import app as appmod, clipper
clipper.transcribe = lambda video, start, end, m, lang, log, ctl: [
    {"start": (start or 0) + i * 0.5, "end": (start or 0) + i * 0.5 + 0.4, "word": "go"} for i in range(40)]
appmod.load_jobs()
appmod.app.run(host="127.0.0.1", port=5055, threaded=True)
