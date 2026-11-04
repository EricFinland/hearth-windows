#!/usr/bin/env python3
"""hearth blame: which run wrote this path, and why.

Hearth audits every run and snapshots the workspace, so after an overnight
marathon the manifest diff can prove THAT a file changed. It cannot say which
of the fourteen runs changed it, or what that run was trying to do at the
moment it did. The audit trail already holds the answer: every tool call
carries a run id, an agent, a tool name, its arguments and a timestamp. This
module joins the two lists, so a changed path is attributed to the call that
is actually recorded as having written it instead of to whichever run the
operator happens to remember.

The join is deliberately literal. A change is blamed on the most recent
write-capable call that names the path, at or before the change time, and on
nothing else:

  - a read-only tool is never blamed for a write, however well its path and
    its timing fit, because reading a file does not change it;
  - a call recorded after the change time is never blamed, because a cause
    cannot follow its effect;
  - when two candidate calls land inside the same second the record names
    BOTH and says plainly that it cannot separate them, rather than letting
    sort order pick a culprit and then presenting that as fact;
  - when no call names the path the record stays unknown and carries an
    honest reason. It never falls back to the busiest run, the nearest run in
    time, or any other plausible-looking guess. A wrong run id in a blame
    report is worse than an admitted gap, because it sends the next hour of
    debugging off in the wrong direction.

What this deliberately does NOT do:

  - it does not run git, read a repository, or shell out to anything. git
    blame answers a different question (which commit last touched a line);
    this answers which hearth run and which tool call did, including for the
    many files under a workspace that were never in version control;
  - it does not open, stat or hash the changed files. It is a pure function
    over two lists of records, so it still works on an archived audit trail
    long after the workspace itself has moved on;
  - it does not diff content or attribute individual lines inside a file.
    The audit trail records calls, not hunks, so per-line blame would be
    invention dressed up as evidence;
  - it does not reach the network, read the clock, or depend on the local
    timezone. Every timestamp arrives as data, which is what lets the
    self-test run offline and come out the same way every time.

Matching is forgiving about spelling, since a manifest and an audit trail
rarely agree: a path given relative against the same path given absolute,
forward against backslash separators, a drive letter or none, and a tool that
names its target under any of several plausible argument keys. Standard
library only.
"""

import argparse
import datetime
import json
import posixpath
import sys

CERTAIN = "certain"
LIKELY = "likely"
UNKNOWN = "unknown"

# Argument keys a recorded tool call might use for the thing it acted on.
# The audit trail stores whatever the tool was handed, and the tools do not
# agree with each other, so all of these are checked.
PATH_KEYS = ("path", "file", "target", "dest")

# Tools that can change a file on disk. Only these are ever blamed outright.
WRITE_TOOLS = frozenset((
    "write", "write_file", "edit", "multiedit", "edit_file", "str_replace",
    "str_replace_editor", "create", "create_file", "apply_patch", "patch",
    "append", "notebookedit", "move", "rename", "copy", "delete", "remove",
    "truncate", "touch", "format", "download",
))

# Tools that only look. A read can match the path and the timing perfectly
# and still be innocent, so these are excluded before a winner is picked.
READ_TOOLS = frozenset((
    "read", "read_file", "cat", "head", "tail", "view", "open", "grep",
    "glob", "ls", "list", "list_dir", "find", "search", "stat", "diff",
    "websearch", "webfetch", "get_page_text",
))

_FALLBACK_TS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
)


