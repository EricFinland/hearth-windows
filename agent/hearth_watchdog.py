#!/usr/bin/env python3
"""hearth watchdog: notice an agent that is busy but getting nowhere.

`hearth_marathon.py` keeps a run going until the work is done and
`hearth_budget.py` stops a run that spends more than it was allowed. Between
those two there is a gap, and it is the one that wastes the most unattended
hours: a loop that is making calls, burning tokens, and not progressing. To a
token counter that looks exactly like healthy work. Reading the same file nine
times, retrying an identical failing command, alternating between two tool
calls forever: every one of those keeps the call rate up, keeps the spend
climbing, and never trips a budget until the budget is gone. The whole point
of leaving Hearth running overnight is that nobody is watching, so something
has to.

A Watchdog accumulates cheap observations, one per tool call, and answers one
question: does this look like progress? Five independent detectors, each with
its own threshold:

  repeat    the identical tool and arguments ran more than N times.
  cycle     the tail of the call sequence is a block of 2 or 3 calls
            repeating (A B A B A B), which no counter of identical calls
            will ever catch because no single call repeats back to back.
  flat      calls keep being made and the result digest has not changed
            once since the first observation.
  starved   tokens keep being spent while the set of distinct result
            digests does not grow, so the spend is buying nothing new.
  stale     wall-clock time since the last NEW result digest passed a
            limit, which catches a slow loop the round counters miss.

Every detector is configurable and every detector can fire on its own, so a
caller that only cares about one of them can set the others out of reach.

Why this reports a signal with a confidence instead of killing the run
---------------------------------------------------------------------
Every detector here is a heuristic over a short history, and the cost of the
two mistakes is not symmetric. A missed stall costs tokens and a night. A
wrong kill costs the work in flight, which may be most of the way through a
task whose last few rounds genuinely look repetitive: a build retried after a
real fix, a poll loop waiting on something external, a long file read in
chunks that happen to hash alike. There is no threshold that separates those
from a true stall with certainty, which is exactly why the answer is a
confidence and a list of plain sentences rather than a verdict acted on in
place. The caller decides. A supervisor can pause at 0.9 and only log at 0.5,
an operator can read the reasons out of a status line and judge, and the
authority to end a run stays where it already lives (marathon owns the loop,
budget owns the spend, hearth_notify owns telling a human). A watchdog that
kills is also a watchdog that destroys the evidence of why it killed, and the
reasons are the useful part.

What this deliberately does NOT do: it does not kill, signal, pause or
otherwise touch any process; it does not send notifications; it does no
network, disk or subprocess I/O of any kind; it does not read logs or
transcripts; it does not hash tool output itself (the caller passes a digest,
so the caller chooses what "the same result" means); it keeps no state across
runs and writes no files; and it never calls time on its own behalf beyond
the injected clock, which is why `--self-test` runs offline, deterministically
and without sleeping. Standard library only.
"""

import argparse
import hashlib
import json
import sys
import time

# "more than this many" identical tool-and-args calls is a repeat. Three is
# deliberately forgiving: a command retried twice and then succeeding is three
# identical calls and must not be called stalled.
DEFAULT_REPEAT_LIMIT = 3
# How many times a block of calls must repeat before it counts as a cycle.
DEFAULT_CYCLE_REPEATS = 3
CYCLE_LENGTHS = (2, 3)
# How many observations with a single unchanging digest count as flat.
DEFAULT_FLAT_ROUNDS = 4
# Rounds and tokens spent since the last new digest before spend looks starved.
DEFAULT_STARVED_ROUNDS = 4
DEFAULT_STARVED_TOKENS = 1
# Seconds since the last new digest before the clock alone raises the flag.
DEFAULT_STALE_SECONDS = 900.0

# Per-detector contribution to the confidence. Repeat and cycle are the two
# that almost never fire on real work, so they weigh most; the rest corroborate.
WEIGHTS = {
    "repeat": 0.4,
    "cycle": 0.4,
    "flat": 0.25,
    "starved": 0.2,
    "stale": 0.2,
}
MAX_CONFIDENCE = 0.95


