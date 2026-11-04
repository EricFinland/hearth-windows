#!/usr/bin/env python3
"""hearth jsonish: recover structured data from what a small model emits.

A 7B model asked for JSON rarely returns JSON. It returns JSON wrapped in a
markdown code fence, or with a polite sentence in front of it, or with a
trailing comma, or with single-quoted keys, or with Python's True/False/None,
or with a `// comment` explaining a field, or with two objects where one was
asked for, or with the closing brace missing because it hit the token limit.
`json.loads` says no to every one of those, and the agent loop then burns a
whole extra round trip asking the model to try again. On a local model that
round trip is seconds of wall clock and a fresh chance to answer differently.

This module recovers what the model meant, as conservatively as it can:

  extract(text, want="object")  first JSON value of the wanted shape
  extract_all(text)             every JSON value found, in order
  loads(text, strict_first=True)  json.loads unchanged first, repairs after

All three return a `(value, repairs)` pair. `repairs` names each fix that had
to be applied, so a caller can log that the model needed help instead of
silently accepting a guess, and can tighten its prompt when one repair keeps
showing up. Valid JSON comes back with an empty `repairs` list and is never
rewritten. An unrecoverable input comes back as `(None, ["failed: ..."])`,
with the reason in the list, never as a plausible wrong value.

What it deliberately does NOT do:

  * It does not guess at missing data. No invented keys, no default values,
    no type coercion, no schema filling. A repair only ever removes noise or
    restores punctuation the model clearly intended.
  * It does not recover a truncated string literal. `{"a": "unfinis` could
    have ended in any number of ways, so it is refused rather than closed.
    Missing *closers* at the very end are restorable; missing *content* is
    not, and the difference is the whole safety argument of this module.
  * It does not use a regex to find the JSON span. A regex cannot tell a
    brace inside a string literal from a real one, so `{"a": "}"}` would be
    cut in half. The span scan here is a real character walk that tracks
    string literals and backslash escapes.
  * It does not convert single quotes when the result would be ambiguous. If
    a single-quoted span contains a double quote or a backslash, every span
    in the text is left alone.
  * It does not apply a repair to text that did not need it. Each repair
    reports whether it changed anything, and only a repair that changed
    something is recorded.
  * It does not talk to the model, the network, or the disk (outside the
    CLI reading a file you name), and it depends on nothing but the standard
    library. It is not a JSON5, YAML, or Python-literal parser.

Standard library only. `--self-test` runs fully offline and deterministically.
"""

import argparse
import json
import sys

# Repair names. These are stable strings: they end up in operator logs and in
# counters that decide whether a prompt needs tightening.
REP_FENCE = "strip-code-fence"
REP_SLICE = "slice-balanced"
REP_COMMENT = "strip-comments"
REP_PYLIT = "python-literals"
REP_SQUOTE = "single-to-double-quotes"
REP_KEYS = "quote-bare-keys"
REP_COMMA = "drop-trailing-comma"
REP_CLOSE = "close-truncated"

FAIL = "failed: "
WANTS = ("object", "array", "value")

_PY_LITERALS = {"True": "true", "False": "false", "None": "null"}
_KEY_CHARS = "_$"
_MAX_LANG_TAG = 24


def failed(repairs):
    """True when a `(value, repairs)` pair reports a refusal rather than a
    recovery. Cheaper for a caller than re-deriving it from `value is None`,
    because a legitimately recovered JSON `null` is also None."""
    return any(r.startswith(FAIL) for r in repairs)


def _try_loads(text):
    """json.loads with no repairs. Returns (ok, value)."""
    try:
        return True, json.loads(text)
    except Exception:  # noqa: BLE001 - any decode problem means "not json"
        return False, None


def _scan(text, start=0, stop_when_closed=True):
    """Walk `text` from `start`, tracking string literals and backslash
    escapes, so that braces and brackets inside a string are ignored. Both
    quote characters open a string, because a model that single-quotes its
    keys will also single-quote a value containing a brace.

    Returns (index_after, opens, in_string) where `opens` is the stack of
    containers still unclosed and `in_string` says the text ran out inside a
    string literal. With stop_when_closed the walk stops as soon as the first
    container it opened is balanced, which is how the outermost span of a
    value is found."""
    opens = []
    quote = None
    esc = False
    seen = False
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if quote is not None:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = None
        elif ch == '"' or ch == "'":
            quote = ch
        elif ch == "{" or ch == "[":
            opens.append(ch)
            seen = True
        elif ch == "}" or ch == "]":
            if opens:
                opens.pop()
            if seen and not opens and stop_when_closed:
                return i + 1, opens, False
        i += 1
    return n, opens, quote is not None


