"""
control.py - lets a running job be paused or cancelled from another thread.

How pause works: the job's subprocess (yt-dlp, ffmpeg...) is stopped, and a `Paused`
exception unwinds the pipeline back to the worker, which frees the queue for other jobs.
"Continue" simply runs the job again. Finished steps are skipped (plan.json), and an
interrupted yt-dlp download carries on from its .part file instead of starting over.
"""
import subprocess
import threading
from collections import deque


class ClipError(Exception):
    """A problem the user can understand and fix. Shown in the website."""


class Paused(Exception):
    """The user paused the job. Not an error."""


class Cancelled(Exception):
    """The user deleted the job while it was running."""


class Control:
    def __init__(self):
        self.pause_requested = False
        self.cancel_requested = False
        self._procs = set()
        self._lock = threading.Lock()

    # -- called from the web thread
    def request_pause(self):
        self.pause_requested = True
        self._stop_procs()

    def request_cancel(self):
        self.cancel_requested = True
        self._stop_procs()

    def _stop_procs(self):
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass

    # -- called from the worker thread
    def checkpoint(self):
        """Call between steps. Raises if the user asked to pause or cancel."""
        if self.cancel_requested:
            raise Cancelled()
        if self.pause_requested:
            raise Paused()

    def track(self, proc):
        with self._lock:
            self._procs.add(proc)
        if self.pause_requested or self.cancel_requested:     # asked just before it started
            proc.terminate()

    def untrack(self, proc):
        with self._lock:
            self._procs.discard(proc)

    def capture(self, cmd):
        """Run a command and return (returncode, stdout, stderr). Stoppable like run()."""
        self.checkpoint()
        p = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, errors="replace")
        self.track(p)
        try:
            out, err = p.communicate()
        finally:
            self.untrack(p)
        self.checkpoint()
        return p.returncode, out, err

    def run(self, cmd, cwd=None, on_line=None, keep=12):
        """Run a command, stdout+stderr merged. Returns (returncode, last_lines).
        If the user paused/cancelled meanwhile, raises Paused/Cancelled instead."""
        self.checkpoint()
        p = subprocess.Popen([str(c) for c in cmd], cwd=cwd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, errors="replace")
        self.track(p)
        tail = deque(maxlen=keep)
        try:
            for line in p.stdout:
                line = line.strip()
                if not line:
                    continue
                tail.append(line)
                if on_line:
                    on_line(line)
            p.wait()
        finally:
            self.untrack(p)
            if p.poll() is None:
                p.kill()
        self.checkpoint()                    # a stopped process lands here as Paused/Cancelled
        return p.returncode, list(tail)
