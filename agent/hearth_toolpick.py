#!/usr/bin/env python3
r"""hearth tool picker: a short, relevant menu instead of every tool you own.

    python agent/hearth_toolpick.py --goal "read the log file" --budget 600 \
        --file tools.json
    python agent/hearth_toolpick.py --self-test

THE PROBLEM
-----------
hearth_context.py measures it: on a four-thousand-token window, nine tool
schemas can hold a third of the budget before the model has read a single
word of the actual task. This module acts on that measurement.

Budget is only half the reason to cut the menu. A small local model also
picks the wrong tool more often the longer the menu gets: every extra schema
is one more plausible near-miss to confuse the right tool with. Sending the
handful that could plausibly serve this turn buys accuracy and tokens at the
same time.

HOW IT SCORES
-------------
Transparent and explainable on purpose, so a bad pick can be argued with
rather than shrugged at. Every signal is visible in the result:

  name verbatim  the tool's own name appears in the goal string
  word overlap   goal words shared with the tool name, then with its
                 description (capped, so a long description cannot win on
                 volume alone)
  recent         the tool was already used in this run, most recent counting
                 for most
  prior          a per-tool nudge the caller sets ("prior" on the tool dict)
  size           a small penalty per hundred tokens of schema, because a
                 fat schema has to earn its place

WHAT THIS DELIBERATELY IS NOT
-----------------------------
Not an embedding model and not a classifier. There is no training, no vector
store, and no second model call to decide what the first model is allowed to
see: that would cost more than the tokens it saves and could not explain
itself afterwards. Keyword overlap is a weak signal and is meant to be. A
tool the caller cannot afford to lose to a weak signal goes in `pinned`,
where no score can touch it.

Not a tool runner, not a router, and not a cache. It takes a list of schemas
and a goal string, and says which ones to print this turn.

A TOOL
------
A dict: {"name", "description", "args": {...}} and optionally "prior", a
float added straight to the score. Names are expected to be unique. A
schema's token cost is estimated from its serialised JSON form with the same
four-characters-per-token rule of thumb hearth_context.py uses, and for the
same reason: close enough to see that the menu is too long, not close enough
to bet the last three tokens of the window on. Pass token_fn to use a real
tokenizer; every number then comes from it.

PINNED, AND THE WEDGED CASE
---------------------------
Pinned tools are always shown, even when they alone overrun the budget. That
case is borrowed wholesale from hearth_context.py and called `wedged` by the
same name: no amount of dropping the rest will help, so the answer is a
smaller pinned set or a bigger window. The result says so out loud instead
of quietly handing back a menu that cannot fit.

Selection is deterministic, and ties are broken by name. Two turns with the
same goal get the same menu in the same order, so the model is not re-reading
a reshuffled list for no reason.

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
# Signal weights. Kept far apart on purpose: a tool the goal names outright
# must not be outscored by a pile of incidental description overlap.
W_NAME_VERBATIM = 3.0
W_NAME_WORD = 1.0
W_DESC_WORD = 0.35
W_RECENT = 0.5
DESC_HIT_CAP = 3            # a long description cannot win on volume alone
SIZE_PENALTY_PER_100 = 0.05  # per hundred tokens of schema
SCORE_PRECISION = 6          # so float noise cannot pretend to be a tie
# Words that overlap with everything and therefore mean nothing here.
STOPWORDS = frozenset((
    "the", "and", "for", "with", "that", "this", "from", "into", "then",
    "you", "your", "its", "our", "are", "was", "were", "has", "have", "had",
    "will", "would", "can", "could", "should", "all", "any", "but", "not",
    "please", "now", "get", "got", "use", "using", "used", "about", "when",
    "what", "which", "who", "how", "why", "there", "here", "some", "more",
    "out", "off", "via", "per", "one", "two",
))


def estimate_tokens(text):
    """Rough token count for a string. Never zero for a non-empty string: a
    schema that is present costs something, and rounding it to free is the
    error that hides nine small tools."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


def schema_text(tool):
    """The serialised form a tool costs the window as. Only the three fields
    the model is actually shown: "prior" is a knob for this module, not part
    of the schema, so it must not change the price."""
    return json.dumps({
        "name": (tool or {}).get("name") or "",
        "description": (tool or {}).get("description") or "",
        "args": (tool or {}).get("args") or {},
    }, sort_keys=True)


def _words(text):
    """Content words of a string: lowercased, split on anything that is not
    a letter or digit (so read_file yields read and file), with stopwords and
    single characters dropped."""
    out = []
    for word in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(word) > 1 and word not in STOPWORDS:
            out.append(word)
    return out