def _is_lang_tag(head):
    """True when the first line of a fenced block looks like a language tag
    (`json`, `jsonc`, `js`) rather than the start of the payload."""
    if len(head) > _MAX_LANG_TAG:
        return False
    for ch in head:
        if not (ch.isalnum() or ch in "-_+.#"):
            return False
    return True


def _drop_lang_tag(body):
    """Remove the ```<tag> line from a fenced block body, if that is what the
    first line is. A first line that already looks like payload is kept."""
    nl = body.find("\n")
    if nl == -1:
        return body
    head = body[:nl].strip()
    if head and not _is_lang_tag(head):
        return body
    return body[nl + 1:]


def strip_code_fences(text):
    """Return (body, changed) with markdown code fences removed.

    The first non-blank fenced block wins, with or without a language tag. A
    lone closing fence with nothing useful after it (the model fenced its
    answer and the opening fence was lost) falls back to the text before it,
    so the payload is never thrown away by the fence step itself."""
    if "```" not in text:
        return text, False
    parts = text.split("```")
    for idx in range(1, len(parts), 2):
        body = _drop_lang_tag(parts[idx])
        if body.strip():
            return body, True
    if parts[0].strip():
        return parts[0], True
    return text, False


def strip_comments(s):
    """Remove `//` line comments and `/* */` block comments that sit outside
    string literals. Returns (text, changed). A `//` inside a string, such as
    a URL, is left alone."""
    out = []
    i = 0
    n = len(s)
    quote = None
    esc = False
    changed = False
    while i < n:
        ch = s[i]
        if quote is not None:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch == '"' or ch == "'":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and s[i + 1] == "/":
            nl = s.find("\n", i)
            i = n if nl == -1 else nl
            changed = True
            continue
        if ch == "/" and i + 1 < n and s[i + 1] == "*":
            end = s.find("*/", i + 2)
            i = n if end == -1 else end + 2
            out.append(" ")
            changed = True
            continue
        out.append(ch)
        i += 1
    return "".join(out), changed


def python_literals(s):
    """Replace bare `True`, `False` and `None` tokens with their JSON
    spellings. Returns (text, changed). Whole words only, and never inside a
    string literal, so `{"note": "True story"}` is untouched."""
    out = []
    i = 0
    n = len(s)
    quote = None
    esc = False
    changed = False
    while i < n:
        ch = s[i]
        if quote is not None:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch == '"' or ch == "'":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (s[j].isalnum() or s[j] == "_"):
                j += 1
            word = s[i:j]
            if word in _PY_LITERALS:
                out.append(_PY_LITERALS[word])
                changed = True
            else:
                out.append(word)
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out), changed


def single_to_double(s):
    """Convert single-quoted keys and strings to double-quoted ones, but only
    when every single-quoted span in the text is unambiguous. Returns
    (text, changed).

    A span is ambiguous when it contains a double quote or a backslash: the
    rewrite would then have to decide what to escape, and a wrong guess
    produces a value that parses but says something the model did not. One
    ambiguous span blocks the repair for the whole text, because a partial
    conversion is worse than none."""
    spans = []
    start = -1
    i = 0
    n = len(s)
    quote = None
    esc = False
    while i < n:
        ch = s[i]
        if quote == '"':
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                quote = None
        elif quote == "'":
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == "'":
                quote = None
                spans.append((start, i + 1))
        elif ch == '"':
            quote = '"'
        elif ch == "'":
            quote = "'"
            start = i
        i += 1
    if quote is not None or not spans:
        # Unterminated quoting, or nothing single-quoted to convert.
        return s, False
    for a, b in spans:
        inner = s[a + 1:b - 1]
        if '"' in inner or "\\" in inner:
            return s, False
    out = []
    prev = 0
    for a, b in spans:
        out.append(s[prev:a])
        out.append('"' + s[a + 1:b - 1] + '"')
        prev = b
    out.append(s[prev:])
    return "".join(out), True


