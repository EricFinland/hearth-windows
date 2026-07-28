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
  ctx.request_approval(tool, args, injection_finding=None) -> "allow"|"deny"
                                                     -- gate a tool call,
                                                        blocking until
                                                        POST /approve
                                                        resolves it (or the
                                                        turn is cancelled).
                                                        injection_finding, if
                                                        given, is a small
                                                        JSON-safe dict (see
                                                        engine.py's
                                                        _injection_finding_for_approval)
                                                        carried on the
                                                        approval_request
                                                        event's data, unread
                                                        and unvalidated by
                                                        this module -- it is
                                                        just another value in
                                                        the event dict, so
                                                        the same "json.dumps
                                                        into one string"
                                                        framing guarantee
                                                        app.py already gives
                                                        every event applies
                                                        to it too.
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

Restart survival (session_state.py, a sibling module, owns the actual disk
format and the atomic write): a Session never touches disk itself. Instead
it accepts an optional `persist_hook(session)` callable and invokes it --
always best-effort, a failure here must never break a live turn or an HTTP
request, see _persist() -- at the moments worth making durable: when a turn
starts (submit_prompt, before any blocking work), when an approval gate is
raised (_request_approval, right after the approval_request event -- this
is deliberately the exact moment a crash would otherwise strand an
unanswerable, half-live approval), and when a turn ends (_run's finally,
after status has flipped back to idle). app.py wires the actual hook and is
responsible for making sure only the *current* session (not one a POST
/session replaced, whose abandoned worker -- see is_workspace_busy above --
might still be finishing up on its own thread) ever wins a write; this
module only calls the hook it was given, unconditionally, since it has no
way to know about sibling sessions itself.

What is NOT persisted, and why, lives in session_state.py's module
docstring; the short version is: the conversation and workspace/model/mode,
yes; a bounded tail of recent events, yes; checkpoint ids, no (the shadow
store is authoritative); a pending approval, never as something a restarted
process could still resolve -- see record_restart_interruption() and
seed_events(), used only by session_state.restore_session() to rebuild a
Session from a prior process's last snapshot.

