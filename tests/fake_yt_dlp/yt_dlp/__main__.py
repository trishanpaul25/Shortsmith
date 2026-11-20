"""A tiny stand-in for yt-dlp, for offline tests only. Reads FAKE_SOURCE (a local video file)."""
import json, os, subprocess, sys, time
from pathlib import Path

args = sys.argv[1:]
SRC = os.environ["FAKE_SOURCE"]
url = args[-1]
log = Path(os.environ.get("FAKE_LOG", "/tmp/fake_ytdlp.log"))
with log.open("a") as f:
    f.write(" ".join(args[:-1]) + "\n")

if "-J" in args:
    print(json.dumps({"title": "Fake VOD", "duration": 90.0, "is_live": False, "formats": []}))
    sys.exit(0)

out = Path(args[args.index("-o") + 1])
folder = out.parent
if "--download-sections" in args:                      # only the requested seconds
    a, b = args[args.index("--download-sections") + 1].lstrip("*").split("-")
    dest = folder / "seg.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", a, "-t", str(float(b) - float(a)), "-i", SRC,
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(dest)], check=True)
    sys.exit(0)

# audio-only download, in 20 slow steps, resumable through a .part file
part = folder / "audio.m4a.part"
done = int(part.read_text()) if part.exists() else 0
if done:
    print(f"[download] Resuming download at step {done}", flush=True)
for i in range(done, 20):
    time.sleep(0.2)
    part.write_text(str(i + 1))
    print(f"[download]  {(i + 1) * 5:.1f}% of 3.00MiB at 1.00MiB/s", flush=True)
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", SRC, "-vn", "-c:a", "aac", "-b:a", "64k",
                str(folder / "audio.m4a")], check=True)
part.unlink()
