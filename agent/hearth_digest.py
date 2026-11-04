#!/usr/bin/env python3
r"""hearth digest: what happened while you were asleep.

    python agent/hearth_digest.py
    python agent/hearth_digest.py --since 24h
    python agent/hearth_digest.py --since 7d --json
    python agent/hearth_digest.py --since 24h --fail-threshold 0.25

Hearth already has a doctor for health, a budget breaker for spend, a ledger
for what the tokens would have cost hosted, and a shop for models. Every one
of those answers a different question well, and not one of them answers the
question you actually have over the first coffee: the box ran unattended all
night, so what happened? This is that one screen. Last night's runs, what
they cost, what broke, which agent is the problem, and the single line worth
reading if you read nothing else.

WHAT IT READS
-------------
The agent_runs table, and nothing else: id, agent_name, run_id, started_at,
finished_at, tokens_in, tokens_out, cost_usd, latency_ms, error, model. Those
rows are written by the loop as it works, so this module adds no bookkeeping
of its own and cannot disagree with the audit log. A null error means the run
succeeded; any non-empty error string means it failed. A null agent_name or
model reads as "unknown" rather than being dropped, because a run nobody
claims is exactly the run you want to see.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not write. The database is opened read-only through a mode=ro URI, so
a digest on a box with no audit database yet reports zeros and leaves the
filesystem exactly as it found it: a reporting tool that creates the thing it
reports on is a reporting tool you cannot trust. It does not alert, page, or
notify; hearth_notify owns that, and a screen you have to read on purpose is
a different thing from a message that wakes you up. It does not retry, pause
or kill anything, so running it can never change what the box does next. It
does not define its own cost model: it asks hearth_ledger, and if that module
is not importable it simply reports no cost line rather than inventing a
second set of rates that could drift away from the first.

WHY THE BLAME RULE HAS A FLOOR
------------------------------
The most error-prone agent is ranked by failure rate, not failure count, so a
busy agent failing a tenth of the time does not hide behind a quiet one. But
rate alone is worthless at small n: an agent that ran once and failed scores
a perfect 1.0 and would top the chart forever, which is both useless and
alarming. So an agent needs at least MIN_RUNS_FOR_BLAME runs in the window
before it can be named. When nobody clears that bar the digest says so
instead of pointing at the loudest accident.

Pure and injectable: you pass the database path, the window, and `now`, so
the whole thing is testable with no clock, no real audit database and no
network. Standard library only.
"""

import argparse
import json
import os
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hearth_paths  # noqa: E402

# An agent must have run at least this many times in the window before it can
# be named as the most error-prone one. See the docstring: without a floor, a
# single failed run scores 1.0 and tops the chart forever.
MIN_RUNS_FOR_BLAME = 3

PANEL_WIDTH = 60
UNKNOWN = "unknown"


def parse_since(text, now=None):
    """A cutoff from "24h", "7d", "2026-07-01", or None for all of it.

    Returns a datetime or None. Raises ValueError on anything else: quietly
    reporting all of history when the operator asked for last night would
    make the digest lie about the window it covers."""
    if text is None or not str(text).strip():
        return None
    now = now or datetime.now(timezone.utc)
    s = str(text).strip()
    m = re.fullmatch(r"(\d+)([dh])", s)
    if m:
        n = int(m.group(1))
        return now - (timedelta(days=n) if m.group(2) == "d" else timedelta(hours=n))
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(
            "since must be like 24h, 7d or 2026-07-01, not {!r}".format(s))


def _median(values):
    """Median latency, or None when nothing recorded one. A single sample is
    its own median, which is what statistics.median already does."""
    if not values:
        return None
    return float(statistics.median(values))


def _p95(values):
    """95th percentile latency, or None when nothing recorded one.

    statistics.quantiles needs at least two data points, so one run is its
    own p95: with a single sample there is no spread to report and the
    alternative (None) would make a one-run digest look broken."""
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    return float(statistics.quantiles(values, n=20, method="inclusive")[18])


def _ledger_cost(db, cutoff):
    """Hosted-equivalent dollars for the window, from hearth_ledger.

    Returns None when the ledger is not importable or cannot price the
    window. The digest is useful without a cost line and must never fail to
    render because a sibling module moved, so this never raises."""
    try:
        import hearth_ledger
    except Exception:  # noqa: BLE001 - the cost line is optional, by design
        return None
    try:
        return float(hearth_ledger.summarize(db, since=cutoff)["usd"])
    except Exception:  # noqa: BLE001 - the cost line is optional, by design
        return None