def quote_bare_keys(s):
    """Quote unquoted object keys: `{a: 1}` becomes `{"a": 1}`. Returns
    (text, changed). Only in key position inside an object, which is tracked
    with a real container stack, so an array element or a bare value is never
    quoted."""
    out = []
    i = 0
    n = len(s)
    stack = []
    quote = None
    esc = False
    expect_key = False
    changed = False
    while i < n:
        ch = s[i]
        if quote is not None:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch == '"' or ch == "'":
            quote = ch
            out.append(ch)
            expect_key = False
            i += 1
            continue
        if ch == "{" or ch == "[":
            stack.append(ch)
            out.append(ch)
            expect_key = ch == "{"
            i += 1
            continue
        if ch == "}" or ch == "]":
            if stack:
                stack.pop()
            out.append(ch)
            expect_key = False
            i += 1
            continue
        if ch == ",":
            out.append(ch)
            expect_key = bool(stack) and stack[-1] == "{"
            i += 1
            continue
        if ch == ":":
            out.append(ch)
            expect_key = False
            i += 1
            continue
        if ch.isspace():
            out.append(ch)
            i += 1
            continue
        if expect_key and (ch.isalpha() or ch in _KEY_CHARS):
            j = i
            while j < n and (s[j].isalnum() or s[j] in _KEY_CHARS):
                j += 1
            k = j
            while k < n and s[k].isspace():
                k += 1
            if k < n and s[k] == ":":
                out.append('"' + s[i:j] + '"')
                changed = True
            else:
                out.append(s[i:j])
            expect_key = False
            i = j
            continue
        out.append(ch)
        expect_key = False
        i += 1
    return "".join(out), changed


def drop_trailing_commas(s):
    """Remove a comma that is followed only by whitespace and then `}` or
    `]`. Returns (text, changed). One pass, outside string literals, so a
    comma inside a string value survives."""
    out = []
    i = 0
    n = len(s)
    quote = None
    esc = False
    changed = False
    while i < n:
        ch = s[i]
        if quote is not None:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch == '"' or ch == "'":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and s[j].isspace():
                j += 1
            if j < n and (s[j] == "}" or s[j] == "]"):
                changed = True
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out), changed


def close_truncated(s):
    """Append the closers a truncated container is missing. Returns
    (text, changed).

    This is the one repair that adds characters, so it is the most tightly
    fenced: it fires only when the text ends outside a string literal with
    containers still open. Text that ran out mid-string is refused, because
    the missing part was content, not punctuation."""
    _, opens, in_string = _scan(s, 0, False)
    if in_string or not opens:
        return s, False
    tail = []
    for ch in reversed(opens):
        tail.append("}" if ch == "{" else "]")
    return s + "".join(tail), True


# Repair order matters. Comments go first so a comma or brace inside one
# cannot mislead a later pass. Closers go last, and the trailing-comma pass
# repeats after them, because `{"a": 1,` only shows its trailing comma once
# the `}` is back.
_STAGES = (
    (REP_COMMENT, strip_comments),
    (REP_PYLIT, python_literals),
    (REP_SQUOTE, single_to_double),
    (REP_KEYS, quote_bare_keys),
    (REP_COMMA, drop_trailing_commas),
    (REP_CLOSE, close_truncated),
    (REP_COMMA, drop_trailing_commas),
)


def _reason(s):
    """Name why `s` is still not JSON, for the refusal message."""
    _, _opens, in_string = _scan(s, 0, False)
    if in_string:
        return ("unterminated string literal, so the truncated content "
                "cannot be recovered safely")
    try:
        json.loads(s)
    except Exception as exc:  # noqa: BLE001 - message is for the operator
        return "not json after repairs: {}".format(exc)
    return "not json after repairs"


def _repair_candidate(s):
    """Run the repair stages over one candidate span. Returns
    (value, repairs, reason); `reason` is empty on success and `repairs` is
    empty when the span was already valid JSON."""
    ok, value = _try_loads(s)
    if ok:
        return value, [], ""
    applied = []
    for name, stage in _STAGES:
        changed_text, changed = stage(s)
        if not changed:
            continue
        s = changed_text
        if name not in applied:
            applied.append(name)
        ok, value = _try_loads(s)
        if ok:
            return value, applied, ""
    return None, applied, _reason(s)


