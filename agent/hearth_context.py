#!/usr/bin/env python3
r"""hearth context autopsy: what filled the window, and what fell out of it.

    python agent/hearth_context.py --window 8192 --file segments.json
    python agent/hearth_context.py --self-test

THE PROBLEM
-----------
A hosted model gives you a couple of hundred thousand tokens and you never
think about it. A local model gives you four or eight thousand, and inside
that you have to fit a system prompt, every tool schema, retrieved knowledge,
and the whole conversation so far. When it does not fit, nothing announces
itself: the oldest turns are dropped, the agent forgets what it was told, and
the symptom is "the model got stupid" rather than "the window overflowed".

This takes the pieces that go into a prompt and says where the window
actually went: the share each piece holds, what gets evicted to make room,
and the case nobody plans for, where the fixed pieces alone do not fit and
no amount of forgetting will help.

THE TOKEN COUNT IS AN ESTIMATE
------------------------------
Counting tokens properly needs the model's own tokenizer, which lives in the
engine and is not free to call. The default here is the four-characters-per
-token rule of thumb: close enough to spot that tool schemas are eating a
third of your window, not close enough to decide whether you are three tokens
from the edge. It leans high on code and punctuation, which is the right
direction to be wrong in when the consequence is silent truncation. Pass
token_fn to use a real tokenizer; every number here comes from it.

A SEGMENT
---------
A dict: {"name", "text"} and optionally "evictable" (default False) and
"kind". Evictable segments are the ones a loop may drop to make room, oldest
first, which in practice means conversation history. A system prompt and the
tool schemas are not evictable, and that is exactly why they are worth
measuring.

Standard library only. Pure computation: nothing here reads a model, a
database or the network.
"""

import argparse
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CHARS_PER_TOKEN = 4  # the rule of thumb; see the module docstring
# Shares that are worth saying out loud rather than leaving in a table.
HOG_SHARE = 0.50     # one segment holding half the window
SCHEMA_SHARE = 0.25  # tool schemas holding a quarter of it
TIGHT_SHARE = 0.90   # this close to full, the next turn will not fit


