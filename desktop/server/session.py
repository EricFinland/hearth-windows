#!/usr/bin/env python3
"""hearth desktop sidecar session: the one live agent session the UI drives.

A Session holds the workspace path, the chosen model, the permission mode,
and everything needed to run one turn at a time: an append-only event log
(for GET /events to stream), a pending-approvals table (for POST /approve to
resolve), and a per-turn cancel flag (for POST /cancel).

This module defines the seam the real agent engine will plug into, without
importing it. An Engine is any object with a `run(ctx)` method, where `ctx`
is a TurnContext offering:

  ctx.workspace, ctx.model, ctx.mode, ctx.message   -- the turn's inputs
  ctx.emit(kind, data)                              -- push an SSE event
  ctx.request_approval(tool, args) -> "allow"|"deny" -- gate a tool call,
                                                         blocking until
                                                         POST /approve
                                                         resolves it (or the
                                                         turn is cancelled)
  ctx.cancelled() -> bool                           -- poll for POST /cancel

The eventual real engine wraps hearth_loop.chat(...) and permissions.decide
(...) inside run(): decide() returning "gate" becomes a call to
ctx.request_approval(); "allow" runs the tool directly; "deny" is reported as
a tool_call event with decision "deny" and the loop continues. None of that
is wired up here -- app.py's self-test drives this seam with a fake engine
so the transport, streaming, approval flow, and cancellation are all proven
before hearth_loop exists in the picture.

Two properties added after the first security pass:

  1. Cancellation abandons rather than kills the engine's in-flight blocking
     call (see engine.py's _run_cancellable): Session.status flips back to
     "idle" as soon as engine.run() returns, which can be well before that
     abandoned call actually finishes. A Session therefore also tracks how
     many such calls are still live, independently of `status`, via
     _worker_started()/_worker_finished(); is_workspace_busy() is the
     combined signal ("running" OR "an abandoned worker is still going") that
     anything about to touch the workspace on disk (POST /restore, replacing
     the session) must check instead of `status` alone.

  2. `_events` is a bounded ring (events_cap, default EVENTS_CAP), not an
     unbounded list -- a session that runs for a long time must not grow
     memory forever just from its own event log. A client that reconnects
     with a `since`/Last-Event-ID older than anything still retained gets an
     explicit synthetic "events_dropped" event instead of a silently
     truncated history, so it can tell "I'm caught up" apart from "I missed
     something and don't know what".

Two more bounded structures, added after a review found them growing
without limit:

  3. `_approvals` is a bounded dict (approvals_cap, default APPROVALS_CAP).
     Approval.args retains the tool call's full arguments for as long as the
     Approval object lives, and for write_file in edit mode (the default
     permission mode) that is the entire file body being written -- a
     session that writes 200 files would otherwise retain all 200 bodies for
     its whole lifetime. Eviction only ever removes an already-RESOLVED
     approval (Approval.decision is not None), oldest insertion order first:
     at most one approval is ever pending at a time, because RealEngine.run()
     blocks on ctx.request_approval() before issuing the next tool call, so
     an entry a turn is still blocked on (decision is None) can never be the
     one an eviction pass removes. See _evict_approvals_locked.

     cancel() now also resolves every pending approval it wakes (setting
     decision to "deny", not just firing the event) instead of leaving
     decision as None forever -- previously a cancelled-but-never-explicitly-
     resolved approval could never become eligible for eviction at all, since
     "resolved" was the only safe eviction signal.

  4. `_cancel_flags` never held more than one live entry's worth of purpose
     (only the *current* turn's flag is ever consulted -- see cancel() and
     _is_cancelled()), so instead of a generic cap it is simply cleared each
     time a new turn starts (submit_prompt). Nothing looks up a previous
     turn's flag once that turn's submit_prompt._run() has returned:
     _run_cancellable's polling loop (the only reader of _is_cancelled other
     than the engine's own ctx.cancelled() calls, which stop once a turn
     returns) lives on the turn's own calling thread and stops polling the
     instant it observes cancellation, and an abandoned worker thread never
     consults the flag itself. So clearing the dict at the top of each new
     submit_prompt cannot drop a flag anything still needs.

Standard library only.
"""

