"""
Integration test of the website API with fake yt-dlp, stubbed Whisper and fake YouTube/Instagram servers.
Run:  FAKE_SOURCE=/path/to/video.mp4 python tests/test_api.py
"""
import os, sys, tempfile, threading, time, json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
os.environ["PYTHONPATH"] = str(HERE / "fake_yt_dlp") + os.pathsep + os.environ.get("PYTHONPATH", "")
os.environ["FAKE_LOG"] = tempfile.mktemp()
os.environ["IG_POLL_SECONDS"] = "0.2"
assert os.environ.get("FAKE_SOURCE"), "set FAKE_SOURCE"

import app as appmod
import clipper, uploader
from flask import Flask, request, jsonify

clipper.transcribe = lambda video, start, end, m, lang, log, ctl: [
    {"start": (start or 0) + i * 0.5, "end": (start or 0) + i * 0.5 + 0.4, "word": "go"} for i in range(40)]

# ---- fake Google + Meta servers
mock = Flask("mock"); hits = []
@mock.post("/token")
def tok():
    hits.append(("token", request.form.get("grant_type")))
    return jsonify(access_token="at", expires_in=3600, refresh_token="rt")
@mock.post("/yt/upload")
def yt_init():
    meta = request.get_json(force=True)
    hits.append(("yt_init", meta["status"]["privacyStatus"], meta["snippet"]["title"], request.headers["Authorization"]))
    r = jsonify({}); r.headers["Location"] = f"http://127.0.0.1:{PORT}/yt/put"; return r
@mock.put("/yt/put")
def yt_put():
    n = len(request.get_data()); hits.append(("yt_put", n > 1000)); return jsonify(id="VID123")
@mock.post("/v25.0/<uid>/media")
def ig_media(uid):
    hits.append(("ig_media", request.form["media_type"], request.form["upload_type"], "Caption" if request.form.get("caption") else ""))
    return jsonify(id="C1", uri=f"http://127.0.0.1:{PORT}/rupload/C1")
@mock.post("/rupload/<cid>")
def ig_up(cid):
    hits.append(("ig_up", request.headers["Authorization"].startswith("OAuth "), request.headers["offset"], len(request.get_data()) > 1000))   # (werkzeug hides the file_size header: it has an underscore)
    return jsonify(success=True)
polls = {"n": 0}
@mock.get("/v25.0/C1")
def ig_status():
    polls["n"] += 1
    return jsonify(status_code="FINISHED" if polls["n"] > 1 else "IN_PROGRESS")
@mock.post("/v25.0/<uid>/media_publish")
def ig_pub(uid):
    hits.append(("ig_publish", request.form["creation_id"])); return jsonify(id="M1")
@mock.get("/v25.0/M1")
def ig_link(): return jsonify(permalink="https://instagram.com/reel/M1")
import socket
s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
threading.Thread(target=lambda: mock.run(port=PORT, threaded=True), daemon=True).start()
time.sleep(1)
uploader.YT_TOKEN = f"http://127.0.0.1:{PORT}/token"
uploader.YT_UPLOAD = f"http://127.0.0.1:{PORT}/yt/upload"
uploader.YT_AUTH = f"http://127.0.0.1:{PORT}/auth"
uploader.IG_HOSTS = {"facebook": f"http://127.0.0.1:{PORT}", "instagram": f"http://127.0.0.1:{PORT}"}

appmod.load_jobs()
c = appmod.app.test_client()

def wait(jid, cond, timeout=150):
    t = time.time()
    while time.time() - t < timeout:
        j = c.get(f"/api/jobs/{jid}").get_json()
        if cond(j): return j
        time.sleep(0.15)
    raise AssertionError("timeout; job=" + json.dumps({k: j[k] for k in ("state", "percent", "step", "error")}))

def new_job(**extra):
    r = c.post("/api/jobs", data={"url": "https://fake.example/watch?v=1", "clips": "2", "length": "15", **extra})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]

# ---- 1. pause during the download, then continue
jid = new_job()
wait(jid, lambda j: j["state"] == "running" and j["percent"] > 8)
assert c.post(f"/api/jobs/{jid}/pause").status_code == 200
j = wait(jid, lambda j: j["state"] == "paused", 10)
part = list(appmod.CACHE_DIR.glob("src_*/audio.m4a.part"))
print("paused at", j["percent"], "% - partial file kept:", bool(part))
assert part
assert c.delete("/api/jobs/" + jid).status_code == 200      # delete the paused job
# (start another one so the next steps use a paused job we can continue)
jid = new_job()
wait(jid, lambda j: j["state"] == "running" and j["percent"] > 8)
c.post(f"/api/jobs/{jid}/pause"); wait(jid, lambda j: j["state"] == "paused", 10)
assert c.post(f"/api/jobs/{jid}/resume").status_code == 200
j = wait(jid, lambda j: j["state"] == "done")
logtxt = open(os.environ["FAKE_LOG"]).read()
print("done after continue; clips:", len(j["clips"]), "live:", j["live"], "title:", j["title"])
assert len(j["clips"]) == 2 and j["title"] == "Fake VOD"
sections = [l for l in logtxt.splitlines() if "--download-sections" in l]
print("video downloads were only these ranges:", [l.split("--download-sections ")[1].split()[0] for l in sections])
assert len(sections) == 2 and not any("--download-sections" not in l and "bv*" in l for l in logtxt.splitlines())

