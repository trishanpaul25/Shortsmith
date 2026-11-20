"""
Offline test of the live-stream path. Serves a fake live HLS stream from a local folder,
stubs yt-dlp's probe and Whisper, then runs the real pipeline (ffmpeg and all).

  python tests/test_live.py /path/to/hls_folder     (folder with live.m3u8 + .ts files, no ENDLIST)
"""
import http.server
import json
import sys
import tempfile
import threading
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import clipper
import sources
from control import Control

hls_dir = Path(sys.argv[1])
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(http.server.SimpleHTTPRequestHandler, directory=str(hls_dir)))
srv.RequestHandlerClass.log_message = lambda *a, **k: None
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_port}/live.m3u8"

sources.probe = lambda u, ctl: sources.Info(
    title="Fake live stream", duration=0, is_live=True, headers={"User-Agent": "t"},
    formats=[{"protocol": "m3u8_native", "url": url, "acodec": "aac", "vcodec": "avc1", "height": 720, "tbr": 2000}])
# stub Whisper: pretend someone says a sentence every second inside the requested window
clipper.transcribe = lambda video, start, end, m, lang, log, ctl: [
    {"start": (start or 0) + i * 0.5, "end": (start or 0) + i * 0.5 + 0.4, "word": "word." if i % 6 == 5 else "word"}
    for i in range(int(((end if end is not None else 60) - (start or 0)) / 0.5))]

out = Path(tempfile.mkdtemp())
logs = []
res = clipper.process(sys.argv[2] if len(sys.argv) > 2 else "https://example.com/live",
                      out, clipper.Options(clips=2, length=20, ratio=sys.argv[3] if len(sys.argv) > 3 else "9:16"),
                      log=logs.append, progress=lambda p, s, m="": None, cache_dir=out / "cache")
print("\n".join(logs))
print(json.dumps(res, indent=1)[:900])
for c in res["clips"]:
    f = out / c["file"]
    import subprocess
    d = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height,codec_type", "-show_entries", "format=duration", "-of", "json", str(f)], capture_output=True, text=True).stdout
    print(c["file"], f.stat().st_size, json.loads(d)["streams"], json.loads(d)["format"]["duration"])
assert res["live"] and len(res["clips"]) == 2
# the loud part was 40-52s: at least one clip must overlap it
assert any(c["start_s"] < 52 and c["end_s"] > 40 for c in res["clips"]), "loud moment missed"
print("LIVE TEST OK", out)
