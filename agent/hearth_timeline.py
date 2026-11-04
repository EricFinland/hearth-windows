#!/usr/bin/env python3
"""hearth timeline: find the dead air in a run.

A four hour marathon that produced almost nothing is rarely slow
everywhere. It stalled in two places, and the transcript buries them:
scrolling past a thousand tokens is not how anyone finds the thirty eight
minutes between the third tool call and the next token. This module takes
the one thing every run already has, a list of timestamped events, and
turns "it felt slow" into a line you can point at.

`build(events)` returns the shape of the run: the events in order, each
carrying the duration until the next one, the total wall time, the single
longest gap, every gap over a threshold, and how the time divides by kind
with percentages. `format_report(result)` prints that as a timeline with a
text bar per step, because the shape of a run is obvious as soon as one bar
is twice as long as the rest and completely invisible in a column of
numbers.

What this deliberately does NOT do:

  No clock.     Every timestamp is data, passed in by the caller. Nothing
                here calls datetime.now(), so the same events always give
                the same answer and the self-test is deterministic.
  No reading.   It does not open log files, databases or the ledger, and
                does not know where a run is stored. Whoever has the
                events hands them over; this only does the arithmetic.
  No network.   Standard library only, no process launching, no I/O other
                than the CLI reading a JSON list from a file or stdin.
  No repair.    Events out of chronological order get sorted so the
                timeline is usable, but the result says `unordered` is
                true rather than quietly pretending the input was fine.
                A timestamp that will not parse is collected in `skipped`
                with a reason. Bad input never raises.
  No blame.     It reports where the time went, not why. A long gap may be
                a model thinking, a rate limit, or an operator at lunch,
                and this module has no way to tell those apart.

One deliberate edge: a run with zero wall time (no events, one event, or
several sharing a timestamp) has nothing to apportion, so every percentage
is 0.0 and `percent_total` is 0.0. Whenever there is time to divide, the
percentages are rounded to one decimal by largest remainder, so they sum
to exactly 100.0 and no second goes missing in the report.
"""

import argparse
import datetime
import json
import math
import sys

DEFAULT_STALL_SECONDS = 60
BAR_WIDTH = 24
KIND_COL = 12
DETAIL_COL = 22
UTC = datetime.timezone.utc


def parse_timestamp(text):
    """Parse an ISO8601 timestamp into an aware UTC datetime.

    Accepts a trailing `Z`, an explicit offset such as `+02:00`, a space
    instead of the `T` separator, and a naive timestamp (read as UTC, since
    a run with no offset recorded is almost always logged in UTC). Raises
    ValueError with a short reason for anything else; callers collect that
    reason rather than letting one bad line kill the timeline."""
    if isinstance(text, bool) or not isinstance(text, str):
        raise ValueError("timestamp is not a string")
    raw = text.strip()
    if not raw:
        raise ValueError("timestamp is empty")
    fixed = raw
    if fixed[-1] in ("Z", "z"):
        fixed = fixed[:-1] + "+00:00"
    if "T" not in fixed and " " in fixed:
        fixed = fixed.replace(" ", "T", 1)
    try:
        when = datetime.datetime.fromisoformat(fixed)
    except ValueError:
        raise ValueError("unparseable timestamp: {}".format(raw))
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC)


def _human(seconds):
    """A duration a person can read at a glance: 0s, 4.2s, 38m 12s,
    1h 02m 03s. Never scientific notation, never bare float seconds."""
    value = float(seconds or 0.0)
    if value <= 0:
        return "0s"
    if value < 1:
        return "{:.1f}s".format(value)
    whole = int(round(value))
    hours, rest = divmod(whole, 3600)
    mins, secs = divmod(rest, 60)
    if hours:
        return "{}h {:02d}m {:02d}s".format(hours, mins, secs)
    if mins:
        return "{}m {:02d}s".format(mins, secs)
    return "{}s".format(secs)