def _canonical_args(args):
    """A stable text form of a call's arguments, for equality only. Order of
    dict keys must not make two identical calls look different, and an object
    json cannot encode falls back to repr rather than raising."""
    try:
        return json.dumps(args, sort_keys=True, default=repr)
    except (TypeError, ValueError):
        return repr(args)


def _signature(tool, args):
    """Identity of a call: tool name plus a short hash of its arguments. The
    hash keeps long arguments (a whole file body, a long prompt) out of the
    accumulated state while still distinguishing them."""
    blob = _canonical_args(args).encode("utf-8", "replace")
    return "{}\x00{}".format(tool, hashlib.sha256(blob).hexdigest()[:16])


class Watchdog(object):
    """Accumulates one observation per tool call and reports whether the run
    looks stalled. The clock is a callable returning seconds (any epoch, only
    differences are used), injected so tests need no real time."""

    def __init__(self, clock=None, repeat_limit=DEFAULT_REPEAT_LIMIT,
                 cycle_repeats=DEFAULT_CYCLE_REPEATS,
                 flat_rounds=DEFAULT_FLAT_ROUNDS,
                 starved_rounds=DEFAULT_STARVED_ROUNDS,
                 starved_tokens=DEFAULT_STARVED_TOKENS,
                 stale_seconds=DEFAULT_STALE_SECONDS):
        self.clock = clock or time.monotonic
        self.repeat_limit = int(repeat_limit)
        self.cycle_repeats = int(cycle_repeats)
        self.flat_rounds = int(flat_rounds)
        self.starved_rounds = int(starved_rounds)
        self.starved_tokens = int(starved_tokens)
        self.stale_seconds = float(stale_seconds)
        self.reset()

    def reset(self):
        """Forget everything observed so far. Called when the run genuinely
        moves on (a new task, a plan change) so old repetition cannot haunt
        the next stretch of work."""
        self._seq = []          # call signature per observation, in order
        self._tools = {}        # signature -> tool name, for readable reasons
        self._counts = {}       # signature -> how many times it was seen
        self._digests = []      # distinct result digests, first seen order
        self._seen = set()      # same, as a set
        self._tokens = 0        # tokens spent across every observation
        self._tokens_at_new = 0  # tokens total when the last new digest landed
        self._last_new_index = -1  # observation index of the last new digest
        self._last_new_time = self.clock()
        self._last_round = None
        self._started = self._last_new_time

    def config(self):
        """The thresholds in force, for a status display or a log line."""
        return {
            "repeat_limit": self.repeat_limit,
            "cycle_repeats": self.cycle_repeats,
            "flat_rounds": self.flat_rounds,
            "starved_rounds": self.starved_rounds,
            "starved_tokens": self.starved_tokens,
            "stale_seconds": self.stale_seconds,
        }

    def observe(self, round_index, tool, args, result_digest, tokens):
        """Record one tool call. `result_digest` is whatever the caller uses to
        mean "the same result as before" (a hash, an exit code plus stderr, a
        normalized output). `tokens` is the spend for this round, not a running
        total. Returns the number of observations held."""
        sig = _signature(tool, args)
        self._seq.append(sig)
        self._tools[sig] = tool
        self._counts[sig] = self._counts.get(sig, 0) + 1
        self._tokens += max(0, int(tokens or 0))
        self._last_round = round_index
        key = "" if result_digest is None else str(result_digest)
        if key not in self._seen:
            self._seen.add(key)
            self._digests.append(key)
            self._last_new_index = len(self._seq) - 1
            self._last_new_time = self.clock()
            self._tokens_at_new = self._tokens
        return len(self._seq)

    def _hottest(self):
        """The most repeated call signature and its count, or (None, 0)."""
        best, best_n = None, 0
        for sig in self._seq:  # sequence order keeps the result deterministic
            n = self._counts[sig]
            if n > best_n:
                best, best_n = sig, n
        return best, best_n

    def _find_cycle(self):
        """The repeating tail block, as a list of tool names, or None. Only a
        block of two or more distinct calls counts: an all-identical tail is
        the repeat detector's business, not a cycle."""
        for length in CYCLE_LENGTHS:
            need = length * self.cycle_repeats
            if len(self._seq) < need:
                continue
            tail = self._seq[-need:]
            block = tail[:length]
            if len(set(block)) < 2:
                continue
            if all(tail[i] == block[i % length] for i in range(need)):
                return [self._tools[s] for s in block]
        return None

    def _rounds_since_new(self):
        if not self._seq:
            return 0
        return len(self._seq) - 1 - self._last_new_index

    def verdict(self):
        """Judge what has been observed. Returns a dict with `stalled` (bool),
        `reasons` (list of plain sentences, empty when not stalled) and
        `confidence` (0.0 when nothing fired, up to 0.95 when several detectors
        agree). Pure: calling it changes nothing and it may be called as often
        as a status display likes."""
        reasons = []
        fired = []
        if not self._seq:
            return {"stalled": False, "reasons": [], "confidence": 0.0,
                    "detectors": []}

        sig, count = self._hottest()
        if count > self.repeat_limit:
            fired.append("repeat")
            reasons.append(
                "The same call to {} with the same arguments ran {} times, "
                "over a limit of {}.".format(
                    self._tools[sig], count, self.repeat_limit))

        block = self._find_cycle()
        if block:
            fired.append("cycle")
            reasons.append(
                "The last {} calls are a cycle of {} repeating: {}.".format(
                    len(block) * self.cycle_repeats, len(block),
                    " then ".join(block)))

        if len(self._seq) >= self.flat_rounds and len(self._digests) == 1:
            fired.append("flat")
            reasons.append(
                "{} calls have been made and every one returned the same "
                "result as the first.".format(len(self._seq)))

        since_rounds = self._rounds_since_new()
        since_tokens = self._tokens - self._tokens_at_new
        if (since_rounds >= self.starved_rounds
                and since_tokens >= self.starved_tokens):
            fired.append("starved")
            reasons.append(
                "{} tokens were spent over the last {} calls without one new "
                "result, so the spend is buying nothing.".format(
                    since_tokens, since_rounds))

        idle = self.clock() - self._last_new_time
        if idle > self.stale_seconds:
            fired.append("stale")
            reasons.append(
                "It has been {} seconds since the last new result, over a "
                "limit of {}.".format(int(idle), int(self.stale_seconds)))

        confidence = 0.0
        for name in fired:
            confidence += WEIGHTS[name]
        confidence = round(min(MAX_CONFIDENCE, confidence), 2)
        return {"stalled": bool(fired), "reasons": reasons,
                "confidence": confidence, "detectors": fired}

    def snapshot(self):
        """A flat dict for a status display: what was seen, plus the verdict.
        Small, printable, and free of raw tool arguments."""
        v = self.verdict()
        sig, count = self._hottest()
        block = self._find_cycle()
        return {
            "observations": len(self._seq),
            "last_round": self._last_round,
            "distinct_results": len(self._digests),
            "tokens": self._tokens,
            "tokens_since_new_result": self._tokens - self._tokens_at_new,
            "rounds_since_new_result": self._rounds_since_new(),
            "seconds_since_new_result": round(
                self.clock() - self._last_new_time, 3),
            "seconds_observed": round(self.clock() - self._started, 3),
            "top_call": self._tools.get(sig),
            "top_call_count": count,
            "cycle": block,
            "stalled": v["stalled"],
            "confidence": v["confidence"],
            "detectors": v["detectors"],
            "reasons": v["reasons"],
        }


