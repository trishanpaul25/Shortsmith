#!/usr/bin/env python3
"""
app.py - the Shortsmith website. Run it, then open http://127.0.0.1:5000

Jobs run one at a time (video work is heavy), in a background thread. A job can be paused
(the download stops and later carries on from where it was), continued, or deleted.
Everything is saved in the ./data folder:
  data/jobs/<id>/       the finished clips + job.json for each job
  data/cache/           downloaded audio of videos (small; safe to delete)
  data/credentials.json YouTube / Instagram logins, if you connected them (stays on this computer)
"""
import io
import json
import os
import queue
import re
import shutil
import threading
import time
import traceback
import uuid
import webbrowser
import zipfile
from pathlib import Path
from urllib.parse import urlparse, quote

import requests
from flask import (Flask, abort, jsonify, redirect, render_template, request, send_file,
                   send_from_directory)
from werkzeug.utils import secure_filename

import clipper
import sources
import uploader
from control import Cancelled, ClipError, Control, Paused

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
JOBS_DIR = DATA / "jobs"
CACHE_DIR = DATA / "cache"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 ** 3      # allow uploads up to 8 GB

jobs = {}                    # id -> job dict (saved to disk as job.json, never holds API keys)
controls = {}                # id -> Control for the job that is running right now
secrets = {}                 # id -> LLM api key, in memory only
work = queue.Queue()
upload_q = queue.Queue()
lock = threading.RLock()
_last_save = {}

creds = uploader.Credentials(DATA / "credentials.json")
youtube = uploader.YouTube(creds)
instagram = uploader.Instagram(creds)
TARGETS = ("youtube", "instagram")


# ---------------------------------------------------------------- saving / loading
def save(job, force=False):
    now = time.time()
    if not force and now - _last_save.get(job["id"], 0) < 0.8:
        return
    _last_save[job["id"]] = now
    folder = JOBS_DIR / job["id"]
    if not folder.exists():                      # deleted while we were saving
        return
    with lock:
        text = json.dumps(job, indent=1)
    tmp = folder / "job.json.tmp"
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(folder / "job.json")


def load_jobs():
    for f in JOBS_DIR.glob("*/job.json"):
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if job.get("state") in ("queued", "running"):       # the app was closed mid-job
            job["state"] = "paused"
            job["pausing"] = False
            job["step"] = "Stopped because the app was closed. Press Continue to carry on."
            save(job, force=True)
        for c in job.get("clips") or []:                     # an upload cut short by closing
            for t, u in (c.get("uploads") or {}).items():
                if u.get("state") in ("queued", "uploading"):
                    c["uploads"][t] = {"state": "error", "error": "The app was closed during the upload."}
        jobs[job["id"]] = job


def update(job, force=False, **fields):
    with lock:
        job.update(fields)
    save(job, force)


# ---------------------------------------------------------------- the worker
def worker():
    while True:
        jid = work.get()
        job = jobs.get(jid)
        if not job or job["state"] != "queued":      # deleted or paused while waiting
            continue
        ctl = controls[jid] = Control()
        try:
            update(job, force=True, state="running", pausing=False, error=None, step="Starting")

            def log(msg):
                with lock:
                    job["log"] = (job["log"] + [msg])[-120:]
                save(job)

            def progress(pct, stage, msg=""):
                update(job, percent=round(pct, 1), stage=stage, step=msg or job["step"])

            def meta(**k):
                keep = {a: b for a, b in k.items() if a in ("title", "live", "duration") and b}
                if keep:
                    update(job, force=True, **keep)

            o = job["options"]
            opts = clipper.Options(clips=o["clips"], length=o["length"], picker=o["picker"],
                                   layout=o["layout"], ratio=o.get("ratio", "9:16"),
                                   model=o["model"], lang=o["lang"], api_key=secrets.get(jid))
            result = clipper.process(job["source"], JOBS_DIR / jid, opts, log=log,
                                     progress=progress, cache_dir=CACHE_DIR, ctl=ctl, meta=meta)
            for c in result["clips"]:
                c["uploads"] = {}
            update(job, force=True, state="done", percent=100, stage="done", step="Done",
                   duration=result["duration"], live=result["live"], clips=result["clips"])
            auto_upload(job)
        except Paused:
            update(job, force=True, state="paused", pausing=False,
                   step=f"Paused at {int(job.get('percent') or 0)}%. Press Continue to carry on.")
        except Cancelled:
            pass                                       # the job was deleted
        except ClipError as e:
            update(job, force=True, state="error", pausing=False, error=str(e))
        except Exception as e:                         # a bug or an unexpected crash
            tail = traceback.format_exc()[-1200:]
            update(job, force=True, state="error", pausing=False,
                   error=f"Unexpected problem: {e}\n\n{tail}")
        finally:
            controls.pop(jid, None)
            if job.get("state") != "paused":
                secrets.pop(jid, None)


threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------- uploads
def upload_worker():
    while True:
        jid, idx, target, privacy = upload_q.get()
        job = jobs.get(jid)
        if not job:
            continue
        clip = job["clips"][idx]
        path = JOBS_DIR / jid / clip["file"]

        def set_state(**s):
            with lock:
                clip.setdefault("uploads", {})[target] = s
            save(job, force=True)

        set_state(state="uploading", step="Starting")
        try:
            if not path.exists():
                raise ClipError("The clip file is missing.")
            step = lambda m: set_state(state="uploading", step=m)
            if target == "youtube":
                url = youtube.upload(path, clip["title"], uploader.caption_for(clip, "youtube"),
                                     clip.get("hashtags") or [], privacy, step)
            else:
                url = instagram.upload(path, uploader.caption_for(clip, "instagram"), step)
            set_state(state="done", url=url, privacy=privacy if target == "youtube" else None)
        except ClipError as e:
            set_state(state="error", error=str(e))
        except requests.RequestException as e:
            set_state(state="error", error=f"Network problem during the upload: {type(e).__name__}")
        except Exception as e:
            set_state(state="error", error=f"Unexpected problem: {e}")


threading.Thread(target=upload_worker, daemon=True).start()


def queue_upload(job, idx, target, privacy):
    with lock:
        job["clips"][idx].setdefault("uploads", {})[target] = {"state": "queued"}
    save(job, force=True)
    upload_q.put((job["id"], idx, target, privacy))


def auto_upload(job):
    o = job["options"]
    for target in o.get("auto") or []:
        ok = youtube.status()["connected"] if target == "youtube" else instagram.status()["connected"]
        if not ok:
            with lock:
                job["log"].append(f"Auto-upload to {target} skipped: it is not connected.")
            continue
        for i in range(len(job["clips"])):
            queue_upload(job, i, target, o.get("yt_privacy", "private"))


# ---------------------------------------------------------------- safety: only our own page may post
@app.before_request
def same_origin_only():
    if request.method in ("POST", "DELETE"):
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            abort(403)


# ---------------------------------------------------------------- pages and API
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify(ffmpeg=bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe")),
                   gemini_key=bool(os.environ.get("GEMINI_API_KEY")),
                   claude_key=bool(os.environ.get("ANTHROPIC_API_KEY")))


def public(job, full=False):
    d = {k: job.get(k) for k in ("id", "title", "state", "created", "stage", "percent", "step",
                                 "error", "duration", "options", "live", "pausing")}
    d["clip_count"] = len(job.get("clips") or [])
    if full:
        with lock:
            d["clips"] = json.loads(json.dumps(job.get("clips") or []))
            d["log"] = list(job.get("log") or [])
    return d


@app.get("/api/jobs")
def list_jobs():
    items = sorted(jobs.values(), key=lambda j: j["created"], reverse=True)
    return jsonify([public(j) for j in items])


@app.get("/api/jobs/<jid>")
def get_job(jid):
    job = jobs.get(jid) or abort(404)
    return jsonify(public(job, full=True))


def clamp(value, lo, hi, default):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


