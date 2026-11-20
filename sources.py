"""
sources.py - gets video WITHOUT downloading the whole thing.

Three kinds of source, all with the same small interface:

    source.title       text to show in the page
    source.total       length in seconds (for a live stream: how much is available right now)
    source.live        True for a live stream
    source.audio       a small audio-only file used to find the best moments
    source.cut(a, b, folder, ctl, log) -> (video_file, t0)
                       fetches ONLY the seconds a..b as a small video file.
                       t0 is where that file starts in the source's own timeline.

  LocalSource   an uploaded file on this computer (nothing to download).
  RemoteSource  a normal video link. Downloads the audio-only stream (a few MB per hour),
                then only the chosen clip ranges (yt-dlp --download-sections).
  LiveSource    a live stream. Reads the stream's HLS playlist (the part the site still lets
                you rewind to), takes the audio from it, and later pulls only the chosen
                segments. The page's playlist is frozen when the job starts, so the job
                analyses "the stream up to that point".
"""
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

from control import ClipError, Control

YTDLP = [sys.executable, "-m", "yt_dlp"]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
FFMPEG_HLS_IN = ["-allowed_extensions", "ALL",
                 "-protocol_whitelist", "file,http,https,tcp,tls,crypto,data"]


def ffprobe_duration(path, ctl=None):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]
    rc, out, err = (ctl or Control()).capture(cmd)
    try:
        return float(out.strip())
    except ValueError:
        raise ClipError(f"Could not read the length of {Path(path).name}.\n{err[-400:]}")


# ============================================================ probing a link
@dataclass
class Info:
    title: str
    duration: float
    is_live: bool
    formats: list
    headers: dict


def probe(url, ctl):
    """Ask yt-dlp about a link without downloading anything."""
    rc, out, err = ctl.capture(YTDLP + ["-J", "--no-playlist", "--no-warnings", url])
    if rc != 0 or not out.strip():
        raise ClipError("Could not read that link. Check that it opens in a browser and is not "
                        "private. If it is fine, update the downloader: pip install -U yt-dlp\n\n"
                        "Details: " + (err.strip().splitlines() or ["no details"])[-1][:300])
    try:
        d = json.loads(out)
    except ValueError:
        raise ClipError("The downloader returned something unexpected for that link.")
    status = d.get("live_status")
    if status == "is_upcoming":
        raise ClipError("That live stream has not started yet. Try again once it is live.")
    return Info(title=d.get("title") or url, duration=float(d.get("duration") or 0),
                is_live=bool(d.get("is_live")) or status == "is_live",
                formats=d.get("formats") or [], headers=d.get("http_headers") or {"User-Agent": UA})


# ============================================================ HLS playlists
@dataclass
class Seg:
    uri: str
    dur: float
    seq: int
    pre: list = field(default_factory=list)     # tags that sit before the segment (KEY, MAP, DISCONTINUITY...)
    extra: list = field(default_factory=list)   # tags between EXTINF and the uri (BYTERANGE)
    key: str = ""                               # KEY / MAP tag in force for this segment
    map: str = ""


@dataclass
class Playlist:
    segs: list
    first_seq: int = 0
    ended: bool = False

    @property
    def total(self):
        return sum(s.dur for s in self.segs)

    def to_json(self):
        return {"first_seq": self.first_seq, "ended": self.ended,
                "segs": [vars(s) for s in self.segs]}

    @staticmethod
    def from_json(d):
        return Playlist([Seg(**s) for s in d["segs"]], d["first_seq"], d["ended"])


def _abs_uri_attr(line, base):
    return re.sub(r'URI="([^"]*)"', lambda m: f'URI="{urljoin(base, m.group(1))}"', line)


def parse_media_playlist(text, base):
    first, ended, segs = 0, False, []
    pre, extra, key, mp, dur = [], [], "", "", None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            first = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-ENDLIST"):
            ended = True
        elif line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",")[0])
        elif line.startswith("#EXT-X-KEY"):
            key = _abs_uri_attr(line, base)
            pre.append(key)
        elif line.startswith("#EXT-X-MAP"):
            mp = _abs_uri_attr(line, base)
            pre.append(mp)
        elif line.startswith(("#EXT-X-DISCONTINUITY", "#EXT-X-PROGRAM-DATE-TIME")):
            pre.append(line)
        elif line.startswith("#EXT-X-BYTERANGE"):
            extra.append(line)
        elif line.startswith("#"):
            continue
        else:
            segs.append(Seg(urljoin(base, line), dur or 0.0, first + len(segs), pre, extra, key, mp))
            pre, extra, dur = [], [], None
    return Playlist(segs, first, ended)