import collections
import itertools
import secrets
import sys
import threading
import time
import uuid

MODES = ("plan", "edit", "auto", "bypass")
DEFAULT_MODE = "edit"

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"

EVENTS_CAP = 500  # bounded ring for Session._events; see module docstring
APPROVALS_CAP = 200  # bounded dict for Session._approvals; see module docstring


class Approval:
    """One pending or resolved tool-call approval."""

    def __init__(self, approval_id, tool, args):
        self.id = approval_id
        self.tool = tool
        self.args = args
        self.decision = None  # None while pending, else "allow" or "deny"
        self.event = threading.Event()


class TurnContext:
    """The interface an Engine implementation sees for exactly one turn."""

    def __init__(self, session, turn_id, message):
        self.session = session
        self.turn_id = turn_id
        self.message = message
        self.workspace = session.workspace
        self.model = session.model
        self.mode = session.mode

    def emit(self, kind, data=None):
        self.session._emit(self.turn_id, kind, data or {})

    def request_approval(self, tool, args=None):
        return self.session._request_approval(self.turn_id, tool, args or {})

    def cancelled(self):
        return self.session._is_cancelled(self.turn_id)


class NullEngine:
    """Default engine when none is injected. Immediately reports an error so
    a Session is always constructible, but no production caller should reach
    this: app.py's SidecarState always injects a real engine (a fake in
    tests, hearth_loop-backed once that seam is wired)."""

    def run(self, ctx):
        ctx.emit("error", {"message": "no engine configured"})


