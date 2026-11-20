"""
uploader.py - posts finished clips to YouTube and Instagram.

YouTube    Google OAuth (you create a free "Desktop app" client in Google Cloud, see README),
           then the YouTube Data API v3 resumable upload.
Instagram  Instagram Graph API "content publishing" for Reels, using the resumable upload
           through rupload.facebook.com. Needs an Instagram Professional (Business/Creator)
           account and an access token. See README.

Credentials are saved in data/credentials.json on this computer only. They are never sent
anywhere except to Google / Meta, and are never returned to the web page.
"""
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

from control import ClipError

# base URLs live here so tests can point them at a fake server
YT_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
YT_TOKEN = "https://oauth2.googleapis.com/token"
YT_UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
YT_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
IG_VERSION = os.environ.get("IG_API_VERSION", "v25.0")
IG_HOSTS = {"facebook": "https://graph.facebook.com", "instagram": "https://graph.instagram.com"}


class UploadError(ClipError):
    """Something went wrong while posting. The message is safe to show."""


def _api_message(r):
    try:
        e = r.json().get("error")
        if isinstance(e, dict):
            return e.get("message") or json.dumps(e)[:300]
        if e:
            return f"{e}: {r.json().get('error_description', '')}"[:300]
    except ValueError:
        pass
    return (r.text or "")[:300] or f"HTTP {r.status_code}"


def _check(r, what):
    if r.status_code >= 400:
        raise UploadError(f"{what} failed (HTTP {r.status_code}): {_api_message(r)}")
    return r


# ---------------------------------------------------------------- saved credentials
class Credentials:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def _read(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def get(self, section):
        with self.lock:
            return dict(self._read().get(section, {}))

    def update(self, section, **fields):
        with self.lock:
            d = self._read()
            d.setdefault(section, {}).update(fields)
            self._write(d)

    def clear(self, section):
        with self.lock:
            d = self._read()
            d.pop(section, None)
            self._write(d)

    def _write(self, d):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)                 # owner-only on Mac/Linux
        except OSError:
            pass
        tmp.replace(self.path)


