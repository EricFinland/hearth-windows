#!/usr/bin/env python3
"""hearth quota: per-agent and per-project token quotas.

hearth_budget.py is one global daily cap for the whole hearth. It answers a
single question, "is this box over budget today?", and that is the wrong
shape for a box running several agents at once: one runaway agent can eat
the entire global cap by itself and starve every other agent even though the
hearth is nowhere near a real spend problem. This module adds the dimension
the global cap is missing, a scope, so each agent (or each project an agent
belongs to) carries its own ceiling over a rolling window, and a misbehaving
agent is the only thing that stops.

Scopes:
  agent     the agent_name column exactly as the audit db wrote it.
  project   the leading segment of agent_name, up to the first "/" or ":",
            so "acme/writer" and "acme:poster" both bill to "acme".
            agent_runs has no project column and this module deliberately
            does not add one: the name is the only grouping key that already
            exists, which keeps this a pure read of the current schema.

Windows are rolling, not calendar: 1h, 24h and 7d, measured back from `now`.
`now` is always a parameter and is never read from the clock inside the query
path, so a caller (and the self-test) can pin time exactly.

Config, read from the environment:
  HEARTH_QUOTA_AGENT_<NAME>      token ceiling for one agent
  HEARTH_QUOTA_AGENT_DEFAULT     ceiling for an agent without its own
  HEARTH_QUOTA_PROJECT_<NAME>    token ceiling for one project
  HEARTH_QUOTA_PROJECT_DEFAULT   ceiling for a project without its own
<NAME> is the scope key upper-cased with every non-alphanumeric character
replaced by "_", so HEARTH_QUOTA_AGENT_ACME_WRITER covers "acme/writer".

Parsing is lenient in one direction only, and on purpose: a value that is
junk, negative, or zero means "no quota at all", never "a quota of zero
tokens". A typo that silently blocks every run of every agent is far worse
than a typo that leaves a ceiling switched off, because the first looks like
a broken hearth and costs a debugging session, while the second looks exactly
like the state the box was already in. So a malformed quota fails open, and
the CLI prints "-" in the quota column so a human can still see the mistake.

Best effort on the data side too: a missing db file, a db with no agent_runs
table, an unknown agent, and a row whose started_at is unreadable all report
zero usage instead of raising. A quota check must never be the thing that
takes the loop down.

What this deliberately does NOT do: it never writes to the audit db (every
statement here is a SELECT), it never kills or pauses an agent (it returns a
verdict and the caller decides what that means), it keeps no quota store of
its own (the environment is the whole configuration, so there is no state to
migrate or corrupt), it ignores cost_usd and latency_ms (tokens are the unit
the loop can actually act on before a call), and it does not replace
hearth_budget: the global daily cap still applies on top of these quotas.
Standard library only.
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hearth_paths  # noqa: E402

SCOPES = ("agent", "project")
WINDOWS = {"1h": 3600, "24h": 86400, "7d": 604800}
# The characters a project prefix may be separated by. The earliest
# occurrence of any of them in the name ends the project segment.
PROJECT_SEPARATORS = ("/", ":")
ZERO = {"tokens": 0, "runs": 0}


def window_seconds(window):
    """Length of a named rolling window in seconds. Raises ValueError for an
    unknown name: an unsupported window is a caller bug, not a data problem,
    and silently measuring the wrong span would be worse than a traceback."""
    try:
        return WINDOWS[window]
    except (KeyError, TypeError):
        raise ValueError("unknown window {!r} (want one of {})".format(
            window, ", ".join(sorted(WINDOWS))))


def project_of(agent_name):
    """The project an agent name bills to: everything before the first
    separator, or the whole name when it carries no separator."""
    name = agent_name or ""
    cut = len(name)
    for sep in PROJECT_SEPARATORS:
        i = name.find(sep)
        if 0 <= i < cut:
            cut = i
    return name[:cut]


def scope_key(scope, agent_name):
    """Fold one agent_name into the key the given scope bills it to."""
    if scope not in SCOPES:
        raise ValueError("unknown scope {!r} (want one of {})".format(
            scope, ", ".join(SCOPES)))
    if scope == "project":
        return project_of(agent_name)
    return agent_name or ""


def _env_name(key):
    """The environment-variable spelling of a scope key: upper-cased, with
    every non-ASCII-alphanumeric character flattened to an underscore."""
    out = []
    for ch in key:
        out.append(ch if (ch.isascii() and ch.isalnum()) else "_")
    return "".join(out).upper()


def _parse_limit(raw):
    """A configured ceiling as an int, where anything unusable means 0 (no
    quota). Junk, a negative number, and an explicit zero all land here."""
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def quota(scope, name):
    """The token ceiling configured for one scope key, 0 when there is none.
    A key's own variable wins over the scope DEFAULT, and a malformed value
    falls through to the default rather than to a quota of zero tokens."""
    if scope not in SCOPES:
        raise ValueError("unknown scope {!r}".format(scope))
    prefix = "HEARTH_QUOTA_{}_".format(scope.upper())
    env = _env_name(name or "")
    if env:
        own = _parse_limit(os.environ.get(prefix + env, ""))
        if own:
            return own
    return _parse_limit(os.environ.get(prefix + "DEFAULT", ""))


def _stamp(moment):
    """A datetime as the audit db spells its timestamps, to the second, so a
    comparison against substr(started_at,1,19) stays lexicographic."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def cutoff(window, now=None):
    """The instant a window starts at, as a comparable timestamp string."""
    now = now or datetime.now(timezone.utc)
    return _stamp(now - timedelta(seconds=window_seconds(window)))


