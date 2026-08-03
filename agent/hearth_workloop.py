#!/usr/bin/env python3
"""hearth work loop: work at one goal across many turns, then finish or say
exactly why it stopped.

WHY THIS MODULE IS SHAPED THE WAY IT IS
======================================

The Linux host runs `hearth_grow`, a self-improvement loop that has turned
roughly a thousand cycles and merged nothing. It is the cautionary example
this module is designed against, so its failure is worth naming precisely.
Reading hearth_grow.run_growth, four separate things had to be true at once:

  1. It has no goal, so it has no definition of done. Every cycle asks the
     model to invent an improvement. The only terminal condition in the
     function is `for cyc in range(1, max_cycles + 1)`. Nothing that happens
     inside the loop can ever end it early, because "finished" is not a
     state the loop can represent.

  2. Its only memory of what it already tried is `attempted[-8:]`, matched
     by nothing at all (it is pasted into a prompt as text). Cycle 100
     re-proposes cycle 1's idea, and cycle 101 re-proposes it with a comma
     moved.

  3. Failure is a completely ordinary outcome with no consequence. The
     `else` branch increments `failed`, writes one memory row, and
     continues. `failed` is read exactly once, to format the summary string
     at the very end. A thousand consecutive failures and a thousand
     consecutive successes take the identical path through the loop, at the
     identical speed.

  4. Its gate can be unavailable and it cannot tell. `nix_check` returns a
     string; anything that does not start with "nix_check PASS" is counted
     as "the model wrote bad Nix". A missing or broken nix, a dirty tree
     that makes `merge_validated`'s `git checkout main` fail every single
     time, and a genuinely incompetent model all produce byte-identical
     loop behaviour.

So: no goal, no memory that bites, no escalation on failure, and a gate
whose own unavailability is silently reinterpreted as the model's fault.
None of that is a missing feature. It is four missing feedback paths, and
the loop cannot explain itself because it never had anything to say.

This module is the inverse of each of those four points:

  1. A run has ONE goal and a deterministic completion check (a command
     whose exit status decides, and/or artifacts that must exist). The
     model's opinion that it is finished is an input to that check, never
     the check itself. See _completion_state.

  2. Every turn is fingerprinted -- the tool calls it made, the state the
     workspace was left in, and the errors it saw -- and those fingerprints
     are compared against every previous turn of the run, not a window of
     eight. See ProgressLedger.

  3. Lack of progress is itself a terminal condition, on the same footing
     as success. A run that stops because it stalled is a normal, expected,
     fully explained outcome. See ProgressLedger.verdict and STOP_STALLED.

  4. Everything the loop depends on that can be unavailable (the completion
     command, the checkpoint store, the engine) reports its own
     unavailability as a distinct state, and the report says so rather than
     folding it into "the model failed". See LoopReport.render.

WHAT IS REUSED RATHER THAN REBUILT
==================================

The brief for this module was explicit that it must not grow a second,
weaker copy of machinery Hearth already has. It does not:

  permissions.decide      the only authority on whether a tool may run,
                          including the `allowed_tools` capability manifest,
                          which is a hard cap in EVERY mode including bypass.
  hearth_tools.execute_tool   the only tool executor.
  hearth_loop.chat        the only chat call, including its streaming
                          `on_token` seam, which is what actually stops a
                          local model mid-generation (see CancelToken).
  hearth_loop.parse_content_tool_calls   the only fallback parser for a
                          model that writes its tool call as prose.
  hearth_checkpoint       the only checkpoint store (git-backed, with undo).

What is genuinely new here, and existed nowhere before, is the loop control:
ceilings, one cancellation token threaded through every blocking point,
progress detection, the journal that survives a restart, and the report.

THE KILL SWITCH
===============

One object, CancelToken, consulted at every point where this module can
block, and nowhere is there a second mechanism:

  between turns            checked at the top of every turn
  during model generation  the token sink passed as chat()'s `on_token`
                           raises hearth_backend.StopStream the instant the
                           token is set, which hangs up on the engine. This
                           is the same mechanism desktop/server/engine.py's
                           _DeltaStream uses, for the same reason: a local
                           model will happily generate for another minute,
                           and waiting for it is not stopping.
  during a tool call       _call_cancellable runs the tool on a worker
                           thread and abandons it. As in engine.py, abandon
                           is not kill: Python cannot safely kill a thread,
                           so a `run_command` already in flight keeps running
                           until it unwinds. live_workers() reports how many
                           such calls are still out, so a caller can tell
                           "the loop stopped" from "the workspace is quiet".
  during a checkpoint      the checkpoint itself is one git call and is not
                           interruptible, but it runs through the same
                           _call_cancellable, so the loop stops waiting on it
                           and does not start another turn behind it.
  during a retry backoff   CancelToken.sleep wakes on cancellation instead
                           of sleeping through it.

A token can also be driven by an EXTERNAL flag (`is_cancelled_fn`), which is
how the desktop sidecar wires POST /cancel straight into a running loop: the
Session's existing per-turn cancel flag becomes this token's source of truth,
so there is exactly one thing a user's Stop button has to set.

PROGRESS, AND WHAT IT CANNOT SEE
================================

ProgressLedger's honest claim is narrow: it detects that the RUN IS NOT
CHANGING ANYTHING IT HAS NOT ALREADY CHANGED. It does that with four
independent detectors (see ProgressLedger.verdict) over the whole run, and
names which one fired.

Its blind spots are real and are documented on ProgressLedger itself rather
than buried: work that happens outside the workspace is invisible to it, and
steady forward motion toward a wrong answer looks exactly like progress.
Read that docstring before trusting a green verdict.

PERMISSIONS
===========

An unattended loop cannot stop at every approval card, and `bypass` is not
the answer. This module refuses bypass outright, at construction, and again
on resume from a journal -- the same refusal desktop/server/session_state.py
makes for the same reason, and for the additional reason that hearth_evolve
hardcodes `st = {"mode": "bypass"}` and is exactly the mistake not to copy.

What a run gets instead is three layers:

  a capability manifest   `allowed_tools`, enforced by permissions.decide as
                          a hard cap in every mode. A run declares the tools
                          it may use and cannot reach anything else.
  auto mode               reads and file edits run unattended; everything
                          dangerous (shell, network) is gated.
  a write budget          `max_writes`, a ceiling on unattended file writes.
                          Unsupervised editing is bounded in the same way
                          time and tokens are, and exhausting it stops the
                          run with that named reason.

A gated tool in an unattended run must not block on an approval nobody is
awake to give. `gate_policy` decides: "deny" (the default) refuses it, tells
the model why, and counts it -- and because a model that keeps trying the
same refused thing produces an identical action fingerprint every turn, the
stall detector ends the run with a legible account instead of letting it
grind. "stop" ends the run at the first gate. "ask" blocks on an approval
callback, for an attended run.

A command the USER supplied when starting the run (`done_command`) is not a
model-chosen action and does not pass through permissions: it is the run's
deterministic gate, exactly as `nix_check` is hearth_evolve's. It is
bounded by its own timeout and runs through the cancellation token.

CRASH SURVIVAL
==============

The journal is append-only JSONL, one file per run, fsynced at each turn
boundary. A turn writes a "start" record before any blocking work and an
"end" record only once it has fully completed, so a start with no matching
end is exactly the signature of a process that died mid-turn.

Such a turn is NEVER resumed. It is recorded as interrupted and the run
continues from the last COMPLETED turn boundary, which is the same refusal
session_state.restore_session makes: there is no way to know whether the
interrupted turn's last tool call landed against the workspace, so pretending
to know is the one unacceptable option. The progress ledger is rebuilt from
the journal, so stall detection spans a restart rather than resetting.

Standard library only.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hearth_backend  # noqa: E402
import hearth_checkpoint  # noqa: E402
import hearth_contain  # noqa: E402
import hearth_loop  # noqa: E402
import hearth_paths  # noqa: E402
import hearth_tools  # noqa: E402
import permissions  # noqa: E402

DEFAULT_OLLAMA = "http://127.0.0.1:11434"
JOURNAL_VERSION = 1

# Why a run stopped. Exactly one of these ends every run, including a crash
# (STOP_ERROR) -- there is no path out of run_workloop that does not set one.
STOP_COMPLETED = "completed"
STOP_CEILING = "ceiling"
STOP_STALLED = "stalled"
STOP_CANCELLED = "cancelled"
STOP_BLOCKED = "blocked"
STOP_ERROR = "error"
STOP_REASONS = (STOP_COMPLETED, STOP_CEILING, STOP_STALLED, STOP_CANCELLED,
                STOP_BLOCKED, STOP_ERROR)

# Modes a run may use. bypass is absent by construction, not by omission:
# derived from permissions.MODES so a mode added there has to be excluded
# here by name if it needs the same treatment. Mirrors
# desktop/server/session_state.RESTORABLE_MODES, which makes the identical
# refusal for the identical reason.
ALLOWED_MODES = tuple(m for m in permissions.MODES if m != "bypass")

# The default capability manifest for an unattended run: everything needed to
# read a workspace, change files in it, and record what was learned. Nothing
# that leaves the machine, and nothing that touches Hearth's own config.
# permissions.decide enforces this as a hard cap in every mode.
DEFAULT_ALLOWED_TOOLS = frozenset({
    "read_file", "list_files", "list_tree", "search_files",
    "write_file", "edit_file", "replace_in_files",
    "git_status", "git_diff",
    "remember", "recall",
    "run_command",
})

GATE_POLICIES = ("deny", "stop", "ask")

# What progress detection genuinely cannot see. This is the same list
# ProgressLedger's docstring carries, lifted into data for one reason: the
# person who most needs it is the one starting an unattended run from a GUI,
# and they will never read a Python docstring. A user who believes "it did not
# stall" means "it worked" has been misled by this module, so the account
# (LoopReport.render) and any UI in front of it render this verbatim rather
# than paraphrasing it into reassurance. Each entry is
# (headline, what_it_means, what_to_do_about_it).
PROGRESS_BLIND_SPOTS = (
    ("Steady progress toward a wrong answer trips nothing.",
     "Every detector here measures CHANGE, not correctness. A model writing a "
     "confident, fluent, completely incorrect solution leaves a new workspace "
     "state every single turn and will never look stalled.",
     "Set a completion check: a command whose exit status decides, or files "
     "that must exist. Without one, 'finished' is the model's own opinion."),
    ("Work outside the workspace reads as a stall.",
     "Progress is measured by fingerprinting the workspace directory. A run "
     "whose real effect lands on a database, a remote service, or any path "
     "outside it looks like it changed nothing, and will be stopped for it.",
     "Widen the workspace, or give the run a completion check that can "
     "observe the real effect."),
    ("Growth is not value.",
     "A model appending one more junk line to a file each turn changes the "
     "fingerprint each turn and reads as progress for as long as its ceilings "
     "allow.",
     "Read the turn-by-turn list and the diff before trusting the result."),
    ("Some changes are below the resolution.",
     "Files inside ignored directories (.git, node_modules, build caches) do "
     "not register at all, and very large files are fingerprinted by size and "
     "modification time rather than by content.",
     "Expect a run that only touches those to look idle."),
    ("A slow start is never called a stall.",
     "No verdict at all is reached before the run's min_turns threshold, so "
     "the first few turns are unjudged by design.",
     "Ceilings, not stall detection, are what bound those turns."),
    ("A completion check that fails quietly is invisible to the error detector.",
     "The repeated-error detector reads the check's output for error-shaped "
     "lines. A check that fails by printing 'wrong at 3 of 6 indices', with no "
     "word like error, failed or traceback anywhere in it, contributes no "
     "signature at all. Measured on this machine: a task built that way ran to "
     "its full turn ceiling under every patience setting, including the most "
     "eager one, because nothing had anything to repeat.",
     "Make the check say FAILED (most test runners already do), or accept that "
     "the ceilings are the only thing that will stop that run."),
    ("How long it keeps trying is a setting, not a fact.",
     "'Stopped making progress' means it crossed the thresholds THIS run was "
     "given. Measured on this machine, the eager settings this loop first "
     "shipped with stopped runs that went on to pass their tests when they "
     "were allowed to continue; the patient ones spend nearly the whole "
     "budget on a task that can never pass. Both are real costs and the "
     "setting decides which one you pay.",
     "Pick the patience setting deliberately before starting, and read which "
     "one judged the run at the top of this account."),
)

# Directories never walked when fingerprinting the workspace. Version control
# internals, caches and virtualenvs churn constantly for reasons that have
# nothing to do with whether the agent is making progress, and including them
# would report novelty on every single turn -- which is precisely the failure
# this module exists to detect, arriving through the back door.
IGNORE_DIRS = frozenset({
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea", ".vscode",
    "dist", "build", ".eggs", ".hearth", ".next", "target",
})
MAX_SCAN_FILES = 4000        # bound on the workspace walk
HASH_BYTES_CAP = 2_000_000   # files larger than this are fingerprinted by
                             # (size, mtime) rather than content
MAX_GENERATION_CHARS = 200_000  # runaway guard, enforced DURING generation

_HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------
# cancellation
# --------------------------------------------------------------------------

class CancelToken:
    """The one thing that stops a run, consulted everywhere this module can
    block. See the module docstring's "THE KILL SWITCH" section.

    `is_cancelled_fn`, when given, is an EXTERNAL source of truth polled by
    cancelled(). It exists so a host that already owns a cancel flag (the
    desktop sidecar's Session, whose flag POST /cancel sets) does not have to
    keep a second flag in sync with this one: the loop adopts the host's flag
    rather than shadowing it. A raising or misbehaving external source is
    treated as "not cancelled" so a broken host cannot wedge a run, but it
    also cannot silently disarm the internal flag, which is checked first.
    """

    def __init__(self, is_cancelled_fn=None, reason="stopped",
                 external_reason="stopped by the host"):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = ""
        self._default_reason = reason
        # What the report says when the EXTERNAL flag is what fired. "the
        # host" is accurate and useless to a person: in the desktop app the
        # host is that person's own Stop button, and this string is what they
        # read in the account. So the host names itself.
        self._external_reason = external_reason
        self._external = is_cancelled_fn

    def cancel(self, reason=None):
        """Set the token. The FIRST reason wins: a later cancel cannot
        overwrite why the run actually stopped."""
        with self._lock:
            if not self._reason:
                self._reason = reason or self._default_reason
        self._event.set()

    def cancelled(self):
        if self._event.is_set():
            return True
        if self._external is not None:
            try:
                if self._external():
                    self.cancel(self._external_reason)
                    return True
            except Exception:  # noqa: BLE001 - a broken host must not wedge the run
                return False
        return False

    @property
    def reason(self):
        with self._lock:
            return self._reason or self._default_reason

    def sleep(self, seconds, slice_seconds=0.05):
        """Sleep up to `seconds`, waking the moment the token is set. Returns
        False if it woke because of cancellation.

        Sliced rather than one long wait because an EXTERNAL flag cannot wake
        an Event: it has to be polled. A backoff that slept through a stop
        request would make the kill switch as slow as the backoff."""
        deadline = time.monotonic() + seconds
        while True:
            if self.cancelled():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            self._event.wait(timeout=min(slice_seconds, remaining))


class _TokenSink:
    """chat()'s `on_token` callback for a loop turn.

    Two jobs, both of which have to happen DURING generation to be worth
    anything: raise hearth_backend.StopStream the instant the run is
    cancelled (which hangs up on the engine and is what actually stops a
    local model producing tokens), and enforce a hard character cap so one
    runaway generation cannot outrun every ceiling the loop checks between
    turns.

    Also forwards each fragment to `on_text` so a UI can stream a loop the
    same way it streams an interactive turn.
    """

    def __init__(self, token, on_text=None, max_chars=MAX_GENERATION_CHARS):
        self._token = token
        self._on_text = on_text
        self._max_chars = max_chars
        self.chars = 0
        self.overran = False

    def __call__(self, piece):
        if self._token.cancelled():
            raise hearth_backend.StopStream("cancelled")
        self.chars += len(piece or "")
        if self._max_chars and self.chars > self._max_chars:
            self.overran = True
            raise hearth_backend.StopStream("generation exceeded the character cap")
        if self._on_text is not None:
            try:
                self._on_text(piece)
            except hearth_backend.StopStream:
                raise
            except Exception:  # noqa: BLE001 - a broken UI sink must not kill a run
                self._on_text = None


class _Workers:
    """Counter of blocking calls this run abandoned but could not kill.

    engine.py tracks the same thing on its Session for the same reason: a
    cancelled `run_command` keeps writing to the workspace long after the
    loop has stopped waiting for it, and anything about to touch that
    workspace needs to know."""

    def __init__(self):
        self._n = 0
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            self._n += 1

    def finish(self):
        with self._lock:
            self._n = max(0, self._n - 1)

    def live(self):
        with self._lock:
            return self._n


def _call_cancellable(fn, token, workers=None, poll=0.05):
    """Run fn() on a worker thread. Returns (True, value) when it finishes, or
    (False, None) as soon as the token is cancelled, whichever is first.
    Re-raises what fn() raised, but only if fn() actually finished.

    The worker is a daemon thread and is NOT killed when cancellation wins:
    Python has no safe way to kill a running thread. It keeps going until its
    blocking call unwinds and its result is discarded. That is what makes
    cancellation prompt rather than polite -- see engine.py's
    _run_cancellable, which this deliberately mirrors."""
    box = {}
    finished = threading.Event()

    def _worker():
        if workers is not None:
            workers.start()
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller
            box["error"] = exc
        finally:
            if workers is not None:
                workers.finish()
            finished.set()

    threading.Thread(target=_worker, daemon=True).start()
    while not finished.is_set():
        if token.cancelled():
            return False, None
        finished.wait(timeout=poll)
    if "error" in box:
        raise box["error"]
    return True, box.get("value")


# --------------------------------------------------------------------------
# ceilings
# --------------------------------------------------------------------------

class Ceilings:
    """Every bound on a run, in one object, each individually optional.

    Local inference costs no money, so the meaningful budgets are time, GPU
    wear and unsupervised authority -- which is why `max_writes` (a budget of
    unattended file writes) sits here alongside wall clock and tokens rather
    than in the permission layer. It is a ceiling in exactly the same sense:
    a quantity a run may spend before it has to come back and ask.
    """

    def __init__(self, max_turns=40, max_seconds=7200, max_tokens=1_000_000,
                 max_writes=200, max_tool_calls=400):
        self.max_turns = max_turns
        self.max_seconds = max_seconds
        self.max_tokens = max_tokens
        self.max_writes = max_writes
        self.max_tool_calls = max_tool_calls

    def to_dict(self):
        return {"max_turns": self.max_turns, "max_seconds": self.max_seconds,
                "max_tokens": self.max_tokens, "max_writes": self.max_writes,
                "max_tool_calls": self.max_tool_calls}

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(**{k: d[k] for k in cls().to_dict() if k in d})

    def exceeded(self, stats):
        """The first ceiling `stats` has reached, as (name, limit, value), or
        None. Order is fixed so the reported ceiling is deterministic when a
        turn crosses two at once. `stats` is what _spend() returns."""
        checks = (
            ("turns", self.max_turns, stats.get("turns", 0)),
            ("wall clock", self.max_seconds, stats.get("elapsed", 0.0)),
            ("tokens", self.max_tokens, stats.get("tokens", 0)),
            ("unattended writes", self.max_writes, stats.get("writes", 0)),
            ("tool calls", self.max_tool_calls, stats.get("tool_calls", 0)),
        )
        for name, limit, value in checks:
            if limit is not None and limit >= 0 and value >= limit:
                return name, limit, value
        return None


def _spend(report, elapsed):
    """Everything a run has spent so far, in the shape Ceilings.exceeded
    reads.

    One function with two callers on purpose: the turn loop feeds it to the
    ceiling check, and the per-turn `progress` event carries it to whoever is
    watching. A budget meter drawn from one tally while enforcement reads
    another is a meter that is eventually wrong, and it is the one the user
    can see -- so they are the same tally, by construction."""
    return {"turns": report.turns, "elapsed": elapsed, "tokens": report.tokens,
            "writes": report.writes, "tool_calls": report.tool_calls}


# --------------------------------------------------------------------------
# workspace fingerprinting
# --------------------------------------------------------------------------

def _sha16(data):
    return hashlib.sha256(data).hexdigest()[:16]


def scan_workspace(workspace, ignore_dirs=IGNORE_DIRS, max_files=MAX_SCAN_FILES,
                   hash_cap=HASH_BYTES_CAP):
    """Fingerprint every file in the workspace.

    Returns {"files": {relpath: signature}, "digest": hex, "count": n,
    "truncated": bool}. The signature is size plus a content hash for files
    up to `hash_cap`, and size plus mtime for anything larger (hashing a
    500MB model file every turn would cost more than the whole loop).

    `digest` is a hash of the whole manifest, which is what makes "has this
    workspace ever been in this exact state before" a dictionary lookup
    rather than a comparison against the previous turn only. That distinction
    is the entire reason this module can see an A-to-B-to-A oscillation."""
    files = {}
    truncated = False
    root_real = os.path.realpath(workspace)
    for root, dirs, names in os.walk(root_real):
        dirs[:] = sorted(d for d in dirs if d not in ignore_dirs)
        for name in sorted(names):
            if len(files) >= max_files:
                truncated = True
                break
            full = os.path.join(root, name)
            rel = os.path.relpath(full, root_real).replace(os.sep, "/")
            try:
                st = os.stat(full)
            except OSError:
                continue  # vanished mid-walk; simply not part of this state
            if st.st_size <= hash_cap:
                try:
                    with open(full, "rb") as fh:
                        sig = _sha16(fh.read())
                except OSError:
                    sig = "unreadable"
            else:
                sig = "big:{}".format(st.st_mtime_ns)
            files[rel] = "{}:{}".format(st.st_size, sig)
        if truncated:
            break
    blob = "\n".join("{}={}".format(k, files[k]) for k in sorted(files))
    return {"files": files, "digest": _sha16(blob.encode("utf-8")),
            "count": len(files), "truncated": truncated}


def diff_manifests(before, after):
    """{"created": [...], "modified": [...], "deleted": [...]}, sorted.

    Reported straight into the account, because "created solver.py, modified
    tests.py" is what a user can act on and a digest comparison is not."""
    b = (before or {}).get("files") or {}
    a = (after or {}).get("files") or {}
    return {
        "created": sorted(k for k in a if k not in b),
        "deleted": sorted(k for k in b if k not in a),
        "modified": sorted(k for k in a if k in b and a[k] != b[k]),
    }


# --------------------------------------------------------------------------
# error fingerprinting
# --------------------------------------------------------------------------

# Every pattern here is linear-time with no nested quantifier: these run over
# untrusted model and tool output on every turn, and this repository has
# already had one ReDoS to fix.
_RE_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_RE_HEX = re.compile(r"0[xX][0-9a-fA-F]+")
_RE_TIME = re.compile(r"\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?")
_RE_WINPATH = re.compile(r"[A-Za-z]:[\\/][^\s'\"]*")
_RE_POSIXPATH = re.compile(r"/(?:tmp|var|home|Users)/[^\s'\"]*")
_RE_NUM = re.compile(r"\d+")
_RE_WS = re.compile(r"\s+")

# A line is treated as an error line when it carries one of these. Kept as a
# plain substring list rather than a regex alternation so the cost is linear
# in the line length and obvious to read.
_ERROR_MARKERS = (
    "error", "exception", "traceback", "failed", "failure", "fatal",
    "cannot", "can't", "not found", "no such", "denied", "invalid",
    "unexpected", "assertionerror", "syntaxerror", "refused", "timed out",
    "timeout", "missing",
)
MAX_ERROR_SCAN_CHARS = 8000   # bound the input before any regex touches it
MAX_ERRORS_PER_TURN = 8


def normalise_error(line):
    """Reduce one error line to a signature stable across reruns.

    Volatile detail -- addresses, uuids, clock times, absolute paths, and
    then any remaining run of digits -- becomes a placeholder, so the same
    failure produces the same signature on turn 3 and turn 30. Line numbers
    and counts collapse too, which is deliberate: "3 tests failed" and "5
    tests failed" are the same failure SHAPE, and shape is what a stall
    detector needs. The verbatim text is kept separately for the report, so
    nothing legible is lost."""
    s = (line or "")[:1000]
    s = _RE_UUID.sub("<uuid>", s)
    s = _RE_HEX.sub("<addr>", s)
    s = _RE_TIME.sub("<time>", s)
    s = _RE_WINPATH.sub("<path>", s)
    s = _RE_POSIXPATH.sub("<path>", s)
    s = _RE_NUM.sub("<n>", s)
    s = _RE_WS.sub(" ", s).strip().lower()
    return s[:240]


def extract_errors(text, limit=MAX_ERRORS_PER_TURN):
    """Error lines in `text`, as [(signature, verbatim), ...], de-duplicated
    by signature and capped. Input is truncated before any scanning so a
    hostile or enormous tool output cannot make this expensive."""
    out = []
    seen = set()
    for raw in (text or "")[:MAX_ERROR_SCAN_CHARS].splitlines():
        line = raw.strip()
        if not line or len(line) > 600:
            continue
        low = line.lower()
        if not any(m in low for m in _ERROR_MARKERS):
            continue
        sig = normalise_error(line)
        if not sig or sig in seen:
            continue
        seen.add(sig)
        out.append((sig, line[:400]))
        if len(out) >= limit:
            break
    return out


def action_fingerprint(calls):
    """A stable fingerprint of the set of tool calls a turn made.

    Arguments are included, because "ran pytest" and "ran pytest with a
    different file" are different actions, and normalised (sorted keys,
    whitespace collapsed, truncated) so cosmetic reformatting does not read
    as a new action. Sorted and de-duplicated, because the ORDER the model
    happened to emit two independent reads in is not a difference worth
    calling progress."""
    parts = []
    for name, args in calls or ():
        try:
            body = json.dumps(args or {}, sort_keys=True, default=str)
        except (TypeError, ValueError):
            body = str(args)
        body = _RE_WS.sub(" ", body)[:300]
        parts.append("{}({})".format(name, body))
    return " | ".join(sorted(set(parts)))


# --------------------------------------------------------------------------
# progress
# --------------------------------------------------------------------------

class StallPolicy:
    """Thresholds for the four stall detectors. Separate from Ceilings
    because a ceiling is a budget the user chose to spend and a stall is a
    judgement about whether spending it is achieving anything.

    The defaults are the `balanced` preset below, measured rather than
    guessed. _self_test asserts the two cannot drift apart."""

    def __init__(self, window=8, repeat_actions=4, repeat_errors=8,
                 oscillations=3, min_turns=3):
        self.window = window                  # turns without a NEW workspace state
        self.repeat_actions = repeat_actions  # consecutive identical action sets
        self.repeat_errors = repeat_errors    # consecutive turns with one error
        self.oscillations = oscillations      # revisits to seen states in the window
        self.min_turns = min_turns            # never judge before this many turns

    def to_dict(self):
        return {"window": self.window, "repeat_actions": self.repeat_actions,
                "repeat_errors": self.repeat_errors,
                "oscillations": self.oscillations, "min_turns": self.min_turns}

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(**{k: d[k] for k in cls().to_dict() if k in d})


# --------------------------------------------------------------------------
# patience, as one choice instead of five numbers
# --------------------------------------------------------------------------

# WHY THESE NUMBERS ARE WHAT THEY ARE
# ===================================
#
# The first shipped defaults (window=6, repeat_actions=4, repeat_errors=5,
# oscillations=3, min_turns=3) were reasoned about, not measured, and a
# benchmark caught them: across 8 unattended runs on this machine's 7B, ALL 8
# stopped `stalled` at a mean of 5.5 turns out of 18 allowed, and the same loop
# with every detector switched off solved a task the default settings never
# solved. The loop was giving up less than a third of the way into a budget the
# user had already granted.
#
# The mechanism turned out to be specific and worth naming, because it is not
# obvious from the thresholds alone: a failing completion check contributes its
# own output to the turn's errors on EVERY turn (see run_workloop, which feeds
# the gate's output into turn_errors on purpose so the account can say "the
# same 3 tests failed each time"). While a run is working toward a gate it has
# not passed yet, that gate fails the same way every turn. So repeat_errors=5
# was not "the model is stuck repeating itself" -- it was a hard cap of five
# turns on any run whose tests do not go green almost immediately. Steady,
# genuine, file-changing progress tripped it.
#
# The retuned numbers come from replaying recorded runs through the real
# detectors (scripts/loop_bench.py, which explains why replay is exact rather
# than approximate). Both directions were measured, because both cost the user
# real time and electricity: whether a solvable task still gets solved, and how
# many turns a hopeless one burns before it is stopped.
#
# WHAT A MORE PATIENT SETTING ACTUALLY COSTS
# ==========================================
#
# It is not free, and the `cost` line of each preset says so in the place the
# choice is made rather than only in the docs. Patience buys the runs that
# needed a few more turns; it pays for them with the runs that were never going
# to work and now grind on longer. `keep_trying` genuinely can spend the entire
# budget on a task that cannot be done. That is the user's call to make, and
# they can only make it if it is written where they choose.
#
# Ceilings remain the real bound in every preset. Nothing here can widen one:
# a preset holds stall thresholds and nothing else, which _self_test asserts
# structurally rather than trusting.
PATIENCE_SETTINGS = (
    {
        "name": "give_up_early",
        "label": "Give up early",
        "summary": "Stop at the first sign of repetition. Cheapest when a run "
                   "is not going to work, and the most likely to stop one that "
                   "would have.",
        "cost": "Measured on this machine's 7B against an 18-turn budget: it ends "
                "a run that cannot succeed after about 6 turns, and it also "
                "ended runs that went on to pass their tests when they were "
                "allowed to continue. Pick it when you would rather be told "
                "quickly and are paying for the electricity.",
        "settings": {"window": 4, "repeat_actions": 3, "repeat_errors": 4,
                     "oscillations": 2, "min_turns": 2},
    },
    {
        "name": "balanced",
        "label": "Balanced",
        "summary": "Keep working while the workspace keeps changing, and stop "
                   "once it is clearly going in circles. This is the default.",
        "cost": "A run that cannot succeed still spends about 10 turns of an "
                "18-turn budget before this notices. That is the price of not "
                "abandoning the runs that were about to finish, and in the same "
                "measurement it kept every one of those.",
        "settings": {"window": 8, "repeat_actions": 4, "repeat_errors": 8,
                     "oscillations": 3, "min_turns": 3},
    },
    {
        "name": "keep_trying",
        "label": "Keep trying",
        "summary": "Only stop when the workspace is provably frozen: nothing "
                   "new for a long stretch, or the identical tool call over and "
                   "over. Repeated errors alone will not end the run.",
        "cost": "A run that cannot succeed will spend almost all of its turns, "
                "tokens and wall clock before stopping: about 16 turns of an "
                "18-turn budget in the same measurement, and the whole budget "
                "when the completion check fails without printing anything "
                "error-shaped. Choose this when the ceilings you set ARE the "
                "budget you are willing to lose.",
        "settings": {"window": 16, "repeat_actions": 8, "repeat_errors": 0,
                     "oscillations": 0, "min_turns": 6},
    },
)
PATIENCE_ORDER = tuple(p["name"] for p in PATIENCE_SETTINGS)
DEFAULT_PATIENCE = "balanced"
#: What patience_of() returns for thresholds that match no preset. Not a
#: choosable setting: a caller may always type its own five numbers, and the
#: account has to be able to say honestly that it did.
CUSTOM_PATIENCE = "custom"


def stall_policy_for(name):
    """The StallPolicy for a named patience setting. Unknown names are refused
    rather than quietly defaulted: a run that silently ignores the setting a
    user picked is worse than one that will not start."""
    for preset in PATIENCE_SETTINGS:
        if preset["name"] == name:
            return StallPolicy(**preset["settings"])
    raise ValueError("unknown patience setting {!r}; choose one of {}".format(
        name, ", ".join(PATIENCE_ORDER)))


def patience_of(policy):
    """The name of the preset `policy` matches, or "custom".

    Derived from the numbers rather than remembered alongside them, so a
    config that arrived over HTTP with hand-edited thresholds can never be
    labelled with a preset it does not actually match."""
    got = policy.to_dict() if hasattr(policy, "to_dict") else dict(policy or {})
    for preset in PATIENCE_SETTINGS:
        if preset["settings"] == got:
            return preset["name"]
    return CUSTOM_PATIENCE


def patience_choices():
    """The preset list as plain data, for a UI to render.

    Ships whole -- label, summary and the cost of choosing it -- for the same
    reason PROGRESS_BLIND_SPOTS does: the person who most needs to know what
    patience costs is the one about to leave a run going unattended, and they
    will never read this file."""
    return [{"name": p["name"], "label": p["label"], "summary": p["summary"],
             "cost": p["cost"], "settings": dict(p["settings"])}
            for p in PATIENCE_SETTINGS]


class ProgressLedger:
    """Whether the run is changing anything it has not already changed.

    FOUR DETECTORS, each reported by name so the account can say which one
    fired rather than just "stalled":

      no_new_state   No turn in the last `window` turns left the workspace in
                     a state the run had not already been in. This is the
                     primary detector and the one that catches the classic
                     failure: rerunning a failing test forever, or thinking
                     in circles without touching anything.
      oscillation    The workspace RETURNED to an already-seen state at least
                     `oscillations` times inside the window. Catches A-to-B-
                     to-A thrash, which a naive "did anything change since
                     last turn" check reports as progress every single turn.
      repeat_action  The identical set of tool calls, arguments included, for
                     `repeat_actions` consecutive turns. Catches a model
                     banging on the same locked door -- including the door
                     this module locks itself, when gate_policy denies a tool.
      repeat_error   The same normalised error in `repeat_errors` consecutive
                     turns.

    WHAT IT GENUINELY CANNOT SEE, and no amount of tuning will fix:

      * Work outside the workspace. A run whose real effect is on a database,
        a remote service, or anywhere off this disk is invisible to the
        manifest, and genuine progress will read as no_new_state. Give such a
        run a `done_command` that can observe the real effect, or widen the
        workspace.
      * Forward motion toward a wrong answer. Every detector here measures
        CHANGE, not correctness. A model steadily writing a beautiful,
        confident, completely incorrect solution produces a new state every
        turn and will never trip a single detector. Only the completion check
        can catch that, which is exactly why the completion check is
        deterministic and not the model's own opinion.
      * Growth without value. A model appending one more junk line to a file
        each turn changes the digest each turn and reads as progress.
      * Work below the fingerprint's resolution. Files inside IGNORE_DIRS, or
        the content of files above HASH_BYTES_CAP whose mtime does not move,
        do not register.
      * Anything at all before `min_turns`. A slow start is not a stall, and
        the ledger deliberately refuses to judge one.

    So a stalled verdict is strong evidence of a stuck run, and an unstalled
    verdict is NOT evidence of a useful one. The report says so too.
    """

    def __init__(self, policy=None):
        self.policy = policy or StallPolicy()
        self.turns = []           # one record per completed turn
        self._seen = {}           # workspace digest -> turn it first appeared
        self.error_counts = {}    # signature -> total occurrences
        self.error_text = {}      # signature -> most recent verbatim line
        self.last_error = ""

    def record(self, turn_no, digest, actions, errors, note=""):
        """Add one completed turn. `errors` is extract_errors' output.
        Returns the record, which includes whether this turn reached a
        workspace state the run had never been in."""
        fresh = digest not in self._seen
        if fresh:
            self._seen[digest] = turn_no
        # Whether this turn MOVED the workspace at all, as opposed to leaving
        # it exactly where the previous turn did. This is what separates
        # oscillation (moved, but back to somewhere it has already been) from
        # a workspace that is simply frozen (never moved), which is
        # no_new_state's job. Without it a static tree reports as thrash, and
        # the account names the wrong problem.
        prev_digest = self.turns[-1]["digest"] if self.turns else None
        moved = prev_digest is not None and digest != prev_digest
        for sig, verbatim in errors or ():
            self.error_counts[sig] = self.error_counts.get(sig, 0) + 1
            self.error_text[sig] = verbatim
            self.last_error = verbatim
        rec = {
            "turn": turn_no,
            "digest": digest,
            "new_state": fresh,
            "moved": moved,
            "first_seen_at": self._seen.get(digest),
            "actions": action_fingerprint(actions),
            "action_count": len(actions or ()),
            "errors": [s for s, _ in (errors or ())],
            "note": note,
        }
        self.turns.append(rec)
        return rec

    def _window(self):
        return self.turns[-self.policy.window:] if self.policy.window else self.turns

    def _error_streak(self):
        """(streak, signature) for the error present in the most consecutive
        turns at the end of the run.

        Deliberately NOT "each turn's first error": a turn's error list is
        ordered by where the text happened to appear, so one extra
        SyntaxError arriving at the top of the output would reset a streak
        that is otherwise unbroken. Measured on a real run against a
        deliberately unsatisfiable check, comparing first-errors took 26
        turns to notice what comparing signatures notices in 6 -- the same
        three assertions had failed every single turn from the start, which
        is exactly the thing this detector exists to say out loud."""
        best = (0, "")
        if not self.turns:
            return best
        for sig in self.turns[-1]["errors"]:
            n = 0
            for rec in reversed(self.turns):
                if sig not in rec["errors"]:
                    break
                n += 1
            if n > best[0]:
                best = (n, sig)
        return best

    def _consecutive_tail(self, key_fn):
        """How many turns at the end of the run share a non-empty key."""
        n = 0
        last = None
        for rec in reversed(self.turns):
            key = key_fn(rec)
            if not key:
                break
            if last is None:
                last = key
            elif key != last:
                break
            n += 1
        return n, last

    def verdict(self):
        """{"stalled": bool, "detectors": [...], "reason": str, "signals": {}}.

        `detectors` lists every detector that fired, each with its own
        sentence, so the report can print all of them instead of only the
        first. A run is stalled if ANY fired."""
        pol = self.policy
        fired = []
        signals = {
            "turns": len(self.turns),
            "distinct_states": len(self._seen),
            "distinct_errors": len(self.error_counts),
        }
        if len(self.turns) < max(1, pol.min_turns):
            return {"stalled": False, "detectors": [],
                    "reason": "too early to judge ({} turn(s) so far)".format(len(self.turns)),
                    "signals": signals}

        window = self._window()
        signals["window"] = len(window)

        # 1. no_new_state
        if len(self.turns) >= pol.window > 0:
            if not any(r["new_state"] for r in window):
                first, last = window[0]["turn"], window[-1]["turn"]
                fired.append({
                    "name": "no_new_state",
                    "detail": ("no turn since turn {} left the workspace in a state "
                               "this run had not already been in (turns {}-{})".format(
                                   window[0]["first_seen_at"] or first, first, last)),
                })

        # 2. oscillation. Only a turn that actually MOVED the workspace and
        # landed somewhere it had already been counts: a frozen tree is
        # no_new_state's finding, not this one. Expressed as one filtered list
        # rather than the same condition written twice, so there is exactly
        # one place for that distinction to be right.
        revisit_rows = [r for r in window
                        if r.get("moved") and not r["new_state"]
                        and r["first_seen_at"] is not None]
        revisits = len(revisit_rows)
        signals["revisits_in_window"] = revisits
        if (pol.oscillations > 0 and revisits >= pol.oscillations
                and len(window) >= pol.oscillations):
            fired.append({
                "name": "oscillation",
                "detail": ("the workspace returned to an already-seen state {} times "
                           "in the last {} turns (back to turn {})".format(
                               revisits, len(window), revisit_rows[-1]["first_seen_at"])),
            })

        # 3. repeat_action
        n_act, act = self._consecutive_tail(lambda r: r["actions"])
        signals["repeated_actions"] = n_act
        if pol.repeat_actions > 0 and n_act >= pol.repeat_actions:
            fired.append({
                "name": "repeat_action",
                "detail": ("the identical action was repeated in the last {} turns: "
                           "{}".format(n_act, (act or "")[:200])),
            })

        # 4. repeat_error
        n_err, sig = self._error_streak()
        signals["repeated_error_turns"] = n_err
        if pol.repeat_errors > 0 and n_err >= pol.repeat_errors:
            fired.append({
                "name": "repeat_error",
                "detail": ("the same error recurred in the last {} turns ({} times in "
                           "total): {}".format(n_err, self.error_counts.get(sig, n_err),
                                               (self.error_text.get(sig) or sig)[:200])),
            })

        stalled = bool(fired)
        reason = "; ".join(f["detail"] for f in fired) if stalled else "making progress"
        return {"stalled": stalled, "detectors": fired, "reason": reason,
                "signals": signals}

    def recurring_errors(self, limit=5):
        """[(count, verbatim), ...] most frequent first, for the report."""
        rows = sorted(self.error_counts.items(), key=lambda kv: -kv[1])[:limit]
        return [(n, self.error_text.get(sig, sig)) for sig, n in rows]


# --------------------------------------------------------------------------
# journal
# --------------------------------------------------------------------------

def journal_dir():
    return os.path.join(hearth_paths.data_dir(), "loops")


def journal_path(run_id):
    return os.path.join(journal_dir(), "{}.jsonl".format(run_id))


class Journal:
    """Append-only JSONL record of a run, durable enough to survive the
    process dying at any point.

    Each record is written and flushed and fsynced before the work it
    describes is allowed to proceed, which is what makes a "turn start" with
    no matching "turn end" an unambiguous signature of a crash mid-turn
    rather than a race with the writer. See load() and the module docstring's
    CRASH SURVIVAL section for what is then done about it: nothing is
    resumed."""

    def __init__(self, path, fsync=True):
        self.path = path
        self._fsync = fsync
        self._lock = threading.Lock()
        parent = os.path.dirname(path) or "."
        os.makedirs(parent, exist_ok=True)

    def append(self, record):
        line = json.dumps(record, default=str)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                if self._fsync:
                    try:
                        os.fsync(fh.fileno())
                    except OSError:
                        pass  # a filesystem that will not fsync is still worth writing to
        return record

    def read(self):
        """Every well-formed record, in order. A truncated or corrupt final
        line (the classic result of losing power mid-write) is dropped rather
        than failing the whole load."""
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
        return out


def load_journal(run_id, path=None):
    """Rebuild what is safely known about a previous run.

    Returns {"header":..., "completed_turns":[...], "interrupted_turn": int|None,
    "messages": [...], "stopped": record|None}. The interrupted turn is a turn
    that started and never ended: the process died inside it. It is reported
    so it can be recorded honestly, and it is NEVER handed back as something
    to continue -- there is no way to know whether its last tool call reached
    the workspace."""
    jr = Journal(path or journal_path(run_id), fsync=False)
    records = jr.read()
    header = None
    completed = []
    started = {}
    messages = []
    stopped = None
    for rec in records:
        kind = rec.get("t")
        if kind == "run" and header is None:
            header = rec
        elif kind == "turn_start":
            started[rec.get("turn")] = rec
        elif kind == "turn_end":
            n = rec.get("turn")
            started.pop(n, None)
            completed.append(rec)
            if isinstance(rec.get("messages"), list):
                messages = rec["messages"]
        elif kind == "stop":
            stopped = rec
    interrupted = min(started) if started else None
    return {"header": header, "completed_turns": completed,
            "interrupted_turn": interrupted, "messages": messages,
            "stopped": stopped, "records": records}


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def _hms(seconds):
    seconds = int(max(0, seconds))
    return "{}:{:02d}:{:02d}".format(seconds // 3600, (seconds % 3600) // 60, seconds % 60)


class LoopReport:
    """What the run did, what it stopped for, and the evidence for both.

    render() is the thing a user actually reads. The bar it is written to is
    the one the brief set: "Stopped: no progress in 6 turns; the same 3 tests
    failed each time; last error was X" -- a sentence that names the reason,
    quantifies it, and quotes the evidence."""

    def __init__(self, goal, model, workspace, mode, run_id, ceilings, stall_policy):
        self.goal = goal
        self.model = model
        self.workspace = workspace
        self.mode = mode
        self.run_id = run_id
        self.ceilings = ceilings
        self.stall_policy = stall_policy
        # Which named patience setting this run actually ran under, derived
        # from the thresholds themselves. The account says it out loud because
        # "stopped making progress" means something different at each setting,
        # and a reader who does not know which one was in force cannot weigh
        # the verdict they are being given.
        self.patience = patience_of(stall_policy) if stall_policy else CUSTOM_PATIENCE
        self.stop_reason = None
        self.stop_detail = ""
        self.turns = 0
        self.elapsed = 0.0
        self.tokens_in = 0
        self.tokens_out = 0
        self.writes = 0
        self.tool_calls = 0
        self.gates_denied = 0
        self.denied_tools = {}
        self.checkpoints = []
        self.checkpoint_error = ""
        self.turn_log = []          # per-turn dicts for the account
        self.verdict = {}           # last ProgressLedger.verdict()
        self.recurring = []         # [(count, verbatim)]
        self.last_error = ""
        self.created = []
        self.modified = []
        self.deleted = []
        self.completion = {}        # what the deterministic check saw
        self.notices = []
        self.live_workers = 0

    @property
    def tokens(self):
        return self.tokens_in + self.tokens_out

    def notice(self, text):
        self.notices.append(text)

    def to_dict(self):
        return {
            "run_id": self.run_id, "goal": self.goal, "model": self.model,
            "workspace": self.workspace, "mode": self.mode,
            "stop_reason": self.stop_reason, "stop_detail": self.stop_detail,
            "turns": self.turns, "elapsed": round(self.elapsed, 2),
            "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
            "tokens": self.tokens, "writes": self.writes,
            "tool_calls": self.tool_calls, "gates_denied": self.gates_denied,
            "denied_tools": self.denied_tools,
            "checkpoints": self.checkpoints,
            "checkpoint_error": self.checkpoint_error,
            "ceilings": self.ceilings.to_dict() if self.ceilings else {},
            "stall_policy": self.stall_policy.to_dict() if self.stall_policy else {},
            "patience": self.patience,
            "verdict": self.verdict, "recurring_errors": self.recurring,
            "last_error": self.last_error, "completion": self.completion,
            "created": self.created, "modified": self.modified,
            "deleted": self.deleted, "turn_log": self.turn_log,
            "notices": self.notices, "live_workers": self.live_workers,
        }

    def headline(self):
        """One sentence: what happened."""
        r = self.stop_reason
        if r == STOP_COMPLETED:
            return "finished the goal in {} turn(s)".format(self.turns)
        if r == STOP_CEILING:
            return "stopped at a ceiling: {}".format(self.stop_detail)
        if r == STOP_STALLED:
            return "stopped making progress: {}".format(self.stop_detail)
        if r == STOP_CANCELLED:
            return "stopped by you ({})".format(self.stop_detail or "cancelled")
        if r == STOP_BLOCKED:
            return "stopped needing permission: {}".format(self.stop_detail)
        if r == STOP_ERROR:
            return "stopped on an error: {}".format(self.stop_detail)
        return "still running"

    def render(self):
        L = []
        L.append("hearth work loop -- {}".format(self.headline()))
        L.append("")
        L.append("goal       {}".format(self.goal))
        L.append("workspace  {}".format(self.workspace))
        L.append("model      {}".format(self.model))
        L.append("mode       {} (bypass is not reachable from a loop)".format(self.mode))
        L.append("patience   {}{}".format(
            self.patience,
            "" if self.stall_policy is None else "  ({})".format(", ".join(
                "{}={}".format(k, v) for k, v in
                sorted(self.stall_policy.to_dict().items())))))
        L.append("run        {}".format(self.run_id))
        L.append("")

        L.append("why it stopped")
        L.append("  {}".format(self.headline()))
        if self.stop_reason == STOP_STALLED:
            for det in (self.verdict.get("detectors") or []):
                L.append("  [{}] {}".format(det["name"], det["detail"]))
            # A stall is a JUDGEMENT, made at a threshold this user chose, and
            # the honest thing is to say that the judgement was tunable rather
            # than present it as a fact about the work. The run below the most
            # patient setting had turns left; whether they were worth spending
            # is not something this module can know.
            if self.patience != PATIENCE_ORDER[-1] and self.ceilings is not None \
                    and self.turns < (self.ceilings.max_turns or 0):
                L.append("  this judgement was made at the {!r} patience setting, with "
                         "{} of {} turns unspent. A more patient setting would have "
                         "kept going.".format(
                             self.patience, self.ceilings.max_turns - self.turns,
                             self.ceilings.max_turns))
        if self.completion:
            ok = self.completion.get("done")
            L.append("  completion check: {}".format(
                self.completion.get("detail")
                or ("passed" if ok else "not satisfied")))
            # The check's own output is the single most useful fact when a run
            # did not finish, and leaving it out was a real defect: a run once
            # reported "completion command exited 1" while the actual answer,
            # a SyntaxError on line 1, appeared nowhere in the account.
            out = (self.completion.get("output") or "").strip()
            if out and not ok:
                L.append("  what the completion check said:")
                for line in out.splitlines()[-12:]:
                    L.append("    {}".format(line[:200]))
        if self.recurring:
            L.append("  errors seen more than once:")
            for count, text in self.recurring:
                L.append("    {}x  {}".format(count, text[:180]))
        if self.last_error:
            L.append("  last error (verbatim):")
            L.append("    {}".format(self.last_error[:400]))
        L.append("")

        L.append("what it spent")
        c = self.ceilings
        L.append("  turns    {:>10} of {}".format(self.turns, c.max_turns if c else "-"))
        L.append("  elapsed  {:>10} of {}".format(
            _hms(self.elapsed), _hms(c.max_seconds) if c else "-"))
        L.append("  tokens   {:>10,} of {:,}  (in {:,} / out {:,})".format(
            self.tokens, c.max_tokens if c else 0, self.tokens_in, self.tokens_out))
        L.append("  writes   {:>10} of {}   (unattended file writes)".format(
            self.writes, c.max_writes if c else "-"))
        L.append("  tools    {:>10} of {}".format(
            self.tool_calls, c.max_tool_calls if c else "-"))
        if self.gates_denied:
            L.append("  refused  {:>10}   (needed approval nobody was there to give: {})".format(
                self.gates_denied,
                ", ".join("{} x{}".format(k, v) for k, v in sorted(self.denied_tools.items()))))
        L.append("")

        L.append("what changed in the workspace")
        if not (self.created or self.modified or self.deleted):
            L.append("  nothing")
        for label, items in (("created", self.created), ("modified", self.modified),
                             ("deleted", self.deleted)):
            if items:
                shown = ", ".join(items[:12])
                more = "" if len(items) <= 12 else " (+{} more)".format(len(items) - 12)
                L.append("  {:9} {}{}".format(label, shown, more))
        if self.checkpoints:
            L.append("  {} checkpoint(s), latest {}".format(
                len(self.checkpoints), self.checkpoints[-1][:12]))
            L.append("  undo any of them with hearth_checkpoint.restore()")
        if self.checkpoint_error:
            L.append("  checkpoints UNAVAILABLE: {}".format(self.checkpoint_error))
        L.append("")

        if self.turn_log:
            L.append("turn by turn")
            for t in self.turn_log:
                L.append("  {:>3}  {:<11} {:<28} {}".format(
                    t.get("turn"),
                    "new state" if t.get("new_state") else "seen before",
                    (t.get("summary") or "")[:28],
                    "{} err".format(len(t.get("errors") or []))))
            L.append("")

        if self.notices:
            L.append("notes")
            for n in self.notices:
                L.append("  {}".format(n))
            L.append("")

        if self.live_workers:
            L.append("WARNING: {} abandoned tool call(s) may still be running against "
                     "this workspace. Cancellation stops the loop waiting; it cannot "
                     "kill a call already in flight.".format(self.live_workers))
            L.append("")

        # ALWAYS, including on a completed run. "It finished" is exactly the
        # verdict a reader is most likely to over-trust, and if no completion
        # check ran then "finished" was the model's own opinion about its own
        # work. Printing the limits only on failure would put the caveat
        # everywhere except the one place it changes what someone believes.
        L.append("what this account cannot tell you")
        if self.completion.get("done") and self.completion.get("verified") is False:
            L.append("  NOTHING VERIFIED THIS. No completion check was configured, so "
                     "'finished' here is the model's own claim about its own work.")
        for headline, means, todo in PROGRESS_BLIND_SPOTS:
            L.append("  * {}".format(headline))
            L.append("      {}".format(means))
            L.append("      {}".format(todo))
        return "\n".join(L)


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------

LOOP_SYSTEM = (
    "You are Hearth working UNATTENDED on one goal, across many turns, on the "
    "user's own machine. Nobody is watching this run.\n"
    "\n"
    "- Work in small concrete steps and actually use your tools. A turn that "
    "only talks achieves nothing.\n"
    "- You have a limited set of tools. Anything dangerous (running commands, "
    "reaching the network) may be refused because no one is here to approve it. "
    "If a tool is refused, do NOT try it again -- find another way or say you "
    "are blocked.\n"
    "- Your progress is measured by what actually changes in the workspace. "
    "Repeating the same action, or leaving the workspace untouched, will end "
    "the run.\n"
    "- When the goal is genuinely and completely achieved, reply with the "
    "single line GOAL COMPLETE and nothing else. Do not say it early: it is "
    "checked against the workspace."
)
DONE_SENTINEL = "GOAL COMPLETE"


def _chat_via_hearth_loop(ollama_url, model, messages, tools, on_token):
    """The run's real chat call, as a named function rather than a lambda.

    hearth_loop.chat returns a 3-TUPLE and that arity is load bearing: a
    previous change to it broke every consumer while every self-test still
    passed, because they all inject a mock chat_fn and a mock cannot catch an
    arity break at a real call site. hearth_loop._self_test therefore walks
    this repository's syntax tree and checks each REAL call site unpacks
    exactly three values -- which it can only do if the call site is a plain
    unpacking assignment. Keeping this out of the default-argument lambda
    below is what puts this module under that guard."""
    msg, tokens_in, tokens_out = hearth_loop.chat(
        ollama_url, model, messages, tools, on_token=on_token)
    return msg, tokens_in, tokens_out


def _trim_messages(messages, keep=24, max_content=6000):
    """Keep the system prompt, the original goal, and the last `keep`
    messages, with each body capped. A run of hundreds of turns must not grow
    its own context until the engine refuses it."""
    if len(messages) <= keep + 2:
        head, tail = messages[:0], messages
    else:
        head, tail = messages[:2], messages[-keep:]
    out = []
    for m in list(head) + list(tail):
        c = m.get("content")
        if isinstance(c, str) and len(c) > max_content:
            m = dict(m)
            m["content"] = c[:max_content] + "\n... (truncated)"
        out.append(m)
    return out


def _run_done_command(command, workspace, token, timeout, workers, runner=None):
    """Run the user's completion command. Exit 0 means the goal is done.

    This does NOT go through permissions: it is not a model-chosen action, it
    is the gate the USER supplied when starting the run, exactly as nix_check
    is hearth_evolve's gate. It is bounded by its own timeout and abandoned
    on cancellation like any other blocking call.

    Returns {"done": bool, "detail": str, "available": bool}. A command that
    could not be run at all reports available=False, so the report can say
    "the completion check itself is broken" rather than blaming the model --
    which is failure mode 4 from the module docstring."""
    def _default():
        return subprocess.run(command, shell=True, cwd=workspace, capture_output=True,
                              text=True, timeout=timeout)
    try:
        finished, proc = _call_cancellable(runner or _default, token, workers)
    except subprocess.TimeoutExpired:
        return {"done": False, "available": True,
                "detail": "completion check timed out after {}s".format(timeout)}
    except Exception as exc:  # noqa: BLE001
        return {"done": False, "available": False,
                "detail": "completion check could not run: {}: {}".format(
                    type(exc).__name__, exc)}
    if not finished:
        return {"done": False, "available": True, "detail": "completion check cancelled"}
    rc = getattr(proc, "returncode", 1)
    tail = ((getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")).strip()
    return {"done": rc == 0, "available": True, "returncode": rc,
            "output": tail[-2000:],
            "detail": ("completion command exited 0" if rc == 0
                       else "completion command exited {}".format(rc))}


def _missing_artifacts(required, workspace):
    """Artifacts that must exist and be non-empty but do not.

    Every name is resolved through the workspace containment check, never
    joined by hand. The list arrives from HTTP config and the config parser
    already refuses a rooted path outright (hearth_contain.is_rooted); this is
    the second layer, so that a caller reaching run_workloop directly cannot
    turn a completion check into a probe for which files exist elsewhere on
    the machine. The hand-rolled join this replaces branched on
    os.path.isabs, whose answer for "/etc/passwd" changed between Python 3.12
    and 3.13, so the same name was checked in two different places depending
    on the interpreter.

    A name that will not resolve inside the workspace counts as missing,
    which is the honest answer: no artifact the loop is allowed to see is
    there, so the loop is not done.
    """
    missing = []
    for name in required or ():
        try:
            p = hearth_contain.safe_join(workspace, name)
        except ValueError:
            missing.append(name)
            continue
        try:
            if os.path.getsize(p) > 0:
                continue
        except OSError:
            pass
        missing.append(name)
    return missing


def run_workloop(goal, model, workspace, *,
                 mode="auto", allowed_tools=None, auto_allow=(),
                 gate_policy="deny", approve_fn=None,
                 ceilings=None, stall=None,
                 done_command=None, done_timeout=300, required_artifacts=None,
                 ollama_url=DEFAULT_OLLAMA, token=None, emit=None,
                 run_id=None, journal=None, resume=False,
                 chat_fn=None, execute_tool_fn=None, checkpoint_fn=None,
                 scan_fn=None, done_runner=None, clock=None,
                 checkpoint_every_turn=True, workers=None):
    """Work at `goal` until it is done, a ceiling is hit, progress stops, or
    the run is cancelled. Always returns a LoopReport; never raises for an
    expected outcome.

    Every collaborator is injectable (chat_fn, execute_tool_fn,
    checkpoint_fn, scan_fn, done_runner, clock) so the self-test can drive
    the whole machine deterministically without an engine, a git store, or a
    real clock. The DEFAULTS are the real thing.

    `workers` is the abandoned-call counter (see _Workers). It is injectable
    for one reason: a caller that already owns such a counter for the same
    workspace must be able to hand it ITS counter rather than have this run
    keep a second, private tally. desktop/server/loop_engine.py does exactly
    that, so a `run_command` this loop abandoned keeps
    Session.is_workspace_busy() true -- which is what stops POST /restore and
    a replacement session from acting on a directory a live worker is still
    writing to. Two counters would mean the one being consulted is not the
    one being incremented.
    """
    clock = clock or time.monotonic
    token = token or CancelToken()
    emit = emit or (lambda ev: None)
    workers = workers if workers is not None else _Workers()
    started = clock()

    if mode not in ALLOWED_MODES:
        # bypass lands here. Not an exception the caller can talk its way out
        # of with a flag: an unattended loop running every tool with no gate
        # is the thing this refusal exists to make unreachable.
        raise ValueError(
            "permission mode {!r} is not available to a work loop; choose one of {}. "
            "bypass is refused outright: an unattended run must not have an "
            "ungated tool.".format(mode, ", ".join(ALLOWED_MODES)))
    if gate_policy not in GATE_POLICIES:
        raise ValueError("gate_policy must be one of {}".format(", ".join(GATE_POLICIES)))

    manifest = frozenset(allowed_tools) if allowed_tools is not None else DEFAULT_ALLOWED_TOOLS
    ceilings = ceilings or Ceilings()
    policy = stall or StallPolicy()
    run_id = run_id or ("loop-" + uuid.uuid4().hex[:12])
    workspace = os.path.realpath(workspace)
    os.makedirs(workspace, exist_ok=True)

    chat_fn = chat_fn or (lambda messages, tools, on_token: _chat_via_hearth_loop(
        ollama_url, model, messages, tools, on_token))
    execute_tool_fn = execute_tool_fn or hearth_tools.execute_tool
    scan_fn = scan_fn or scan_workspace
    if checkpoint_fn is None and checkpoint_every_turn:
        checkpoint_fn = hearth_checkpoint.checkpoint

    report = LoopReport(goal, model, workspace, mode, run_id, ceilings, policy)
    ledger = ProgressLedger(policy)

    if journal is None:
        journal = Journal(journal_path(run_id))
    baseline = scan_fn(workspace)
    start_turn = 1
    messages = None

    # ---- resume ----------------------------------------------------------
    if resume:
        prior = load_journal(run_id, getattr(journal, "path", None))
        head = prior.get("header") or {}
        prior_mode = head.get("mode")
        if prior_mode is not None and prior_mode not in ALLOWED_MODES:
            # The same refusal session_state.restore_session makes: a mode is
            # never honoured just because a file on disk says so. A journal is
            # a file the agent's own tools could have written.
            raise ValueError(
                "refusing to resume run {}: its journal says mode {!r}".format(
                    run_id, prior_mode))
        if prior.get("completed_turns"):
            last = prior["completed_turns"][-1]
            start_turn = int(last.get("turn", 0)) + 1
            messages = prior.get("messages") or None
            if last.get("baseline"):
                baseline = {"files": last["baseline"], "digest": last.get("digest", ""),
                            "count": len(last["baseline"]), "truncated": False}
            for rec in prior["completed_turns"]:
                ledger.record(rec.get("turn"), rec.get("digest", ""),
                              rec.get("actions_raw") or (),
                              [(s, rec.get("error_text", {}).get(s, s))
                               for s in (rec.get("errors") or ())],
                              note="restored")
                report.turn_log.append(rec.get("log") or {"turn": rec.get("turn")})
                report.tokens_in += int(rec.get("tokens_in") or 0)
                report.tokens_out += int(rec.get("tokens_out") or 0)
                report.writes += int(rec.get("writes") or 0)
                report.tool_calls += int(rec.get("tool_calls") or 0)
                if rec.get("checkpoint"):
                    report.checkpoints.append(rec["checkpoint"])
            report.turns = start_turn - 1
        interrupted = prior.get("interrupted_turn")
        if interrupted is not None:
            # Never resumed. There is no way to know whether its last tool
            # call reached the workspace, and pretending to know is the one
            # unacceptable option.
            journal.append({"t": "interrupted", "turn": interrupted, "ts": time.time()})
            report.notice(
                "turn {} was interrupted by a restart and was NOT resumed; its last "
                "tool call may or may not have completed. The workspace was "
                "re-fingerprinted before continuing.".format(interrupted))
            emit({"type": "notice", "detail": "turn {} interrupted by a restart".format(interrupted)})
        now = scan_fn(workspace)
        if prior.get("completed_turns") and now["digest"] != (
                prior["completed_turns"][-1].get("digest")):
            report.notice("the workspace changed while the loop was not running; "
                          "progress detection was re-based on what is there now.")
        ledger.record(start_turn - 1 if start_turn > 1 else 0, now["digest"], (), (),
                      note="post-restart re-base")
    else:
        journal.append({
            "t": "run", "version": JOURNAL_VERSION, "run_id": run_id, "goal": goal,
            "model": model, "workspace": workspace, "mode": mode,
            "allowed_tools": sorted(manifest), "gate_policy": gate_policy,
            "ceilings": ceilings.to_dict(), "stall": policy.to_dict(),
            "patience": patience_of(policy),
            "done_command": done_command, "ts": time.time(),
        })

    if messages is None:
        messages = [
            {"role": "system", "content": LOOP_SYSTEM},
            {"role": "user", "content": "GOAL: {}".format(goal)},
        ]
    tool_specs = hearth_tools.ollama_tool_specs(allowed=manifest)

    emit({"type": "loop_start", "run_id": run_id, "goal": goal, "model": model,
          "mode": mode, "ceilings": ceilings.to_dict(),
          "allowed_tools": sorted(manifest)})

    prev_manifest = baseline
    stop_reason = None
    stop_detail = ""

    def _finish(reason, detail):
        report.stop_reason = reason
        report.stop_detail = detail
        report.elapsed = clock() - started
        report.verdict = ledger.verdict()
        report.recurring = ledger.recurring_errors()
        report.last_error = ledger.last_error
        final = scan_fn(workspace)
        d = diff_manifests(baseline, final)
        report.created, report.modified, report.deleted = (
            d["created"], d["modified"], d["deleted"])
        report.live_workers = workers.live()
        try:
            journal.append({"t": "stop", "reason": reason, "detail": detail,
                            "ts": time.time(), "summary": report.to_dict()})
        except OSError:
            pass
        emit({"type": "loop_stop", "reason": reason, "detail": detail,
              "report": report.to_dict()})
        return report

    try:
        # A run that is already done before it starts is a legitimate and very
        # cheap outcome, and one hearth_grow could never reach.
        if done_command or required_artifacts:
            state = _completion_state(done_command, required_artifacts, workspace,
                                      token, done_timeout, workers, done_runner)
            report.completion = state
            if state.get("done"):
                report.turns = start_turn - 1
                return _finish(STOP_COMPLETED,
                               "the goal was already satisfied before the first turn")

        turn = start_turn
        while True:
            if token.cancelled():
                return _finish(STOP_CANCELLED, token.reason)

            stats = _spend(report, clock() - started)
            hit = ceilings.exceeded(stats)
            if hit is not None:
                name, limit, value = hit
                return _finish(STOP_CEILING, "{} ceiling reached ({} of {})".format(
                    name, _hms(value) if name == "wall clock" else value,
                    _hms(limit) if name == "wall clock" else limit))

            journal.append({"t": "turn_start", "turn": turn, "ts": time.time()})
            emit({"type": "turn_start", "turn": turn})
            t_turn = clock()
            turn_actions = []
            turn_errors = []
            turn_tokens_in = turn_tokens_out = 0
            turn_writes = 0
            summary_bits = []

            # ---- model ----------------------------------------------------
            sink = _TokenSink(token, on_text=lambda p: emit({"type": "delta", "text": p}))
            messages = _trim_messages(messages)
            try:
                finished, got = _call_cancellable(
                    lambda: chat_fn(list(messages), tool_specs, sink), token, workers)
            except hearth_backend.StopStream:
                # The sink's raise beat the poll. Same meaning either way.
                finished, got = False, None
            except Exception as exc:  # noqa: BLE001
                return _finish(STOP_ERROR, "{}: {}".format(type(exc).__name__, exc))
            if not finished or token.cancelled():
                return _finish(STOP_CANCELLED, token.reason)

            msg, t_in, t_out = got
            turn_tokens_in += int(t_in or 0)
            turn_tokens_out += int(t_out or 0)
            report.tokens_in += int(t_in or 0)
            report.tokens_out += int(t_out or 0)
            messages.append(msg)
            content = msg.get("content") or ""
            if content:
                emit({"type": "message", "role": "assistant", "content": content})
            if sink.overran:
                report.notice("turn {}: the model's reply was cut off at the "
                              "{:,}-character generation cap".format(turn, sink.chars))

            calls = msg.get("tool_calls") or []
            if not calls:
                parsed = hearth_loop.parse_content_tool_calls(content, allowed=manifest)
                if parsed:
                    calls = [{"function": c} for c in parsed]

            # ---- tools ----------------------------------------------------
            blocked_detail = ""
            for call in calls:
                if token.cancelled():
                    return _finish(STOP_CANCELLED, token.reason)
                fn = call.get("function") or {}
                name = fn.get("name") or ""
                raw = fn.get("arguments")
                if isinstance(raw, dict):
                    cargs = raw
                elif raw:
                    try:
                        cargs = json.loads(raw)
                    except (ValueError, TypeError):
                        cargs = {}
                else:
                    cargs = {}
                turn_actions.append((name, cargs))
                report.tool_calls += 1

                verdict = permissions.decide(mode, name, cargs, auto_allow,
                                             allowed_tools=manifest)
                if verdict == "gate":
                    if gate_policy == "stop":
                        blocked_detail = "{} needs approval ({})".format(
                            name, json.dumps(cargs, default=str)[:160])
                        break
                    if gate_policy == "ask" and approve_fn is not None:
                        ok, decision = _call_cancellable(
                            lambda n=name, a=cargs: approve_fn(n, a), token, workers)
                        if not ok:
                            return _finish(STOP_CANCELLED, token.reason)
                        verdict = "allow" if decision else "deny"
                    else:
                        verdict = "deny"
                    if verdict == "deny":
                        report.gates_denied += 1
                        report.denied_tools[name] = report.denied_tools.get(name, 0) + 1

                if verdict != "allow":
                    result = ("denied: this run is unattended and {} requires approval "
                              "nobody is here to give. Do not try it again; work with "
                              "the tools you have.".format(name))
                    if manifest is not None and name not in manifest:
                        result = ("denied: {} is not in this run's capability "
                                  "manifest.".format(name))
                    emit({"type": "tool_result", "tool": name, "denied": True,
                          "output": result})
                    messages.append({"role": "tool", "content": result})
                    turn_errors.extend(extract_errors(result))
                    continue

                emit({"type": "tool_call", "tool": name, "args": cargs})
                try:
                    ok, result = _call_cancellable(
                        lambda n=name, a=cargs: execute_tool_fn(n, a, workspace),
                        token, workers)
                except Exception as exc:  # noqa: BLE001 - a tool crash is a tool result
                    ok, result = True, "{}: {}".format(type(exc).__name__, exc)
                if not ok:
                    return _finish(STOP_CANCELLED, token.reason)
                result = result if isinstance(result, str) else str(result)
                if permissions.risk_of(name) == "edit":
                    turn_writes += 1
                    report.writes += 1
                emit({"type": "tool_result", "tool": name,
                      "output": result[:hearth_loop.MAX_EVENT_OUT]})
                messages.append({"role": "tool",
                                 "content": result[:hearth_loop.MAX_EVENT_OUT]})
                turn_errors.extend(extract_errors(result))
                summary_bits.append(name)

            # ---- checkpoint ----------------------------------------------
            ck_id = ""
            if checkpoint_fn is not None:
                try:
                    ok, ck = _call_cancellable(
                        lambda: checkpoint_fn(workspace, label="loop {} turn {}".format(
                            run_id, turn)), token, workers)
                    if ok and isinstance(ck, dict):
                        ck_id = ck.get("id") or ""
                        if ck_id:
                            report.checkpoints.append(ck_id)
                            emit({"type": "checkpoint", "id": ck_id, "turn": turn})
                except Exception as exc:  # noqa: BLE001 - a broken store must not end a run
                    if not report.checkpoint_error:
                        report.checkpoint_error = "{}: {}".format(type(exc).__name__, exc)
                        report.notice("checkpoints are unavailable this run: {}".format(
                            report.checkpoint_error))

            # ---- completion check ----------------------------------------
            # Run BEFORE the ledger records this turn, because the gate's own
            # output is a first-class error source: "the same 3 tests failed
            # each time" is the archetypal thing a stalled run needs to be
            # able to say, and those 3 failures come from here, not from a
            # tool call. Feeding them into turn_errors is what lets
            # repeat_error see them at all.
            claims_done = DONE_SENTINEL in content.upper()
            completion_state = None
            if done_command or required_artifacts:
                completion_state = _completion_state(
                    done_command, required_artifacts, workspace, token,
                    done_timeout, workers, done_runner)
                report.completion = completion_state
                if token.cancelled():
                    return _finish(STOP_CANCELLED, token.reason)
                if not completion_state.get("done"):
                    turn_errors.extend(extract_errors(
                        completion_state.get("output") or completion_state.get("detail") or ""))

            # ---- fingerprint + ledger ------------------------------------
            manifest_now = scan_fn(workspace)
            delta = diff_manifests(prev_manifest, manifest_now)
            prev_manifest = manifest_now
            uniq_errors = []
            seen_sigs = set()
            for sig, text in turn_errors:
                if sig not in seen_sigs:
                    seen_sigs.add(sig)
                    uniq_errors.append((sig, text))
            rec = ledger.record(turn, manifest_now["digest"], turn_actions, uniq_errors)
            report.turns = turn

            changed = len(delta["created"]) + len(delta["modified"]) + len(delta["deleted"])
            summary = ", ".join(summary_bits[:3]) if summary_bits else "no tools"
            if changed:
                summary = "{} ({} file{})".format(summary, changed, "" if changed == 1 else "s")
            log_row = {"turn": turn, "new_state": rec["new_state"], "summary": summary,
                       "errors": [s for s, _ in uniq_errors],
                       "created": delta["created"][:10], "modified": delta["modified"][:10],
                       "deleted": delta["deleted"][:10],
                       "seconds": round(clock() - t_turn, 2)}
            report.turn_log.append(log_row)
            # `spend` is built by the same expression the ceiling check is fed
            # at the top of the next turn, deliberately: a watcher that draws
            # a budget meter from this and an enforcement that reads a
            # different tally would eventually disagree, and the one the user
            # can see is the one they would believe. One expression, one
            # meaning. See _spend().
            emit({"type": "progress", "turn": turn, "new_state": rec["new_state"],
                  "changed": changed, "errors": len(uniq_errors),
                  "spend": _spend(report, clock() - started)})

            journal.append({
                "t": "turn_end", "turn": turn, "ts": time.time(),
                "digest": manifest_now["digest"], "baseline": baseline.get("files"),
                "actions_raw": [[n, a] for n, a in turn_actions],
                "errors": [s for s, _ in uniq_errors],
                "error_text": {s: t for s, t in uniq_errors},
                "tokens_in": turn_tokens_in, "tokens_out": turn_tokens_out,
                "writes": turn_writes, "tool_calls": len(turn_actions),
                "checkpoint": ck_id, "log": log_row,
                "messages": _trim_messages(messages),
            })

            if blocked_detail:
                return _finish(STOP_BLOCKED, blocked_detail)

            # ---- act on the completion check -----------------------------
            steered = False
            if completion_state is not None:
                if completion_state.get("done"):
                    return _finish(STOP_COMPLETED,
                                   completion_state.get("detail") or "completion check passed")
                # Not done. Hand the check's actual output back to the model:
                # telling it only "not done" is what leaves it guessing, and a
                # guessing model is one that repeats itself.
                why = completion_state.get("detail") or "the completion check did not pass"
                out = (completion_state.get("output") or "").strip()
                if out:
                    why += "\nThe check reported:\n" + out[-1500:]
                if claims_done:
                    # The veto. The model's opinion is an input, never the check.
                    emit({"type": "notice", "detail":
                          "turn {}: completion claim vetoed ({})".format(
                              turn, completion_state.get("detail"))})
                messages.append({"role": "user",
                                 "content": "Not done yet: " + why + "\nKeep working."})
                steered = True
            elif claims_done and not calls:
                # No deterministic check was configured, so the honest thing is
                # to accept the claim AND say in the report that nothing
                # verified it.
                report.completion = {"done": True, "verified": False,
                                     "detail": "the model declared the goal complete; "
                                               "no completion check was configured to "
                                               "verify it"}
                return _finish(STOP_COMPLETED, "the model declared the goal complete "
                                               "(unverified: no completion check was set)")

            # ---- stall ----------------------------------------------------
            v = ledger.verdict()
            if v["stalled"]:
                report.verdict = v
                return _finish(STOP_STALLED, v["reason"])

            if not calls and not content.strip():
                messages.append({"role": "user", "content":
                                 "That turn did nothing. Take one concrete step "
                                 "toward the goal now, using a tool."})
            elif not steered:
                # Only when the completion check has not already said something
                # more specific, so the model gets one instruction per turn
                # rather than a generic nudge stacked on top of a real one.
                messages.append({"role": "user", "content":
                                 "Continue toward the goal. If it is fully achieved, "
                                 "reply with " + DONE_SENTINEL + "."})
            turn += 1
            if not token.sleep(0.0):
                return _finish(STOP_CANCELLED, token.reason)

    except Exception as exc:  # noqa: BLE001 - a loop must always end with a reason
        return _finish(STOP_ERROR, "{}: {}".format(type(exc).__name__, exc))


def _completion_state(done_command, required_artifacts, workspace, token, timeout,
                      workers, runner):
    """Combine the deterministic checks into one verdict. Artifacts first
    (free), then the command (expensive)."""
    missing = _missing_artifacts(required_artifacts, workspace)
    if missing:
        return {"done": False, "verified": True,
                "detail": "required file(s) missing or empty: " + ", ".join(missing),
                "missing": missing}
    if not done_command:
        return {"done": True, "verified": True,
                "detail": "every required artifact is present"}
    state = _run_done_command(done_command, workspace, token, timeout, workers, runner)
    state["verified"] = True
    return state


# --------------------------------------------------------------------------
# self-test
# --------------------------------------------------------------------------

def _self_test():  # noqa: PLR0915 - one function on purpose, per house style
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    tmp_root = tempfile.mkdtemp(prefix="workloop-")

    def ws(name):
        d = os.path.join(tmp_root, name)
        os.makedirs(d, exist_ok=True)
        return d

    def jr(name):
        return Journal(os.path.join(tmp_root, name + ".jsonl"), fsync=False)

    def write(d, rel, text):
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p) or d, exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)

    # === _missing_artifacts ==============================================
    # The completion check resolves every artifact name through the workspace
    # containment check, so it can never be turned into a probe for which
    # files exist elsewhere on the machine. The config parsers refuse a rooted
    # path before it gets here (hearth_contain.is_rooted); this is the layer
    # underneath that, tested on its own because a caller can reach
    # run_workloop() without going through parse_loop_config().
    _art_ws = ws("artifacts")
    write(_art_ws, "out/report.txt", "content")
    write(_art_ws, "empty.txt", "")
    assert _missing_artifacts(["out/report.txt"], _art_ws) == []
    assert _missing_artifacts(["out/nope.txt"], _art_ws) == ["out/nope.txt"]
    assert _missing_artifacts(["empty.txt"], _art_ws) == ["empty.txt"], \
        "an existing but empty artifact is not a produced artifact"
    assert _missing_artifacts(None, _art_ws) == []

    # A rooted name is resolved INSIDE the workspace, never against the real
    # filesystem, and reports missing because nothing is there. The control
    # case is the point: a file that certainly exists on the host, named
    # absolutely, must still come back missing. Before this went through
    # safe_join the branch was os.path.isabs, whose answer for a
    # single-leading-slash path changed in Python 3.13 - so on 3.12 this
    # stat'ed the real file and on 3.13 it did not, from the same source.
    _host_file = os.path.abspath(__file__)
    assert os.path.getsize(_host_file) > 0, "the control case needs a real file"
    _rooted = "/" + _host_file.replace("\\", "/").lstrip("/")
    assert _missing_artifacts([_rooted], _art_ws) == [_rooted], (
        "an absolute path to a real file outside the workspace was accepted "
        "as a produced artifact")
    assert _missing_artifacts([_host_file], _art_ws) == [_host_file], (
        "a drive-qualified path to a real file outside the workspace was "
        "accepted as a produced artifact")
    assert _missing_artifacts(["../../etc/passwd"], _art_ws) == ["../../etc/passwd"]

    # === CancelToken =====================================================
    t = CancelToken()
    assert not t.cancelled()
    t.cancel("because")
    assert t.cancelled() and t.reason == "because"
    t.cancel("a later reason")
    assert t.reason == "because", "the first reason must win, not the last"

    ext = {"on": False}
    t2 = CancelToken(is_cancelled_fn=lambda: ext["on"])
    assert not t2.cancelled(), "an external flag that is off must not cancel"
    ext["on"] = True
    assert t2.cancelled(), "an external flag is a real cancel source"
    assert "host" in t2.reason

    t3 = CancelToken(is_cancelled_fn=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert not t3.cancelled(), "a raising external source must not wedge a run"
    t3.cancel()
    assert t3.cancelled(), "...and must not disarm the internal flag either"

    # sleep returns True when it slept the whole time, False when cancelled,
    # and a cancel from another thread wakes it promptly.
    t4 = CancelToken()
    assert t4.sleep(0.01) is True
    t5 = CancelToken()
    threading.Timer(0.05, lambda: t5.cancel("stop")).start()
    _t0 = time.monotonic()
    assert t5.sleep(10.0) is False, "sleep must wake on cancel"
    assert time.monotonic() - _t0 < 3.0, "sleep must not sleep through a stop"

    # An EXTERNAL cancel must also wake a sleep, which is the case an Event
    # alone cannot handle and the reason sleep() is sliced.
    ext2 = {"on": False}
    t6 = CancelToken(is_cancelled_fn=lambda: ext2["on"])
    threading.Timer(0.05, lambda: ext2.__setitem__("on", True)).start()
    _t0 = time.monotonic()
    assert t6.sleep(10.0) is False, "sleep must wake on an EXTERNAL cancel too"
    assert time.monotonic() - _t0 < 3.0

    # === _TokenSink ======================================================
    st = CancelToken()
    seen = []
    sink = _TokenSink(st, on_text=seen.append)
    sink("hello")
    assert seen == ["hello"]
    st.cancel()
    try:
        sink("more")
        raise AssertionError("the sink must raise StopStream once cancelled; without "
                             "that raise nothing stops a local model mid-generation")
    except hearth_backend.StopStream:
        pass

    cap = _TokenSink(CancelToken(), max_chars=10)
    cap("12345")
    try:
        cap("678901234")
        raise AssertionError("the generation character cap must be enforced DURING "
                             "generation, not between turns")
    except hearth_backend.StopStream:
        pass
    assert cap.overran

    def _bad_sink(_p):
        raise ValueError("ui is broken")
    bs = _TokenSink(CancelToken(), on_text=_bad_sink)
    bs("x")
    bs("y")  # must not raise: a broken UI sink cannot kill a run

    # === _call_cancellable ===============================================
    ok, val = _call_cancellable(lambda: 42, CancelToken())
    assert ok and val == 42

    try:
        _call_cancellable(lambda: (_ for _ in ()).throw(KeyError("k")), CancelToken())
        raise AssertionError("a finished call's exception must reach the caller")
    except KeyError:
        pass

    # Cancellation abandons rather than kills, and that is visible: the call
    # returns immediately, the worker is still counted as live, and it keeps
    # running until its own blocking call unwinds.
    release = threading.Event()
    ran = {"done": False}
    wk = _Workers()
    ct = CancelToken()

    def _blocker():
        release.wait(10)
        ran["done"] = True
        return "late"
    threading.Timer(0.05, lambda: ct.cancel("stopped")).start()
    _t0 = time.monotonic()
    ok, val = _call_cancellable(_blocker, ct, wk)
    _dt = time.monotonic() - _t0
    assert ok is False and val is None, "cancellation must win the race"
    assert _dt < 3.0, ("cancellation must be prompt, took {:.2f}s".format(_dt))
    assert wk.live() == 1, ("an abandoned worker must still be counted as live; "
                            "otherwise a caller cannot tell 'the loop stopped' from "
                            "'the workspace is quiet'")
    release.set()
    for _ in range(200):
        if wk.live() == 0:
            break
        time.sleep(0.01)
    assert ran["done"] and wk.live() == 0

    # === scan_workspace / diff_manifests =================================
    d = ws("scan")
    write(d, "a.txt", "one")
    write(d, "sub/b.txt", "two")
    os.makedirs(os.path.join(d, "__pycache__"), exist_ok=True)
    write(d, "__pycache__/junk.pyc", "noise")
    m1 = scan_workspace(d)
    assert set(m1["files"]) == {"a.txt", "sub/b.txt"}, ("ignored dirs must not be "
                                                       "fingerprinted", m1["files"])
    assert scan_workspace(d)["digest"] == m1["digest"], "digest must be stable"

    write(d, "__pycache__/junk.pyc", "different noise")
    assert scan_workspace(d)["digest"] == m1["digest"], \
        "churn inside an ignored dir must NOT read as progress"

    write(d, "a.txt", "one!")
    m2 = scan_workspace(d)
    assert m2["digest"] != m1["digest"]
    dd = diff_manifests(m1, m2)
    assert dd["modified"] == ["a.txt"] and not dd["created"] and not dd["deleted"], dd
    write(d, "c.txt", "new")
    os.remove(os.path.join(d, "sub", "b.txt"))
    m3 = scan_workspace(d)
    dd3 = diff_manifests(m2, m3)
    assert dd3["created"] == ["c.txt"] and dd3["deleted"] == ["sub/b.txt"], dd3

    # returning a file to its old content returns the workspace to its old
    # digest, which is exactly what oscillation detection depends on
    write(d, "a.txt", "one")
    os.remove(os.path.join(d, "c.txt"))
    write(d, "sub/b.txt", "two")
    assert scan_workspace(d)["digest"] == m1["digest"], \
        "an identical tree must produce an identical digest, or A-to-B-to-A is invisible"

    trunc = scan_workspace(d, max_files=1)
    assert trunc["truncated"] and trunc["count"] == 1

    # === error fingerprinting ============================================
    a = normalise_error("  File \"C:\\Users\\x\\t.py\", line 42, in f: ValueError: bad")
    b = normalise_error("  File \"C:\\Users\\y\\t.py\", line 77, in f: ValueError: bad")
    assert a == b, ("line numbers and absolute paths must normalise away, or the "
                    "same failure looks new every turn", a, b)
    assert normalise_error("3 tests failed") == normalise_error("5 tests failed")
    assert normalise_error("ValueError: bad") != normalise_error("TypeError: bad")
    assert normalise_error("at 0xdeadbeef failed") == normalise_error("at 0x1234 failed")

    errs = extract_errors("all good\nTraceback (most recent call last):\n"
                          "ModuleNotFoundError: No module named 'reqeusts'\nfine\n")
    sigs = [s for s, _ in errs]
    assert len(errs) == 2, errs
    assert any("modulenotfounderror" in s for s in sigs), sigs
    assert extract_errors("everything is fine\nall tests pass") == []
    assert len(extract_errors("\n".join("error {}".format(i) for i in range(50)))) \
        <= MAX_ERRORS_PER_TURN
    # a hostile enormous line is bounded, not scanned forever
    assert extract_errors("error " + "x" * 100000) == []

    # === action_fingerprint ==============================================
    f1 = action_fingerprint([("read_file", {"path": "a"}), ("list_files", {})])
    f2 = action_fingerprint([("list_files", {}), ("read_file", {"path": "a"})])
    assert f1 == f2, "emission order is not a difference worth calling progress"
    f3 = action_fingerprint([("read_file", {"path": "b"}), ("list_files", {})])
    assert f1 != f3, "different arguments are a different action"
    assert action_fingerprint([]) == ""

    # === Ceilings ========================================================
    c = Ceilings(max_turns=5, max_seconds=60, max_tokens=100, max_writes=3,
                 max_tool_calls=10)
    assert c.exceeded({"turns": 1, "elapsed": 1, "tokens": 1, "writes": 1,
                       "tool_calls": 1}) is None
    assert c.exceeded({"turns": 5})[0] == "turns"
    assert c.exceeded({"elapsed": 60})[0] == "wall clock"
    assert c.exceeded({"tokens": 100})[0] == "tokens"
    assert c.exceeded({"writes": 3})[0] == "unattended writes"
    assert c.exceeded({"tool_calls": 10})[0] == "tool calls"
    assert c.exceeded({"turns": 5, "tokens": 100})[0] == "turns", "order is deterministic"
    assert Ceilings(max_turns=-1).exceeded({"turns": 10**9}) is None, "-1 disables"
    assert Ceilings.from_dict(c.to_dict()).to_dict() == c.to_dict()

    # === patience presets ================================================
    # (a) The shipped default IS the balanced preset. Two places hold these
    # five numbers -- the constructor signature, which is what a reader sees,
    # and the preset table, which is what the UI offers -- and they drifting
    # apart would mean the app's "Balanced" and the library's default were
    # quietly different runs.
    assert StallPolicy().to_dict() == dict(
        [p for p in PATIENCE_SETTINGS if p["name"] == "balanced"][0]["settings"]), \
        "StallPolicy's defaults and the balanced preset must be the same numbers"
    assert DEFAULT_PATIENCE in PATIENCE_ORDER
    assert patience_of(StallPolicy()) == DEFAULT_PATIENCE

    # (b) A preset holds stall thresholds and NOTHING else. This is the
    # structural half of "a stall policy must not be able to widen a ceiling":
    # there is no key a preset could carry that Ceilings would read, so the
    # question cannot arise at this layer at all. The behavioural half is
    # test (13) below, which runs the most patient preset into a ceiling.
    _ceiling_keys = set(Ceilings().to_dict())
    for preset in PATIENCE_SETTINGS:
        assert set(preset["settings"]) == set(StallPolicy().to_dict()), preset["name"]
        assert not set(preset["settings"]) & _ceiling_keys, \
            ("a patience preset must not carry anything a ceiling reads", preset["name"])
        for field in ("label", "summary", "cost"):
            assert preset[field].strip(), (preset["name"], field)
        # Round trip: every preset must be recognisable from its numbers alone.
        assert patience_of(stall_policy_for(preset["name"])) == preset["name"]

    # (c) The most patient setting says, where it is chosen, that it can spend
    # the whole budget. Requirement, not decoration: a user who picks patience
    # without being told what it costs was misled by this module.
    _keep = [p for p in PATIENCE_SETTINGS if p["name"] == PATIENCE_ORDER[-1]][0]
    assert "cannot succeed" in _keep["cost"], _keep["cost"]

    # (d) The names are in order of patience, so the word a user reads means
    # what it says. A threshold of 0 is a detector switched OFF, which is the
    # most patient value there is, not the least -- ranking it as 0 would call
    # the loosest preset the strictest.
    def _rank(v):
        return float("inf") if v == 0 else v
    for earlier, later in zip(PATIENCE_SETTINGS, PATIENCE_SETTINGS[1:]):
        for key in earlier["settings"]:
            assert _rank(later["settings"][key]) >= _rank(earlier["settings"][key]), \
                ("presets must be ordered least to most patient", key,
                 earlier["name"], later["name"])

    # (e) An unknown name is REFUSED, never defaulted. A run that silently
    # ignores the setting a user picked is worse than one that will not start,
    # because nothing on screen would ever say so.
    for bad in ("nonsense", "", None, "BALANCED"):
        try:
            stall_policy_for(bad)
            raise AssertionError("unknown patience must be refused: {!r}".format(bad))
        except ValueError:
            pass
    assert patience_of(StallPolicy(window=999)) == CUSTOM_PATIENCE, \
        "hand-typed thresholds must never be labelled with a preset they do not match"
    assert CUSTOM_PATIENCE not in PATIENCE_ORDER, \
        "'custom' is what patience_of reports, not something a caller may ask for"

    # (f) The CLI's preset and its one numeric override agree.
    assert _cli_stall_policy("balanced", None).to_dict() == StallPolicy().to_dict()
    assert _cli_stall_policy("give_up_early", 0).window == 0
    assert _cli_stall_policy("give_up_early", 0).repeat_actions == \
        stall_policy_for("give_up_early").repeat_actions

    # === ProgressLedger ==================================================
    # Each detector is isolated by disabling the others, so a test that passes
    # proves THAT detector works rather than that something fired.
    def _pol(**kw):
        base = {"window": 999, "repeat_actions": 999, "repeat_errors": 999,
                "oscillations": 999, "min_turns": 1}
        base.update(kw)
        return StallPolicy(**base)

    # (a) no_new_state
    lg = ProgressLedger(_pol(window=3))
    lg.record(1, "D1", [("x", {})], [])
    lg.record(2, "D1", [("x", {})], [])
    lg.record(3, "D1", [("x", {})], [])
    assert not lg.verdict()["stalled"], "turn 1 reached a new state; the window is not stale yet"
    lg.record(4, "D1", [("x", {})], [])
    v = lg.verdict()
    assert v["stalled"] and v["detectors"][0]["name"] == "no_new_state", v
    assert "turns 2-4" in v["reason"], v["reason"]

    # (b) a genuinely progressing run must NEVER stall. Without this control
    # every other assertion here could pass with a detector hardwired to True.
    lg_ok = ProgressLedger(StallPolicy(window=3, repeat_actions=3, repeat_errors=3,
                                       oscillations=3, min_turns=1))
    for i in range(1, 30):
        lg_ok.record(i, "state{}".format(i), [("write_file", {"path": "f{}".format(i)})], [])
        assert not lg_ok.verdict()["stalled"], \
            ("a run reaching a new state every turn must never be called stalled "
             "(turn {})".format(i), lg_ok.verdict())

    # (c) oscillation: A-B-A-B changes every turn, so a naive "did anything
    # change since last turn" check calls it progress forever.
    lg_o = ProgressLedger(_pol(oscillations=3, window=999))
    for i, digest in enumerate(["A", "B", "A", "B", "A", "B"], start=1):
        lg_o.record(i, digest, [("edit_file", {"n": i})], [])
    v = lg_o.verdict()
    assert v["stalled"] and v["detectors"][0]["name"] == "oscillation", v
    assert "returned to an already-seen state" in v["reason"], v["reason"]

    # ...but a workspace that never moves at all is FROZEN, not oscillating.
    # Reporting that as oscillation would put the wrong sentence in the
    # account, which is the whole product here.
    lg_static = ProgressLedger(_pol(oscillations=2, window=999))
    for i in range(1, 8):
        lg_static.record(i, "SAME", [("read_file", {"n": i})], [])
    assert not any(det["name"] == "oscillation"
                   for det in lg_static.verdict()["detectors"]), \
        "a never-moving workspace must not be reported as oscillation"

    # (d) repeat_action
    lg_a = ProgressLedger(_pol(repeat_actions=3))
    for i in range(1, 5):
        lg_a.record(i, "s{}".format(i), [("run_command", {"command": "pytest"})], [])
    v = lg_a.verdict()
    assert v["stalled"] and v["detectors"][0]["name"] == "repeat_action", v
    assert "pytest" in v["reason"], v["reason"]

    # (e) repeat_error
    lg_e = ProgressLedger(_pol(repeat_errors=3))
    for i in range(1, 5):
        lg_e.record(i, "s{}".format(i), [("run_command", {"n": i})],
                    [("modulenotfounderror: no module named 'reqeusts'",
                      "ModuleNotFoundError: No module named 'reqeusts'")])
    v = lg_e.verdict()
    assert v["stalled"] and v["detectors"][0]["name"] == "repeat_error", v
    assert "reqeusts" in v["reason"], "the account must quote the actual error"
    assert "4 times in total" in v["reason"], v["reason"]

    # (e2) A persistent error must be seen THROUGH noise. Every turn below
    # carries the same three failing assertions; some turns additionally carry
    # a transient SyntaxError that lands first in the output. Comparing each
    # turn's FIRST error would reset the streak on every noisy turn and never
    # fire -- which is exactly what happened on a real unsatisfiable-check run
    # before this was fixed (26 turns instead of 6).
    lg_noise = ProgressLedger(_pol(repeat_errors=5))
    persistent = [("sig-one", "FAILED test_one"), ("sig-two", "FAILED test_two"),
                  ("sig-three", "FAILED test_three")]
    for i in range(1, 7):
        errs = list(persistent)
        if i % 2 == 0:  # noise that sorts to the front on alternate turns
            errs.insert(0, ("sig-syntax-{}".format(i), "SyntaxError: line {}".format(i)))
        lg_noise.record(i, "state{}".format(i), [("write_file", {"n": i})], errs)
    v = lg_noise.verdict()
    assert v["stalled"] and v["detectors"][0]["name"] == "repeat_error", (
        "a persistent failure must be detected through transient noise", v)
    assert "test_one" in v["reason"] or "test_two" in v["reason"] \
        or "test_three" in v["reason"], v["reason"]
    assert lg_noise._error_streak()[0] == 6, lg_noise._error_streak()

    # (f) min_turns refuses to judge a slow start
    lg_m = ProgressLedger(StallPolicy(window=1, min_turns=5, repeat_actions=1,
                                      repeat_errors=1, oscillations=1))
    lg_m.record(1, "D", [("x", {})], [])
    assert not lg_m.verdict()["stalled"], "a slow start is not a stall"
    assert "too early" in lg_m.verdict()["reason"]

    # (g) recurring_errors feeds the report
    lg_r = ProgressLedger(_pol())
    for i in range(3):
        lg_r.record(i + 1, "s{}".format(i), [], [("sig-a", "Boom A")])
    lg_r.record(9, "s9", [], [("sig-b", "Boom B")])
    assert lg_r.recurring_errors()[0] == (3, "Boom A"), lg_r.recurring_errors()
    assert lg_r.last_error == "Boom B"

    # === Journal =========================================================
    j = jr("j1")
    j.append({"t": "run", "run_id": "r1", "mode": "auto"})
    j.append({"t": "turn_start", "turn": 1})
    j.append({"t": "turn_end", "turn": 1, "digest": "D1", "messages": [{"role": "x"}]})
    j.append({"t": "turn_start", "turn": 2})  # the crash: no matching end
    with open(j.path, "a", encoding="utf-8") as fh:
        fh.write('{"t": "turn_en')  # a truncated final line, as a power cut leaves
    got = load_journal("r1", j.path)
    assert got["interrupted_turn"] == 2, ("a turn_start with no turn_end is the "
                                          "signature of a crash mid-turn", got)
    assert len(got["completed_turns"]) == 1
    assert got["messages"] == [{"role": "x"}]
    assert got["header"]["run_id"] == "r1"

    # === run_workloop: refusals ==========================================
    try:
        run_workloop("g", "m", ws("refuse"), mode="bypass", journal=jr("x"))
        raise AssertionError("bypass must be unreachable from a work loop")
    except ValueError as exc:
        assert "bypass" in str(exc), exc
    for bad in ("nonsense", "", None):
        try:
            run_workloop("g", "m", ws("refuse"), mode=bad, journal=jr("x"))
            raise AssertionError("unknown mode must be refused: {!r}".format(bad))
        except ValueError:
            pass
    assert "bypass" not in ALLOWED_MODES
    try:
        run_workloop("g", "m", ws("refuse"), gate_policy="whatever", journal=jr("x"))
        raise AssertionError("unknown gate_policy must be refused")
    except ValueError:
        pass

    # A journal claiming bypass must not be honoured on resume: a journal is a
    # file the agent's own write_file could have produced.
    jb = jr("bypass-journal")
    jb.append({"t": "run", "run_id": "rb", "mode": "bypass"})
    try:
        run_workloop("g", "m", ws("refuse2"), journal=jb, resume=True, run_id="rb")
        raise AssertionError("a persisted bypass mode must be refused on resume")
    except ValueError as exc:
        assert "bypass" in str(exc), exc

    # === run_workloop: harness ===========================================
    def scripted(script, sink_pieces=("tok",)):
        """chat_fn returning one scripted (content, tool_calls) per turn, and
        streaming a token first so the cancellation seam is always exercised."""
        state = {"i": 0}

        def _chat(messages, tools, on_token):
            i = min(state["i"], len(script) - 1)
            state["i"] += 1
            for p in sink_pieces:
                on_token(p)
            content, calls = script[i]
            msg = {"role": "assistant", "content": content}
            if calls:
                msg["tool_calls"] = [{"function": {"name": n, "arguments": a}}
                                     for n, a in calls]
            return msg, 10, 20
        return _chat

    def tool_ok(name, args, workspace):
        return "ok: {}".format(name)

    ck_ids = {"n": 0}

    def fake_ck(workspace, label=None, timestamp=None):
        ck_ids["n"] += 1
        return {"id": "ck{:040d}".format(ck_ids["n"]), "label": label}

    # (1) COMPLETES because a deterministic check passed.
    d1 = ws("complete")
    calls_seen = []

    def writing_tool(name, args, workspace):
        calls_seen.append(name)
        if name == "write_file":
            write(workspace, args["path"], args.get("content", "x"))
        return "ok"
    rep = run_workloop(
        "make report.md", "m", d1, journal=jr("r1"),
        chat_fn=scripted([("working", [("write_file", {"path": "report.md",
                                                       "content": "body"})]),
                          ("more", [])]),
        execute_tool_fn=writing_tool, checkpoint_fn=fake_ck,
        required_artifacts=["report.md"], ceilings=Ceilings(max_turns=10))
    assert rep.stop_reason == STOP_COMPLETED, (rep.stop_reason, rep.stop_detail)
    assert rep.turns == 1, rep.turns
    assert rep.created == ["report.md"], rep.created
    assert rep.checkpoints and len(rep.checkpoints) == 1
    assert rep.completion["verified"] is True
    assert "finished the goal" in rep.headline()
    assert "report.md" in rep.render()

    # done_command as the gate
    d1b = ws("complete-cmd")
    runs = {"n": 0}

    class _P:
        def __init__(self, rc):
            self.returncode = rc
            self.stdout = "out"
            self.stderr = ""

    def runner():
        # Call 1 is the pre-flight check (a goal already satisfied before the
        # first turn is a legitimate, very cheap outcome). Calls 2 and 3 are
        # after turns 1 and 2.
        runs["n"] += 1
        return _P(0 if runs["n"] >= 3 else 1)
    rep = run_workloop(
        "pass the tests", "m", d1b, journal=jr("r1b"),
        chat_fn=scripted([("t1", [("write_file", {"path": "a.py", "content": "1"})]),
                          ("t2", [("write_file", {"path": "b.py", "content": "2"})])]),
        execute_tool_fn=writing_tool, checkpoint_fn=fake_ck,
        done_command="pytest", done_runner=runner, ceilings=Ceilings(max_turns=10))
    assert rep.stop_reason == STOP_COMPLETED, (rep.stop_reason, rep.stop_detail)
    assert rep.turns == 2, ("the check failed on turn 1 and passed on turn 2", rep.turns)

    # a completion command that cannot run at all is reported as UNAVAILABLE,
    # not silently blamed on the model (hearth_grow failure mode 4)
    def broken_runner():
        raise OSError("no such shell")
    rep = run_workloop(
        "x", "m", ws("broken-check"), journal=jr("r1c"),
        chat_fn=scripted([("t", [])]), execute_tool_fn=tool_ok, checkpoint_fn=None,
        done_command="nope", done_runner=broken_runner,
        ceilings=Ceilings(max_turns=1))
    assert rep.completion.get("available") is False, rep.completion
    assert "could not run" in rep.completion["detail"]
    assert "could not run" in rep.render()

    # (2) CEILINGS, each named correctly.
    def _ceil(name, ceilings, **kw):
        return run_workloop("endless", "m", ws("ceil-" + name), journal=jr("c-" + name),
                            chat_fn=kw.pop("chat_fn", scripted([("thinking", [])])),
                            execute_tool_fn=kw.pop("execute_tool_fn", tool_ok),
                            checkpoint_fn=None, ceilings=ceilings,
                            stall=StallPolicy(window=0, repeat_actions=0,
                                              repeat_errors=0, oscillations=0),
                            **kw)
    r = _ceil("turns", Ceilings(max_turns=3))
    assert r.stop_reason == STOP_CEILING and "turns ceiling" in r.stop_detail, r.stop_detail
    assert r.turns == 3, r.turns
    assert "3 of 3" in r.stop_detail, r.stop_detail

    clk = {"t": 0.0}
    r = _ceil("wall", Ceilings(max_turns=999, max_seconds=10),
              clock=lambda: clk.__setitem__("t", clk["t"] + 4.0) or clk["t"])
    assert r.stop_reason == STOP_CEILING and "wall clock" in r.stop_detail, r.stop_detail
    assert "0:00:1" in r.stop_detail, r.stop_detail

    r = _ceil("tokens", Ceilings(max_turns=999, max_tokens=100))
    assert r.stop_reason == STOP_CEILING and "tokens ceiling" in r.stop_detail, r.stop_detail
    assert r.turns == 4, ("30 tokens per turn against a 100 ceiling", r.turns)

    r = _ceil("writes", Ceilings(max_turns=999, max_writes=2),
              chat_fn=scripted([("w", [("write_file", {"path": "f.txt",
                                                       "content": "c"})])] * 9),
              execute_tool_fn=tool_ok)
    assert r.stop_reason == STOP_CEILING and "unattended writes" in r.stop_detail, r.stop_detail
    assert r.writes == 2, r.writes

    r = _ceil("tools", Ceilings(max_turns=999, max_tool_calls=3),
              chat_fn=scripted([("t", [("read_file", {"path": "a"})])] * 9))
    assert r.stop_reason == STOP_CEILING and "tool calls" in r.stop_detail, r.stop_detail

    # (2b) A WATCHER CAN SEE THE SPEND, AND IT IS THE ENFORCED SPEND.
    # A UI showing "18 of 200 writes" from a tally other than the one the
    # ceiling check reads would eventually be wrong, and it is the visible one
    # the user would believe. The per-turn `progress` event therefore carries
    # exactly what Ceilings.exceeded is fed, and the last one before a ceiling
    # trip must already show the number that trips it.
    spends = []
    r = run_workloop(
        "watched", "m", ws("spend"), journal=jr("spend"),
        chat_fn=scripted([("w", [("write_file", {"path": "f.txt", "content": "c"})])] * 9),
        execute_tool_fn=tool_ok, checkpoint_fn=None,
        ceilings=Ceilings(max_turns=999, max_writes=3),
        stall=StallPolicy(window=0, repeat_actions=0, repeat_errors=0, oscillations=0),
        emit=lambda ev: spends.append(ev["spend"]) if ev.get("type") == "progress" else None)
    assert r.stop_reason == STOP_CEILING and "unattended writes" in r.stop_detail
    assert spends, "a watcher gets no progress events at all"
    for s in spends:
        assert set(s) == {"turns", "elapsed", "tokens", "writes", "tool_calls"}, s
    assert [s["writes"] for s in spends] == [1, 2, 3], (
        "the visible write tally must be the enforced one", spends)
    assert Ceilings(max_writes=3).exceeded(spends[-1]) is not None, (
        "the last spend a watcher saw must already be the one that trips the "
        "ceiling; a meter that reads under the limit while the run stops on it "
        "is a meter that lies", spends[-1])
    assert spends[-1]["turns"] == r.turns and spends[-1]["tokens"] == r.tokens

    # (3) STALLS on a task it cannot complete, with a legible account. This is
    # the hearth-grow failure mode, and the whole point of the module.
    d3 = ws("stall")
    write(d3, "target.py", "def f(): raise ValueError('nope')")
    rep = run_workloop(
        "make the failing test pass", "m", d3, journal=jr("r3"),
        chat_fn=scripted([("let me run the tests",
                           [("run_command", {"command": "python -m pytest"})])] * 40),
        execute_tool_fn=lambda n, a, w: (
            "Traceback (most recent call last):\n"
            "  File \"/tmp/t.py\", line 12, in test_f\n"
            "ValueError: nope\n1 failed"),
        checkpoint_fn=fake_ck, auto_allow={"python"},
        ceilings=Ceilings(max_turns=100),
        stall=StallPolicy(window=4, repeat_actions=5, repeat_errors=5,
                          oscillations=3, min_turns=2))
    assert rep.stop_reason == STOP_STALLED, (rep.stop_reason, rep.stop_detail)
    assert rep.turns < 12, ("a stall must be caught in a handful of turns, not a "
                            "thousand cycles", rep.turns)
    names = {det["name"] for det in rep.verdict["detectors"]}
    assert "no_new_state" in names and "repeat_action" in names, names
    assert "oscillation" not in names, (
        "a workspace that never moved is frozen, not oscillating; naming the wrong "
        "detector would put the wrong sentence in the account", names)
    text = rep.render()
    assert "ValueError: nope" in text, "the account must quote the last error verbatim"
    assert "no_new_state" in text and "repeat_action" in text, text
    assert "python -m pytest" in text, "the account must name the repeated action"
    assert "stopped making progress" in text

    # (3a) THE DETECTORS' HONEST LIMITS REACH THE READER, ON EVERY ACCOUNT.
    # The person who most needs to know that "did not stall" is not "worked"
    # is someone starting an unattended run from a GUI, and they will never
    # read a docstring. Every rendered account carries the list -- including a
    # COMPLETED one, which is the verdict most likely to be over-trusted.
    for headline, _means, _todo in PROGRESS_BLIND_SPOTS:
        assert headline in text, ("a stalled account must carry the limits", headline)
    assert "wrong answer" in text, (
        "the single most important blind spot -- confident, fluent, wrong -- must "
        "be named in words, not left as a reference to a docstring")

    ok_rep = run_workloop(
        "trivial", "m", ws("blind-ok"), journal=jr("blind-ok"),
        chat_fn=scripted([(DONE_SENTINEL, [])]), execute_tool_fn=tool_ok,
        checkpoint_fn=None)
    assert ok_rep.stop_reason == STOP_COMPLETED, ok_rep.stop_reason
    ok_text = ok_rep.render()
    for headline, _means, _todo in PROGRESS_BLIND_SPOTS:
        assert headline in ok_text, (
            "a COMPLETED account must carry the limits too: 'it finished' is the "
            "verdict a reader is most likely to over-trust", headline)
    assert "NOTHING VERIFIED THIS" in ok_text, (
        "a completion nobody checked must say so in the account, not only in the "
        "stop_detail", ok_text)

    # the ledger's own numbers back the sentence up
    assert rep.verdict["signals"]["distinct_states"] == 1, rep.verdict["signals"]
    assert rep.recurring and rep.recurring[0][0] >= 4, rep.recurring

    # (3b) THE COMPLETION CHECK'S OWN FAILURES ARE ERRORS THE LEDGER SEES.
    # "the same 3 tests failed each time" is the archetypal sentence a stalled
    # run has to be able to say, and those failures come from the completion
    # command, not from a tool call. A run where the model edits a file every
    # turn but the tests keep failing identically must stop on repeat_error,
    # and the account must quote the test output and hand it to the model.
    d3b = ws("check-errors")
    fed_back = []

    def edit_each_turn(messages, tools, on_token):
        fed_back.append(messages[-1].get("content", ""))
        n = len(fed_back)
        return ({"role": "assistant", "content": "trying again",
                 "tool_calls": [{"function": {"name": "write_file",
                                              "arguments": {"path": "sol.py",
                                                            "content": "v{}".format(n)}}}]},
                5, 5)

    class _Fail:
        returncode = 1
        stdout = ("FAILED test_a.py::test_one - AssertionError: expected 3\n"
                  "FAILED test_b.py::test_two - AssertionError: expected 7\n"
                  "FAILED test_c.py::test_three - AssertionError: expected 9\n"
                  "3 failed in 0.4s\n")
        stderr = ""
    rep = run_workloop(
        "make the tests pass", "m", d3b, journal=jr("r3b"), chat_fn=edit_each_turn,
        execute_tool_fn=writing_tool, checkpoint_fn=None,
        done_command="pytest", done_runner=lambda: _Fail(),
        ceilings=Ceilings(max_turns=30),
        stall=StallPolicy(window=999, repeat_actions=999, oscillations=999,
                          repeat_errors=4, min_turns=2))
    assert rep.stop_reason == STOP_STALLED, (rep.stop_reason, rep.stop_detail)
    names = {det["name"] for det in rep.verdict["detectors"]}
    assert names == {"repeat_error"}, (
        "the workspace changed every turn, so ONLY the repeated test failure "
        "should have ended this run", names)
    text = rep.render()
    assert "expected 3" in text, ("the account must quote the failing test output", text)
    assert "what the completion check said" in text, text
    assert "3 failed" in text, text
    # and the model was actually told what failed, rather than just "not done"
    assert any("FAILED test_a.py" in m for m in fed_back), \
        "the completion check's output must be handed back to the model"

    # (4) CANCELLED mid-generation. The token is set from inside the stream,
    # which is the only moment that proves the sink is what stops the model.
    d4 = ws("cancel-gen")
    ct = CancelToken()
    pieces = {"n": 0}

    def gen_chat(messages, tools, on_token):
        for _ in range(10000):
            pieces["n"] += 1
            if pieces["n"] == 3:
                ct.cancel("stopped by user")
            on_token("tok ")  # raises StopStream once cancelled
        raise AssertionError("generation was never interrupted")
    _t0 = time.monotonic()
    rep = run_workloop("x", "m", d4, journal=jr("r4"), token=ct, chat_fn=gen_chat,
                       execute_tool_fn=tool_ok, checkpoint_fn=None)
    _dt = time.monotonic() - _t0
    assert rep.stop_reason == STOP_CANCELLED, (rep.stop_reason, rep.stop_detail)
    assert rep.stop_detail == "stopped by user", rep.stop_detail
    assert pieces["n"] == 3, ("generation must stop AT the cancel, not drain",
                              pieces["n"])
    assert _dt < 5.0, ("mid-generation cancel must be prompt, took {:.2f}s".format(_dt))
    assert "stopped by you" in rep.headline()

    # (5) CANCELLED mid-tool-call, with the abandoned worker reported.
    d5 = ws("cancel-tool")
    ct5 = CancelToken()
    gate = threading.Event()

    def slow_tool(name, args, workspace):
        gate.wait(10)
        return "finally"
    threading.Timer(0.15, lambda: ct5.cancel("stopped by user")).start()
    _t0 = time.monotonic()
    rep = run_workloop("x", "m", d5, journal=jr("r5"), token=ct5,
                       chat_fn=scripted([("go", [("read_file", {"path": "a"})])]),
                       execute_tool_fn=slow_tool, checkpoint_fn=None)
    _dt = time.monotonic() - _t0
    assert rep.stop_reason == STOP_CANCELLED, (rep.stop_reason, rep.stop_detail)
    assert _dt < 5.0, ("mid-tool-call cancel must be prompt, took {:.2f}s".format(_dt))
    assert rep.live_workers >= 1, ("the report must admit an abandoned tool call is "
                                   "still running", rep.live_workers)
    assert "abandoned tool call" in rep.render()
    gate.set()

    # (5b) The abandoned-worker counter is injectable, and an injected one is
    # the ONLY one used. A caller that owns a counter for this workspace
    # (desktop/server/loop_engine.py hands in one wired to its Session, so
    # POST /restore and a replacement session can see a live worker) must not
    # end up consulting a counter this function kept privately instead.
    d5c = ws("cancel-tool-shared")
    ct5c = CancelToken()
    gate_c = threading.Event()
    shared = _Workers()
    peak = {"n": 0}

    def slow_tool_c(name, args, workspace):
        peak["n"] = max(peak["n"], shared.live())
        gate_c.wait(10)
        return "finally"
    threading.Timer(0.15, lambda: ct5c.cancel("stopped by user")).start()
    rep = run_workloop("x", "m", d5c, journal=jr("r5c"), token=ct5c, workers=shared,
                       chat_fn=scripted([("go", [("read_file", {"path": "a"})])]),
                       execute_tool_fn=slow_tool_c, checkpoint_fn=None)
    assert rep.stop_reason == STOP_CANCELLED, rep.stop_reason
    assert peak["n"] >= 1, ("the INJECTED counter must be the one the run "
                            "increments, not a private second one", peak["n"])
    assert shared.live() >= 1, ("the injected counter must still show the "
                                "abandoned worker after the run returned",
                                shared.live())
    assert rep.live_workers >= 1, rep.live_workers
    gate_c.set()
    for _ in range(500):
        if shared.live() == 0:
            break
        time.sleep(0.01)
    assert shared.live() == 0, ("the injected counter must fall back to zero "
                                "once the abandoned worker unwinds, so a caller "
                                "can tell 'stopped' from 'still writing'")

    # a token cancelled before the first turn stops without calling the model
    never = {"called": False}

    def _never(messages, tools, on_token):
        never["called"] = True
        raise AssertionError("must not be reached")
    pre = CancelToken()
    pre.cancel("stopped before it began")
    rep = run_workloop("x", "m", ws("cancel-pre"), journal=jr("r5b"), token=pre,
                       chat_fn=_never, execute_tool_fn=tool_ok, checkpoint_fn=None)
    assert rep.stop_reason == STOP_CANCELLED and not never["called"]

    # (6) PERMISSIONS.
    # gate_policy="deny": run_command is refused, counted, and the model is
    # told; the run keeps working. And because the refused action fingerprints
    # identically every turn, the stall detector is what ends it.
    d6 = ws("gate-deny")
    rep = run_workloop(
        "shell out", "m", d6, journal=jr("r6"),
        chat_fn=scripted([("try", [("run_command", {"command": "rm -rf /"})])] * 20),
        execute_tool_fn=lambda n, a, w: (_ for _ in ()).throw(
            AssertionError("a denied tool must never execute")),
        checkpoint_fn=None, gate_policy="deny",
        stall=StallPolicy(window=3, repeat_actions=3, min_turns=2),
        ceilings=Ceilings(max_turns=50))
    assert rep.stop_reason == STOP_STALLED, (rep.stop_reason, rep.stop_detail)
    assert rep.gates_denied >= 3 and rep.denied_tools.get("run_command"), rep.denied_tools
    assert "needed approval" in rep.render()

    # a pre-authorised command head DOES run unattended: that is the escape
    # hatch that makes "deny by default" workable instead of useless
    d6b = ws("gate-allow")
    ran6 = []
    rep = run_workloop(
        "run git", "m", d6b, journal=jr("r6b"),
        chat_fn=scripted([("go", [("run_command", {"command": "git status"})])]),
        execute_tool_fn=lambda n, a, w: (ran6.append(a["command"]) or "clean"),
        checkpoint_fn=None, auto_allow={"git"}, ceilings=Ceilings(max_turns=1))
    assert ran6 == ["git status"], ("auto_allow is the pre-authorised scope", ran6)
    assert rep.gates_denied == 0

    # gate_policy="stop" ends the run at the first gate, naming the command
    d6c = ws("gate-stop")
    rep = run_workloop(
        "shell out", "m", d6c, journal=jr("r6c"),
        chat_fn=scripted([("try", [("run_command", {"command": "curl evil.example"})])]),
        execute_tool_fn=tool_ok, checkpoint_fn=None, gate_policy="stop",
        ceilings=Ceilings(max_turns=9))
    assert rep.stop_reason == STOP_BLOCKED, (rep.stop_reason, rep.stop_detail)
    assert "run_command needs approval" in rep.stop_detail
    assert "curl evil.example" in rep.stop_detail
    assert "stopped needing permission" in rep.headline()

    # gate_policy="ask" consults the callback and honours both answers
    for answer, expect_ran in ((True, True), (False, False)):
        dd6 = ws("gate-ask-{}".format(answer))
        ran = []
        run_workloop("ask", "m", dd6, journal=jr("r6d{}".format(answer)),
                     chat_fn=scripted([("go", [("run_command", {"command": "ls"})])]),
                     execute_tool_fn=lambda n, a, w: (ran.append(1) or "out"),
                     checkpoint_fn=None, gate_policy="ask",
                     approve_fn=lambda n, a: answer, ceilings=Ceilings(max_turns=1))
        assert bool(ran) is expect_ran, (answer, ran)

    # the capability manifest is a hard cap: a tool outside it is denied even
    # though the mode would otherwise allow it
    d6e = ws("manifest")
    rep = run_workloop(
        "write", "m", d6e, journal=jr("r6e"),
        chat_fn=scripted([("go", [("write_file", {"path": "a", "content": "b"})])]),
        execute_tool_fn=lambda n, a, w: (_ for _ in ()).throw(
            AssertionError("a tool outside the manifest must never execute")),
        checkpoint_fn=None, allowed_tools={"read_file"}, ceilings=Ceilings(max_turns=1))
    assert rep.stop_reason == STOP_CEILING, rep.stop_reason
    assert rep.writes == 0

    # (7) COMPLETION IS NOT THE MODEL'S OPINION. It says GOAL COMPLETE on
    # every turn and the check refuses until the artifact actually exists.
    d7 = ws("veto")
    turns7 = {"n": 0}

    def liar(messages, tools, on_token):
        turns7["n"] += 1
        if turns7["n"] >= 3:
            write(d7, "out.md", "the real thing")
        return {"role": "assistant", "content": DONE_SENTINEL}, 1, 1
    rep = run_workloop("write out.md", "m", d7, journal=jr("r7"), chat_fn=liar,
                       execute_tool_fn=tool_ok, checkpoint_fn=None,
                       required_artifacts=["out.md"], ceilings=Ceilings(max_turns=20),
                       stall=StallPolicy(window=0, repeat_actions=0, repeat_errors=0,
                                         oscillations=0))
    assert rep.stop_reason == STOP_COMPLETED, (rep.stop_reason, rep.stop_detail)
    assert rep.turns == 3, ("two claims vetoed, the third verified", rep.turns)

    # with no check configured, a claim is accepted but the report SAYS it was
    # never verified rather than implying it was
    rep = run_workloop("do a thing", "m", ws("unverified"), journal=jr("r7b"),
                       chat_fn=scripted([(DONE_SENTINEL, [])]),
                       execute_tool_fn=tool_ok, checkpoint_fn=None)
    assert rep.stop_reason == STOP_COMPLETED
    assert rep.completion["verified"] is False
    assert "unverified" in rep.stop_detail, rep.stop_detail

    # (8) A BROKEN CHECKPOINT STORE degrades, it does not end the run.
    def bad_ck(workspace, label=None, timestamp=None):
        raise RuntimeError("git is not available")
    rep = run_workloop("x", "m", ws("badck"), journal=jr("r8"),
                       chat_fn=scripted([("t", [])] * 3), execute_tool_fn=tool_ok,
                       checkpoint_fn=bad_ck, ceilings=Ceilings(max_turns=2),
                       stall=StallPolicy(window=0, repeat_actions=0, repeat_errors=0,
                                         oscillations=0))
    assert rep.stop_reason == STOP_CEILING, rep.stop_reason
    assert "git is not available" in rep.checkpoint_error
    assert "checkpoints UNAVAILABLE" in rep.render()

    # (9) AN ENGINE THAT RAISES ends the run with STOP_ERROR and a reason,
    # never with an unhandled traceback.
    def boom(messages, tools, on_token):
        raise ConnectionRefusedError("engine is down")
    rep = run_workloop("x", "m", ws("boom"), journal=jr("r9"), chat_fn=boom,
                       execute_tool_fn=tool_ok, checkpoint_fn=None)
    assert rep.stop_reason == STOP_ERROR and "engine is down" in rep.stop_detail
    assert rep.stop_reason in STOP_REASONS

    # a tool that raises is a tool RESULT, not the end of the run
    rep = run_workloop("x", "m", ws("toolboom"), journal=jr("r9b"),
                       chat_fn=scripted([("go", [("read_file", {"path": "a"})])] * 3),
                       execute_tool_fn=lambda n, a, w: (_ for _ in ()).throw(
                           OSError("disk on fire")),
                       checkpoint_fn=None, ceilings=Ceilings(max_turns=2),
                       stall=StallPolicy(window=0, repeat_actions=0, repeat_errors=0,
                                         oscillations=0))
    assert rep.stop_reason == STOP_CEILING, rep.stop_reason
    assert "disk on fire" in rep.last_error, rep.last_error

    # (10) CRASH RECOVERY. Run two turns, simulate the process dying inside
    # turn 3, then resume: turn 3 is never resumed, it is recorded as
    # interrupted, and the ledger spans the restart.
    d10 = ws("resume")
    j10 = jr("r10")
    rep = run_workloop(
        "long job", "m", d10, run_id="r10", journal=j10,
        chat_fn=scripted([("t1", [("write_file", {"path": "a.txt", "content": "1"})]),
                          ("t2", [("write_file", {"path": "b.txt", "content": "2"})])]),
        execute_tool_fn=writing_tool, checkpoint_fn=fake_ck,
        ceilings=Ceilings(max_turns=2),
        stall=StallPolicy(window=0, repeat_actions=0, repeat_errors=0, oscillations=0))
    assert rep.stop_reason == STOP_CEILING and rep.turns == 2
    j10.append({"t": "turn_start", "turn": 3})  # the process dies here

    after_restart = scripted([("t3", [("write_file", {"path": "c.txt",
                                                      "content": "3"})])])
    chats_after = {"n": 0}

    def counted_chat(messages, tools, on_token):
        chats_after["n"] += 1
        return after_restart(messages, tools, on_token)
    resumed = run_workloop(
        "long job", "m", d10, run_id="r10", journal=j10, resume=True,
        chat_fn=counted_chat,
        execute_tool_fn=writing_tool, checkpoint_fn=fake_ck,
        ceilings=Ceilings(max_turns=4),
        stall=StallPolicy(window=0, repeat_actions=0, repeat_errors=0, oscillations=0))
    assert resumed.turns == 4, ("resumed at turn 3, not turn 1", resumed.turns)
    # The count is what actually proves work was not repeated: turns 1 and 2
    # were already done, so a correct resume calls the model exactly twice
    # (turns 3 and 4). Restarting from turn 1 would call it four times and
    # still reach turns == 4, which is why the turn total alone proves nothing.
    assert chats_after["n"] == 2, (
        "a resumed run must not redo turns the journal already recorded as "
        "complete; the model was called {} times, expected 2".format(chats_after["n"]))
    assert [row["turn"] for row in resumed.turn_log] == [1, 2, 3, 4], \
        ("the account must show one row per turn of the WHOLE run, restored rows "
         "included and none repeated", [r["turn"] for r in resumed.turn_log])
    assert any("interrupted by a restart" in n for n in resumed.notices), resumed.notices
    assert any("NOT resumed" in n for n in resumed.notices), resumed.notices
    assert resumed.tokens_in >= 30, ("prior turns' tokens must survive the restart",
                                     resumed.tokens_in)
    assert len(resumed.checkpoints) >= 3, resumed.checkpoints
    assert "interrupted" in resumed.render()
    recs = load_journal("r10", j10.path)["records"]
    assert any(r.get("t") == "interrupted" and r.get("turn") == 3 for r in recs), \
        "the interruption must be written into the journal, not just the report"
    # the ledger really did span the restart rather than starting fresh
    assert len(resumed.turn_log) >= 4, resumed.turn_log

    # a workspace edited while the loop was down is noticed, not silently
    # folded into the model's progress
    d10b = ws("resume-edited")
    j10b = jr("r10b")
    run_workloop("j", "m", d10b, run_id="r10b", journal=j10b,
                 chat_fn=scripted([("t1", [("write_file", {"path": "a.txt",
                                                           "content": "1"})])]),
                 execute_tool_fn=writing_tool, checkpoint_fn=None,
                 ceilings=Ceilings(max_turns=1),
                 stall=StallPolicy(window=0, repeat_actions=0, repeat_errors=0,
                                   oscillations=0))
    write(d10b, "meddled.txt", "a human did this")
    r10b = run_workloop("j", "m", d10b, run_id="r10b", journal=j10b, resume=True,
                        chat_fn=scripted([("t2", [])]), execute_tool_fn=tool_ok,
                        checkpoint_fn=None, ceilings=Ceilings(max_turns=2),
                        stall=StallPolicy(window=0, repeat_actions=0, repeat_errors=0,
                                          oscillations=0))
    assert any("changed while the loop was not running" in n for n in r10b.notices), \
        r10b.notices

    # (11) THE REPORT is machine-readable as well as legible, and every run
    # ends with exactly one recognised stop reason.
    d11 = ws("report")
    rep = run_workloop("report goal", "m", d11, journal=jr("r11"),
                       chat_fn=scripted([("t", [])] * 3), execute_tool_fn=tool_ok,
                       checkpoint_fn=fake_ck, ceilings=Ceilings(max_turns=2),
                       stall=StallPolicy(window=0, repeat_actions=0, repeat_errors=0,
                                         oscillations=0))
    dj = rep.to_dict()
    json.dumps(dj)  # must be JSON-safe for the sidecar to put it on the wire
    for key in ("run_id", "goal", "stop_reason", "turns", "tokens", "verdict",
                "turn_log", "ceilings", "created", "modified", "deleted"):
        assert key in dj, key
    assert dj["stop_reason"] in STOP_REASONS
    assert dj["mode"] == "auto" and dj["mode"] != "bypass"
    body = rep.render()
    for must in ("hearth work loop", "why it stopped", "what it spent",
                 "what changed in the workspace", "turn by turn",
                 "bypass is not reachable"):
        assert must in body, must
    assert "CHANGE, not correctness" in body, \
        "the report must state the limits of its own progress detection"

    # (12) The default manifest cannot reach anything that leaves the machine.
    for dangerous in ("http_request", "web_fetch", "web_search", "fetch_to_kb",
                      "write_self_config", "nix_check"):
        assert dangerous not in DEFAULT_ALLOWED_TOOLS, dangerous
    for t_ in DEFAULT_ALLOWED_TOOLS:
        assert t_ in permissions.RISK, ("manifest names a tool permissions has never "
                                        "heard of, so it would fail closed as "
                                        "dangerous", t_)
    # and under the real permission engine the default run is exactly
    # "read and write files, ask before anything else"
    for t_ in sorted(DEFAULT_ALLOWED_TOOLS):
        got = permissions.decide("auto", t_, {}, (), allowed_tools=DEFAULT_ALLOWED_TOOLS)
        assert got in ("allow", "gate"), (t_, got)
        if permissions.risk_of(t_) == "dangerous":
            assert got == "gate", (t_, got)

    # (13) A PATIENCE SETTING CANNOT WIDEN A CEILING. The most patient preset
    # there is, on a run that never finishes and never trips a detector, must
    # still stop at every ceiling, at exactly the value the ceiling names.
    # Patience decides when a run is judged pointless; it has no vote on what
    # the run is allowed to spend, and this is the test that says so out loud.
    _patient = stall_policy_for(PATIENCE_ORDER[-1])

    def _bounded(chat, limit):
        """A chat_fn that refuses to be called more than `limit` times.

        Without it, the natural way to break ceiling enforcement -- letting a
        patient policy skip the check -- produces a run that never ends, and
        the test HANGS instead of failing. A test that can only fail by never
        returning is not a test anyone will trust at 3am, so a runaway is
        turned into a named, immediate failure here."""
        n = {"i": 0}

        def _chat(messages, tools, on_token):
            n["i"] += 1
            if n["i"] > limit:
                raise AssertionError(
                    "ran {} turns without reaching any ceiling: a patience setting "
                    "must never buy even one turn past one".format(n["i"]))
            return chat(messages, tools, on_token)
        return _chat

    def _patient_run(name, ceilings, limit=25, **kw):
        chat = kw.pop("chat_fn", scripted([("thinking", [])]))
        return run_workloop("endless", "m", ws("patient-" + name),
                            journal=jr("p-" + name),
                            chat_fn=_bounded(chat, limit),
                            execute_tool_fn=kw.pop("execute_tool_fn", tool_ok),
                            checkpoint_fn=None, ceilings=ceilings, stall=_patient, **kw)

    r13 = _patient_run("turns", Ceilings(max_turns=3))
    assert r13.stop_reason == STOP_CEILING and r13.turns == 3, \
        ("the most patient stall setting must not buy a single turn beyond the turn "
         "ceiling", r13.stop_reason, r13.turns, r13.stop_detail)
    assert "turns ceiling" in r13.stop_detail, r13.stop_detail

    r13b = _patient_run("tokens", Ceilings(max_turns=999, max_tokens=100))
    assert r13b.stop_reason == STOP_CEILING and "tokens ceiling" in r13b.stop_detail, \
        (r13b.stop_reason, r13b.stop_detail)

    _clk13 = {"t": 0.0}
    r13c = _patient_run("wall", Ceilings(max_turns=999, max_seconds=10),
                        clock=lambda: _clk13.__setitem__("t", _clk13["t"] + 4.0)
                        or _clk13["t"])
    assert r13c.stop_reason == STOP_CEILING and "wall clock" in r13c.stop_detail, \
        (r13c.stop_reason, r13c.stop_detail)

    d13 = ws("patient-writes")
    r13d = run_workloop(
        "write forever", "m", d13, journal=jr("p-writes"),
        chat_fn=_bounded(scripted(
            [("t", [("write_file", {"path": "a.txt", "content": "1"})]),
             ("t", [("write_file", {"path": "b.txt", "content": "2"})]),
             ("t", [("write_file", {"path": "c.txt", "content": "3"})])]), 25),
        execute_tool_fn=writing_tool, checkpoint_fn=None,
        ceilings=Ceilings(max_turns=999, max_writes=2), stall=_patient)
    assert r13d.stop_reason == STOP_CEILING and "unattended writes" in r13d.stop_detail, \
        ("the unattended write budget binds the most patient run too",
         r13d.stop_reason, r13d.stop_detail)

    # ...and the account names which patience setting was in force, because
    # "stopped making progress" means something different at each one.
    assert r13.patience == PATIENCE_ORDER[-1]
    assert r13.to_dict()["patience"] == PATIENCE_ORDER[-1]
    assert "patience   {}".format(PATIENCE_ORDER[-1]) in r13.render(), r13.render()[:400]

    # A stalled run at a less patient setting says the judgement was tunable
    # and how much budget was left, rather than presenting it as a fact about
    # the work.
    d13e = ws("patience-stalled")
    r13e = run_workloop(
        "go in circles", "m", d13e, journal=jr("p-stalled"),
        chat_fn=scripted([("thinking", [("read_file", {"path": "a"})])]),
        execute_tool_fn=tool_ok, checkpoint_fn=None,
        ceilings=Ceilings(max_turns=50),
        stall=stall_policy_for("give_up_early"))
    assert r13e.stop_reason == STOP_STALLED, (r13e.stop_reason, r13e.stop_detail)
    assert r13e.patience == "give_up_early"
    _body13 = r13e.render()
    assert "more patient setting would have kept going" in _body13, _body13[:600]
    # The number it names is what is LEFT, not what was used. Reporting the
    # spend here and calling it unspent would tell a reader the run had far
    # less budget remaining than it did, which is the exact fact the sentence
    # exists to give them.
    assert "{} of 50 turns unspent".format(50 - r13e.turns) in _body13, _body13[:600]
    assert r13e.turns < 50

    shutil.rmtree(tmp_root, ignore_errors=True)
    print("hearth-workloop self-test OK")
    return 0


def _cli_stall_policy(patience, window):
    """The named preset, with --stall-window overriding its window if given.

    One place so the CLI cannot end up meaning something different from the
    sidecar, which does the same thing in loop_engine.parse_loop_config."""
    policy = stall_policy_for(patience)
    if window is not None:
        policy.window = window
    return policy


def main(argv=None):
    p = argparse.ArgumentParser(prog="hearth-workloop")
    p.add_argument("goal", nargs="?")
    p.add_argument("--model", default="auto")
    p.add_argument("--workspace", default=".")
    p.add_argument("--mode", default="auto", choices=list(ALLOWED_MODES))
    p.add_argument("--gate-policy", default="deny", choices=list(GATE_POLICIES))
    p.add_argument("--allow-command", action="append", default=[],
                   help="pre-authorise a command head to run unattended (repeatable)")
    p.add_argument("--tool", action="append", default=[],
                   help="capability manifest; repeatable. Defaults to the safe set.")
    p.add_argument("--done-command", default=None,
                   help="shell command whose exit status 0 means the goal is done")
    p.add_argument("--artifact", action="append", default=[],
                   help="file that must exist and be non-empty for the goal to be done")
    p.add_argument("--max-turns", type=int, default=40)
    p.add_argument("--max-seconds", type=int, default=7200)
    p.add_argument("--max-tokens", type=int, default=1000000)
    p.add_argument("--max-writes", type=int, default=200)
    p.add_argument("--patience", default=DEFAULT_PATIENCE, choices=list(PATIENCE_ORDER),
                   help="how long to keep going without visible progress; "
                        "--stall-window overrides this preset's window")
    p.add_argument("--stall-window", type=int, default=None,
                   help="turns without a new workspace state before stopping; "
                        "0 switches that detector off")
    p.add_argument("--run-id", default=None)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--ollama-url", default=DEFAULT_OLLAMA)
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    p.add_argument("--quiet", action="store_true", help="do not stream tokens")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.goal:
        p.error("a goal is required unless --self-test")

    token = CancelToken()

    def _emit(ev):
        if a.quiet:
            return
        kind = ev.get("type")
        if kind == "delta":
            sys.stdout.write(ev.get("text") or "")
            sys.stdout.flush()
        elif kind == "turn_start":
            sys.stdout.write("\n\n--- turn {} ---\n".format(ev.get("turn")))
        elif kind == "tool_call":
            sys.stdout.write("\n  [{}]\n".format(ev.get("tool")))
        elif kind == "notice":
            sys.stdout.write("\n  ({})\n".format(ev.get("detail")))

    report = run_workloop(
        a.goal, a.model, a.workspace, mode=a.mode, gate_policy=a.gate_policy,
        auto_allow=set(a.allow_command), allowed_tools=(set(a.tool) or None),
        done_command=a.done_command, required_artifacts=a.artifact,
        ceilings=Ceilings(max_turns=a.max_turns, max_seconds=a.max_seconds,
                          max_tokens=a.max_tokens, max_writes=a.max_writes),
        stall=_cli_stall_policy(a.patience, a.stall_window),
        ollama_url=a.ollama_url, token=token, emit=_emit,
        run_id=a.run_id, resume=a.resume)
    sys.stdout.write("\n\n")
    print(json.dumps(report.to_dict(), indent=2) if a.json else report.render())
    return 0 if report.stop_reason == STOP_COMPLETED else 1


if __name__ == "__main__":
    sys.exit(main())