def estimate_tokens(text):
    """Rough token count for a string. Never zero for a non-empty string: a
    piece that is present costs something, and rounding it to free is the
    error that hides a thousand small segments."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


def _measure(segments, token_fn):
    out = []
    for i, seg in enumerate(segments or []):
        text = seg.get("text") or ""
        out.append({
            "name": seg.get("name") or "segment {}".format(i + 1),
            "kind": seg.get("kind") or "",
            "evictable": bool(seg.get("evictable")),
            "tokens": token_fn(text),
            "chars": len(text),
            "order": i,
        })
    return out


def autopsy(segments, window, reserve=0, token_fn=None):
    """Account for a context window.

    `reserve` is room held back for the model's own answer, which is part of
    the window and the thing people forget to subtract.

    Returns the measured segments (largest first), the totals, and an
    eviction plan when it does not fit. `wedged` is the case that matters:
    true when the pieces that cannot be evicted do not fit on their own, so
    dropping history will never make room and something has to get smaller."""
    token_fn = token_fn or estimate_tokens
    window = max(0, int(window))
    reserve = max(0, int(reserve))
    measured = _measure(segments, token_fn)
    total = sum(s["tokens"] for s in measured)
    budget = max(0, window - reserve)
    fixed = sum(s["tokens"] for s in measured if not s["evictable"])

    # Evict the evictable, oldest first, until what is left fits the budget.
    evicted, kept_total = [], total
    for seg in sorted((s for s in measured if s["evictable"]), key=lambda s: s["order"]):
        if kept_total <= budget:
            break
        evicted.append(seg["name"])
        kept_total -= seg["tokens"]

    for seg in measured:
        seg["share"] = (seg["tokens"] / window) if window else 0.0
        seg["evicted"] = seg["name"] in evicted
    measured.sort(key=lambda s: (-s["tokens"], s["order"]))

    schema_tokens = sum(s["tokens"] for s in measured if s["kind"] == "tools")
    result = {
        "window": window, "reserve": reserve, "budget": budget,
        "total": total, "fixed": fixed, "after_eviction": kept_total,
        "headroom": budget - kept_total,
        "overflow": max(0, total - budget),
        "fits": total <= budget,
        "fits_after_eviction": kept_total <= budget,
        "wedged": fixed > budget,
        "evicted": evicted,
        "segments": measured,
        "schema_tokens": schema_tokens,
        "schema_share": (schema_tokens / window) if window else 0.0,
        "warnings": [],
    }

    w = result["warnings"]
    if result["wedged"]:
        w.append("the pieces that cannot be evicted need {:,} tokens of a {:,} budget: "
                 "dropping history will not help".format(fixed, budget))
    elif not result["fits"]:
        w.append("over by {:,} tokens: {} segment(s) will be dropped".format(
            result["overflow"], len(evicted)))
    if window and result["schema_share"] >= SCHEMA_SHARE:
        w.append("tool schemas hold {:.0%} of the window: consider fewer tools "
                 "per turn".format(result["schema_share"]))
    for seg in measured:
        if seg["share"] >= HOG_SHARE:
            w.append("{!r} alone holds {:.0%} of the window".format(seg["name"], seg["share"]))
    if result["fits"] and window and (kept_total / window) >= TIGHT_SHARE:
        w.append("{:.0%} full with {:,} tokens of headroom: the next turn may not fit".format(
            kept_total / window, max(0, result["headroom"])))
    return result


def format_report(r):
    """The report, as the CLI prints it."""
    lines = ["hearth context autopsy: {:,}-token window".format(r["window"])]
    if r["reserve"]:
        lines.append("  reserving {:,} for the answer, so {:,} usable".format(
            r["reserve"], r["budget"]))
    lines.append("")
    if r["segments"]:
        width = max(len(s["name"]) for s in r["segments"])
        for s in r["segments"]:
            flag = "  DROPPED" if s["evicted"] else ("  fixed" if not s["evictable"] else "")
            lines.append("  {:<{w}}  {:>8,}  {:>5.1f}%{}".format(
                s["name"], s["tokens"], s["share"] * 100, flag, w=width))
    else:
        lines.append("  nothing in the window")
    lines += [
        "",
        "  total           {:>8,}".format(r["total"]),
        "  fixed           {:>8,}".format(r["fixed"]),
        "  after eviction  {:>8,}".format(r["after_eviction"]),
        "  headroom        {:>8,}".format(r["headroom"]),
        "",
        "  verdict: " + ("fits" if r["fits"] else
                         ("WEDGED" if r["wedged"] else
                          "fits only after dropping {} segment(s)".format(len(r["evicted"])))),
    ]
    for warning in r["warnings"]:
        lines.append("  ! " + warning)
    return "\n".join(lines)


def _self_test():
    tok = lambda n: "x" * (n * CHARS_PER_TOKEN)  # noqa: E731 - exactly n tokens

    # estimate_tokens: proportional, never zero for real text, rounds up
    assert estimate_tokens("") == 0 and estimate_tokens(None) == 0
    assert estimate_tokens("x") == 1, "a present segment is never free"
    assert estimate_tokens("x" * 4) == 1 and estimate_tokens("x" * 5) == 2
    assert estimate_tokens(tok(100)) == 100

    # a comfortable window: everything fits, nothing dropped
    segs = [
        {"name": "system", "text": tok(200)},
        {"name": "tools", "kind": "tools", "text": tok(300)},
        {"name": "turn 1", "text": tok(100), "evictable": True},
        {"name": "turn 2", "text": tok(100), "evictable": True},
    ]
    r = autopsy(segs, window=8192)
    assert r["fits"] and not r["wedged"] and r["evicted"] == []
    assert r["total"] == 700 and r["fixed"] == 500
    assert r["headroom"] == 8192 - 700
    assert r["after_eviction"] == 700
    # largest first, and shares sum to the total share
    assert [s["name"] for s in r["segments"]][0] == "tools"
    assert abs(sum(s["share"] for s in r["segments"]) - 700 / 8192) < 1e-9
    assert r["warnings"] == [], r["warnings"]
    assert "fits" in format_report(r)

    # reserve is taken out of the window, not added to it
    r = autopsy(segs, window=1000, reserve=400)
    assert r["budget"] == 600 and not r["fits"], r
    assert r["overflow"] == 100, r["overflow"]

    # eviction: oldest evictable first, and only as many as needed
    segs = [
        {"name": "system", "text": tok(100)},
        {"name": "turn 1", "text": tok(100), "evictable": True},
        {"name": "turn 2", "text": tok(100), "evictable": True},
        {"name": "turn 3", "text": tok(100), "evictable": True},
    ]
    r = autopsy(segs, window=350)
    assert not r["fits"] and r["fits_after_eviction"] and not r["wedged"]
    assert r["evicted"] == ["turn 1"], r["evicted"]
    assert r["after_eviction"] == 300 and r["headroom"] == 50
    assert [s["evicted"] for s in r["segments"] if s["name"] == "turn 1"] == [True]
    assert "1 segment(s) will be dropped" in " ".join(r["warnings"])

    # two must go when one is not enough
    r = autopsy(segs, window=250)
    assert r["evicted"] == ["turn 1", "turn 2"], r["evicted"]
    assert r["after_eviction"] == 200 and r["fits_after_eviction"]

    # wedged: the fixed pieces alone do not fit, so eviction cannot save it
    r = autopsy([
        {"name": "system", "text": tok(500)},
        {"name": "tools", "kind": "tools", "text": tok(500)},
        {"name": "turn 1", "text": tok(100), "evictable": True},
    ], window=800)
    assert r["wedged"] and not r["fits"] and not r["fits_after_eviction"]
    assert r["evicted"] == ["turn 1"], "it still drops what it can"
    assert r["after_eviction"] == 1000 > r["budget"]
    assert "will not help" in " ".join(r["warnings"])
    assert "WEDGED" in format_report(r)

    # schema warning fires on share, and names tools by kind not by name
    r = autopsy([{"name": "whatever", "kind": "tools", "text": tok(300)},
                 {"name": "system", "text": tok(100)}], window=1000)
    assert r["schema_tokens"] == 300 and abs(r["schema_share"] - 0.3) < 1e-9
    assert "tool schemas hold 30%" in " ".join(r["warnings"])
    # under the threshold it stays quiet
    quiet = autopsy([{"name": "t", "kind": "tools", "text": tok(100)}], window=1000)
    assert not any("tool schemas" in w for w in quiet["warnings"])

    # one segment holding half the window is called out
    r = autopsy([{"name": "giant doc", "text": tok(600)}], window=1000)
    assert any("'giant doc' alone holds 60%" in w for w in r["warnings"]), r["warnings"]

    # nearly full, but fitting, warns about the next turn
    r = autopsy([{"name": "system", "text": tok(950)}], window=1000)
    assert r["fits"] and any("the next turn may not fit" in w for w in r["warnings"])

    # degenerate inputs: no segments, zero window, negative arguments
    empty = autopsy([], window=1000)
    assert empty["total"] == 0 and empty["fits"] and empty["segments"] == []
    assert "nothing in the window" in format_report(empty)
    assert autopsy(None, window=1000)["total"] == 0
    zero = autopsy([{"name": "a", "text": "hello"}], window=0)
    assert zero["window"] == 0 and not zero["fits"] and zero["segments"][0]["share"] == 0.0
    assert autopsy(segs, window=-5)["window"] == 0
    assert autopsy(segs, window=1000, reserve=-5)["reserve"] == 0

    # unnamed and empty segments are measured, not dropped
    r = autopsy([{"text": tok(10)}, {"name": "blank", "text": ""}], window=1000)
    assert [s["name"] for s in sorted(r["segments"], key=lambda s: s["order"])] == [
        "segment 1", "blank"]
    assert r["total"] == 10

    # an injected tokenizer is used for every number
    r = autopsy([{"name": "a", "text": "hello"}], window=100, token_fn=lambda t: 7)
    assert r["total"] == 7 and r["segments"][0]["tokens"] == 7

    # a tie in size keeps the original order, so the report is stable
    r = autopsy([{"name": "b", "text": tok(10)}, {"name": "a", "text": tok(10)}], window=100)
    assert [s["name"] for s in r["segments"]] == ["b", "a"]

    print("hearth-context self-test OK")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="hearth-context",
                                description="account for a model's context window")
    p.add_argument("--window", type=int, default=8192, help="context size in tokens")
    p.add_argument("--reserve", type=int, default=0, help="tokens held back for the answer")
    p.add_argument("--file", default=None,
                   help="JSON list of {name, text, evictable, kind}; omit to read stdin")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    try:
        raw = (open(a.file, encoding="utf-8").read() if a.file else sys.stdin.read())
        segments = json.loads(raw)
    except OSError as exc:
        p.error("cannot read segments: {}".format(exc))
    except ValueError as exc:
        p.error("segments must be JSON: {}".format(exc))
    if not isinstance(segments, list):
        p.error("segments must be a JSON list of objects")
    r = autopsy(segments, window=a.window, reserve=a.reserve)
    print(json.dumps(r, indent=2) if a.as_json else format_report(r))
    return 0 if r["fits_after_eviction"] else 1


if __name__ == "__main__":
    sys.exit(main())
