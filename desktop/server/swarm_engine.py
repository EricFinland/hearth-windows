#!/usr/bin/env python3
"""hearth desktop sidecar: drive an agent swarm through the Session seam, and
expose its RUN STATE to a UI.

WHY THIS MIRRORS loop_engine.py SO CLOSELY
==========================================

Because the shapes it needs are the ones that module already argued for, and
a second, different answer to the same question would be a second thing to
keep right. The reasoning is in loop_engine.py's docstring and is not repeated
here; what follows is only what a SWARM changes.

Inherited wholesale, by construction rather than by copying:

  the kill switch   POST /cancel sets Session's per-turn flag, ctx.cancelled
                    becomes the CancelToken's external source, and that one
                    token is threaded through every phase of the relay. There
                    is no second stop button and no per-role one: stopping
                    means the active role stops mid-generation and no later
                    role starts.
  the two shapes    the transcript is a LOG on the session event ring; the
                    run state is a GAUGE on a versioned whole-snapshot stream
                    (SwarmStatus). Same split, same reasons.
  the refusals      bypass is unreachable here, in hearth_swarmloop, in
                    session_state.RESTORABLE_MODES and in app.py's POST
                    /session. inspect_journal refuses a journal that claims
                    otherwise on the way back in.
  ceilings          parse_swarm_config validates every bound and there is no
                    spelling of "unlimited".

WHAT IS GENUINELY DIFFERENT
===========================

  ONE BUDGET, AND THE UI HAS TO SAY SO. The single most misleading thing a
  swarm UI could do is draw one meter per role. The ceilings here are shared
  by every role (hearth_swarmloop._residual), so the gauge carries ONE set of
  meters for the whole relay, and the per-phase breakdown is a separate list
  that reports spend rather than budget. A user who reads "3 roles" and
  assumes 3x the tokens has been misled by the screen, not by the engine.

  PHASES ARE A FIRST-CLASS PART OF THE STATE. The brief required that the
  swarm be able to explain itself: which role did what, where the handoffs
  were, and why it stopped. That is not derivable from the transcript alone
  once the event ring has dropped its oldest entries, so the gauge carries
  the phase list explicitly and it is bounded (MAX_PHASES_IN_SNAPSHOT) so a
  long relay cannot grow the snapshot without limit.

  A REVIEWER'S APPROVAL IS NOT A GREEN TICK. `reviewer_approved` and
  `verified` are separate fields all the way to the screen, and they must
  stay separate: the first is one model's opinion of another model's work and
  the second is a command that exited 0. A UI that merged them would be
  telling the user their code passes when nothing ran it.

  ROLES ARE NOT CONFIGURABLE OVER THIS TRANSPORT. parse_swarm_config accepts
  ceilings, gate policy, tools and the completion check, and it does NOT
  accept a role list. A caller that could define roles could define a second
  writing role, and the single-writer property that keeps two agents from
  corrupting one workspace would become a thing an HTTP body could switch
  off. What a caller MAY do is narrow the tool manifest, which can only ever
  make every role less capable. See parse_swarm_config.

Standard library only.
"""