def _clock(seconds):
    """Elapsed time since the first event, as H:MM:SS, for the left gutter
    of the timeline so steps can be located by offset into the run."""
    whole = int(round(float(seconds or 0.0)))
    hours, rest = divmod(whole, 3600)
    mins, secs = divmod(rest, 60)
    return "{:d}:{:02d}:{:02d}".format(hours, mins, secs)


def _apportion(values, total):
    """Percentages of `total`, one decimal, summing to exactly 100.0.

    Rounding each share on its own loses or invents a tenth of a percent
    per row, which looks like a bug in a report whose whole job is to
    account for time. Largest remainder hands the leftover tenths to the
    rows that were rounded down hardest. With nothing to divide, every
    share is 0.0 rather than a division by zero."""
    if not values:
        return []
    if not total or float(total) <= 0:
        return [0.0 for _ in values]
    scaled = [float(v) / float(total) * 1000.0 for v in values]
    tenths = [int(math.floor(x)) for x in scaled]
    short = 1000 - sum(tenths)
    if short > 0:
        order = sorted(range(len(values)),
                       key=lambda i: (-(scaled[i] - tenths[i]), i))
        for step in range(short):
            tenths[order[step % len(order)]] += 1
    return [t / 10.0 for t in tenths]


def build(events, stall_seconds=DEFAULT_STALL_SECONDS):
    """Turn a list of {"at", "kind", "detail"} events into a timeline.

    Returns a dict with: `steps` (events in order, each with the duration
    until the next event, its share of the run, and whether that counts as
    a stall), `total_seconds`, `longest_gap`, `stalls`, `by_kind`,
    `skipped`, and `unordered`. Never raises on bad input."""
    try:
        threshold = float(stall_seconds)
    except (TypeError, ValueError):
        threshold = float(DEFAULT_STALL_SECONDS)
    if math.isnan(threshold) or threshold < 0:
        threshold = 0.0

    parsed = []
    skipped = []
    for index, event in enumerate(events or []):
        if not isinstance(event, dict):
            skipped.append({"index": index, "event": event,
                            "reason": "event is not an object"})
            continue
        try:
            when = parse_timestamp(event.get("at"))
        except ValueError as exc:
            skipped.append({"index": index, "event": event,
                            "reason": str(exc)})
            continue
        parsed.append({"index": index, "at": when,
                       "kind": str(event.get("kind") or "unknown"),
                       "detail": str(event.get("detail") or "")})

    unordered = False
    for pos in range(1, len(parsed)):
        if parsed[pos]["at"] < parsed[pos - 1]["at"]:
            unordered = True
            break
    parsed.sort(key=lambda e: (e["at"], e["index"]))

    total = 0.0
    started = None
    ended = None
    if parsed:
        started = parsed[0]["at"]
        ended = parsed[-1]["at"]
        total = (ended - started).total_seconds()

    gaps = []
    for pos, event in enumerate(parsed):
        if pos + 1 < len(parsed):
            gaps.append((parsed[pos + 1]["at"] - event["at"]).total_seconds())
        else:
            gaps.append(0.0)
    shares = _apportion(gaps, total)

    steps = []
    for pos, event in enumerate(parsed):
        nxt = parsed[pos + 1] if pos + 1 < len(parsed) else None
        elapsed = (event["at"] - started).total_seconds()
        steps.append({
            "position": pos,
            "source_index": event["index"],
            "at": event["at"].isoformat(),
            "elapsed_seconds": elapsed,
            "clock": _clock(elapsed),
            "kind": event["kind"],
            "detail": event["detail"],
            "seconds": gaps[pos],
            "human": _human(gaps[pos]),
            "percent": shares[pos],
            "next_kind": nxt["kind"] if nxt else None,
            "next_detail": nxt["detail"] if nxt else None,
            "is_stall": bool(nxt) and gaps[pos] >= threshold,
        })

    stalls = [dict(s) for s in steps if s["is_stall"]]
    stalls.sort(key=lambda s: (-s["seconds"], s["position"]))

    longest = None
    for step in steps:
        if step["next_kind"] is None:
            continue
        if longest is None or step["seconds"] > longest["seconds"]:
            longest = dict(step)

    order = []
    totals = {}
    counts = {}
    for step in steps:
        kind = step["kind"]
        if kind not in totals:
            totals[kind] = 0.0
            counts[kind] = 0
            order.append(kind)
        totals[kind] += step["seconds"]
        counts[kind] += 1
    order.sort(key=lambda k: (-totals[k], k))
    kind_shares = _apportion([totals[k] for k in order], total)
    by_kind = []
    for pos, kind in enumerate(order):
        by_kind.append({"kind": kind, "seconds": totals[kind],
                        "human": _human(totals[kind]),
                        "events": counts[kind],
                        "percent": kind_shares[pos]})

    return {
        "event_count": len(parsed),
        "stall_seconds": threshold,
        "unordered": unordered,
        "skipped": skipped,
        "started_at": started.isoformat() if started else None,
        "ended_at": ended.isoformat() if ended else None,
        "total_seconds": total,
        "total_human": _human(total),
        "steps": steps,
        "stalls": stalls,
        "longest_gap": longest,
        "by_kind": by_kind,
        "percent_total": round(sum(k["percent"] for k in by_kind), 1),
    }


