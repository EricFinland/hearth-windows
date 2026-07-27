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

Standard library only.
"""

import itertools
import sys
import threading
import time
import uuid

MODES = ("plan", "edit", "auto", "bypass")
DEFAULT_MODE = "edit"

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"


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

    def __init__(self, workspace, model, mode=DEFAULT_MODE, engine=None):
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
        self._events = []  # list of dicts: id, turn_id, kind, data, ts
        self._event_id_seq = itertools.count(1)
        self._approvals = {}  # approval_id -> Approval
        self._approval_id_seq = itertools.count(1)
        self._cancel_flags = {}  # turn_id -> threading.Event
        self._thread = None

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
        woken this way resolves as "deny" (see _request_approval)."""
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
                    appr.event.set()
        return True

    def _is_cancelled(self, turn_id):
        with self._lock:
            flag = self._cancel_flags.get(turn_id)
        return bool(flag and flag.is_set())

    # ---- events (GET /events) ----

    def _emit(self, turn_id, kind, data):
        with self._lock:
            event_id = next(self._event_id_seq)
            self._events.append({
                "id": event_id, "turn_id": turn_id, "kind": kind,
                "data": data, "ts": time.time(),
            })
            self._new_event.notify_all()
        return event_id

    def events_after(self, last_id, timeout=None):
        """Block until at least one event with id > last_id exists or
        `timeout` seconds elapse, then return the (possibly empty, on
        timeout) list of such events in order."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while True:
                pending = [e for e in self._events if e["id"] > last_id]
                if pending:
                    return pending
                if deadline is not None and time.monotonic() >= deadline:
                    return []
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                self._new_event.wait(timeout=remaining)

    # ---- approvals (POST /approve) ----

    def _request_approval(self, turn_id, tool, args):
        appr_id = "appr-{}".format(next(self._approval_id_seq))
        appr = Approval(appr_id, tool, args)
        with self._lock:
            self._approvals[appr_id] = appr
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

    print("hearth-desktop-session self-test OK")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