def _candidates(body):
    """Yield every top-level container span in `body`, in order, as
    (span, sliced, is_container) triples. `sliced` says the span is a cut out
    of a longer text rather than the whole of it.

    When there is no container at all the trimmed body is offered once as a
    non-container candidate, so a bare `42`, `true` or `'hi'` can still be
    recovered."""
    out = []
    i = 0
    n = len(body)
    lo = len(body) - len(body.lstrip())
    hi = len(body.rstrip())
    while i < n:
        if body[i] == "{" or body[i] == "[":
            end, _opens, _in_string = _scan(body, i, True)
            sliced = not (i == lo and end >= hi)
            out.append((body[i:end], sliced, True))
            i = end
            continue
        i += 1
    if not out:
        trimmed = body.strip()
        if trimmed:
            out.append((trimmed, False, False))
    return out


def _wanted(value, want):
    if want == "object":
        return isinstance(value, dict)
    if want == "array":
        return isinstance(value, list)
    return True


def _collect(text, want, limit, strict):
    """Shared engine. Returns (results, reason) where results is a list of
    (value, repairs) pairs and reason is empty when results is non-empty."""
    if want not in WANTS:
        raise ValueError("want must be one of {}, got {!r}".format(WANTS, want))
    if not isinstance(text, str):
        return [], "input is not text"
    if not text.strip():
        return [], "empty input"
    if strict:
        ok, value = _try_loads(text)
        if ok and _wanted(value, want):
            return [(value, [])], ""
    pre = []
    body, fenced = strip_code_fences(text)
    if fenced:
        pre.append(REP_FENCE)
        ok, value = _try_loads(body)
        if ok and _wanted(value, want):
            return [(value, list(pre))], ""
    results = []
    reason = ""
    saw_container = False
    for span, sliced, is_container in _candidates(body):
        if is_container:
            saw_container = True
        value, repairs, why = _repair_candidate(span)
        if why:
            if not reason:
                reason = why
            continue
        if not _wanted(value, want):
            if not reason:
                reason = "found a {} but want={!r}".format(
                    type(value).__name__, want)
            continue
        found = list(pre)
        if sliced:
            found.append(REP_SLICE)
        found.extend(repairs)
        results.append((value, found))
        if limit is not None and len(results) >= limit:
            return results, ""
    if results:
        return results, ""
    if not saw_container:
        return [], "no json value found"
    return [], reason or "no json value found"


def extract(text, want="object"):
    """Return (value, repairs) for the first JSON value in `text` whose shape
    matches `want` ("object", "array" or "value" for anything).

    `want` is a filter, not a preference: with the default, an array is not
    handed back as if it were the object that was asked for. Pass
    want="value" to take whatever the model produced."""
    results, reason = _collect(text, want, 1, True)
    if results:
        return results[0]
    return None, [FAIL + reason]


def extract_all(text, want="value"):
    """Return a list of (value, repairs) pairs for every JSON value found in
    `text`, in the order they appear. Empty when nothing is recoverable; use
    `extract` when a refusal reason is wanted."""
    results, _reason_text = _collect(text, want, None, True)
    return results


def loads(text, strict_first=True):
    """Return (value, repairs) for `text`.

    With strict_first (the default) `json.loads` is tried on the text exactly
    as given before anything else, so well-behaved output costs one parse and
    comes back with an empty `repairs` list. Only then are repairs attempted.
    strict_first=False skips that shortcut; it exists so the repair path can
    be exercised against text that would have parsed anyway."""
    results, reason = _collect(text, "value", 1, strict_first)
    if results:
        return results[0]
    return None, [FAIL + reason]