# ---------------------------------------------------------------- YouTube
class YouTube:
    def __init__(self, creds):
        self.creds = creds
        self._states = {}                        # OAuth "state" values waiting for the redirect back
        self._token = ("", 0.0)

    def status(self):
        c = self.creds.get("youtube")
        return {"configured": bool(c.get("client_id") and c.get("client_secret")),
                "connected": bool(c.get("refresh_token"))}

    def save_client(self, client_id, client_secret):
        self.creds.update("youtube", client_id=client_id.strip(), client_secret=client_secret.strip())

    def disconnect(self):
        self.creds.clear("youtube")
        self._token = ("", 0.0)

    def auth_url(self, redirect_uri):
        c = self.creds.get("youtube")
        if not (c.get("client_id") and c.get("client_secret")):
            raise UploadError("Save your Google client ID and secret first.")
        state = secrets.token_urlsafe(24)
        self._states = {s: t for s, t in self._states.items() if time.time() - t < 600}
        self._states[state] = time.time()
        return YT_AUTH + "?" + urlencode({
            "client_id": c["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
            "scope": YT_SCOPE, "access_type": "offline", "prompt": "consent", "state": state})

    def finish(self, code, state, redirect_uri):
        if self._states.pop(state, None) is None:
            raise UploadError("The sign-in link expired or did not come from this app. Try Connect again.")
        c = self.creds.get("youtube")
        r = _check(requests.post(YT_TOKEN, data={
            "code": code, "client_id": c["client_id"], "client_secret": c["client_secret"],
            "redirect_uri": redirect_uri, "grant_type": "authorization_code"}, timeout=30),
            "Google sign-in")
        tok = r.json()
        if not tok.get("refresh_token"):
            raise UploadError("Google did not give a long-lived login. Remove Shortsmith at "
                              "myaccount.google.com/permissions and connect again.")
        self.creds.update("youtube", refresh_token=tok["refresh_token"])
        self._token = (tok.get("access_token", ""), time.time() + int(tok.get("expires_in", 0)) - 60)

    def _access_token(self):
        tok, exp = self._token
        if tok and time.time() < exp:
            return tok
        c = self.creds.get("youtube")
        if not c.get("refresh_token"):
            raise UploadError("YouTube is not connected. Connect it in the Accounts section.")
        r = requests.post(YT_TOKEN, data={
            "client_id": c["client_id"], "client_secret": c["client_secret"],
            "refresh_token": c["refresh_token"], "grant_type": "refresh_token"}, timeout=30)
        if r.status_code >= 400:
            raise UploadError("YouTube login expired. Press Connect in the Accounts section again. "
                              "(If your Google project is in 'Testing' mode, logins expire after 7 days.) "
                              + _api_message(r))
        j = r.json()
        self._token = (j["access_token"], time.time() + int(j.get("expires_in", 3600)) - 60)
        return self._token[0]

    def upload(self, path, title, description, tags, privacy, progress=None):
        path = Path(path)
        size = path.stat().st_size
        auth = {"Authorization": f"Bearer {self._access_token()}"}
        meta = {"snippet": {"title": title[:100] or "Clip", "description": description[:4900],
                            "tags": [t.lstrip("#") for t in tags][:15],
                            "categoryId": os.environ.get("YT_CATEGORY", "22")},
                "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
        init = _check(requests.post(
            YT_UPLOAD, params={"uploadType": "resumable", "part": "snippet,status"},
            headers={**auth, "Content-Type": "application/json; charset=UTF-8",
                     "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"},
            data=json.dumps(meta), timeout=60), "Starting the YouTube upload")
        loc = init.headers.get("Location")
        if not loc:
            raise UploadError("YouTube did not return an upload address.")
        if progress:
            progress("Uploading to YouTube")
        with open(path, "rb") as f:
            r = _check(requests.put(loc, data=f, headers={**auth, "Content-Type": "video/mp4",
                                                         "Content-Length": str(size)},
                                    timeout=(30, 3600)), "The YouTube upload")
        vid = r.json().get("id")
        if not vid:
            raise UploadError("YouTube accepted the file but did not return a video id.")
        return f"https://youtu.be/{vid}"


# ---------------------------------------------------------------- Instagram
class Instagram:
    def __init__(self, creds):
        self.creds = creds

    def status(self):
        c = self.creds.get("instagram")
        return {"connected": bool(c.get("token") and c.get("user_id")),
                "user_id": c.get("user_id", ""), "host": c.get("host", "facebook")}

    def save(self, token, user_id, host):
        if host not in IG_HOSTS:
            raise UploadError("Unknown Instagram login type.")
        self.creds.update("instagram", token=token.strip(), user_id=user_id.strip(), host=host)

    def disconnect(self):
        self.creds.clear("instagram")

    def upload(self, path, caption, progress=None):
        c = self.creds.get("instagram")
        if not (c.get("token") and c.get("user_id")):
            raise UploadError("Instagram is not connected. Add your token in the Accounts section.")
        path = Path(path)
        size = path.stat().st_size
        base = f"{IG_HOSTS.get(c.get('host', 'facebook'), IG_HOSTS['facebook'])}/{IG_VERSION}"
        bearer = {"Authorization": f"Bearer {c['token']}"}

        r = _check(requests.post(f"{base}/{c['user_id']}/media", headers=bearer, data={
            "media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200]},
            timeout=60), "Creating the Instagram upload")
        j = r.json()
        container, uri = j.get("id"), j.get("uri")
        if not container or not uri:
            raise UploadError("Instagram did not return an upload address.")

        if progress:
            progress("Uploading to Instagram")
        with open(path, "rb") as f:
            _check(requests.post(uri, data=f, timeout=(30, 3600), headers={
                "Authorization": f"OAuth {c['token']}", "offset": "0", "file_size": str(size)}),
                "The Instagram upload")

        if progress:
            progress("Instagram is processing the video")
        deadline = time.time() + 15 * 60
        while True:
            s = _check(requests.get(f"{base}/{container}", headers=bearer,
                                    params={"fields": "status_code,status"}, timeout=30),
                       "Checking the Instagram upload").json()
            code = s.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise UploadError(f"Instagram rejected the video ({code}): {s.get('status', '')}")
            if time.time() > deadline:
                raise UploadError("Instagram is taking too long to process the video. Check the app later.")
            time.sleep(float(os.environ.get("IG_POLL_SECONDS", "5")))

        pub = _check(requests.post(f"{base}/{c['user_id']}/media_publish", headers=bearer,
                                   data={"creation_id": container}, timeout=60),
                     "Publishing on Instagram").json()
        media_id = pub.get("id")
        try:
            link = requests.get(f"{base}/{media_id}", headers=bearer,
                                params={"fields": "permalink"}, timeout=30).json().get("permalink")
        except Exception:
            link = None
        return link or "https://www.instagram.com/"


# ---------------------------------------------------------------- helpers for the app
def caption_for(clip, target):
    parts = [clip.get("title", ""), clip.get("description", ""), " ".join(clip.get("hashtags") or [])]
    text = "\n\n".join(p for p in parts if p)
    if target == "youtube" and clip.get("ratio", "9:16") != "16:9" and "#shorts" not in text.lower():
        text += "\n\n#Shorts"
    return text
