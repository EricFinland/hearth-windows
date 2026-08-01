#!/usr/bin/env python3
"""Model downloads for the desktop sidecar: the state behind POST /downloads,
GET /downloads, GET /downloads/events, and the two action routes.

Why this is not part of the session's event stream
--------------------------------------------------
GET /events is per-session and turn-oriented, and a download is neither:

  1. GET /events answers 404 when no session exists. The single most
     important moment for this feature is a fresh install with no model on
     disk at all -- the user cannot create a session, because there is
     nothing to select as its model, so the download that fixes that is
     exactly the one whose progress /events could never carry.
  2. The event ring belongs to a Session object. POST /session replaces that
     object, and with it the ring. A 4 GB download must not lose its history
     because the user started a chat while it ran.
  3. Every frame on /events carries a turn_id. A download has no turn, and
     inventing a null one would make every consumer of that stream special
     case a thing that is not a turn.

So downloads get their own manager, whose lifetime is the sidecar process
rather than any session, and their own stream.

Why the stream sends whole snapshots rather than a delta log
------------------------------------------------------------
Session._events is a bounded ring with a synthetic "events_dropped" marker,
because a turn's events are a narrative: losing "tool_call" between two
retained events genuinely loses information. Download state is not a
narrative, it is a gauge. hearth_hf calls on_progress once per 1 MiB chunk,
so a 4 GB download produces ~4000 progress ticks; any ring small enough to
be sane would wrap many times over, and a client that reconnected would be
told "you missed some" about numbers it does not need the history of.

This manager therefore keeps the CURRENT state of every job and a monotonic
`version` that bumps whenever any of it changes. The stream blocks on
snapshot_after(version) and writes the complete list each time. That makes
every frame idempotent and self-sufficient: a page reload opens the stream
at version 0 and is immediately correct, with no replay, no gap marker, and
no way for a missed frame to leave a stale bar on screen. Terminal
transitions (done, cancelled, error) always notify immediately; progress
ticks are coalesced to at most one notification per PROGRESS_INTERVAL so a
fast link cannot turn a download into a write storm on the SSE socket.

Resume and cancel
-----------------
hearth_hf.download already does the hard parts: a Range request resumes from
<path>.part, the completed file is hashed against the Hub's published LFS
sha256 before it is renamed into place, and is_cancelled() is consulted
between chunks so a cancel leaves a valid, resumable partial rather than a
truncated model. This module's only job there is to make cancellation
reachable (a per-job threading.Event) and to make the surviving partial
VISIBLE, via local_file_state(), so "cancel" reads as "pause" rather than
"throw away 3 GB".

What is downloaded is decided here, not by the caller
-----------------------------------------------------
POST /downloads takes a repo id and a quantisation name and nothing else.
The file list, every part's size, and every part's sha256 are re-fetched
from the Hub by this module at start time. A caller cannot hand in a size or
a hash, so a compromised or merely stale page cannot talk the sidecar into
verifying a download against a hash of the page's choosing, and cannot ask
for a path that was not in the repository's own GGUF listing.

Gated repositories fail here the same way they fail everywhere else: the
tree listing returns hearth_hf's ERR_GATED and the job never starts. The UI
refuses the click first, but this is the check that actually holds.

Python standard library only, plus agent/hearth_hf.
"""

import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))  # desktop/server -> desktop -> repo root
_AGENT_DIR = os.path.join(_REPO_ROOT, "agent")
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

import hearth_hf  # noqa: E402


# At most one notification per this many seconds while bytes are arriving.
# The job's own numbers are updated on every chunk regardless; this only
# bounds how often a waiting SSE stream is woken, so the wire cost of a
# multi-gigabyte download stays flat instead of scaling with its size.
PROGRESS_INTERVAL = 0.25

# Finished jobs stay in the list so a reload still shows what happened, but
# not forever. Oldest finished job is evicted first; a running or queued job
# is never evicted.
RETAIN_FINISHED = 24

STATUS_QUEUED = "queued"
STATUS_RESOLVING = "resolving"
STATUS_DOWNLOADING = "downloading"
STATUS_VERIFYING = "verifying"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"
STATUS_ERROR = "error"

ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RESOLVING, STATUS_DOWNLOADING, STATUS_VERIFYING)


class DownloadError(Exception):
    """A start request that cannot become a job. `kind` is one of hearth_hf's
    ERR_* strings where one applies, so the UI can tell "no space" and "this
    repository needs a licence" apart from a generic failure without parsing
    prose."""

    def __init__(self, message, kind=None, status=400):
        super().__init__(message)
        self.kind = kind
        self.http_status = status