def _self_test():
    # Valid JSON is parsed, not repaired, and comes back untouched.
    value, repairs = loads('{"a": 1, "b": [2, 3]}')
    assert value == {"a": 1, "b": [2, 3]}, value
    assert repairs == [], repairs
    for good in ("{}", "[]", '{"a": null}', "[1, 2, 3]", '  {"a": 1}  ',
                 '{"a": {"b": {"c": [1, {"d": true}]}}}', "42", "true",
                 '"plain string"'):
        value, repairs = loads(good)
        assert repairs == [], (good, repairs)
        assert value == json.loads(good), good

    # Nested objects survive intact, with no repairs.
    value, repairs = extract('{"a": {"b": {"c": 1}}}')
    assert value == {"a": {"b": {"c": 1}}}, value
    assert repairs == [], repairs

    # A brace inside a string literal must not confuse the balance scan.
    value, repairs = loads('{"a": "}"}')
    assert value == {"a": "}"}, value
    assert repairs == [], repairs
    value, repairs = extract('sure: {"a": "}{", "b": "[\\"]"} done')
    assert value == {"a": "}{", "b": "[\"]"}, value
    assert repairs == [REP_SLICE], repairs
    end, opens, in_string = _scan('{"a": "}"} tail', 0, True)
    assert (end, opens, in_string) == (10, [], False), (end, opens, in_string)

    # Code fences, with and without a language tag, and with prose around.
    value, repairs = loads('```json\n{"a": 1}\n```')
    assert (value, repairs) == ({"a": 1}, [REP_FENCE]), (value, repairs)
    value, repairs = loads('```\n{"a": 1}\n```')
    assert (value, repairs) == ({"a": 1}, [REP_FENCE]), (value, repairs)
    value, repairs = loads(
        'Here you go:\n```json\n{"a": 1}\n```\nHope that helps!')
    assert (value, repairs) == ({"a": 1}, [REP_FENCE]), (value, repairs)
    value, repairs = loads('{"a": 1}\n```')
    assert (value, repairs) == ({"a": 1}, [REP_FENCE]), (value, repairs)

    # A sentence in front of the object.
    value, repairs = loads('Sure! Here is the object: {"a": 1} Let me know.')
    assert (value, repairs) == ({"a": 1}, [REP_SLICE]), (value, repairs)

    # Trailing commas.
    value, repairs = loads('{"a": 1, "b": 2,}')
    assert (value, repairs) == ({"a": 1, "b": 2}, [REP_COMMA]), repairs
    value, repairs = loads("[1, 2, 3, ]")
    assert (value, repairs) == ([1, 2, 3], [REP_COMMA]), (value, repairs)

    # Single quotes, but only when unambiguous.
    value, repairs = loads("{'a': 'b', 'n': 1}")
    assert (value, repairs) == ({"a": "b", "n": 1}, [REP_SQUOTE]), repairs
    value, repairs = loads("{'a': 'he said \"hi\"'}")
    assert value is None, value
    assert failed(repairs), repairs
    assert single_to_double("{'a': 'he said \"hi\"'}")[1] is False

    # Unquoted object keys.
    value, repairs = loads('{a: 1, b_2: "x"}')
    assert (value, repairs) == ({"a": 1, "b_2": "x"}, [REP_KEYS]), repairs

    # Python literals, including one that only looks like a literal.
    value, repairs = loads('{"ok": True, "bad": False, "who": None}')
    assert value == {"ok": True, "bad": False, "who": None}, value
    assert repairs == [REP_PYLIT], repairs
    value, repairs = loads('{"note": "True story", "ok": True}')
    assert value == {"note": "True story", "ok": True}, value
    assert repairs == [REP_PYLIT], repairs

    # Comments, and a slash pair that is not one.
    value, repairs = loads('{\n  // the id\n  "id": 7 /* inline */\n}')
    assert (value, repairs) == ({"id": 7}, [REP_COMMENT]), (value, repairs)
    value, repairs = loads('{"u": "http://x/y"}')
    assert (value, repairs) == ({"u": "http://x/y"}, []), (value, repairs)

    # Truncated containers: closers only.
    value, repairs = loads('{"a": 1, "b": [2, 3')
    assert value == {"a": 1, "b": [2, 3]}, value
    assert repairs == [REP_CLOSE], repairs
    value, repairs = loads('{"a": 1,')
    assert value == {"a": 1}, value
    assert repairs == [REP_CLOSE, REP_COMMA], repairs
    value, repairs = loads('```json\n{"a": [1, 2')
    assert value == {"a": [1, 2]}, value
    assert repairs == [REP_FENCE, REP_CLOSE], repairs

    # A truncated string literal is NOT safely recoverable and is refused.
    value, repairs = loads('{"a": "unfinished')
    assert value is None, value
    assert failed(repairs) and "unterminated" in repairs[0], repairs
    value, repairs = loads('{"items": ["one", "tw')
    assert value is None, value
    assert failed(repairs) and "unterminated" in repairs[0], repairs
    assert close_truncated('{"a": "unfinished')[1] is False

    # Two objects back to back, with and without prose between them.
    pairs = extract_all('{"a": 1}{"b": 2}')
    assert [p[0] for p in pairs] == [{"a": 1}, {"b": 2}], pairs
    pairs = extract_all('first {"a": 1} then {"b": 2} end')
    assert [p[0] for p in pairs] == [{"a": 1}, {"b": 2}], pairs
    assert all(p[1] == [REP_SLICE] for p in pairs), pairs
    value, repairs = extract('{"a": 1}\n{"b": 2}')
    assert value == {"a": 1}, value

    # Empty input, and text with no JSON in it at all.
    for junk in ("", "   ", "\n\n"):
        value, repairs = loads(junk)
        assert value is None, junk
        assert failed(repairs) and "empty input" in repairs[0], repairs
        assert extract_all(junk) == [], junk
    for prose in ("The weather in Dublin is mild today.",
                  "I am not going to answer that.", "no"):
        value, repairs = loads(prose)
        assert value is None, prose
        assert failed(repairs), repairs
        assert "no json value found" in repairs[0], repairs
        assert extract_all(prose) == [], prose

    # Unrecoverable input names a reason instead of inventing a value.
    for bad in ('{"a": 1 "b": 2}', '{"a": }', "{: 1}", '{"a" 1, "b" 2}'):
        value, repairs = loads(bad)
        assert value is None, bad
        assert failed(repairs) and len(repairs) == 1, (bad, repairs)
        assert repairs[0] != FAIL, repairs

    # want is a filter, not a preference.
    value, repairs = extract("[1, 2]", want="object")
    assert value is None and failed(repairs), (value, repairs)
    value, repairs = extract("[1, 2]", want="array")
    assert (value, repairs) == ([1, 2], []), (value, repairs)
    value, repairs = extract("42", want="value")
    assert (value, repairs) == (42, []), (value, repairs)
    try:
        extract("{}", want="banana")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown want must raise")

    # No repair is applied to text that did not need it.
    clean = '{"a": 1, "b": "c, d", "u": "a//b", "q": "it\'s", "t": "True"}'
    assert json.loads(clean)["t"] == "True", clean
    for stage in (strip_comments, python_literals, single_to_double,
                  quote_bare_keys, drop_trailing_commas, close_truncated):
        text, changed = stage(clean)
        assert changed is False, stage.__name__
        assert text == clean, stage.__name__
    value, repairs = loads(clean, strict_first=False)
    assert value == json.loads(clean), value
    assert repairs == [], repairs

    # Everything wrong at once, in the order the stages run.
    messy = ("Sure, here it is:\n```json\n"
             "{\n  // the id\n  'id': 7,\n  name: 'Ada',\n"
             "  ok: True,\n}\n```\nAnything else?")
    value, repairs = loads(messy)
    assert value == {"id": 7, "name": "Ada", "ok": True}, value
    assert repairs == [REP_FENCE, REP_COMMENT, REP_PYLIT, REP_SQUOTE,
                       REP_KEYS, REP_COMMA], repairs

    print("hearth-jsonish self-test OK")
    return 0


