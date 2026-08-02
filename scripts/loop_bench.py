#!/usr/bin/env python3
"""Measure what the work loop's stall detectors cost and what they save.

WHY THIS EXISTS
===============

docs/agent-swarm.md carries a measurement that says the swarm lost to a single
work loop, and one line inside it says something more useful than the verdict
it was built to reach:

    8 of 8 default single runs stopped for `stalled`, at a mean of 5.5 turns
    out of 18 allowed.

A loop that gives up less than a third of the way into a budget the user
already granted is not being careful, it is being wrong, and the arm with
every detector switched off passed the one solvable task 2 times in 3 while
the default arm passed it 0 times in 3.

That earlier comparison was reproducible only in the sense that its numbers
were written down: its harness was a scratch file. This module is that harness,
kept, so the tuning it produced can be re-run and disagreed with.

THE METHOD, AND WHY REPLAY IS EXACT RATHER THAN APPROXIMATE
===========================================================

Tuning five thresholds by re-running the model once per candidate is both
ruinously slow and statistically useless: a 7B on a coding task varies more
between two identical runs than between two neighbouring policies.

It is also unnecessary, because of a property of hearth_workloop worth stating
plainly:

    A StallPolicy has EXACTLY ONE effect on a run: it decides the turn on
    which the run stops. It reaches nothing else.

Read run_workloop: `ledger.verdict()` is consulted once per turn, after the
turn is complete, and its only consequence is `_finish(STOP_STALLED, ...)`.
No verdict, signal or threshold is ever appended to `messages`, so the model
cannot know which policy it is running under. The turn sequence a run produces
is therefore identical under every policy, up to the turn the policy ends it.

So: run the model ONCE per task with every detector off, record each turn's
workspace digest, action fingerprint and error signatures, and then replay
that recording through `ProgressLedger` for any candidate policy. The turn a
policy would have stopped on is not an estimate of the counterfactual, it IS
the counterfactual, computed by the same detector code the product ships.
Every candidate sees the identical trace, which removes run-to-run variance
from the comparison entirely.

What replay cannot do is prove that the tuned default behaves as measured when
it is really in charge, because a bug in the wiring (a policy that never
reaches the run) is invisible to a method that never uses the wiring. The
`live` subcommand exists for that, and the tuning claim rests on both.

THE TWO DIRECTIONS, BOTH OF WHICH COST THE USER
===============================================

Patience is not free and neither is impatience, so a candidate is judged on
both at once:

  solvable tasks     did the run reach the deterministic gate before the
                     policy stopped it? (giving up too early)
  hopeless tasks     how many turns did it burn before stopping? (grinding
                     on something that will never pass)

`roman` and `oracle` are hopeless in two different ways on purpose. `roman` is
EMPIRICALLY hopeless: an ordinary task this 7B simply cannot get right, which
is the common case. `oracle` is STRUCTURALLY hopeless: it asks for values the
model cannot possibly derive.

`oracle` then measured something better than it was aimed at. It runs to its
ceiling under every patience setting, because its gate fails without using any
word that looks like an error, so the error detector has nothing to repeat.
See the comment on the task itself: a completion check that fails quietly is
invisible to stall detection, and only a ceiling will stop that run.

The hidden test suites live outside the workspace and are invoked by absolute
path, so the model never reads them and only ever sees failure output, which
is the situation a real user is in.

Standard library only.
"""

import argparse
import json
import os
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "agent"))

import hearth_workloop as W  # noqa: E402

DEFAULT_MODEL = "qwen2.5-coder:latest"
DEFAULT_TURNS = 18
DEFAULT_SECONDS = 900
DEFAULT_TOKENS = 120000


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------

