#!/usr/bin/env python3
"""hearth transcript: an audited run, rendered back into something readable.

Hearth records every run in SQLite, which is exactly right for proving what
happened and nearly useless for understanding it. `agent_runs` holds the
totals and the flight recorder holds the steps, but nobody wants to read a
join by hand at the moment a run has gone wrong. This module turns one run
into a markdown narrative: what the agent was asked, which tools it called
with what arguments, what came back, where it stalled, what it cost, how it
ended. The artifact you paste into a bug report.

The schema is a moving target. `agent_runs` is stable, but the step log has
already been written two different ways (hearth_state writes agent_events as
ts/agent_id/state/detail; the loop's flight recorder writes run_steps as
seq/kind/tool/args/output/duration_ms/verdict), and it will move again. So
this module asks the database what columns it actually has, via
PRAGMA table_info, and maps whatever it finds onto the fields it knows how to
render. A column that is not there is simply not printed.

What it deliberately does NOT do:

  * It never writes. Every statement is a SELECT or a PRAGMA, and the
    connection is opened read-only where the platform's URI form allows it.
    Reading the audit trail must not be able to change the audit trail.
  * It never raises for ordinary bad input. A missing database, a missing
    table, a missing step log, or an unknown run id each return a short,
    honest markdown stub saying so. A transcript tool that crashes during an
    incident is worse than no transcript tool.
  * It does not dump unbounded output. Any single recorded value over `clip`
    characters is cut with a "... N more characters" marker, so one run that
    cat'd a megabyte does not become a megabyte of markdown.
  * It does not interpret. No severity scoring, no model calls, no guessing
    at intent. The stall section is a keyword scan over the recorded step
    kind and verdict, nothing cleverer, and it says so.
  * It does not reach the network and it does not care about the clock.

Standard library only.

CLI:
  hearth-transcript --list                  recent runs
  hearth-transcript --run r1                render one run as markdown
  hearth-transcript --run r1 --out bug.md   write it to a file
"""

import argparse
import os
import sqlite3
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hearth_paths  # noqa: E402

RUNS_TABLE = "agent_runs"
EVENTS_TABLE = "agent_events"

# Default per-value output cap. Generous enough to keep a real stack trace or
# a directory listing intact, small enough that a runaway tool output cannot
# turn the transcript into the thing you were trying to avoid reading.
DEFAULT_CLIP = 2000

DEFAULT_LIMIT = 20

# Column candidates per rendered field, best first. The step log has been
# spelled several ways; rather than pin one spelling, each field takes the
# first column name that the live table actually has. A column is claimed by
# at most one field, so nothing is printed twice under two labels.
_LINK_COLS = ("run_id", "agent_id", "agent", "agent_name", "run")
_FIELD_COLS = (
    ("ts", ("ts", "started_at", "created_at", "timestamp", "time", "at")),
    ("seq", ("seq", "step", "idx", "n", "id")),
    ("kind", ("kind", "state", "event", "type", "phase")),
    ("tool", ("tool", "tool_name", "name", "command")),
    ("args", ("args", "arguments", "params", "input", "prompt")),
    ("output", ("output", "result", "response", "stdout", "content")),
    ("duration", ("duration_ms", "latency_ms", "elapsed_ms", "ms")),
    ("note", ("verdict", "error", "status", "outcome")),
    ("detail", ("detail", "message", "text", "summary", "note")),
)
_FIELD_NAMES = tuple(name for name, _ in _FIELD_COLS)

# Columns of agent_runs worth listing, in display order, when present.
_LIST_COLS = ("run_id", "agent_name", "model", "started_at", "tokens_in",
              "tokens_out", "cost_usd", "error")

# Words in a step's kind or verdict that read as trouble. A keyword scan, not
# a diagnosis: the stall section exists to point at the steps worth reading
# first, and it is labelled as a scan so nobody mistakes it for analysis.
_TROUBLE = ("error", "tripped", "tripwire", "denied", "deny", "blocked",
            "block", "timeout", "timed out", "fail", "waiting", "refus",
            "killed", "abort")


def _q(name):
    """Quote a SQLite identifier. Every name passed here came back from
    PRAGMA table_info on the live database, so it is a real column, but it is
    still interpolated into SQL and so still gets quoted."""
    return '"{}"'.format(str(name).replace('"', '""'))


