#!/usr/bin/env python3
"""hearth retry: one retry policy for the whole engine.

Hearth talks to three kinds of remote thing and every one of them fails
differently. The local llama-server refuses connections for the first few
seconds of its life, so a connection refused during startup is not really an
error at all, it is "not yet". Hugging Face answers 404 for a model repo that
does not exist and 401 for a token that is wrong or expired, and both of those
are permanent: retrying the 404 cannot conjure the model, and hammering the
401 is actively harmful because the account gets rate limited or locked for
the abuse. MCP servers sit somewhere in between, dropping sockets mid-stream
when they restart.

Before this module each caller invented its own loop. Some had no retry and
fell over while the engine was still warming up, some retried a 401 twenty
times, and none of them agreed on how long to wait. This gives all of them
one answer:

  classify(exc_or_status)   retry / fatal / unknown, from an HTTPError status
                            code or a socket level exception.
  delays(attempts, ...)     exponential backoff, capped, with full jitter.
  call(fn, attempts=...)    run fn, retry only what is worth retrying.

Why "unknown" exists, and why it is retried exactly once. Treating an
unrecognised failure as retryable means a brand new permanent error (a 402
billing wall, a TypeError in our own request building) turns into a loop that
burns the full attempt budget, and every wait with it, on something that will
never succeed, hiding the real bug behind a long pause. Treating it as fatal
means the first unmapped-but-transient status (a 598 from some proxy, an
OSError flavour not in the table) becomes a hard failure on a request that a
single wait would have fixed. Neither extreme is right, so unknown gets one
retry and no more: enough to absorb a genuine blip, cheap enough that a
permanent failure surfaces almost immediately with its own exception intact.

What this deliberately does NOT do:

  No circuit breaker, no shared failure counters, no adaptive rate limiting.
  Those need state that outlives a call and a place to keep it, and hearth
  runs one process per agent, so there is nothing to share it through.
  No per-host policy registry. Three transports and one table of status codes
  beat a configuration format nobody will read.
  No logging, no metrics, no notifications. The caller owns its output;
  on_retry is the hook if it wants to say something.
  No swallowing. A failure that runs out of attempts is re-raised exactly as
  it arrived, traceback included, so the caller sees the real cause instead
  of a wrapper exception that threw the detail away.
  No async variant and no thread pool. Callers are plain blocking code.
  No wall-clock deadline. Attempts plus a capped backoff are easier to reason
  about than a budget that can expire halfway through a request.

Timing and randomness are injectable (sleep_fn, rand, classify_fn) so the
whole policy is testable with no sockets and no real waiting. Standard
library only.
"""

import argparse
import random
import socket
import sys
import time
import urllib.error

RETRY = "retry"
FATAL = "fatal"
UNKNOWN = "unknown"

# 408 request timeout, 425 too early, 429 rate limited, and the 5xx family:
# all of these mean "ask again later" rather than "you asked wrongly".
RETRY_STATUS = frozenset((408, 425, 429, 500, 502, 503, 504))
# Bad request, unauthorized, forbidden, not found, conflict, unprocessable:
# the request itself is wrong, so the same request stays wrong forever.
FATAL_STATUS = frozenset((400, 401, 403, 404, 409, 422))

# Socket level failures worth another go. A refused connection is the normal
# shape of "llama-server has not finished binding its port yet", and a reset
# is the normal shape of "the MCP server restarted under us".
RETRY_EXCEPTIONS = (
    ConnectionRefusedError,
    ConnectionResetError,
    TimeoutError,
    socket.timeout,
)

DEFAULT_ATTEMPTS = 3
DEFAULT_BASE = 0.5
DEFAULT_CAP = 30.0


