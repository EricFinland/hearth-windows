#!/usr/bin/env python3
"""hearth ratelimit: per-tool token buckets for an unattended run.

Hearth's agent runs for hours with nobody watching. The model emits tool
calls as fast as it can think of them, and a loop that keeps calling
`web_fetch` or a search endpoint at that speed is indistinguishable, from
the other side, from a scraper: the user gets throttled, IP banned, or
billed for traffic they never saw. The egress wall decides WHERE a run may
talk; this decides HOW OFTEN. Software that runs unsupervised has to be a
good network citizen on its own, because there is no human at the keyboard
to notice the hammering and stop it.

The shape is a classic token bucket per tool name. Each bucket holds up to
`burst` tokens and refills continuously at `rate_per_sec`, so a run gets a
small opening burst (the normal case: a handful of fetches back to back)
and then settles to the sustained rate. Refill is computed from elapsed
time on every query, not stepped by a timer thread, so a bucket is correct
after an idle hour and costs nothing while idle.

Configuration comes from a mapping plus the environment, as
`HEARTH_RATE_<TOOL>=<rate>/<burst>` (for example
`HEARTH_RATE_WEB_FETCH=1/5`). Env values are parsed defensively and a
malformed value means UNLIMITED, never blocked: it is discarded, the tool
is left with no limit, and the name is recorded in `Limiter.rejected` for
the status display. The reasoning is that a typo in a rate string is an
operator mistake, and the failure mode of "every tool silently returns
zero tokens forever" turns a typo into a dead overnight run that looks
like a hang. Too much traffic is recoverable and visible; a run that does
nothing for eight hours is neither. A tool with no configured limit is
unlimited by the same logic: limits are opt in, per tool.

What this deliberately does NOT do:

  - It never sleeps, and it never blocks. `take()` and `check()` return a
    verdict and come straight back. Whether to wait, skip the call, pick a
    different tool, or end the run is the caller's decision, and the
    caller is the only layer that knows which. This also keeps the module
    testable with no wall-clock time involved at all.
  - It does not retry, queue, or reorder calls. There is no hidden backlog
    that fires later; a denied call is simply denied, with an accurate
    `retry_after` attached.
  - It keeps no state on disk and none across processes. Buckets live in
    memory for the life of the run. Two Hearth processes do not share a
    budget, and a restart starts with full buckets. Cross-process limiting
    needs a lock or a broker, which is more machinery than an unattended
    single-run agent earns.
  - It is not a quota, cost, or billing tracker. It limits calls per
    second, not dollars or total calls, and it has no idea what a call
    costs. The budget tripwire is a separate concern.
  - It does not decide whether a destination is allowed. That is the
    egress wall's job and this module never looks at a URL.
  - It is not thread safe. Hearth's tool dispatch is single threaded, and
    a lock here would buy nothing but a false sense of safety.

Standard library only. The clock is injected as a plain callable, so the
self-test controls time exactly and runs offline in milliseconds.
"""

import argparse
import math
import os
import sys
import time

ENV_PREFIX = "HEARTH_RATE_"
# Slack for float error. Refill is elapsed * rate, so a caller that waits
# exactly the retry_after it was handed can land a hair under the token it
# was promised. Without this, an honest caller gets denied twice and the
# retry_after we advertise would be a lie.
EPSILON = 1e-9

# Sensible starting points for the tools that talk to somebody else's
# machine. Values are (rate_per_sec, burst); the "1/5" string form the env
# uses is accepted here too.
DEFAULT_LIMITS = {
    "web_fetch": (1.0, 5),
    "web_search": (0.25, 3),
    "llm_call": (0.5, 4),
    "telegram_send": (1.0, 4),
    "discord_post": (0.5, 5),
    "ntfy_post": (1.0, 5),
}


def _tool_name(tool):
    """Normalize a tool name so the mapping, the env var suffix, and the
    call site all agree: lowercase, non-alphanumerics to underscore. That
    makes HEARTH_RATE_WEB_FETCH and the tool "web_fetch" the same key."""
    chars = []
    for ch in str(tool).strip().lower():
        chars.append(ch if ch.isalnum() else "_")
    return "".join(chars).strip("_")