def totals(db, scope, window, now=None):
    """Usage per scope key over the rolling window, as
    {key: {"tokens", "runs"}}. The window is closed at both ends, [now minus
    the window, now]: a run stamped after `now` is outside it, so a clock
    skew on one box cannot spend another box's quota, and a pinned `now`
    really does see only the past. Read-only and best effort: a missing db or
    table is an empty report, and a row whose started_at cannot be compared
    falls inside no window at all."""
    now = now or datetime.now(timezone.utc)
    since = cutoff(window, now=now)
    until = _stamp(now)
    try:
        con = sqlite3.connect(db, timeout=10)
        try:
            rows = con.execute(
                "SELECT COALESCE(agent_name,''), "
                "COALESCE(SUM(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)),0), "
                "COUNT(*) FROM agent_runs WHERE substr(started_at,1,19)>=? "
                "AND substr(started_at,1,19)<=? "
                "GROUP BY COALESCE(agent_name,'')", (since, until)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    out = {}
    for name, tokens, runs in rows:
        acc = out.setdefault(scope_key(scope, name), {"tokens": 0, "runs": 0})
        acc["tokens"] += int(tokens or 0)
        acc["runs"] += int(runs or 0)
    return out


def used(db, scope, name, window, now=None):
    """Tokens and runs one scope key spent over the rolling window. An
    unknown key, a missing db, and a missing table all read as zeros."""
    key = scope_key(scope, name)
    return dict(totals(db, scope, window, now=now).get(key, ZERO))


def check(db, scope, name, limit, window, now=None):
    """One-call quota verdict for a scope key:
    {"scope", "name", "window", "used", "runs", "limit", "remaining",
     "pct", "allowed"}.

    limit is passed in (usually from quota()) so a caller can override the
    environment for a single decision. A limit that is not a positive int
    means no quota, so allowed is True, remaining is 0 (unbounded headroom
    has no honest number) and pct is 0.0. With a quota set, spend equal to
    the limit is already over: allowed flips to False at the boundary, the
    same way hearth_budget treats its global cap."""
    key = scope_key(scope, name)
    cap = _parse_limit(limit)
    u = used(db, scope, key, window, now=now)
    tokens = u["tokens"]
    verdict = {"scope": scope, "name": key, "window": window,
               "used": tokens, "runs": u["runs"], "limit": cap}
    if cap <= 0:
        verdict.update({"remaining": 0, "pct": 0.0, "allowed": True})
        return verdict
    verdict.update({"remaining": max(0, cap - tokens),
                    "pct": round(100.0 * tokens / cap, 1),
                    "allowed": tokens < cap})
    return verdict


def report(db, scope, window, now=None):
    """A verdict for every scope key seen in the window, in name order. Keys
    with no usage are absent: this reports what the window holds, not what
    the environment happens to configure."""
    seen = totals(db, scope, window, now=now)
    return [check(db, scope, key, quota(scope, key), window, now=now)
            for key in sorted(seen)]


def _self_test():
    import tempfile
    d = tempfile.mkdtemp(prefix="hearth-quota-")
    db = os.path.join(d, "audit.db")
    empty = os.path.join(d, "empty.db")
    now = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)

    saved = {k: v for k, v in os.environ.items()
             if k.startswith("HEARTH_QUOTA_")}
    for k in saved:
        os.environ.pop(k, None)
    try:
        # window names and the cutoffs they compute
        assert window_seconds("1h") == 3600
        assert cutoff("1h", now=now) == "2026-07-19T11:00:00"
        assert cutoff("24h", now=now) == "2026-07-18T12:00:00"
        assert cutoff("7d", now=now) == "2026-07-12T12:00:00"
        for bad in ("2h", "", "1d", None):
            try:
                window_seconds(bad)
            except ValueError:
                pass
            else:
                raise AssertionError("window {!r} parsed".format(bad))

        # an unknown scope is a caller bug, not a silent default
        for bad in ("box", "", "AGENT"):
            try:
                scope_key(bad, "a")
            except ValueError:
                pass
            else:
                raise AssertionError("scope {!r} parsed".format(bad))

        # project folding, including a name with no separator at all and one
        # carrying both separators (the earliest one wins)
        assert project_of("acme/writer") == "acme"
        assert project_of("acme:poster") == "acme"
        assert project_of("acme:deep/writer") == "acme"
        assert project_of("solo") == "solo"
        assert project_of("") == ""

        # missing db file: zero usage, empty report, nothing raised
        assert used(db, "agent", "a", "24h", now=now) == ZERO, "missing db"
        assert totals(db, "agent", "24h", now=now) == {}, "missing db"
        assert report(db, "agent", "24h", now=now) == [], "missing db"

        # a db that exists but has no agent_runs table reads the same way
        con = sqlite3.connect(empty)
        con.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
        con.commit()
        con.close()
        assert used(empty, "agent", "a", "1h", now=now) == ZERO, "no table"

        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE agent_runs (id INTEGER PRIMARY KEY, agent_name TEXT, "
            "run_id TEXT, started_at TEXT, finished_at TEXT, tokens_in INTEGER, "
            "tokens_out INTEGER, cost_usd REAL, latency_ms INTEGER, error TEXT, "
            "model TEXT)")
        rows = [
            ("a", "r1", "2026-07-19T11:30:00+00:00", 100, 50),     # 1h
            ("a", "r2", "2026-07-19T03:00:00+00:00", 200, None),   # 24h
            ("a", "r3", "2026-07-10T00:00:00+00:00", 1000, 1000),  # past 7d
            ("b", "r4", "2026-07-15T00:00:00+00:00", 7, 3),        # 7d only
            ("acme/writer", "r5", "2026-07-19T11:45:00+00:00", 10, 5),
            ("acme:poster", "r6", "2026-07-19T11:50:00+00:00", 1, 1),
            ("ghost", "r7", None, 500, 500),                       # no stamp
            ("edge", "r8", "2026-07-19T12:00:00+00:00", 2, 3),     # at now
            ("ahead", "r9", "2026-07-19T12:00:01+00:00", 900, 900),  # future
        ]
        for name, rid, ts, tin, tout in rows:
            con.execute(
                "INSERT INTO agent_runs (agent_name, run_id, started_at, "
                "finished_at, tokens_in, tokens_out, cost_usd, latency_ms, "
                "error, model) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (name, rid, ts, ts, tin, tout, 0.0, 1, None, "mock"))
        con.commit()
        con.close()

        # rolling windows: each one holds strictly more of the same agent,
        # and the row older than 7d never appears in any of them
        assert used(db, "agent", "a", "1h", now=now) == {"tokens": 150, "runs": 1}
        assert used(db, "agent", "a", "24h", now=now) == {"tokens": 350, "runs": 2}
        assert used(db, "agent", "a", "7d", now=now) == {"tokens": 350, "runs": 2}
        # a NULL token column counts as zero, not as a broken row
        assert used(db, "agent", "b", "1h", now=now) == ZERO, "outside 1h"
        assert used(db, "agent", "b", "7d", now=now) == {"tokens": 10, "runs": 1}
        # an unreadable started_at falls in no window, and neither does a
        # run stamped after `now`, while one stamped exactly at `now` counts
        for w in sorted(WINDOWS):
            assert used(db, "agent", "ghost", w, now=now) == ZERO, w
            assert used(db, "agent", "ahead", w, now=now) == ZERO, w
            assert used(db, "agent", "edge", w, now=now) == {
                "tokens": 5, "runs": 1}, w
        # an agent that never ran reports zeros rather than raising
        assert used(db, "agent", "nobody", "7d", now=now) == ZERO, "unknown"

        # project scope aggregates the two acme agents into one bill
        assert used(db, "agent", "acme/writer", "1h", now=now) == {
            "tokens": 15, "runs": 1}
        assert used(db, "project", "acme", "1h", now=now) == {
            "tokens": 17, "runs": 2}
        assert used(db, "project", "a", "24h", now=now) == {
            "tokens": 350, "runs": 2}

        # quota(): unset, junk, negative and zero all mean "no quota", so a
        # typo can never block every run of an agent
        assert quota("agent", "a") == 0, "unset -> no quota"
        for bad in ("banana", "-5", "0", "", "  ", "1e5", "200_000.5"):
            os.environ["HEARTH_QUOTA_AGENT_A"] = bad
            assert quota("agent", "a") == 0, bad
        os.environ["HEARTH_QUOTA_AGENT_A"] = " 200000 "
        assert quota("agent", "a") == 200000, "whitespace tolerated"

        # DEFAULT covers an agent with no variable of its own, and a real
        # value for that agent wins over DEFAULT
        os.environ["HEARTH_QUOTA_AGENT_DEFAULT"] = "1000"
        assert quota("agent", "b") == 1000, "default applies"
        assert quota("agent", "a") == 200000, "own beats default"
        # a malformed own value falls through to DEFAULT, not to zero tokens
        os.environ["HEARTH_QUOTA_AGENT_A"] = "lots"
        assert quota("agent", "a") == 1000, "junk own -> default"
        os.environ["HEARTH_QUOTA_AGENT_A"] = "400"

        # name normalization: separators and case become underscores
        assert _env_name("acme:poster") == "ACME_POSTER"
        os.environ["HEARTH_QUOTA_AGENT_ACME_WRITER"] = "12"
        assert quota("agent", "acme/writer") == 12, _env_name("acme/writer")
        assert quota("agent", "acme:poster") == 1000, "no own -> default"
        # the project scope reads its own prefix, not the agent one
        assert quota("project", "acme") == 0, "agent vars do not leak"
        os.environ["HEARTH_QUOTA_PROJECT_ACME"] = "20"
        assert quota("project", "acme") == 20

        # check(): under, at, and over the ceiling
        v = check(db, "agent", "a", 400, "24h", now=now)
        assert v == {"scope": "agent", "name": "a", "window": "24h",
                     "used": 350, "runs": 2, "limit": 400, "remaining": 50,
                     "pct": 87.5, "allowed": True}, v
        # boundary: spend equal to the quota is already over
        v2 = check(db, "agent", "a", 350, "24h", now=now)
        assert v2["allowed"] is False and v2["remaining"] == 0, v2
        assert v2["pct"] == 100.0, v2
        # one token of headroom is still allowed
        assert check(db, "agent", "a", 351, "24h", now=now)["allowed"] is True
        # well over: remaining floors at zero, pct keeps climbing
        v3 = check(db, "agent", "a", 100, "24h", now=now)
        assert v3["remaining"] == 0 and v3["pct"] == 350.0, v3
        assert v3["allowed"] is False, v3
        # no quota, in every malformed spelling: always allowed
        for bad in (0, -5, "banana", None, ""):
            v4 = check(db, "agent", "a", bad, "24h", now=now)
            assert v4["allowed"] is True, bad
            assert v4["limit"] == 0 and v4["remaining"] == 0, bad
            assert v4["pct"] == 0.0 and v4["used"] == 350, bad
        # an agent with no runs is allowed even against a tiny quota
        v5 = check(db, "agent", "nobody", 1, "7d", now=now)
        assert v5["allowed"] is True and v5["used"] == 0, v5
        assert v5["remaining"] == 1 and v5["pct"] == 0.0, v5
        # a project check bills both of its agents together
        v6 = check(db, "project", "acme", 17, "1h", now=now)
        assert v6["allowed"] is False and v6["used"] == 17, v6
        # an agent name resolves to its project key under project scope
        v7 = check(db, "project", "acme/writer", 20, "1h", now=now)
        assert v7["name"] == "acme" and v7["used"] == 17, v7

        # report(): every agent seen in the window, in name order, each
        # against the quota the environment gives it
        rep = report(db, "agent", "1h", now=now)
        assert [r["name"] for r in rep] == ["a", "acme/writer",
                                            "acme:poster", "edge"], rep
        assert [r["limit"] for r in rep] == [400, 12, 1000, 1000], rep
        assert [r["allowed"] for r in rep] == [True, False, True, True], rep
        # agents with no usage in the window are absent from the report
        for absent in ("b", "ghost", "ahead"):
            assert absent not in [r["name"] for r in rep], absent
        assert [r["name"] for r in report(db, "project", "1h", now=now)] == [
            "a", "acme", "edge"], "project scope folds names"

        # the CLI exits nonzero exactly when some agent is over its quota
        assert main(["--db", db, "--window", "1h", "--now",
                     "2026-07-19T12:00:00"]) == 1, "an agent is over"
        os.environ["HEARTH_QUOTA_AGENT_ACME_WRITER"] = "500"
        assert main(["--db", db, "--window", "1h", "--now",
                     "2026-07-19T12:00:00"]) == 0, "everyone under"
        # an empty window is not a failure
        assert main(["--db", db, "--window", "1h", "--now",
                     "2020-01-01T00:00:00"]) == 0, "no runs -> ok"
        # a missing db is not a failure either
        assert main(["--db", os.path.join(d, "nope.db")]) == 0, "missing db"
    finally:
        for k in [k for k in os.environ if k.startswith("HEARTH_QUOTA_")]:
            os.environ.pop(k, None)
        os.environ.update(saved)

    print("hearth-quota self-test OK")
    return 0


def _print_report(rows, scope, window):
    """The operator table: one line per scope key seen in the window, with
    "-" where no quota is configured (or where one failed to parse)."""
    print("{:<28} {:>10} {:>10} {:>8} {:>6} {:>4}".format(
        scope, "tokens", "quota", "pct", "runs", "ok"))
    for r in rows:
        print("{:<28} {:>10} {:>10} {:>8} {:>6} {:>4}".format(
            r["name"][:28] or "(unnamed)", r["used"],
            r["limit"] if r["limit"] else "-",
            "{:.1f}%".format(r["pct"]) if r["limit"] else "-",
            r["runs"], "yes" if r["allowed"] else "NO"))
    over = [r["name"] for r in rows if not r["allowed"]]
    print("window={} seen={} over={}".format(
        window, len(rows), ",".join(over) if over else "none"))


def main(argv=None):
    p = argparse.ArgumentParser(prog="hearth-quota")
    p.add_argument("--db", default=hearth_paths.db_path())
    p.add_argument("--scope", default="agent", choices=list(SCOPES))
    p.add_argument("--window", default="24h", choices=sorted(WINDOWS))
    p.add_argument("--now", help="pin the window end, ISO-8601 UTC")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    now = None
    if a.now:
        try:
            now = datetime.fromisoformat(a.now)
        except ValueError:
            p.error("--now is not an ISO-8601 timestamp: {}".format(a.now))
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
    rows = report(a.db, a.scope, a.window, now=now)
    _print_report(rows, a.scope, a.window)
    return 1 if any(not r["allowed"] for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