def _clip(text, width):
    """Fit text into a fixed column, marking anything cut with a +."""
    value = str(text if text is not None else "")
    if len(value) <= width:
        return value
    return value[:max(width - 1, 0)] + "+"


def _bar(seconds, longest):
    """A text bar scaled against the longest step in the run. Any nonzero
    step gets at least one block, so a short step shows up as a short step
    rather than vanishing out of the gutter."""
    if not longest or float(longest) <= 0 or float(seconds or 0.0) <= 0:
        return ""
    filled = int(round(BAR_WIDTH * (float(seconds) / float(longest))))
    return "#" * min(max(filled, 1), BAR_WIDTH)


def format_report(result):
    """Render a build() result as a readable timeline."""
    if not isinstance(result, dict):
        return "hearth timeline: nothing to report"
    steps = result.get("steps") or []
    skipped = result.get("skipped") or []
    lines = ["hearth timeline"]
    if not steps:
        lines.append("  no events to place on a timeline")
        for item in skipped:
            lines.append("  skipped [{}] {}".format(item.get("index"),
                                                    item.get("reason")))
        return "\n".join(lines)

    lines.append("  window  {} -> {}".format(result.get("started_at"),
                                             result.get("ended_at")))
    lines.append("  total   {} across {} event(s)".format(
        result.get("total_human"), result.get("event_count")))
    if result.get("unordered"):
        lines.append("  note    events arrived out of chronological order; "
                     "sorted here by timestamp")
    if skipped:
        lines.append("  note    {} event(s) skipped, listed below".format(
            len(skipped)))
    lines.append("")

    longest_step = 0.0
    for step in steps:
        longest_step = max(longest_step, float(step.get("seconds") or 0.0))
    for step in steps:
        lines.append("  {:>8}  {}  {}  {}  {:>9}  {:>5.1f}%{}".format(
            step.get("clock", ""),
            _clip(step.get("kind"), KIND_COL).ljust(KIND_COL),
            _clip(step.get("detail"), DETAIL_COL).ljust(DETAIL_COL),
            _bar(step.get("seconds"), longest_step).ljust(BAR_WIDTH),
            step.get("human", "0s"),
            float(step.get("percent") or 0.0),
            "  STALL" if step.get("is_stall") else ""))
    lines.append("")

    gap = result.get("longest_gap")
    if gap:
        lines.append("  longest gap  {} between {} ({}) and {} ({})".format(
            gap.get("human"), gap.get("kind"),
            _clip(gap.get("detail") or "no detail", DETAIL_COL),
            gap.get("next_kind"),
            _clip(gap.get("next_detail") or "no detail", DETAIL_COL)))
    else:
        lines.append("  longest gap  none (a timeline needs two events)")

    stalls = result.get("stalls") or []
    lines.append("  stalls over {}: {}".format(
        _human(result.get("stall_seconds")), len(stalls)))
    for stall in stalls:
        lines.append("    {:>9}  {} -> {}  ({})".format(
            stall.get("human"), stall.get("kind"), stall.get("next_kind"),
            _clip(stall.get("detail") or "no detail", DETAIL_COL)))

    lines.append("  time by kind")
    for row in result.get("by_kind") or []:
        lines.append("    {}  {:>9}  {:>5.1f}%  {} event(s)".format(
            _clip(row.get("kind"), KIND_COL).ljust(KIND_COL),
            row.get("human"), float(row.get("percent") or 0.0),
            row.get("events")))
    if skipped:
        lines.append("  skipped")
        for item in skipped:
            lines.append("    [{}] {}".format(item.get("index"),
                                              item.get("reason")))
    return "\n".join(lines)