@app.post("/api/jobs")
def create_job():
    f = request.form
    picker = f.get("picker", "audio")
    layout = f.get("layout", "blur")
    ratio = f.get("ratio", "9:16")
    model = f.get("model", "base")
    lang = (f.get("lang") or "").strip().lower() or None
    api_key = (f.get("api_key") or "").strip()
    privacy = f.get("yt_privacy", "private")
    auto = [t for t in TARGETS if f.get(f"auto_{t}")]
    if picker not in ("audio", "gemini", "claude"):
        return jsonify(error="Unknown way of finding moments."), 400
    if layout not in ("blur", "crop"):
        return jsonify(error="Unknown layout."), 400
    if ratio not in clipper.RATIOS:
        return jsonify(error="Unknown format."), 400
    if model not in ("tiny", "base", "small", "medium"):
        return jsonify(error="Unknown caption model."), 400
    if privacy not in ("private", "unlisted", "public"):
        return jsonify(error="Unknown YouTube privacy setting."), 400
    if lang and not re.fullmatch(r"[a-z]{2,3}", lang):
        return jsonify(error="Language must be a short code like en or hi."), 400
    env_name = {"gemini": "GEMINI_API_KEY", "claude": "ANTHROPIC_API_KEY"}.get(picker)
    if env_name and not api_key and not os.environ.get(env_name):
        return jsonify(error="Add an API key to use this option, or pick 'Loudest moments'."), 400
    if "youtube" in auto and not youtube.status()["connected"]:
        return jsonify(error="Connect YouTube in the Accounts section before turning on auto-upload."), 400
    if "instagram" in auto and not instagram.status()["connected"]:
        return jsonify(error="Connect Instagram in the Accounts section before turning on auto-upload."), 400

    jid = uuid.uuid4().hex[:10]
    folder = JOBS_DIR / jid
    folder.mkdir(parents=True, exist_ok=True)

    upload = request.files.get("file")
    if upload and upload.filename:
        name = secure_filename(upload.filename) or "video.mp4"
        path = folder / f"upload_{name}"
        upload.save(path)
        source, title = str(path), upload.filename
    else:
        url = (f.get("url") or "").strip()
        if not re.match(r"https?://", url):
            shutil.rmtree(folder, ignore_errors=True)
            return jsonify(error="Paste a link that starts with http:// or https://"), 400
        source, title = url, url

    job = {"id": jid, "title": title, "source": source, "state": "queued",
           "created": time.time(), "stage": "get", "percent": 0, "pausing": False, "live": False,
           "step": "Waiting in line", "error": None, "log": [], "clips": [], "duration": None,
           "options": {"clips": clamp(f.get("clips"), 1, 20, 5),
                       "length": clamp(f.get("length"), 15, 90, 30),
                       "picker": picker, "layout": layout, "ratio": ratio, "model": model,
                       "lang": lang, "auto": auto, "yt_privacy": privacy}}
    jobs[jid] = job
    if api_key:
        secrets[jid] = api_key
    save(job, force=True)
    work.put(jid)
    return jsonify(public(job, full=True)), 201


# ---- pause / continue / delete
@app.post("/api/jobs/<jid>/pause")
def pause_job(jid):
    job = jobs.get(jid) or abort(404)
    with lock:
        state = job["state"]
        if state == "queued":
            job["state"], job["step"] = "paused", "Paused before it started. Press Continue."
        elif state == "running":
            ctl = controls.get(jid)
            if not ctl:
                return jsonify(error="The job is just starting. Try again in a moment."), 409
            job["pausing"], job["step"] = True, "Pausing..."
            ctl.request_pause()
        else:
            return jsonify(error="Only a running job can be paused."), 409
    save(job, force=True)
    return jsonify(public(job))


@app.post("/api/jobs/<jid>/resume")
def resume_job(jid):
    job = jobs.get(jid) or abort(404)
    with lock:
        if job["state"] != "paused":
            return jsonify(error="This job is not paused."), 409
        job["state"], job["step"], job["pausing"] = "queued", "Waiting in line", False
    save(job, force=True)
    work.put(jid)
    return jsonify(public(job))


@app.post("/api/jobs/<jid>/retry")
def retry_job(jid):
    """Retry a failed job from where it left off (like resume, but from an error state)."""
    job = jobs.get(jid) or abort(404)
    with lock:
        if job["state"] != "error":
            return jsonify(error="Only a failed job can be retried."), 409
        job["state"] = "queued"
        job["step"] = "Retrying..."
        job["error"] = None
        job["pausing"] = False
    save(job, force=True)
    work.put(jid)
    return jsonify(public(job))


@app.delete("/api/jobs/<jid>")
def delete_job(jid):
    job = jobs.get(jid) or abort(404)
    if job["state"] == "running":
        return jsonify(error="This job is still running. Pause it first, then delete it."), 409
    with lock:
        jobs.pop(jid, None)
        secrets.pop(jid, None)
    shutil.rmtree(JOBS_DIR / jid, ignore_errors=True)
    if re.match(r"https?://", job.get("source", "")):       # drop a half-finished download too
        sources.discard_partial(sources.cache_folder(CACHE_DIR, job["source"]))
    return jsonify(ok=True)


