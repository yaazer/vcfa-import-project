"""Background jobs for the web console.

Anything that talks to vCenter or the cluster runs as a job: in its own thread,
with its own state-store connection, its log captured line by line so the
browser can follow it, and a copy written to <workdir>/jobs/ so it survives a
restart of the console.

Only one job runs at a time. The engine was built for one operator driving one
campaign; two concurrent runs against the same state would fight over it.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

MAX_LINES_IN_MEMORY = 20000
MAX_JOBS_KEPT = 60

# Terminal job statuses. "warning" = finished, but something did not go to plan
# (failed VMs, preflight problems); the job itself did not crash.
RUNNING, SUCCEEDED, WARNING, FAILED, STOPPED = "running", "succeeded", "warning", "failed", "stopped"


class JobBusy(Exception):
    """Another job is already running."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Job:
    _seq = itertools.count(1)

    def __init__(self, kind: str, title: str, params: Dict[str, Any], log_dir: Optional[Path],
                 stoppable: bool = False):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.id = "{}-{:03d}-{}".format(stamp, next(Job._seq) % 1000, kind)
        self.kind = kind
        self.title = title
        self.params = params
        self.status = RUNNING
        self.started_at = _now_iso()
        self.finished_at: Optional[str] = None
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self.progress: Optional[Dict[str, Any]] = None
        self.stop_requested = False
        # Declared up front, so Stop works from the first instant; what Stop
        # does is registered later by the job itself (on_stop).
        self.stoppable = stoppable
        self.line_count = 0
        self._lines: List[List[Any]] = []     # [seq, ts, text]
        self._first_seq = 0
        self._lock = threading.Lock()
        self._on_stop: Optional[Callable[[], None]] = None
        self._log_path = (log_dir / (self.id + ".log")) if log_dir else None
        self._meta_path = (log_dir / (self.id + ".json")) if log_dir else None
        self._fh = None
        if self._log_path:
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
                self._fh = open(self._log_path, "a", encoding="utf-8")
            except OSError:
                self._fh = None

    # ------------------------------------------------------------ logging
    def log(self, msg: Any = "") -> None:
        text = str(msg)
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            for line in text.split("\n"):
                self._lines.append([self.line_count, ts, line])
                self.line_count += 1
                if self._fh:
                    self._fh.write("{} {}\n".format(ts, line))
            if len(self._lines) > MAX_LINES_IN_MEMORY:
                drop = len(self._lines) - MAX_LINES_IN_MEMORY
                self._lines = self._lines[drop:]
                self._first_seq = self._lines[0][0]
            if self._fh:
                self._fh.flush()

    def set_progress(self, done: int, total: int, label: str = "") -> None:
        self.progress = {"done": done, "total": total, "label": label}

    def on_stop(self, fn: Callable[[], None]) -> None:
        """Register what Stop does; makes the job stoppable."""
        self._on_stop = fn
        self.stoppable = True
        if self.stop_requested:
            fn()

    def request_stop(self) -> bool:
        if self.status != RUNNING or not self.stoppable:
            return False
        self.stop_requested = True
        if self._on_stop:
            self._on_stop()
        return True

    # ----------------------------------------------------------- snapshot
    def summary(self) -> Dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "title": self.title, "params": self.params,
            "status": self.status, "started_at": self.started_at,
            "finished_at": self.finished_at, "result": self.result, "error": self.error,
            "progress": self.progress, "stoppable": self.stoppable and self.status == RUNNING,
            "stop_requested": self.stop_requested, "lines": self.line_count,
        }

    def snapshot(self, since: int = 0, limit: int = 5000) -> Dict[str, Any]:
        with self._lock:
            since = max(since, self._first_seq)
            start = since - self._first_seq
            chunk = self._lines[start:start + limit]
        out = self.summary()
        out["log"] = chunk
        out["next"] = chunk[-1][0] + 1 if chunk else since
        return out

    def finish(self, status: str, result: Optional[Dict[str, Any]] = None,
               error: Optional[str] = None) -> None:
        self.status = status
        self.result = result
        self.error = error
        self.finished_at = _now_iso()
        if self._fh:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        self.save_meta()

    def save_meta(self) -> None:
        if not self._meta_path:
            return
        try:
            tmp = self._meta_path.with_name(self._meta_path.name + ".tmp")
            tmp.write_text(json.dumps(self.summary(), indent=1), encoding="utf-8")
            tmp.replace(self._meta_path)     # a crash mid-write never leaves half a file
        except OSError:
            pass