TASKS = {
    # --- solvable: the regime where giving up early actually costs something.
    "interval": {
        "kind": "solvable",
        "module": "intervals.py",
        "goal": ("Create a file `intervals.py` in this workspace defining "
                 "`merge_intervals(intervals)` which takes a list of (start, end) "
                 "tuples and returns the merged, sorted, non-overlapping list. "
                 "Intervals that merely touch, like (1,2) and (2,3), merge into "
                 "(1,3). An empty list returns an empty list. Raise ValueError if "
                 "any interval has start > end."),
        "tests": '''
import sys
sys.path.insert(0, sys.argv[1])
from intervals import merge_intervals
assert merge_intervals([])==[]
assert merge_intervals([(1,3),(2,6),(8,10)])==[(1,6),(8,10)]
assert merge_intervals([(1,2),(2,3)])==[(1,3)]
assert merge_intervals([(5,6),(1,2)])==[(1,2),(5,6)]
assert merge_intervals([(1,10),(2,3)])==[(1,10)]
assert merge_intervals([(1,4),(4,5),(5,9)])==[(1,9)]
assert merge_intervals([(1,1)])==[(1,1)]
try:
    merge_intervals([(5,1)]); raise SystemExit('no ValueError')
except ValueError: pass
print('ALL INTERVAL TESTS PASSED')
''',
    },
    # `rle` was WRITTEN as a second, easier solvable task and the measurement
    # disagreed: on qwen2.5-coder 7B it passed 0 of 3 recorded 18-turn runs
    # with every detector switched off, which is the arm that cannot give up.
    # It is labelled by what it measured rather than by what it was for, so
    # the aggregate columns stay meaningful; its per-task row still shows the
    # 0/N that earned the label.
    "rle": {
        "kind": "hopeless",
        "module": "rle.py",
        "goal": ("Create a file `rle.py` in this workspace defining `encode(s)` and "
                 "`decode(s)` for run-length encoding. encode('aaabbc') is 'a3b2c1': "
                 "every run becomes the character followed by its count, including "
                 "runs of length one. decode is the exact inverse and must handle "
                 "counts of more than one digit. Both return '' for ''. decode must "
                 "raise ValueError on input that is not a valid encoding."),
        "tests": '''
import sys
sys.path.insert(0, sys.argv[1])
from rle import encode, decode
assert encode('')==''
assert encode('a')=='a1'
assert encode('aaabbc')=='a3b2c1'
assert encode('abc')=='a1b1c1'
assert encode('zzzzzzzzzzzz')=='z12'
assert decode('')==''
assert decode('a3b2c1')=='aaabbc'
assert decode('z12')=='zzzzzzzzzzzz'
for s in ('', 'a', 'aaabbc', 'abc', 'zzzzzzzzzzzz', 'aabbaa'):
    assert decode(encode(s))==s, s
for bad in ('3a', 'a', 'a3b'):
    try:
        decode(bad); raise SystemExit('no ValueError for %r' % (bad,))
    except ValueError: pass
print('ALL RLE TESTS PASSED')
''',
    },
    # --- hopeless, empirically. Nothing passed this in any arm of the swarm
    # comparison. A run that cannot win should end promptly, not at the ceiling.
    "roman": {
        "kind": "hopeless",
        "module": "roman.py",
        "goal": ("Create a file `roman.py` in this workspace defining `to_roman(n)` "
                 "converting an integer 1..3999 to a Roman numeral string, and "
                 "`from_roman(s)` converting a Roman numeral string back to an "
                 "integer. Both must raise ValueError on out-of-range or malformed "
                 "input, including non-canonical numerals like 'IIII' or 'VV'."),
        "tests": '''
import sys
sys.path.insert(0, sys.argv[1])
from roman import to_roman, from_roman
assert to_roman(1)=='I' and to_roman(4)=='IV' and to_roman(9)=='IX'
assert to_roman(14)=='XIV' and to_roman(40)=='XL' and to_roman(90)=='XC'
assert to_roman(400)=='CD' and to_roman(900)=='CM'
assert to_roman(3999)=='MMMCMXCIX' and to_roman(1994)=='MCMXCIV'
assert to_roman(2024)=='MMXXIV' and to_roman(58)=='LVIII'
for n in (1, 4, 9, 14, 40, 400, 1994, 2024, 3999):
    assert from_roman(to_roman(n))==n, n
for bad in (0, 4000, -1):
    try:
        to_roman(bad); raise SystemExit('no ValueError for %r' % (bad,))
    except ValueError: pass
for bad in ('IIII','VV','ABC','','MMMM','IC'):
    try:
        from_roman(bad); raise SystemExit('no ValueError for %r' % (bad,))
    except ValueError: pass
print('ALL ROMAN TESTS PASSED')
''',
    },
    # --- hopeless, structurally. The checker holds values nothing in the
    # workspace implies, and reports only HOW MANY are wrong, never which or
    # what they should be, so no amount of reading the failure output helps.
    # This looks like the case stall detection exists for: a run that cannot
    # converge and has no way to learn that it cannot.
    #
    # MEASURED, AND NOT WHAT WAS EXPECTED. This task runs to its full turn
    # ceiling under EVERY patience setting, the most eager included, and the
    # reason is the wording of the failure rather than anything about
    # patience: "predict is wrong at 3 of 6 indices" contains no word in
    # hearth_workloop._ERROR_MARKERS, so extract_errors returns nothing, no
    # error signature is ever recorded, and repeat_error has nothing to
    # repeat. A gate that fails QUIETLY is invisible to the one detector that
    # would have caught this, and only a ceiling ends the run.
    #
    # The task is kept exactly as it is, because that finding is worth more
    # than the cost number it was written to produce. It ships as the
    # completion-check blind spot in PROGRESS_BLIND_SPOTS.
    "oracle": {
        "kind": "hopeless",
        "module": "oracle.py",
        "goal": ("Create a file `oracle.py` in this workspace defining `predict(i)` "
                 "returning the i-th value, for i from 0 to 5, of the sequence the "
                 "hidden test suite expects. The test suite is not in this "
                 "workspace and reports only how many indices are wrong."),
        "tests": '''
import sys
sys.path.insert(0, sys.argv[1])
from oracle import predict
SECRET = [93, 17, 58, 4, 71, 39]
bad = 0
for i, v in enumerate(SECRET):
    try:
        got = predict(i)
    except Exception:
        got = None
    if got != v:
        bad += 1
if bad:
    raise SystemExit('predict is wrong at %d of %d indices' % (bad, len(SECRET)))
print('ALL ORACLE TESTS PASSED')
''',
    },
}