def _self_test():
    # 1. no events at all: no steps, no gap, no stalls, no division by zero
    empty = build([])
    assert empty["event_count"] == 0, empty
    assert empty["steps"] == [], empty
    assert empty["stalls"] == [], empty
    assert empty["by_kind"] == [], empty
    assert empty["longest_gap"] is None, empty
    assert empty["total_seconds"] == 0.0, empty
    assert empty["percent_total"] == 0.0, empty
    assert empty["started_at"] is None, empty
    assert empty["ended_at"] is None, empty
    assert "no events" in format_report(empty), format_report(empty)

    # 2. one event: a zero length run, still renders, still no percentages
    one = build([{"at": "2026-01-01T00:00:00Z", "kind": "start",
                  "detail": "boot"}])
    assert one["event_count"] == 1, one
    assert one["total_seconds"] == 0.0, one
    assert len(one["steps"]) == 1, one
    assert one["steps"][0]["seconds"] == 0.0, one["steps"]
    assert one["steps"][0]["percent"] == 0.0, one["steps"]
    assert one["steps"][0]["is_stall"] is False, one["steps"]
    assert one["stalls"] == [], one
    assert one["longest_gap"] is None, one
    assert one["by_kind"] == [{"kind": "start", "seconds": 0.0,
                               "human": "0s", "events": 1,
                               "percent": 0.0}], one["by_kind"]
    assert one["percent_total"] == 0.0, one
    assert "0:00:00" in format_report(one), format_report(one)

    # 3. the real case: an hour with one obvious stall in the middle
    run = [{"at": "2026-01-01T00:00:00Z", "kind": "start",
            "detail": "boot"},
           {"at": "2026-01-01T00:10:00+00:00", "kind": "tool_call",
            "detail": "grep"},
           {"at": "2026-01-01T00:48:00Z", "kind": "token",
            "detail": "first token"},
           {"at": "2026-01-01T01:00:00Z", "kind": "done",
            "detail": "wrap"}]
    got = build(run)
    assert got["unordered"] is False, got
    assert got["skipped"] == [], got
    assert got["total_seconds"] == 3600.0, got
    assert got["total_human"] == "1h 00m 00s", got
    assert [s["seconds"] for s in got["steps"]] == [600.0, 2280.0, 720.0,
                                                    0.0], got["steps"]
    assert [s["percent"] for s in got["steps"]] == [16.7, 63.3, 20.0,
                                                    0.0], got["steps"]
    assert abs(sum(s["percent"] for s in got["steps"]) - 100.0) < 1e-9
    assert got["percent_total"] == 100.0, got["percent_total"]
    assert abs(sum(k["percent"] for k in got["by_kind"]) - 100.0) < 1e-9
    assert got["longest_gap"]["seconds"] == 2280.0, got["longest_gap"]
    assert got["longest_gap"]["kind"] == "tool_call", got["longest_gap"]
    assert got["longest_gap"]["next_kind"] == "token", got["longest_gap"]
    assert got["longest_gap"]["human"] == "38m 00s", got["longest_gap"]
    assert len(got["stalls"]) == 3, got["stalls"]
    assert got["stalls"][0]["seconds"] == 2280.0, got["stalls"]
    assert [k["kind"] for k in got["by_kind"]] == ["tool_call", "token",
                                                   "start", "done"], got
    assert got["by_kind"][0]["percent"] == 63.3, got["by_kind"]
    assert got["steps"][-1]["next_kind"] is None, got["steps"]
    assert got["steps"][2]["clock"] == "0:48:00", got["steps"][2]

    # a higher threshold leaves only the real stall
    tuned = build(run, stall_seconds=1800)
    assert len(tuned["stalls"]) == 1, tuned["stalls"]
    assert tuned["stalls"][0]["kind"] == "tool_call", tuned["stalls"]
    assert tuned["stall_seconds"] == 1800.0, tuned
    # a nonsense threshold falls back instead of raising
    assert build(run, stall_seconds="x")["stall_seconds"] == 60.0
    assert build(run, stall_seconds=-5)["stall_seconds"] == 0.0

    report = format_report(got)
    assert "hearth timeline" in report, report
    assert "#" in report, report
    assert "STALL" in report, report
    assert "longest gap" in report, report
    assert "time by kind" in report, report
    assert "38m 00s" in report, report
    assert "out of chronological order" not in report, report

    # 4. the same events shuffled: same timeline, reported as unordered
    shuffled = [run[2], run[0], run[3], run[1]]
    fixed = build(shuffled)
    assert fixed["unordered"] is True, fixed
    assert [s["kind"] for s in fixed["steps"]] == [
        s["kind"] for s in got["steps"]], fixed["steps"]
    assert [s["seconds"] for s in fixed["steps"]] == [
        s["seconds"] for s in got["steps"]], fixed["steps"]
    assert fixed["total_seconds"] == 3600.0, fixed
    assert fixed["steps"][0]["source_index"] == 1, fixed["steps"]
    assert "out of chronological order" in format_report(fixed)

    # 5. mixed offsets, a trailing Z and a naive stamp all land in UTC
    mixed = build([{"at": "2026-01-01T02:00:00+02:00", "kind": "a",
                    "detail": ""},
                   {"at": "2026-01-01 00:30:00", "kind": "b",
                    "detail": ""},
                   {"at": "2025-12-31T19:45:00-05:00", "kind": "c",
                    "detail": ""}])
    assert mixed["unordered"] is False, mixed
    assert mixed["started_at"] == "2026-01-01T00:00:00+00:00", mixed
    assert mixed["ended_at"] == "2026-01-01T00:45:00+00:00", mixed
    assert mixed["total_seconds"] == 2700.0, mixed
    assert [s["seconds"] for s in mixed["steps"]] == [1800.0, 900.0,
                                                      0.0], mixed["steps"]
    assert mixed["percent_total"] == 100.0, mixed

    # 6. duplicate timestamps: zero length steps, percentages still whole
    dup = build([{"at": "2026-01-01T00:00:00Z", "kind": "a", "detail": ""},
                 {"at": "2026-01-01T00:00:00Z", "kind": "b", "detail": ""},
                 {"at": "2026-01-01T00:01:00Z", "kind": "c", "detail": ""}])
    assert [s["seconds"] for s in dup["steps"]] == [0.0, 60.0, 0.0], dup
    assert [s["percent"] for s in dup["steps"]] == [0.0, 100.0, 0.0], dup
    assert dup["percent_total"] == 100.0, dup
    assert dup["longest_gap"]["kind"] == "b", dup["longest_gap"]
    assert len(dup["stalls"]) == 1, dup["stalls"]
    # every event sharing one timestamp: zero wall time, no percentages
    flat = build([{"at": "2026-01-01T00:00:00Z", "kind": "a", "detail": ""},
                  {"at": "2026-01-01T00:00:00Z", "kind": "a", "detail": ""}])
    assert flat["total_seconds"] == 0.0, flat
    assert flat["percent_total"] == 0.0, flat
    assert flat["longest_gap"]["seconds"] == 0.0, flat["longest_gap"]
    assert flat["stalls"] == [], flat
    assert flat["by_kind"][0]["events"] == 2, flat["by_kind"]
    assert format_report(flat), "a flat run still renders"

    # 7. bad input is collected, never raised
    messy = build([{"at": "2026-01-01T00:00:00Z", "kind": "start",
                    "detail": "ok"},
                   {"at": "not-a-date", "kind": "token", "detail": "bad"},
                   {"at": "", "kind": "token", "detail": "empty"},
                   {"at": None, "kind": "token", "detail": "missing"},
                   "a bare string, not an event",
                   {"at": "2026-01-01T00:02:00Z", "kind": "done",
                    "detail": "ok"}])
    assert messy["event_count"] == 2, messy
    assert len(messy["skipped"]) == 4, messy["skipped"]
    assert [s["index"] for s in messy["skipped"]] == [1, 2, 3, 4], messy
    assert "unparseable" in messy["skipped"][0]["reason"], messy["skipped"]
    assert "empty" in messy["skipped"][1]["reason"], messy["skipped"]
    assert "not a string" in messy["skipped"][2]["reason"], messy["skipped"]
    assert "not an object" in messy["skipped"][3]["reason"], messy["skipped"]
    assert messy["total_seconds"] == 120.0, messy
    assert messy["percent_total"] == 100.0, messy
    assert "skipped" in format_report(messy), format_report(messy)
    # an event with no kind or detail is still placed on the timeline
    bare = build([{"at": "2026-01-01T00:00:00Z"},
                  {"at": "2026-01-01T00:00:30Z"}])
    assert bare["by_kind"][0]["kind"] == "unknown", bare["by_kind"]
    assert bare["steps"][0]["detail"] == "", bare["steps"]
    # nothing usable at all: a report, not a crash
    none_usable = build([{"at": "nope"}])
    assert none_usable["steps"] == [], none_usable
    assert "no events" in format_report(none_usable)
    assert format_report(None) == "hearth timeline: nothing to report"
    assert build(None)["event_count"] == 0, "None instead of a list"

    # 8. the whole result survives a JSON round trip for --json
    assert json.loads(json.dumps(got))["total_seconds"] == 3600.0
    assert json.loads(json.dumps(messy))["event_count"] == 2

    # 9. durations read the way a person would say them
    assert _human(0) == "0s", _human(0)
    assert _human(0.25) == "0.2s", _human(0.25)
    assert _human(45) == "45s", _human(45)
    assert _human(2280) == "38m 00s", _human(2280)
    assert _human(3723) == "1h 02m 03s", _human(3723)
    assert _clock(3723) == "1:02:03", _clock(3723)
    assert _bar(0, 100) == "", _bar(0, 100)
    assert _bar(1, 10000) == "#", "a short step is still visible"
    assert _bar(100, 100) == "#" * BAR_WIDTH, _bar(100, 100)
    assert _bar(5, 0) == "", "no scale, no bar"
    assert _clip("abcdef", 4) == "abc+", _clip("abcdef", 4)
    assert _apportion([], 10) == [], "nothing to apportion"
    assert _apportion([1, 1, 1], 3) == [33.4, 33.3, 33.3], \
        _apportion([1, 1, 1], 3)
    assert sum(_apportion([1, 1, 1, 1, 1, 1, 7], 13)) == 100.0
    assert _apportion([1, 2], 0) == [0.0, 0.0], "zero total, zero shares"

    print("hearth-timeline self-test OK")
    return 0


def _load_events(path):
    """Read a JSON event list from a file, or from stdin for "-". Accepts
    either a bare list or an object with an "events" key, since run logs
    tend to wrap the list in some envelope."""
    if path in (None, "-"):
        text = sys.stdin.read()
    else:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("events", [])
    if not isinstance(data, list):
        raise ValueError("expected a JSON list of events")
    return data


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="hearth-timeline",
        description="show where a run's wall time actually went")
    p.add_argument("events", nargs="?", default="-",
                   help="JSON event list, or - for stdin")
    p.add_argument("--stall-seconds", type=float,
                   default=float(DEFAULT_STALL_SECONDS),
                   help="gap size that counts as a stall (default 60)")
    p.add_argument("--json", action="store_true",
                   help="print the timeline dict instead of the report")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    try:
        events = _load_events(a.events)
    except (OSError, ValueError) as exc:
        sys.stderr.write("hearth-timeline: {}\n".format(exc))
        return 2
    result = build(events, stall_seconds=a.stall_seconds)
    if a.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
