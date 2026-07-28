#!/usr/bin/env python3
"""hearth router: declarative model selection for "auto" launches.

A rule-based map from a goal (and the tools a run was granted) to which local
model should run it, so cheap models handle easy tasks and a coder or larger
model handles the harder ones. The rules are declared once, rendered by the
system configuration to a read-only JSON file, and consulted by every launch
that asks for model "auto".

Rules come from /etc/hearth/router.json (env HEARTH_ROUTER overrides the path).
A missing or invalid file yields an empty ruleset, which makes the router a
no-op: it just returns the caller's default. The file shape (the contract with
the system configuration agent) is:

  {
    "default": "llama3.2:3b",
    "rules": [
      {"name": "code",
       "any_keywords": ["code", "refactor", "python", "bug"],
       "tools_any": ["edit_file", "replace_in_files"],
       "model": "qwen2.5-coder:latest"},
      {"name": "research",
       "any_keywords": ["research", "summarize", "analyze"],
       "model": "qwen2.5:7b-instruct"}
    ]
  }

The selection is pure and deterministic (you pass the goal, the tools, and the
rules), so it is fully testable with no config file on disk. Standard library
only.

This module also does task-aware routing: classify() scores a turn's
difficulty from its prompt, conversation history, and tool signals into one
of three tiers (small/medium/large), escalate() moves a tier up by exactly
one step on a demonstrated failure, and route() turns a tier into a
concrete, installed, hardware-appropriate model using hearth_shop's fit
verdicts and hearth_hw's hardware probe. See the "Task-aware routing"
section below for the full design and its honesty tradeoffs - short version:
this is a heuristic, it will misroute sometimes, and it is deliberately
biased toward under- rather than over-estimating difficulty because
escalation is a tested recovery path and there is no equivalent
"de-escalation" for the opposite mistake.

Config:
  HEARTH_ROUTER   path to the rules JSON; default /etc/hearth/router.json
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hearth_shop  # noqa: E402
import hearth_hw  # noqa: E402

DEFAULT_RULES = os.environ.get("HEARTH_ROUTER", "/etc/hearth/router.json")


def _tools_list(tools):
    """Normalize a tools value (list, comma string, or None) to a list of
    stripped non-empty strings."""
    if tools is None:
        return []
    if isinstance(tools, str):
        parts = tools.split(",")
    else:
        parts = list(tools)
    out = []
    for p in parts:
        s = str(p).strip()
        if s:
            out.append(s)
    return out


def load_rules(path=None):
    """Return {"default": str, "rules": [...]} from the rules JSON, or an empty
    ruleset if the file is missing, unreadable, not a dict, or malformed. Rule
    entries that are not dicts or lack a model are skipped."""
    path = path or DEFAULT_RULES
    empty = {"default": "", "rules": []}
    if not path or not os.path.exists(path):
        return empty
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    default = data.get("default")
    default = default if isinstance(default, str) else ""
    rules = []
    raw = data.get("rules")
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict) or not entry.get("model"):
                sys.stderr.write(
                    "hearth-router: skipping invalid rule: {!r}\n".format(
                        entry.get("name") if isinstance(entry, dict) else entry))
                continue
            rules.append(entry)
    return {"default": default, "rules": rules}


def _matches(rule, goal_lower, tools):
    """Pure: does this rule match the (lowercased) goal or the tools list?
    A rule matches when ANY of its any_keywords is a case-insensitive substring
    of the goal, OR any of its tools_any is present in tools."""
    for kw in rule.get("any_keywords") or []:
        if kw and str(kw).lower() in goal_lower:
            return True
    wanted = rule.get("tools_any") or []
    if wanted and tools:
        have = set(tools)
        for t in wanted:
            if t in have:
                return True
    return False


def choose_model(goal, tools=None, rules=None, fallback=None):
    """Return the model that should run this goal. Evaluate the rules in order;
    the first one that matches (by keyword or by tool) wins and returns its
    model. With no match, return rules["default"] if set, else the caller's
    fallback, else "". Pure and deterministic."""
    if rules is None:
        rules = load_rules()
    goal_lower = (goal or "").lower()
    toollist = _tools_list(tools)
    for rule in rules.get("rules") or []:
        if _matches(rule, goal_lower, toollist):
            return rule.get("model")
    default = rules.get("default")
    if default:
        return default
    if fallback:
        return fallback
    return ""


def explain(goal, tools, rules):
    """Return {"chosen", "matched_rule", "why"} describing the selection, for
    observability and logging."""
    goal_lower = (goal or "").lower()
    toollist = _tools_list(tools)
    for rule in rules.get("rules") or []:
        if _matches(rule, goal_lower, toollist):
            return {"chosen": rule.get("model"),
                    "matched_rule": rule.get("name"),
                    "why": "matched rule {!r}".format(rule.get("name"))}
    default = rules.get("default")
    if default:
        return {"chosen": default, "matched_rule": None,
                "why": "no rule matched; used default"}
    return {"chosen": "", "matched_rule": None,
            "why": "no rule matched and no default set"}


# ---------------------------------------------------------------------------
# Task-aware routing
# ---------------------------------------------------------------------------
#
# The rules above answer "which model handles goals shaped like X" from a
# static, hand-authored config. This section answers a narrower, live
# question instead: given the turn actually in front of us right now - its
# prompt, how the conversation has gone so far, and what the machine can
# actually run - which of Hearth's three difficulty tiers should handle it,
# and which installed model in that tier should get the work?
#
# HONESTY UP FRONT: classify() is a heuristic built from a handful of
# surface signals (keyword lists, word counts, a regex for "names a file and
# line"). It WILL misroute sometimes - a prompt can use none of the listed
# keywords and still be hard, or use "why" in a sentence that is not
# actually hard. This module is deliberately biased toward under-estimating
# difficulty rather than over-estimating it: a tie or a weak/absent signal
# lands in the middle tier, and reaching the top tier requires a real, named
# signal (difficulty language, a failed/retried previous turn, or a
# search-then-edit pattern across several files), not just "not obviously
# easy". The bias is toward the cheap tier because the two mistakes have
# asymmetric consequences, not because "cheap" is more often correct:
# routing a hard task to a small model wastes at most one turn until
# escalate() below corrects it - a tested, built-in recovery. Routing an
# easy task to a big model wastes GPU time and wall-clock latency with no
# corresponding correction, because stickiness (below) deliberately never
# evicts a capable loaded model just to save memory on a task that turned
# out to be trivial. An unrecoverable waste is worse than a recoverable one,
# so classify() leans cheap by default.
#
# Escalation is what makes leaning cheap defensible: a malformed tool call,
# an outright failure, or a retried turn is a concrete, observed signal that
# the current tier was not enough, and route() acts on it via escalate(),
# which moves exactly one tier up - never straight to the top on a single
# bad turn, and never standing still and repeating the same failure.
#
# Model switching is not free: evicting whatever Ollama has resident and
# loading a different model costs real seconds. route() is sticky - it will
# not swap a currently-loaded model for a different one just because a new
# turn scores a little differently, as long as the loaded model is still
# capable enough and still fits. It only switches on a genuine escalation
# signal, or when the currently loaded model is no longer a viable
# candidate at all (uninstalled, or no longer fits the reported hardware).

TIER_SMALL = "small"
TIER_MEDIUM = "medium"
TIER_LARGE = "large"

# Ascending order: cheapest/fastest first. escalate() and the tier-fallback
# search in route() both walk this tuple, so its order IS the policy.
TIER_ORDER = (TIER_SMALL, TIER_MEDIUM, TIER_LARGE)

# (min_params_b inclusive, max_params_b exclusive), bucketing hearth_shop's
# CATALOG into tiers - a judgment call, not a spec. Small tops out below 3B:
# "does not need to think" work (variable renames, import fixes, a single
# known-file edit). Medium covers the 3-10B "reference all-rounder" band
# that most everyday work should land in. Large is everything 10B and up,
# for the tasks worth waiting for: debugging across files, real design
# work, or anything that has already failed once.
TIER_PARAM_RANGE = {
    TIER_SMALL: (0.0, 3.0),
    TIER_MEDIUM: (3.0, 10.0),
    TIER_LARGE: (10.0, None),
}

# Difficulty language: each hit nudges classify() toward the large tier.
# Deliberately short and specific rather than exhaustive - see the honesty
# note above on why a list that sometimes misses is an accepted, stated
# limitation rather than a hidden one. "refactor across" is deliberately
# NOT in this list as a fixed phrase: see _refactor_across_signal below,
# which checks for both words anywhere in the prompt instead of requiring
# them adjacent - "refactor the permission engine across three modules"
# does not contain the literal substring "refactor across" but is exactly
# the case this signal exists to catch.
_HARD_KEYWORDS = (
    "why", "debug", "root cause", "investigate",
    "redesign", "design", "architecture", "diagnose", "trace through",
    "figure out why", "across the codebase", "regression", "flaky",
    "race condition", "deep dive", "intermittent", "not sure why",
)

# Mechanical-edit language: each hit nudges classify() toward the small
# tier, alongside a short prompt or a named file:line. Bare "add"/"remove"/
# "delete" are included deliberately broad (this module's stated bias is
# cheap-by-default, and a single -1 here cannot reach the small tier alone -
# it takes at least one more negative signal, e.g. a short prompt or a
# named file). "fix" alone is deliberately NOT included: "fix the bug" is
# genuinely ambiguous difficulty and must not be pre-judged mechanical.
_EASY_KEYWORDS = (
    "rename", "typo", "fix the import", "fix import", "add a comment",
    "bump the version", "bump version", "delete the", "remove the",
    "one-line", "one line fix", "small fix", "trivial fix",
    "add", "remove", "delete", "update the docstring", "format", "sort",
)

# A file name with an extension, optionally followed by a line number
# ("utils.py:42", "src/foo.py, line 12"). Naming an exact location is
# itself a signal the task is scoped and mechanical, not exploratory.
_FILE_LOCATION_RE = re.compile(
    r"[\w./\\-]+\.[A-Za-z][A-Za-z0-9]{0,5}(:\d+|,?\s*line\s+\d+)", re.IGNORECASE
)

# A snake_case identifier (hearth_loop, hearth_tools) - the underscore is
# what distinguishes "this names a real module/file" from ordinary English
# prose, which is why this stays narrow rather than trying to parse noun
# phrases in general.
_SNAKE_CASE_RE = re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]*\b")

# Tool names (from hearth_tools.TOOL_SPECS) this module reads as "exploring"
# versus "changing" the codebase. A search followed by edits across several
# files in the same turn is exactly the "search_files sweep followed by
# multi-file edits" pattern called out as non-mechanical.
_SEARCH_TOOLS = frozenset({"search_files", "list_tree", "list_files", "web_search"})
_EDIT_TOOLS = frozenset({"edit_file", "write_file", "replace_in_files"})


def _word_boundary_hits(haystack_lower, phrases):
    """Phrases (already lowercase) that appear in haystack_lower as whole
    words/word-sequences, not as a substring of some unrelated word."""
    hits = []
    for phrase in phrases:
        if re.search(r"\b" + re.escape(phrase) + r"\b", haystack_lower):
            hits.append(phrase)
    return hits


def _named_components(text):
    """Distinct snake_case identifiers mentioned in text (e.g. hearth_loop,
    hearth_tools), order-preserving. A cheap, concrete proxy for "this
    prompt names several real modules/files" - deliberately narrow
    (snake_case only, via _SNAKE_CASE_RE) rather than an attempt at general
    noun-phrase parsing, which this module has no reliable way to do."""
    seen = []
    for m in _SNAKE_CASE_RE.finditer(text.lower()):
        tok = m.group(0)
        if tok not in seen:
            seen.append(tok)
    return seen


def _refactor_across_signal(low):
    """True when the prompt says "refactor" (or refactoring/refactored/
    refactors) AND "across" anywhere in it, not necessarily adjacent -
    "refactor the permission engine across hearth_loop and hearth_tools"
    is exactly the multi-module-refactor case this exists to catch, and it
    does not contain the literal substring "refactor across"."""
    return bool(re.search(r"\brefactor\w*\b", low)) and bool(re.search(r"\bacross\b", low))


def classify(prompt, history=None, signals=None):
    """Estimate how hard this turn is, as one of the three tiers, from the
    prompt text, the conversation so far, and whatever the caller knows
    about this turn's tools (signals). Pure and deterministic - no I/O, no
    randomness, no network - so it is fully unit-testable and safe to call
    on every turn.

    prompt: the user's (or agent's next-step) request text.
    history: an optional list of prior turns, oldest first, as dicts that
        may carry "failed": bool and/or "malformed_tool_call": bool. Only
        the last two entries are consulted - recent state matters far more
        than ancient history for "should this turn get a bigger model".
    signals: an optional dict of extra context the caller already has
        cheaply available, so classify() does not have to re-derive it:
          turn_index          - which turn this is (defaults to len(history)).
          previous_failed      - bool, in addition to any history-derived flag.
          retried               - bool: this turn is a redo of a failed one.
          malformed_tool_call   - bool, in addition to any history-derived flag.
          tools_used            - list (or comma string) of tool names this
                                 turn is expected to use.
          distinct_files        - int count of files this turn touches.

    Returns {"tier": TIER_*, "reason": str, "score": int}. reason lists
    every signal that fired, in the order it was evaluated, so a caller can
    show its work ("routed to the 14B because ...") instead of a black-box
    tier name - see the module docstring: silent routing is
    indistinguishable from a bug.
    """
    prompt = "" if prompt is None else str(prompt)
    history = list(history) if history else []
    signals = dict(signals) if signals else {}

    stripped = prompt.strip()
    low = stripped.lower()
    words = stripped.split()
    wc = len(words)

    score = 0
    reasons = []

    # Difficulty signals are computed first and gate the length-based prior
    # below, not the other way around. A short prompt can still name one of
    # the hardest things you can ask for in ten words ("refactor the
    # permission engine across hearth_loop, hearth_tools and the sidecar"),
    # and brevity must not outvote that - see the module docstring's
    # honesty note. An explicit difficulty term (a keyword hit, a
    # refactor-across pattern, or naming several components) always beats
    # the short-prompt prior; it does not just compete with it on points.
    hard_hits = _word_boundary_hits(low, _HARD_KEYWORDS)
    refactor_across = _refactor_across_signal(low)
    named = _named_components(stripped)
    multi_component = len(named) >= 2
    has_hard_signal = bool(hard_hits) or refactor_across or multi_component

    if hard_hits:
        score += 4
        reasons.append("difficulty language ({})".format(", ".join(hard_hits)))
    if refactor_across:
        score += 3
        reasons.append('refactor spanning multiple components ("refactor" ... "across")')
    if multi_component:
        score += 2
        reasons.append("names {} components ({})".format(len(named), ", ".join(named[:4])))

    if wc == 0:
        score -= 1
        reasons.append("empty prompt")
    elif wc <= 10 and not has_hard_signal:
        score -= 1
        reasons.append("short prompt ({} words)".format(wc))
    if wc >= 45:
        score += 2
        reasons.append("long prompt ({} words)".format(wc))

    easy_hits = _word_boundary_hits(low, _EASY_KEYWORDS)
    if easy_hits and not has_hard_signal:
        score -= 1
        reasons.append("mechanical-edit language ({})".format(", ".join(easy_hits)))

    if _FILE_LOCATION_RE.search(stripped):
        score -= 1
        reasons.append("names a specific file and location")

    if stripped.endswith("?") and not has_hard_signal:
        score += 1
        reasons.append("phrased as an open question")

    turn_index = signals.get("turn_index")
    if turn_index is None:
        turn_index = len(history)
    if turn_index >= 4:
        score += 1
        reasons.append("turn {} of a long-running conversation".format(turn_index))

    recent = history[-2:]
    prev_failed = bool(signals.get("previous_failed")) or any(
        h.get("failed") for h in recent if isinstance(h, dict))
    if prev_failed:
        score += 4
        reasons.append("a previous turn in this task failed")

    retried = bool(signals.get("retried"))
    if retried:
        score += 3
        reasons.append("this turn is being retried after the last attempt")

    malformed = bool(signals.get("malformed_tool_call")) or any(
        h.get("malformed_tool_call") for h in recent if isinstance(h, dict))
    if malformed:
        score += 4
        reasons.append("a previous turn produced a malformed tool call")

    tools_used = set(_tools_list(signals.get("tools_used")))
    distinct_files = signals.get("distinct_files")

    search_then_edit = bool(tools_used & _SEARCH_TOOLS) and bool(tools_used & _EDIT_TOOLS)
    if search_then_edit:
        score += 2
        reasons.append("a search-then-edit tool sequence")

    if isinstance(distinct_files, int) and distinct_files >= 3:
        score += 2
        reasons.append("touches {} files".format(distinct_files))

    single_known_edit = (
        bool(tools_used) and tools_used <= _EDIT_TOOLS and len(tools_used) <= 1
        and not search_then_edit
        and (distinct_files is None or distinct_files <= 1)
    )
    if single_known_edit:
        score -= 1
        reasons.append("a single mechanical edit against a known file")

    if not reasons:
        reasons.append("no strong signal either way; defaulted to the middle tier")

    if score <= -2:
        tier = TIER_SMALL
    elif score >= 3:
        tier = TIER_LARGE
    else:
        tier = TIER_MEDIUM

    return {"tier": tier, "reason": "; ".join(reasons), "score": score}


def escalate(current_tier, reason=""):
    """The next tier up from current_tier, one step at a time, capped at
    TIER_LARGE. This is the router's designed recovery for a weakness of
    small local models: a malformed tool call, an outright failure, or a
    retried turn all mean "this tier was not enough", and the fix is to
    move up exactly one tier and try again - never straight to the top
    (that overcorrects on a single bad turn) and never staying put (that
    repeats a failure that has already been observed once).

    reason is accepted for the caller's logging/audit trail (route() folds
    it into the reason string it returns) but does not change the outcome:
    escalate() does not grade reasons, it trusts the caller already decided
    escalation was warranted before calling it.

    An unrecognized current_tier is treated as "start from the bottom" -
    the safe default given this module's bias (see the module docstring)
    toward starting cheap. Already at TIER_LARGE stays at TIER_LARGE: there
    is nowhere higher to go, and that fact is surfaced to the caller
    (route() says so explicitly) rather than silently swallowed.
    """
    if current_tier not in TIER_ORDER:
        return TIER_ORDER[0]
    idx = TIER_ORDER.index(current_tier)
    if idx >= len(TIER_ORDER) - 1:
        return current_tier
    return TIER_ORDER[idx + 1]


def _tier_for_params(params_b):
    for tier in TIER_ORDER:
        lo, hi = TIER_PARAM_RANGE[tier]
        if params_b >= lo and (hi is None or params_b < hi):
            return tier
    return TIER_LARGE


def _models_for_tier(tier, catalog=None):
    """hearth_shop CATALOG coding entries whose params_b falls in tier's
    range, cheapest first. Tiers are defined against the catalog, not a
    hardcoded model list, so a catalog change (a new size added, an old one
    dropped) reshapes the tiers automatically instead of needing a second
    place to edit."""
    catalog = hearth_shop.CATALOG if catalog is None else catalog
    lo, hi = TIER_PARAM_RANGE[tier]
    out = [
        m for m in catalog
        if m.get("focus") == "coding" and m.get("params_b", 0) >= lo
        and (hi is None or m.get("params_b", 0) < hi)
    ]
    out.sort(key=lambda m: m["params_b"])
    return out


def _match_installed(model_id, available_models):
    """Match a catalog model id against installed model names (e.g. from
    Ollama's GET /api/tags), exact match first, then by the part before the
    colon on both sides, so a catalog id like
    'qwen2.5-coder:7b-instruct-q4_K_M' also matches a locally retagged
    'qwen2.5-coder:latest' or a bare 'qwen2.5-coder'. Mirrors
    hearth_pull._find_tag_entry's approach, applied to a plain name list
    instead of an /api/tags response. Returns the matched installed name, or
    None when nothing in available_models corresponds to this catalog entry
    - this module only routes to models actually present on the machine."""
    if not available_models:
        return None
    if model_id in available_models:
        return model_id
    base = model_id.split(":", 1)[0]
    for name in available_models:
        if str(name).split(":", 1)[0] == base:
            return name
    return None


def _tier_for_installed_model(model_name, catalog=None):
    """Which tier a currently-loaded model name belongs to, matched against
    the catalog the same way _match_installed does (exact, then base name).
    None when the model is not a recognized catalog entry at all (e.g. the
    user pulled something outside Hearth's curated coding catalog) -
    callers must treat that as "unknown", never guess a tier for it."""
    catalog = hearth_shop.CATALOG if catalog is None else catalog
    if not model_name:
        return None
    for m in catalog:
        if m["id"] == model_name:
            return _tier_for_params(m["params_b"])
    base = str(model_name).split(":", 1)[0]
    for m in catalog:
        if m["id"].split(":", 1)[0] == base:
            return _tier_for_params(m["params_b"])
    return None


def _viable_models_for_tier(tier, available_models, hw, context_tokens,
                            catalog=None, allow_spillover=False):
    """Installed candidates for tier that this hw can actually run, best
    verdict first and cheapest-among-equals second. hw=None skips the fit
    check entirely (every installed candidate in the tier is returned,
    cheapest first) - callers with no hardware reading get "was it
    installed" as the only gate, which is honest: there is nothing more to
    check without one.

    Always excludes VERDICT_WONT_FIT (this module's hard constraint: never
    route to a model the machine cannot run). Excludes VERDICT_CPU_SPILLOVER
    unless allow_spillover=True: local inference's cost shape is latency,
    not tokens (see the module docstring), and a model that only runs by
    spilling onto the CPU defeats the point of picking a tier for speed.
    route() tries the fast tiers first and only reaches for
    allow_spillover=True as a last resort when nothing fast fits anywhere.
    """
    out = []
    for entry in _models_for_tier(tier, catalog=catalog):
        installed_name = _match_installed(entry["id"], available_models)
        if not installed_name:
            continue
        verdict_dict = None
        rank = 0
        if hw is not None:
            verdict_dict = hearth_shop.verdict_for(entry, hw, context_tokens=context_tokens)
            v = verdict_dict["verdict"]
            if v == hearth_shop.VERDICT_WONT_FIT:
                continue
            if v == hearth_shop.VERDICT_CPU_SPILLOVER and not allow_spillover:
                continue
            rank = hearth_shop.VERDICT_RANK[v]
        out.append({
            "catalog_id": entry["id"],
            "installed_name": installed_name,
            "params_b": entry["params_b"],
            "verdict": verdict_dict,
            "_rank": rank,
        })
    out.sort(key=lambda c: (c["_rank"], c["params_b"]))
    return out


def _find_viable(from_idx, available_models, hw, context_tokens, catalog, allow_spillover):
    """Walk tiers downward from from_idx (the desired tier) toward
    TIER_SMALL, returning the first tier with any installed+runnable
    candidate. Only ever steps DOWN, never up: if the machine cannot run the
    tier a task wants, the fix is a cheaper model, not a bigger one. Returns
    (tier_name, candidates), or (None, []) when nothing at or below from_idx
    is both installed and runnable."""
    for i in range(from_idx, -1, -1):
        viable = _viable_models_for_tier(TIER_ORDER[i], available_models, hw,
                                         context_tokens, catalog=catalog,
                                         allow_spillover=allow_spillover)
        if viable:
            return TIER_ORDER[i], viable
    return None, []


def route(prompt, available_models, hw=None, history=None, signals=None,
         model=None, current_model=None, context_tokens=None, catalog=None):
    """Pick a concrete, installed, hardware-appropriate model for this turn.

    model: an explicit caller-supplied model name (anything other than None,
        "", or "auto"). Always wins outright - no classification, no
        installed/hw check - this module's absolute override, per "the
        router should decide, but the caller can always overrule it".
    available_models: model names actually present on this machine (e.g.
        from Ollama's GET /api/tags). The router never invents a model that
        is not installed.
    hw: a hearth_hw.probe()-shaped dict, or None to defer to
        hearth_hw.probe() (real local detection, no network - see
        hearth_hw's own module docstring). This module's self-test always
        passes a synthetic hw dict; it never calls probe().
    history / signals: forwarded to classify() unchanged; signals'
        malformed_tool_call / previous_failed / retried additionally drive
        the explicit escalation floor here (see below), on top of already
        nudging classify()'s own score.
    current_model: the model presently loaded, if any - drives stickiness.
    context_tokens: forwarded to hearth_shop.verdict_for; None defers to
        hearth_shop.DEFAULT_CONTEXT_TOKENS.

    Returns a dict:
      model            - the chosen, installed model name, or None when
                          nothing installed on this machine can run any
                          tier at all (an honest "nothing fits" rather than
                          inventing an answer).
      tier             - the tier actually landed on (may be cheaper than
                          classify()'s pick - see hardware_limited).
      reason           - human-readable explanation, meant to be shown to a
                          user ("routed to the 14B because the previous
                          turn produced a malformed tool call"). Silent
                          routing is indistinguishable from a bug.
      escalated        - True when an escalation-eligible signal
                          (malformed tool call / previous failure / retry)
                          was present and there was a tier above the
                          currently loaded model's tier to move to.
      hardware_limited - True when the tier actually used is cheaper than
                          what classify() asked for, because nothing
                          installed at the requested tier (or above the
                          landed tier, within the fallback search) fits
                          this machine - e.g. a 6GB card where the large
                          tier never has a fast-fitting candidate, so even
                          a "this needs the 14B" turn lands on the 7B
                          instead.
    """
    signals = dict(signals) if signals else {}
    available_models = [str(m) for m in available_models] if available_models else []
    context_tokens = context_tokens if context_tokens is not None else hearth_shop.DEFAULT_CONTEXT_TOKENS
    hw_arg = hw  # keep None (unknown hardware) distinguishable from a real probe

    if model and str(model).strip().lower() not in ("", "auto"):
        return {"model": model, "tier": None,
                "reason": "explicit model override; router not consulted",
                "escalated": False, "hardware_limited": False}

    base = classify(prompt, history=history, signals=signals)
    tier = base["tier"]
    reason = base["reason"]
    escalated = False

    current_tier = _tier_for_installed_model(current_model, catalog=catalog)

    trigger = None
    if signals.get("malformed_tool_call"):
        trigger = "the previous turn produced a malformed tool call"
    elif signals.get("previous_failed"):
        trigger = "the previous turn failed"
    elif signals.get("retried"):
        trigger = "this turn is being retried after the last attempt"

    if trigger and current_tier is not None:
        bumped = escalate(current_tier, trigger)
        bumped_idx = TIER_ORDER.index(bumped)
        cur_idx = TIER_ORDER.index(current_tier)
        if bumped_idx > cur_idx:
            escalated = True
            if bumped_idx > TIER_ORDER.index(tier):
                tier = bumped
            reason += "; escalated at least to the {} tier (from the currently loaded {}) because {}".format(
                tier, current_tier, trigger)
        else:
            reason += "; would escalate because {}, but {} is already the top tier".format(
                trigger, current_tier)

    desired_idx = TIER_ORDER.index(tier)
    landed_tier, candidates = _find_viable(desired_idx, available_models, hw_arg,
                                           context_tokens, catalog, allow_spillover=False)
    spillover_used = False
    if not candidates:
        landed_tier, candidates = _find_viable(desired_idx, available_models, hw_arg,
                                               context_tokens, catalog, allow_spillover=True)
        spillover_used = bool(candidates)

    if not candidates:
        return {"model": None, "tier": tier,
                "reason": reason + "; no installed model fits this machine at "
                "the {} tier or below".format(tier),
                "escalated": escalated, "hardware_limited": True}

    hardware_limited = landed_tier != tier
    if hardware_limited:
        reason += ("; nothing installed fits the {} tier on this hardware"
                   "{}, fell back to {}").format(
            tier, " without CPU spillover" if spillover_used else "", landed_tier)
    elif spillover_used:
        reason += "; only fits by spilling onto the CPU, will be noticeably slower"

    # -- stickiness: do not evict a loaded model that already covers this --
    # turn's landed tier, just because a fresh classification came out
    # slightly different. Only an actual escalation signal, or the loaded
    # model no longer being viable at all, should trigger a switch - see
    # the module docstring on why swapping is not free.
    if current_model and not escalated and current_tier is not None:
        if TIER_ORDER.index(current_tier) >= TIER_ORDER.index(landed_tier):
            current_viable = _viable_models_for_tier(
                current_tier, [current_model], hw_arg, context_tokens,
                catalog=catalog, allow_spillover=True)
            if current_viable:
                kept = current_viable[0]
                return {
                    "model": kept["installed_name"], "tier": current_tier,
                    "reason": (
                        "kept {} loaded: it already covers this turn's estimated "
                        "difficulty ({} tier), and switching models costs seconds "
                        "of reload time for no benefit here"
                    ).format(kept["installed_name"], landed_tier),
                    "escalated": False, "hardware_limited": False,
                }

    picked = candidates[0]
    return {
        "model": picked["installed_name"], "tier": landed_tier, "reason": reason,
        "escalated": escalated, "hardware_limited": hardware_limited,
    }


def _self_test_task_aware():
    """Self-test for classify/escalate/route - the task-aware extension.
    Kept separate from the original _self_test() so the two suites read as
    what they are: the original declarative router untouched, and this
    module's new behaviour proven independently. Called from _self_test()
    so `--self-test` still runs everything in one pass. Uses only synthetic
    model lists and synthetic hardware dicts, per this module's own
    constraint: no Ollama, no GPU, no network required to pass."""

    def _hw(vram_bytes, ram_bytes, vendor="nvidia", approximate=False):
        gpus = [{"name": "synthetic", "vram_bytes": vram_bytes, "vendor": vendor,
                 "approximate": approximate}] if vram_bytes else []
        return {"platform": "synthetic", "gpus": gpus,
                "system_ram_bytes": ram_bytes, "cpu_count": 8}

    hw_strong = _hw(24 * 1024 ** 3, 64 * 1024 ** 3)    # comfortable 24GB card
    hw_6gb = _hw(6_442_450_944, 32 * 1024 ** 3)         # the project's RTX 2060

    small_id = "qwen2.5-coder:1.5b-instruct-q4_K_M"
    medium_id = "qwen2.5-coder:7b-instruct-q4_K_M"
    large_id = "qwen2.5-coder:14b-instruct-q4_K_M"
    all_ids = [small_id, medium_id, large_id,
              "deepseek-coder:6.7b-instruct-q4_K_M",
              "codellama:13b-instruct-q4_K_M"]

    # -- classify(): tier assignment from prompt content --------------------
    trivial = classify("Rename `data` to `payload` in utils.py:42")
    assert trivial["tier"] == TIER_SMALL, trivial
    assert "specific file" in trivial["reason"], trivial

    hard = classify(
        "Why do the auth and session tests fail intermittently across modules?",
        signals={"tools_used": ["search_files", "edit_file", "edit_file"],
                "distinct_files": 4})
    assert hard["tier"] == TIER_LARGE, hard
    assert "difficulty language" in hard["reason"], hard
    assert "search-then-edit" in hard["reason"], hard

    neutral = classify(
        "Add a new settings page that lets users toggle between light and "
        "dark theme across the application.")
    assert neutral["tier"] == TIER_MEDIUM, neutral

    # -- regression fixtures: a difficulty term must beat the short-prompt --
    # prior. This prompt used to score "medium" ("short prompt (10 words)")
    # because "refactor" and "across" are not adjacent, so the old literal
    # "refactor across" phrase match never fired and the difficulty term
    # never showed up in the reason at all - length won by default.
    cross_module_refactor = classify(
        "refactor the permission engine across hearth_loop, hearth_tools and the sidecar")
    assert cross_module_refactor["tier"] == TIER_LARGE, cross_module_refactor
    assert "short prompt" not in cross_module_refactor["reason"], cross_module_refactor
    assert "refactor" in cross_module_refactor["reason"].lower(), cross_module_refactor

    # -- regression fixture: mechanical work beyond "rename" also lands -----
    # small. This used to read as neither hard nor mechanical (the easy
    # keyword list only caught "rename") and default to medium.
    add_import = classify("add the missing import for json in scripts/foo.py")
    assert add_import["tier"] == TIER_SMALL, add_import

    # "fix the bug" must stay ambiguous, not get swept into "mechanical" by
    # a too-broad easy-keyword list - ambiguous difficulty must not be
    # pre-judged easy.
    fix_bug = classify("fix the bug")
    assert "mechanical-edit language" not in fix_bug["reason"], fix_bug

    # A failed-previous-turn signal must visibly raise the score versus the
    # same prompt with no such signal, and classify() must never crash on
    # signal-only, content-free input.
    plain = classify("do the thing")
    failed_before = classify("do the thing", signals={"previous_failed": True})
    assert failed_before["score"] > plain["score"], (plain, failed_before)
    assert "previous turn in this task failed" in failed_before["reason"], failed_before

    # -- escalate(): exactly one tier at a time, saturating at the top ------
    assert escalate(TIER_SMALL) == TIER_MEDIUM
    assert escalate(TIER_MEDIUM) == TIER_LARGE
    assert escalate(TIER_LARGE) == TIER_LARGE
    assert escalate("not-a-real-tier") == TIER_SMALL
    # Every non-top tier must escalate to exactly the next tier in
    # TIER_ORDER - not stand still, not skip one. This is the exact
    # guarantee route() leans on to convert a small model's malformed tool
    # call into a bump to the next tier rather than a repeat of the same
    # failure.
    for i, t in enumerate(TIER_ORDER[:-1]):
        nxt = escalate(t)
        assert TIER_ORDER.index(nxt) == i + 1, (
            "escalate() must move exactly one tier up from {!r}, got {!r}".format(t, nxt)
        )

    # -- route(): explicit override always wins, no classification at all ---
    r = route("anything at all", [], model="some-custom:model")
    assert r == {"model": "some-custom:model", "tier": None,
                "reason": "explicit model override; router not consulted",
                "escalated": False, "hardware_limited": False}, r

    # -- route(): a trivial, mechanical edit lands on the small tier --------
    r = route("Rename `data` to `payload` in utils.py:42", all_ids, hw=hw_strong)
    assert r["tier"] == TIER_SMALL, r
    assert r["model"] == small_id, r
    assert r["escalated"] is False and r["hardware_limited"] is False, r

    # -- route(): a hard, multi-file debugging turn lands on the large tier -
    r = route(
        "Why do the auth and session tests fail intermittently across modules?",
        all_ids, hw=hw_strong,
        signals={"tools_used": ["search_files", "edit_file", "edit_file"],
                "distinct_files": 4})
    assert r["tier"] == TIER_LARGE, r
    assert r["model"] in (large_id, "codellama:13b-instruct-q4_K_M"), r
    assert r["hardware_limited"] is False, r

    # -- route(): regression fixtures for the two coordinator-reported ------
    # misclassifications - a cross-module refactor must not get routed to
    # the small model just because it is short, and mechanical import-add
    # work must not default to medium.
    r = route(
        "refactor the permission engine across hearth_loop, hearth_tools and the sidecar",
        all_ids, hw=hw_strong)
    assert r["tier"] == TIER_LARGE, r
    assert r["model"] in (large_id, "codellama:13b-instruct-q4_K_M"), r

    r = route("add the missing import for json in scripts/foo.py", all_ids, hw=hw_strong)
    assert r["tier"] == TIER_SMALL, r
    assert r["model"] == small_id, r

    # -- route(): a 6GB card never has a fast-fitting large-tier candidate, -
    # so even a large-difficulty turn is honestly capped at medium. This
    # matches hearth_shop's own calibration for the project's Linux host
    # (an RTX 2060) - see hearth_shop._self_test's recommend() checks.
    r = route(
        "Why do the auth and session tests fail intermittently across modules?",
        all_ids, hw=hw_6gb,
        signals={"tools_used": ["search_files", "edit_file", "edit_file"],
                "distinct_files": 4})
    assert r["tier"] == TIER_MEDIUM, r
    assert r["model"] == medium_id, r
    assert r["hardware_limited"] is True, r
    assert "fell back to medium" in r["reason"], r

    # -- route(): a malformed tool call escalates off the currently loaded --
    # small model, and the reason string names the cause - "silent routing
    # is indistinguishable from a bug".
    r = route("fix this", all_ids, hw=hw_strong, current_model=small_id,
              signals={"malformed_tool_call": True})
    assert r["escalated"] is True, r
    assert TIER_ORDER.index(r["tier"]) > TIER_ORDER.index(TIER_SMALL), r
    assert "malformed tool call" in r["reason"], r
    assert r["model"] != small_id, (
        "an escalation signal must actually change the model, not just the "
        "label", r,
    )

    # -- route(): stickiness - a trivial follow-up must NOT evict a capable -
    # already-loaded large model just because this turn scores lower. No
    # escalation signal fired, so nothing should switch.
    r = route("Rename `data` to `payload` in utils.py:42", all_ids, hw=hw_strong,
              current_model=large_id)
    assert r["model"] == large_id, r
    assert "kept" in r["reason"].lower(), r
    assert r["escalated"] is False, r

    # -- route(): nothing installed at all is an honest "no model", not a --
    # crash and not a fabricated answer.
    r = route("do anything", [], hw=hw_strong)
    assert r["model"] is None, r
    assert r["hardware_limited"] is True, r

    print("hearth-router task-aware self-test OK")


def _self_test():
    import tempfile
    d = tempfile.mkdtemp(prefix="hearth-router-")
    cfg = os.path.join(d, "router.json")
    with open(cfg, "w") as fh:
        json.dump({
            "default": "llama3.2:3b",
            "rules": [
                {"name": "code",
                 "any_keywords": ["code", "refactor", "python", "bug",
                                  "compile", "function"],
                 "tools_any": ["edit_file", "replace_in_files"],
                 "model": "qwen2.5-coder:latest"},
                {"name": "research",
                 "any_keywords": ["research", "summarize", "analyze", "compare"],
                 "model": "qwen2.5:7b-instruct"},
                {"name": "bad-no-model",
                 "any_keywords": ["whatever"]},
            ],
        }, fh)

    rules = load_rules(cfg)
    # the malformed entry (no model) is skipped
    assert [r["name"] for r in rules["rules"]] == ["code", "research"], rules
    assert rules["default"] == "llama3.2:3b", rules

    # a code-ish goal picks the coder model by keyword
    assert choose_model("please refactor this Python function", rules=rules) \
        == "qwen2.5-coder:latest"
    # ... and separately by tools_any, even with an unrelated goal
    assert choose_model("do the thing", tools=["read_file", "edit_file"],
                        rules=rules) == "qwen2.5-coder:latest"
    # tools as a comma string is normalized the same way
    assert choose_model("do the thing", tools="read_file,replace_in_files",
                        rules=rules) == "qwen2.5-coder:latest"
    # matching is case-insensitive
    assert choose_model("BIG REFACTOR", rules=rules) == "qwen2.5-coder:latest"

    # a research goal picks the instruct model
    assert choose_model("summarize and compare these papers", rules=rules) \
        == "qwen2.5:7b-instruct"

    # an unmatched goal falls back to the declared default
    assert choose_model("say hello", rules=rules) == "llama3.2:3b"

    # first matching rule wins (code before research)
    assert choose_model("research this code", rules=rules) \
        == "qwen2.5-coder:latest"

    # explain() reports the matched rule and the default path
    e = explain("refactor the parser", None, rules)
    assert e["chosen"] == "qwen2.5-coder:latest" and e["matched_rule"] == "code", e
    e2 = explain("say hello", None, rules)
    assert e2["chosen"] == "llama3.2:3b" and e2["matched_rule"] is None, e2

    # an empty ruleset: no default, so choose_model returns the caller's fallback
    empty = {"default": "", "rules": []}
    assert choose_model("anything", rules=empty, fallback="mistral:7b") \
        == "mistral:7b"
    # ... and "" when there is no fallback either
    assert choose_model("anything", rules=empty) == ""
    e3 = explain("anything", None, empty)
    assert e3["chosen"] == "" and e3["matched_rule"] is None, e3

    # a missing config file is a safe no-op empty ruleset
    missing = os.path.join(d, "does-not-exist.json")
    assert load_rules(missing) == {"default": "", "rules": []}

    # HEARTH_ROUTER pointing at a missing file: still a safe no-op
    saved = os.environ.pop("HEARTH_ROUTER", None)
    try:
        os.environ["HEARTH_ROUTER"] = missing
        assert load_rules() == {"default": "", "rules": []}
        # with no rules and no default, choose_model returns the fallback
        assert choose_model("hi", rules=load_rules(), fallback="llama3.2:3b") \
            == "llama3.2:3b"
        # invalid JSON also reads as empty
        badf = os.path.join(d, "bad.json")
        with open(badf, "w") as fh:
            fh.write("{not json")
        assert load_rules(badf) == {"default": "", "rules": []}
        # a non-dict top level reads as empty
        listf = os.path.join(d, "list.json")
        with open(listf, "w") as fh:
            json.dump(["nope"], fh)
        assert load_rules(listf) == {"default": "", "rules": []}
    finally:
        if saved is None:
            os.environ.pop("HEARTH_ROUTER", None)
        else:
            os.environ["HEARTH_ROUTER"] = saved

    _self_test_task_aware()

    print("hearth-router self-test OK")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="hearth-router")
    p.add_argument("--rules", default=DEFAULT_RULES,
                   help="path to the rules JSON")
    p.add_argument("--self-test", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    pc = sub.add_parser("choose", help="print the model chosen for a goal")
    pc.add_argument("--goal", default="")
    pc.add_argument("--tools", default="")
    pc.add_argument("--fallback", default="")

    sub.add_parser("list", help="print the loaded rules")

    pr = sub.add_parser("route", help="print the task-aware routing decision")
    pr.add_argument("--prompt", default="")
    pr.add_argument("--available", default="",
                    help="comma-separated installed model names")
    pr.add_argument("--current-model", default=None)
    pr.add_argument("--context-tokens", type=int, default=None)

    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()

    rules = load_rules(a.rules)
    if a.cmd == "choose":
        print(choose_model(a.goal, tools=a.tools, rules=rules,
                           fallback=a.fallback or None))
        return 0
    if a.cmd == "list":
        print("default: {}".format(rules["default"] or "(none)"))
        for r in rules["rules"]:
            print("{}  keywords={}  tools_any={}  model={}".format(
                r.get("name"), r.get("any_keywords") or [],
                r.get("tools_any") or [], r.get("model")))
        return 0
    if a.cmd == "route":
        result = route(a.prompt, _tools_list(a.available),
                       current_model=a.current_model,
                       context_tokens=a.context_tokens)
        print(json.dumps(result, indent=2))
        return 0
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
