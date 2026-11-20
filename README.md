# Shortsmith

A small website that runs on your own computer. Paste a video link **or a live stream link** and it
makes captioned clips for Instagram Reels, YouTube Shorts, the Instagram feed, or normal YouTube.
It can also post them for you.

It does **not** download the whole video. It downloads a small audio-only track (a few MB per hour) to find
the best moments, then fetches only those few seconds of video.

## What you need
- Python 3.10 or newer
- ffmpeg (a separate program): Windows `winget install ffmpeg` (reopen the terminal), Mac `brew install ffmpeg`, Linux `sudo apt install ffmpeg`
- Node.js is **not** needed. It is only for editing the page's TypeScript (see "For developers").

## Start it
- Windows: double-click `start.bat`. Mac / Linux: run `./start.sh`.

The first start installs everything. Your browser opens at http://127.0.0.1:5000.

## How to use it
1. Paste a link (video or live stream), or choose "Upload a file".
2. Pick how many clips, how long, and the **clip format**:
   - Instagram Reels & YouTube Shorts: 9:16 (1080x1920). Both use the same shape.
   - Instagram feed 4:5, Square 1:1, Landscape 16:9.
3. Pick how to find moments: loudest moments (free) or Gemini / Claude reading the transcript (needs an API key).
4. Click **Make clips**. Watch them, then Download, or Post.

### Pause, Continue, Delete
While a job runs, **Pause** stops it. An audio download keeps its partial file and **Continue** picks up where it
stopped; clips already made are not redone. A paused job also offers **Delete**, which removes the job and the half-downloaded file.
If you close the app mid-job, the job shows as Paused and can be continued. Pausing during captioning or
cutting takes effect after the current step.

### Live streams
Paste the link of a stream that is on right now. Shortsmith reads the stream's HLS playlist, takes the audio of
what has been streamed so far, finds the best moments, and fetches only those segments.
- YouTube streams with rewind (DVR) on usually expose a long history. Some sites (Twitch, for example) only
  keep a few minutes available, so only that part can be used. For those, paste the VOD link after the stream.
- The stream is "frozen" when the job starts. Run it again later to catch newer moments.
- Live streams whose video and audio are separate feeds are not supported yet.

### Posting to YouTube and Instagram (optional)
Open **Accounts** on the page. Posting is off unless you connect an account and tick auto-post, or press a
clip's **Post** button (it asks first). Logins are saved in `data/credentials.json` on this computer only.

**YouTube**
1. Google Cloud Console: create a project, enable "YouTube Data API v3".
2. Set up the OAuth consent screen, add your own Google account as a test user.
3. Create credentials: OAuth client ID, type **Desktop app**. Paste the client ID and secret into Accounts, Save, then Connect.
- Videos default to **Private**. Google has applied extra limits to API uploads from unaudited projects (including forcing
  uploads to private) and has changed its rules over time, so check a posted video's visibility in YouTube Studio.
- While your Google project is in "Testing" mode, the login expires after 7 days. Press Connect again.
- Daily upload quotas apply.

**Instagram**
- Needs an Instagram Professional account (Business or Creator) and a Meta developer app with the
  content publishing permission, plus an access token and your numeric Instagram account ID.
- Clips are sent as **Reels** (best with 9:16) and are public once published. Instagram limits file size and posts per day.
- If your API version is rejected, set the environment variable `IG_API_VERSION` (default `v25.0`).

## Tips
- First run: the Whisper caption model downloads once (about 150 MB for "base").
- Not English? Set the spoken language in More settings (for example `hi`).
- Jobs run one at a time. Extra jobs wait in line.

## If something goes wrong
- **"Could not download that link"**: update the downloader: `pip install -U yt-dlp`.
- **"ffmpeg is not installed"**: install it, then restart the app.
- **AI option HTTP 400/401/403**: wrong key or outdated model. Set `GEMINI_MODEL` or `CLAUDE_MODEL`.
- **Upload errors** are shown under the clip, with a Retry button.

## Where things are saved (`data` folder)
- `data/jobs/<id>/`: finished clips and job info
- `data/cache/`: downloaded audio tracks (small, safe to delete)
- `data/credentials.json`: your YouTube / Instagram logins (plain text, owner-only on Mac/Linux). Keep it private.

## For developers
- `clipper.py` engine, `sources.py` partial downloads and live streams, `control.py` pause/cancel,
  `uploader.py` YouTube/Instagram, `app.py` server.
- The page is TypeScript in `src/`, compiled into `static/js/` (already included). To edit: `npm install`, then `npm run build` (or `npm run watch`).
- Tests (need ffmpeg): `tests/test_live.py` (fake live stream), `tests/test_api.py` (fake yt-dlp, fake YouTube/Instagram).
  They use stand-ins, so they check the logic, not the real sites.

## Please use it responsibly
Only clip and post videos you own or have permission to use. Reposting other people's streams can cause copyright strikes.
The app only listens on your own computer (127.0.0.1).