Standard library only.
"""

import collections
import itertools
import os
import secrets
import sys
import threading
import time
import uuid

# permissions.py (agent/) is the authoritative source of the permission-mode
# set. This module used to carry its own hardcoded duplicate of that tuple,
# which meant a mode added to permissions.MODES (as "edit" once was) had to
# be remembered here too, by hand, or session construction would reject a
# mode permissions.decide already understands. sys.path is extended the same
# way engine.py does (desktop/server -> desktop -> repo root -> agent/),
# since this module is a sibling of engine.py and permissions.py is a small,
# pure, I/O-free module -- importing just its MODES constant does not pull in
# hearth_loop/hearth_tools or otherwise compromise the "no engine import"
# seam described below.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))  # desktop/server -> desktop -> repo root
_AGENT_DIR = os.path.join(_REPO_ROOT, "agent")
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

import permissions  # noqa: E402

MODES = permissions.MODES
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

    def request_approval(self, tool, args=None, injection_finding=None):
        return self.session._request_approval(self.turn_id, tool, args or {}, injection_finding)

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
                 approvals_cap=None, persist_hook=None):
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
        # Restart survival: see the module docstring. None (the default,
        # used by every existing caller and every test that does not care
        # about persistence) means "never write anything" -- _persist()
        # below is a no-op in that case.
        self._persist_hook = persist_hook

    def to_dict(self):
        with self._lock:
            return {
                "workspace": self.workspace,
                "model": self.model,
                "mode": self.mode,
                "status": self.status,
                "turn_id": self.turn_id,
            }

    # ---- restart survival (session_state.py) ----

    def set_persist_hook(self, hook):
        """Attach (or replace) the persist hook after construction. Used
        only by app.py when adopting a session that session_state.py just
        rebuilt from a prior process's disk snapshot (main.py's startup
        path): that Session is built before the hook can be safely wired
        (the hook needs to know it is "the current session", which is a
        fact only app.py's SidecarState can answer -- see the module
        docstring), so it starts with none and gets one attached right
        after app.py adopts it."""
        with self._lock:
            self._persist_hook = hook

    def _persist(self):
        """Best-effort: hand this session to the injected persist hook, if
        any. Must never raise -- a disk-full, permissions, or Windows
        sharing-violation failure here must never take down a live turn or
        an HTTP request; the worst case of a failed persist is that the
        next restart recovers a little less history than it otherwise
        would have, not a broken session right now."""
        hook = self._persist_hook
        if hook is None:
            return
        try:
            hook(self)
        except Exception:  # noqa: BLE001 - persistence must never break a live session
            pass

    def recent_events(self, n=50):
        """The most recent `n` events, oldest first -- a bounded tail for
        session_state.py to persist, so a UI reconnecting after a restart
        sees recent history without this module (or the state file) ever
        having to hold the unbounded log. Already the same JSON-safe dicts
        _emit builds and app.py's _write_sse serialises, so nothing here
        needs its own sanitisation pass."""
        with self._lock:
            return list(self._events)[-n:]

    def seed_events(self, events):
        """Pre-populate the event ring from a persisted tail. For
        session_state.restore_session()'s use only, and only immediately
        after construction, before this session has emitted anything of
        its own: continues the event id sequence past the highest
        restored id, so the first newly emitted event (typically
        record_restart_interruption's own marker) can never collide with,
        or sort before, a restored one."""
        if not events:
            return
        with self._lock:
            max_id = 0
            for ev in events[-self._events_cap:]:
                self._events.append(ev)
                max_id = max(max_id, ev.get("id") or 0)
            self._event_id_seq = itertools.count(max_id + 1)

    def pending_approval_tools(self):
        """Tool names of every currently-pending (unresolved) approval, for
        session_state.py's snapshot to note as "this may be abandoned" --
        deliberately just the tool name, never the full args (which, for
        write_file in edit mode, can be an entire file body): the point of
        recording this is an honest note in the history, not a second copy
        of a payload the approval itself already isn't allowed to
        resurrect. See record_restart_interruption for the other half."""
        with self._lock:
            return [a.tool for a in self._approvals.values() if a.decision is None]

    def record_restart_interruption(self, turn_id, pending_tool=None):
        """Called at most once, by session_state.restore_session(), right
        after rebuilding a Session whose last known status (per the
        persisted snapshot) was STATUS_RUNNING: the process that owned
        that turn is gone. There is no thread left to resume it, and no
        way to know whether its last tool call actually finished against
        the workspace -- so this never resumes anything. It only appends
        to the event log, honestly, that the turn was interrupted (and,
        if one was pending, that an approval was abandoned and can never
        be resolved -- resurrecting it would let a user "approve" an
        action with no caller left to carry it out, or one that would now
        run against a workspace that has since changed underneath it).

        self.status/self.turn_id are forced to idle/None (they should
        already be that way for a freshly constructed Session, but this
        makes it explicit and immune to future construction changes) so a
        fresh POST /prompt is never blocked by a turn nothing is running
        anymore.
        """
        with self._lock:
            self.status = STATUS_IDLE
            self.turn_id = None
        if pending_tool:
            self._emit(turn_id, "approval_abandoned", {
                "tool": pending_tool,
                "reason": "the sidecar restarted while this approval was still "
                          "pending; it can no longer be resolved",
            })
        self._emit(turn_id, "turn_interrupted", {
            "reason": "the sidecar restarted while this turn was still in "
                      "progress; its last tool call may or may not have "
                      "completed. Review the workspace before continuing.",
        })

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
                # Persist once the turn is over (normal completion, denial,
                # cancellation, or an engine bug -- every path above reaches
                # this finally): the conversation the engine just extended
                # is worth being durable now, not just when the next turn
                # happens to start. See the module docstring.
                self._persist()

        t = threading.Thread(target=_run, daemon=True)
        self._thread = t
        t.start()
        # Persist the "a turn just started" moment too, before any blocking
        # model or tool call -- if the process dies mid-turn, the state
        # file's status_at_save is what tells session_state.restore_session
        # to mark this turn interrupted rather than silently forgotten.
        self._persist()
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

    def _request_approval(self, turn_id, tool, args, injection_finding=None):
        # secrets.token_urlsafe rather than a sequential counter: an approval
        # id must not be guessable from a previous one (defense in depth --
        # nothing today lets an unauthenticated party race an approval, but
        # a predictable id is a needless foothold if that ever changes).
        appr_id = "appr-{}".format(secrets.token_urlsafe(9))
        appr = Approval(appr_id, tool, args)
        with self._lock:
            self._approvals[appr_id] = appr
            self._evict_approvals_locked()
        event_data = {"id": appr_id, "tool": tool, "args": args}
        # injection_finding is opaque to this module: whatever the engine
        # passed (a small dict from a hearth_injection.scan() result, or
        # None) rides straight into the event dict. It is only ever added
        # when the caller has something to say, never an explicit None/False
        # placeholder, so an ordinary approval's event shape is unchanged
        # from before this field existed.
        if injection_finding is not None:
            event_data["injection_finding"] = injection_finding
        self._emit(turn_id, "approval_request", event_data)
        # Persist right here, before blocking on the wait below: this is
        # deliberately the exact moment a crash would otherwise strand an
        # approval a restarted process has no thread left to resolve. See
        # the module docstring and record_restart_interruption.
        self._persist()
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
    # MODES must be sourced from permissions.MODES, not a local duplicate:
    # a mode permissions.py knows about (all four, today) must be exactly
    # the set Session accepts, so the two can never drift apart again.
    assert MODES == permissions.MODES, (MODES, permissions.MODES)
    assert MODES is permissions.MODES, "MODES must be the same object, not a copy that can drift"
    assert "edit" in MODES, MODES

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

    # --- injection_finding, when the engine passes one, rides on the ---
    # --- approval_request event verbatim; when omitted (the ordinary ---
    # --- case, exercised just above by ScriptedEngine), the key is not -
    # --- present at all rather than present-and-None. This is session.py's
    # --- own slice of the wiring; engine.py's self-test (section L) proves
    # --- the other half, that a real hearth_injection.scan() finding
    # --- actually reaches this parameter in the first place. ---
    sample_finding = {"source": "read_file", "severity": "high", "score": 42,
                       "category": "imperative_override", "matched": "ignore all previous instructions",
                       "explanation": "tells the assistant to discard prior instructions"}

    class FindingEngine:
        def run(self, ctx):
            decision = ctx.request_approval("write_file", {"path": "y.txt"},
                                             injection_finding=sample_finding)
            ctx.emit("tool_call", {"tool": "write_file", "decision": decision})

    s_finding = Session("/tmp/ws-finding", "m", "edit", engine=FindingEngine())
    s_finding.submit_prompt("write something suspicious")
    deadline = time.monotonic() + 5
    appr_data = None
    while appr_data is None and time.monotonic() < deadline:
        seen = s_finding.events_after(0, timeout=1)
        for e in seen:
            if e["kind"] == "approval_request":
                appr_data = e["data"]
                break
    assert appr_data is not None, "approval_request never arrived"
    assert appr_data.get("injection_finding") == sample_finding, appr_data
    s_finding.resolve_approval(appr_data["id"], True)
    deadline = time.monotonic() + 5
    while s_finding.to_dict()["status"] != STATUS_IDLE and time.monotonic() < deadline:
        time.sleep(0.01)

    # the ordinary path (no injection_finding passed at all) never gets the
    # key -- checked again here directly, not only inferred from the earlier
    # ScriptedEngine assertions, so this fixture is self-contained.
    no_finding_events = s2.events_after(0, timeout=1)
    appr_no_finding = next(e for e in no_finding_events if e["kind"] == "approval_request")
    assert "injection_finding" not in appr_no_finding["data"], appr_no_finding

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

    # === restart survival: persist_hook is invoked at the moments the ======
    # === module docstring promises -- turn start, an approval gate, and ===
    # === turn end -- and never otherwise, and never with anything but ====
    # === `self` ============================================================
    persist_calls = []

    def recording_hook(sess):
        persist_calls.append(sess.to_dict())

    class HookEngine:
        def run(self, ctx):
            ctx.emit("delta", {"text": "..."})
            decision = ctx.request_approval("write_file", {"path": "h.txt"})
            ctx.emit("tool_call", {"tool": "write_file", "decision": decision})
            ctx.emit("done", {})

    s_hook = Session("/tmp/ws-hook", "m", "edit", engine=HookEngine(), persist_hook=recording_hook)
    assert persist_calls == [], "persist_hook must not fire at construction time"
    s_hook.submit_prompt("go")
    # call #1: right after submit_prompt starts the turn, status "running"
    deadline = time.monotonic() + 5
    while len(persist_calls) < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(persist_calls) >= 1, "persist_hook never fired at turn start"
    assert persist_calls[0]["status"] == STATUS_RUNNING, persist_calls[0]

    appr_h = None
    deadline = time.monotonic() + 5
    while appr_h is None and time.monotonic() < deadline:
        pending = s_hook.pending_approvals()
        if pending:
            appr_h = pending[0]
        time.sleep(0.005)
    assert appr_h, "approval never arrived"
    # call #2 (or later, if the turn-start call above raced ahead): fired at
    # the approval gate, before it was resolved -- this is the exact crash
    # window record_restart_interruption exists for.
    deadline = time.monotonic() + 5
    while len(persist_calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(persist_calls) >= 2, "persist_hook never fired at the approval gate"
    assert s_hook.resolve_approval(appr_h, True) is True
    deadline = time.monotonic() + 5
    while s_hook.to_dict()["status"] != STATUS_IDLE and time.monotonic() < deadline:
        time.sleep(0.01)
    # call #3: fired again once the turn actually finished.
    deadline = time.monotonic() + 5
    while len(persist_calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(persist_calls) >= 3, "persist_hook never fired at turn end"
    assert persist_calls[-1]["status"] == STATUS_IDLE, persist_calls[-1]

    # A persist_hook that raises must never take the session down with it.
    def broken_hook(sess):
        raise RuntimeError("disk is on fire")

    s_broken = Session("/tmp/ws-broken-hook", "m", "edit", engine=HookEngine(), persist_hook=broken_hook)
    s_broken.submit_prompt("go")
    deadline = time.monotonic() + 5
    while not s_broken.pending_approvals() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert s_broken.pending_approvals(), "a broken persist_hook must not prevent the turn from proceeding"
    s_broken.resolve_approval(s_broken.pending_approvals()[0], True)
    deadline = time.monotonic() + 5
    while s_broken.to_dict()["status"] != STATUS_IDLE and time.monotonic() < deadline:
        time.sleep(0.01)
    assert s_broken.to_dict()["status"] == STATUS_IDLE, \
        "a broken persist_hook must not prevent the turn from ever completing"

    # set_persist_hook attaches one after construction (the main.py restore
    # path: the hook needs to know it is "the current session", a fact only
    # available after app.py has adopted the rebuilt Session -- see the
    # module docstring).
    late_calls = []
    s_late = Session("/tmp/ws-late-hook", "m", "edit", engine=HookEngine())
    s_late.set_persist_hook(lambda sess: late_calls.append(sess.to_dict()))
    s_late.submit_prompt("go")
    deadline = time.monotonic() + 5
    while not late_calls and time.monotonic() < deadline:
        time.sleep(0.005)
    assert late_calls, "set_persist_hook did not actually attach a working hook"
    s_late.cancel()
    deadline = time.monotonic() + 5
    while s_late.to_dict()["status"] != STATUS_IDLE and time.monotonic() < deadline:
        time.sleep(0.01)

    # === recent_events / seed_events / pending_approval_tools / ===========
    # === record_restart_interruption: the building blocks session_state.py
    # === composes to persist a snapshot and rebuild a Session from one =====
    class ManyEventsEngine:
        def run(self, ctx):
            for i in range(5):
                ctx.emit("delta", {"text": "chunk-{}".format(i)})
            ctx.emit("done", {})

    s_re = Session("/tmp/ws-recent-events", "m", "edit", engine=ManyEventsEngine())
    s_re.submit_prompt("go")
    deadline = time.monotonic() + 5
    while s_re.to_dict()["status"] != STATUS_IDLE and time.monotonic() < deadline:
        time.sleep(0.01)
    all_re = s_re.events_after(0, timeout=1)
    tail = s_re.recent_events(3)
    assert len(tail) == 3, tail
    assert [e["id"] for e in tail] == [e["id"] for e in all_re[-3:]], (tail, all_re)
    assert s_re.recent_events(1000) == all_re, "recent_events(n >= total) must return everything, oldest first"

    # seed_events rebuilds the ring on a freshly constructed session and
    # continues the id sequence past the highest seeded id -- a newly
    # emitted event must never collide with, or sort before, a seeded one.
    s_seed = Session("/tmp/ws-seed", "m", "edit")
    assert list(s_seed._events) == [], "a fresh session must start with an empty ring"
    s_seed.seed_events(tail)
    assert [e["id"] for e in s_seed.recent_events(10)] == [e["id"] for e in tail]
    new_id = s_seed._emit(None, "marker", {})
    assert new_id > max(e["id"] for e in tail), \
        "an event emitted after seed_events must sort after every seeded event"

    # pending_approval_tools: the tool name of a still-pending approval,
    # never its args -- and empty once nothing is pending.
    class PendingToolEngine:
        def run(self, ctx):
            ctx.request_approval("run_command", {"command": "rm -rf /", "secret": "shh"})

    s_pt = Session("/tmp/ws-pending-tool", "m", "auto", engine=PendingToolEngine())
    assert s_pt.pending_approval_tools() == []
    s_pt.submit_prompt("go")
    deadline = time.monotonic() + 5
    while not s_pt.pending_approvals() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert s_pt.pending_approval_tools() == ["run_command"], s_pt.pending_approval_tools()
    s_pt.cancel()
    deadline = time.monotonic() + 5
    while s_pt.to_dict()["status"] != STATUS_IDLE and time.monotonic() < deadline:
        time.sleep(0.01)

    # record_restart_interruption: forces idle/None, and appends an honest
    # marker (plus an abandoned-approval marker when one was pending) --
    # never anything that looks like a resolvable approval.
    s_ri = Session("/tmp/ws-restart", "m", "edit")
    s_ri.status = "running"  # simulate what a freshly-constructed-then-reconstructed session looked like
    s_ri.turn_id = "some-turn-id"
    s_ri.record_restart_interruption("some-turn-id", pending_tool="write_file")
    d_ri = s_ri.to_dict()
    assert d_ri["status"] == STATUS_IDLE and d_ri["turn_id"] is None, d_ri
    ri_events = s_ri.events_after(0, timeout=1)
    ri_kinds = [e["kind"] for e in ri_events]
    assert ri_kinds == ["approval_abandoned", "turn_interrupted"], ri_kinds
    assert ri_events[0]["data"]["tool"] == "write_file", ri_events[0]
    assert s_ri.pending_approvals() == [], \
        "record_restart_interruption must never leave anything resolvable"

    s_ri2 = Session("/tmp/ws-restart-2", "m", "edit")
    s_ri2.record_restart_interruption("turn-x", pending_tool=None)
    ri2_kinds = [e["kind"] for e in s_ri2.events_after(0, timeout=1)]
    assert ri2_kinds == ["turn_interrupted"], \
        "no approval_abandoned marker when nothing was actually pending: {}".format(ri2_kinds)

    print("hearth-desktop-session self-test OK")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