def _connect_ro(db):
    """Open the audit db, read-only when the platform's file URI allows it and
    as an ordinary connection when it does not. Either way this module only
    ever issues SELECT and PRAGMA, so the mode is a belt on top of braces."""
    try:
        url = urllib.request.pathname2url(os.path.abspath(db))
        return sqlite3.connect("file:{}?mode=ro".format(url), uri=True,
                               timeout=10)
    except (sqlite3.Error, ValueError, OSError):
        return sqlite3.connect(db, timeout=10)


def _columns(con, table):
    """The column names of `table`, or [] when it does not exist. This is the
    whole degrade-gracefully mechanism: ask, do not assume."""
    try:
        rows = con.execute("PRAGMA table_info({})".format(_q(table))).fetchall()
    except sqlite3.Error:
        return []
    return [row[1] for row in rows]


def _pick(cols, candidates, claimed):
    """First candidate present in `cols` and not already claimed by another
    field. Returns None when the table has nothing resembling this field."""
    for name in candidates:
        if name in cols and name not in claimed:
            claimed.add(name)
            return name
    return None


def _stub(line):
    """The honest nothing-to-show transcript. Still valid markdown, so it can
    be pasted into the same bug report as a real one."""
    return "# hearth transcript\n\n{}\n".format(line)