def parse_master(text, base):
    """Returns [(bandwidth, height, url)]."""
    out, attrs = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = line
        elif attrs and line and not line.startswith("#"):
            bw = re.search(r"BANDWIDTH=(\d+)", attrs)
            res = re.search(r"RESOLUTION=(\d+)x(\d+)", attrs)
            out.append((int(bw.group(1)) if bw else 0, int(res.group(2)) if res else 0,
                        urljoin(base, line)))
            attrs = None
    return out


def http_get(url, headers):
    import requests
    try:
        r = requests.get(url, headers=headers or {"User-Agent": UA}, timeout=30)
        r.raise_for_status()
    except Exception as e:
        raise ClipError(f"Could not read the live stream's playlist: {e}")
    return r.text


def load_media_playlist(url, headers, prefer):
    """Fetch a playlist; if it is a master playlist pick a variant ('lowest' or 'best')."""
    text = http_get(url, headers)
    if "#EXT-X-STREAM-INF" in text:
        variants = parse_master(text, url)
        if not variants:
            raise ClipError("The live stream's playlist has no usable video qualities.")
        if prefer == "lowest":
            url = min(variants, key=lambda v: (v[1] or 10**6, v[0]))[2]
        else:
            ok = [v for v in variants if 0 < v[1] <= 1080] or variants
            url = max(ok, key=lambda v: (v[1], v[0]))[2]
        text = http_get(url, headers)
    pl = parse_media_playlist(text, url)
    if not pl.segs:
        raise ClipError("The live stream's playlist is empty right now.")
    return pl


def write_vod_playlist(segs, path):
    """Write a local, seekable playlist that holds only `segs` (they stay on the site's servers)."""
    out = ["#EXTM3U", "#EXT-X-VERSION:7",
           f"#EXT-X-TARGETDURATION:{int(math.ceil(max(s.dur for s in segs))) or 1}",
           "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD"]
    first = segs[0]
    if first.key:
        out.append(first.key)
    if first.map:
        out.append(first.map)
    for i, s in enumerate(segs):
        for line in s.pre:
            if i == 0 and line.startswith(("#EXT-X-KEY", "#EXT-X-MAP")):
                continue
            out.append(line)
        out.append(f"#EXTINF:{s.dur:.3f},")
        out += s.extra
        out.append(s.uri)
    out.append("#EXT-X-ENDLIST")
    Path(path).write_text("\n".join(out) + "\n", encoding="utf-8")


def hls_input_args(playlist):
    # Note: ffmpeg cannot attach custom HTTP headers when the playlist is a local file,
    # so segments are fetched with ffmpeg's default headers. YouTube/Twitch accept that.
    return list(FFMPEG_HLS_IN) + ["-i", str(playlist)]


# ============================================================ the three source kinds
class LocalSource:
    kind, live = "file", False

    def __init__(self, path, ctl):
        self.path = Path(path)
        self.title = self.path.name
        self.audio = self.path
        self.total = ffprobe_duration(self.path, ctl)

    def cut(self, a, b, folder, ctl, log):
        return self.path, 0.0                 # already local: no need to cut a copy


class RemoteSource:
    kind, live = "vod", False

    def __init__(self, url, info, audio, total):
        self.url, self.title, self.audio, self.total = url, info.title, audio, total

    def cut(self, a, b, folder, ctl, log):
        folder = Path(folder)
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
        cmd = YTDLP + ["-f", "bv*[height<=1080]+ba/b", "--merge-output-format", "mp4",
                       "--no-playlist", "--newline", "--no-warnings",
                       "--download-sections", f"*{a:.2f}-{b:.2f}", "--force-keyframes-at-cuts",
                       "-o", str(folder / "seg.%(ext)s"), self.url]
        rc, tail = ctl.run(cmd)
        files = [f for f in folder.iterdir() if f.suffix in (".mp4", ".mkv", ".webm")]
        if rc != 0 or not files:
            raise ClipError("Could not download just that part of the video.\n" + " | ".join(tail[-3:]))
        return max(files, key=lambda f: f.stat().st_size), a