def classify(exc_or_status):
    """Return RETRY, FATAL or UNKNOWN for an exception or an HTTP status.

    Accepts an int status code, a urllib.error.HTTPError (its status is read
    from .code), a urllib.error.URLError, or any OSError flavour. Anything not
    in the tables is UNKNOWN, which call() retries once; see the module
    docstring for why that is the middle answer and not either extreme."""
    status = None
    if isinstance(exc_or_status, bool):
        return UNKNOWN
    if isinstance(exc_or_status, int):
        status = exc_or_status
    elif isinstance(exc_or_status, urllib.error.HTTPError):
        status = getattr(exc_or_status, "code", None)
    if status is not None:
        if status in RETRY_STATUS:
            return RETRY
        if status in FATAL_STATUS:
            return FATAL
        return UNKNOWN
    if isinstance(exc_or_status, RETRY_EXCEPTIONS):
        return RETRY
    # URLError covers dns failure, refused connection and the rest of the
    # transport layer as urllib reports it. Checked after HTTPError because
    # HTTPError subclasses it and carries a far more precise answer.
    if isinstance(exc_or_status, urllib.error.URLError):
        return RETRY
    return UNKNOWN


def delays(attempts, base=DEFAULT_BASE, cap=DEFAULT_CAP, jitter=True,
           rand=None):
    """Yield one wait, in seconds, per attempt: base, base*2, base*4 and so
    on, each clamped to cap.

    With jitter (the default) each wait is drawn from [0, ceiling] rather than
    being the ceiling itself. Full jitter, not a small wobble, because every
    hearth agent that trips over the same cold engine would otherwise wake in
    lockstep and retry as one synchronised herd. rand is the source of
    randomness, any zero-argument callable returning a float in [0, 1); it is
    injectable so a test can pin the whole sequence."""
    if attempts < 0:
        raise ValueError("attempts must not be negative")
    if base <= 0 or cap <= 0:
        raise ValueError("base and cap must be positive")
    source = rand if rand is not None else random.random
    for i in range(attempts):
        ceiling = min(cap, base * (2 ** i))
        yield ceiling * source() if jitter else ceiling


def _retry_after(exc, cap=DEFAULT_CAP):
    """Seconds the server asked us to wait, or None.

    Only honoured on a 429, and only in its numeric-seconds form: the HTTP
    date form is rare from the services hearth talks to and parsing it buys
    nothing. Clamped to cap so a hostile or confused "Retry-After: 86400"
    cannot wedge an agent for a day."""
    if not isinstance(exc, urllib.error.HTTPError):
        return None
    if getattr(exc, "code", None) != 429:
        return None
    headers = getattr(exc, "headers", None)
    raw = ""
    if headers is not None:
        try:
            raw = headers.get("Retry-After", "") or ""
        except Exception:  # noqa: BLE001 - an odd header store is just absent
            raw = ""
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return min(seconds, cap)