SOLVABLE = tuple(k for k, t in TASKS.items() if t["kind"] == "solvable")
HOPELESS = tuple(k for k, t in TASKS.items() if t["kind"] == "hopeless")


def _tests_dir(root=None):
    return os.path.join(root or tempfile.gettempdir(), "hearth-loop-bench-tests")


def write_tests(root=None):
    """Write every hidden checker OUTSIDE any workspace. Returns the directory."""
    d = _tests_dir(root)
    os.makedirs(d, exist_ok=True)
    for name, task in TASKS.items():
        with open(os.path.join(d, "check_%s.py" % name), "w", encoding="utf-8") as fh:
            fh.write(task["tests"].lstrip())
    return d


def done_command(task, tests_dir):
    """The deterministic gate: the hidden checker, given the workspace as
    argv[1] so it can import what the agent wrote without the agent ever being
    able to read the checker."""
    return '"%s" "%s" "%%CD%%"' % (
        sys.executable, os.path.join(tests_dir, "check_%s.py" % task))


def make_workspace(task):
    """A fresh workspace, and a journal path OUTSIDE it.

    The journal must not live in the workspace, and this is not a tidiness
    preference: `scan_workspace` fingerprints every file in the tree, the
    journal is appended on every turn, so a journal inside the workspace makes
    the digest change every single turn no matter what the model did. That
    silently disables the two detectors that look for a workspace which has
    stopped moving -- in the harness built to measure exactly those detectors.
    The product itself never does this (`journal_dir()` is under the data
    directory), so it would have been a defect purely of the measurement."""
    d = tempfile.mkdtemp(prefix="loopbench-%s-" % task)
    ws = os.path.join(d, "ws")
    os.makedirs(ws, exist_ok=True)
    # A seed file, so the workspace is not empty: an empty tree makes the very
    # first fingerprint trivially "new state" and skews the first turn.
    with open(os.path.join(ws, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("# scratch workspace\n\nPut the module described in the goal here.\n")
    return ws, os.path.join(d, "journal.jsonl")


def ceilings(turns, seconds, tokens):
    return W.Ceilings(max_turns=turns, max_seconds=seconds, max_tokens=tokens,
                      max_writes=60, max_tool_calls=200)


# --------------------------------------------------------------------------
# recording a trace
# --------------------------------------------------------------------------

# Every detector off. This is the arm that produces a trace long enough to
# replay any candidate against: a run stopped at turn 5 cannot answer what a
# more patient policy would have done with turn 6.
TRACE_POLICY = W.StallPolicy(window=0, repeat_actions=0, repeat_errors=0,
                             oscillations=0, min_turns=0)


def record_trace(task, *, model, ollama_url, turns, seconds, tokens, tests_dir):
    """One live run with stall detection off, kept turn by turn."""
    ws, journal_path = make_workspace(task)
    journal = W.Journal(journal_path, fsync=False)
    t0 = time.monotonic()
    rep = W.run_workloop(
        TASKS[task]["goal"], model, ws, mode="auto",
        ceilings=ceilings(turns, seconds, tokens), stall=TRACE_POLICY,
        done_command=done_command(task, tests_dir), done_timeout=60,
        required_artifacts=[TASKS[task]["module"]],
        ollama_url=ollama_url, checkpoint_every_turn=False, journal=journal)
    completed = rep.stop_reason == W.STOP_COMPLETED and bool(rep.completion.get("done"))
    rows = []
    for rec in W.load_journal("", journal.path)["completed_turns"]:
        log = rec.get("log") or {}
        rows.append({
            "turn": rec.get("turn"),
            "digest": rec.get("digest", ""),
            "actions_raw": rec.get("actions_raw") or [],
            "errors": rec.get("errors") or [],
            "error_text": rec.get("error_text") or {},
            "tokens": int(rec.get("tokens_in") or 0) + int(rec.get("tokens_out") or 0),
            "seconds": float(log.get("seconds") or 0.0),
        })
    return {
        "task": task, "kind": TASKS[task]["kind"], "model": model,
        "stop_reason": rep.stop_reason, "stop_detail": rep.stop_detail[:200],
        "completed": completed,
        # The turn the deterministic gate passed on, or None. A policy that
        # stops before this turn turned a win into a loss.
        "win_turn": rep.turns if completed else None,
        "turns": rep.turns, "tokens": rep.tokens,
        "seconds": round(time.monotonic() - t0, 1),
        "workspace": ws, "journal_in_workspace": False, "rows": rows,
    }


def trace_from_journal(path, task, kind=None):
    """A trace read back out of a run's own journal.

    A run that was recorded before this harness existed is still replayable,
    because the journal already holds everything a detector reads: each turn's
    digest, its tool calls with arguments, and its error signatures.

    ONLY A RUN WITH EVERY DETECTOR OFF may be imported. A run that was stopped
    at turn 5 by its own stall policy says nothing about what turn 6 would have
    looked like, so replaying a MORE patient policy against it would silently
    score that policy on a trace it never got to produce -- and would flatter
    the eager settings by construction, since the record ends exactly where
    they chose to end it. Refused rather than warned about.
    """
    records = W.Journal(path, fsync=False).read()
    header = next((r for r in records if r.get("t") == "run"), {})
    stall = header.get("stall") or {}
    if any(int(stall.get(k) or 0) != 0 for k in
           ("window", "repeat_actions", "repeat_errors", "oscillations")):
        raise ValueError(
            "{}: this run had stall detection ON ({}), so its record stops where its "
            "own policy stopped it and cannot answer what a more patient policy "
            "would have done".format(os.path.basename(os.path.dirname(path)), stall))
    stop = next((r for r in records if r.get("t") == "stop"), {})
    summary = stop.get("summary") or {}
    completed = (stop.get("reason") == W.STOP_COMPLETED
                 and bool((summary.get("completion") or {}).get("done")))
    rows = []
    for rec in records:
        if rec.get("t") != "turn_end":
            continue
        log = rec.get("log") or {}
        rows.append({
            "turn": rec.get("turn"), "digest": rec.get("digest", ""),
            "actions_raw": rec.get("actions_raw") or [],
            "errors": rec.get("errors") or [],
            "error_text": rec.get("error_text") or {},
            "tokens": int(rec.get("tokens_in") or 0) + int(rec.get("tokens_out") or 0),
            "seconds": float(log.get("seconds") or 0.0),
        })
    return {
        "task": task, "kind": kind or TASKS.get(task, {}).get("kind", "solvable"),
        "model": header.get("model", ""), "stop_reason": stop.get("reason") or "",
        "stop_detail": (stop.get("detail") or "")[:200], "completed": completed,
        "win_turn": summary.get("turns") if completed else None,
        "turns": summary.get("turns") or len(rows),
        "tokens": summary.get("tokens") or sum(r["tokens"] for r in rows),
        "seconds": summary.get("elapsed") or 0.0,
        "workspace": header.get("workspace") or os.path.dirname(path),
        "imported_from": path,
        # Recorded, not remembered: a journal written INSIDE the workspace is
        # fingerprinted along with it, so every turn reads as a change and the
        # two detectors that watch for a workspace which has stopped moving
        # cannot fire at all. Traces with this set true are still sound for
        # everything driven by errors and actions, and say nothing about
        # no_new_state or oscillation.
        "journal_in_workspace": os.path.realpath(os.path.dirname(path)) == os.path.realpath(
            header.get("workspace") or ""),
        "rows": rows,
    }


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------

def replay(trace, policy):
    """What `policy` would have done to this recorded run.

    Returns {"stop_turn": int|None, "detectors": [...], "outcome": str,
    "tokens": int, "seconds": float} where stop_turn is the turn the policy
    would have called stalled, or None if it never fired.

    The turn ORDER here mirrors run_workloop exactly, and the order matters:
    the completion check is acted on BEFORE the stall verdict is consulted, so
    a policy that would fire on the very turn the run won does not take the win
    away. Getting that backwards would understate every patient policy.
    """
    ledger = W.ProgressLedger(policy)
    tokens = 0.0
    seconds = 0.0
    for row in trace["rows"]:
        errs = [(s, row.get("error_text", {}).get(s, s)) for s in row.get("errors") or ()]
        actions = [(a[0], a[1] if len(a) > 1 else {}) for a in row.get("actions_raw") or ()]
        ledger.record(row["turn"], row.get("digest", ""), actions, errs)
        tokens += row.get("tokens") or 0
        seconds += row.get("seconds") or 0.0
        if trace.get("win_turn") == row["turn"]:
            return {"stop_turn": None, "detectors": [], "outcome": "completed",
                    "turn": row["turn"], "tokens": tokens, "seconds": round(seconds, 1)}
        v = ledger.verdict()
        if v["stalled"]:
            return {"stop_turn": row["turn"],
                    "detectors": [d["name"] for d in v["detectors"]],
                    "outcome": "stalled", "turn": row["turn"],
                    "tokens": tokens, "seconds": round(seconds, 1)}
    return {"stop_turn": None, "detectors": [], "outcome": trace.get("stop_reason") or "ran out",
            "turn": trace.get("turns"), "tokens": tokens, "seconds": round(seconds, 1)}


def candidates():
    """The policies the tuning compares, named.

    The shipped presets are read from hearth_workloop so this harness measures
    what the product actually does rather than a copy of it that can drift."""
    out = {
        # The settings the loop first shipped with, kept as a fixed comparison
        # arm rather than read from the module: once the module changes, "what
        # the old default would have done" has to stay computable or the
        # before/after claim becomes unfalsifiable.
        "old default (pre-tuning)": W.StallPolicy(
            window=6, repeat_actions=4, repeat_errors=5, oscillations=3, min_turns=3),
        "detectors off": TRACE_POLICY,
    }
    for name in getattr(W, "PATIENCE_ORDER", ()):
        out["preset: " + name] = W.stall_policy_for(name)
    return out


def sweep_policies(key="repeat_errors", values=(3, 4, 5, 6, 7, 8, 9, 12, 16, 0)):
    """One threshold varied, everything else held at the shipped default.

    The tuning is not really five numbers: on any run with a completion check,
    one of them dominates. A gate that has not passed yet fails the same way
    every turn, so `repeat_errors` alone decides when such a run is stopped,
    and sweeping it draws the actual trade curve between "gave up on a run
    that would have finished" and "ground on a run that never could".
    0 means the detector is switched off."""
    out = {}
    for v in values:
        pol = W.StallPolicy(**dict(W.StallPolicy().to_dict(), **{key: v}))
        out["{}={}".format(key, "off" if v == 0 else v)] = pol
    return out


def replay_table(traces, policies=None):
    """The two-direction table: pass rate on solvable tasks, turns burnt on
    hopeless ones. Returns rows of dicts; `render_table` prints them."""
    policies = policies or candidates()
    rows = []
    for label, policy in policies.items():
        row = {"policy": label, "settings": policy.to_dict(), "per_task": {}}
        for task in sorted({t["task"] for t in traces}):
            sel = [t for t in traces if t["task"] == task]
            results = [replay(t, policy) for t in sel]
            wins = sum(1 for r in results if r["outcome"] == "completed")
            row["per_task"][task] = {
                "kind": TASKS.get(task, {}).get("kind", "?"),
                "n": len(sel), "wins": wins,
                "mean_turns": round(sum(r["turn"] or 0 for r in results) / max(1, len(results)), 1),
                "mean_tokens": round(sum(r["tokens"] for r in results) / max(1, len(results))),
                "mean_seconds": round(sum(r["seconds"] for r in results) / max(1, len(results)), 1),
                "detectors": sorted({d for r in results for d in r["detectors"]}),
            }
        solv = [c for t, c in row["per_task"].items() if c["kind"] == "solvable"]
        hope = [c for t, c in row["per_task"].items() if c["kind"] == "hopeless"]
        row["solvable_wins"] = sum(c["wins"] for c in solv)
        row["solvable_n"] = sum(c["n"] for c in solv)
        row["hopeless_mean_turns"] = round(
            sum(c["mean_turns"] for c in hope) / max(1, len(hope)), 1)
        row["hopeless_mean_tokens"] = round(
            sum(c["mean_tokens"] for c in hope) / max(1, len(hope)))
        rows.append(row)
    return rows


def render_table(rows):
    tasks = sorted({t for r in rows for t in r["per_task"]},
                   key=lambda t: (TASKS.get(t, {}).get("kind", "z"), t))
    out = []
    head = "{:<30}".format("policy")
    for t in tasks:
        head += "{:>16}".format("{} ({})".format(
            t, "S" if TASKS.get(t, {}).get("kind") == "solvable" else "H"))
    head += "{:>14}{:>16}{:>17}".format("solvable", "hopeless turns", "hopeless tokens")
    out.append(head)
    out.append("-" * len(head))
    for r in rows:
        line = "{:<30}".format(r["policy"][:29])
        for t in tasks:
            c = r["per_task"].get(t) or {}
            if not c:
                line += "{:>16}".format("-")
            elif c["kind"] == "solvable":
                line += "{:>16}".format("{}/{} @{}".format(c["wins"], c["n"], c["mean_turns"]))
            else:
                line += "{:>16}".format("{} turns".format(c["mean_turns"]))
        line += "{:>14}{:>16}{:>17,}".format(
            "{}/{}".format(r["solvable_wins"], r["solvable_n"]),
            r["hopeless_mean_turns"], r["hopeless_mean_tokens"])
        out.append(line)
    out.append("")
    out.append("S = solvable: wins/trials @ mean turns spent.  "
               "H = hopeless: mean turns before stopping.")
    return "\n".join(out)


# --------------------------------------------------------------------------
# live confirmation
# --------------------------------------------------------------------------

def run_live(task, *, model, ollama_url, turns, seconds, tokens, stall, tests_dir,
             label=""):
    """One live run under a real policy. This is what replay cannot prove."""
    ws, journal_path = make_workspace(task)
    t0 = time.monotonic()
    rep = W.run_workloop(
        TASKS[task]["goal"], model, ws, mode="auto",
        ceilings=ceilings(turns, seconds, tokens), stall=stall,
        done_command=done_command(task, tests_dir), done_timeout=60,
        required_artifacts=[TASKS[task]["module"]],
        ollama_url=ollama_url, checkpoint_every_turn=False,
        journal=W.Journal(journal_path, fsync=False))
    return {
        "arm": label or "live", "task": task, "kind": TASKS[task]["kind"],
        "passed": rep.stop_reason == W.STOP_COMPLETED and bool(rep.completion.get("done")),
        "stop_reason": rep.stop_reason, "stop_detail": rep.stop_detail[:200],
        "turns": rep.turns, "tokens": rep.tokens,
        "seconds": round(time.monotonic() - t0, 1),
        "stall": stall.to_dict(), "workspace": ws,
        "detectors": [d["name"] for d in (rep.verdict or {}).get("detectors") or []],
    }


def run_swarm_live(task, *, model, ollama_url, turns, seconds, tokens, cycles,
                   tests_dir, label="swarm"):
    """The swarm arm, re-measured. Its phases are work loops, so retuning the
    default stall policy changes the relay too and the published comparison
    has to be re-checked rather than assumed to hold."""
    import hearth_swarmloop as S  # noqa: PLC0415 - optional, only this arm needs it
    ws, journal_path = make_workspace(task)
    t0 = time.monotonic()
    rep = S.run_swarm(
        TASKS[task]["goal"], model, ws, mode="auto",
        ceilings=S.SwarmCeilings(ceilings(turns, seconds, tokens), max_cycles=cycles),
        done_command=done_command(task, tests_dir), done_timeout=60,
        required_artifacts=[TASKS[task]["module"]],
        ollama_url=ollama_url, checkpoint_every_turn=False,
        journal=W.Journal(journal_path, fsync=False))
    return {
        "arm": label, "task": task, "kind": TASKS[task]["kind"],
        "passed": rep.stop_reason == S.STOP_COMPLETED and rep.verified,
        "stop_reason": rep.stop_reason, "stop_detail": rep.stop_detail[:200],
        "turns": rep.turns, "tokens": rep.tokens,
        "seconds": round(time.monotonic() - t0, 1), "workspace": ws,
        "phases": [(p.role, p.report.stop_reason, p.report.turns) for p in rep.phases],
    }


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def _save(path, payload):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def _cmd_trace(a):
    tests_dir = write_tests()
    traces = []
    if a.append and os.path.exists(a.out):
        with open(a.out, encoding="utf-8") as fh:
            traces = json.load(fh).get("traces") or []
    for task in a.tasks.split(","):
        for trial in range(a.trials):
            tr = record_trace(task, model=a.model, ollama_url=a.ollama_url,
                              turns=a.turns, seconds=a.seconds, tokens=a.tokens,
                              tests_dir=tests_dir)
            tr["trial"] = trial
            traces.append(tr)
            # Written after every run: a long recording is exactly the kind of
            # process that gets killed, and losing an hour of completed runs
            # because the file is only written at the end is a harness bug.
            _save(a.out, {"traces": traces})
            print("  trace %-9s t%d  %-10s turns=%-3s win=%-4s tok=%-7s %.0fs" % (
                task, trial, tr["stop_reason"], tr["turns"], tr["win_turn"],
                tr["tokens"], tr["seconds"]))
            sys.stdout.flush()
    return 0


def _cmd_import(a):
    traces = []
    if a.append and os.path.exists(a.out):
        with open(a.out, encoding="utf-8") as fh:
            traces = json.load(fh).get("traces") or []
    for path in a.journals:
        try:
            tr = trace_from_journal(path, a.task)
        except (ValueError, OSError) as exc:
            print("  skipped {}: {}".format(path, exc))
            continue
        traces.append(tr)
        print("  imported %-9s %-10s turns=%-3s win=%s" % (
            tr["task"], tr["stop_reason"], tr["turns"], tr["win_turn"]))
    _save(a.out, {"traces": traces})
    return 0


def _cmd_replay(a):
    traces = []
    for p in a.traces:
        with open(p, encoding="utf-8") as fh:
            traces.extend(json.load(fh).get("traces") or [])
    if not traces:
        print("no traces")
        return 1
    rows = replay_table(traces, sweep_policies(a.sweep) if a.sweep else None)
    dirty = [t for t in traces if t.get("journal_in_workspace")]
    print("traces: {} run(s) over {} task(s)".format(
        len(traces), len({t["task"] for t in traces})))
    if dirty:
        # Said out loud on every run rather than left in a commit message: a
        # reader comparing window or oscillation thresholds on these traces is
        # comparing nothing.
        print("NOTE: {} of them recorded with the journal inside the workspace, so "
              "every turn read as a change. no_new_state and oscillation cannot fire "
              "on those; error- and action-driven results are unaffected.".format(
                  len(dirty)))
    print()
    print(render_table(rows))
    if a.out:
        _save(a.out, {"rows": rows, "trace_count": len(traces)})
    return 0


def _cmd_live(a):
    tests_dir = write_tests()
    rows = []
    if a.append and os.path.exists(a.out):
        with open(a.out, encoding="utf-8") as fh:
            rows = json.load(fh).get("rows") or []
    arms = {}
    for name in a.arms.split(","):
        if name == "current":
            arms[name] = W.StallPolicy(window=6, repeat_actions=4, repeat_errors=5,
                                       oscillations=3, min_turns=3)
        elif name == "off":
            arms[name] = TRACE_POLICY
        else:
            arms[name] = W.stall_policy_for(name)
    for task in a.tasks.split(","):
        for trial in range(a.trials):
            for name, policy in arms.items():
                row = run_live(task, model=a.model, ollama_url=a.ollama_url,
                               turns=a.turns, seconds=a.seconds, tokens=a.tokens,
                               stall=policy, tests_dir=tests_dir, label=name)
                row["trial"] = trial
                rows.append(row)
                _save(a.out, {"rows": rows})
                print("  live %-10s %-9s t%d  pass=%-5s %-9s turns=%-3s tok=%-7s %.0fs" % (
                    name, task, trial, row["passed"], row["stop_reason"],
                    row["turns"], row["tokens"], row["seconds"]))
                sys.stdout.flush()
    return 0


def _cmd_swarm(a):
    tests_dir = write_tests()
    rows = []
    if a.append and os.path.exists(a.out):
        with open(a.out, encoding="utf-8") as fh:
            rows = json.load(fh).get("rows") or []
    for task in a.tasks.split(","):
        for trial in range(a.trials):
            row = run_swarm_live(task, model=a.model, ollama_url=a.ollama_url,
                                 turns=a.turns, seconds=a.seconds, tokens=a.tokens,
                                 cycles=a.cycles, tests_dir=tests_dir)
            row["trial"] = trial
            rows.append(row)
            _save(a.out, {"rows": rows})
            print("  swarm %-9s t%d  pass=%-5s %-9s turns=%-3s tok=%-7s %.0fs" % (
                task, trial, row["passed"], row["stop_reason"], row["turns"],
                row["tokens"], row["seconds"]))
            sys.stdout.flush()
    return 0


def _self_test():
    # === replay is exact about the turn a policy would have stopped on =====
    def trace(digests, errors=None, actions=None, win=None, task="interval"):
        rows = []
        for i, d in enumerate(digests, start=1):
            errs = (errors or {}).get(i, [])
            rows.append({"turn": i, "digest": d,
                         "actions_raw": (actions or {}).get(i, [["write_file", {"n": i}]]),
                         "errors": [s for s, _ in errs],
                         "error_text": {s: t for s, t in errs},
                         "tokens": 100, "seconds": 10.0})
        return {"task": task, "kind": TASKS[task]["kind"], "rows": rows,
                "win_turn": win, "turns": len(digests),
                "stop_reason": "completed" if win else "ceiling"}

    frozen = trace(["A"] * 8)
    r = replay(frozen, W.StallPolicy(window=3, repeat_actions=0, repeat_errors=0,
                                     oscillations=0, min_turns=1))
    assert r["stop_turn"] == 4, ("a workspace that stops moving must be caught on the "
                                 "turn the window fills", r)
    assert r["detectors"] == ["no_new_state"], r

    r_off = replay(frozen, TRACE_POLICY)
    assert r_off["stop_turn"] is None, ("every detector off must never fire", r_off)
    assert r_off["turn"] == 8

    # A policy that would fire on the very turn the run WON must not take the
    # win away: run_workloop acts on the completion check first. Getting this
    # backwards would understate every patient policy in the table.
    won = trace(["A", "A", "A", "A"], win=4)
    r = replay(won, W.StallPolicy(window=3, repeat_actions=0, repeat_errors=0,
                                  oscillations=0, min_turns=1))
    assert r["outcome"] == "completed", ("the completion check is consulted before the "
                                         "stall verdict, so a win on the firing turn "
                                         "stands", r)
    # ...but a policy that fires BEFORE the winning turn does take it away.
    r = replay(won, W.StallPolicy(window=2, repeat_actions=0, repeat_errors=0,
                                  oscillations=0, min_turns=1))
    assert r["outcome"] == "stalled" and r["stop_turn"] == 3, r

    # a run reaching a new state every turn is never stalled by any of these
    moving = trace(["s%d" % i for i in range(1, 13)])
    for label, pol in candidates().items():
        assert replay(moving, pol)["stop_turn"] is None, \
            ("a run changing the workspace every turn must never be called stalled: "
             "{}".format(label))

    # the same error every turn is what repeat_error is for
    errs = {i: [("sig", "AssertionError: nope")] for i in range(1, 13)}
    grind = trace(["s%d" % i for i in range(1, 13)], errors=errs)
    r = replay(grind, W.StallPolicy(window=0, repeat_actions=0, repeat_errors=4,
                                    oscillations=0, min_turns=1))
    assert r["stop_turn"] == 4 and r["detectors"] == ["repeat_error"], r

    # === the table reports both directions ================================
    rows = replay_table([won, grind, trace(["A"] * 18, task="oracle")],
                        {"eager": W.StallPolicy(window=2, repeat_actions=2,
                                                repeat_errors=2, oscillations=2,
                                                min_turns=1),
                         "off": TRACE_POLICY})
    eager = [r for r in rows if r["policy"] == "eager"][0]
    off = [r for r in rows if r["policy"] == "off"][0]
    assert off["solvable_wins"] == 1 and eager["solvable_wins"] == 0, (eager, off)
    assert off["hopeless_mean_turns"] > eager["hopeless_mean_turns"], \
        "an eager policy must burn fewer turns on a hopeless task"
    assert "policy" in render_table(rows)

    # === the shipped presets are ordered by patience ======================
    if hasattr(W, "PATIENCE_ORDER"):
        stops = []
        for name in W.PATIENCE_ORDER:
            stops.append(replay(trace(["A"] * 18, task="oracle"),
                                W.stall_policy_for(name))["turn"])
        assert stops == sorted(stops), \
            ("the presets must be ordered least to most patient, or the name a user "
             "picks does not mean what it says", list(zip(W.PATIENCE_ORDER, stops)))

    # === importing a journal, and the refusal that keeps it honest ========
    tmp = tempfile.mkdtemp(prefix="loopbench-journal-")

    def _journal(name, stall, reason, turns, done):
        p = os.path.join(tmp, name + ".jsonl")
        j = W.Journal(p, fsync=False)
        j.append({"t": "run", "version": 1, "run_id": name, "goal": "g", "model": "m",
                  "mode": "auto", "stall": stall, "ceilings": {}})
        for i in range(1, turns + 1):
            j.append({"t": "turn_start", "turn": i})
            j.append({"t": "turn_end", "turn": i, "digest": "d%d" % i,
                      "actions_raw": [["write_file", {"n": i}]],
                      "errors": ["sig"], "error_text": {"sig": "boom"},
                      "tokens_in": 5, "tokens_out": 5, "log": {"seconds": 1.0}})
        j.append({"t": "stop", "reason": reason,
                  "summary": {"turns": turns, "tokens": turns * 10,
                              "completion": {"done": done}}})
        return p

    off = {"window": 0, "repeat_actions": 0, "repeat_errors": 0,
           "oscillations": 0, "min_turns": 0}
    got = trace_from_journal(_journal("off", off, "completed", 6, True), "interval")
    assert len(got["rows"]) == 6 and got["win_turn"] == 6 and got["completed"], got

    # A run whose own detectors stopped it is REFUSED, not imported with a
    # warning. Its record ends exactly where an eager policy chose to end it,
    # so replaying a more patient policy against it would score that policy on
    # turns the run was never allowed to take -- flattering the eager settings
    # by construction, in the one comparison this whole harness exists to make.
    on = dict(off, repeat_errors=5)
    try:
        trace_from_journal(_journal("on", on, "stalled", 5, False), "interval")
        raise AssertionError("a journal from a run with stall detection ON must be "
                             "refused: its trace is truncated by the very policy "
                             "under test")
    except ValueError as exc:
        assert "stall detection ON" in str(exc), exc

    # === tasks are well formed ============================================
    for name, task in TASKS.items():
        assert task["kind"] in ("solvable", "hopeless"), name
        assert task["module"].endswith(".py") and task["goal"] and task["tests"]
    d = write_tests(tempfile.mkdtemp(prefix="loopbench-selftest-"))
    for name in TASKS:
        p = os.path.join(d, "check_%s.py" % name)
        assert os.path.getsize(p) > 0, p
        compile(open(p, encoding="utf-8").read(), p, "exec")  # a checker must parse
    assert "%CD%" in done_command("interval", d)

    print("loop_bench self-test: OK")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    def common(p, default_out):
        p.add_argument("--model", default=DEFAULT_MODEL)
        p.add_argument("--ollama-url", default="http://127.0.0.1:11434")
        p.add_argument("--trials", type=int, default=3)
        p.add_argument("--turns", type=int, default=DEFAULT_TURNS)
        p.add_argument("--seconds", type=int, default=DEFAULT_SECONDS)
        p.add_argument("--tokens", type=int, default=DEFAULT_TOKENS)
        p.add_argument("--append", action="store_true")
        p.add_argument("--out", default=os.path.join(_HERE, default_out))

    p_trace = sub.add_parser("trace", help="record live runs with every detector off")
    p_trace.add_argument("--tasks", default=",".join(TASKS))
    common(p_trace, "traces.json")

    p_import = sub.add_parser(
        "import", help="read traces out of the journals of runs recorded earlier")
    p_import.add_argument("journals", nargs="+", help="paths to journal.jsonl files")
    p_import.add_argument("--task", required=True)
    p_import.add_argument("--append", action="store_true")
    p_import.add_argument("--out", default=os.path.join(_HERE, "traces.json"))

    p_replay = sub.add_parser("replay", help="score candidate policies against traces")
    p_replay.add_argument("traces", nargs="+")
    p_replay.add_argument("--sweep", nargs="?", const="repeat_errors", default="",
                          help="vary one threshold instead of comparing the presets")
    p_replay.add_argument("--out", default="")

    p_live = sub.add_parser("live", help="run named policies for real")
    p_live.add_argument("--tasks", default=",".join(TASKS))
    p_live.add_argument("--arms", default="current,balanced")
    common(p_live, "live.json")

    p_swarm = sub.add_parser("swarm", help="re-measure the relay under new defaults")
    p_swarm.add_argument("--tasks", default="interval")
    p_swarm.add_argument("--cycles", type=int, default=3)
    common(p_swarm, "swarm.json")

    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if a.cmd == "trace":
        return _cmd_trace(a)
    if a.cmd == "import":
        return _cmd_import(a)
    if a.cmd == "replay":
        return _cmd_replay(a)
    if a.cmd == "live":
        return _cmd_live(a)
    if a.cmd == "swarm":
        return _cmd_swarm(a)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