class LiveSource:
    kind, live = "live", True

    def __init__(self, title, headers, audio_pl, video_pl, job_dir):
        self.title, self.headers = title, headers
        self.audio_pl, self.video_pl = audio_pl, video_pl
        self.job_dir = Path(job_dir)
        self.total = audio_pl.total
        self.audio = self.job_dir / "live_audio.m4a"
        # The audio and video playlists were read a moment apart. Line them up by sequence number.
        self.voff = 0.0
        vs = video_pl.segs
        if vs[0].seq <= audio_pl.first_seq <= vs[-1].seq:
            self.voff = sum(s.dur for s in vs if s.seq < audio_pl.first_seq)

    # -- state is saved so Continue after a pause reuses the same frozen window
    def save(self):
        (self.job_dir / "live.json").write_text(json.dumps({
            "title": self.title, "headers": self.headers,
            "audio": self.audio_pl.to_json(), "video": self.video_pl.to_json()}), encoding="utf-8")

    @classmethod
    def load(cls, job_dir):
        d = json.loads((Path(job_dir) / "live.json").read_text(encoding="utf-8"))
        return cls(d["title"], d["headers"], Playlist.from_json(d["audio"]),
                   Playlist.from_json(d["video"]), job_dir)

    @classmethod
    def create(cls, info, job_dir, log):
        hls = [f for f in info.formats if "m3u8" in (f.get("protocol") or "") and f.get("url")]
        if not hls:
            raise ClipError("This live stream does not offer an HLS feed that Shortsmith can read "
                            "(it works with YouTube, Twitch, Kick and most others). "
                            "If the stream has ended, paste the recording's link instead.")
        has_a = lambda f: f.get("acodec") != "none"
        has_v = lambda f: f.get("vcodec") != "none"
        with_audio = [f for f in hls if has_a(f)]
        with_video = [f for f in with_audio if has_v(f)]
        if not with_video:
            raise ClipError("This live stream's video and sound are on separate feeds, "
                            "which Shortsmith cannot join yet.")
        # for finding moments: the smallest feed that has sound
        fa = min(with_audio, key=lambda f: (has_v(f), f.get("height") or 0, f.get("tbr") or 0))
        # for the clips: the best picture up to 1080p
        ok = [f for f in with_video if (f.get("height") or 0) <= 1080] or with_video
        fv = max(ok, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        ha = fa.get("http_headers") or info.headers
        hv = fv.get("http_headers") or info.headers
        audio_pl = load_media_playlist(fa["url"], ha, "lowest")
        video_pl = load_media_playlist(fv["url"], hv, "best")
        log(f"Live stream: {fmt_len(audio_pl.total)} of it can be read right now"
            f"{' (the stream has ended)' if audio_pl.ended else ''}.")
        if audio_pl.total < 120:
            log("That is a short window. Some sites (Twitch, for example) only keep a few minutes "
                "of a live stream available. YouTube streams with rewind switched on keep much more.")
        self = cls(info.title, hv, audio_pl, video_pl, job_dir)
        self.save()
        return self

    def fetch_audio(self, ctl, log, progress, lo=2, hi=25):
        """Pull the audio of the whole available window into a small file."""
        if self.audio.exists():
            return
        part = self.audio.with_name("live_audio.part.m4a")
        pl = self.job_dir / "live_audio.m3u8"
        write_vod_playlist(self.audio_pl.segs, pl)
        log("Reading the audio of the stream so far (video is not downloaded).")
        cmd = (["ffmpeg", "-v", "error", "-nostats", "-progress", "pipe:1", "-y"]
               + hls_input_args(pl) +
               ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "48k", str(part)])
        total = max(self.total, 1)

        def on_line(line):
            m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
            if m:
                done = min(1.0, int(m.group(1)) / 1e6 / total)
                progress(lo + done * (hi - lo), "get", f"Reading the stream's audio ({int(done * 100)}%)")

        rc, tail = ctl.run(cmd, on_line=on_line)
        if rc != 0 or not part.exists():
            part.unlink(missing_ok=True)
            raise ClipError("Could not read the live stream's audio. The stream may have stopped "
                            "or the site blocked the request.\n" + " | ".join(tail[-3:]))
        part.replace(self.audio)

    def cut(self, a, b, folder, ctl, log):
        folder = Path(folder)
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
        lo, hi = a + self.voff, b + self.voff
        picked, t, t_first = [], 0.0, 0.0
        for s in self.video_pl.segs:
            if t + s.dur > lo and t < hi:
                if not picked:
                    t_first = t
                picked.append(s)
            t += s.dur
        if not picked:
            raise ClipError("That moment is outside the part of the stream that is available.")
        pl = folder / "seg.m3u8"
        write_vod_playlist(picked, pl)
        out = folder / "seg.mp4"
        rc, tail = ctl.run(["ffmpeg", "-v", "error", "-y"] + hls_input_args(pl) +
                           ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", str(out)])
        if rc != 0 or not out.exists():
            raise ClipError("Could not fetch that part of the stream. It may have already left "
                            "the part the site keeps available.\n" + " | ".join(tail[-3:]))
        return out, t_first - self.voff


# ============================================================ opening a source
def fmt_len(t):
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


def cache_folder(cache_dir, url):
    return Path(cache_dir) / f"src_{hashlib.md5(url.encode()).hexdigest()[:8]}"


def _finished_audio(folder):
    return [f for f in Path(folder).glob("audio.*")
            if f.suffix not in (".part", ".ytdl", ".temp") and ".part" not in f.name]


def discard_partial(folder):
    """Called when a job is deleted: throw away a half-finished download, keep a finished one."""
    folder = Path(folder)
    if folder.exists() and not _finished_audio(folder):
        shutil.rmtree(folder, ignore_errors=True)


def download_audio(url, folder, ctl, log, progress, lo=2, hi=25):
    folder.mkdir(parents=True, exist_ok=True)
    done = _finished_audio(folder)
    if done:
        log("The audio of this link was downloaded before, reusing it.")
        return done[0]
    log("Downloading only the audio (a few MB per hour). The video itself is not downloaded.")
    cmd = YTDLP + ["-f", "ba[abr<=96]/wa/ba/worst", "--no-playlist", "--newline", "--no-warnings",
                   "-N", "4", "-o", str(folder / "audio.%(ext)s"), url]
    last = [-1]

    def on_line(line):
        m = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
        if m and int(float(m.group(1))) != last[0]:
            last[0] = int(float(m.group(1)))
            progress(lo + last[0] / 100 * (hi - lo), "get", f"Downloading the audio ({last[0]}%)")

    rc, tail = ctl.run(cmd, on_line=on_line)       # raises Paused/Cancelled if stopped; .part stays
    done = _finished_audio(folder)
    if rc != 0 or not done:
        raise ClipError("Could not download that link. Check that it opens in a browser and is not "
                        "private. If it is fine, update the downloader: pip install -U yt-dlp\n\n"
                        "Details: " + " | ".join(tail[-3:]))
    return done[0]


def open_source(src, cache_dir, job_dir, ctl, log, progress, meta):
    """Returns a source whose audio is ready. `meta(**fields)` reports title / live flag."""
    job_dir = Path(job_dir)
    if os.path.exists(src):
        return LocalSource(src, ctl)
    if (job_dir / "live.json").exists():                  # a live job being continued
        s = LiveSource.load(job_dir)
        meta(title=s.title, live=True)
        s.fetch_audio(ctl, log, progress)
        return s
    progress(1, "get", "Reading the link")
    info = probe(src, ctl)
    meta(title=info.title, live=info.is_live)
    if info.is_live:
        log("This is a live stream. Using what has been streamed so far.")
        s = LiveSource.create(info, job_dir, log)
        s.fetch_audio(ctl, log, progress)
        return s
    audio = download_audio(src, cache_folder(cache_dir, src), ctl, log, progress)
    total = info.duration or ffprobe_duration(audio, ctl)
    return RemoteSource(src, info, audio, total)