def rank(tools, goal, recent_tools=None, token_fn=None):
    """Score every tool against a goal string, best first.

    `recent_tools` is the names already used in this run, most recent first;
    being used recently is a mild vote for being wanted again, worth less the
    further back it was.

    Returns a list of entries, each carrying the score, the schema's token
    cost, and the `signals` the score was built from, so the decision can be
    read rather than trusted. Ties are broken by name, which makes the order
    stable across turns."""
    token_fn = token_fn or estimate_tokens
    goal_low = (goal or "").lower()
    goal_words = set(_words(goal))
    recent = list(recent_tools or [])
    ranked = []
    for i, tool in enumerate(tools or []):
        name = tool.get("name") or "tool {}".format(i + 1)
        tokens = token_fn(schema_text(tool))
        name_hits = len(set(_words(name)) & goal_words)
        desc_hits = min(DESC_HIT_CAP,
                        len(set(_words(tool.get("description"))) & goal_words))
        verbatim = bool(tool.get("name")) and name.lower() in goal_low
        position = recent.index(name) if name in recent else -1
        recent_score = (W_RECENT / (1 + position)) if position >= 0 else 0.0
        prior = float(tool.get("prior") or 0.0)
        penalty = SIZE_PENALTY_PER_100 * (tokens / 100.0)
        score = (W_NAME_VERBATIM * (1.0 if verbatim else 0.0)
                 + W_NAME_WORD * name_hits
                 + W_DESC_WORD * desc_hits
                 + recent_score + prior - penalty)
        ranked.append({
            "name": name,
            "tokens": tokens,
            "score": round(score, SCORE_PRECISION),
            "order": i,
            "pinned": False,
            "reason": "",
            "signals": {
                "name_verbatim": verbatim,
                "name_hits": name_hits,
                "desc_hits": desc_hits,
                "recent": round(recent_score, SCORE_PRECISION),
                "recent_position": position,
                "prior": prior,
                "size_penalty": round(penalty, SCORE_PRECISION),
            },
        })
    ranked.sort(key=lambda e: (-e["score"], e["name"]))
    return ranked


def _explain(entry):
    """The one line that says why this tool scored what it scored."""
    s = entry["signals"]
    bits = []
    if s["name_verbatim"]:
        bits.append("named in the goal")
    if s["name_hits"]:
        bits.append("{} name word(s) match".format(s["name_hits"]))
    if s["desc_hits"]:
        bits.append("{} description word(s) match".format(s["desc_hits"]))
    if s["recent"]:
        bits.append("used recently (#{})".format(s["recent_position"] + 1))
    if s["prior"]:
        bits.append("prior {:+.2f}".format(s["prior"]))
    if not bits:
        bits.append("nothing in the goal matches")
    return ", ".join(bits)


def select(tools, goal, budget_tokens, pinned=None, recent_tools=None,
           token_fn=None):
    """Choose the menu for one turn.

    Pinned tools come first, in the order the caller listed them, and are
    always included even when they alone overrun the budget. The rest are
    taken best-first while they fit; a tool too big for what is left is
    dropped and the next one is still considered, so one fat schema does not
    cost every smaller tool behind it its place.

    Returns the selected tools, the dropped ones, the tokens the selection
    actually costs, a one-line reason per decision, and `wedged`: true when
    the pinned set alone does not fit, where dropping the rest will not
    help."""
    token_fn = token_fn or estimate_tokens
    budget = max(0, int(budget_tokens))
    pinned_names = list(pinned or [])
    ranked = rank(tools, goal, recent_tools=recent_tools, token_fn=token_fn)

    pinned_entries = [e for e in ranked if e["name"] in pinned_names]
    pinned_entries.sort(key=lambda e: (pinned_names.index(e["name"]), e["order"]))
    pinned_tokens = sum(e["tokens"] for e in pinned_entries)

    selected, dropped, used = [], [], 0
    for entry in pinned_entries:
        entry["pinned"] = True
        entry["reason"] = "pinned: always shown ({:,} tokens)".format(entry["tokens"])
        used += entry["tokens"]
        selected.append(entry)
    for entry in ranked:
        if entry["pinned"]:
            continue
        left = budget - used
        if entry["tokens"] <= left:
            used += entry["tokens"]
            entry["reason"] = "score {:.2f}: {} ({:,} tokens)".format(
                entry["score"], _explain(entry), entry["tokens"])
            selected.append(entry)
        else:
            entry["reason"] = "score {:.2f}: no room ({:,} tokens, {:,} left)".format(
                entry["score"], entry["tokens"], max(0, left))
            dropped.append(entry)

    result = {
        "goal": goal or "",
        "budget": budget,
        "tokens": used,
        "pinned_tokens": pinned_tokens,
        "headroom": budget - used,
        "wedged": pinned_tokens > budget,
        "selected": selected,
        "dropped": dropped,
        "reasons": ["{}: {}".format(e["name"], e["reason"])
                    for e in selected + dropped],
        "warnings": [],
    }
    w = result["warnings"]
    if result["wedged"]:
        w.append("the pinned tools need {:,} tokens of a {:,} budget: "
                 "dropping the rest will not help".format(pinned_tokens, budget))
    if dropped:
        w.append("{} of {} tool(s) dropped to fit {:,} tokens".format(
            len(dropped), len(ranked), budget))
    return result