def _empty(cutoff):
    """The digest of a box with no audit database, no table, or no runs.

    Every rate and percentile is None or zero rather than a division, so the
    zero-run case cannot divide by anything."""
    return {
        "since": cutoff.isoformat() if cutoff else None,
        "runs": 0, "successes": 0, "failures": 0, "failure_rate": 0.0,
        "tokens_in": 0, "tokens_out": 0, "tokens": 0,
        "latency_median_ms": None, "latency_p95_ms": None,
        "busiest_agent": None, "busiest_agent_runs": 0,
        "worst_agent": None, "worst_agent_runs": 0,
        "worst_agent_failures": 0, "worst_agent_rate": None,
        "models": [],
        "headline": "nothing ran in this window",
    }


def _rows(db, cutoff):
    """The raw rows for the window, read-only. Returns [] for a missing
    database, a missing agent_runs table, or a schema this does not know."""
    try:
        con = sqlite3.connect("file:{}?mode=ro".format(db), uri=True)
    except sqlite3.Error:
        return []
    try:
        sql = ("SELECT agent_name, model, tokens_in, tokens_out, latency_ms, "
               "error FROM agent_runs")
        args = ()
        if cutoff is not None:
            sql += " WHERE started_at >= ?"
            args = (cutoff.isoformat(),)
        return con.execute(sql + " ORDER BY started_at, id", args).fetchall()
    except sqlite3.Error:
        # No agent_runs table yet, or a schema this does not know. A fresh
        # install has nothing to report, which is not an error.
        return []
    finally:
        con.close()


def _failed(error):
    """A null or blank error is a success; any other string is a failure."""
    return bool(error is not None and str(error).strip())


def _headline(d):
    """The one line worth reading. Names the single most important thing:
    nothing ran, everything worked, or who is breaking and how often."""
    if d["runs"] == 0:
        return "nothing ran in this window"
    if d["failures"] == 0:
        return "all {} runs succeeded".format(d["runs"])
    pct = 100.0 * d["failure_rate"]
    if d["worst_agent"] is not None:
        return "{} failed {} of its {} runs ({:.0f}% of all runs failed)".format(
            d["worst_agent"], d["worst_agent_failures"],
            d["worst_agent_runs"], pct)
    return "{} of {} runs failed, no agent has enough runs to blame yet".format(
        d["failures"], d["runs"])


def collect(db, since=None, now=None, cost_fn=None):
    """Read agent_runs for the window and answer "how is the box doing".

    Read-only and best effort: a missing database, a missing agent_runs
    table, or zero runs in the window all return the empty digest rather than
    raising, and none of them creates the database. Returns a dict with run
    counts, failure rate, token totals, median and p95 latency, the busiest
    agent, the most error-prone agent (rate, floored at MIN_RUNS_FOR_BLAME
    runs), the models used, a one-line headline, and `usd` when the ledger
    could price the window."""
    cutoff = since if isinstance(since, datetime) or since is None else \
        parse_since(since, now)
    rows = _rows(db, cutoff)
    d = _empty(cutoff)

    usd = (cost_fn or _ledger_cost)(db, cutoff)
    if usd is not None:
        d["usd"] = usd

    if not rows:
        return d

    per_agent = {}
    per_model = {}
    latencies = []
    for agent, model, tin, tout, lat, error in rows:
        name = (agent or "").strip() or UNKNOWN
        mname = (model or "").strip() or UNKNOWN
        bad = _failed(error)
        a = per_agent.setdefault(name, {"runs": 0, "failures": 0})
        a["runs"] += 1
        a["failures"] += 1 if bad else 0
        per_model[mname] = per_model.get(mname, 0) + 1
        d["runs"] += 1
        d["failures"] += 1 if bad else 0
        d["tokens_in"] += int(tin or 0)
        d["tokens_out"] += int(tout or 0)
        if lat is not None:
            latencies.append(int(lat))

    d["successes"] = d["runs"] - d["failures"]
    # d["runs"] is at least 1 here: the zero-run case returned above, so this
    # is the only division in the module and it cannot divide by zero.
    d["failure_rate"] = d["failures"] / float(d["runs"])
    d["tokens"] = d["tokens_in"] + d["tokens_out"]
    d["latency_median_ms"] = _median(latencies)
    d["latency_p95_ms"] = _p95(latencies)

    # Busiest agent: most runs, name as the tiebreak so the digest is stable.
    busiest = sorted(per_agent.items(), key=lambda kv: (-kv[1]["runs"], kv[0]))[0]
    d["busiest_agent"] = busiest[0]
    d["busiest_agent_runs"] = busiest[1]["runs"]

    # Most error-prone agent: by rate, but only among agents with enough runs
    # for that rate to mean anything. Ties break on runs, then name.
    blamable = [(k, v) for k, v in per_agent.items()
                if v["runs"] >= MIN_RUNS_FOR_BLAME and v["failures"] > 0]
    if blamable:
        blamable.sort(key=lambda kv: (-(kv[1]["failures"] / float(kv[1]["runs"])),
                                      -kv[1]["runs"], kv[0]))
        name, v = blamable[0]
        d["worst_agent"] = name
        d["worst_agent_runs"] = v["runs"]
        d["worst_agent_failures"] = v["failures"]
        d["worst_agent_rate"] = v["failures"] / float(v["runs"])

    d["models"] = [m for m, _ in sorted(per_model.items(),
                                        key=lambda kv: (-kv[1], kv[0]))]
    d["headline"] = _headline(d)
    return d