# ---- 2. pause then DELETE removes the partial download
shutil_dir = list(appmod.CACHE_DIR.glob("src_*"))
for d in shutil_dir: __import__("shutil").rmtree(d)
jid2 = new_job()
wait(jid2, lambda j: j["state"] == "running" and j["percent"] > 8)
assert c.delete(f"/api/jobs/{jid2}").status_code == 409          # cannot delete while running
c.post(f"/api/jobs/{jid2}/pause"); wait(jid2, lambda j: j["state"] == "paused", 10)
assert c.delete(f"/api/jobs/{jid2}").status_code == 200
assert c.get(f"/api/jobs/{jid2}").status_code == 404
assert not list(appmod.CACHE_DIR.glob("src_*/audio.m4a.part")), "partial download should be removed"
print("pause -> delete: job and partial download removed")

# ---- 3. accounts + manual uploads
assert c.post("/api/jobs/%s/clips/1/upload" % jid, json={"target": "youtube"}).status_code == 400   # not connected
assert c.post("/api/settings/youtube", json={"client_id": "cid", "client_secret": "sec"}).status_code == 200
r = c.get("/auth/youtube/start"); assert r.status_code == 302
from urllib.parse import urlparse, parse_qs
q = parse_qs(urlparse(r.headers["Location"]).query)
assert q["scope"][0].endswith("youtube.upload") and q["access_type"] == ["offline"] and q["client_id"] == ["cid"]
bad = c.get("/auth/youtube/callback?code=x&state=forged"); assert "yt=error" in bad.headers["Location"]   # CSRF check
ok = c.get(f"/auth/youtube/callback?code=abc&state={q['state'][0]}"); assert "yt=ok" in ok.headers["Location"]
assert c.get("/api/settings").get_json()["youtube"]["connected"]
assert "rt" not in json.dumps(c.get("/api/settings").get_json()), "secrets must never be returned"
assert c.post("/api/settings/instagram", json={"token": "tok", "user_id": "17841400000000000", "host": "facebook"}).status_code == 200
assert c.post("/api/settings/instagram", json={"token": "tok", "user_id": "abc"}).status_code == 400

assert c.post(f"/api/jobs/{jid}/clips/1/upload", json={"target": "youtube", "privacy": "unlisted"}).status_code == 202
assert c.post(f"/api/jobs/{jid}/clips/2/upload", json={"target": "instagram"}).status_code == 202
j = wait(jid, lambda j: all(c_.get("uploads", {}).get(t, {}).get("state") in ("done", "error") for c_, t in ((j["clips"][0], "youtube"), (j["clips"][1], "instagram"))))
print("uploads:", j["clips"][0]["uploads"], j["clips"][1]["uploads"])
assert j["clips"][0]["uploads"]["youtube"]["url"] == "https://youtu.be/VID123"
assert j["clips"][1]["uploads"]["instagram"]["url"] == "https://instagram.com/reel/M1"
assert c.post(f"/api/jobs/{jid}/clips/1/upload", json={"target": "youtube"}).status_code == 409   # no double posting
first = hits[[h[0] for h in hits].index("yt_init")]
assert first[1] == "unlisted" and first[3] == "Bearer at"

# ---- 4. auto-upload when the job finishes
jid3 = new_job(auto_youtube="on", auto_instagram="on", yt_privacy="private")
j = wait(jid3, lambda j: j["state"] == "done" and all(
    cl.get("uploads", {}).get(t, {}).get("state") == "done" for cl in j["clips"] for t in ("youtube", "instagram")))
print("auto-upload finished for", len(j["clips"]), "clips x 2 sites")
assert [h for h in hits if h[0] == "yt_init" and h[1] == "private"]
c.delete("/api/settings/youtube"); assert not c.get("/api/settings").get_json()["youtube"]["connected"]
r = c.post("/api/jobs", data={"url": "https://fake.example/x", "auto_youtube": "on"}); assert r.status_code == 400
print("ALL API TESTS OK")