def _parse_limit(text):
    """Parse "<rate>/<burst>" into (rate, burst), or None when the value is
    not usable. None means unlimited, never blocked: see the module
    docstring. Anything unexpected lands here, including an empty string, a
    missing slash, junk on either side, a non-positive rate, a burst below
    one token, and inf/nan."""
    if text is None:
        return None
    raw = str(text).strip()
    if "/" not in raw:
        return None
    rate_text, _, burst_text = raw.partition("/")
    try:
        rate = float(rate_text.strip())
        burst = float(burst_text.strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(rate) or not math.isfinite(burst):
        return None
    if rate <= 0 or burst < 1:
        return None
    return (rate, burst)


def _coerce_limit(value):
    """Accept either a (rate, burst) pair or a "rate/burst" string, and run
    both through the same defensive parse, so a mapping entry cannot get a
    looser check than an env var."""
    if value is None:
        return None
    if isinstance(value, (tuple, list)):
        if len(value) != 2:
            return None
        return _parse_limit("{}/{}".format(value[0], value[1]))
    return _parse_limit(value)


class Bucket(object):
    """A token bucket holding up to `burst` tokens, refilling continuously
    at `rate_per_sec`. `clock` is any callable returning seconds as a float
    (time.monotonic by default); injecting it is what makes this testable
    without sleeping. Construction raises ValueError on a nonsensical rate
    or burst, which is how `Limiter` turns a bad config value into
    "unlimited" rather than into a bucket that never opens."""

    def __init__(self, rate_per_sec, burst, clock=None):
        rate = float(rate_per_sec)
        cap = float(burst)
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("rate_per_sec must be a finite positive number")
        if not math.isfinite(cap) or cap < 1:
            raise ValueError("burst must be a finite number of at least 1")
        self.rate_per_sec = rate
        self.burst = cap
        self.clock = clock or time.monotonic
        self.tokens = cap
        self.updated = self.clock()

    def _need(self, n):
        """Tokens a take(n) costs. A nonsensical n is clamped to one token
        rather than raising: a bad count from a caller must not take down
        an unattended run, and charging one token is the safe direction."""
        try:
            need = float(n)
        except (TypeError, ValueError):
            return 1.0
        if not math.isfinite(need) or need < 1:
            return 1.0
        return need

    def _refill(self):
        """Add the tokens earned since the last query and return the total.
        Continuous, not per tick: half a refill interval earns half a
        token. Capped at `burst`, so an idle bucket never banks credit no
        matter how long it sits. A clock that jumps backwards (never with
        monotonic, but an injected clock can) only moves the reference
        point, so it cannot mint tokens."""
        now = self.clock()
        elapsed = now - self.updated
        if elapsed > 0:
            self.tokens = min(self.burst,
                              self.tokens + elapsed * self.rate_per_sec)
            self.updated = now
        elif elapsed < 0:
            self.updated = now
        return self.tokens

    def available(self):
        """Tokens available right now. Refills, consumes nothing."""
        return self._refill()

    def wait_time(self, n=1):
        """Seconds until `n` tokens would be available: 0.0 when the call
        is allowed right now, and float("inf") when n exceeds `burst` and
        so can never be satisfied by waiting."""
        need = self._need(n)
        if need > self.burst:
            return float("inf")
        tokens = self._refill()
        if tokens + EPSILON >= need:
            return 0.0
        return (need - tokens) / self.rate_per_sec

    def take(self, n=1):
        """Try to spend `n` tokens. Returns a verdict dict:

            allowed      bool, and the tokens are spent only when True
            tokens_left  tokens in the bucket after the attempt
            retry_after  seconds to wait before trying again, 0.0 when
                         allowed, inf when n > burst

        A denial costs nothing: the bucket is left exactly as it was, so a
        caller that polls is not punished for polling."""
        need = self._need(n)
        if need > self.burst:
            return {"allowed": False, "tokens_left": self._refill(),
                    "retry_after": float("inf")}
        tokens = self._refill()
        if tokens + EPSILON >= need:
            self.tokens = max(0.0, tokens - need)
            return {"allowed": True, "tokens_left": self.tokens,
                    "retry_after": 0.0}
        return {"allowed": False, "tokens_left": tokens,
                "retry_after": (need - tokens) / self.rate_per_sec}


class Limiter(object):
    """One named bucket per tool, built from a mapping and from the
    environment. Env wins over the mapping, including when the env value is
    malformed: that clears the limit and leaves the tool unlimited, with
    the name recorded in `rejected` so a status display can say so. `env`
    is injectable (a dict) so the self-test never reads os.environ."""

    def __init__(self, config=None, clock=None, env=None):
        self.clock = clock or time.monotonic
        self.limits = {}
        self.rejected = []
        for tool, value in (config or {}).items():
            name = _tool_name(tool)
            if not name:
                continue
            limit = _coerce_limit(value)
            if limit is None:
                self._reject(name)
                continue
            self.limits[name] = limit
        env_map = os.environ if env is None else env
        for key in sorted(env_map.keys()):
            if not key.startswith(ENV_PREFIX):
                continue
            name = _tool_name(key[len(ENV_PREFIX):])
            if not name:
                continue
            limit = _parse_limit(env_map[key])
            if limit is None:
                self.limits.pop(name, None)
                self._reject(name)
                continue
            self.limits[name] = limit
        self.buckets = {}

    def _reject(self, name):
        if name not in self.rejected:
            self.rejected.append(name)

    def bucket(self, tool):
        """The bucket for `tool`, created on first use, or None when the
        tool has no configured limit (unlimited)."""
        name = _tool_name(tool)
        limit = self.limits.get(name)
        if limit is None:
            return None
        bucket = self.buckets.get(name)
        if bucket is None:
            bucket = Bucket(limit[0], limit[1], clock=self.clock)
            self.buckets[name] = bucket
        return bucket

    def check(self, tool, n=1):
        """Ask whether `tool` may be called now, spending `n` tokens when
        it may. Returns the Bucket verdict plus `tool` and `limit`. An
        unconfigured tool is allowed with limit None and tokens_left
        None, so a caller can tell "plenty of budget" from "not limited"."""
        name = _tool_name(tool)
        bucket = self.bucket(name)
        if bucket is None:
            return {"tool": name, "limit": None, "allowed": True,
                    "tokens_left": None, "retry_after": 0.0}
        verdict = bucket.take(n)
        verdict["tool"] = name
        verdict["limit"] = (bucket.rate_per_sec, bucket.burst)
        return verdict

    def wait_time(self, tool, n=1):
        """Seconds until `tool` could spend `n` tokens. 0.0 when it could
        now, and for any tool with no configured limit."""
        bucket = self.bucket(tool)
        if bucket is None:
            return 0.0
        return bucket.wait_time(n)

    def snapshot(self):
        """Current state of every limited tool, name sorted, for a status
        display. Reading the state refills it (that is what makes refill
        continuous) but spends nothing."""
        out = {}
        for name in sorted(self.limits):
            bucket = self.bucket(name)
            out[name] = {"rate_per_sec": bucket.rate_per_sec,
                         "burst": bucket.burst,
                         "tokens_left": bucket.available(),
                         "retry_after": bucket.wait_time(1)}
        return out


class _FakeClock(object):
    """A clock as a callable, advanced by hand. The whole reason the clock
    is injectable: the self-test and the CLI simulation cover hours of
    bucket behaviour without sleeping for a single millisecond."""

    def __init__(self, start=0.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += float(seconds)
        return self.t


def _self_test():
    # Continuous refill: half an interval earns exactly half a token.
    clock = _FakeClock()
    b = Bucket(1.0, 2, clock=clock)
    v = b.take(2)
    assert v["allowed"], v
    assert abs(v["tokens_left"]) < EPSILON, v
    clock.advance(0.5)
    assert abs(b.available() - 0.5) < EPSILON, b.available()
    v = b.take(1)
    assert not v["allowed"], v
    assert abs(v["tokens_left"] - 0.5) < EPSILON, v
    assert abs(v["retry_after"] - 0.5) < EPSILON, v
    # A denial costs nothing.
    assert abs(b.available() - 0.5) < EPSILON, b.available()

    # retry_after is accurate enough to be obeyed: wait exactly that long
    # and the same call goes through, first try.
    clock.advance(v["retry_after"])
    assert b.take(1)["allowed"], "retry_after must be honoured"

    # Same promise with a rate that does not land on round floats.
    c2 = _FakeClock(123.456)
    b2 = Bucket(0.3, 4, clock=c2)
    assert b2.take(4)["allowed"]
    v = b2.take(3)
    assert not v["allowed"], v
    assert abs(v["retry_after"] - 3.0 / 0.3) < 1e-6, v
    c2.advance(v["retry_after"])
    assert b2.take(3)["allowed"], "retry_after must be honoured"

    # Burst is never exceeded, however long the bucket idles.
    clock.advance(1e9)
    assert b.available() == b.burst, b.available()
    clock.advance(86400.0 * 365)
    assert b.available() == b.burst, b.available()
    assert b.take(2)["allowed"], "a full bucket covers a full burst"
    assert abs(b.available()) < EPSILON, b.available()

    # Taking more than burst can never succeed, idle or not, and says so
    # instead of handing back a retry_after that will not help.
    clock.advance(1e9)
    for n in (3, 10, 10 ** 9):
        v = b.take(n)
        assert not v["allowed"], (n, v)
        assert v["retry_after"] == float("inf"), (n, v)
        assert b.wait_time(n) == float("inf"), n
    assert b.available() == b.burst, "an impossible take spends nothing"

    # A count below one token is clamped up, not raised on.
    b3 = Bucket(1.0, 1, clock=_FakeClock())
    assert b3.take(0)["allowed"]
    assert not b3.take(1)["allowed"], "take(0) still charges one token"

    # A clock that jumps backwards cannot mint tokens.
    c4 = _FakeClock()
    b4 = Bucket(1.0, 2, clock=c4)
    assert b4.take(2)["allowed"]
    c4.advance(-10000.0)
    assert not b4.take(1)["allowed"], "rewound clock must not refill"

    # Nonsense limits are refused at construction, which is what lets the
    # Limiter treat a bad config value as unlimited.
    for rate, burst in ((0, 1), (-1, 1), (1, 0), (1, 0.5),
                        (float("inf"), 1), (1, float("nan"))):
        try:
            Bucket(rate, burst, clock=_FakeClock())
        except ValueError:
            pass
        else:
            raise AssertionError("accepted bad limit {}/{}".format(rate, burst))

    # Two tools do not share a bucket.
    c5 = _FakeClock()
    lim = Limiter({"web_fetch": (1.0, 2), "web_search": "0.5/1"},
                  clock=c5, env={})
    assert lim.check("web_search")["allowed"], "first search allowed"
    assert not lim.check("web_search")["allowed"], "burst of 1 is spent"
    assert lim.check("web_fetch")["allowed"], "fetch has its own bucket"
    assert lim.check("web_fetch")["allowed"], "fetch has its own bucket"
    assert not lim.check("web_fetch")["allowed"], "fetch burst spent"
    c5.advance(2.0)
    assert lim.check("web_search")["allowed"], "search refilled on its own"

    # Verdicts carry the tool and its limit for logging.
    v = lim.check("web_fetch")
    assert v["tool"] == "web_fetch", v
    assert v["limit"] == (1.0, 2.0), v

    # A tool with no configured limit is unlimited, and says so.
    v = lim.check("read_file", 10 ** 6)
    assert v["allowed"] and v["limit"] is None, v
    assert v["tokens_left"] is None, v
    assert lim.wait_time("read_file", 10 ** 6) == 0.0

    # Names normalize, so the env suffix and the call site agree.
    assert lim.check("WEB_SEARCH")["tool"] == "web_search"
    assert lim.check("web-search")["tool"] == "web_search"

    # Env config, and a malformed value meaning unlimited rather than
    # blocked. A typo must not silently stop a tool for the whole run.
    env = {"HEARTH_RATE_WEB_FETCH": "junk",
           "HEARTH_RATE_WEB_SEARCH": "2/4",
           "HEARTH_RATE_LLM_CALL": "0/5",
           "HEARTH_RATE_TELEGRAM_SEND": "1/0",
           "HEARTH_RATE_NTFY_POST": "",
           "HEARTH_RATE_DISCORD_POST": "5",
           "NOT_A_RATE_VAR": "1/1"}
    lim2 = Limiter({"web_fetch": (1.0, 2), "llm_call": (0.5, 4),
                    "shell": "bogus"}, clock=_FakeClock(), env=env)
    assert lim2.limits["web_search"] == (2.0, 4.0), lim2.limits
    for tool in ("web_fetch", "llm_call", "telegram_send", "ntfy_post",
                 "discord_post", "shell", "not_a_rate_var"):
        assert tool not in lim2.limits, tool
        assert lim2.check(tool, 10 ** 6)["allowed"], tool
        assert lim2.wait_time(tool) == 0.0, tool
    assert sorted(lim2.rejected) == ["discord_post", "llm_call", "ntfy_post",
                                     "shell", "telegram_send",
                                     "web_fetch"], lim2.rejected

    # snapshot(): limited tools only, name sorted, nothing spent.
    snap = lim2.snapshot()
    assert list(snap) == ["web_search"], snap
    assert snap["web_search"] == {"rate_per_sec": 2.0, "burst": 4.0,
                                  "tokens_left": 4.0,
                                  "retry_after": 0.0}, snap
    assert lim2.snapshot() == snap, "snapshot must not consume"

    c6 = _FakeClock()
    lim3 = Limiter({"web_fetch": (1.0, 2)}, clock=c6, env={})
    assert lim3.check("web_fetch", 2)["allowed"]
    snap = lim3.snapshot()["web_fetch"]
    assert abs(snap["tokens_left"]) < EPSILON, snap
    assert abs(snap["retry_after"] - 1.0) < EPSILON, snap

    # The shipped defaults are all valid, so nothing is unlimited by
    # accident and the CLI has something honest to print.
    lim4 = Limiter(DEFAULT_LIMITS, clock=_FakeClock(), env={})
    assert lim4.rejected == [], lim4.rejected
    assert sorted(lim4.limits) == sorted(_tool_name(t) for t in DEFAULT_LIMITS)

    print("hearth-ratelimit self-test OK")
    return 0


def _print_limits(limiter):
    print("configured per-tool rate limits:")
    snap = limiter.snapshot()
    if not snap:
        print("  (none configured, every tool unlimited)")
    for name in snap:
        info = snap[name]
        print("  {:<16} {:>8.3f}/sec   burst {:g}".format(
            name, info["rate_per_sec"], info["burst"]))
    if limiter.rejected:
        print("unlimited (no usable limit configured): {}".format(
            ", ".join(sorted(limiter.rejected))))
    print("any tool not listed above is unlimited")


def _simulate(tool, calls, step):
    """Replay `calls` attempts at `step` seconds apart against a fake
    clock. No sleeping: the clock is injected, so this prints what an hour
    of traffic would do in well under a millisecond."""
    clock = _FakeClock()
    limiter = Limiter(DEFAULT_LIMITS, clock=clock)
    print("")
    print("simulation: {} x {}, one call every {:g}s "
          "(injected clock, no sleeping)".format(calls, tool, step))
    for i in range(calls):
        v = limiter.check(tool)
        if v["limit"] is None:
            print("  t={:>6.2f}  {}  allowed  (unlimited)".format(
                clock(), tool))
        elif v["allowed"]:
            print("  t={:>6.2f}  {}  allowed  tokens_left {:.2f}".format(
                clock(), tool, v["tokens_left"]))
        else:
            print("  t={:>6.2f}  {}  DENIED   retry_after {:.2f}s".format(
                clock(), tool, v["retry_after"]))
        if i + 1 < calls:
            clock.advance(step)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="hearth-ratelimit",
        description="per-tool token buckets for an unattended run")
    p.add_argument("--tool", default="web_fetch",
                   help="tool name to simulate (default: web_fetch)")
    p.add_argument("--calls", type=int, default=8,
                   help="number of simulated calls (default: 8)")
    p.add_argument("--step", type=float, default=0.25,
                   help="simulated seconds between calls (default: 0.25)")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    _print_limits(Limiter(DEFAULT_LIMITS))
    return _simulate(_tool_name(a.tool), max(1, a.calls), max(0.0, a.step))


if __name__ == "__main__":
    sys.exit(main())
