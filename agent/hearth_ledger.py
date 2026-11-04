#!/usr/bin/env python3
r"""hearth ledger: what this month would have cost someone else.

    python agent/hearth_ledger.py
    python agent/hearth_ledger.py --since 30d --per-model
    python agent/hearth_ledger.py --json

Hearth's claim is that the work runs on your machine at no per-token cost.
That claim is cheap to make and nobody ever shows the arithmetic. This reads
the audit database Hearth already keeps, counts the tokens that actually went
through it, and prices them at a hosted API's rates. The output is the bill
that did not arrive.

THE NUMBER IS AN ESTIMATE, AND SAYS SO
--------------------------------------
Hosted prices change, differ per model and per provider, and a serious
comparison would have to argue about which hosted model is the fair
counterpart to the local one you ran. This module does not pretend to settle
that. It ships one documented rate assumption, labelled with the tier it
represents, and lets you replace it:

    HEARTH_RATE_IN   dollars per million input tokens
    HEARTH_RATE_OUT  dollars per million output tokens
    HEARTH_RATE_NAME a label for the above, used in the report

The defaults are a round mid-tier assumption, not a quote from any vendor,
and the report always names the rate it used. A number whose assumption is
printed beside it can be argued with; one without cannot.

WHAT IT COUNTS
--------------
The agent_runs table: tokens_in and tokens_out per run, with the run's model
and start time. Rows the budget breaker already writes, so this adds no
bookkeeping of its own and cannot disagree with the audit log. A missing
database, or a database with no runs yet, reports zeros rather than failing:
a fresh install has nothing to show and that is not an error.

Standard library only. Read-only: this module never writes to the database.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hearth_paths  # noqa: E402

# A round mid-tier assumption, as of 2026-11. Not a quote from any vendor.
# Override with HEARTH_RATE_IN / HEARTH_RATE_OUT / HEARTH_RATE_NAME.
DEFAULT_RATE_IN = 3.00
DEFAULT_RATE_OUT = 15.00
DEFAULT_RATE_NAME = "mid-tier hosted API (assumption, not a quote)"


def rates(env=None):
    """The rate assumption in force: (usd per million in, out, label). A
    malformed or negative override falls back to the default rather than
    producing a nonsense bill."""
    env = os.environ if env is None else env

    def num(key, default):
        try:
            v = float(env.get(key, ""))
        except (TypeError, ValueError):
            return default
        return v if v >= 0 else default

    rin, rout = num("HEARTH_RATE_IN", DEFAULT_RATE_IN), num("HEARTH_RATE_OUT", DEFAULT_RATE_OUT)
    name = (env.get("HEARTH_RATE_NAME") or "").strip()
    if not name:
        name = (DEFAULT_RATE_NAME if (rin, rout) == (DEFAULT_RATE_IN, DEFAULT_RATE_OUT)
                else "custom rate")
    return rin, rout, name


def parse_since(text, now=None):
    """A cutoff from "30d", "12h", "2026-07-01", or None for all of it.
    Returns a datetime or None. Raises ValueError on anything else, because
    silently reporting all time when the user asked for a window would
    overstate the saving."""
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
        raise ValueError("since must be like 30d, 12h or 2026-07-01, not {!r}".format(s))


def _cost(tokens_in, tokens_out, rate_in, rate_out):
    return (tokens_in / 1e6) * rate_in + (tokens_out / 1e6) * rate_out


def summarize(db=None, since=None, now=None, env=None):
    """Read the audit database and price what went through it.

    Returns a dict with totals, the rate used, and a per-model breakdown
    ordered by cost. Never raises for a missing or empty database."""
    db = db or hearth_paths.db_path()
    rate_in, rate_out, rate_name = rates(env)
    cutoff = since if isinstance(since, datetime) or since is None else parse_since(since, now)
    out = {
        "runs": 0, "tokens_in": 0, "tokens_out": 0, "tokens": 0,
        "usd": 0.0, "rate_in": rate_in, "rate_out": rate_out, "rate_name": rate_name,
        "since": cutoff.isoformat() if cutoff else None, "models": [],
    }
    try:
        con = sqlite3.connect("file:{}?mode=ro".format(db), uri=True)
    except sqlite3.Error:
        return out
    try:
        sql = ("SELECT COALESCE(model, 'unknown'), COUNT(*), "
               "COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0) FROM agent_runs")
        args = ()
        if cutoff is not None:
            sql += " WHERE started_at >= ?"
            args = (cutoff.isoformat(),)
        rows = con.execute(sql + " GROUP BY 1", args).fetchall()
    except sqlite3.Error:
        # No database, no agent_runs table yet, or a schema this does not
        # know. A fresh install has nothing to report, which is not an error.
        return out
    finally:
        con.close()

    for model, runs, tin, tout in rows:
        out["runs"] += runs
        out["tokens_in"] += tin
        out["tokens_out"] += tout
        out["models"].append({
            "model": model, "runs": runs, "tokens_in": tin, "tokens_out": tout,
            "usd": _cost(tin, tout, rate_in, rate_out),
        })
    out["tokens"] = out["tokens_in"] + out["tokens_out"]
    out["usd"] = _cost(out["tokens_in"], out["tokens_out"], rate_in, rate_out)
    out["models"].sort(key=lambda m: (-m["usd"], m["model"]))
    return out


def _money(usd):
    """Dollars, but never rounded to a flat zero when something was spent:
    a bill of 0.004 reported as $0.00 reads as "nothing happened"."""
    if usd and usd < 0.01:
        return "<$0.01"
    return "${:,.2f}".format(usd)


def format_report(s, per_model=False):
    """The report, as the CLI prints it."""
    window = "since {}".format(s["since"][:10]) if s["since"] else "all time"
    lines = [
        "hearth ledger: {}".format(window),
        "",
        "  runs            {:,}".format(s["runs"]),
        "  tokens in       {:,}".format(s["tokens_in"]),
        "  tokens out      {:,}".format(s["tokens_out"]),
        "  tokens total    {:,}".format(s["tokens"]),
        "",
        "  would have cost {}".format(_money(s["usd"])),
        "  actually cost   $0.00 (local)",
        "",
        "  priced at ${:,.2f}/M in, ${:,.2f}/M out: {}".format(
            s["rate_in"], s["rate_out"], s["rate_name"]),
    ]
    if s["runs"] == 0:
        lines.insert(2, "  nothing recorded yet")
    if per_model and s["models"]:
        lines += ["", "  by model:"]
        width = max(len(m["model"]) for m in s["models"])
        for m in s["models"]:
            lines.append("    {:<{w}}  {:>9}  {:,} runs, {:,} tokens".format(
                m["model"], _money(m["usd"]), m["runs"],
                m["tokens_in"] + m["tokens_out"], w=width))
    return "\n".join(lines)


def _self_test():
    import tempfile

    now = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)

    # rates: defaults, overrides, and refusal of nonsense
    assert rates({}) == (DEFAULT_RATE_IN, DEFAULT_RATE_OUT, DEFAULT_RATE_NAME)
    rin, rout, name = rates({"HEARTH_RATE_IN": "1.5", "HEARTH_RATE_OUT": "9"})
    assert (rin, rout) == (1.5, 9.0) and name == "custom rate", (rin, rout, name)
    assert rates({"HEARTH_RATE_IN": "banana"})[0] == DEFAULT_RATE_IN
    assert rates({"HEARTH_RATE_IN": "-5"})[0] == DEFAULT_RATE_IN, "negative rate refused"
    assert rates({"HEARTH_RATE_NAME": "  my deal  "})[2] == "my deal"

    # parse_since: relative, absolute, nothing, and junk
    assert parse_since(None) is None and parse_since("  ") is None
    assert parse_since("30d", now=now) == now - timedelta(days=30)
    assert parse_since("12h", now=now) == now - timedelta(hours=12)
    assert parse_since("2026-07-01") == datetime(2026, 7, 1, tzinfo=timezone.utc)
    for bad in ("30", "d30", "last week", "2026-13-01"):
        try:
            parse_since(bad)
            raise AssertionError("accepted {!r}".format(bad))
        except ValueError:
            pass

    # _cost: a million in at $3 is $3
    assert _cost(1_000_000, 0, 3.0, 15.0) == 3.0
    assert _cost(0, 2_000_000, 3.0, 15.0) == 30.0

    # _money: a real but tiny bill never prints as a flat zero
    assert _money(0) == "$0.00" and _money(0.004) == "<$0.01"
    assert _money(1234.5) == "$1,234.50"

    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "audit.db")

        # a database that does not exist reports zeros, and does not create one
        s = summarize(db, env={})
        assert s["runs"] == 0 and s["usd"] == 0.0, s
        assert not os.path.exists(db), "read-only: must not create the database"
        assert "nothing recorded yet" in format_report(s)

        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE agent_runs (id INTEGER PRIMARY KEY, agent_name TEXT, "
            "run_id TEXT, started_at TEXT, finished_at TEXT, tokens_in INTEGER, "
            "tokens_out INTEGER, cost_usd REAL, latency_ms INTEGER, error TEXT, "
            "model TEXT)")
        rows = [
            ("2026-07-19T01:00:00+00:00", 1_000_000, 100_000, "qwen"),
            ("2026-07-19T11:00:00+00:00", 500_000, 50_000, "qwen"),
            ("2026-06-01T10:00:00+00:00", 9_000_000, 900_000, "llama"),
            ("2026-07-19T02:00:00+00:00", 10, 10, None),  # model may be null
        ]
        for ts, tin, tout, model in rows:
            con.execute(
                "INSERT INTO agent_runs (agent_name, run_id, started_at, finished_at, "
                "tokens_in, tokens_out, cost_usd, latency_ms, error, model) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("a", ts, ts, ts, tin, tout, 0.0, 1, None, model))
        con.commit()
        con.close()

        # an empty table is still zeros, not a crash
        assert summarize(db, since="2030-01-01", env={})["runs"] == 0

        # all time: every row, priced at the default rate
        s = summarize(db, env={})
        assert s["runs"] == 4, s["runs"]
        assert s["tokens_in"] == 10_500_010 and s["tokens_out"] == 1_050_010, s
        assert s["tokens"] == s["tokens_in"] + s["tokens_out"]
        want = _cost(10_500_010, 1_050_010, DEFAULT_RATE_IN, DEFAULT_RATE_OUT)
        assert abs(s["usd"] - want) < 1e-9, (s["usd"], want)

        # a window excludes what is outside it
        s30 = summarize(db, since=parse_since("30d", now=now), env={})
        assert s30["runs"] == 3, s30["runs"]
        assert s30["tokens_in"] == 1_500_010, s30
        assert s30["usd"] < s["usd"], "a window cannot cost more than all time"
        assert s30["since"].startswith("2026-06-19")

        # per model: ordered by cost, null model named rather than dropped
        names = [m["model"] for m in s["models"]]
        assert names == ["llama", "qwen", "unknown"], names
        assert s["models"][0]["usd"] > s["models"][1]["usd"]
        assert sum(m["runs"] for m in s["models"]) == s["runs"]
        assert abs(sum(m["usd"] for m in s["models"]) - s["usd"]) < 1e-9, "parts sum to whole"

        # the rate assumption travels with the number, in both renderings
        rep = format_report(s, per_model=True)
        assert "would have cost" in rep and "actually cost   $0.00 (local)" in rep
        assert DEFAULT_RATE_NAME in rep, rep
        assert "llama" in rep and "unknown" in rep
        custom = summarize(db, env={"HEARTH_RATE_IN": "0", "HEARTH_RATE_OUT": "0"})
        assert custom["usd"] == 0.0 and "custom rate" in format_report(custom)

        # a table this module does not understand reports zeros, not a stack trace
        other = os.path.join(d, "other.db")
        con = sqlite3.connect(other)
        con.execute("CREATE TABLE something_else (x INTEGER)")
        con.commit()
        con.close()
        assert summarize(other, env={})["runs"] == 0

    print("hearth-ledger self-test OK")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="hearth-ledger",
                                description="what the local work would have cost hosted")
    p.add_argument("--db", default=None, help="audit database (default: hearth's own)")
    p.add_argument("--since", default=None, help="30d, 12h or 2026-07-01 (default: all time)")
    p.add_argument("--per-model", action="store_true", help="break the bill down by model")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    try:
        cutoff = parse_since(a.since)
    except ValueError as exc:
        p.error(str(exc))
    s = summarize(a.db, since=cutoff)
    print(json.dumps(s, indent=2) if a.as_json else format_report(s, per_model=a.per_model))
    return 0


if __name__ == "__main__":
    sys.exit(main())