def _money(usd):
    """Dollars, but never rounded to a flat zero when something was spent."""
    if usd and usd < 0.01:
        return "<$0.01"
    return "${:,.2f}".format(usd)


def _ms(value):
    if value is None:
        return "not recorded"
    return "{:,.0f} ms".format(value)


def format_report(digest):
    """The digest as a compact fixed-width panel, as the CLI prints it."""
    inner = PANEL_WIDTH - 4
    border = "+" + "-" * (PANEL_WIDTH - 2) + "+"

    def row(text=""):
        return "| {:<{w}} |".format(text[:inner], w=inner)

    def field(label, text):
        return row("{:<14}{}".format(label, text))

    window = ("since {}".format(digest["since"][:10]) if digest["since"]
              else "all time")
    lines = [border, row("hearth digest: {}".format(window)), border]
    lines.append(field("runs", "{:,}  ({:,} ok, {:,} failed)".format(
        digest["runs"], digest["successes"], digest["failures"])))
    lines.append(field("failure rate", "{:.1%}".format(digest["failure_rate"])))
    lines.append(field("tokens", "{:,} in, {:,} out, {:,} total".format(
        digest["tokens_in"], digest["tokens_out"], digest["tokens"])))
    if "usd" in digest:
        lines.append(field("hosted cost", "{} (ledger estimate)".format(
            _money(digest["usd"]))))
    if digest["latency_median_ms"] is None:
        lines.append(field("latency", "not recorded"))
    else:
        lines.append(field("latency", "{} median, {} p95".format(
            _ms(digest["latency_median_ms"]), _ms(digest["latency_p95_ms"]))))
    if digest["busiest_agent"]:
        lines.append(field("busiest", "{} ({:,} runs)".format(
            digest["busiest_agent"], digest["busiest_agent_runs"])))
    if digest["worst_agent"]:
        lines.append(field("most errors", "{} ({} of {}, {:.0%})".format(
            digest["worst_agent"], digest["worst_agent_failures"],
            digest["worst_agent_runs"], digest["worst_agent_rate"])))
    elif digest["failures"]:
        lines.append(field("most errors", "no agent has {} runs yet".format(
            MIN_RUNS_FOR_BLAME)))
    if digest["models"]:
        lines.append(field("models", ", ".join(digest["models"])))
    lines += [border, row(digest["headline"]), border]
    return "\n".join(lines)