# ---- media
@app.get("/media/<jid>/<path:name>")
def media(jid, name):
    if jid not in jobs or not re.fullmatch(r"clip_\d+\.mp4", name):
        abort(404)
    return send_from_directory(JOBS_DIR / jid, name, conditional=True)


@app.get("/api/jobs/<jid>/zip")
def zip_job(jid):
    job = jobs.get(jid) or abort(404)
    if job["state"] != "done":
        abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for c in job["clips"]:
            z.write(JOBS_DIR / jid / c["file"], c["file"])
        z.write(JOBS_DIR / jid / "clips.json", "clips.json")
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"shortsmith_{jid}.zip")


# ---- uploads
@app.post("/api/jobs/<jid>/clips/<int:n>/upload")
def upload_clip(jid, n):
    job = jobs.get(jid) or abort(404)
    body = request.get_json(silent=True) or {}
    target, privacy = body.get("target"), body.get("privacy", "private")
    if target not in TARGETS:
        return jsonify(error="Unknown place to upload to."), 400
    if privacy not in ("private", "unlisted", "public"):
        return jsonify(error="Unknown privacy setting."), 400
    if job["state"] != "done" or not 1 <= n <= len(job["clips"]):
        return jsonify(error="That clip is not ready."), 404
    ok = youtube.status()["connected"] if target == "youtube" else instagram.status()["connected"]
    if not ok:
        return jsonify(error=f"Connect {target.capitalize()} in the Accounts section first."), 400
    clip = job["clips"][n - 1]
    state = (clip.get("uploads") or {}).get(target, {}).get("state")
    if state in ("queued", "uploading", "done") and not body.get("again"):
        return jsonify(error="This clip was already sent there."), 409
    queue_upload(job, n - 1, target, privacy)
    return jsonify(public(job, full=True)), 202


# ---- accounts
@app.get("/api/settings")
def get_settings():
    return jsonify(youtube=youtube.status(), instagram=instagram.status(),
                   redirect_uri=f"{request.host_url}auth/youtube/callback")


@app.post("/api/settings/youtube")
def set_youtube():
    b = request.get_json(silent=True) or {}
    cid, sec = (b.get("client_id") or "").strip(), (b.get("client_secret") or "").strip()
    if not cid or not sec:
        return jsonify(error="Enter both the client ID and the client secret."), 400
    youtube.save_client(cid, sec)
    return jsonify(youtube.status())


@app.post("/api/settings/instagram")
def set_instagram():
    b = request.get_json(silent=True) or {}
    token, uid = (b.get("token") or "").strip(), (b.get("user_id") or "").strip()
    if not token or not re.fullmatch(r"\d{5,25}", uid):
        return jsonify(error="Enter the access token and the numeric Instagram account ID."), 400
    try:
        instagram.save(token, uid, b.get("host") or "facebook")
    except uploader.UploadError as e:
        return jsonify(error=str(e)), 400
    return jsonify(instagram.status())


@app.delete("/api/settings/<service>")
def disconnect(service):
    if service == "youtube":
        youtube.disconnect()
    elif service == "instagram":
        instagram.disconnect()
    else:
        abort(404)
    return jsonify(ok=True)


@app.get("/auth/youtube/start")
def youtube_start():
    try:
        return redirect(youtube.auth_url(f"{request.host_url}auth/youtube/callback"))
    except uploader.UploadError as e:
        return redirect("/?yt=error&msg=" + quote(str(e)) + "#accounts")


@app.get("/auth/youtube/callback")
def youtube_callback():
    if request.args.get("error"):
        return redirect("/?yt=error&msg=" + quote("Google said: " + request.args["error"]) + "#accounts")
    try:
        youtube.finish(request.args.get("code", ""), request.args.get("state", ""),
                       f"{request.host_url}auth/youtube/callback")
    except (uploader.UploadError, requests.RequestException) as e:
        return redirect("/?yt=error&msg=" + quote(str(e)[:300]) + "#accounts")
    return redirect("/?yt=ok#accounts")


# ---------------------------------------------------------------- start
if __name__ == "__main__":
    load_jobs()
    port = int(os.environ.get("PORT", "5000"))
    url = f"http://127.0.0.1:{port}"
    print(f"\n  Shortsmith is running. Open {url}\n  Press Ctrl+C to stop.\n")
    if os.environ.get("NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)