def local_file_state(repo_id, filename, store_dir=None):
    """What is already on disk for one file of one repository.

      present       - the finished file exists (it has been verified by
                      whatever wrote it; hearth_hf never renames an
                      unverified file into place).
      bytes_present - its size, or None.
      partial_bytes - the size of a leftover <path>.part, or None. This is
                      the number that makes a cancel honest: those bytes are
                      kept and a later download resumes from them.
      path          - where it would live.

    Returns a dict with everything None/False rather than raising if the
    path cannot even be computed (an unusable repo id, a store that cannot
    be resolved): a UI annotation must never be the thing that breaks a
    listing.
    """
    out = {"present": False, "bytes_present": None, "partial_bytes": None, "path": None}
    try:
        if store_dir:
            path = os.path.join(hearth_hf.model_dir(repo_id, store_dir), filename)
        else:
            path = hearth_hf.model_path(repo_id, filename)
    except Exception:  # noqa: BLE001 - an annotation must never break a listing
        return out
    out["path"] = path
    try:
        long_path = path
        if os.path.isfile(long_path):
            out["present"] = True
            out["bytes_present"] = os.path.getsize(long_path)
        part = long_path + hearth_hf.PART_SUFFIX
        if os.path.isfile(part):
            out["partial_bytes"] = os.path.getsize(part)
    except OSError:
        pass
    return out


def quant_local_state(repo_id, quant, store_dir=None):
    """local_file_state summed across every part of one quantisation.

    A split model is many files, so "already downloaded" and "partially
    downloaded" are only meaningful across the whole set: reporting the
    first part's 1.9 GB as the quantisation's progress would understate a
    three-part download by two thirds.
    """
    parts = quant.get("parts") or []
    if not parts and quant.get("path"):
        parts = [{"path": quant["path"]}]
    present_parts = 0
    bytes_present = 0
    partial_bytes = 0
    for part in parts:
        path = part.get("path")
        if not isinstance(path, str) or not path:
            continue
        state = local_file_state(repo_id, path, store_dir)
        if state["present"]:
            present_parts += 1
            bytes_present += state["bytes_present"] or 0
        if state["partial_bytes"]:
            partial_bytes += state["partial_bytes"]
    return {
        "part_count": len(parts),
        "parts_present": present_parts,
        "present": bool(parts) and present_parts == len(parts),
        "bytes_present": bytes_present or None,
        "partial_bytes": partial_bytes or None,
    }