class Session:
    """One live agent session: workspace + model + permission mode, plus the
    event stream and approval queue for whichever turn is currently running.

    Only one turn runs at a time. POST /session replaces the whole session
    (a fresh Session object, a fresh event log, a fresh approval table); it
    does not try to migrate state from the session it replaces.
    """

    def __init__(self, workspace, model, mode=DEFAULT_MODE, engine=None, events_cap=None,
                 approvals_cap=None):
        if not workspace:
            raise ValueError("workspace is required")
        if not model:
            raise ValueError("model is required")
        if mode not in MODES:
            raise ValueError("bad mode: {!r} (must be one of {})".format(mode, MODES))
        self.workspace = workspace
        self.model = model
        self.mode = mode
        self.engine = engine or NullEngine()
        self.status = STATUS_IDLE
        self.turn_id = None

        self._lock = threading.Lock()
        self._new_event = threading.Condition(self._lock)
        self._events_cap = events_cap or EVENTS_CAP
        self._events = collections.deque(maxlen=self._events_cap)  # dicts: id, turn_id, kind, data, ts
        self._dropped_count = 0  # events evicted from the ring so far
        self._event_id_seq = itertools.count(1)
        self._approvals_cap = approvals_cap or APPROVALS_CAP
        self._approvals = {}  # approval_id -> Approval; bounded, see _evict_approvals_locked
        self._cancel_flags = {}  # turn_id -> threading.Event; cleared per-turn, see submit_prompt
        self._thread = None
        self._live_workers = 0  # abandoned/in-flight _run_cancellable calls; see is_workspace_busy()

    def to_dict(self):
        with self._lock:
            return {
                "workspace": self.workspace,
                "model": self.model,
                "mode": self.mode,
                "status": self.status,
                "turn_id": self.turn_id,
            }

    # ---- driving a turn ----

    def submit_prompt(self, message):
        """Start a new turn on a background thread and return its id
        immediately. Raises RuntimeError if a turn is already running."""
        with self._lock:
            if self.status == STATUS_RUNNING:
                raise RuntimeError("a turn is already running")
            turn_id = uuid.uuid4().hex
            self.turn_id = turn_id
            self.status = STATUS_RUNNING
            # Only the current turn's flag is ever consulted (see the module
            # docstring, point 4): drop every previous turn's flag here
            # rather than letting _cancel_flags grow by one entry per turn
            # for the life of the session.
            self._cancel_flags.clear()
            self._cancel_flags[turn_id] = threading.Event()

        ctx = TurnContext(self, turn_id, message)

        def _run():
            try:
                self.engine.run(ctx)
            except Exception as exc:  # noqa: BLE001 - an engine bug must not orphan the session
                self._emit(turn_id, "error", {"message": "{}: {}".format(type(exc).__name__, exc)})
            finally:
                with self._lock:
                    if self.turn_id == turn_id:
                        self.status = STATUS_IDLE

        t = threading.Thread(target=_run, daemon=True)
        self._thread = t
        t.start()
        return turn_id

    def cancel(self):
        """Signal the running turn to stop. Returns True if there was a turn
        to cancel. Also wakes any approval currently blocking that turn's
        worker thread, so a pending gate does not hang forever; an approval
        woken this way is explicitly marked resolved with decision "deny"
        (not just its event fired) so it becomes eligible for eviction like
        any other resolved approval -- see _evict_approvals_locked."""
        with self._lock:
            if self.status != STATUS_RUNNING:
                return False  # nothing in flight, including a turn that already finished
            turn_id = self.turn_id
            flag = self._cancel_flags.get(turn_id) if turn_id else None
            if flag is None:
                return False
            flag.set()
            for appr in self._approvals.values():
                if appr.decision is None:
                    appr.decision = "deny"
                    appr.event.set()
            self._evict_approvals_locked()
        return True

    def _is_cancelled(self, turn_id):
        with self._lock:
            flag = self._cancel_flags.get(turn_id)
        return bool(flag and flag.is_set())

    # ---- abandoned-worker tracking (see module docstring, point 1) ----

    def _worker_started(self):
        """Called by engine._run_cancellable when it starts a blocking call
        on its own worker thread -- including one that later gets abandoned
        because cancellation wins the race. Paired with _worker_finished()."""
        with self._lock:
            self._live_workers += 1

    def _worker_finished(self):
        """Called when that worker thread's blocking call actually returns,
        whether or not the turn that started it was cancelled in the
        meantime. This is what lets is_workspace_busy() stay true after
        `status` has already gone back to idle."""
        with self._lock:
            self._live_workers = max(0, self._live_workers - 1)

    def is_workspace_busy(self):
        """True if a turn is actively running, OR a previous turn's
        cancelled-but-abandoned model/tool call is still executing against
        this session's workspace. `status == STATUS_RUNNING` alone is not
        enough: cancellation flips status back to idle within about a
        second while the abandoned call can keep writing to the workspace
        for as long as 1800s (run_command) behind it. Anything that is about
        to act on the workspace on disk -- POST /restore, replacing the
        session -- must check this, not `status`."""
        with self._lock:
            return self.status == STATUS_RUNNING or self._live_workers > 0

    # ---- events (GET /events) ----

    def _emit(self, turn_id, kind, data):
        with self._lock:
            event_id = next(self._event_id_seq)
            if len(self._events) >= self._events.maxlen:
                self._dropped_count += 1
            self._events.append({
                "id": event_id, "turn_id": turn_id, "kind": kind,
                "data": data, "ts": time.time(),
            })
            self._new_event.notify_all()
        return event_id

    def _gap_marker_locked(self, last_id):
        """If events have been evicted from the ring since `last_id`, and the
        caller's bookmark falls into that gap, build a synthetic
        "events_dropped" marker to return ahead of whatever is still
        retained. Returns None when there is nothing to signal: either
        nothing has ever been dropped, or the caller's bookmark is already
        within (or ahead of) the retained window. Must be called with
        self._lock held."""
        if self._dropped_count <= 0 or not self._events:
            return None
        oldest_id = self._events[0]["id"]
        if last_id >= oldest_id - 1:
            return None
        return {
            "id": oldest_id - 1, "turn_id": None, "kind": "events_dropped",
            "data": {"missed_at_least": self._dropped_count, "resume_from_id": oldest_id},
            "ts": time.time(),
        }

    def events_after(self, last_id, timeout=None):
        """Block until at least one event with id > last_id exists or
        `timeout` seconds elapse, then return the (possibly empty, on
        timeout) list of such events in order. If the caller's bookmark
        (`last_id`) has fallen behind the retained ring, the first item
        returned is a synthetic "events_dropped" marker rather than a
        silent gap -- see _gap_marker_locked."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while True:
                pending = [e for e in self._events if e["id"] > last_id]
                marker = self._gap_marker_locked(last_id)
                if marker is not None:
                    return [marker] + pending
                if pending:
                    return pending
                if deadline is not None and time.monotonic() >= deadline:
                    return []
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                self._new_event.wait(timeout=remaining)

    # ---- approvals (POST /approve) ----

    def _evict_approvals_locked(self):
        """Keep self._approvals bounded at self._approvals_cap entries.
        Approval.args can hold an entire file's contents (write_file's
        content argument), so an edit-mode session (the default permission
        mode, where every write and every dangerous call is gated) that
        performs many approvals over its lifetime would otherwise retain
        every one of those payloads, plus a threading.Event each, forever.

        Only RESOLVED approvals (decision is not None) are ever removed,
        oldest insertion-order first, stopping as soon as the dict is back
        at or under the cap. This can never drop an approval a turn is
        still blocked on: RealEngine.run() blocks on ctx.request_approval()
        (which blocks on appr.event.wait()) before issuing its next tool
        call, so at most one approval is ever pending (decision is None) at
        any moment for the whole session, and a pending entry is never a
        candidate for eviction here. Must be called with self._lock held.
        """
        if len(self._approvals) <= self._approvals_cap:
            return
        for appr_id in list(self._approvals.keys()):
            if len(self._approvals) <= self._approvals_cap:
                break
            if self._approvals[appr_id].decision is not None:
                del self._approvals[appr_id]

    def _request_approval(self, turn_id, tool, args):
        # secrets.token_urlsafe rather than a sequential counter: an approval
        # id must not be guessable from a previous one (defense in depth --
        # nothing today lets an unauthenticated party race an approval, but
        # a predictable id is a needless foothold if that ever changes).
        appr_id = "appr-{}".format(secrets.token_urlsafe(9))
        appr = Approval(appr_id, tool, args)
        with self._lock:
            self._approvals[appr_id] = appr
            self._evict_approvals_locked()
        self._emit(turn_id, "approval_request", {"id": appr_id, "tool": tool, "args": args})
        appr.event.wait()  # released by resolve_approval() or by cancel()
        with self._lock:
            decision = appr.decision
        return decision or "deny"  # cancelled with no explicit decision: deny

    def resolve_approval(self, approval_id, allow):
        """Resolve a pending approval. Returns False if the id is unknown or
        already resolved, so a stale or duplicate /approve call is a no-op
        rather than a silent double-resolution."""
        with self._lock:
            appr = self._approvals.get(approval_id)
            if appr is None or appr.decision is not None:
                return False
            appr.decision = "allow" if allow else "deny"
            self._evict_approvals_locked()
        appr.event.set()
        return True

    def pending_approvals(self):
        with self._lock:
            return [a.id for a in self._approvals.values() if a.decision is None]


def _self_test():
    # --- construction validates its inputs ---
    for bad in (dict(workspace="", model="m"), dict(workspace="w", model=""),
                dict(workspace="w", model="m", mode="yolo")):
        try:
            Session(**bad)
            assert False, "expected ValueError for {}".format(bad)
        except ValueError:
            pass

    s = Session("/tmp/ws", "qwen2.5-coder", "auto")
    d = s.to_dict()
    assert d == {"workspace": "/tmp/ws", "model": "qwen2.5-coder", "mode": "auto",
                 "status": "idle", "turn_id": None}, d

    # --- a NullEngine session immediately errors and returns to idle ---
    turn_id = s.submit_prompt("hello")
    events = s.events_after(0, timeout=5)
    assert events, "no events emitted"
    assert events[0]["kind"] == "error"
    assert events[0]["turn_id"] == turn_id
    for _ in range(100):
        if s.to_dict()["status"] == STATUS_IDLE:
            break
        time.sleep(0.01)
    assert s.to_dict()["status"] == STATUS_IDLE

    # --- a real engine: emits deltas, gates a tool call, resolves, completes ---
    class ScriptedEngine:
        def run(self, ctx):
            ctx.emit("delta", {"text": "thinking..."})
            decision = ctx.request_approval("write_file", {"path": "x.txt"})
            ctx.emit("tool_call", {"tool": "write_file", "decision": decision})
            ctx.emit("done", {})

    s2 = Session("/tmp/ws2", "m", "edit", engine=ScriptedEngine())
    turn_id2 = s2.submit_prompt("write a file")
    # Wait for the approval_request event.
    seen = []
    deadline = time.monotonic() + 5
    approval_id = None
    while time.monotonic() < deadline:
        seen = s2.events_after(seen[-1]["id"] if seen else 0, timeout=1) or seen
        kinds = [e["kind"] for e in seen]
        if "approval_request" in kinds:
            approval_id = next(e["data"]["id"] for e in seen if e["kind"] == "approval_request")
            break
    assert approval_id, "approval_request never arrived"
    assert s2.pending_approvals() == [approval_id]
    assert s2.resolve_approval("bogus-id", True) is False
    assert s2.resolve_approval(approval_id, True) is True
    assert s2.resolve_approval(approval_id, True) is False  # already resolved
    assert s2.pending_approvals() == []

    deadline = time.monotonic() + 5
    while s2.to_dict()["status"] != STATUS_IDLE and time.monotonic() < deadline:
        time.sleep(0.01)
    all_events = s2.events_after(0, timeout=1)
    kinds = [e["kind"] for e in all_events]
    assert kinds == ["delta", "approval_request", "tool_call", "done"], kinds
    tool_call_event = next(e for e in all_events if e["kind"] == "tool_call")
    assert tool_call_event["data"]["decision"] == "allow"

    # --- cancellation wakes a blocked approval and reports "deny" ---
    class WaitingEngine:
        def run(self, ctx):
            decision = ctx.request_approval("run_command", {"command": "rm -rf /"})
            ctx.emit("tool_call", {"tool": "run_command", "decision": decision})

    s3 = Session("/tmp/ws3", "m", "auto", engine=WaitingEngine())
    s3.submit_prompt("do something dangerous")
    deadline = time.monotonic() + 5
    while not s3.pending_approvals() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert s3.pending_approvals(), "approval never arrived"
    assert s3.cancel() is True
    deadline = time.monotonic() + 5
    while s3.to_dict()["status"] != STATUS_IDLE and time.monotonic() < deadline:
        time.sleep(0.01)
    events3 = s3.events_after(0, timeout=1)
    tool_call3 = next(e for e in events3 if e["kind"] == "tool_call")
    assert tool_call3["data"]["decision"] == "deny", "a cancelled approval must deny, not hang or allow"
    assert s3.cancel() is False, "cancelling an idle session reports nothing to cancel"
    # A cancelled approval must be explicitly resolved (decision set to
    # "deny"), not merely have its event fired with decision left None --
    # otherwise it can never become eligible for eviction (see Finding 2 /
    # _evict_approvals_locked, which only ever removes a resolved entry).
    with s3._lock:
        cancelled_apprs = list(s3._approvals.values())
    assert cancelled_apprs, "expected the cancelled approval to still be present"
    assert all(a.decision == "deny" for a in cancelled_apprs), \
        "cancel() must set decision, not just fire the event: {}".format(
            [(a.id, a.decision) for a in cancelled_apprs])

    # --- only one turn at a time ---
    class SlowEngine:
        def run(self, ctx):
            while not ctx.cancelled():
                time.sleep(0.02)

    s4 = Session("/tmp/ws4", "m", "bypass", engine=SlowEngine())
    s4.submit_prompt("first")
    try:
        s4.submit_prompt("second")
        assert False, "expected RuntimeError: a turn is already running"
    except RuntimeError:
        pass
    s4.cancel()
    deadline = time.monotonic() + 5
    while s4.to_dict()["status"] != STATUS_IDLE and time.monotonic() < deadline:
        time.sleep(0.01)

    # --- approval ids are unguessable, not a sequential counter ---
    class TwoApprovalsEngine:
        def run(self, ctx):
            ids = []
            ids.append(ctx.request_approval("write_file", {"path": "a"}))
            ids.append(ctx.request_approval("write_file", {"path": "b"}))
            ctx.emit("done", {})

    captured_ids = []
    s_appr = Session("/tmp/ws-appr", "m", "edit", engine=TwoApprovalsEngine())
    s_appr.submit_prompt("go")
    deadline = time.monotonic() + 5
    while len(captured_ids) < 2 and time.monotonic() < deadline:
        pending = s_appr.pending_approvals()
        for pid in pending:
            if pid not in captured_ids:
                captured_ids.append(pid)
                s_appr.resolve_approval(pid, True)
        time.sleep(0.01)
    assert len(captured_ids) == 2, "expected two approval ids, got {}".format(captured_ids)
    for appr_id in captured_ids:
        assert appr_id.startswith("appr-"), appr_id
        suffix = appr_id[len("appr-"):]
        # a plain incrementing counter would make this suffix pure digits
        # ("1", "2", ...); secrets.token_urlsafe never produces that.
        assert not suffix.isdigit(), "approval id looks sequential, not random: {}".format(appr_id)
        assert len(suffix) >= 8, "approval id suffix too short to be unguessable: {}".format(appr_id)
    assert captured_ids[0] != captured_ids[1]
    s_appr.cancel()

    # --- is_workspace_busy(): tracks abandoned workers independently of ---
    # --- `status`, which is exactly what POST /restore must consult -----
    worker_release = threading.Event()
    worker_entered = threading.Event()

    class AbandonedWorkerEngine:
        """Simulates engine.py's _run_cancellable: starts a blocking call on
        its own thread, registers it with the session, and returns to the
        caller (as if cancellation won the race) while that thread is still
        live -- without waiting for it."""

        def run(self, ctx):
            def _blocking():
                ctx.session._worker_started()
                try:
                    worker_entered.set()
                    worker_release.wait(timeout=10)
                finally:
                    ctx.session._worker_finished()

            t = threading.Thread(target=_blocking, daemon=True)
            t.start()
            # return immediately, exactly like a cancelled _run_cancellable call

    s_busy = Session("/tmp/ws-busy", "m", "edit", engine=AbandonedWorkerEngine())
    assert s_busy.is_workspace_busy() is False, "a fresh session must not report busy"
    s_busy.submit_prompt("go")
    assert worker_entered.wait(timeout=5), "abandoned worker never started"
    deadline = time.monotonic() + 5
    while s_busy.to_dict()["status"] != STATUS_IDLE and time.monotonic() < deadline:
        time.sleep(0.01)
    assert s_busy.to_dict()["status"] == STATUS_IDLE, "engine.run() returned, so status must be idle"
    # THE PIN: status is idle, but the abandoned worker is still running --
    # is_workspace_busy() must still say True.
    assert s_busy.is_workspace_busy() is True, \
        "is_workspace_busy() must stay true while an abandoned worker is still live, even once status is idle"
    worker_release.set()
    deadline = time.monotonic() + 5
    while s_busy.is_workspace_busy() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert s_busy.is_workspace_busy() is False, "is_workspace_busy() never cleared after the worker finished"

    # --- bounded event ring: capped size, and a stale reconnect gets an ---
    # --- explicit "events_dropped" marker instead of a silent gap --------
    class QuietEngine:
        def run(self, ctx):
            for i in range(6):
                ctx.emit("delta", {"text": "chunk-{}".format(i)})
            ctx.emit("done", {})

    s5 = Session("/tmp/ws5", "m", "edit", engine=QuietEngine(), events_cap=3)
    s5.submit_prompt("go")
    deadline = time.monotonic() + 5
    while s5.to_dict()["status"] != STATUS_IDLE and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(s5._events) <= 3, "event ring must stay bounded by events_cap"
    assert len(s5._events) == 3, s5._events  # exactly 7 events emitted, cap 3
    # a stale bookmark (id 1, long since evicted) must surface an explicit
    # gap marker, not a silently-truncated history
    stale = s5.events_after(1, timeout=1)
    assert stale and stale[0]["kind"] == "events_dropped", stale
    assert stale[0]["data"]["missed_at_least"] >= 1, stale
    # once the caller resumes from the marker's own id, no repeat marker,
    # and it sees exactly what is still retained, in order
    resumed = s5.events_after(stale[0]["id"], timeout=1)
    assert all(e["kind"] != "events_dropped" for e in resumed), resumed
    assert [e["id"] for e in resumed] == [e["id"] for e in s5._events], resumed
    # a bookmark that is already caught up sees no gap marker at all
    fresh = s5.events_after(s5._events[-1]["id"], timeout=1)
    assert fresh == [], fresh

    # === Finding 2: _approvals is bounded, across many turns of one =======
    # === session, without ever evicting a still-pending approval ==========
    class OneApprovalEngine:
        def run(self, ctx):
            decision = ctx.request_approval("write_file", {"path": "p.txt", "content": "y" * 50})
            ctx.emit("tool_call", {"tool": "write_file", "decision": decision})
            ctx.emit("done", {})

    approvals_cap = 3
    s_cap = Session("/tmp/ws-approvals-cap", "m", "edit", engine=OneApprovalEngine(),
                    approvals_cap=approvals_cap)
    n_turns = 6  # more turns than the cap, to actually exercise eviction
    for i in range(n_turns):
        s_cap.submit_prompt("turn {}".format(i))
        deadline = time.monotonic() + 5
        approval_id = None
        while approval_id is None and time.monotonic() < deadline:
            pending = s_cap.pending_approvals()
            if pending:
                approval_id = pending[0]
            time.sleep(0.005)
        assert approval_id, "approval never arrived on turn {}".format(i)
        assert s_cap.resolve_approval(approval_id, True) is True
        deadline = time.monotonic() + 5
        while s_cap.to_dict()["status"] != STATUS_IDLE and time.monotonic() < deadline:
            time.sleep(0.01)
    assert len(s_cap._approvals) <= approvals_cap, \
        "approvals dict must stay bounded by approvals_cap across many turns: {} entries, cap {}".format(
            len(s_cap._approvals), approvals_cap)

    # THE PIN: eviction must never remove a still-pending approval, even
    # when resolved entries around it push the dict well past the cap.
    # Exercised directly against _evict_approvals_locked (the mechanism
    # itself), with a manufactured mix of one pending and several resolved
    # entries, rather than only indirectly through timing-sensitive turns.
    s_evict = Session("/tmp/ws-evict-pin", "m", "edit", approvals_cap=3)
    pending_appr = Approval("appr-pending", "run_command", {"command": "does not matter"})
    with s_evict._lock:
        s_evict._approvals["appr-pending"] = pending_appr
        for i in range(6):
            resolved = Approval("appr-resolved-{}".format(i), "write_file", {"path": "f"})
            resolved.decision = "allow"
            s_evict._approvals[resolved.id] = resolved
        s_evict._evict_approvals_locked()
        surviving = dict(s_evict._approvals)
    assert "appr-pending" in surviving, \
        "eviction must never drop a still-pending approval: {}".format(list(surviving))
    assert len(surviving) == 3, \
        "eviction must trim resolved entries down to the cap: {}".format(list(surviving))
    # oldest-resolved-first: the two most recently inserted resolved entries
    # survive alongside the pending one.
    assert set(surviving) == {"appr-pending", "appr-resolved-4", "appr-resolved-5"}, surviving

    # === Finding 2: _cancel_flags never accumulates past the current ======
    # === turn's single flag, across many turns of one session =============
    class QuickEngine:
        def run(self, ctx):
            ctx.emit("done", {})

    s_flags = Session("/tmp/ws-flags", "m", "edit", engine=QuickEngine())
    assert s_flags._cancel_flags == {}, "no cancel flag should exist before any turn has run"
    for i in range(5):
        s_flags.submit_prompt("turn {}".format(i))
        deadline = time.monotonic() + 5
        while s_flags.to_dict()["status"] != STATUS_IDLE and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(s_flags._cancel_flags) == 1, \
            "cancel flags must never accumulate across turns of one session: {}".format(
                s_flags._cancel_flags)

    print("hearth-desktop-session self-test OK")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
