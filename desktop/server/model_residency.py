#!/usr/bin/env python3
"""Model residency: what the loaded model costs in memory, and freeing it.

A local model is the single largest thing Hearth holds: several GB of VRAM,
or of RAM on a machine without a usable GPU, for as long as the bundled
llama-server is up. Until this existed, that was until the app exited. A
person who chats for five minutes and then switches to a game should not
have to quit Hearth to get their graphics card back.

This module is the sidecar's half of that (the backend's half is
hearth_backend.LlamaBackend.unload and residency_snapshot):

  * GET /model reads snapshot(): which model, loaded or loading, an
    approximate memory figure, whether anything is using it, and when the
    idle timer would free it.
  * POST /model/unload calls unload(): refused with 409 while a turn, the
    work loop or a swarm is running, or while the model is loading or
    answering, because stopping a server mid-generation is the one thing
    this must never do.
  * POST /model/autounload sets the idle delay, persisted here in its own
    small prefs file.
  * A daemon watcher frees the model after that long without use. It
    defers (never unloads) while anything is busy, and it only acts on the
    bundled engine: an Ollama daemon already unloads on its own keep-alive
    schedule, and quietly evicting a model another program may be using is
    not Hearth's call to make on a timer. The manual button still works
    for Ollama, because a click is the user's call.

WHICH BACKEND IS ACTED ON. hearth_backend keeps one instance per backend
for the life of the process, so a session that used both the bundled engine
and Ollama has both built. Every unload here names its target: the timer
names the bundled engine only, and the button names the backend the chip
is showing. Neither ever walks every instance, because that would also tell
Ollama to drop the model Hearth last used there, which is neither shown on
the chip nor (for the timer) Hearth's decision to make.

The next prompt after an unload reloads the model through the backend's
ordinary load path. Nothing here starts a model.

THE BUSY RULE. Busy is the backend's own in-flight count (authoritative: it
covers every request on any session, including one being replaced), OR a
load in progress, OR the sidecar saying a session is working (state_busy_fn,
SidecarState.model_busy_reason). The last is what keeps a model resident
between the model calls of a running turn, while tools execute.

Standard library only. Never builds a backend and never runs backend
selection: hearth_backend.get_backend() probes Ollama and constructs
instances, which a status read has no business doing.
"""

import json
import os
import sys
import tempfile
import threading
import time
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT_DIR = os.path.join(os.path.dirname(os.path.dirname(_HERE)), "agent")
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

import hearth_backend  # noqa: E402
import hearth_paths  # noqa: E402


PREFS_FILENAME = "model_residency.json"
PREFS_SCHEMA = 1

#: The idle delays a person can choose, in minutes. None means never.
AUTO_UNLOAD_OPTIONS = (5, 15, 30, 60, None)
DEFAULT_AUTO_UNLOAD_MINUTES = 15

#: The only backend the idle timer ever unloads: the bundled engine, whose
#: memory Hearth owns. Ollama runs its own keep-alive schedule.
TIMER_BACKENDS = (hearth_backend.BACKEND_LLAMA,)

#: How often the watcher looks. The shortest delay is five minutes, so a
#: twenty-second granularity is invisible and costs nothing.
DEFAULT_POLL_SECONDS = 20


def valid_minutes(value):
    """True for exactly the values AUTO_UNLOAD_OPTIONS lists. A bool is not
    a number here, even though Python thinks True == 1."""
    if value is None:
        return True
    return (isinstance(value, int) and not isinstance(value, bool)
            and value in AUTO_UNLOAD_OPTIONS)


def _retry_io(fn, attempts=3, delay=0.1):
    """fn(), retried briefly on a Windows sharing violation. The same
    reasoning as hearth_tools._retry_io: antivirus, the Search Indexer and
    sync clients hold files open for moments at a time."""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except PermissionError as exc:
            last = exc
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (5, 32):
                raise
            last = exc
        if i < attempts - 1:
            time.sleep(delay * (i + 1))
    raise last