def _parse_ts(value):
    """Parse an ISO8601 timestamp into a naive UTC datetime, or None.

    Mixed offsets are normalised to UTC so an aware audit timestamp and a
    naive manifest timestamp can still be compared. Anything unparseable
    returns None rather than raising: a malformed record should degrade one
    row of the report, not take the whole report down."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text[-1] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    parsed = None
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        for fmt in _FALLBACK_TS:
            try:
                parsed = datetime.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(datetime.timezone.utc)
        parsed = parsed.replace(tzinfo=None)
    return parsed


def _iso(when):
    """Render a naive UTC datetime back as a Z-suffixed ISO8601 string."""
    return when.isoformat() + "Z"


def _norm_parts(path):
    """Split a path into comparable components.

    Separators are unified, "." segments and a leading drive letter are
    dropped, and the rest is case-folded. Hearth runs on Windows, where the
    same file reaches the audit trail as an absolute backslash path and the
    manifest as a relative forward-slash one, so none of that may be allowed
    to count as a difference."""
    if not isinstance(path, str):
        return []
    text = path.replace("\\", "/").strip()
    if not text:
        return []
    text = posixpath.normpath(text)
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if parts and len(parts[0]) == 2 and parts[0][1] == ":":
        parts = parts[1:]
    return [p.casefold() for p in parts]


def _same_path(left, right):
    """True when two spellings plausibly name the same file.

    The shorter path has to be a tail of the longer one on component
    boundaries, which is exactly the relative-versus-absolute case and
    nothing looser: "agent/a.py" matches "C:/ws/agent/a.py" but not
    "agent/other_a.py"."""
    a = _norm_parts(left)
    b = _norm_parts(right)
    if not a or not b:
        return False
    k = min(len(a), len(b))
    return a[-k:] == b[-k:]


def _call_paths(call):
    """Every path a recorded call names, under any of the plausible keys."""
    args = call.get("args")
    if not isinstance(args, dict):
        return []
    found = []
    for key in PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, (list, tuple)):
            found.extend([v for v in value if isinstance(v, str)])
    return found


def _tool_class(tool):
    """Classify a tool as "write", "read" or "unclear".

    An unfamiliar tool name is "unclear", not "write": it can still be named
    as the best available match, but only at `likely`, because nothing in the
    trail proves it touched the bytes."""
    name = str(tool or "").strip().casefold()
    if name in WRITE_TOOLS:
        return "write"
    if name in READ_TOOLS:
        return "read"
    return "unclear"


def _candidate(when, call):
    """The public shape of one blamed or co-blamed call."""
    return {
        "run_id": call.get("run_id"),
        "agent": call.get("agent"),
        "tool": call.get("tool"),
        "at": call.get("at"),
        "at_utc": _iso(when),
    }


def _describe(when, call):
    """One-line "run via tool at time" label used inside a `why`."""
    return "{} via {} at {}".format(call.get("run_id"), call.get("tool"),
                                    _iso(when))


def _sort_key(row):
    """Deterministic ordering for calls, including exact timestamp ties."""
    when, call, _paths = row
    return (when, str(call.get("run_id") or ""), str(call.get("tool") or ""))


def attribute(changes, calls):
    """Attribute each change to the run and tool call responsible.

    `changes` is a list of {"path", "changed_at"} as a manifest diff would
    give. `calls` is a list of {"run_id", "agent", "tool", "args", "at"} from
    the audit trail. Returns one record per change, in input order, each with
    the path, the winning run and tool, a `confidence` of certain, likely or
    unknown, the candidate calls, and a short `why` an operator can read."""
    indexed = []
    for call in calls or []:
        if not isinstance(call, dict):
            continue
        when = _parse_ts(call.get("at"))
        if when is None:
            continue
        indexed.append((when, call, _call_paths(call)))
    indexed.sort(key=_sort_key)
    return [_attribute_one(c, indexed) for c in (changes or [])]


def _attribute_one(change, indexed):
    path = change.get("path") if isinstance(change, dict) else None
    raw_when = change.get("changed_at") if isinstance(change, dict) else None
    record = {
        "path": path,
        "run_id": None,
        "agent": None,
        "tool": None,
        "at": None,
        "confidence": UNKNOWN,
        "candidates": [],
        "why": "",
    }
    when = _parse_ts(raw_when)
    if when is None:
        record["why"] = ("changed_at {!r} is not an ISO8601 timestamp, so no "
                         "call can be placed before or after the change"
                         .format(raw_when))
        return record

    mentions = [(w, c) for w, c, paths in indexed
                if any(_same_path(path, p) for p in paths)]
    if not mentions:
        record["why"] = ("no audit call names this path, so there is nothing "
                         "in the trail to blame")
        return record

    eligible = [(w, c) for w, c in mentions
                if _tool_class(c.get("tool")) != "read" and w <= when]
    if not eligible:
        record["why"] = _why_nothing_eligible(mentions, when)
        return record

    top = eligible[-1][0].replace(microsecond=0)
    tied = [(w, c) for w, c in eligible if w.replace(microsecond=0) == top]
    superseded = len(eligible) - len(tied)

    if len(tied) > 1:
        # Two calls inside the same second. Sub-second order in the trail is
        # not evidence, so both are named and neither is crowned.
        record["confidence"] = LIKELY
        record["candidates"] = [_candidate(w, c) for w, c in tied]
        record["why"] = ("{} calls land in the same second ({}) and the audit "
                         "trail cannot separate them, so both are named: {}"
                         .format(len(tied), _iso(top),
                                 "; ".join([_describe(w, c)
                                            for w, c in tied])))
        return record

    when_won, call = tied[0]
    record["run_id"] = call.get("run_id")
    record["agent"] = call.get("agent")
    record["tool"] = call.get("tool")
    record["at"] = call.get("at")
    record["candidates"] = [_candidate(when_won, call)]
    if _tool_class(call.get("tool")) == "write":
        record["confidence"] = CERTAIN
        record["why"] = ("{} is the only write-capable call naming this path "
                         "in the second before the change ({})"
                         .format(_describe(when_won, call), _iso(when)))
        if superseded:
            record["why"] += ("; {} earlier write(s) to the same path were "
                              "superseded".format(superseded))
    else:
        record["confidence"] = LIKELY
        record["why"] = ("{!r} is not a known write-capable tool, so {} is "
                         "the best available match rather than a proven "
                         "cause".format(call.get("tool"),
                                        _describe(when_won, call)))
    return record


def _why_nothing_eligible(mentions, when):
    """Explain an unknown that had path matches but no usable culprit."""
    later = [(w, c) for w, c in mentions
             if _tool_class(c.get("tool")) != "read" and w > when]
    readers = [(w, c) for w, c in mentions
               if _tool_class(c.get("tool")) == "read"]
    if later:
        w, c = later[0]
        why = ("the nearest write-capable call naming this path ({}) is "
               "recorded after the change at {}, and a cause cannot follow "
               "its effect".format(_describe(w, c), _iso(when)))
        if readers:
            why += ("; the {} earlier call(s) on this path are read-only"
                    .format(len(readers)))
        return why
    tools = sorted(set([str(c.get("tool")) for _w, c in readers]))
    return ("{} call(s) name this path but all of them are read-only ({}), "
            "and reading a file does not change it"
            .format(len(readers), ", ".join(tools)))


def summary(records):
    """Group attribution records by run, worst-first.

    Each group is {"run_id", "agent", "paths", "count", "certain", "likely",
    "unknown"}. Records with no provable single run (the unknowns, and the
    same-second ambiguities) collect under a run_id of None, which always
    sorts last: they are a gap in the evidence, not damage done by some run,
    and folding them into a real run's count would overstate that run's
    blast radius."""
    groups = {}
    order = []
    for record in records or []:
        key = record.get("run_id")
        if key not in groups:
            groups[key] = {"run_id": key, "agent": record.get("agent"),
                           "paths": [], CERTAIN: 0, LIKELY: 0, UNKNOWN: 0}
            order.append(key)
        group = groups[key]
        group["paths"].append(record.get("path"))
        confidence = record.get("confidence", UNKNOWN)
        if confidence in (CERTAIN, LIKELY, UNKNOWN):
            group[confidence] += 1
        if group["agent"] is None:
            group["agent"] = record.get("agent")
    out = []
    for key in order:
        group = groups[key]
        group["count"] = len(group["paths"])
        out.append(group)
    out.sort(key=lambda g: (g["run_id"] is None, -g["count"],
                            str(g["run_id"] or "")))
    return out


def format_report(records):
    """Render the attribution as plain text, worst-offending run first."""
    records = list(records or [])
    named = len([r for r in records if r.get("run_id")])
    lines = ["hearth blame: {} change(s), {} attributed to a run, {} "
             "unattributed".format(len(records), named, len(records) - named)]
    for group in summary(records):
        if group["run_id"]:
            head = "  {}  {} path(s)".format(group["run_id"], group["count"])
            if group["agent"]:
                head += "  agent={}".format(group["agent"])
        else:
            head = "  (unattributed)  {} path(s)".format(group["count"])
        lines.append(head)
        for record in records:
            if record.get("run_id") != group["run_id"]:
                continue
            lines.append("    {}  [{}]".format(record.get("path"),
                                               record.get("confidence")))
            if record.get("run_id"):
                lines.append("      tool={} at={}".format(record.get("tool"),
                                                          record.get("at")))
            else:
                for cand in record.get("candidates", []):
                    lines.append("      candidate: {} via {} at {}".format(
                        cand.get("run_id"), cand.get("tool"),
                        cand.get("at")))
            lines.append("      why: {}".format(record.get("why")))
    return "\n".join(lines)


def _load(path):
    """Read a JSON list of records, or a dict wrapping one."""
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        for key in ("changes", "calls", "records", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    if not isinstance(data, list):
        raise ValueError("{}: expected a JSON list of records".format(path))
    return data


# Fixed data for the self-test. Every timestamp is a literal, so the test
# never reads the clock and never depends on the local timezone.
_T_CHANGE = "2026-11-03T23:00:00Z"

_SELF_TEST_CALLS = [
    # run-1 wrote a.py last, after run-9 had written it earlier
    {"run_id": "run-1", "agent": "planner", "tool": "Write",
     "args": {"path": "C:\\ws\\hearth\\agent\\a.py"},
     "at": "2026-11-03T22:00:00Z"},
    {"run_id": "run-9", "agent": "scout", "tool": "Write",
     "args": {"target": "./agent/a.py"}, "at": "2026-11-03T21:00:00Z"},
    # run-1 also edited e.py, named under a different argument key
    {"run_id": "run-1", "agent": "planner", "tool": "Edit",
     "args": {"file": "agent/e.py"}, "at": "2026-11-03T22:05:00Z"},
    # two runs inside the same second on b.py: genuinely ambiguous
    {"run_id": "run-2", "agent": "builder", "tool": "Write",
     "args": {"path": "agent\\b.py"},
     "at": "2026-11-03T22:30:00.100000Z"},
    {"run_id": "run-3", "agent": "tinker", "tool": "Edit",
     "args": {"dest": "C:/ws/hearth/agent/b.py"},
     "at": "2026-11-03T22:30:00.900000Z"},
    # c.py was only ever read
    {"run_id": "run-4", "agent": "reader", "tool": "Read",
     "args": {"path": "agent/c.py"}, "at": "2026-11-03T22:40:00Z"},
    # d.py was written, but only after the change was observed
    {"run_id": "run-5", "agent": "latecomer", "tool": "Write",
     "args": {"path": "agent/d.py"}, "at": "2026-11-03T23:30:00Z"},
    # f.py was touched by a tool this module does not recognise
    {"run_id": "run-6", "agent": "oddball", "tool": "Frobnicate",
     "args": {"path": "agent/f.py"}, "at": "2026-11-03T22:50:00Z"},
    # a call with an unusable timestamp must be skipped, not crash the join
    {"run_id": "run-7", "agent": "broken", "tool": "Write",
     "args": {"path": "agent/a.py"}, "at": "not-a-timestamp"},
]

_SELF_TEST_CHANGES = [
    {"path": "agent/a.py", "changed_at": _T_CHANGE},
    {"path": "agent\\e.py", "changed_at": _T_CHANGE},
    {"path": "C:/ws/hearth/agent/b.py", "changed_at": _T_CHANGE},
    {"path": "agent/c.py", "changed_at": _T_CHANGE},
    {"path": "agent/d.py", "changed_at": _T_CHANGE},
    {"path": "agent/zz.py", "changed_at": _T_CHANGE},
    {"path": "agent/f.py", "changed_at": _T_CHANGE},
]


def _self_test():
    # path spelling: relative against absolute, and both separators
    assert _same_path("agent/a.py", "C:\\ws\\hearth\\agent\\a.py")
    assert _same_path("./agent/a.py", "agent/a.py")
    assert _same_path("C:/ws/agent/a.py", "AGENT\\A.PY")
    assert not _same_path("agent/a.py", "agent/other_a.py")
    assert not _same_path("agent/a.py", "tests/agent/b.py")
    assert not _same_path("", "agent/a.py")

    # offsets normalise, so an aware and a naive timestamp still compare
    assert _parse_ts("2026-11-03T23:00:00Z") == _parse_ts(
        "2026-11-03T18:00:00-05:00")
    assert _parse_ts("nope") is None
    assert _parse_ts(None) is None

    records = attribute(_SELF_TEST_CHANGES, _SELF_TEST_CALLS)
    assert len(records) == len(_SELF_TEST_CHANGES), len(records)
    by_path = {}
    for record in records:
        by_path[_norm_parts(record["path"])[-1]] = record

    # exactly one write-capable call matches e.py: certain
    rec = by_path["e.py"]
    assert rec["confidence"] == CERTAIN, rec
    assert rec["run_id"] == "run-1", rec
    assert rec["tool"] == "Edit", rec
    assert rec["agent"] == "planner", rec

    # a.py had an earlier write too, so the latest one wins and says so
    rec = by_path["a.py"]
    assert rec["confidence"] == CERTAIN, rec
    assert rec["run_id"] == "run-1", rec
    assert rec["at"] == "2026-11-03T22:00:00Z", rec
    assert "superseded" in rec["why"], rec["why"]
    assert "run-7" not in rec["why"], rec["why"]

    # two candidates in the same second: likely, and BOTH are named
    rec = by_path["b.py"]
    assert rec["confidence"] == LIKELY, rec
    assert rec["run_id"] is None, rec
    assert rec["tool"] is None, rec
    assert sorted([c["run_id"] for c in rec["candidates"]]) == [
        "run-2", "run-3"], rec["candidates"]
    assert "run-2" in rec["why"] and "run-3" in rec["why"], rec["why"]
    assert "cannot separate" in rec["why"], rec["why"]

    # a read-only tool is never blamed for a write
    rec = by_path["c.py"]
    assert rec["confidence"] == UNKNOWN, rec
    assert rec["run_id"] is None, rec
    assert rec["candidates"] == [], rec
    assert "read-only" in rec["why"], rec["why"]

    # a call after the change time is never blamed
    rec = by_path["d.py"]
    assert rec["confidence"] == UNKNOWN, rec
    assert rec["run_id"] is None, rec
    assert rec["candidates"] == [], rec
    assert "after the change" in rec["why"], rec["why"]

    # a path no call mentions stays unknown, with no guessed run id
    rec = by_path["zz.py"]
    assert rec["confidence"] == UNKNOWN, rec
    assert rec["run_id"] is None, rec
    assert rec["candidates"] == [], rec
    assert "no audit call names this path" in rec["why"], rec["why"]
    for call in _SELF_TEST_CALLS:
        assert call["run_id"] not in rec["why"], rec["why"]

    # an unrecognised tool is named, but only ever as likely
    rec = by_path["f.py"]
    assert rec["confidence"] == LIKELY, rec
    assert rec["run_id"] == "run-6", rec
    assert "not a known write-capable tool" in rec["why"], rec["why"]

    # an unparseable change timestamp degrades one row and says why
    bad = attribute([{"path": "agent/a.py", "changed_at": "soon"}],
                    _SELF_TEST_CALLS)[0]
    assert bad["confidence"] == UNKNOWN, bad
    assert bad["run_id"] is None, bad
    assert "not an ISO8601 timestamp" in bad["why"], bad["why"]

    # empty inputs are quiet, not an error
    assert attribute([], []) == []
    assert attribute(None, None) == []
    assert summary([]) == []

    groups = summary(records)
    assert [g["run_id"] for g in groups] == ["run-1", "run-6", None], groups
    assert groups[0]["count"] == 2, groups[0]
    assert groups[0][CERTAIN] == 2, groups[0]
    assert groups[0]["agent"] == "planner", groups[0]
    assert groups[-1]["run_id"] is None, groups[-1]
    assert groups[-1]["count"] == 4, groups[-1]
    assert groups[-1][UNKNOWN] == 3 and groups[-1][LIKELY] == 1, groups[-1]

    report = format_report(records)
    assert report.startswith("hearth blame: 7 change(s), 3 attributed to a "
                             "run, 4 unattributed"), report
    # worst-first: the run with two paths is reported before the run with one
    assert report.index("run-1") < report.index("run-6"), report
    assert report.index("run-6") < report.index("(unattributed)"), report
    for path in ("agent/a.py", "agent/zz.py", "C:/ws/hearth/agent/b.py"):
        assert path in report, path
    # the ambiguous record shows both candidates in the report itself
    assert report.count("candidate: run-2") == 1, report
    assert report.count("candidate: run-3") == 1, report

    # records round-trip as JSON, which is what --json prints
    assert json.loads(json.dumps(records)) == records

    print("hearth-blame self-test OK")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="hearth-blame",
        description="attribute changed paths to the run and tool call "
                    "responsible, from the audit trail")
    p.add_argument("--changes",
                   help="JSON file of {path, changed_at} manifest changes")
    p.add_argument("--calls",
                   help="JSON file of {run_id, agent, tool, args, at} calls")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="print the attribution records as JSON")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    if a.changes and a.calls:
        records = attribute(_load(a.changes), _load(a.calls))
        if a.as_json:
            print(json.dumps(records, indent=2, sort_keys=True))
        else:
            print(format_report(records))
        return 0
    p.error("nothing to do (use --self-test, or --changes FILE --calls FILE)")


if __name__ == "__main__":
    sys.exit(main())