class PastJob:
    """A job from an earlier console session, read back from disk."""

    def __init__(self, meta: Dict[str, Any], log_path: Path):
        self.meta = meta
        self.id = meta["id"]
        self.status = meta.get("status", FAILED)
        self._log_path = log_path

    def summary(self) -> Dict[str, Any]:
        return dict(self.meta, stoppable=False)

    def snapshot(self, since: int = 0, limit: int = 5000) -> Dict[str, Any]:
        lines: List[List[Any]] = []
        try:
            with open(self._log_path, "r", encoding="utf-8", errors="replace") as fh:
                for i, raw in enumerate(fh):
                    if i < since:
                        continue
                    if len(lines) >= limit:
                        break
                    raw = raw.rstrip("\n")
                    ts, _, text = raw.partition(" ")
                    lines.append([i, ts, text])
        except OSError:
            pass
        out = self.summary()
        out["log"] = lines
        out["next"] = lines[-1][0] + 1 if lines else since
        return out

    def request_stop(self) -> bool:
        return False


class JobManager:
    def __init__(self, log_dir: Optional[Path]):
        self.log_dir = log_dir
        self._jobs: Dict[str, Any] = {}
        self._active: Optional[Job] = None
        self._lock = threading.Lock()
        self._load_history()

    def _load_history(self) -> None:
        if not self.log_dir or not self.log_dir.is_dir():
            return
        metas = sorted(self.log_dir.glob("*.json"))[-MAX_JOBS_KEPT:]
        for path in metas:
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(meta, dict) or not isinstance(meta.get("id"), str):
                continue   # truncated or foreign file: skip it, never refuse to start
            if meta.get("status") == RUNNING:
                # The console stopped while this ran. The campaign state is
                # durable; only the job's own bookkeeping was cut short.
                meta["status"] = STOPPED
                meta["error"] = "the console was stopped while this job ran"
            self._jobs[meta["id"]] = PastJob(meta, path.with_suffix(".log"))

    @property
    def active(self) -> Optional[Job]:
        job = self._active
        return job if job is not None and job.status == RUNNING else None

    def start(self, kind: str, title: str, params: Dict[str, Any],
              fn: Callable[[Job], Dict[str, Any]], stoppable: bool = False,
              lock: Any = None) -> Job:
        """Run fn(job) in a thread. fn returns a result dict; a "status" key in it
        (warning/stopped) overrides the default "succeeded".

        `lock` (a service.WorkspaceLock) is acquired before the job exists --
        so a busy workspace is refused, not started -- and released when the
        job ends, however it ends."""
        with self._lock:
            if self.active is not None:
                raise JobBusy("'{}' is still running; wait for it or stop it first".format(
                    self._active.title))
            if lock is not None:
                lock.acquire()      # raises WorkspaceBusy: another process holds it
            job = Job(kind, title, params, self.log_dir, stoppable=stoppable)
            # Record the job now, not only when it ends: if the console dies
            # mid-run, a restart must still find it (and mark it stopped).
            job.save_meta()
            self._jobs[job.id] = job
            self._active = job
            self._trim()

        def runner() -> None:
            started = time.time()
            try:
                result = fn(job) or {}
                status = result.pop("status", None) or (STOPPED if job.stop_requested else SUCCEEDED)
                result["seconds"] = round(time.time() - started, 1)
                job.finish(status, result)
            except BaseException as exc:  # noqa: BLE001 -- report every failure to the browser
                job.log("")
                job.log("error: {}".format(exc))
                known = type(exc).__name__ in (
                    "ConfigError", "InventoryError", "SelectionError", "VCenterError",
                    "VCenterAuthError", "KubectlError", "WorkspaceBusy")
                if not known:
                    job.log(traceback.format_exc().rstrip())
                job.finish(FAILED, error=str(exc)[:2000] or type(exc).__name__)
            finally:
                if lock is not None:
                    lock.release()

        threading.Thread(target=runner, name="job-" + job.id, daemon=True).start()
        return job

    def get(self, job_id: str) -> Optional[Any]:
        return self._jobs.get(job_id)

    def list(self) -> List[Dict[str, Any]]:
        return [j.summary() for j in reversed(list(self._jobs.values()))]

    def _trim(self) -> None:
        while len(self._jobs) > MAX_JOBS_KEPT:
            oldest = next(iter(self._jobs))
            if self._jobs[oldest] is self._active:
                break
            del self._jobs[oldest]