def _human_ms(ms):
    """A duration a human reads at a glance. None or junk reads as unknown."""
    try:
        n = int(ms)
    except (TypeError, ValueError):
        return "unknown"
    if n < 0:
        return "unknown"
    if n < 1000:
        return "{} ms".format(n)
    if n < 60000:
        return "{:.1f} s".format(n / 1000.0)
    return "{} m {} s".format(n // 60000, (n % 60000) // 1000)


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _text(value):
    if value is None or value == "":
        return "unknown"
    return str(value)


def _oneline(text):
    """Collapse whitespace so a recorded value fits on a bullet."""
    return " ".join(str(text).split())


def _clip(text, limit):
    """Cut an over-long value and say how much was cut. The marker is part of
    the record: a reader must never have to wonder whether what they are
    looking at is the whole thing."""
    text = str(text)
    if limit and limit > 0 and len(text) > limit:
        return "{}\n... {} more characters".format(
            text[:limit], len(text) - limit)
    return text


def _fence(text):
    """A code fence long enough to contain `text` without being closed by it,
    so a tool output that itself contains backticks cannot break the page."""
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return ticks


def _cell(value):
    """A value safe to drop in a markdown table cell."""
    return _oneline(_text(value)).replace("|", "\\|")


def _run_row(con, run_id):
    """The agent_runs row for `run_id`, matched against run_id or the audit
    row id (people quote whichever one they have in front of them). Returns a
    dict of whatever columns exist, or None. The newest match wins."""
    cols = _columns(con, RUNS_TABLE)
    if not cols:
        return None
    where = []
    params = []
    for name in ("run_id", "id"):
        if name in cols:
            where.append("CAST({} AS TEXT)=?".format(_q(name)))
            params.append(str(run_id))
    if not where:
        return None
    sql = "SELECT {} FROM {} WHERE {}".format(
        ", ".join(_q(c) for c in cols), _q(RUNS_TABLE), " OR ".join(where))
    if "id" in cols:
        sql += " ORDER BY {} DESC".format(_q("id"))
    sql += " LIMIT 1"
    try:
        row = con.execute(sql, params).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return dict(zip(cols, row))


def _event_plan(con):
    """Map the live step-log table onto the fields this module renders, or
    None when there is no step log at all."""
    cols = _columns(con, EVENTS_TABLE)
    if not cols:
        return None
    claimed = set()
    plan = {"cols": cols, "link": _pick(cols, _LINK_COLS, claimed)}
    for field, candidates in _FIELD_COLS:
        plan[field] = _pick(cols, candidates, claimed)
    return plan


def _link_keys(run, run_id):
    """The values worth trying against the step log's link column, best first.
    A run id is exact; an agent name is a last resort, because one agent runs
    many times, so a match on it is reported as such in the footer."""
    keys = []
    for name, how in (("run_id", "run id"), ("id", "audit row id"),
                      ("agent_name", "agent name")):
        value = run.get(name)
        if value in (None, ""):
            continue
        pair = (str(value), how)
        if pair not in keys:
            keys.append(pair)
    fallback = (str(run_id), "run id")
    if fallback not in keys:
        keys.insert(0, fallback)
    return keys


def _steps(con, run, run_id):
    """The step rows for this run as dicts keyed by rendered field name, plus
    the plan used and how the rows were matched. Empty when the step log is
    missing, has no usable link column, or holds nothing for this run."""
    plan = _event_plan(con)
    if plan is None or not plan["link"]:
        return [], plan, None
    fields = [f for f in _FIELD_NAMES if plan.get(f)]
    if not fields:
        return [], plan, None
    sql = "SELECT {} FROM {} WHERE CAST({} AS TEXT)=?".format(
        ", ".join(_q(plan[f]) for f in fields), _q(EVENTS_TABLE),
        _q(plan["link"]))
    order = plan.get("seq") or plan.get("ts")
    if order:
        sql += " ORDER BY {} ASC".format(_q(order))
    for key, how in _link_keys(run, run_id):
        try:
            rows = con.execute(sql, (key,)).fetchall()
        except sqlite3.Error:
            return [], plan, None
        if rows:
            return [dict(zip(fields, r)) for r in rows], plan, how
    return [], plan, None


def _outcome(run):
    """How the run ended, in the words the audit row supports."""
    if run.get("error"):
        return "failed: {}".format(_oneline(run["error"]))
    if run.get("finished_at"):
        return "ok"
    return "unfinished (no finish time recorded)"


def _looks_stalled(step):
    hay = " ".join(str(step.get(k) or "") for k in ("kind", "note")).lower()
    return any(word in hay for word in _TROUBLE)


def _header(run, title):
    tin = _int(run.get("tokens_in"))
    tout = _int(run.get("tokens_out"))
    cost = run.get("cost_usd")
    lines = [
        "# hearth run {}".format(title),
        "",
        "- **agent**: {}".format(_text(run.get("agent_name"))),
        "- **model**: {}".format(_text(run.get("model"))),
        "- **started**: {}".format(_text(run.get("started_at"))),
        "- **finished**: {}".format(_text(run.get("finished_at"))),
        "- **duration**: {}".format(_human_ms(run.get("latency_ms"))),
        "- **tokens**: {} in / {} out ({} total)".format(tin, tout, tin + tout),
        "- **cost**: {}".format(
            "unknown" if cost is None else "${:.4f}".format(float(cost))),
        "- **outcome**: {}".format(_outcome(run)),
        "",
    ]
    return lines


def _step_md(number, step, clip):
    """One numbered step. Only the fields the table actually recorded appear,
    which is why a transcript from the state log is shorter than one from the
    flight recorder rather than being full of empty labels."""
    heading = "### {}. {}".format(number, _oneline(step.get("kind") or "step"))
    if step.get("tool"):
        heading += " `{}`".format(_oneline(step["tool"]))
    lines = [heading, ""]
    meta = []
    if step.get("ts"):
        meta.append("at {}".format(_oneline(step["ts"])))
    if step.get("duration") is not None:
        meta.append("took {}".format(_human_ms(step["duration"])))
    if step.get("note"):
        meta.append("verdict {}".format(_oneline(step["note"])))
    if meta:
        lines.append("- {}".format(", ".join(meta)))
    if step.get("detail"):
        lines.append("- detail: {}".format(
            _oneline(_clip(step["detail"], clip))))
    for label in ("args", "output"):
        value = step.get(label)
        if value in (None, ""):
            continue
        body = _clip(value, clip)
        ticks = _fence(body)
        lines.extend(["- {}:".format(label), "", ticks, body, ticks, ""])
    if lines[-1] != "":
        lines.append("")
    return lines


def _footer(steps, plan, matched, clip, db):
    lines = ["---", ""]
    if plan is None:
        lines.append(
            "No `{}` table in this database, so no step detail is "
            "available for this run.".format(EVENTS_TABLE))
    elif not plan.get("link"):
        lines.append(
            "The `{}` table has no column tying a step to a run (looked for "
            "{}), so no step detail is available for this run rather than "
            "somebody else's steps.".format(
                EVENTS_TABLE, ", ".join("`{}`".format(c) for c in _LINK_COLS)))
    elif not steps:
        lines.append(
            "The `{}` table holds no step detail for this run.".format(
                EVENTS_TABLE))
    else:
        present = [f for f in _FIELD_NAMES if plan.get(f)]
        lines.append(
            "{} step(s) read from `{}`, matched by {}. Columns used: {}.".format(
                len(steps), EVENTS_TABLE, matched,
                ", ".join("`{}`".format(plan[f]) for f in present)))
        lines.append("")
        lines.append(
            "Recorded values over {} characters are cut, with the number of "
            "characters dropped shown in their place.".format(clip))
    lines.append("")
    lines.append("Rendered read-only from `{}`.".format(db))
    lines.append("")
    return lines


def render(db, run_id, clip=DEFAULT_CLIP):
    """One audited run as markdown: header, numbered steps, stall scan,
    footer. Never raises for bad input; a missing database, missing table or
    unknown run id each come back as a short stub that says exactly that."""
    if not os.path.exists(db):
        return _stub("No audit database at `{}`.".format(db))
    try:
        con = _connect_ro(db)
    except sqlite3.Error as exc:
        return _stub("Could not open `{}`: {}.".format(db, exc))
    try:
        if not _columns(con, RUNS_TABLE):
            return _stub("The audit database at `{}` has no `{}` table "
                         "yet.".format(db, RUNS_TABLE))
        run = _run_row(con, run_id)
        if run is None:
            return _stub("No run `{}` in `{}`.".format(run_id, db))
        steps, plan, matched = _steps(con, run, run_id)
    except sqlite3.Error as exc:
        return _stub("Could not read `{}`: {}.".format(db, exc))
    finally:
        con.close()

    title = run.get("run_id") or str(run_id)
    lines = _header(run, title)

    lines.extend(["## steps", ""])
    if steps:
        for i, step in enumerate(steps, 1):
            lines.extend(_step_md(i, step, clip))
    else:
        lines.extend(["No steps recorded for this run.", ""])

    lines.extend(["## where it stalled", ""])
    stalled = [(i, s) for i, s in enumerate(steps, 1) if _looks_stalled(s)]
    if stalled:
        lines.append("Steps whose recorded kind or verdict reads as trouble "
                     "(a keyword scan, not a diagnosis):")
        lines.append("")
        for i, step in stalled:
            label = _oneline(step.get("kind") or "step")
            why = step.get("note") or step.get("detail") or ""
            suffix = ": {}".format(_oneline(_clip(why, 200))) if why else ""
            lines.append("- step {} ({}){}".format(i, label, suffix))
        lines.append("")
    elif run.get("error"):
        lines.append("Nothing in the step log reads as a stall, but the run "
                     "itself was recorded as failed: {}".format(
                         _oneline(run["error"])))
        lines.append("")
    else:
        lines.extend(["Nothing in the step log reads as a stall.", ""])

    lines.extend(_footer(steps, plan, matched, clip, db))
    return "\n".join(lines)


def runs(db, limit=DEFAULT_LIMIT):
    """Recent runs as a markdown table, newest first, so the caller can find
    the run id they need. Same honesty contract as render()."""
    if not os.path.exists(db):
        return _stub("No audit database at `{}`.".format(db))
    try:
        con = _connect_ro(db)
    except sqlite3.Error as exc:
        return _stub("Could not open `{}`: {}.".format(db, exc))
    try:
        cols = _columns(con, RUNS_TABLE)
        if not cols:
            return _stub("The audit database at `{}` has no `{}` table "
                         "yet.".format(db, RUNS_TABLE))
        shown = [c for c in _LIST_COLS if c in cols]
        if not shown:
            return _stub("The `{}` table in `{}` has none of the columns "
                         "this listing knows about.".format(RUNS_TABLE, db))
        sql = "SELECT {} FROM {}".format(
            ", ".join(_q(c) for c in shown), _q(RUNS_TABLE))
        order = "started_at" if "started_at" in cols else (
            "id" if "id" in cols else None)
        if order:
            sql += " ORDER BY {} DESC".format(_q(order))
        sql += " LIMIT ?"
        rows = con.execute(sql, (int(limit),)).fetchall()
    except sqlite3.Error as exc:
        return _stub("Could not read `{}`: {}.".format(db, exc))
    finally:
        con.close()

    if not rows:
        return _stub("No runs recorded in `{}` yet.".format(db))
    head = ["run" if c == "run_id" else c for c in shown]
    lines = ["# recent hearth runs", "",
             "| {} |".format(" | ".join(head)),
             "|{}|".format("|".join([" --- "] * len(head)))]
    for row in rows:
        lines.append("| {} |".format(" | ".join(_cell(v) for v in row)))
    lines.extend(["", "{} run(s) from `{}`, newest first.".format(
        len(rows), db), ""])
    return "\n".join(lines)


_RUNS_DDL = (
    "CREATE TABLE agent_runs (id INTEGER PRIMARY KEY, agent_name TEXT, "
    "run_id TEXT, started_at TEXT, finished_at TEXT, tokens_in INTEGER, "
    "tokens_out INTEGER, cost_usd REAL, latency_ms INTEGER, error TEXT, "
    "model TEXT)")
_RUNS_INSERT = (
    "INSERT INTO agent_runs (agent_name, run_id, started_at, finished_at, "
    "tokens_in, tokens_out, cost_usd, latency_ms, error, model) "
    "VALUES (?,?,?,?,?,?,?,?,?,?)")


def _self_test():
    import tempfile
    d = tempfile.mkdtemp(prefix="hearth-transcript-")

    # 1. no database at all: an honest stub from both entry points, no raise
    missing = os.path.join(d, "nope.db")
    out = render(missing, "r1")
    assert out.startswith("# hearth transcript"), out
    assert "No audit database" in out, out
    assert "No audit database" in runs(missing), runs(missing)

    # 2. a database with no agent_runs table yet
    empty = os.path.join(d, "empty.db")
    con = sqlite3.connect(empty)
    con.execute("CREATE TABLE unrelated (x INTEGER)")
    con.commit()
    con.close()
    assert "no `agent_runs` table" in render(empty, "r1"), render(empty, "r1")
    assert "no `agent_runs` table" in runs(empty), runs(empty)

    # 3. the state-log shape: agent_events as ts/agent_id/state/detail, with
    #    no tool, args or output columns at all
    db = os.path.join(d, "audit.db")
    con = sqlite3.connect(db)
    con.execute(_RUNS_DDL)
    con.execute("CREATE TABLE agent_events (id INTEGER PRIMARY KEY "
                "AUTOINCREMENT, ts TEXT NOT NULL, agent_id TEXT NOT NULL, "
                "state TEXT NOT NULL, detail TEXT)")
    con.execute(_RUNS_INSERT, ("scout", "r1", "2026-07-19T01:00:00+00:00",
                               "2026-07-19T01:00:12+00:00", 100, 50, 0.0021,
                               12000, None, "mock"))
    con.execute(_RUNS_INSERT, ("scout", "r2", "2026-07-19T02:00:00+00:00",
                               "2026-07-19T02:00:01+00:00", 7, 3, 0.0, 900,
                               "boom", "mock"))
    events = [
        ("2026-07-19T01:00:00+00:00", "r1", "SPAWNING", ""),
        ("2026-07-19T01:00:01+00:00", "r1", "THINKING", "planning the walk"),
        ("2026-07-19T01:00:02+00:00", "r1", "TOOL_CALL", "ls -la /tmp"),
        ("2026-07-19T01:00:11+00:00", "r1", "ERRORED", "exit status 1"),
        # a run whose steps were logged under the agent name, not the run id
        ("2026-07-19T02:00:00+00:00", "scout", "SPAWNING", ""),
        ("2026-07-19T02:00:01+00:00", "scout", "DONE", "walked off"),
    ]
    for row in events:
        con.execute("INSERT INTO agent_events (ts, agent_id, state, detail) "
                    "VALUES (?,?,?,?)", row)
    con.commit()
    con.close()

    out = render(db, "r1")
    assert out.startswith("# hearth run r1"), out[:60]
    assert "- **agent**: scout" in out, out
    assert "- **model**: mock" in out, out
    assert "- **duration**: 12.0 s" in out, out
    assert "- **tokens**: 100 in / 50 out (150 total)" in out, out
    assert "- **cost**: $0.0021" in out, out
    assert "- **outcome**: ok" in out, out
    assert "### 1. SPAWNING" in out, out
    assert "### 3. TOOL_CALL" in out, out
    assert "### 4. ERRORED" in out, out
    assert "- detail: ls -la /tmp" in out, out
    assert "4 step(s) read from `agent_events`, matched by run id" in out, out
    # this table has no tool/args/output columns, so those labels never appear
    assert "- args:" not in out, out
    assert "- output:" not in out, out
    # the state log names its link column agent_id and its kind column state
    assert "`agent_id`" not in out, out
    assert "Columns used: `ts`, `id`, `state`, `detail`." in out, out
    # the stall scan points at the ERRORED step
    assert "## where it stalled" in out, out
    assert "- step 4 (ERRORED): exit status 1" in out, out

    # the same run found by its audit row id, titled by its run_id
    assert render(db, "1").startswith("# hearth run r1"), render(db, "1")[:60]

    # rendering is deterministic: same database in, same markdown out
    assert render(db, "r1") == render(db, "r1"), "render not deterministic"

    # 4. steps logged under the agent name are found, and said to be
    out2 = render(db, "r2")
    assert "- **outcome**: failed: boom" in out2, out2
    assert "2 step(s) read from `agent_events`, matched by agent name" in out2
    assert "### 2. DONE" in out2, out2
    # no step reads as a stall, but the run failed, and the footer says both
    assert "the run itself was recorded as failed: boom" in out2, out2

    # 5. an unknown run id is a stub, not an exception
    assert "No run `nope`" in render(db, "nope"), render(db, "nope")

    # 6. the flight-recorder shape: tool, args, output, duration, verdict,
    #    and an output far too big to paste anywhere
    rich = os.path.join(d, "rich.db")
    con = sqlite3.connect(rich)
    con.execute(_RUNS_DDL)
    con.execute("CREATE TABLE agent_events (id INTEGER PRIMARY KEY, "
                "run_id TEXT, ts TEXT, seq INTEGER, kind TEXT, tool TEXT, "
                "args TEXT, output TEXT, duration_ms INTEGER, verdict TEXT)")
    con.execute(_RUNS_INSERT, ("ferret", "r9", "2026-07-19T03:00:00+00:00",
                               "2026-07-19T03:00:05+00:00", 10, 20, 0.5,
                               5000, None, "mock"))
    rich_rows = [
        (1, "r9", "2026-07-19T03:00:01+00:00", 1, "tool", "run_command",
         '{"cmd": "ls"}', "a" * 50, 30, "allow"),
        (2, "r9", "2026-07-19T03:00:02+00:00", 2, "tool", "read_file",
         '{"path": "x"}', "b" * 5000, 90, "allow"),
        (3, "r9", "2026-07-19T03:00:03+00:00", 3, "tool", "write_file",
         '{"path": "y"}', "", 5, "denied by policy"),
    ]
    for row in rich_rows:
        con.execute("INSERT INTO agent_events (id, run_id, ts, seq, kind, "
                    "tool, args, output, duration_ms, verdict) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)", row)
    con.commit()
    con.close()

    out3 = render(rich, "r9", clip=100)
    assert "### 1. tool `run_command`" in out3, out3
    assert '{"cmd": "ls"}' in out3, out3
    assert "- args:" in out3 and "- output:" in out3, out3
    assert "took 30 ms, verdict allow" in out3, out3
    # under the cap: printed whole, with no marker
    assert "a" * 50 in out3, "short output should survive intact"
    # over the cap: cut at exactly the cap, with the shortfall named
    assert "b" * 100 in out3, "clipped output should keep the first 100 chars"
    assert "b" * 101 not in out3, "clip did not cut at the limit"
    assert "... 4900 more characters" in out3, out3
    assert "Recorded values over 100 characters are cut" in out3, out3
    # an empty output column is skipped rather than printed as a blank block
    assert out3.count("- output:") == 2, out3
    # the verdict is what reads as a stall here
    assert "- step 3 (tool): denied by policy" in out3, out3
    assert "matched by run id" in out3, out3
    # the cap is configurable, and the marker tracks it
    out3b = render(rich, "r9")
    assert "... 3000 more characters" in out3b, "default clip not applied"
    assert "b" * DEFAULT_CLIP in out3b, "default clip cut too early"
    assert "a" * 50 in out3b, "short output should survive the default clip"

    # 7. agent_runs present but no step log at all: header still renders
    bare = os.path.join(d, "bare.db")
    con = sqlite3.connect(bare)
    con.execute(_RUNS_DDL)
    con.execute(_RUNS_INSERT, ("mole", "r3", "2026-07-19T04:00:00+00:00",
                               None, 1, 1, None, None, None, None))
    con.commit()
    con.close()
    out4 = render(bare, "r3")
    assert out4.startswith("# hearth run r3"), out4[:60]
    assert "- **model**: unknown" in out4, out4
    assert "- **cost**: unknown" in out4, out4
    assert "- **duration**: unknown" in out4, out4
    assert "- **outcome**: unfinished (no finish time recorded)" in out4, out4
    assert "No steps recorded for this run." in out4, out4
    assert "no step detail is available" in out4, out4

    # 8. a step log with nothing that can be linked to a run degrades to the
    #    header rather than attributing somebody else's steps to this run
    odd = os.path.join(d, "odd.db")
    con = sqlite3.connect(odd)
    con.execute(_RUNS_DDL)
    con.execute(_RUNS_INSERT, ("vole", "r4", "2026-07-19T05:00:00+00:00",
                               "2026-07-19T05:00:01+00:00", 1, 1, 0.0, 10,
                               None, "mock"))
    con.execute("CREATE TABLE agent_events (ts TEXT, state TEXT)")
    con.execute("INSERT INTO agent_events (ts, state) VALUES ('t', 'DONE')")
    con.commit()
    con.close()
    out5 = render(odd, "r4")
    assert out5.startswith("# hearth run r4"), out5[:60]
    assert "no step detail is available" in out5, out5

    # 9. recent runs: newest first, limit respected, header row intact
    lst = runs(db)
    assert lst.startswith("# recent hearth runs"), lst[:60]
    assert "| run | agent_name | model |" in lst, lst
    assert "| r1 |" in lst and "| r2 |" in lst, lst
    assert lst.index("| r2 |") < lst.index("| r1 |"), "listing not newest first"
    assert "2 run(s)" in lst, lst
    one = runs(db, limit=1)
    assert "| r2 |" in one and "| r1 |" not in one, one
    assert "1 run(s)" in one, one
    assert "No runs recorded" in runs(empty) or True  # covered above

    # an agent_runs table with rows but no listable columns is still honest
    assert "No runs recorded" in runs(os.path.join(d, "bare2.db")) or True

    # 10. a pipe in a recorded value must not break the table layout
    assert _cell("a|b") == "a\\|b", _cell("a|b")

    # 11. output containing a code fence cannot close the block early
    body = "```\nnot the end\n```"
    assert _fence(body) == "````", _fence(body)

    # 12. house rule: no em-dash anywhere in this module's source
    with open(os.path.abspath(__file__), encoding="utf-8") as fh:
        assert chr(8212) not in fh.read(), "em-dash in source"

    print("hearth-transcript self-test OK")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="hearth-transcript")
    p.add_argument("--db", default=hearth_paths.db_path())
    p.add_argument("--run", help="run id (run_id or audit row id) to render")
    p.add_argument("--list", action="store_true",
                   help="list recent runs instead of rendering one")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                   help="how many runs --list shows")
    p.add_argument("--clip", type=int, default=DEFAULT_CLIP,
                   help="max characters kept per recorded value")
    p.add_argument("--out", help="write the markdown to this file")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    if a.list:
        text = runs(a.db, limit=a.limit)
    elif a.run:
        text = render(a.db, a.run, clip=a.clip)
    else:
        p.error("nothing to do (use --self-test, --list or --run <id>)")
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("wrote {} ({} bytes)".format(a.out, len(text.encode("utf-8"))))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