class _FakeClock(object):
    """A clock that only moves when told to, so the self-test never sleeps and
    never depends on how fast the machine is."""

    def __init__(self, t=1000.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += float(seconds)


def _self_test():
    # --- true positive: the same file read nine times ---------------------
    clock = _FakeClock()
    w = Watchdog(clock=clock)
    for i in range(9):
        w.observe(i, "read", {"path": "notes.md"}, "digest-a", 400)
    v = w.verdict()
    assert v["stalled"] is True, v
    assert "repeat" in v["detectors"], v
    assert "flat" in v["detectors"], v
    assert "starved" in v["detectors"], v
    assert v["confidence"] >= 0.8, v
    assert any("ran 9 times" in r for r in v["reasons"]), v["reasons"]

    # --- true positive: an identical failing command retried past N -------
    w = Watchdog(clock=_FakeClock())
    for i in range(5):
        w.observe(i, "bash", {"cmd": "pytest -q"}, "exit=1 same traceback", 90)
    v = w.verdict()
    assert v["stalled"] is True, v
    assert "repeat" in v["detectors"], v

    # --- true positive: a two-call cycle no identical counter would see ---
    # Digests alternate between two values and no single call repeats back to
    # back, so only the cycle detector has anything to go on. Tokens are zero
    # so the starved detector stays out of it.
    clock = _FakeClock()
    w = Watchdog(clock=clock)
    pairs = [("read", {"path": "a.py"}, "da"), ("bash", {"cmd": "make"}, "db")]
    for i in range(6):
        tool, args, dig = pairs[i % 2]
        w.observe(i, tool, args, dig, 0)
    v = w.verdict()
    assert v["stalled"] is True, v
    assert v["detectors"] == ["cycle"], v
    assert "repeat" not in v["detectors"], v
    assert any("cycle of 2" in r for r in v["reasons"]), v["reasons"]

    # --- true positive: a three-call cycle --------------------------------
    w = Watchdog(clock=_FakeClock())
    trio = [("read", {"path": "a"}, "d1"), ("edit", {"path": "b"}, "d2"),
            ("bash", {"cmd": "c"}, "d3")]
    for i in range(9):
        tool, args, dig = trio[i % 3]
        w.observe(i, tool, args, dig, 0)
    v = w.verdict()
    assert "cycle" in v["detectors"], v
    assert any("cycle of 3" in r for r in v["reasons"]), v["reasons"]

    # --- true positive: distinct calls, result digest never changes --------
    w = Watchdog(clock=_FakeClock())
    for i in range(5):
        w.observe(i, "bash", {"cmd": "status {}".format(i)}, "still pending", 50)
    v = w.verdict()
    assert v["stalled"] is True, v
    assert "flat" in v["detectors"], v
    assert "repeat" not in v["detectors"], v
    assert "cycle" not in v["detectors"], v

    # --- true positive: spend climbing, distinct result set not growing ----
    # Real progress for three rounds, then four more rounds of different calls
    # that all land on a result already seen. Only the starved detector should
    # fire: there are three distinct digests, so nothing is flat, and no call
    # or block repeats.
    w = Watchdog(clock=_FakeClock())
    w.observe(0, "read", {"path": "a"}, "d1", 100)
    w.observe(1, "edit", {"path": "b"}, "d2", 100)
    w.observe(2, "bash", {"cmd": "build"}, "d3", 100)
    for i in range(4):
        w.observe(3 + i, "bash", {"cmd": "probe {}".format(i)}, "d3", 700)
    v = w.verdict()
    assert v["detectors"] == ["starved"], v
    assert any("2800 tokens" in r for r in v["reasons"]), v["reasons"]
    snap = w.snapshot()
    assert snap["rounds_since_new_result"] == 4, snap
    assert snap["tokens_since_new_result"] == 2800, snap
    assert snap["distinct_results"] == 3, snap

    # --- true positive: wall clock since the last new result ---------------
    # One round, far too long ago, with nothing else to go on: too few rounds
    # for starved, two distinct digests so not flat, no repetition at all.
    clock = _FakeClock()
    w = Watchdog(clock=clock, stale_seconds=900)
    w.observe(0, "read", {"path": "a"}, "d1", 0)
    w.observe(1, "bash", {"cmd": "slow"}, "d2", 0)
    clock.advance(1000)
    w.observe(2, "bash", {"cmd": "slow again"}, "d2", 0)
    v = w.verdict()
    assert v["detectors"] == ["stale"], v
    assert v["confidence"] == 0.2, v
    assert any("1000 seconds" in r for r in v["reasons"]), v["reasons"]

    # --- FALSE POSITIVE GUARD: ten different files is legitimate work ------
    clock = _FakeClock()
    w = Watchdog(clock=clock)
    for i in range(10):
        clock.advance(5)
        w.observe(i, "read", {"path": "mod_{}.py".format(i)},
                  "body-{}".format(i), 300)
    v = w.verdict()
    assert v["stalled"] is False, v
    assert v["reasons"] == [], v
    assert v["confidence"] == 0.0, v
    snap = w.snapshot()
    assert snap["distinct_results"] == 10, snap
    assert snap["rounds_since_new_result"] == 0, snap

    # --- FALSE POSITIVE GUARD: retry twice, then succeed -------------------
    clock = _FakeClock()
    w = Watchdog(clock=clock)
    clock.advance(10)
    w.observe(0, "bash", {"cmd": "pytest -q"}, "exit=1", 200)
    clock.advance(10)
    w.observe(1, "bash", {"cmd": "pytest -q"}, "exit=1", 200)
    clock.advance(10)
    w.observe(2, "edit", {"path": "fix.py"}, "patched", 200)
    clock.advance(10)
    w.observe(3, "bash", {"cmd": "pytest -q"}, "exit=0", 200)
    v = w.verdict()
    assert v["stalled"] is False, v
    assert v["reasons"] == [], v
    assert v["confidence"] == 0.0, v

    # --- FALSE POSITIVE GUARD: a long run that keeps producing new results -
    clock = _FakeClock()
    w = Watchdog(clock=clock)
    for i in range(40):
        clock.advance(30)
        w.observe(i, "bash", {"cmd": "step {}".format(i)},
                  "out-{}".format(i), 1000)
    assert w.verdict()["stalled"] is False, w.snapshot()

    # --- key-order and unhashable arguments ------------------------------
    w = Watchdog(clock=_FakeClock())
    for i in range(4):
        w.observe(i, "read", {"path": "a", "limit": 10}, "d{}".format(i), 1)
        w.observe(i, "read", {"limit": 10, "path": "a"}, "d{}".format(i), 1)
    sig, count = w._hottest()
    assert count == 8, count  # key order must not split one call in two
    w.observe(99, "tool", {"obj": object()}, "d-obj", 1)  # must not raise

    # --- configurability: each detector can be pushed out of reach --------
    w = Watchdog(clock=_FakeClock(), repeat_limit=100, cycle_repeats=100,
                 flat_rounds=10 ** 6, starved_rounds=10 ** 6,
                 stale_seconds=10 ** 9)
    for i in range(20):
        w.observe(i, "read", {"path": "same.md"}, "one digest", 500)
    assert w.verdict()["stalled"] is False, w.verdict()

    # ... and tightened until it fires on two calls
    w = Watchdog(clock=_FakeClock(), repeat_limit=1, flat_rounds=2)
    w.observe(0, "read", {"path": "x"}, "d", 1)
    w.observe(1, "read", {"path": "x"}, "d", 1)
    v = w.verdict()
    assert "repeat" in v["detectors"] and "flat" in v["detectors"], v
    assert w.config()["repeat_limit"] == 1, w.config()

    # --- reset forgets a stall, snapshot is clean before any observation ---
    clock = _FakeClock()
    w = Watchdog(clock=clock)
    for i in range(9):
        w.observe(i, "read", {"path": "stuck.md"}, "d", 400)
    assert w.verdict()["stalled"] is True
    w.reset()
    v = w.verdict()
    assert v["stalled"] is False, v
    assert v["confidence"] == 0.0 and v["reasons"] == [], v
    snap = w.snapshot()
    assert snap["observations"] == 0, snap
    assert snap["tokens"] == 0, snap
    assert snap["top_call"] is None and snap["cycle"] is None, snap
    assert snap["last_round"] is None, snap
    # the clock restarts with the reset, so an old stale flag does not survive
    assert snap["seconds_since_new_result"] == 0.0, snap

    # --- snapshot stays printable and json friendly -----------------------
    w = Watchdog(clock=_FakeClock())
    for i in range(6):
        w.observe(i, "read", {"path": "loop.md"}, "d", 10)
    text = json.dumps(w.snapshot(), sort_keys=True)
    assert "loop.md" not in text, "arguments must stay out of the snapshot"
    assert "\"stalled\": true" in text, text

    print("hearth-watchdog self-test OK")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="hearth-watchdog")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--config", action="store_true",
                   help="print the default thresholds and exit")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    if a.config:
        for k, v in sorted(Watchdog().config().items()):
            print("{} = {}".format(k, v))
        return 0
    p.error("nothing to do (use --self-test or --config)")


if __name__ == "__main__":
    sys.exit(main())