class ResidencyManager:
    """The model chip's backend: status, manual unload, the idle timer.

    Everything is injectable so the self-tests never need a real engine:
    `backend` is anything with residency_snapshot(probe) and
    unload_all(only_if_idle, backends) (the hearth_backend module by default),
    `state_busy_fn` returns a reason string or None, `now_fn` is the wall
    clock the idle timer reads (the backend stamps last_used_at with
    time.time()), and `poll_seconds` of None or 0 means no watcher thread,
    so a test drives tick() itself.
    """

    def __init__(self, state_busy_fn=None, backend=None, prefs_path=None,
                 now_fn=None, poll_seconds=DEFAULT_POLL_SECONDS, log_fn=None):
        self.state_busy_fn = state_busy_fn or (lambda: None)
        self.backend = backend if backend is not None else hearth_backend
        self._prefs_path = prefs_path
        self.now_fn = now_fn or time.time
        self.poll_seconds = poll_seconds
        self.log_fn = log_fn or (lambda text: print(text, file=sys.stderr))
        self._prefs_lock = threading.Lock()
        self._lock = threading.Lock()
        self._last_busy_at = None      # when the watcher last saw anything busy
        self._loaded_seen_at = None    # first sight of the current resident model
        self._loaded_ref = None
        self._last_auto_unload_at = None
        self._thread = None
        self._stop = threading.Event()
        self._logged_failure = False

    # -- prefs ---------------------------------------------------------------

    def prefs_path(self):
        """Resolved at call time, not construction, so HEARTH_DATA_DIR set
        by a test (or by main.py) is the one honoured."""
        if self._prefs_path:
            return self._prefs_path
        return os.path.join(hearth_paths.data_dir(), PREFS_FILENAME)

    def _read_prefs_locked(self):
        try:
            with open(self.prefs_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get_minutes(self):
        """The idle delay in minutes, or None for never. A missing, corrupt
        or unrecognised file reads as the default, never as an error: a
        preference is not worth a broken model chip."""
        with self._prefs_lock:
            data = self._read_prefs_locked()
        if "auto_unload_minutes" not in data:
            return DEFAULT_AUTO_UNLOAD_MINUTES
        value = data["auto_unload_minutes"]
        return value if valid_minutes(value) else DEFAULT_AUTO_UNLOAD_MINUTES

    def set_minutes(self, minutes):
        """Persist a new delay. Raises ValueError, naming the options, for
        anything AUTO_UNLOAD_OPTIONS does not list; never clamps.

        Written atomically: a uniquely named temp file in the same directory,
        then os.replace, so a crash or a concurrent reader sees either the
        old file or the new one and never half of one."""
        if not valid_minutes(minutes):
            raise ValueError("minutes must be one of 5, 15, 30, 60 or null (never)")
        path = self.prefs_path()
        with self._prefs_lock:
            data = self._read_prefs_locked()
            data["schema"] = PREFS_SCHEMA
            data["auto_unload_minutes"] = minutes
            folder = os.path.dirname(path) or "."
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".model_residency.", suffix="."
                                       + uuid.uuid4().hex[:8] + ".tmp", dir=folder)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2, sort_keys=True)
                    fh.flush()
                    os.fsync(fh.fileno())
                _retry_io(lambda: os.replace(tmp, path))
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        return minutes

    # -- status --------------------------------------------------------------

    def _busy_reason(self, snap):
        """Why the model must stay put right now, or None."""
        try:
            reason = self.state_busy_fn()
        except Exception:  # noqa: BLE001 - a broken busy check counts as busy
            reason = "Hearth could not tell whether a turn is running"
        if reason:
            return reason
        if snap.get("loading"):
            return "the model is still loading"
        if int(snap.get("inflight_total", snap.get("inflight")) or 0) > 0:
            return "the model is answering a request"
        return None

    def _note_loaded(self, snap):
        """Track when the current resident model was first seen, so a model
        with no last_used_at (which a load always stamps, but defensively)
        still has an idle clock that starts somewhere."""
        with self._lock:
            ref = snap.get("ref") if snap.get("loaded") else None
            if ref != self._loaded_ref:
                self._loaded_ref = ref
                self._loaded_seen_at = self.now_fn() if ref else None

    def _idle_since(self, snap):
        """The later of the last request's end and the last busy sighting.
        First sight of the model stands in for the former only when the
        backend has no last_used_at at all."""
        last_used = snap.get("last_used_at")
        with self._lock:
            if not isinstance(last_used, (int, float)):
                last_used = self._loaded_seen_at
            candidates = [last_used, self._last_busy_at]
        known = [c for c in candidates if isinstance(c, (int, float))]
        return max(known) if known else None

    def _times_out(self, snap):
        """Whether the idle timer applies to this model at all: only a
        model the bundled engine holds, and only with a delay set."""
        return bool(snap.get("loaded")) and snap.get("managed_by") == "hearth"

    def snapshot(self, probe=True):
        """GET /model's body."""
        try:
            snap = dict(self.backend.residency_snapshot(probe=probe))
        except Exception as exc:  # noqa: BLE001 - a status read never 500s
            snap = {"backend": None, "managed_by": None, "loaded": None,
                    "loading": False, "ref": None, "model": None, "inflight": 0,
                    "last_used_at": None, "memory": {"vram_bytes": None,
                                                     "rss_bytes": None,
                                                     "approximate": True},
                    "note": "model status unavailable: {}".format(type(exc).__name__)}
        self._note_loaded(snap)
        reason = self._busy_reason(snap)
        minutes = self.get_minutes()
        unload_at = None
        if self._times_out(snap) and minutes is not None and reason is None:
            since = self._idle_since(snap)
            if since is not None:
                unload_at = since + minutes * 60
        if snap.get("loaded") is None:
            status = "unknown"
        elif snap.get("loading"):
            status = "loading"
        else:
            status = "loaded" if snap.get("loaded") else "not_loaded"
        memory = snap.get("memory") or {}
        return {
            "backend": snap.get("backend"),
            "managed_by": snap.get("managed_by"),
            "model": snap.get("model"),
            "ref": snap.get("ref"),
            "status": status,
            "loaded": snap.get("loaded"),
            "loading": bool(snap.get("loading")),
            "memory": {"vram_bytes": memory.get("vram_bytes"),
                       "rss_bytes": memory.get("rss_bytes"),
                       "approximate": True},
            "inflight": int(snap.get("inflight_total", snap.get("inflight")) or 0),
            "busy": reason is not None,
            "busy_reason": reason,
            "auto_unload_minutes": minutes,
            "auto_unload_options": list(AUTO_UNLOAD_OPTIONS),
            "last_used_at": snap.get("last_used_at"),
            "unload_at": unload_at,
            "note": snap.get("note"),
        }

    # -- manual unload -------------------------------------------------------

    def unload(self):
        """POST /model/unload. Returns (http_status, body).

        409 {"error": reason} while busy (checked here first, then again by
        the backend under its own lock, which is the check that actually
        closes the race). 200 with the fresh snapshot otherwise, carrying
        unloaded true or false and why. 502 when Ollama would not answer.

        Only the backend the chip is showing is unloaded (see WHICH BACKEND
        IS ACTED ON in the module docstring). Whatever freed memory is a
        200, even when something else also refused: a 409 after the model
        was in fact stopped would tell the person the opposite of what
        happened."""
        snap = self.snapshot(probe=False)
        if snap["busy"]:
            return 409, {"error": snap["busy_reason"]}
        target = snap.get("backend")
        try:
            result = self.backend.unload_all(
                only_if_idle=True, backends=(target,) if target else ())
        except Exception as exc:  # noqa: BLE001 - surfaced, not raised
            return 502, {"error": "unload failed: {}: {}".format(type(exc).__name__, exc)}
        if not result.get("unloaded"):
            if result.get("busy"):
                return 409, {"error": result.get("reason") or "the model is in use"}
            if result.get("error"):
                return 502, {"error": result.get("reason") or "unload failed"}
        body = self.snapshot(probe=True)
        body["unloaded"] = bool(result.get("unloaded"))
        body["reason"] = None if body["unloaded"] else result.get("reason")
        return 200, body

    # -- the idle timer --------------------------------------------------------

    def tick(self):
        """One look by the watcher. Returns what it decided, for the tests
        and for nothing else: "busy", "disabled", "idle" (nothing to do),
        "waiting", "unloaded" or "refused"."""
        try:
            snap = dict(self.backend.residency_snapshot(probe=False))
        except Exception:  # noqa: BLE001 - try again next tick
            return "idle"
        self._note_loaded(snap)
        now = self.now_fn()
        if self._busy_reason(snap) is not None:
            # Defer: remember that it was busy just now. The idle clock then
            # restarts from the end of the busy spell, so a model is never
            # freed a moment after a long tool call finishes.
            with self._lock:
                self._last_busy_at = now
            return "busy"
        minutes = self.get_minutes()
        if minutes is None:
            return "disabled"
        if not self._times_out(snap):
            return "idle"
        since = self._idle_since(snap)
        if since is None or now - since < minutes * 60:
            return "waiting"
        result = self.backend.unload_all(only_if_idle=True, backends=TIMER_BACKENDS)
        if result.get("unloaded"):
            with self._lock:
                self._last_auto_unload_at = now
            return "unloaded"
        return "refused"

    def ensure_started(self):
        """Start the watcher once. Lazy, from the first GET /model, so a
        process that never shows the chip (every self-test that does not
        ask) never runs a thread."""
        if not self.poll_seconds:
            return
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="hearth-model-residency",
                                            daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)

    def _run(self):
        while not self._stop.wait(self.poll_seconds):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - the watcher must not die
                if not self._logged_failure:
                    self._logged_failure = True
                    try:
                        self.log_fn("[model-residency] idle check failed: {}: {}".format(
                            type(exc).__name__, exc))
                    except Exception:  # noqa: BLE001
                        pass


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

