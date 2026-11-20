#!/usr/bin/env python3
"""
clipper.py - the engine behind Shortsmith.

Give it a video link (or a file) and it makes captioned clips:
  1. Get only the AUDIO of the video, or of a live stream so far   (sources.py)
  2. Find the best moments    (loud peaks = free, or an AI reads the transcript)
  3. Fetch only those moments as small video files                 (sources.py)
  4. Make captions            (faster-whisper, runs on your PC)
  5. Reframe (9:16, 4:5, 1:1, 16:9), burn captions, save          (ffmpeg)

The whole video is never downloaded. From the terminal:
  python clipper.py "https://www.youtube.com/watch?v=XXXX" --clips 6 --ratio 9:16
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from control import Cancelled, ClipError, Control, Paused   # noqa: F401  (re-exported)
from sources import fmt_len, open_source

# Output formats. Instagram Reels and YouTube Shorts are both 9:16 at 1080x1920.
# x, y anchor captions securely at lower-third so they never bounce or jump around.
RATIOS = {
    "9:16": dict(w=1080, h=1920, font=72, x=540, y=1420),    # Reels / Shorts
    "4:5":  dict(w=1080, h=1350, font=64, x=540, y=1050),    # Instagram feed portrait
    "1:1":  dict(w=1080, h=1080, font=58, x=540, y=880),     # square
    "16:9": dict(w=1920, h=1080, font=52, x=960, y=880),     # landscape (normal YouTube)
}


@dataclass
class Options:
    clips: int = 5
    length: int = 30
    picker: str = "audio"          # audio | gemini | claude
    layout: str = "blur"           # blur | crop
    ratio: str = "9:16"            # key of RATIOS
    model: str = "base"            # whisper size
    lang: Optional[str] = None     # e.g. "en", "hi"; None = auto
    api_key: Optional[str] = None  # only for gemini / claude, never saved


def fmt(t):
    return fmt_len(t)


def need(tool):
    if shutil.which(tool) is None:
        raise ClipError(f"'{tool}' is not installed. Install ffmpeg first (see README).")


def run_checked(ctl, cmd, cwd=None):
    rc, tail = ctl.run(cmd, cwd=cwd)
    if rc != 0:
        raise ClipError(f"A step failed: {' '.join(str(c) for c in cmd[:3])}...\n" + "\n".join(tail)[-1200:])


# ---------------------------------------------------------------- 2a. pick by loudness (free)
def loudness(audio, ctl, frame=0.5):
    import numpy as np
    sr = 16000
    n = int(sr * frame)
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(audio), "-vn", "-ac", "1",
                          "-ar", str(sr), "-f", "s16le", "-"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    ctl.track(p)
    vals = []
    try:
        while True:
            buf = p.stdout.read(n * 2)
            if len(buf) < n * 2:
                break
            x = np.frombuffer(buf, dtype=np.int16).astype(np.float32)
            vals.append(float(np.sqrt(np.mean(x * x))))
        p.wait()
    finally:
        ctl.untrack(p)
        if p.poll() is None:
            p.kill()
    ctl.checkpoint()
    return np.array(vals)


def pick_by_audio(audio, total, count, length, ctl, log=print, frame=0.5):
    """Streamers get loud on big moments. Find the loudest windows."""
    import numpy as np
    rms = loudness(audio, ctl, frame)
    k = max(1, int(length / frame))
    if len(rms) <= k:
        return [{"start": 0.0, "end": total, "score": 50.0, "title": "Clip 1"}]
    base = np.median(rms) + 1e-6                  # this video's normal volume
    level = np.log1p(rms / base)                  # stops one huge spike from winning
    csum = np.concatenate([[0], np.cumsum(level)])
    mean = (csum[k:] - csum[:-k]) / k             # average loudness of each window
    peak = np.lib.stride_tricks.sliding_window_view(level, k).max(axis=1)
    score = mean + 0.3 * peak
    chosen = []
    for i in np.argsort(-score):
        s = float(i * frame)
        if any(abs(s - c["start"]) < length for c in chosen):
            continue                               # no overlapping clips
        chosen.append({"start": s, "end": min(total, s + length),
                       "score": round(float(score[i]) * 40, 1), "title": ""})
        if len(chosen) == count:
            break
    chosen.sort(key=lambda c: c["start"])
    for n, c in enumerate(chosen, 1):
        c["title"] = f"Clip {n}"
    return chosen


# ---------------------------------------------------------------- 3. captions (whisper)
_models = {}


def read_wav_samples(path):
    """Read a 16 kHz mono 16-bit WAV (made by ffmpeg) as float32 samples in -1..1."""
    import wave
    import numpy as np
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _detect_language(model, samples):
    """Return the language code Whisper thinks the audio is in."""
    clip = samples[:16000 * 30]
    try:
        lang, prob, _ = model.detect_language(clip)
        return lang
    except Exception:
        pass
    return None


def transcribe(video, start, end, model_name, lang, log, ctl, for_captions=False):
    """Returns words [{start,end,word}] in the time of `video` (start/end are in that file's time).
    
    If for_captions=True:
      Captions are only produced for English videos (as requested).
      If regional audio is detected, returns [] so no subtitles are burned on the clip.
    If for_captions=False (for Gemini/AI analysis):
      Transcribes regional audio in its detected language so Gemini can still analyze and pick moments!
    """
    if model_name not in _models:
        from faster_whisper import WhisperModel
        threads = min(8, os.cpu_count() or 4)
        log(f"Loading Whisper '{model_name}' model (first time downloads weights)...")
        _models[model_name] = WhisperModel(model_name, device="auto", compute_type="int8", cpu_threads=threads)
    model = _models[model_name]
    offset = start or 0.0
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "a.wav"
        cmd = ["ffmpeg", "-v", "error", "-y"]
        if start is not None:
            cmd += ["-ss", f"{start:.2f}"]
        cmd += ["-i", video]
        if end is not None:
            cmd += ["-t", f"{end - (start or 0):.2f}"]
        cmd += ["-vn", "-ac", "1", "-ar", "16000", wav]
        run_checked(ctl, cmd)
        samples = read_wav_samples(wav)
        if len(samples) == 0:
            return []

        # Detect language if not explicitly specified
        detected = None
        if not lang or lang.strip().lower() in ("auto", "none"):
            detected = _detect_language(model, samples)
            if detected:
                if for_captions and detected != "en":
                    log(f"Detected language '{detected}'. Skipping video captions (captions enabled for English only).")
                    return []
                lang = detected
            else:
                lang = "en"

        # beam_size=1 (greedy) is significantly faster with virtually identical timestamp accuracy
        try:
            segs, _ = model.transcribe(samples, word_timestamps=True, vad_filter=True, language=lang, beam_size=1)
        except Exception as e:
            log(f"Whisper transcription warning: {e}")
            return []

        words = []
        for s in segs:
            ctl.checkpoint()
            for w in (s.words or []):
                if w.word.strip():
                    words.append({"start": w.start + offset, "end": w.end + offset,
                                  "word": w.word.strip()})
    return words


def snap(clip, words, total):
    """Move start/end so the clip begins and ends on a sentence, not mid-word."""
    s, e = clip["start"], clip["end"]
    inside = [w for w in words if w["start"] >= s and w["end"] <= e]
    if len(inside) < 3:
        return clip
    new_start = inside[0]["start"]
    for i, w in enumerate(inside):
        if w["start"] > s + 5:
            break
        prev = inside[i - 1] if i else None
        if prev is None or prev["word"][-1] in ".?!" or w["start"] - prev["end"] > 0.7:
            new_start = w["start"]
            break
    new_end = inside[-1]["end"]
    for w in reversed(inside):
        if w["end"] < e - 8:
            break
        if w["word"][-1] in ".?!":
            new_end = w["end"]
            break
    clip = dict(clip)
    clip["start"] = max(0.0, new_start - 0.3)
    clip["end"] = min(total, new_end + 0.5)
    return clip


# ---------------------------------------------------------------- 2b. AI / Hybrid moment picker
# Lighter models first: gemini-3.5-flash-lite consumes fewer quota units, avoids rate limits & 503 errors.
_GEMINI_MODELS = ["gemini-3.5-flash-lite", "gemini-2.0-flash", "gemini-3.8-flash"]

HYBRID_PROMPT = """You choose the best clips for YouTube Shorts / Instagram Reels from a list of exciting candidate moments detected by audio volume/reaction peaks.

Candidate moments:
{candidates}

Select up to {n} BEST moments from these candidates.
Combine high excitement/loudness with the strongest hook, funniest reaction, or most compelling complete thought.
Adjust start and end times slightly if needed (between {lo} and {hi} seconds) so sentences begin and end cleanly.

Reply with ONLY a valid JSON list, nothing else:
[{"candidate_id": <id>, "start": <start_seconds>, "end": <end_seconds>, "score": <0-100>,
  "title": "<short catchy title>", "description": "<one sentence description for social media>",
  "hashtags": ["#tag1", "#tag2", "#tag3"]}]
"""


def ask_llm(provider, prompt, api_key=None, log=print):
    import requests
    if provider == "gemini":
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise ClipError("Add your Gemini API key (or set GEMINI_API_KEY).")
        user_model = os.environ.get("GEMINI_MODEL", "").strip()
        models_to_try = ([user_model] if user_model else []) + [m for m in _GEMINI_MODELS if m != user_model]
        
        hit_quota = False
        last_error = None
        for model in models_to_try:
            for attempt in range(2):
                try:
                    r = requests.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                        headers={"x-goog-api-key": key},
                        json={"contents": [{"parts": [{"text": prompt}]}],
                              "generationConfig": {"responseMimeType": "application/json"}},
                        timeout=90)
                    
                    if r.status_code == 200:
                        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    
                    status = r.status_code
                    err_msg = ""
                    try:
                        err_msg = r.json().get("error", {}).get("message", "")
                    except Exception:
                        pass

                    if status == 404:
                        last_error = f"Model '{model}' not found (HTTP 404)."
                        break  # try next model

                    elif status == 429:
                        hit_quota = True
                        last_error = f"Rate/usage limit reached on '{model}': {err_msg or 'Quota exhausted'}."
                        log(f"Notice: Gemini API usage limit hit on '{model}'. Trying lighter model...")
                        break  # try lighter model

                    elif status in (500, 502, 503, 504):
                        last_error = f"Gemini server error ({status}) on '{model}': {err_msg or 'Unavailable'}."
                        if attempt == 0:
                            time.sleep(2)  # wait 2s and retry once
                            continue
                        log(f"Notice: Gemini '{model}' busy/overloaded (HTTP {status}). Trying alternative model...")
                        break

                    elif status in (400, 401, 403):
                        raise ClipError(f"Gemini API refused request (HTTP {status}): {err_msg or 'Invalid API key'}. Check your Gemini API key.")
                    
                    else:
                        r.raise_for_status()

                except requests.RequestException as e:
                    last_error = str(e)
                    if attempt == 0:
                        time.sleep(1.5)
                        continue
                    break

        if hit_quota:
            raise ClipError(
                "Gemini API usage limit reached (HTTP 429). Your free-tier API quota was exceeded. "
                "Recommendation: Use a lighter model like 'gemini-3.5-flash-lite' which uses fewer tokens, "
                "or wait a minute.")
        raise ClipError(f"Gemini API call failed on available models ({last_error}).")

    # Claude provider
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ClipError("Add your Anthropic API key (or set ANTHROPIC_API_KEY).")
    model = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": 2000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=180)
        r.raise_for_status()
        return r.json()["content"][0]["text"]
    except requests.HTTPError as ex:
        code = ex.response.status_code
        if code in (400, 401, 403):
            raise ClipError(f"Anthropic API refused request (HTTP {code}). Check your Anthropic API key.")
        raise ClipError(f"Anthropic API failed (HTTP {code}). Try again in a minute.")


def pick_hybrid(audio, words, total, count, length, lo, hi, provider, api_key, log, ctl):
    """Combines acoustic loudness peaks with AI semantic selection.
    
    1. Finds candidate loud/excitement windows from the audio.
    2. Pairs each loud window with its spoken transcript text.
    3. Asks the AI (Gemini/Claude) to evaluate and pick the top viral moments.
    4. If the AI hits rate limits or server overload, falls back to the loud peaks smoothly!
    """
    log("Listening for loud excitement peaks and big reactions...")
    candidates = pick_by_audio(audio, total, max(count * 2, 8), length, ctl, log)
    if not candidates:
        return [{"start": 0.0, "end": min(total, float(length)), "score": 50.0, "title": "Clip 1"}]

    ctl.checkpoint()
    log(f"Asking {provider.capitalize()} to evaluate candidate loud moments and pick the best highlights...")

    cand_lines = []
    for idx, c in enumerate(candidates, 1):
        c["candidate_id"] = idx
        c_words = [w["word"] for w in (words or []) if c["start"] - 1 <= w["start"] <= c["end"] + 1]
        text = " ".join(c_words).strip() or "(High excitement / scream / reaction / music peak)"
        cand_lines.append(f"[Candidate {idx}] {fmt_len(c['start'])} to {fmt_len(c['end'])} (Loudness Score: {c['score']}/100):\n  Speech: \"{text}\"")

    prompt = (HYBRID_PROMPT.replace("{n}", str(count))
              .replace("{lo}", str(lo)).replace("{hi}", str(hi))
              .replace("{candidates}", "\n\n".join(cand_lines)))

    raw = ask_llm(provider, prompt, api_key, log=log)
    try:
        a, b = raw.find("["), raw.rfind("]")
        ai_picks = json.loads(raw[a:b + 1])
        chosen = []
        for pick in ai_picks:
            cid = pick.get("candidate_id")
            cand = next((c for c in candidates if c["candidate_id"] == cid), None)
            s = float(pick.get("start", cand["start"] if cand else 0.0))
            e = float(pick.get("end", cand["end"] if cand else s + length))
            if e <= s:
                e = s + length
            e = min(total, e)
            s = max(0.0, s)
            score = float(pick.get("score", cand["score"] if cand else 70.0))
            title = str(pick.get("title", cand["title"] if cand else f"Clip {len(chosen)+1}"))[:120]
            desc = str(pick.get("description", ""))[:300]
            tags = [("#" + str(t).lstrip("#")) for t in (pick.get("hashtags") or [])][:6]
            chosen.append({
                "start": s, "end": e, "score": score,
                "title": title, "description": desc, "hashtags": tags
            })
        if chosen:
            chosen.sort(key=lambda c: -c["score"])
            final = []
            for c in chosen:
                if all(c["end"] <= x["start"] or c["start"] >= x["end"] for x in final):
                    final.append(c)
                if len(final) == count:
                    break
            final.sort(key=lambda c: c["start"])
            for n, c in enumerate(final, 1):
                if not c["title"] or c["title"].startswith("Clip"):
                    c["title"] = f"Clip {n}"
            log(f"{provider.capitalize()} successfully selected {len(final)} top moments!")
            return final
    except Exception as ex:
        log(f"AI response parsing failed ({ex}). Continuing with loudest candidate moments.")

    return candidates[:count]


# ---------------------------------------------------------------- 4. captions file + render
def ass_time(t):
    cs = int(round(max(t, 0) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: @W@
PlayResY: @H@
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,@FONT@,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,@OUT@,2,5,40,40,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def clean(word):
    return re.sub(r"[{}\\]", "", word).strip(".,;:").upper()


def make_ass(words, c_start, c_end, path, ratio="9:16", per_line=3):
    """Captions 3 words at a time, active spoken word in yellow.
    Fixed position with {\\an5\\pos(X, Y)} so captions never bounce or jump up/down.
    """
    r = RATIOS[ratio]
    head = (ASS_HEAD.replace("@W@", str(r["w"])).replace("@H@", str(r["h"]))
            .replace("@FONT@", str(r["font"])).replace("@OUT@", str(round(r["font"] / 12))))
    ws = [w for w in words if w["start"] >= c_start - 0.05 and w["end"] <= c_end + 0.05]
    if not ws:
        Path(path).write_text(head, encoding="utf-8")
        return
    lines = [head]
    pos_tag = f"{{\\an5\\pos({r['x']},{r['y']})}}"
    for g in range(0, len(ws), per_line):
        group = ws[g:g + per_line]
        for i, w in enumerate(group):
            start = w["start"] - c_start
            end = (group[i + 1]["start"] if i + 1 < len(group) else w["end"]) - c_start
            parts = []
            for j, x in enumerate(group):
                colour = "&H0000FFFF&" if j == i else "&H00FFFFFF&"   # yellow / white
                parts.append("{\\c" + colour + "}" + clean(x["word"]))
            text = pos_tag + " ".join(parts)
            lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(max(end, start + 0.05))},"
                         f"Default,,0,0,0,,{text}\n")
    Path(path).write_text("".join(lines), encoding="utf-8")


def video_filter(layout, ratio, ass_name=None):
    r = RATIOS[ratio]
    W, H = r["w"], r["h"]
    sub_filter = f",subtitles={ass_name}" if ass_name else ""
    if layout == "crop":     # fill the frame, cut whatever does not fit
        return f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}{sub_filter}[v]"
    # Fast blurred background: downscale to 180:320, blur lightly, then upscale to full size.
    # This is ~15-20x faster than doing boxblur directly on 1080x1920 frames.
    return ("[0:v]split[a][b];"
            f"[a]scale=180:320:force_original_aspect_ratio=increase,crop=180:320,boxblur=8:2,scale={W}:{H}[bg];"
            f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2{sub_filter}[v]")


def render(video, clip, words, out_mp4, layout, ratio, ctl):
    out_mp4 = Path(out_mp4).resolve()
    video = Path(video).resolve()          # ffmpeg runs inside the output folder
    if words:
        ass = out_mp4.with_suffix(".ass")
        make_ass(words, clip["start"], clip["end"], ass, ratio)
        fc = video_filter(layout, ratio, ass.name)
    else:
        fc = video_filter(layout, ratio, None)

    run_checked(ctl, ["ffmpeg", "-v", "error", "-y", "-ss", f"{clip['start']:.2f}", "-i", video,
                      "-t", f"{clip['end'] - clip['start']:.2f}", "-filter_complex", fc,
                      "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
                      "-crf", "22", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                      "-movflags", "+faststart", out_mp4.name], cwd=out_mp4.parent)


# ---------------------------------------------------------------- the whole pipeline
def _save_plan(path, plan):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(plan), encoding="utf-8")
    tmp.replace(path)


def process(source, out_dir, opts, log=print, progress=None, cache_dir=None, ctl=None, meta=None):
    """Runs everything. Returns {"duration": seconds, "clips": [ ... ], "live": bool}.

    Safe to call again after Paused: finished steps are skipped (plan.json) and an
    interrupted download resumes from its partial file."""
    progress = progress or (lambda pct, stage, msg="": None)
    meta = meta or (lambda **k: None)
    ctl = ctl or Control()
    need("ffmpeg")
    need("ffprobe")
    if opts.ratio not in RATIOS:
        raise ClipError(f"Unknown format {opts.ratio}.")
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(cache_dir or out_dir).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)

    progress(1, "get", "Getting the audio")
    src = open_source(source, cache_dir, out_dir, ctl, log, progress, meta)
    total = src.total
    meta(duration=total, live=src.live)
    log(("Live stream so far: " if src.live else "Video length: ") + fmt(total))
    ctl.checkpoint()

    plan_file = out_dir / "plan.json"
    if plan_file.exists():
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
    else:
        lo, hi = max(15, opts.length - 10), min(120, opts.length + 15)
        progress(25, "find", "Finding the best moments")
        if opts.picker == "audio":
            clips = pick_by_audio(src.audio, total, opts.clips, opts.length, ctl, log)
        else:
            log("Generating audio transcript for AI analysis...")
            # for_captions=False: transcribes regional audio so Gemini can read it and pick moments!
            all_words = transcribe(src.audio, None, None, opts.model, opts.lang, log, ctl, for_captions=False)
            progress(33, "find", f"Combining audio excitement & {opts.picker.capitalize()} analysis")
            try:
                clips = pick_hybrid(src.audio, all_words, total, opts.clips, opts.length,
                                    lo, hi, opts.picker, opts.api_key, log, ctl)
            except ClipError as e:
                log(f"{opts.picker.capitalize()} notice: {e}")
                log("Continuing with loudest moments automatically...")
                clips = pick_by_audio(src.audio, total, opts.clips, opts.length, ctl, log)
            except Exception as e:
                log(f"{opts.picker.capitalize()} encountered an error ({e}). Continuing with loudest moments...")
                clips = pick_by_audio(src.audio, total, opts.clips, opts.length, ctl, log)

        if not clips:
            raise ClipError("No clips were found. Is there any speech or sound in the video?")
        plan = {"total": total, "clips": clips, "results": {}}
        _save_plan(plan_file, plan)

    first = 35
    share = (100 - first) / len(plan["clips"])
    for n, clip in enumerate(plan["clips"], 1):
        if str(n) in plan["results"]:
            continue                                    # finished before a pause
        ctl.checkpoint()
        base = first + share * (n - 1)
        N = len(plan["clips"])
        progress(base, "render", f"Clip {n} of {N}: fetching section")
        seg_dir = out_dir / f"_seg{n}"
        path, t0 = src.cut(max(0.0, clip["start"] - 6), min(total, clip["end"] + 6), seg_dir, ctl, log)
        seg_len = total if path == getattr(src, "path", None) else _length(path, ctl)
        rel = dict(clip, start=clip["start"] - t0, end=clip["end"] - t0)
        progress(base + share * 0.25, "render", f"Clip {n} of {N}: captions check")
        
        # for_captions=True: only English produces captions; regional audio returns [] so no subtitles are burned
        words = transcribe(path, max(0.0, rel["start"] - 3), min(seg_len, rel["end"] + 3),
                           opts.model, opts.lang, log, ctl, for_captions=True)
        
        if words:
            rel = snap(rel, words, seg_len)
        else:
            log(f"Clip {n}: rendering clean clip without subtitles (regional audio or no English speech).")
        
        progress(base + share * 0.55, "render", f"Clip {n} of {N}: rendering")
        name = out_dir / f"clip_{n:02d}.mp4"
        render(path, rel, words, name, opts.layout, opts.ratio, ctl)
        shutil.rmtree(seg_dir, ignore_errors=True)
        a, b = rel["start"] + t0, rel["end"] + t0       # back to the source's own timeline
        plan["results"][str(n)] = {
            "file": name.name, "title": clip["title"],
            "description": clip.get("description", ""), "hashtags": clip.get("hashtags", []),
            "start_s": round(a, 1), "end_s": round(b, 1), "from": fmt(a), "to": fmt(b),
            "seconds": round(b - a, 1), "score": clip["score"], "ratio": opts.ratio}
        _save_plan(plan_file, plan)
        log(f"Clip {n} done: {fmt(a)} to {fmt(b)}")

    results = [plan["results"][str(n)] for n in range(1, len(plan["clips"]) + 1)]
    (out_dir / "clips.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    progress(100, "render", "Done")
    return {"duration": total, "clips": results, "live": src.live}


def _length(path, ctl):
    from sources import ffprobe_duration
    return ffprobe_duration(path, ctl)


# ---------------------------------------------------------------- terminal use
def main():
    ap = argparse.ArgumentParser(description="Video or live-stream link -> captioned clips")
    ap.add_argument("source", help="video link, live stream link, or a local file")
    ap.add_argument("--clips", type=int, default=5)
    ap.add_argument("--length", type=int, default=30)
    ap.add_argument("--picker", choices=["audio", "gemini", "claude"], default="audio")
    ap.add_argument("--layout", choices=["blur", "crop"], default="blur")
    ap.add_argument("--ratio", choices=list(RATIOS), default="9:16",
                    help="9:16 = Reels and Shorts, 4:5 = Instagram feed, 1:1, 16:9")
    ap.add_argument("--model", default="base", help="whisper size: tiny/base/small/medium")
    ap.add_argument("--lang", default=None)
    ap.add_argument("--out", default="clips")
    a = ap.parse_args()
    opts = Options(a.clips, a.length, a.picker, a.layout, a.ratio, a.model, a.lang)
    try:
        res = process(a.source, a.out, opts,
                      progress=lambda p, s, m="": print(f"[{int(p):3d}%] {m}"))
    except ClipError as e:
        sys.exit(f"Error: {e}")
    print(f"\nDone. {len(res['clips'])} clips are in {Path(a.out).resolve()}")


if __name__ == "__main__":
    main()