import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_AGENT_DIR = os.path.join(_REPO_ROOT, "agent")
for _p in (_AGENT_DIR, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import hearth_contain  # noqa: E402
import hearth_swarmloop  # noqa: E402
import hearth_workloop  # noqa: E402
import permissions  # noqa: E402
# ConfigError and _bounded_int are imported rather than redefined: app.py
# catches exactly one config exception type, and a swarm raising a different
# one would fall through to a 500 instead of the 400 with a readable message
# that a bad ceiling deserves.
from loop_engine import (  # noqa: E402
    ConfigError, GATE_TIMEOUT_DEFAULT, GATE_TIMEOUT_RANGE, MAX_GOAL_CHARS,
    MAX_DONE_COMMAND_CHARS, MAX_REQUIRED_ARTIFACTS, STALL_LIMITS, _bounded_int,
)

#: The modes a swarm may run in. Derived from the swarm's own allowed set
#: minus "edit" for the reason loop_engine gives: nobody is awake to approve
#: the writes "edit" gates, so the implementer would deny its own first write
#: and the relay would spend its whole budget handing off about it.
SWARM_MODES = tuple(m for m in hearth_swarmloop.ALLOWED_MODES if m != "edit")

RESUME_WORDS = ("resume", "continue", "carry on", "keep going")

CEILING_LIMITS = {
    "max_turns": (1, 5000),
    "max_seconds": (1, 7 * 24 * 3600),
    "max_tokens": (1, 1_000_000_000),
    "max_writes": (1, 20000),
    "max_tool_calls": (1, 100000),
    "max_cycles": (1, 100),
}
#: A relay's phase list is unbounded in principle and the snapshot is sent in
#: full on every frame, so it is bounded here. The journal keeps all of them.
MAX_PHASES_IN_SNAPSHOT = 60


# --------------------------------------------------------------------------
# the gauge
# --------------------------------------------------------------------------

class SwarmStatus:
    """The current state of the relay, as a versioned whole snapshot.

    Same contract as loop_engine.LoopStatus and downloads.DownloadManager:
    `version` bumps whenever anything changes and snapshot_after(version)
    blocks until it does. One instance per sidecar process.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._new = threading.Condition(self._lock)
        self._version = 0
        self._pending = None
        self._reset_locked()

    def _reset_locked(self):
        self._run = None
        self._report = None
        self._account = None
        self._cancelled_fn = None
        self._workers_fn = None

    def _bump_locked(self):
        self._version += 1
        self._new.notify_all()

    def touch(self):
        """Bump the version without changing anything. POST /cancel calls
        this so a watcher blocked in snapshot_after learns that `stop_requested`
        (which is DERIVED from the session's own cancel flag, never stored
        here) has changed, instead of finding out a keep-alive later."""
        with self._lock:
            self._bump_locked()

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001 - a broken probe must not break the gauge
            return None

    def _snapshot_locked(self):
        run = dict(self._run) if self._run else None
        if run is not None:
            run["stop_requested"] = bool(
                self._cancelled_fn and self._safe(self._cancelled_fn))
            run["live_workers"] = (int(self._safe(self._workers_fn) or 0)
                                   if self._workers_fn else 0)
            if run.get("state") == "running" and run["stop_requested"]:
                run["state"] = "stopping"
            if run.get("started_at"):
                run["elapsed"] = round(max(0.0, time.time() - run["started_at"]), 2)
        return {
            "version": self._version,
            "run": run,
            "report": self._report,
            "account": self._account,
            "pending": dict(self._pending) if self._pending else None,
            # Both lists, verbatim and structured, exactly as the loop ships
            # its own. A UI renders them; it does not summarise them.
            "blind_spots": [
                {"headline": h, "means": m, "remedy": r}
                for h, m, r in hearth_swarmloop.SWARM_BLIND_SPOTS
            ],
            "loop_blind_spots": [
                {"headline": h, "means": m, "remedy": r}
                for h, m, r in hearth_workloop.PROGRESS_BLIND_SPOTS
            ],
        }

    def snapshot(self):
        with self._lock:
            return self._snapshot_locked()

    def snapshot_after(self, version, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while self._version <= version:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                self._new.wait(timeout=remaining)
            return self._snapshot_locked()

    # ---- writes ---------------------------------------------------------

    def set_pending(self, pending):
        """Record (or clear) an unfinished relay inherited from a previous
        process. Outside the per-run state begin() resets, because it must
        outlive the run that replaces it."""
        with self._lock:
            self._pending = dict(pending) if pending else None
            self._bump_locked()

    def begin(self, *, run_id, goal, model, workspace, mode, ceilings, roles,
              gate_policy, gate_timeout, allowed_tools, done_command,
              required_artifacts, resumed, cancelled_fn=None, workers_fn=None):
        with self._lock:
            self._reset_locked()
            self._cancelled_fn = cancelled_fn
            self._workers_fn = workers_fn
            self._run = {
                "run_id": run_id, "goal": goal, "model": model,
                "workspace": workspace, "mode": mode,
                "state": "running", "resumed": bool(resumed),
                "started_at": time.time(), "elapsed": 0.0,
                "cycle": 0, "phase": 0, "role": None, "turn": 0,
                # ONE spend, for the whole relay. See the module docstring.
                "spend": {"turns": 0, "elapsed": 0.0, "tokens": 0,
                          "writes": 0, "tool_calls": 0},
                "ceilings": dict(ceilings),
                "roles": list(roles),
                "phases": [],
                "gate_policy": gate_policy, "gate_timeout": gate_timeout,
                "allowed_tools": list(allowed_tools),
                "done_command": done_command,
                "required_artifacts": list(required_artifacts or ()),
                # Whether a DETERMINISTIC check exists at all. Not the same
                # question as whether a reviewer liked it, and kept apart from
                # `reviewer_approved` all the way to the screen.
                "verified": bool(done_command or required_artifacts),
                "reviewer_approved": False,
                "swaps": 0, "swap_seconds": 0.0,
                "lease_refusals": 0,
                "gates_allowed": 0, "gates_denied": 0, "gates_timed_out": 0,
                "checkpoints": 0, "last_tool": None, "last_progress": None,
            }
            self._bump_locked()

    def _edit(self, fn):
        with self._lock:
            if self._run is None:
                return
            fn(self._run)
            self._bump_locked()

    def note_phase_start(self, index, cycle, role, model, writes):
        def _apply(r):
            r["phase"] = int(index)
            r["cycle"] = int(cycle)
            r["role"] = role
            r["turn"] = 0
            phases = list(r.get("phases") or [])
            phases.append({"index": index, "cycle": cycle, "role": role,
                           "model": model, "writes": bool(writes),
                           "state": "running", "turns": 0, "tokens": 0,
                           "stop_reason": None, "bound_by": None})
            r["phases"] = phases[-MAX_PHASES_IN_SNAPSHOT:]
        self._edit(_apply)

    def note_phase_end(self, index, summary, bound_by, spend):
        def _apply(r):
            phases = [dict(p) for p in (r.get("phases") or [])]
            for p in phases:
                if p["index"] == index:
                    p["state"] = "done"
                    p["stop_reason"] = summary.get("stop_reason")
                    p["bound_by"] = bound_by
                    p["turns"] = summary.get("turns") or 0
                    p["tokens"] = summary.get("tokens") or 0
                    p["created"] = (summary.get("created") or [])[:10]
                    p["modified"] = (summary.get("modified") or [])[:10]
                    p["lease_refusals"] = summary.get("lease_refusals") or 0
                    p["final_text"] = summary.get("final_text") or ""
            r["phases"] = phases
            if isinstance(spend, dict):
                r["spend"] = dict(spend)
            r["lease_refusals"] = r.get("lease_refusals", 0) + (
                summary.get("lease_refusals") or 0)
        self._edit(_apply)

    def note_turn(self, turn):
        self._edit(lambda r: r.update(turn=int(turn)))

    def note_progress(self, turn, spend, detail):
        def _apply(r):
            r["turn"] = int(turn)
            if isinstance(spend, dict):
                # A phase's spend is only that phase's. The relay's own total
                # arrives with note_phase_end, which is authoritative; this
                # only keeps the meters moving DURING a long phase, by adding
                # the running phase's spend to what previous phases already
                # used. Without the offset the meters would jump backwards at
                # every handoff.
                base = r.get("_phase_base") or {}
                merged = dict(r.get("spend") or {})
                for k, v in spend.items():
                    if isinstance(v, (int, float)):
                        merged[k] = base.get(k, 0) + v
                r["spend"] = merged
            r["last_progress"] = detail
        self._edit(_apply)

    def rebase_phase(self, spend):
        """Remember the relay-wide totals as of the start of a phase, so a
        phase's own running spend can be added to them rather than replacing
        them. Called at each phase start."""
        self._edit(lambda r: r.update(_phase_base=dict(spend or {})))

    def note_tool(self, tool):
        self._edit(lambda r: r.update(last_tool=tool))

    def note_checkpoint(self):
        self._edit(lambda r: r.update(checkpoints=r.get("checkpoints", 0) + 1))

    def note_swap(self, seconds):
        def _apply(r):
            r["swaps"] = r.get("swaps", 0) + 1
            r["swap_seconds"] = round(r.get("swap_seconds", 0.0) + (seconds or 0), 2)
        self._edit(_apply)

    def note_gate(self, outcome):
        key = {"allow": "gates_allowed", "deny": "gates_denied",
               "timeout": "gates_timed_out"}.get(outcome)
        if key:
            self._edit(lambda r: r.update({key: r.get(key, 0) + 1}))

    def finish(self, report, account):
        with self._lock:
            if self._run is not None:
                self._run["state"] = "stopped"
                self._run["stop_reason"] = (report or {}).get("stop_reason")
                self._run["stop_detail"] = (report or {}).get("stop_detail")
                self._run["reviewer_approved"] = bool(
                    (report or {}).get("reviewer_approved"))
                # The report's own `verified` is the deterministic one. It
                # REPLACES the "a check was configured" value set at begin(),
                # because what matters at the end is whether it passed.
                self._run["verified"] = bool((report or {}).get("verified"))
                if isinstance((report or {}).get("spend"), dict):
                    self._run["spend"] = dict(report["spend"])
            self._report = report
            self._account = account
            self._bump_locked()

    def fail(self, message):
        with self._lock:
            if self._run is not None:
                self._run["state"] = "stopped"
                self._run["stop_reason"] = hearth_swarmloop.STOP_ERROR
                self._run["stop_detail"] = message
            self._bump_locked()


class _SessionWorkers(hearth_workloop._Workers):  # noqa: SLF001 - the seam is deliberate
    """The relay's abandoned-call counter, wired to the Session's.

    One counter reporting to both, for loop_engine's reason: POST /restore and
    POST /session consult Session.is_workspace_busy(), and a second private
    tally would mean the number being consulted is not the number being
    incremented."""

    def __init__(self, session=None, on_change=None):
        super().__init__()
        self._session = session
        self._on_change = on_change

    def start(self):
        super().start()
        if self._session is not None:
            self._session._worker_started()  # noqa: SLF001
        self._changed()

    def finish(self):
        super().finish()
        if self._session is not None:
            self._session._worker_finished()  # noqa: SLF001
        self._changed()

    def _changed(self):
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------
# what a journal on disk is allowed to claim
# --------------------------------------------------------------------------

def inspect_journal(run_id, load_fn=None):
    """Decide what a prior relay's journal may be believed about.

    Identical suspicion to loop_engine.inspect_journal, plus the one this
    shape adds: a swarm journal names ROLES, and a role carries a `writes`
    flag and a tool manifest. Believing them would let a finished run grant
    its successor a second writer, or a writer with tools no role is allowed,
    by appending a line to a log in a directory the agent's own write_file can
    reach. So the roles are validated and never adopted: what comes back is a
    verdict about resumability, and run_swarm is always given this build's own
    DEFAULT_ROLES.
    """
    load = load_fn or hearth_swarmloop.load_journal
    try:
        prior = load(run_id)
    except OSError as exc:
        return {"found": False, "resumable": False,
                "refusal": "its journal could not be read: {}".format(exc),
                "completed_phases": 0, "interrupted_phase": None,
                "stopped": None, "goal": None}

    records = prior.get("records") or []
    header = prior.get("header")
    completed = len(prior.get("completed_phases") or [])
    interrupted = prior.get("interrupted_phase")
    stopped = prior.get("stopped")
    out = {"found": bool(records), "resumable": False, "refusal": None,
           "completed_phases": completed, "interrupted_phase": interrupted,
           "stopped": (stopped or {}).get("reason") if stopped else None,
           "goal": None}

    if not records:
        out["refusal"] = "its journal is empty or missing"
        return out
    if not isinstance(header, dict):
        out["refusal"] = "its journal has no run header, so nothing in it can be placed"
        return out

    out["goal"] = header.get("goal") if isinstance(header.get("goal"), str) else None

    if header.get("run_id") != run_id:
        out["refusal"] = ("its journal claims to belong to a different run "
                          "({!r})".format(header.get("run_id")))
        return out
    if header.get("version") != hearth_swarmloop.JOURNAL_VERSION:
        out["refusal"] = ("its journal is format version {!r}, and this build writes "
                          "version {}".format(header.get("version"),
                                              hearth_swarmloop.JOURNAL_VERSION))
        return out
    mode = header.get("mode")
    if mode not in SWARM_MODES:
        out["refusal"] = ("its journal says the run used mode {!r}, which a swarm "
                          "may not run in".format(mode))
        return out
    if header.get("gate_policy") not in hearth_swarmloop.GATE_POLICIES:
        out["refusal"] = ("its journal names gate policy {!r}, which is not a real "
                          "one".format(header.get("gate_policy")))
        return out

    roles = header.get("roles")
    if not isinstance(roles, list) or not roles:
        out["refusal"] = "its journal does not record what roles the run had"
        return out
    writers = [r for r in roles
               if isinstance(r, dict) and r.get("writes")]
    if len(writers) > 1:
        # The single-writer property, checked on the way back in. A journal
        # claiming two writers describes a run this build would never have
        # started, so it is evidence the file has been edited.
        out["refusal"] = ("its journal claims {} roles could write, and at most one "
                          "ever may".format(len(writers)))
        return out
    widened = set()
    for r in roles:
        if not isinstance(r, dict) or not isinstance(r.get("tools"), list):
            out["refusal"] = "its journal's role manifests are not lists of tool names"
            return out
        widened |= set(r["tools"]) - set(hearth_workloop.DEFAULT_ALLOWED_TOOLS)
    if widened:
        out["refusal"] = ("its journal claims tools outside what any role may ever be "
                          "given ({})".format(", ".join(sorted(widened))))
        return out
    if stopped:
        out["refusal"] = "it already stopped ({})".format(stopped.get("reason"))
        return out

    out["resumable"] = True
    return out


# --------------------------------------------------------------------------
# what POST /session may ask for
# --------------------------------------------------------------------------

def parse_swarm_config(raw):
    """Validate a POST /session `swarm` object into kwargs for build_swarm_engine.

    Two refusals worth naming, both the same in spirit as loop_engine's:

      * No ceiling can be omitted into unboundedness and none may be 0. There
        is no way to spell "unlimited" over this transport.
      * `allowed_tools` may only NARROW, and is applied to EVERY role by
        intersection with that role's own manifest. It can therefore make the
        implementer read-only (which makes the relay useless but harmless) and
        can never give the reviewer a writer.

    And one that is specific to this module: THERE IS NO `roles` FIELD. A
    caller that could describe roles could describe two writers, and the
    single-writer property would stop being structural.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("swarm must be an object")
    if "roles" in raw:
        raise ConfigError(
            "roles cannot be set over this transport: the swarm's roles are fixed "
            "so that exactly one of them can ever write. Narrow allowed_tools "
            "instead, which applies to every role.")

    ceil_raw = raw.get("ceilings") or {}
    if not isinstance(ceil_raw, dict):
        raise ConfigError("swarm.ceilings must be an object")
    ceilings = hearth_swarmloop.SwarmCeilings().to_dict()
    for key, value in ceil_raw.items():
        if key not in CEILING_LIMITS:
            raise ConfigError("unknown ceiling {!r}".format(key))
        low, high = CEILING_LIMITS[key]
        ceilings[key] = _bounded_int(value, "ceilings." + key, low, high)

    stall_raw = raw.get("stall") or {}
    if not isinstance(stall_raw, dict):
        raise ConfigError("swarm.stall must be an object")
    stall = hearth_workloop.StallPolicy().to_dict()
    for key, value in stall_raw.items():
        if key not in STALL_LIMITS:
            raise ConfigError("unknown stall setting {!r}".format(key))
        low, high = STALL_LIMITS[key]
        stall[key] = _bounded_int(value, "stall." + key, low, high)

    gate_policy = raw.get("gate_policy") or "deny"
    if gate_policy not in hearth_swarmloop.GATE_POLICIES:
        raise ConfigError("gate_policy must be one of {}".format(
            ", ".join(hearth_swarmloop.GATE_POLICIES)))
    gate_timeout = raw.get("gate_timeout_seconds")
    if gate_timeout is None:
        gate_timeout = GATE_TIMEOUT_DEFAULT
    gate_timeout = _bounded_int(gate_timeout, "gate_timeout_seconds",
                                *GATE_TIMEOUT_RANGE)

    tools_raw = raw.get("allowed_tools")
    if tools_raw is None:
        allowed_tools = None
    else:
        if not isinstance(tools_raw, list) or not all(
                isinstance(t, str) for t in tools_raw):
            raise ConfigError("allowed_tools must be a list of tool names")
        if not tools_raw:
            raise ConfigError("allowed_tools cannot be empty: a run with no tools "
                              "can only talk to itself")
        outside = sorted(set(tools_raw) - set(hearth_workloop.DEFAULT_ALLOWED_TOOLS))
        if outside:
            raise ConfigError(
                "a swarm may only narrow its capability manifest, never widen it; "
                "{} {} outside what any role may be given".format(
                    ", ".join(outside), "is" if len(outside) == 1 else "are"))
        allowed_tools = sorted(set(tools_raw))

    done_command = raw.get("done_command")
    if done_command is not None:
        if not isinstance(done_command, str):
            raise ConfigError("done_command must be a string")
        done_command = done_command.strip() or None
        if done_command and len(done_command) > MAX_DONE_COMMAND_CHARS:
            raise ConfigError("done_command is longer than {} characters".format(
                MAX_DONE_COMMAND_CHARS))

    artifacts_raw = raw.get("required_artifacts")
    required_artifacts = None
    if artifacts_raw is not None:
        if not isinstance(artifacts_raw, list) or not all(
                isinstance(a, str) for a in artifacts_raw):
            raise ConfigError("required_artifacts must be a list of paths")
        cleaned = [a.strip() for a in artifacts_raw if a.strip()]
        if len(cleaned) > MAX_REQUIRED_ARTIFACTS:
            raise ConfigError("at most {} required artifacts".format(
                MAX_REQUIRED_ARTIFACTS))
        for path in cleaned:
            # hearth_contain.is_rooted, not os.path.isabs: ntpath.isabs stopped
            # calling a single leading slash absolute in Python 3.13, so this
            # check refused "/etc/passwd" under a 3.12 sidecar and accepted it
            # under a 3.14 one. The artifact list is user-supplied over HTTP,
            # so the rule has to be the same rule on every interpreter.
            if hearth_contain.is_rooted(path) or ".." in path.replace("\\", "/").split("/"):
                raise ConfigError(
                    "required artifact {!r} must be a path inside the workspace"
                    .format(path))
        required_artifacts = cleaned or None

    return {
        "ceilings": ceilings, "stall": stall, "gate_policy": gate_policy,
        "gate_timeout": gate_timeout, "allowed_tools": allowed_tools,
        "done_command": done_command, "required_artifacts": required_artifacts,
    }


def narrowed_roles(allowed_tools):
    """This build's roles, each intersected with `allowed_tools`.

    Intersection, never assignment: a role's own manifest is the ceiling and
    the request can only cut into it. That is what makes it impossible for a
    request to hand the reviewer a write tool, and it is why this returns
    RoleSpec objects built from the ROLE's flags rather than the request's.

    A role whose intersection leaves it no write tool is reported as
    read-only, because it is. `writes` drives the "can change files" badge on
    the role card and the write lease, and a narrowing that removes every
    write tool from the implementer would otherwise leave the screen
    promising an authority the run does not actually have. It can only ever
    move True to False here; nothing can move it the other way.
    """
    edit_tools = {t for t in hearth_swarmloop.IMPLEMENTER_TOOLS
                  if permissions.risk_of(t) == "edit"}
    roles = []
    for r in hearth_swarmloop.DEFAULT_ROLES:
        tools = r.tools if allowed_tools is None else (r.tools & set(allowed_tools))
        writes = bool(r.writes and (tools & edit_tools))
        roles.append(hearth_swarmloop.RoleSpec(
            r.name, r.purpose, r.instruction, tools, writes=writes,
            max_turns=r.max_turns, tier=r.tier))
    return tuple(roles)


def config_defaults():
    """Everything a UI needs to draw the form BEFORE a run exists.

    Shipped from here rather than written in JavaScript, so the form cannot
    offer a value parse_swarm_config would refuse. `roles` carries each role's
    manifest and, per tool, what permissions.decide itself would do with it in
    each mode -- computed by the permission layer, never paraphrased by the
    page. "Which role can change my files" is the question a person starting
    an unattended relay most needs answered, and the honest answer is the
    permission layer's.
    """
    return {
        "config": parse_swarm_config({}),
        "roles": [
            {
                "name": r.name, "purpose": r.purpose, "writes": r.writes,
                "max_turns": r.max_turns, "tier": r.tier,
                "tools": [
                    {"name": t, "risk": permissions.risk_of(t),
                     "effect": {m: permissions.decide(m, t, {}, (), r.tools)
                                for m in SWARM_MODES}}
                    for t in sorted(r.tools)
                ],
            }
            for r in hearth_swarmloop.DEFAULT_ROLES
        ],
        "all_tools": sorted(hearth_workloop.DEFAULT_ALLOWED_TOOLS),
        "gate_policies": list(hearth_swarmloop.GATE_POLICIES),
        "modes": list(SWARM_MODES),
        "limits": {
            "ceilings": {k: list(v) for k, v in CEILING_LIMITS.items()},
            "stall": {k: list(v) for k, v in STALL_LIMITS.items()},
            "gate_timeout_seconds": list(GATE_TIMEOUT_RANGE),
        },
    }


def build_swarm_engine(config=None, status=None, **kwargs):
    """A SwarmEngine from an already-validated parse_swarm_config() result."""
    cfg = dict(config or parse_swarm_config({}))
    return SwarmEngine(
        ceilings=hearth_swarmloop.SwarmCeilings.from_dict(cfg["ceilings"]),
        stall=hearth_workloop.StallPolicy.from_dict(cfg["stall"]),
        gate_policy=cfg["gate_policy"], gate_timeout=cfg["gate_timeout"],
        allowed_tools=cfg["allowed_tools"],
        done_command=cfg["done_command"],
        required_artifacts=cfg["required_artifacts"],
        status=status, **kwargs)


# --------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------

#: The first message get_state() persists, and what
#: session_state._engine_state_is_trustworthy compares byte for byte on the
#: way back in. Its own string rather than the loop's, so a persisted loop
#: conversation cannot be restored into a swarm session or the reverse.
SWARM_SYSTEM = (
    "You are Hearth working UNATTENDED as a relay of narrow roles on one goal, "
    "on the user's own machine. Exactly one role is active at a time and only "
    "one of them may change a file. Nobody is watching this run."
)


class SwarmEngine:
    """An Engine (see session.py's seam) whose `run` is a whole relay.

    Every collaborator is injectable so the self-test can drive a full run, a
    cancel and a restart without an inference engine or a git store.
    """

    ENGINE_KIND = "swarm"

    def __init__(self, run_fn=None, ceilings=None, stall=None, gate_policy="deny",
                 allowed_tools=None, auto_allow=(), done_command=None,
                 required_artifacts=None, journal_factory=None, status=None,
                 gate_timeout=GATE_TIMEOUT_DEFAULT, inspect_fn=None):
        self._run_fn = run_fn or hearth_swarmloop.run_swarm
        self._ceilings = ceilings or hearth_swarmloop.SwarmCeilings()
        self._stall = stall or hearth_workloop.StallPolicy()
        if gate_policy not in hearth_swarmloop.GATE_POLICIES:
            raise ValueError("gate_policy must be one of {}".format(
                ", ".join(hearth_swarmloop.GATE_POLICIES)))
        self._gate_policy = gate_policy
        self._gate_timeout = gate_timeout
        self._allowed_tools = allowed_tools
        self._roles = narrowed_roles(allowed_tools)
        self._auto_allow = auto_allow
        self._done_command = done_command
        self._required_artifacts = required_artifacts
        self._journal_factory = journal_factory
        self._status = status if status is not None else SwarmStatus()
        self._inspect = inspect_fn or inspect_journal
        self._run_id = None
        self._goal = ""
        self._last = None
        self._finished = True
        self._stream_seq = 0

    @property
    def status(self):
        return self._status

    def manifest(self):
        """What this engine would let a relay do, as the UI shows it before
        the user presses go. Read off the same objects run() passes to
        run_swarm, so the card cannot describe a different run than the one
        that starts."""
        return {
            "ceilings": self._ceilings.to_dict(),
            "stall": self._stall.to_dict(),
            "gate_policy": self._gate_policy,
            "gate_timeout": self._gate_timeout,
            "allowed_tools": (sorted(self._allowed_tools)
                              if self._allowed_tools is not None else None),
            "roles": [r.to_dict() for r in self._roles],
            "done_command": self._done_command,
            "required_artifacts": list(self._required_artifacts or ()),
            "modes": list(SWARM_MODES),
        }

    def restore_config(self):
        """This run's configuration, in the shape parse_swarm_config returns.

        Written to a file the agent's own write_file can reach, so it is
        re-validated through parse_swarm_config on the way back in (main.py).
        This side only has to be honest about what was used."""
        return {
            "ceilings": self._ceilings.to_dict(),
            "stall": self._stall.to_dict(),
            "gate_policy": self._gate_policy,
            "gate_timeout_seconds": self._gate_timeout,
            "allowed_tools": (sorted(self._allowed_tools)
                              if self._allowed_tools is not None else None),
            "done_command": self._done_command,
            "required_artifacts": list(self._required_artifacts or ()),
        }

    # ---- the session_state seam -----------------------------------------

    def expected_system_prompt(self, mode):  # noqa: ARG002
        return SWARM_SYSTEM

    def get_state(self):
        return {
            "messages": [{"role": "system", "content": SWARM_SYSTEM}],
            "run_id": self._run_id,
            "goal": self._goal,
            "finished": self._finished,
            "last_report": self._last,
        }

    def load_state(self, state):
        if not isinstance(state, dict):
            return
        run_id = state.get("run_id")
        self._run_id = run_id if isinstance(run_id, str) and run_id else None
        goal = state.get("goal")
        self._goal = goal if isinstance(goal, str) else ""
        self._finished = bool(state.get("finished", True))
        last = state.get("last_report")
        self._last = last if isinstance(last, dict) else None

    def pending_run(self):
        """What this process inherited unfinished, or None."""
        if not self._run_id or self._finished:
            return None
        verdict = self._inspect(self._run_id)
        journal_goal = verdict.get("goal")
        return {
            "run_id": self._run_id,
            "goal": self._goal,
            "completed_phases": verdict.get("completed_phases") or 0,
            "interrupted_phase": verdict.get("interrupted_phase"),
            "resumable": bool(verdict.get("resumable")),
            "refusal": verdict.get("refusal"),
            "journal_goal_differs": bool(
                journal_goal is not None and self._goal and journal_goal != self._goal),
        }

    def publish_pending(self):
        self._status.set_pending(self.pending_run())

    # ---- the turn -------------------------------------------------------

    def _forward(self, ctx):
        """Translate hearth_swarmloop events onto the session event stream and
        onto the gauge.

        Text deltas are re-framed into the {text, stream_id, index} shape the
        existing UI already renders and de-duplicates, so a relay streams into
        the transcript exactly like a chat turn. Every other event keeps its
        own name and its `role`/`phase`/`cycle` tags, which is what lets the
        transcript say which role is speaking.
        """
        state = {"index": 0}

        def emit(ev):
            kind = ev.get("type") or "notice"
            if kind == "delta":
                text = ev.get("text") or ""
                ctx.emit("delta", {"text": text, "stream_id": self._stream_seq,
                                   "index": state["index"], "role": ev.get("role")})
                state["index"] += len(text)
                return
            if kind == "phase_start":
                self._stream_seq += 1
                state["index"] = 0
                self._status.rebase_phase(
                    (self._status.snapshot().get("run") or {}).get("spend"))
                self._status.note_phase_start(
                    ev.get("index") or 0, ev.get("cycle") or 0, ev.get("role"),
                    ev.get("model"), ev.get("writes"))
                # Make the transcript durable at a handoff, for loop_engine's
                # reason: Session only persists at turn start and turn end,
                # which for a relay can be a very long way apart.
                sess = getattr(ctx, "session", None)
                persist = getattr(sess, "persist_now", None)
                if callable(persist):
                    persist()
            elif kind == "phase_end":
                self._status.note_phase_end(ev.get("index") or 0,
                                            ev.get("summary") or {},
                                            ev.get("bound_by"), ev.get("spend"))
            elif kind == "turn_start":
                self._stream_seq += 1
                state["index"] = 0
                self._status.note_turn(ev.get("turn") or 0)
            elif kind == "progress":
                self._status.note_progress(ev.get("turn") or 0, ev.get("spend"), {
                    "turn": ev.get("turn"), "role": ev.get("role"),
                    "new_state": ev.get("new_state"),
                    "changed": ev.get("changed"), "errors": ev.get("errors"),
                })
            elif kind == "tool_call":
                self._status.note_tool(ev.get("tool"))
            elif kind == "checkpoint":
                self._status.note_checkpoint()
            elif kind == "swarm_swap":
                self._status.note_swap(ev.get("seconds"))
            data = {k: v for k, v in ev.items() if k != "type"}
            ctx.emit(kind, data)
        return emit

    def _approver(self, ctx):
        """The approve_fn for gate_policy="ask": a real approval card with a
        deadline. loop_engine's argument applies unchanged -- an unbounded
        wait in an unattended run is a hang that reads as work."""
        if self._gate_policy != "ask":
            return None

        def approve(tool, args):
            decision = ctx.request_approval(tool, args, timeout=self._gate_timeout)
            self._status.note_gate(decision)
            return decision == "allow"
        return approve

    def run(self, ctx):
        mode = getattr(ctx, "mode", None)
        if mode not in SWARM_MODES:
            ctx.emit("error", {
                "message": "a swarm cannot run in {!r} mode".format(mode),
                "remedy": ("Set the session to 'auto' (the implementer may read and "
                           "write files unattended, and anything dangerous is "
                           "refused) or 'plan' (every role read-only, which makes "
                           "the relay an investigation rather than a change). "
                           "'edit' gates every write and nobody is here to approve "
                           "them; 'bypass' is not available at all."),
                "modes": list(SWARM_MODES),
            })
            return

        goal = (getattr(ctx, "message", "") or "").strip()
        pending = self.pending_run()
        resume = False
        if pending and goal.lower() in RESUME_WORDS:
            if not pending.get("resumable"):
                ctx.emit("error", {
                    "message": "that relay cannot be resumed: {}".format(
                        pending.get("refusal") or "its journal is not trustworthy"),
                    "remedy": ("Give a new goal to start a fresh relay. The old "
                               "run's workspace and checkpoints are untouched."),
                })
                self._status.set_pending(pending)
                return
            resume = True
            goal = pending["goal"] or goal
            ctx.emit("swarm_resuming", pending)
        elif pending:
            ctx.emit("swarm_abandoned", dict(pending, reason=(
                "a new goal was given, so this unfinished relay was left alone. Its "
                "journal and checkpoints are intact; send 'resume' to continue it "
                "instead.")))
        if not goal:
            ctx.emit("error", {"message": "a swarm needs a goal"})
            return
        if len(goal) > MAX_GOAL_CHARS:
            ctx.emit("error", {"message": "that goal is longer than {} characters".format(
                MAX_GOAL_CHARS)})
            return

        if not resume:
            self._run_id = "swarm-" + os.urandom(6).hex()
        self._goal = goal
        self._finished = False
        token = hearth_workloop.CancelToken(
            is_cancelled_fn=ctx.cancelled,
            external_reason="you pressed Stop")
        session = getattr(ctx, "session", None)
        workers = _SessionWorkers(session=session, on_change=self._status.touch)
        self._status.begin(
            run_id=self._run_id, goal=goal, model=ctx.model,
            workspace=ctx.workspace, mode=mode,
            ceilings=self._ceilings.to_dict(),
            roles=[r.to_dict() for r in self._roles],
            gate_policy=self._gate_policy, gate_timeout=self._gate_timeout,
            allowed_tools=sorted(
                self._allowed_tools if self._allowed_tools is not None
                else hearth_workloop.DEFAULT_ALLOWED_TOOLS),
            done_command=self._done_command,
            required_artifacts=self._required_artifacts,
            resumed=resume, cancelled_fn=ctx.cancelled, workers_fn=workers.live)
        self._status.set_pending(None)
        kwargs = {}
        if self._journal_factory is not None:
            kwargs["journal"] = self._journal_factory(self._run_id)
        try:
            report = self._run_fn(
                goal, ctx.model, ctx.workspace,
                mode=mode, roles=self._roles, ceilings=self._ceilings,
                stall=self._stall, gate_policy=self._gate_policy,
                approve_fn=self._approver(ctx), auto_allow=self._auto_allow,
                done_command=self._done_command,
                required_artifacts=self._required_artifacts,
                token=token, emit=self._forward(ctx), workers=workers,
                run_id=self._run_id, resume=resume, **kwargs)
        except Exception as exc:  # noqa: BLE001 - a relay must never orphan the session
            self._finished = True
            message = "{}: {}".format(type(exc).__name__, exc)
            self._status.fail(message)
            ctx.emit("error", {"message": message})
            return
        finally:
            self._finished = True

        self._last = report.to_dict()
        account = report.render()
        self._status.finish(self._last, account)
        ctx.emit("swarm_report", self._last)
        if report.stop_reason == hearth_swarmloop.STOP_CANCELLED:
            ctx.emit("cancelled", {"tokens_in": report.tokens_in,
                                   "tokens_out": report.tokens_out,
                                   "live_workers": report.live_workers,
                                   "account": account})
        else:
            ctx.emit("done", {"tokens_in": report.tokens_in,
                              "tokens_out": report.tokens_out,
                              "stop_reason": report.stop_reason,
                              "live_workers": report.live_workers,
                              "account": account})


def _self_test():  # noqa: PLR0915
    import json  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    import session as session_mod  # noqa: PLC0415

    tmp = tempfile.mkdtemp(prefix="swarmengine-")
    ws = os.path.join(tmp, "ws")
    os.makedirs(ws, exist_ok=True)

    def journal_for(run_id):
        return hearth_workloop.Journal(os.path.join(tmp, run_id + ".jsonl"), fsync=False)

    def wait_idle(sess, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if sess.status == session_mod.STATUS_IDLE:
                return True
            time.sleep(0.01)
        return False

    def event(sess, kind):
        for e in sess.recent_events(500):
            if e["kind"] == kind:
                return e
        return None

    def kinds(sess):
        return [e["kind"] for e in sess.recent_events(500)]

    def refuses(name, fn, needle=None):
        try:
            fn()
        except ConfigError as exc:
            if needle is not None:
                assert needle in str(exc), (name, str(exc))
            return str(exc)
        raise AssertionError("{} was accepted and must not be".format(name))

    # -- modes -----------------------------------------------------------
    assert "bypass" not in SWARM_MODES and "edit" not in SWARM_MODES, SWARM_MODES
    assert "auto" in SWARM_MODES and "plan" in SWARM_MODES, SWARM_MODES
    for m in SWARM_MODES:
        assert m in permissions.MODES, m

    eng = SwarmEngine(run_fn=lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not run in edit mode")), journal_factory=journal_for)
    s = session_mod.Session(ws, "m", mode="edit", engine=eng)
    s.submit_prompt("do the thing")
    assert wait_idle(s)
    err = event(s, "error")
    assert err and "edit" in err["data"]["message"], err
    assert "auto" in err["data"]["remedy"], err["data"]["remedy"]

    # -- config validation ------------------------------------------------
    base = parse_swarm_config({})
    assert base["ceilings"] == hearth_swarmloop.SwarmCeilings().to_dict(), base
    assert base["gate_policy"] == "deny"
    assert base["allowed_tools"] is None, "no narrowing means every role keeps its own"

    tuned = parse_swarm_config({"ceilings": {"max_turns": 6, "max_cycles": 2}})
    assert tuned["ceilings"]["max_turns"] == 6 and tuned["ceilings"]["max_cycles"] == 2
    assert tuned["ceilings"]["max_tokens"] == \
        hearth_swarmloop.SwarmCeilings().to_dict()["max_tokens"], (
            "an unmentioned ceiling keeps its default rather than vanishing")

    # THERE IS NO WAY TO SPELL "UNLIMITED".
    for bad in (0, -1, None, "40", 4.5, True, 10 ** 12):
        refuses("ceiling {!r}".format(bad),
                lambda b=bad: parse_swarm_config({"ceilings": {"max_turns": b}}))
    refuses("an unknown ceiling", lambda: parse_swarm_config({"ceilings": {"max_vibes": 3}}))
    refuses("a bogus gate policy", lambda: parse_swarm_config({"gate_policy": "yolo"}))
    refuses("a non-object config", lambda: parse_swarm_config([1, 2, 3]))

    # ROLES ARE NOT SETTABLE. A caller that could name roles could name two
    # writers, and the single-writer property would stop being structural.
    msg = refuses("a roles field", lambda: parse_swarm_config(
        {"roles": [{"name": "x", "writes": True}]}))
    assert "roles cannot be set" in msg and "one of them can ever write" in msg, msg

    # The manifest may only NARROW, and narrowing is applied to every role by
    # INTERSECTION, so it can never hand a read-only role a writer.
    narrow = parse_swarm_config({"allowed_tools": ["read_file", "write_file"]})
    assert narrow["allowed_tools"] == ["read_file", "write_file"]
    roles = narrowed_roles(narrow["allowed_tools"])
    by_name = {r.name: r for r in roles}
    assert by_name["reviewer"].tools == {"read_file"}, sorted(by_name["reviewer"].tools)
    assert "write_file" not in by_name["reviewer"].tools, (
        "narrowing must never GIVE a read-only role a write tool")
    assert by_name["implementer"].tools == {"read_file", "write_file"}
    assert len([r for r in roles if r.writes]) == 1, "still exactly one writer"

    # A narrowing that leaves the implementer no write tool makes it read-only
    # in the manifest too, rather than leaving the role card promising a "can
    # change files" authority the run does not have.
    readonly = narrowed_roles(["read_file", "list_files"])
    assert all(not r.writes for r in readonly), [r.to_dict() for r in readonly]
    assert {r.name for r in readonly} == {"planner", "implementer", "reviewer"}
    # And narrowing can never move it the other way.
    assert not any(r.writes for r in narrowed_roles(["read_file"]))
    assert sum(1 for r in narrowed_roles(None) if r.writes) == 1
    msg = refuses("a tool outside the default manifest",
                  lambda: parse_swarm_config({"allowed_tools": ["read_file", "rm_rf"]}))
    assert "rm_rf" in msg and "narrow" in msg, msg
    refuses("an empty manifest", lambda: parse_swarm_config({"allowed_tools": []}))

    for bad in ("/etc/passwd", "C:\\Windows\\x", "../escape", "a/../../b",
                "\\windows\\system32\\drivers\\etc\\hosts", "//host/share/x",
                "C:relative"):
        refuses("artifact {!r}".format(bad),
                lambda b=bad: parse_swarm_config({"required_artifacts": [b]}))

    # config_defaults describes a form that parse_swarm_config would accept.
    d = config_defaults()
    assert parse_swarm_config(
        {"ceilings": d["config"]["ceilings"]})["ceilings"] == d["config"]["ceilings"]
    assert len(d["roles"]) == 3 and [r["name"] for r in d["roles"]] == [
        "planner", "implementer", "reviewer"], d["roles"]
    assert sum(1 for r in d["roles"] if r["writes"]) == 1
    # The per-tool effect comes from permissions.decide, not from a rule the
    # page re-implements.
    impl = next(r for r in d["roles"] if r["name"] == "implementer")
    wf = next(t for t in impl["tools"] if t["name"] == "write_file")
    assert wf["effect"]["auto"] == "allow" and wf["effect"]["plan"] == "deny", wf

    # -- the gauge --------------------------------------------------------
    st = SwarmStatus()
    v0 = st.snapshot()["version"]
    st.begin(run_id="r1", goal="g", model="m", workspace=ws, mode="auto",
             ceilings=hearth_swarmloop.SwarmCeilings().to_dict(),
             roles=[r.to_dict() for r in hearth_swarmloop.DEFAULT_ROLES],
             gate_policy="deny", gate_timeout=300, allowed_tools=["read_file"],
             done_command="pytest", required_artifacts=[], resumed=False)
    snap = st.snapshot()
    assert snap["version"] > v0
    assert snap["run"]["state"] == "running" and snap["run"]["verified"] is True
    assert snap["run"]["reviewer_approved"] is False
    # ONE spend for the whole relay, not one per role.
    assert set(snap["run"]["spend"]) == {"turns", "elapsed", "tokens", "writes",
                                         "tool_calls"}, snap["run"]["spend"]
    assert snap["blind_spots"] and snap["loop_blind_spots"], "both lists ship"
    assert any("not a verification" in b["headline"] for b in snap["blind_spots"])

    st.note_phase_start(1, 1, "planner", "m", False)
    st.note_phase_end(1, {"stop_reason": "stalled", "turns": 2, "tokens": 50,
                          "lease_refusals": 0},
                      "role", {"turns": 2, "tokens": 50})
    snap = st.snapshot()
    assert snap["run"]["phases"][0]["role"] == "planner"
    assert snap["run"]["phases"][0]["state"] == "done"
    assert snap["run"]["phases"][0]["bound_by"] == "role"
    assert snap["run"]["spend"]["tokens"] == 50

    # A stop request is DERIVED from the session's flag, never stored twice.
    flag = {"on": False}
    st2 = SwarmStatus()
    st2.begin(run_id="r2", goal="g", model="m", workspace=ws, mode="auto",
              ceilings={}, roles=[], gate_policy="deny", gate_timeout=300,
              allowed_tools=[], done_command=None, required_artifacts=[],
              resumed=False, cancelled_fn=lambda: flag["on"])
    assert st2.snapshot()["run"]["stop_requested"] is False
    flag["on"] = True
    assert st2.snapshot()["run"]["stop_requested"] is True
    assert st2.snapshot()["run"]["state"] == "stopping"
    # A raising probe must not break the gauge.
    st3 = SwarmStatus()
    st3.begin(run_id="r3", goal="g", model="m", workspace=ws, mode="auto",
              ceilings={}, roles=[], gate_policy="deny", gate_timeout=300,
              allowed_tools=[], done_command=None, required_artifacts=[],
              resumed=False,
              cancelled_fn=lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert st3.snapshot()["run"]["stop_requested"] is False

    # A long relay cannot grow the snapshot without limit.
    st4 = SwarmStatus()
    st4.begin(run_id="r4", goal="g", model="m", workspace=ws, mode="auto",
              ceilings={}, roles=[], gate_policy="deny", gate_timeout=300,
              allowed_tools=[], done_command=None, required_artifacts=[],
              resumed=False)
    for i in range(MAX_PHASES_IN_SNAPSHOT + 25):
        st4.note_phase_start(i, 1, "implementer", "m", True)
    assert len(st4.snapshot()["run"]["phases"]) == MAX_PHASES_IN_SNAPSHOT

    # finish() replaces `verified` with what the report actually proved, and
    # keeps a reviewer's approval as a SEPARATE fact.
    st.finish({"stop_reason": "exhausted", "verified": False,
               "reviewer_approved": True, "spend": {"tokens": 99}}, "the account")
    snap = st.snapshot()
    assert snap["run"]["state"] == "stopped"
    assert snap["run"]["verified"] is False, (
        "a run that configured a check but never passed it is NOT verified")
    assert snap["run"]["reviewer_approved"] is True, (
        "a reviewer's approval is recorded, and kept apart from verification")
    assert snap["account"] == "the account"

    # -- a journal is never believed about authority ----------------------
    def journal_saying(**header):
        base_header = {"run_id": "swarm-j", "version": hearth_swarmloop.JOURNAL_VERSION,
                       "goal": "g", "mode": "auto", "gate_policy": "deny",
                       "roles": [r.to_dict() for r in hearth_swarmloop.DEFAULT_ROLES]}
        base_header.update(header)
        return lambda rid: {"records": [base_header], "header": base_header,
                            "completed_phases": [], "interrupted_phase": None,
                            "stopped": None}

    ok = inspect_journal("swarm-j", journal_saying())
    assert ok["resumable"] is True, ok

    bad_mode = inspect_journal("swarm-j", journal_saying(mode="bypass"))
    assert bad_mode["resumable"] is False and "bypass" in bad_mode["refusal"], bad_mode

    two_writers = inspect_journal("swarm-j", journal_saying(roles=[
        {"name": "a", "writes": True, "tools": ["write_file"]},
        {"name": "b", "writes": True, "tools": ["write_file"]}]))
    assert two_writers["resumable"] is False, two_writers
    assert "at most one" in two_writers["refusal"], two_writers["refusal"]

    widened = inspect_journal("swarm-j", journal_saying(roles=[
        {"name": "a", "writes": True, "tools": ["write_file", "rm_rf"]}]))
    assert widened["resumable"] is False and "rm_rf" in widened["refusal"], widened

    for bad in ({"version": 99}, {"run_id": "other"}, {"gate_policy": "yolo"},
                {"roles": []}, {"roles": "nope"}):
        v = inspect_journal("swarm-j", journal_saying(**bad))
        assert v["resumable"] is False and v["refusal"], (bad, v)

    # -- a full run through a real Session --------------------------------
    class FakeReport:
        def __init__(self):
            self.stop_reason = hearth_swarmloop.STOP_COMPLETED
            self.stop_detail = "ok"
            self.tokens_in = 10
            self.tokens_out = 20
            self.live_workers = 0

        def to_dict(self):
            return {"stop_reason": self.stop_reason, "stop_detail": "ok",
                    "verified": True, "reviewer_approved": False,
                    "phases": [{"index": 1, "role": "planner"}],
                    "spend": {"tokens": 30}}

        def render(self):
            return "THE ACCOUNT"

    seen = {}

    def fake_run(goal, model, workspace, **kw):
        seen.update(kw)
        seen["goal"] = goal
        emit = kw["emit"]
        emit({"type": "phase_start", "index": 1, "cycle": 1, "role": "planner",
              "model": model, "writes": False})
        emit({"type": "delta", "text": "hi", "role": "planner"})
        emit({"type": "phase_end", "index": 1, "cycle": 1, "role": "planner",
              "bound_by": "role", "summary": {"stop_reason": "stalled", "turns": 1,
                                              "tokens": 30, "lease_refusals": 0},
              "spend": {"turns": 1, "tokens": 30}})
        return FakeReport()

    status = SwarmStatus()
    eng2 = SwarmEngine(run_fn=fake_run, status=status, journal_factory=journal_for)
    s2 = session_mod.Session(ws, "m", mode="auto", engine=eng2)
    s2.submit_prompt("build the thing")
    assert wait_idle(s2)
    ks = kinds(s2)
    assert "phase_start" in ks and "phase_end" in ks and "swarm_report" in ks, ks
    assert "done" in ks, ks
    # The relay got THIS build's roles, never anything from a request.
    assert len([r for r in seen["roles"] if r.writes]) == 1, seen["roles"]
    assert seen["mode"] == "auto"
    # Events carry the role, which is how a transcript says who is speaking.
    ps = event(s2, "phase_start")
    assert ps["data"]["role"] == "planner", ps
    snap = status.snapshot()
    assert snap["run"]["state"] == "stopped"
    assert snap["account"] == "THE ACCOUNT"
    assert snap["report"]["verified"] is True

    # The engine is a valid session_state citizen.
    assert SwarmEngine.ENGINE_KIND == "swarm"
    assert eng2.expected_system_prompt("auto") == SWARM_SYSTEM
    got = eng2.get_state()
    assert got["messages"][0]["content"] == eng2.expected_system_prompt("auto"), (
        "session_state compares these byte for byte; a mismatch silently drops "
        "every restored conversation")
    assert got["messages"][0]["content"] != hearth_workloop.LOOP_SYSTEM, (
        "a swarm's persisted prompt must differ from a loop's, or a persisted "
        "loop session could be restored as a swarm")
    assert json.dumps(eng2.restore_config())  # JSON-safe for the state file
    assert parse_swarm_config(eng2.restore_config())  # and round-trips

    # -- a relay that raises reports it and does not orphan the session ---
    eng3 = SwarmEngine(run_fn=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
                       status=SwarmStatus(), journal_factory=journal_for)
    s3 = session_mod.Session(ws, "m", mode="auto", engine=eng3)
    s3.submit_prompt("go")
    assert wait_idle(s3)
    e = event(s3, "error")
    assert e and "boom" in e["data"]["message"], e
    assert eng3.status.snapshot()["run"]["state"] == "stopped"

    # -- an inherited run is offered, and a bad one is refused in words ---
    eng4 = SwarmEngine(run_fn=fake_run, status=SwarmStatus(),
                       journal_factory=journal_for,
                       inspect_fn=lambda rid: {"resumable": False,
                                               "refusal": "its journal is empty or missing",
                                               "completed_phases": 0,
                                               "interrupted_phase": None, "goal": "g"})
    eng4.load_state({"run_id": "swarm-old", "goal": "old goal", "finished": False})
    pend = eng4.pending_run()
    assert pend and pend["resumable"] is False, pend
    s4 = session_mod.Session(ws, "m", mode="auto", engine=eng4)
    s4.submit_prompt("resume")
    assert wait_idle(s4)
    e4 = event(s4, "error")
    assert e4 and "cannot be resumed" in e4["data"]["message"], e4
    assert "empty or missing" in e4["data"]["message"], e4

    # A new goal beside an unfinished run says so rather than silently
    # abandoning or silently continuing it.
    eng5 = SwarmEngine(run_fn=fake_run, status=SwarmStatus(),
                       journal_factory=journal_for,
                       inspect_fn=lambda rid: {"resumable": True, "refusal": None,
                                               "completed_phases": 2,
                                               "interrupted_phase": 3, "goal": "old goal"})
    eng5.load_state({"run_id": "swarm-old", "goal": "old goal", "finished": False})
    s5 = session_mod.Session(ws, "m", mode="auto", engine=eng5)
    s5.submit_prompt("something completely different")
    assert wait_idle(s5)
    ab = event(s5, "swarm_abandoned")
    assert ab and "left alone" in ab["data"]["reason"], ab

    shutil.rmtree(tmp, ignore_errors=True)
    print("swarm-engine self-test OK")
    return 0


if __name__ == "__main__":
    import sys as _sys
    if "--self-test" in _sys.argv:
        _sys.exit(_self_test())
    print("swarm_engine is a library; run with --self-test")