class _FakeBackend:
    """residency_snapshot/unload_all over a scripted state."""

    def __init__(self):
        self.state = {"backend": "llama", "managed_by": "hearth", "loaded": True,
                      "loading": False, "ref": "gguf:m.gguf", "model": "m.gguf",
                      "inflight": 0, "inflight_total": 0, "last_used_at": 1000.0,
                      "memory": {"vram_bytes": 4 * 1024 ** 3, "rss_bytes": 1,
                                 "approximate": True},
                      "note": None}
        self.unloads = 0
        self.targets = []

    def residency_snapshot(self, probe=True):
        return dict(self.state)

    def unload_all(self, only_if_idle=True, backends=None):
        self.targets.append(None if backends is None else tuple(backends))
        if backends is not None and self.state["backend"] not in backends:
            return {"unloaded": False, "busy": False, "reason": "nothing is loaded"}
        if self.state["inflight_total"] or self.state["loading"]:
            return {"unloaded": False, "busy": True, "reason": "the model is busy"}
        if not self.state["loaded"]:
            return {"unloaded": False, "busy": False, "reason": "nothing is loaded"}
        self.unloads += 1
        self.state["loaded"] = False
        return {"unloaded": True, "busy": False, "reason": None}


def _real_test_both_backends():
    """The timer and the button against the real hearth_backend with a
    LlamaBackend and an OllamaBackend both built. Ollama's HTTP is faked and
    every /api/generate is recorded: the timer must send none, and the
    button must send one only when the chip is showing Ollama."""
    hb = hearth_backend

    class _Proc:
        def __init__(self, pid):
            self.pid = pid
            self.stopped = False

        def poll(self):
            return 0 if self.stopped else None

    class _Server:
        def __init__(self, pid):
            self.proc = _Proc(pid)

        def stop(self):
            self.proc.stopped = True

    posted = []

    def _fake_http(url, timeout, body=None):
        if url.endswith("/api/ps"):
            return {"models": [{"name": "ours:7b", "model": "ours:7b",
                                "size": 6 * 1024 ** 3, "size_vram": 5 * 1024 ** 3}]}
        if url.endswith("/api/chat"):
            return {"message": {"role": "assistant", "content": "ok"},
                    "prompt_eval_count": 1, "eval_count": 1}
        if url.endswith("/api/generate"):
            posted.append(body)
            return {"done": True, "done_reason": "unload"}
        raise AssertionError("unexpected Ollama call " + url)

    saved_instances = dict(hb._INSTANCES)
    saved_active = hb._ACTIVE
    real_http = hb._http_json
    hb._http_json = _fake_http
    try:
        llama = hb.LlamaBackend()
        llama._server = _Server(4242)
        llama._ref = hb.ModelRef.gguf(os.path.join("models", "m.gguf"))
        llama.last_used_at = 0.0  # idle since the epoch: long past any delay
        ollama = hb.OllamaBackend("http://ollama.invalid")
        ollama.chat("ours:7b", [{"role": "user", "content": "x"}])
        with hb._ACTIVE_LOCK:
            hb._INSTANCES.clear()
            hb._INSTANCES.update({hb.BACKEND_LLAMA: llama, hb.BACKEND_OLLAMA: ollama})
            hb._ACTIVE = ollama
        clock = {"now": 10.0 ** 9}
        mgr = ResidencyManager(backend=hb, now_fn=lambda: clock["now"], poll_seconds=None,
                               prefs_path=os.path.join(hearth_paths.data_dir(), "both.json"))
        mgr.set_minutes(5)
        assert mgr.tick() == "unloaded", "the idle bundled engine must be freed"
        assert llama.server is None
        assert posted == [], ("the timer told Ollama to unload", posted)
        # Ollama on display now, model resident, idle for an hour: still
        # the timer's business never.
        ollama.last_used_at = clock["now"] - 3600
        assert mgr.snapshot(probe=False)["managed_by"] == "ollama"
        assert mgr.tick() == "idle" and posted == [], posted
        # The button with Ollama on display frees Ollama's model, and only it.
        code, body = mgr.unload()
        assert code == 200 and body["unloaded"] is True, (code, body)
        assert posted == [{"model": "ours:7b", "keep_alive": 0, "stream": False}], posted
        # The button with the bundled engine on display leaves Ollama alone.
        posted.clear()
        llama._server = _Server(4243)
        llama._ref = hb.ModelRef.gguf(os.path.join("models", "m.gguf"))
        assert mgr.snapshot(probe=False)["backend"] == hb.BACKEND_LLAMA
        code, body = mgr.unload()
        assert code == 200 and body["unloaded"] is True and llama.server is None, (code, body)
        assert posted == [], ("the button unloaded a backend it was not showing", posted)
    finally:
        hb._http_json = real_http
        with hb._ACTIVE_LOCK:
            hb._INSTANCES.clear()
            hb._INSTANCES.update(saved_instances)
            hb._ACTIVE = saved_active