def _self_test():
    import tempfile

    now = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
    d = tempfile.mkdtemp(prefix="hearth-digest-")
    missing = os.path.join(d, "not-there.db")
    other = os.path.join(d, "other.db")
    db = os.path.join(d, "audit.db")
    single = os.path.join(d, "single.db")
    allbad = os.path.join(d, "allbad.db")
    schema = ("CREATE TABLE agent_runs (id INTEGER PRIMARY KEY, agent_name TEXT, "
              "run_id TEXT, started_at TEXT, finished_at TEXT, tokens_in INTEGER, "
              "tokens_out INTEGER, cost_usd REAL, latency_ms INTEGER, error TEXT, "
              "model TEXT)")
    insert = ("INSERT INTO agent_runs (agent_name, run_id, started_at, "
              "finished_at, tokens_in, tokens_out, cost_usd, latency_ms, error, "
              "model) VALUES (?,?,?,?,?,?,?,?,?,?)")

    def write(path, rows):
        con = sqlite3.connect(path)
        con.execute(schema)
        for i, (agent, ts, tin, tout, lat, err, model) in enumerate(rows):
            con.execute(insert, (agent, "r{}".format(i), ts, ts, tin, tout,
                                 0.0, lat, err, model))
        con.commit()
        con.close()

    # No ledger in the loop: the cost line is optional and the maths below
    # must not depend on whatever rates the ledger happens to carry.
    def no_cost(_db, _cutoff):
        return None

    # parse_since: relative, absolute, nothing, and junk
    assert parse_since(None) is None and parse_since("   ") is None
    assert parse_since("24h", now) == datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    assert parse_since("7d", now) == datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    assert parse_since("2026-07-01") == datetime(2026, 7, 1, tzinfo=timezone.utc)
    for junk in ("banana", "7w", "", "  7 d", "2026-13-01"):
        if not str(junk).strip():
            continue
        try:
            parse_since(junk, now)
        except ValueError:
            pass
        else:
            raise AssertionError("junk since accepted: {!r}".format(junk))

    # Missing database: empty digest, no raise, and nothing created. A
    # reporting tool must never bring the thing it reports on into being.
    empty = collect(missing, since="24h", now=now, cost_fn=no_cost)
    assert empty["runs"] == 0 and empty["failures"] == 0, empty
    assert empty["failure_rate"] == 0.0, empty
    assert empty["latency_median_ms"] is None, empty
    assert empty["latency_p95_ms"] is None, empty
    assert empty["busiest_agent"] is None and empty["worst_agent"] is None, empty
    assert empty["models"] == [] and "nothing ran" in empty["headline"], empty
    assert not os.path.exists(missing), "collect() created the database"
    assert "nothing ran" in format_report(empty), format_report(empty)

    # A database without an agent_runs table reads the same way, and is not
    # touched either.
    con = sqlite3.connect(other)
    con.execute("CREATE TABLE something_else (x INTEGER)")
    con.commit()
    con.close()
    before = os.path.getsize(other)
    not_a_log = collect(other, now=now, cost_fn=no_cost)
    assert not_a_log["runs"] == 0, not_a_log
    assert "nothing ran" in not_a_log["headline"], not_a_log
    assert os.path.getsize(other) == before, "collect() wrote to the database"

    # The main fixture. Agent a runs four times and fails once; agent b runs
    # once and fails; one run claims neither an agent nor a model; one run of
    # agent a is older than the 7d window.
    write(db, [
        ("a", "2026-07-19T01:00:00+00:00", 100, 50, 100, None, "m1"),
        ("a", "2026-07-19T02:00:00+00:00", 100, 50, 200, "", "m1"),
        ("a", "2026-07-19T03:00:00+00:00", 100, 50, 300, "   ", "m1"),
        ("a", "2026-07-19T04:00:00+00:00", 100, 50, 400, "boom", "m1"),
        ("b", "2026-07-19T05:00:00+00:00", 10, 5, 500, "kaput", "m2"),
        (None, "2026-07-19T06:00:00+00:00", 1, 1, 600, None, None),
        ("a", "2026-07-10T00:00:00+00:00", 1000, 1000, 5000, None, "m1"),
    ])

    g = collect(db, since="7d", now=now, cost_fn=no_cost)
    assert g["runs"] == 6, g
    assert (g["successes"], g["failures"]) == (4, 2), g
    assert abs(g["failure_rate"] - (2 / 6.0)) < 1e-12, g
    assert (g["tokens_in"], g["tokens_out"], g["tokens"]) == (411, 206, 617), g
    # Null and blank error strings are successes; "boom" and "kaput" are not.
    assert g["latency_median_ms"] == 350.0, g
    assert g["latency_p95_ms"] == 575.0, g
    assert (g["busiest_agent"], g["busiest_agent_runs"]) == ("a", 4), g
    # Agent b failed its only run (rate 1.0) but has fewer than
    # MIN_RUNS_FOR_BLAME runs, so the floor keeps it off the chart.
    assert g["worst_agent"] == "a", g
    assert (g["worst_agent_failures"], g["worst_agent_runs"]) == (1, 4), g
    assert g["worst_agent_rate"] == 0.25, g
    # A run with no agent_name or model is reported, not dropped.
    assert UNKNOWN in g["models"], g
    assert g["models"] == ["m1", "m2", UNKNOWN], g
    assert "a failed 1 of its 4 runs" in g["headline"], g
    assert "usd" not in g, "cost omitted when the ledger says nothing"

    # The window is a window: all of time sees the older run too.
    allt = collect(db, now=now, cost_fn=no_cost)
    assert allt["runs"] == 7 and allt["tokens"] == 2617, allt
    assert allt["since"] is None, allt
    assert allt["busiest_agent_runs"] == 5, allt

    # A window with nothing in it is the zero-run case, not a crash.
    quiet = collect(db, since="2026-07-20", now=now, cost_fn=no_cost)
    assert quiet["runs"] == 0 and quiet["failure_rate"] == 0.0, quiet
    assert "nothing ran" in quiet["headline"], quiet

    # One run: median and p95 are both defined and both that run.
    write(single, [("solo", "2026-07-19T09:00:00+00:00", 7, 3, 1234, None, "m1")])
    one = collect(single, since="24h", now=now, cost_fn=no_cost)
    assert one["runs"] == 1 and one["failures"] == 0, one
    assert one["failure_rate"] == 0.0, one
    assert one["latency_median_ms"] == 1234.0, one
    assert one["latency_p95_ms"] == 1234.0, one
    assert one["headline"] == "all 1 runs succeeded", one

    # Every run failed: the rate is exactly 1.0, not 0.9999999.
    write(allbad, [
        ("a", "2026-07-19T01:00:00+00:00", 1, 1, None, "boom", "m1"),
        ("a", "2026-07-19T02:00:00+00:00", 1, 1, None, "boom", "m1"),
        ("a", "2026-07-19T03:00:00+00:00", 1, 1, None, "boom", "m1"),
    ])
    bad = collect(allbad, since="24h", now=now, cost_fn=no_cost)
    assert bad["failure_rate"] == 1.0, bad
    assert (bad["successes"], bad["failures"]) == (0, 3), bad
    # No run recorded a latency, so there is no median to invent.
    assert bad["latency_median_ms"] is None, bad
    assert bad["latency_p95_ms"] is None, bad
    assert bad["worst_agent"] == "a" and bad["worst_agent_rate"] == 1.0, bad

    # Failures, but nobody has run enough times to be blamed for them.
    thin = collect(db, since="7d", now=now, cost_fn=no_cost)
    assert thin["worst_agent"] == "a", thin
    few = _empty(None)
    few.update({"runs": 2, "successes": 1, "failures": 1, "failure_rate": 0.5})
    assert "no agent has enough runs" in _headline(few), _headline(few)

    # A cost figure, when something can price the window, rides along.
    priced = collect(db, since="7d", now=now,
                     cost_fn=lambda _db, _cut: 1.234)
    assert priced["usd"] == 1.234, priced
    panel = format_report(priced)
    assert "$1.23" in panel, panel

    # The panel: fixed width, bordered, and it always carries the headline.
    panel = format_report(g)
    widths = set(len(line) for line in panel.splitlines())
    assert widths == {PANEL_WIDTH}, widths
    assert panel.splitlines()[0] == "+" + "-" * (PANEL_WIDTH - 2) + "+", panel
    assert "runs" in panel and "33.3%" in panel, panel
    assert "most errors" in panel, panel
    assert "hosted cost" not in panel, "no cost line when there is no cost"

    # The real cost path is optional, never fatal, and never a surprise type.
    live = _ledger_cost(db, None)
    assert live is None or isinstance(live, float), live

    # json round-trip: the CLI's --json output has to survive serialization.
    assert json.loads(json.dumps(g))["runs"] == 6

    print("hearth-digest self-test OK")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="hearth-digest",
        description="one screen: how the box did while you were asleep")
    p.add_argument("--db", default=None, help="audit database (default: hearth's own)")
    p.add_argument("--since", default=None,
                   help="24h, 7d or 2026-07-01 (default: all time)")
    p.add_argument("--fail-threshold", type=float, default=1.0,
                   help="exit 1 when the failure rate is above this (0.0-1.0; "
                        "the default of 1.0 never trips, since a rate cannot "
                        "exceed it)")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    try:
        cutoff = parse_since(a.since)
    except ValueError as exc:
        p.error(str(exc))
    db = a.db or hearth_paths.db_path()
    digest = collect(db, since=cutoff)
    print(json.dumps(digest, indent=2) if a.as_json else format_report(digest))
    return 1 if digest["failure_rate"] > a.fail_threshold else 0


if __name__ == "__main__":
    sys.exit(main())