def call(fn, attempts=DEFAULT_ATTEMPTS, on_retry=None, sleep_fn=None,
         rand=None, classify_fn=None):
    """Run fn() and retry it only when the failure is worth retrying.

    A FATAL verdict is re-raised on the spot without spending an attempt or a
    wait, so a 404 for a model that does not exist costs exactly one request.
    A RETRY verdict waits and goes again until attempts is used up. An UNKNOWN
    verdict is retried once in total. When the budget runs out the LAST
    exception is re-raised unchanged, traceback included.

    on_retry(attempt, exc, delay) is called before each wait, if given, and
    its own failures are ignored: a noisy progress hook must not be the thing
    that breaks the call. sleep_fn, rand and classify_fn are the injection
    points that make this testable without waiting or networking."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    sleeper = sleep_fn if sleep_fn is not None else time.sleep
    decide = classify_fn if classify_fn is not None else classify
    plan = list(delays(attempts, rand=rand))
    unknown_spent = False
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - the verdict decides its fate
            verdict = decide(exc)
            if verdict == FATAL:
                raise
            if verdict == UNKNOWN:
                if unknown_spent:
                    raise
                unknown_spent = True
            if attempt >= attempts:
                raise
            delay = _retry_after(exc)
            if delay is None:
                delay = plan[attempt - 1]
            if on_retry is not None:
                try:
                    on_retry(attempt, exc, delay)
                except Exception:  # noqa: BLE001 - a hook cannot fail the call
                    pass
            sleeper(delay)


def _http_error(status, retry_after=None):
    """Build a urllib HTTPError the way urllib raises one, for the self-test."""
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://example.invalid/x", status,
                                  "status {}".format(status), headers, None)


def _self_test():
    # classification: every mapped status, as a bare int and as an HTTPError
    for status in sorted(RETRY_STATUS):
        assert classify(status) == RETRY, status
        assert classify(_http_error(status)) == RETRY, status
    for status in sorted(FATAL_STATUS):
        assert classify(status) == FATAL, status
        assert classify(_http_error(status)) == FATAL, status
    for status in (402, 418, 451, 598):
        assert classify(status) == UNKNOWN, status
        assert classify(_http_error(status)) == UNKNOWN, status

    # transport failures: the shapes a cold engine and a restarting MCP
    # server actually produce
    assert classify(urllib.error.URLError("connection refused")) == RETRY
    assert classify(ConnectionRefusedError("engine still starting")) == RETRY
    assert classify(ConnectionResetError("mcp restarted")) == RETRY
    assert classify(TimeoutError("slow")) == RETRY
    assert classify(socket.timeout("slow")) == RETRY
    # anything unrecognised, our own bugs included, is UNKNOWN not RETRY
    assert classify(ValueError("bad json")) == UNKNOWN
    assert classify(True) == UNKNOWN, "a bool is not a status code"

    # delays without jitter: doubling, clamped, monotonic non-decreasing
    seq = list(delays(6, base=0.5, cap=4.0, jitter=False))
    assert seq == [0.5, 1.0, 2.0, 4.0, 4.0, 4.0], seq
    for a, b in zip(seq, seq[1:]):
        assert b >= a, seq
    for d in seq:
        assert 0 <= d <= 4.0, seq
    assert list(delays(0)) == []

    # jitter stays inside [0, ceiling] for every draw the source can give
    for value in (0.0, 0.25, 0.5, 0.999):
        jittered = list(delays(6, base=0.5, cap=4.0,
                               rand=lambda v=value: v))
        for d, ceiling in zip(jittered, seq):
            assert 0.0 <= d <= ceiling, (d, ceiling, value)
        assert jittered[0] == 0.5 * value, jittered
    # a source at its maximum reproduces the un-jittered ceilings exactly
    assert list(delays(6, base=0.5, cap=4.0, rand=_one)) == seq

    sleeps = []
    spy_sleep = sleeps.append

    # a fatal fails on the first attempt: exactly one call, nothing slept
    calls = []

    def fatal_fn():
        calls.append(1)
        raise _http_error(404)

    try:
        call(fatal_fn, attempts=5, sleep_fn=spy_sleep, rand=_one)
    except urllib.error.HTTPError as exc:
        assert exc.code == 404, exc.code
    else:
        raise AssertionError("a 404 must not be retried")
    assert len(calls) == 1, calls
    assert sleeps == [], sleeps

    # a retryable succeeds on the third of three attempts, and sleep_fn saw
    # the expected backoff sequence
    del sleeps[:]
    calls = []

    def flaky_fn():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionRefusedError("engine still starting")
        return "ready"

    retries = []
    got = call(flaky_fn, attempts=3,
               on_retry=lambda a, e, d: retries.append((a, d)),
               sleep_fn=spy_sleep, rand=_one)
    assert got == "ready", got
    assert len(calls) == 3, calls
    assert sleeps == [0.5, 1.0], sleeps
    assert retries == [(1, 0.5), (2, 1.0)], retries

    # exhaustion re-raises the final error itself, not a wrapper
    del sleeps[:]
    calls = []
    errors = [ConnectionResetError("first"), ConnectionResetError("second"),
              ConnectionResetError("last")]

    def always_fn():
        calls.append(1)
        raise errors[len(calls) - 1]

    try:
        call(always_fn, attempts=3, sleep_fn=spy_sleep, rand=_one)
    except ConnectionResetError as exc:
        assert str(exc) == "last", str(exc)
        assert exc is errors[2], "the LAST exception must come back out"
    else:
        raise AssertionError("exhaustion must re-raise")
    assert len(calls) == 3, calls
    assert sleeps == [0.5, 1.0], sleeps

    # unknown is retried exactly once: two calls, never the full budget
    del sleeps[:]
    calls = []

    def odd_fn():
        calls.append(1)
        raise ValueError("unmapped")

    try:
        call(odd_fn, attempts=5, sleep_fn=spy_sleep, rand=_one)
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown failure must surface")
    assert len(calls) == 2, calls
    assert sleeps == [0.5], sleeps

    # an unknown failure that clears on its one retry still returns
    del sleeps[:]
    calls = []

    def odd_once_fn():
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("blip")
        return "ok"

    assert call(odd_once_fn, attempts=5, sleep_fn=spy_sleep, rand=_one) == "ok"
    assert len(calls) == 2, calls

    # a 429 with Retry-After waits what the server asked for, clamped to cap
    del sleeps[:]
    calls = []

    def throttled_fn():
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(429, "7")
        if len(calls) == 2:
            raise _http_error(429, "86400")
        return "through"

    assert call(throttled_fn, attempts=3, sleep_fn=spy_sleep,
                rand=_one) == "through"
    assert sleeps == [7.0, DEFAULT_CAP], sleeps

    # a missing or unparseable Retry-After falls back to the backoff plan
    del sleeps[:]
    calls = []

    def throttled_bare_fn():
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(429)
        if len(calls) == 2:
            raise _http_error(429, "Wed, 21 Oct 2015 07:28:00 GMT")
        return "through"

    assert call(throttled_bare_fn, attempts=3, sleep_fn=spy_sleep,
                rand=_one) == "through"
    assert sleeps == [0.5, 1.0], sleeps

    # one attempt means one call and no wait at all
    del sleeps[:]
    calls = []

    def single_fn():
        calls.append(1)
        raise TimeoutError("slow")

    try:
        call(single_fn, attempts=1, sleep_fn=spy_sleep, rand=_one)
    except TimeoutError:
        pass
    else:
        raise AssertionError("a single attempt must not retry")
    assert len(calls) == 1, calls
    assert sleeps == [], sleeps

    # a success never sleeps, and classify_fn is a real override
    del sleeps[:]
    assert call(lambda: 7, sleep_fn=spy_sleep, rand=_one) == 7
    assert sleeps == [], sleeps
    calls = []

    def hopeless_fn():
        calls.append(1)
        raise ConnectionRefusedError("looks retryable, is not")

    try:
        call(hopeless_fn, attempts=4, sleep_fn=spy_sleep, rand=_one,
             classify_fn=lambda exc: FATAL)
    except ConnectionRefusedError:
        pass
    else:
        raise AssertionError("classify_fn must decide")
    assert len(calls) == 1, calls

    # a raising on_retry hook is ignored rather than failing the call
    del sleeps[:]
    calls = []

    def flaky_again_fn():
        calls.append(1)
        if len(calls) < 2:
            raise ConnectionResetError("blip")
        return "fine"

    def boom(attempt, exc, delay):
        raise RuntimeError("hook exploded")

    assert call(flaky_again_fn, attempts=2, on_retry=boom, sleep_fn=spy_sleep,
                rand=_one) == "fine"
    assert sleeps == [0.5], sleeps

    # bad arguments are rejected rather than quietly doing something odd
    for bad in (0, -1):
        try:
            call(lambda: None, attempts=bad, sleep_fn=spy_sleep)
        except ValueError:
            pass
        else:
            raise AssertionError("attempts={} must be rejected".format(bad))
    try:
        list(delays(3, base=0))
    except ValueError:
        pass
    else:
        raise AssertionError("base=0 must be rejected")

    print("hearth-retry self-test OK")
    return 0


def _one():
    """A randomness source pinned to its maximum, so the self-test's jittered
    waits land exactly on the un-jittered ceilings and can be asserted."""
    return 1.0


def main(argv=None):
    p = argparse.ArgumentParser(prog="hearth-retry")
    p.add_argument("--classify", metavar="STATUS",
                   help="print the verdict for an HTTP status code")
    p.add_argument("--plan", type=int, metavar="N",
                   help="print the un-jittered backoff plan for N attempts")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    if a.classify:
        try:
            status = int(a.classify)
        except ValueError:
            p.error("--classify takes an integer HTTP status code")
        print(classify(status))
        return 0
    if a.plan:
        print(" ".join("{:.2f}".format(d)
                       for d in delays(a.plan, jitter=False)))
        return 0
    p.error("nothing to do (use --self-test, --classify or --plan)")


if __name__ == "__main__":
    sys.exit(main())