def _self_test():
    tmp = tempfile.mkdtemp(prefix="hearth-residency-")
    prev = os.environ.get("HEARTH_DATA_DIR")
    os.environ["HEARTH_DATA_DIR"] = tmp
    try:
        # -- prefs: default, persistence, validation -----------------------
        mgr = ResidencyManager(backend=_FakeBackend(), poll_seconds=None)
        assert mgr.prefs_path() == os.path.join(tmp, PREFS_FILENAME), mgr.prefs_path()
        assert mgr.get_minutes() == DEFAULT_AUTO_UNLOAD_MINUTES
        for value in (5, 30, 60, None, 15):
            mgr.set_minutes(value)
            again = ResidencyManager(backend=_FakeBackend(), poll_seconds=None)
            assert again.get_minutes() == value, (value, again.get_minutes())
        with open(mgr.prefs_path(), encoding="utf-8") as fh:
            on_disk = json.load(fh)
        assert on_disk == {"schema": 1, "auto_unload_minutes": 15}, on_disk
        # Nothing but the prefs file is left behind: no temp files.
        assert os.listdir(tmp) == [PREFS_FILENAME], os.listdir(tmp)
        for bad in (0, 7, 61, -5, "15", 15.0, True, False, [15], {"m": 1}):
            try:
                mgr.set_minutes(bad)
                raise AssertionError("accepted {!r}".format(bad))
            except ValueError as exc:
                assert "5, 15, 30, 60" in str(exc), exc
        assert mgr.get_minutes() == 15, "a refused value must not be written"

        # Corrupt, wrong-shaped and unknown values read as the default.
        for text in ("{not json", "[1, 2]", '{"auto_unload_minutes": 7}',
                     '{"auto_unload_minutes": "never"}', '{"auto_unload_minutes": true}',
                     "", "\x00\x01"):
            with open(mgr.prefs_path(), "w", encoding="utf-8") as fh:
                fh.write(text)
            assert mgr.get_minutes() == DEFAULT_AUTO_UNLOAD_MINUTES, text
        # ...and a write over a corrupt file repairs it.
        mgr.set_minutes(30)
        assert mgr.get_minutes() == 30
        # An explicit null is "never", not "missing".
        with open(mgr.prefs_path(), "w", encoding="utf-8") as fh:
            fh.write('{"schema": 1, "auto_unload_minutes": null}')
        assert mgr.get_minutes() is None

        # A failed replace leaves the old file intact and no temp behind.
        mgr.set_minutes(5)
        real_replace = os.replace

        def _refuse(src, dst):
            raise OSError(28, "No space left on device")
        os.replace = _refuse
        try:
            try:
                mgr.set_minutes(60)
                raise AssertionError("a failed replace must raise")
            except OSError:
                pass
        finally:
            os.replace = real_replace
        assert mgr.get_minutes() == 5 and os.listdir(tmp) == [PREFS_FILENAME], os.listdir(tmp)

        # Concurrent writers serialise: every value read back is a whole one.
        def _writer(v):
            for _ in range(20):
                mgr.set_minutes(v)
        threads = [threading.Thread(target=_writer, args=(v,)) for v in (5, 15, 30, 60)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert mgr.get_minutes() in (5, 15, 30, 60)
        assert os.listdir(tmp) == [PREFS_FILENAME], os.listdir(tmp)

        # -- the timer: fires after the delay, never while busy -------------
        clock = {"now": 1000.0}
        busy = {"reason": None}
        fake = _FakeBackend()
        mgr = ResidencyManager(state_busy_fn=lambda: busy["reason"], backend=fake,
                               now_fn=lambda: clock["now"], poll_seconds=None)
        mgr.set_minutes(15)
        snap = mgr.snapshot()
        assert snap["status"] == "loaded" and snap["busy"] is False, snap
        assert snap["unload_at"] == 1000.0 + 15 * 60, snap
        assert snap["auto_unload_options"] == [5, 15, 30, 60, None], snap
        clock["now"] = 1000.0 + 14 * 60
        assert mgr.tick() == "waiting" and fake.unloads == 0
        # Busy at the moment the delay would have elapsed: deferred, and the
        # idle clock restarts from when it was last seen busy.
        clock["now"] = 1000.0 + 16 * 60
        busy["reason"] = "a turn is running"
        assert mgr.tick() == "busy" and fake.unloads == 0
        snap = mgr.snapshot()
        assert snap["busy"] and snap["busy_reason"] == "a turn is running", snap
        assert snap["unload_at"] is None, "no countdown is shown while busy"
        busy["reason"] = None
        clock["now"] += 60
        assert mgr.tick() == "waiting", "the delay must restart after a busy spell"
        # In-flight work on the backend defers too, even with no session busy
        # (a session being replaced still has a model call in flight).
        fake.state["inflight_total"] = 1
        clock["now"] += 20 * 60
        assert mgr.tick() == "busy" and fake.unloads == 0
        fake.state["inflight_total"] = 0
        fake.state["loading"] = True
        clock["now"] += 20 * 60
        assert mgr.tick() == "busy" and fake.unloads == 0, "never unload while loading"
        fake.state["loading"] = False
        clock["now"] += 15 * 60 + 1
        assert mgr.tick() == "unloaded" and fake.unloads == 1
        assert mgr.snapshot()["status"] == "not_loaded"
        assert mgr.tick() == "idle", "nothing loaded is nothing to do"

        # "Never" means never.
        fake.state.update(loaded=True, last_used_at=clock["now"])
        mgr.set_minutes(None)
        clock["now"] += 10 * 3600
        assert mgr.tick() == "disabled" and fake.unloads == 1
        assert mgr.snapshot()["unload_at"] is None
        # A shorter delay applies to a model already idle that long.
        mgr.set_minutes(5)
        assert mgr.tick() == "unloaded" and fake.unloads == 2

        # Ollama manages its own memory: the timer leaves it alone.
        fake.state.update(loaded=True, managed_by="ollama", backend="ollama",
                          last_used_at=clock["now"] - 3600)
        assert mgr.tick() == "idle" and fake.unloads == 2
        assert mgr.snapshot()["unload_at"] is None
        fake.state.update(managed_by="hearth", backend="llama")

        # An unreachable Ollama reads as unknown, not as an error.
        fake.state["loaded"] = None
        assert mgr.snapshot()["status"] == "unknown"
        fake.state["loaded"] = True

        # -- manual unload: 409 while busy, 200 otherwise -------------------
        busy["reason"] = "the work loop is running"
        code, body = mgr.unload()
        assert code == 409 and body == {"error": "the work loop is running"}, (code, body)
        busy["reason"] = None
        fake.state["inflight_total"] = 1
        code, body = mgr.unload()
        assert code == 409 and "answering" in body["error"], (code, body)
        fake.state["inflight_total"] = 0
        code, body = mgr.unload()
        assert code == 200 and body["unloaded"] is True and body["status"] == "not_loaded", body
        code, body = mgr.unload()
        assert code == 200 and body["unloaded"] is False, body
        assert body["reason"] == "nothing is loaded", body

        # A broken busy check is treated as busy, never as permission.
        def _broken():
            raise RuntimeError("boom")
        fake.state["loaded"] = True
        mgr2 = ResidencyManager(state_busy_fn=_broken, backend=fake, poll_seconds=None)
        code, body = mgr2.unload()
        assert code == 409, (code, body)

        # A backend whose status read raises still yields a snapshot.
        class _Raising:
            def residency_snapshot(self, probe=True):
                raise RuntimeError("no")

            def unload_all(self, only_if_idle=True, backends=None):
                raise RuntimeError("no")
        snap = ResidencyManager(backend=_Raising(), poll_seconds=None).snapshot()
        assert snap["status"] == "unknown" and snap["busy"] is False, snap
        assert ResidencyManager(backend=_Raising(), poll_seconds=None).tick() == "idle"

        # Every unload names its target: the timer only ever the bundled
        # engine, the button the backend on display.
        assert fake.targets and all(t is not None for t in fake.targets), fake.targets
        fake.targets.clear()
        fake.state.update(loaded=True, managed_by="hearth", backend="llama",
                          last_used_at=0.0)
        mgr.set_minutes(5)
        assert mgr.tick() == "unloaded" and fake.targets == [TIMER_BACKENDS], fake.targets
        fake.state.update(loaded=True, managed_by="ollama", backend="ollama")
        fake.targets.clear()
        code, body = mgr.unload()
        assert code == 200 and fake.targets == [("ollama",)], (code, fake.targets)

        # Freed memory is a 200 even if another backend refused at the same
        # time: a 409 would say nothing was stopped when something was.
        class _Mixed(_FakeBackend):
            def unload_all(self, only_if_idle=True, backends=None):
                return {"unloaded": True, "busy": True, "error": False,
                        "reason": "the model is answering a request"}
        code, body = ResidencyManager(backend=_Mixed(), poll_seconds=None).unload()
        assert code == 200 and body["unloaded"] is True and body["reason"] is None, (code, body)

        # -- with BOTH real backends built, the timer leaves Ollama alone ----
        # The real hearth_backend, not a fake: a session that used the
        # bundled engine and then an Ollama model has both instances, and
        # the timer must free the engine without telling Ollama anything.
        _real_test_both_backends()

        # -- the watcher thread runs, swallows failures, and stops ----------
        logged = []

        class _Flaky(_FakeBackend):
            calls = 0

            def residency_snapshot(self, probe=True):
                type(self).calls += 1
                return super().residency_snapshot(probe)

            def unload_all(self, only_if_idle=True, backends=None):
                raise RuntimeError("engine went away")

        flaky = _Flaky()
        flaky.state["last_used_at"] = 0.0
        mgr3 = ResidencyManager(backend=flaky, poll_seconds=0.02,
                                now_fn=lambda: 10 ** 9, log_fn=logged.append)
        mgr3.set_minutes(5)
        mgr3.ensure_started()
        mgr3.ensure_started()  # idempotent: still one thread
        deadline = time.monotonic() + 5
        while _Flaky.calls < 5 and time.monotonic() < deadline:
            time.sleep(0.02)
        mgr3.stop()
        assert _Flaky.calls >= 5, "the watcher died after its first failure"
        assert len(logged) == 1 and "engine went away" in logged[0], logged
        assert not mgr3._thread.is_alive()

        # The real backend module satisfies the interface without building
        # anything (no instance exists in this process).
        real = ResidencyManager(poll_seconds=None).snapshot()
        assert real["status"] == "not_loaded" and real["backend"] is None, real
    finally:
        if prev is None:
            os.environ.pop("HEARTH_DATA_DIR", None)
        else:
            os.environ["HEARTH_DATA_DIR"] = prev
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("model_residency self-test: ok")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    print("model_residency.py is a library; run with --self-test", file=sys.stderr)
    sys.exit(2)