def _report(repairs):
    """Name the repairs on stderr, so stdout stays a clean JSON stream that a
    caller can pipe straight into another tool."""
    sys.stdout.flush()
    sys.stderr.write("repairs: {}\n".format(", ".join(repairs) or "none"))


def _read_input(path):
    if path and path != "-":
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return sys.stdin.read()


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="hearth-jsonish",
        description="recover JSON from what a small model actually emitted")
    p.add_argument("path", nargs="?",
                   help="file to read; omit or pass - to read stdin")
    p.add_argument("--want", default="value", choices=WANTS,
                   help="shape to accept (default: value)")
    p.add_argument("--all", action="store_true",
                   help="print every JSON value found, not just the first")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    text = _read_input(a.path)
    if a.all:
        pairs = extract_all(text, want=a.want)
        if not pairs:
            _, repairs = extract(text, want=a.want)
            sys.stderr.write("{}\n".format(repairs[0]))
            return 1
        for value, repairs in pairs:
            print(json.dumps(value, indent=2, sort_keys=True))
            _report(repairs)
        return 0
    value, repairs = extract(text, want=a.want)
    if failed(repairs):
        sys.stderr.write("{}\n".format(repairs[0]))
        return 1
    print(json.dumps(value, indent=2, sort_keys=True))
    _report(repairs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