def format_menu(r):
    """The menu, as the CLI prints it."""
    lines = ["hearth tool menu: {:,}-token budget".format(r["budget"])]
    if r["goal"]:
        lines.append('  goal: "{}"'.format(r["goal"]))
    lines.append("")
    entries = r["selected"] + r["dropped"]
    if entries:
        width = max(len(e["name"]) for e in entries)
        for entry in r["selected"]:
            lines.append("  SHOW  {:<{w}}  {}".format(
                entry["name"], entry["reason"], w=width))
        for entry in r["dropped"]:
            lines.append("  drop  {:<{w}}  {}".format(
                entry["name"], entry["reason"], w=width))
    else:
        lines.append("  no tools offered")
    lines += [
        "",
        "  shown     {:>8,} tool(s)".format(len(r["selected"])),
        "  tokens    {:>8,}".format(r["tokens"]),
        "  headroom  {:>8,}".format(r["headroom"]),
        "",
        "  verdict: " + ("WEDGED" if r["wedged"] else
                         ("the whole set fits" if not r["dropped"] else
                          "{} tool(s) fit".format(len(r["selected"])))),
    ]
    for warning in r["warnings"]:
        lines.append("  ! " + warning)
    return "\n".join(lines)


def _self_test():
    def tool(name, desc, args=None, prior=None):
        t = {"name": name, "description": desc, "args": args or {}}
        if prior is not None:
            t["prior"] = prior
        return t

    # estimate_tokens: proportional, never zero for real text, rounds up
    assert estimate_tokens("") == 0 and estimate_tokens(None) == 0
    assert estimate_tokens("x") == 1, "a present schema is never free"
    assert estimate_tokens("x" * 4) == 1 and estimate_tokens("x" * 5) == 2

    # the price is the serialised schema, and "prior" is not part of it
    plain = tool("read_file", "read a file from disk")
    nudged = tool("read_file", "read a file from disk", prior=9.0)
    assert schema_text(plain) == schema_text(nudged)
    assert estimate_tokens(schema_text(plain)) > 0

    kit = [
        tool("read_file", "read a file from disk", {"path": "str"}),
        tool("write_file", "write text to a file on disk", {"path": "str"}),
        tool("send_email", "send an email to a recipient", {"to": "str"}),
        tool("list_dir", "list the entries of a directory", {"path": "str"}),
        tool("http_get", "fetch a url over http", {"url": "str"}),
    ]
    goal = "read_file the config and tell me what is in it"

    priced = [estimate_tokens(schema_text(t)) for t in kit]
    whole_kit = sum(priced)

    # deterministic: the same input scores the same, twice, in the same order
    first = [(e["name"], e["score"]) for e in rank(kit, goal)]
    second = [(e["name"], e["score"]) for e in rank(kit, goal)]
    assert first == second, (first, second)
    run_a = select(kit, goal, 60, recent_tools=["http_get"])
    run_b = select(kit, goal, 60, recent_tools=["http_get"])
    assert run_a["reasons"] == run_b["reasons"], "same turn, same menu"
    assert [e["name"] for e in run_a["selected"]] == \
           [e["name"] for e in run_b["selected"]]

    # a tool the goal names outright beats one that only shares a word
    order = [e["name"] for e in rank(kit, goal)]
    assert order[0] == "read_file", order
    assert order.index("read_file") < order.index("write_file"), order
    top = rank(kit, goal)[0]
    assert top["signals"]["name_verbatim"], top["signals"]
    assert not rank(kit, goal)[1]["signals"]["name_verbatim"]
    # and the explanation says so, rather than only showing a number
    assert "named in the goal" in _explain(top), _explain(top)
    unrelated = [e for e in rank(kit, "nothing relevant whatsoever")
                 if e["name"] == "send_email"][0]
    assert "nothing in the goal matches" in _explain(unrelated)

    # ties break by name, so the menu does not reshuffle between turns
    tied = [tool("b_tool", "same words here"), tool("a_tool", "same words here")]
    scored = rank(tied, "a goal sharing nothing")
    assert scored[0]["score"] == scored[1]["score"], scored
    assert [e["name"] for e in scored] == ["a_tool", "b_tool"], scored
    # reversing the input does not change the order it comes back in
    assert [e["name"] for e in rank(list(reversed(tied)), "x")] == \
           ["a_tool", "b_tool"]

    # at equal relevance, a tool used recently is preferred to a fresh one
    pair = [tool("a_tool", "same words here"), tool("b_tool", "same words here")]
    used_b = rank(pair, "a goal sharing nothing", recent_tools=["b_tool"])
    assert [e["name"] for e in used_b] == ["b_tool", "a_tool"], used_b
    assert used_b[0]["score"] > used_b[1]["score"]
    assert used_b[0]["signals"]["recent"] == W_RECENT
    assert used_b[1]["signals"]["recent"] == 0.0
    # the most recent counts for most
    both = rank(pair, "x", recent_tools=["a_tool", "b_tool"])
    assert [e["name"] for e in both] == ["a_tool", "b_tool"], both

    # a per-tool prior is a nudge the caller controls
    nudge = [tool("a_tool", "same words here"),
             tool("b_tool", "same words here", prior=2.0)]
    assert [e["name"] for e in rank(nudge, "x")] == ["b_tool", "a_tool"]
    # a negative prior pushes a tool down without removing it
    down = [tool("a_tool", "same words here", prior=-2.0),
            tool("b_tool", "same words here")]
    assert [e["name"] for e in rank(down, "x")] == ["b_tool", "a_tool"]

    # size is a penalty: the same tool costs more and so scores less when fat
    thin = rank([tool("a_tool", "words")], "x")[0]
    fatter = rank([tool("a_tool", "words " * 100)], "x")[0]
    assert fatter["tokens"] > thin["tokens"]
    assert fatter["score"] < thin["score"], (fatter["score"], thin["score"])

    # a budget big enough yields everything, and nothing is dropped
    r = select(kit, goal, whole_kit)
    assert len(r["selected"]) == len(kit) and r["dropped"] == []
    assert r["tokens"] == whole_kit and r["headroom"] == 0
    assert not r["wedged"] and r["warnings"] == [], r["warnings"]
    assert "the whole set fits" in format_menu(r)

    # a budget of zero yields nothing at all without pins
    r = select(kit, goal, 0)
    assert r["selected"] == [] and len(r["dropped"]) == len(kit)
    assert r["tokens"] == 0 and not r["wedged"], r
    assert "no room" in r["dropped"][0]["reason"]

    # a budget of zero yields exactly the pinned tools, and says it is wedged
    r = select(kit, goal, 0, pinned=["send_email"])
    assert [e["name"] for e in r["selected"]] == ["send_email"], r["selected"]
    assert r["wedged"] and r["headroom"] < 0
    assert r["tokens"] == r["pinned_tokens"] > 0
    assert "will not help" in " ".join(r["warnings"]), r["warnings"]
    assert "WEDGED" in format_menu(r)
    assert "pinned: always shown" in r["selected"][0]["reason"]

    # a pinned tool is shown even when it alone overruns the budget
    big = tool("big_tool", "a very long description. " + "detail " * 200)
    r = select([big] + kit, goal, 5, pinned=["big_tool"])
    assert [e["name"] for e in r["selected"]] == ["big_tool"], r["selected"]
    assert r["wedged"] and r["tokens"] > r["budget"]

    # pinned beats score: it is shown first, in the order the caller listed
    r = select(kit, goal, whole_kit, pinned=["http_get", "send_email"])
    assert [e["name"] for e in r["selected"][:2]] == ["http_get", "send_email"]
    assert all(e["pinned"] for e in r["selected"][:2])
    assert not any(e["pinned"] for e in r["selected"][2:])

    # one fat schema does not cost the smaller tools behind it their place
    fat = tool("read_file", "read a file from disk. " + "detail " * 100)
    lean = tool("list_dir", "list the entries of a directory")
    lean_price = estimate_tokens(schema_text(lean))
    r = select([fat, lean], goal, lean_price + 5)
    assert [e["name"] for e in r["selected"]] == ["list_dir"], r["selected"]
    assert [e["name"] for e in r["dropped"]] == ["read_file"], r["dropped"]
    assert "no room" in r["dropped"][0]["reason"]

    # the tokens figure is the sum of what was actually selected, always
    for budget in (0, 1, 23, 47, 60, 93, whole_kit, whole_kit * 3):
        for pins in ([], ["send_email"], ["http_get", "list_dir"]):
            r = select(kit, goal, budget, pinned=pins, recent_tools=["list_dir"])
            assert r["tokens"] == sum(e["tokens"] for e in r["selected"]), r
            assert r["headroom"] == r["budget"] - r["tokens"]
            # nothing is lost and nothing is shown twice
            shown = [e["name"] for e in r["selected"]]
            gone = [e["name"] for e in r["dropped"]]
            assert len(shown) + len(gone) == len(kit), (shown, gone)
            assert len(set(shown + gone)) == len(kit), (shown, gone)
            assert sorted(shown + gone) == sorted(t["name"] for t in kit)
            # one line of reasoning per decision, no more and no less
            assert len(r["reasons"]) == len(kit), r["reasons"]
            assert all(line.strip() for line in r["reasons"])
            # everything that fits and is not wedged stays inside the budget
            if not r["wedged"]:
                assert r["tokens"] <= r["budget"], r

    # an injected tokenizer is used for every number
    r = select(kit, goal, 25, token_fn=lambda t: 10)
    assert [e["tokens"] for e in r["selected"]] == [10, 10], r["selected"]
    assert r["tokens"] == 20 and len(r["dropped"]) == 3

    # degenerate inputs: no tools, no goal, negative budget
    empty = select([], "anything", 100)
    assert empty["selected"] == [] and empty["dropped"] == []
    assert empty["tokens"] == 0 and not empty["wedged"]
    assert "no tools offered" in format_menu(empty)
    assert select(None, "anything", 100)["tokens"] == 0
    assert select(kit, "", 100)["goal"] == ""
    assert select(kit, None, 100)["goal"] == ""
    assert select(kit, goal, -5)["budget"] == 0
    assert rank(kit, None) and rank(None, goal) == []

    # an unnamed tool is named and measured, not quietly dropped
    r = rank([{"description": "no name here"}], goal)
    assert [e["name"] for e in r] == ["tool 1"], r
    assert r[0]["tokens"] > 0 and not r[0]["signals"]["name_verbatim"]

    # a pin naming a tool that is not on offer is simply not there
    r = select(kit, goal, whole_kit, pinned=["nonexistent"])
    assert len(r["selected"]) == len(kit) and r["pinned_tokens"] == 0
    assert not r["wedged"]

    # the printed menu marks each line as shown or dropped
    r = select(kit, goal, 60)
    menu = format_menu(r)
    assert menu.count("SHOW  ") == len(r["selected"]), menu
    assert menu.count("drop  ") == len(r["dropped"]), menu
    assert "read_file" in menu and 'goal: "' in menu

    print("hearth-toolpick self-test OK")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="hearth-toolpick",
        description="choose which tool schemas to show the model this turn")
    p.add_argument("--goal", default="", help="what the model is trying to do")
    p.add_argument("--budget", type=int, default=800,
                   help="tokens the tool menu may spend")
    p.add_argument("--file", default=None,
                   help="JSON list of {name, description, args, prior}; "
                        "omit to read stdin")
    p.add_argument("--pin", default="",
                   help="comma separated names that are always shown")
    p.add_argument("--recent", default="",
                   help="comma separated names already used, most recent first")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    try:
        raw = (open(a.file, encoding="utf-8").read() if a.file else sys.stdin.read())
        tools = json.loads(raw)
    except OSError as exc:
        p.error("cannot read tools: {}".format(exc))
    except ValueError as exc:
        p.error("tools must be JSON: {}".format(exc))
    if not isinstance(tools, list):
        p.error("tools must be a JSON list of objects")
    names = lambda s: [x.strip() for x in s.split(",") if x.strip()]  # noqa: E731
    r = select(tools, a.goal, a.budget, pinned=names(a.pin),
               recent_tools=names(a.recent))
    print(json.dumps(r, indent=2) if a.as_json else format_menu(r))
    return 1 if r["wedged"] else 0


if __name__ == "__main__":
    sys.exit(main())