class DownloadManager:
    """Every download this process knows about, and the one worker thread
    that runs them.

    Serial on purpose. Concurrent downloads of multi-gigabyte files share
    one link and one disk, so running two halves the speed of each and
    doubles the time before either is usable; and a serial queue makes
    cancelling a not-yet-started job a state change rather than a race.
    The queue position is reported, so waiting is visible rather than
    mysterious.

    `list_fn` and `download_fn` default to hearth_hf.list_gguf_files and
    hearth_hf.download. They are the injection seam the self-test drives, so
    the whole queue/progress/cancel/terminal-state machine is exercised with
    no network and no disk beyond a temporary directory.
    """

    def __init__(self, list_fn=None, download_fn=None, store_dir=None,
                 retain_finished=RETAIN_FINISHED, progress_interval=PROGRESS_INTERVAL):
        self._list_fn = list_fn or hearth_hf.list_gguf_files
        self._download_fn = download_fn or hearth_hf.download
        self._store_dir = store_dir
        self._retain = retain_finished
        self._progress_interval = progress_interval
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._jobs = []            # oldest first
        self._by_id = {}
        self._seq = 0
        self._version = 0
        self._last_notify = 0.0
        self._queue = []           # job ids awaiting the worker
        self._worker = None
        self._stopping = False

    # ---- state and notification ----

    def _bump_locked(self):
        self._version += 1
        self._last_notify = time.monotonic()
        self._changed.notify_all()

    def _touch(self, coalesce=False):
        """Publish whatever just changed. With coalesce=True the bump is
        skipped if one happened within progress_interval -- the job's own
        fields are already updated either way, so a skipped bump only delays
        a waiting reader to the next tick, it never loses a value."""
        with self._lock:
            if coalesce and (time.monotonic() - self._last_notify) < self._progress_interval:
                return
            self._bump_locked()

    def _public(self, job):
        """The wire shape of one job. Never the internal dict itself: the
        worker mutates that from another thread, and handing a live
        reference to a JSON encoder is how a snapshot ends up half from
        before a transition and half from after it."""
        done = job["completed_bytes"] + job["current_bytes"]
        total = job["bytes_total"]
        return {
            "id": job["id"],
            "repo_id": job["repo_id"],
            "filename": job["filename"],
            "label": job["label"],
            "quant": job["quant"],
            "status": job["status"],
            "bytes_done": done,
            "bytes_total": total,
            "fraction": (max(0.0, min(1.0, done / total)) if total else None),
            "speed_bytes_per_sec": job["speed"],
            "eta_seconds": job["eta"],
            "part_index": job["part_index"],
            "part_count": job["part_count"],
            "resumed_from": job["resumed_from"],
            "queue_position": job["queue_position"],
            "path": job["path"],
            "error": job["error"],
            "error_kind": job["error_kind"],
            "verified": job["verified"],
            "verification": job["verification"],
            "already_present": job["already_present"],
            "cancellable": job["status"] in ACTIVE_STATUSES,
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
        }

    def snapshot(self):
        with self._lock:
            return {"version": self._version,
                    "downloads": [self._public(j) for j in self._jobs]}

    def snapshot_after(self, version, timeout=None):
        """Block until self._version exceeds `version`, then return a full
        snapshot. On timeout, returns the current snapshot anyway (whose
        version the caller compares itself) rather than an empty result, so
        a keep-alive tick and a real change go down the same code path."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while self._version <= version:
                if deadline is None:
                    self._changed.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._changed.wait(timeout=remaining)
            return {"version": self._version,
                    "downloads": [self._public(j) for j in self._jobs]}

    # ---- starting ----

    def _resolve_quant(self, repo_id, filename):
        """The repository's own GGUF listing entry for `filename`, matched on
        either the download path (a single file) or the logical model name (a
        split model, whose parts have no single path). Raises DownloadError
        with hearth_hf's own error kind when the listing fails, which is what
        makes a gated repository refuse here rather than 401 mid-download."""
        listing = self._list_fn(repo_id)
        if not isinstance(listing, dict) or not listing.get("ok"):
            kind = (listing or {}).get("error_kind") or hearth_hf.ERR_UNREACHABLE
            message = (listing or {}).get("error") or \
                "Could not read the file list for {}.".format(repo_id)
            if kind == hearth_hf.ERR_GATED:
                message = (
                    "{} is a gated repository: Hugging Face requires a licence to be "
                    "accepted before its files can be downloaded, and Hearth has no "
                    "Hugging Face sign-in yet. {}".format(repo_id, message)
                )
            raise DownloadError(message, kind=kind,
                                status=404 if kind == hearth_hf.ERR_NOT_FOUND else 502)
        for entry in listing.get("files") or []:
            if entry.get("path") == filename or entry.get("name") == filename:
                return entry
        raise DownloadError(
            "{} has no GGUF called {}.".format(repo_id, filename),
            kind=hearth_hf.ERR_NOT_FOUND, status=404)

    def start(self, repo_id, filename):
        """Queue one quantisation for download and return its public job dict.

        Raises DownloadError for anything that means no job exists: an
        unusable repo id, a gated or missing repository, a quantisation this
        repository does not publish, one whose parts are not all present in
        the repository, or one that is already downloading."""
        if not isinstance(repo_id, str) or not repo_id:
            raise DownloadError("repo_id is required.", kind=hearth_hf.ERR_BAD_REQUEST)
        if not isinstance(filename, str) or not filename:
            raise DownloadError("filename is required.", kind=hearth_hf.ERR_BAD_REQUEST)
        reason = hearth_hf.validate_repo_id(repo_id)
        if reason:
            raise DownloadError("Not a usable Hugging Face repo id: {}.".format(reason),
                                kind=hearth_hf.ERR_BAD_REQUEST)

        with self._lock:
            for job in self._jobs:
                if (job["repo_id"] == repo_id and job["filename"] == filename
                        and job["status"] in ACTIVE_STATUSES):
                    raise DownloadError(
                        "{} from {} is already downloading.".format(filename, repo_id),
                        kind=hearth_hf.ERR_BAD_REQUEST, status=409)

        entry = self._resolve_quant(repo_id, filename)
        if not entry.get("complete", True):
            raise DownloadError(
                "{} is missing parts {} in {}, so it cannot be assembled into a "
                "loadable model.".format(filename, entry.get("missing_parts"), repo_id),
                kind=hearth_hf.ERR_BAD_REQUEST)
        parts = [p for p in (entry.get("parts") or []) if isinstance(p.get("path"), str)]
        if not parts:
            raise DownloadError("{} lists no downloadable file.".format(filename),
                                kind=hearth_hf.ERR_BAD_REQUEST)
        sizes = [p.get("size_bytes") for p in parts]
        total = sum(sizes) if all(isinstance(s, int) for s in sizes) else None

        with self._lock:
            self._seq += 1
            job = {
                "id": "dl-{}".format(self._seq),
                "repo_id": repo_id,
                "filename": filename,
                "label": entry.get("name") or filename,
                "quant": entry.get("quant"),
                "parts": parts,
                "status": STATUS_QUEUED,
                "completed_bytes": 0,
                "current_bytes": 0,
                "bytes_total": total,
                "speed": None,
                "eta": None,
                "part_index": 0,
                "part_count": len(parts),
                "resumed_from": None,
                "queue_position": None,
                "path": None,
                "error": None,
                "error_kind": None,
                "verified": False,
                "verification": None,
                "already_present": False,
                "started_at": time.time(),
                "finished_at": None,
                "cancel": threading.Event(),
            }
            self._jobs.append(job)
            self._by_id[job["id"]] = job
            self._queue.append(job["id"])
            self._evict_locked()
            self._renumber_queue_locked()
            self._bump_locked()
            self._ensure_worker_locked()
            return self._public(job)

    def _evict_locked(self):
        finished = [j for j in self._jobs if j["status"] not in ACTIVE_STATUSES]
        excess = len(finished) - self._retain
        if excess <= 0:
            return
        drop = {id(j) for j in finished[:excess]}
        for job in finished[:excess]:
            self._by_id.pop(job["id"], None)
        self._jobs = [j for j in self._jobs if id(j) not in drop]

    def _renumber_queue_locked(self):
        for position, job_id in enumerate(self._queue):
            job = self._by_id.get(job_id)
            if job is not None:
                job["queue_position"] = position + 1

    def _ensure_worker_locked(self):
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._run_queue, name="hearth-downloads",
                                        daemon=True)
        self._worker.start()

    # ---- cancelling and dismissing ----

    def cancel(self, job_id):
        """Ask a queued or running job to stop. True if a job was actually
        asked; False if there is no such job or it had already finished.
        A running job stops within roughly one chunk read and leaves its
        .part file in place; a queued one never opens a connection at all."""
        with self._lock:
            job = self._by_id.get(job_id)
            if job is None or job["status"] not in ACTIVE_STATUSES:
                return False
            job["cancel"].set()
            if job["status"] == STATUS_QUEUED:
                if job["id"] in self._queue:
                    self._queue.remove(job["id"])
                    self._renumber_queue_locked()
                job["status"] = STATUS_CANCELLED
                job["queue_position"] = None
                job["finished_at"] = time.time()
            self._bump_locked()
            return True

    def dismiss(self, job_id):
        """Forget a finished job. False if it does not exist or is still
        active -- a running download is cancelled, never dismissed, so that
        a stray dismiss can never orphan a worker the UI has stopped
        watching."""
        with self._lock:
            job = self._by_id.get(job_id)
            if job is None or job["status"] in ACTIVE_STATUSES:
                return False
            self._by_id.pop(job_id, None)
            self._jobs = [j for j in self._jobs if j["id"] != job_id]
            self._bump_locked()
            return True

    def stop(self):
        """Cancel everything and let the worker drain. Used by the self-test
        and by any future shutdown path; a daemon worker would not otherwise
        be given the chance to leave its partials flushed."""
        with self._lock:
            self._stopping = True
            for job in self._jobs:
                if job["status"] in ACTIVE_STATUSES:
                    job["cancel"].set()
            self._queue = []
            self._changed.notify_all()

    # ---- the worker ----

    def _next_job(self):
        with self._lock:
            while self._queue:
                job_id = self._queue.pop(0)
                self._renumber_queue_locked()
                job = self._by_id.get(job_id)
                if job is None or job["status"] != STATUS_QUEUED:
                    continue
                job["queue_position"] = None
                job["status"] = STATUS_RESOLVING
                self._bump_locked()
                return job
            return None

    def _run_queue(self):
        while True:
            job = self._next_job()
            if job is None:
                return
            try:
                self._run_job(job)
            except Exception as exc:  # noqa: BLE001 - a worker must never die on one job
                with self._lock:
                    job["status"] = STATUS_ERROR
                    job["error"] = "The download failed unexpectedly: {}: {}".format(
                        type(exc).__name__, exc)
                    job["error_kind"] = hearth_hf.ERR_LOCAL_IO
                    job["finished_at"] = time.time()
                    self._bump_locked()

    def _run_job(self, job):
        cancel = job["cancel"]
        parts = job["parts"]
        for index, part in enumerate(parts):
            if cancel.is_set():
                self._finish(job, STATUS_CANCELLED)
                return
            with self._lock:
                job["part_index"] = index + 1
                job["current_bytes"] = 0
                job["status"] = STATUS_DOWNLOADING
                self._bump_locked()

            def on_progress(progress, job=job):
                with self._lock:
                    job["current_bytes"] = progress.get("bytes_done") or 0
                    job["speed"] = progress.get("speed_bytes_per_sec")
                    job["resumed_from"] = progress.get("resumed_from")
                    if job["bytes_total"] is None and progress.get("bytes_total"):
                        job["bytes_total"] = progress["bytes_total"]
                    eta = progress.get("eta_seconds")
                    remaining_parts = job["part_count"] - job["part_index"]
                    if eta is not None and remaining_parts > 0 and job["bytes_total"]:
                        # A per-file ETA understates a split model by however
                        # many parts have not started. Scale it by the bytes
                        # actually left across the whole job instead of
                        # reporting a number that will visibly stall at zero.
                        speed = progress.get("speed_bytes_per_sec")
                        left = job["bytes_total"] - (job["completed_bytes"] + job["current_bytes"])
                        eta = (left / speed) if speed else eta
                    job["eta"] = eta
                self._touch(coalesce=True)

            result = self._download_fn(
                job["repo_id"], part["path"],
                dest_dir=(hearth_hf.model_dir(job["repo_id"], self._store_dir)
                          if self._store_dir else None),
                on_progress=on_progress,
                is_cancelled=cancel.is_set,
                expected_sha256=part.get("sha256"),
                expected_size=part.get("size_bytes"),
            )
            result = result if isinstance(result, dict) else {}

            with self._lock:
                # The FIRST part's path, not the last. llama-server is given
                # the first file of a split model and loads its siblings
                # itself, so this is the path that is actually loadable and
                # the one a "use this model" action must hand back.
                job["path"] = job["path"] or result.get("path")
                job["resumed_from"] = result.get("resumed_from")
                job["already_present"] = bool(result.get("already_present"))
                landed = result.get("bytes_done") or 0
                job["current_bytes"] = landed

            if result.get("cancelled"):
                self._finish(job, STATUS_CANCELLED)
                return
            if not result.get("ok"):
                with self._lock:
                    job["error"] = result.get("error") or "The download failed."
                    job["error_kind"] = result.get("error_kind") or hearth_hf.ERR_HTTP
                self._finish(job, STATUS_ERROR)
                return

            with self._lock:
                job["completed_bytes"] += job["current_bytes"]
                job["current_bytes"] = 0
                job["verified"] = bool(result.get("verified"))
                job["verification"] = result.get("verification")
                if job["bytes_total"] is None and index == len(parts) - 1:
                    job["bytes_total"] = job["completed_bytes"]

        self._finish(job, STATUS_DONE)

    def _finish(self, job, status):
        with self._lock:
            job["status"] = status
            job["finished_at"] = time.time()
            job["speed"] = None
            job["eta"] = None
            if status == STATUS_DONE and job["bytes_total"]:
                # Land the bar exactly on 100%. Resumed parts report
                # bytes_done from byte zero of the file, so the running sum
                # is already right; this only guards the case where the Hub
                # published a size that disagrees with what arrived by a few
                # bytes, where a bar frozen at 99% would be the only visible
                # symptom of a completed, verified download.
                job["completed_bytes"] = max(job["completed_bytes"], job["bytes_total"])
            # Evict here as well as in start(): a session that starts four
            # downloads and then walks away would otherwise keep every one of
            # them forever, since start() is the only other place that trims
            # and by then none of them had finished yet.
            self._evict_locked()
            self._bump_locked()


def _self_test():
    import tempfile

    failures = []

    def check(name, condition, detail=""):
        if condition:
            print("  ok   {}".format(name))
        else:
            failures.append(name)
            print("  FAIL {} {}".format(name, detail))

    # ---- fixtures -----------------------------------------------------------

    def listing_ok(repo_id, **kwargs):  # noqa: ARG001
        return {"ok": True, "files": [
            {"name": "m-Q4_K_M.gguf", "path": "m-Q4_K_M.gguf", "quant": "Q4_K_M",
             "size_bytes": 400, "sha256": "a" * 64, "multipart": False, "part_count": 1,
             "complete": True, "missing_parts": [],
             "parts": [{"path": "m-Q4_K_M.gguf", "size_bytes": 400, "sha256": "a" * 64,
                        "index": 1}]},
            {"name": "m-Q8_0", "path": None, "quant": "Q8_0", "size_bytes": 300,
             "sha256": None, "multipart": True, "part_count": 2, "complete": True,
             "missing_parts": [],
             "parts": [{"path": "m-Q8_0-00001-of-00002.gguf", "size_bytes": 100,
                        "sha256": "b" * 64, "index": 1},
                       {"path": "m-Q8_0-00002-of-00002.gguf", "size_bytes": 200,
                        "sha256": "c" * 64, "index": 2}]},
            {"name": "broken", "path": None, "quant": "Q2_K", "size_bytes": 50,
             "sha256": None, "multipart": True, "part_count": 3, "complete": False,
             "missing_parts": [2, 3],
             "parts": [{"path": "broken-00001-of-00003.gguf", "size_bytes": 50,
                        "sha256": "d" * 64, "index": 1}]},
        ]}

    def listing_gated(repo_id, **kwargs):  # noqa: ARG001
        return {"ok": False, "error_kind": hearth_hf.ERR_GATED,
                "error": "This repository is gated."}

    def fake_download(repo_id, filename, dest_dir=None, on_progress=None,  # noqa: ARG001
                      is_cancelled=None, expected_sha256=None, expected_size=None,
                      **kwargs):
        total = expected_size or 100
        done = 0
        step = max(1, total // 4)
        while done < total:
            if is_cancelled is not None and is_cancelled():
                return {"ok": False, "cancelled": True, "bytes_done": done,
                        "bytes_total": total, "resumed_from": 0,
                        "path": os.path.join(dest_dir or "", filename),
                        "part_path": os.path.join(dest_dir or "", filename) + ".part"}
            done = min(total, done + step)
            if on_progress:
                on_progress({"bytes_done": done, "bytes_total": total, "resumed_from": 0,
                             "fraction": done / total, "speed_bytes_per_sec": 1000.0,
                             "eta_seconds": 0.0, "elapsed_seconds": 0.1})
            time.sleep(0.005)
        return {"ok": True, "cancelled": False, "bytes_done": total, "bytes_total": total,
                "resumed_from": 0, "already_present": False, "verified": True,
                "verification": "sha256",
                "path": os.path.join(dest_dir or "", filename)}

    def wait_for(manager, job_id, statuses, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = manager.snapshot()
            for job in snap["downloads"]:
                if job["id"] == job_id and job["status"] in statuses:
                    return job
            time.sleep(0.01)
        return None

    print("downloads.py self-test")

    # ---- 1. a single-file download runs to done, with real byte counts ------

    m = DownloadManager(list_fn=listing_ok, download_fn=fake_download,
                        progress_interval=0.0)
    started = m.start("acme/Model-GGUF", "m-Q4_K_M.gguf")
    check("start returns a queued job", started["status"] in (STATUS_QUEUED, STATUS_RESOLVING),
          started)
    check("start reports the total size from the repository listing",
          started["bytes_total"] == 400, started)
    finished = wait_for(m, started["id"], (STATUS_DONE,))
    check("a single-file download reaches done", finished is not None)
    check("bytes_done lands on the full size", finished and finished["bytes_done"] == 400,
          finished)
    check("fraction lands on 1.0", finished and finished["fraction"] == 1.0, finished)
    check("a finished job is not cancellable", finished and finished["cancellable"] is False)

    # ---- 2. a split model sums across its parts ----------------------------

    split = m.start("acme/Model-GGUF", "m-Q8_0")
    check("a split quantisation is startable by its logical name",
          split["part_count"] == 2, split)
    check("a split quantisation's total is the sum of its parts",
          split["bytes_total"] == 300, split)
    split_done = wait_for(m, split["id"], (STATUS_DONE,))
    check("a split download reaches done", split_done is not None)
    check("a split download's bytes_done is the sum, not the last part",
          split_done and split_done["bytes_done"] == 300, split_done)

    # ---- 3. refusals ------------------------------------------------------

    def refuses(name, fn, kind=None):
        try:
            fn()
        except DownloadError as exc:
            check(name, kind is None or exc.kind == kind, "kind={}".format(exc.kind))
            return exc
        check(name, False, "no DownloadError raised")
        return None

    refuses("an incomplete split model is refused before any connection",
            lambda: m.start("acme/Model-GGUF", "broken"), hearth_hf.ERR_BAD_REQUEST)
    refuses("a quantisation the repository does not publish is refused",
            lambda: m.start("acme/Model-GGUF", "nope.gguf"), hearth_hf.ERR_NOT_FOUND)
    refuses("an unusable repo id is refused",
            lambda: m.start("../../etc", "m-Q4_K_M.gguf"), hearth_hf.ERR_BAD_REQUEST)

    gated = DownloadManager(list_fn=listing_gated, download_fn=fake_download)
    gated_err = refuses("a gated repository is refused at start, not mid-download",
                        lambda: gated.start("meta/Llama-GGUF", "m.gguf"),
                        hearth_hf.ERR_GATED)
    check("the gated refusal explains that Hearth has no Hugging Face sign-in",
          gated_err is not None and "sign-in" in str(gated_err), str(gated_err))
    check("a refused start creates no job at all", gated.snapshot()["downloads"] == [])

    # ---- 4. cancel leaves a job cancelled, and a duplicate start is refused -

    slow_release = threading.Event()

    def slow_download(repo_id, filename, dest_dir=None, on_progress=None,  # noqa: ARG001
                      is_cancelled=None, expected_size=None, **kwargs):
        total = expected_size or 100
        done = 0
        while done < total:
            if is_cancelled is not None and is_cancelled():
                return {"ok": False, "cancelled": True, "bytes_done": done,
                        "bytes_total": total, "resumed_from": 0, "path": "/tmp/x"}
            done += 1
            if on_progress:
                on_progress({"bytes_done": done, "bytes_total": total, "resumed_from": 0,
                             "speed_bytes_per_sec": 10.0, "eta_seconds": 1.0})
            if slow_release.wait(timeout=0.02):
                pass
        return {"ok": True, "bytes_done": total, "bytes_total": total, "verified": True,
                "verification": "sha256", "path": "/tmp/x"}

    c = DownloadManager(list_fn=listing_ok, download_fn=slow_download,
                        progress_interval=0.0)
    running = c.start("acme/Model-GGUF", "m-Q4_K_M.gguf")
    check("a job in flight is cancellable", wait_for(
        c, running["id"], (STATUS_DOWNLOADING,)) is not None)
    refuses("starting the same file twice while it runs is refused",
            lambda: c.start("acme/Model-GGUF", "m-Q4_K_M.gguf"))
    check("cancel reports that it asked", c.cancel(running["id"]) is True)
    cancelled = wait_for(c, running["id"], (STATUS_CANCELLED,))
    check("a cancelled job settles as cancelled", cancelled is not None, cancelled)
    check("a cancelled job still reports the bytes it got",
          cancelled and cancelled["bytes_done"] > 0, cancelled)
    check("cancelling a settled job reports nothing to cancel",
          c.cancel(running["id"]) is False)
    check("cancelling an unknown job is False, not an error", c.cancel("dl-999") is False)

    # ---- 5. a queued job cancels without ever starting ---------------------

    q = DownloadManager(list_fn=listing_ok, download_fn=slow_download,
                        progress_interval=0.0)
    first = q.start("acme/Model-GGUF", "m-Q4_K_M.gguf")
    second = q.start("acme/Model-GGUF", "m-Q8_0")
    queued = next(j for j in q.snapshot()["downloads"] if j["id"] == second["id"])
    check("a second job waits in the queue with a visible position",
          queued["status"] == STATUS_QUEUED and queued["queue_position"] == 1, queued)
    check("cancelling a queued job succeeds", q.cancel(second["id"]) is True)
    settled = next(j for j in q.snapshot()["downloads"] if j["id"] == second["id"])
    check("a cancelled queued job never runs", settled["status"] == STATUS_CANCELLED
          and settled["bytes_done"] == 0, settled)
    q.cancel(first["id"])
    q.stop()

    # ---- 6. an error is reported with its kind, not swallowed --------------

    def no_space(repo_id, filename, **kwargs):  # noqa: ARG001
        return {"ok": False, "cancelled": False, "bytes_done": 0,
                "error_kind": hearth_hf.ERR_NO_SPACE,
                "error": "Not enough free disk space for {}.".format(filename),
                "disk_check": {"needed_bytes": 999, "free_bytes": 1, "checked": True,
                               "ok": False}}

    e = DownloadManager(list_fn=listing_ok, download_fn=no_space)
    bad = e.start("acme/Model-GGUF", "m-Q4_K_M.gguf")
    errored = wait_for(e, bad["id"], (STATUS_ERROR,))
    check("a failed download settles as error", errored is not None, errored)
    check("the disk-space failure keeps its own error_kind",
          errored and errored["error_kind"] == hearth_hf.ERR_NO_SPACE, errored)
    check("the disk-space failure keeps its own message",
          errored and "disk space" in (errored["error"] or ""), errored)

    # ---- 7. the version counter and snapshot_after -------------------------

    v = DownloadManager(list_fn=listing_ok, download_fn=fake_download,
                        progress_interval=0.0)
    before = v.snapshot()["version"]
    seen = {}

    def watcher():
        seen["snap"] = v.snapshot_after(before, timeout=5)

    watcher_thread = threading.Thread(target=watcher, daemon=True)
    watcher_thread.start()
    time.sleep(0.05)
    v.start("acme/Model-GGUF", "m-Q4_K_M.gguf")
    watcher_thread.join(timeout=5)
    check("snapshot_after wakes on a change", seen.get("snap") is not None)
    check("snapshot_after reports a higher version",
          seen.get("snap") and seen["snap"]["version"] > before, seen.get("snap"))
    check("snapshot_after carries the whole list, not a delta",
          seen.get("snap") and len(seen["snap"]["downloads"]) == 1, seen.get("snap"))
    timed_out = v.snapshot_after(10 ** 9, timeout=0.05)
    check("snapshot_after returns the current state on timeout rather than nothing",
          isinstance(timed_out, dict) and "downloads" in timed_out, timed_out)

    # ---- 8. dismiss ---------------------------------------------------------

    d = DownloadManager(list_fn=listing_ok, download_fn=slow_download,
                        progress_interval=0.0)
    live = d.start("acme/Model-GGUF", "m-Q4_K_M.gguf")
    wait_for(d, live["id"], (STATUS_DOWNLOADING,))
    check("a running job cannot be dismissed", d.dismiss(live["id"]) is False)
    d.cancel(live["id"])
    wait_for(d, live["id"], (STATUS_CANCELLED,))
    check("a settled job can be dismissed", d.dismiss(live["id"]) is True)
    check("a dismissed job is gone", d.snapshot()["downloads"] == [])
    d.stop()

    # ---- 9. retention is bounded -------------------------------------------

    r = DownloadManager(list_fn=listing_ok, download_fn=fake_download,
                        retain_finished=2, progress_interval=0.0)
    for _ in range(4):
        job = r.start("acme/Model-GGUF", "m-Q4_K_M.gguf")
        wait_for(r, job["id"], (STATUS_DONE,))
    check("finished jobs are retained but bounded",
          len(r.snapshot()["downloads"]) <= 2, r.snapshot())

    # ---- 10. local_file_state sees a partial, which is what makes cancel ----
    # ----     honest rather than "you lost 3 GB" ----------------------------

    with tempfile.TemporaryDirectory() as tmp:
        store = os.path.join(tmp, "store")
        target_dir = hearth_hf.model_dir("acme/Model-GGUF", store)
        os.makedirs(target_dir, exist_ok=True)
        with open(os.path.join(target_dir, "m-Q4_K_M.gguf" + hearth_hf.PART_SUFFIX), "wb") as fh:
            fh.write(b"x" * 1234)
        state = local_file_state("acme/Model-GGUF", "m-Q4_K_M.gguf", store_dir=store)
        check("a leftover .part is reported as resumable bytes",
              state["partial_bytes"] == 1234, state)
        check("a leftover .part is not reported as a present model",
              state["present"] is False, state)
        with open(os.path.join(target_dir, "done.gguf"), "wb") as fh:
            fh.write(b"y" * 77)
        present = local_file_state("acme/Model-GGUF", "done.gguf", store_dir=store)
        check("a finished file is reported as present with its size",
              present["present"] is True and present["bytes_present"] == 77, present)
        quant_state = quant_local_state("acme/Model-GGUF", {
            "parts": [{"path": "done.gguf"}, {"path": "missing.gguf"}]}, store_dir=store)
        check("a split model is only 'present' when every part is",
              quant_state["present"] is False and quant_state["parts_present"] == 1,
              quant_state)
        check("an unusable repo id yields an empty annotation rather than an exception",
              local_file_state("../../etc", "x.gguf")["path"] is None)

    print()
    if failures:
        print("FAILED: {}".format(", ".join(failures)))
        return 1
    print("all downloads.py self-tests passed")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(_self_test())
    print("downloads.py is a library; run with --self-test", file=sys.stderr)
    raise SystemExit(2)
