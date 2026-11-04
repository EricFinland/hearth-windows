#!/usr/bin/env python3
"""hearth condense: make a huge tool result fit a small context window.

One call, `condense(text, max_chars)`, returns a shortened copy of a tool
result plus a note saying honestly what was thrown away. It exists because
the local model runs with roughly four to eight thousand tokens of room,
and a single tool result routinely swallows all of it: one `run_shell` that
cats a log, one failing test suite, one directory listing of a vendored
node_modules, and the window is gone before the agent gets to think. The
obvious fix, asking the model to summarise the wall of text, costs a round
trip and spends exactly the budget it is trying to save, so the work here
is mechanical, local, free and instant.

Four strategies, picked by content shape in `auto` mode:

  head_tail  Keep the beginning and the end with an explicit elision marker
             between them. A log's cause is near the top and its effect is
             near the bottom; the middle is almost always the same thing
             happening repeatedly.
  errors     When the text looks like build or test output, prefer the lines
             matching error, failed, traceback, exception, warning and the
             lines immediately around them, because in practice that is the
             only part anyone reads.
  unique     When the text is highly repetitive, like a progress log or a
             retry loop, collapse each run of same-shaped lines into
             "<first line of the run> (x N)".
  structure  When the text is JSON, keep the shape and clip long leaf
             strings and long sequences, so the keys survive and the bulk
             does not.

The result is guaranteed to be at most `max_chars` characters including any
elision marker, for every mode and for pathological input: an empty string,
a `max_chars` smaller than the marker itself, one line longer than the whole
budget with no newline anywhere in it, and text already short enough, which
comes back byte for byte with an empty note.

What this deliberately does NOT do:

  * No model calls and no network. Standard library only, no third-party
    imports, so it cannot fail in a way that costs tokens or latency.
  * No semantic summarising, no paraphrasing, no reordering. Every character
    that survives is a character the tool actually printed, in the order it
    printed it. A condensed result is quotable; a summary is not.
  * No tokenizing. The budget is characters, not tokens, because the caller
    may be pointed at any local weights and a character budget is a safe
    under-estimate of a token budget for log-shaped text.
  * No file writing, no caching, no state. Pure function in, pure value out,
    so two identical inputs always condense identically.
  * No guessing about what matters in prose. These heuristics are tuned for
    machine output and will do a poor job on an essay, by design.
"""

import argparse
import json
import re
import sys

LINE_MARKER = "... [{:,} lines elided] ..."
CHAR_MARKER = "... [{:,} chars elided] ..."

# Words that mark the interesting line of a build or test run.
ERROR_WORDS = ("error", "failed", "failure", "traceback", "exception",
               "warning", "fatal", "panic", "assert")
# Lines of context kept on each side of an error-ish line.
ERROR_CONTEXT = 1
# `errors` only wins when the loud lines are a minority of the output; if
# nearly every line matches, matching tells us nothing useful.
ERROR_MAJORITY = 0.5
# `unique` only wins when normalized lines collapse to under this fraction.
REPEAT_RATIO = 0.5
# Below this many lines the shape heuristics are noise, so do not trust them.
MIN_LINES_FOR_SHAPE = 8
# (string clip, sequence clip) pairs tried in order by `structure`, from
# generous to brutal, stopping at the first one that fits the budget.
JSON_STEPS = ((400, 40), (200, 20), (80, 10), (40, 5), (20, 3), (8, 2),
              (4, 1), (2, 0))

_DIGITS = re.compile(r"\d+")
_SPACES = re.compile(r"\s+")


def _join_notes(*notes):
    """Join the non-empty notes with "; " so callers can stack them."""
    return "; ".join([n for n in notes if n])


def _looks_error(line):
    """True when a line carries one of the words people actually grep for."""
    low = line.lower()
    for word in ERROR_WORDS:
        if word in low:
            return True
    return False


def _norm(line):
    """Normalize a line down to its shape for repetition detection: runs of
    digits become "#" and runs of whitespace become one space. That makes
    "downloading chunk 12 of 400" and "downloading chunk 13 of 400" the same
    shape, which is the whole point of the `unique` strategy."""
    return _SPACES.sub(" ", _DIGITS.sub("#", line)).strip()


def _truncate_chars(text, max_chars):
    """Last resort: clip in the middle of a single run of characters. Used
    when not even one whole line fits, including the degenerate case of a
    `max_chars` smaller than the elision marker, where the marker is dropped
    and the head alone is returned."""
    if max_chars <= 0:
        return ""
    total = len(text)
    if total <= max_chars:
        return text
    # The marker states how much was dropped, and its own width changes that
    # number, so settle on a fixed point instead of guessing once.
    guess = total - max_chars
    budget = 0
    for _ in range(4):
        marker = CHAR_MARKER.format(guess)
        budget = max_chars - len(marker)
        if budget < 2:
            return text[:max_chars]
        if total - budget == guess:
            break
        guess = total - budget
    marker = CHAR_MARKER.format(guess)
    budget = max_chars - len(marker)
    if budget < 2:
        return text[:max_chars]
    head = budget - budget // 2
    tail = budget - head
    out = text[:head] + marker
    if tail:
        out += text[total - tail:]
    return out


def _pick_ends(lines, max_chars):
    """Grow a head and a tail alternately while the kept lines plus the
    elision marker still fit, and return (n_head, n_tail). Alternating keeps
    the two ends balanced when the lines are uneven, and an end that stops
    fitting is retired on its own so a single enormous line at one end does
    not cost the other end its budget."""
    total = len(lines)
    head = 0
    tail = 0
    used = 0
    blocked = [False, False]
    turn = 0
    while head + tail < total and not (blocked[0] and blocked[1]):
        if blocked[turn]:
            turn = 1 - turn
            continue
        if turn == 0:
            cand = lines[head]
        else:
            cand = lines[total - 1 - tail]
        marker = LINE_MARKER.format(total - head - tail)
        cost = len(cand) + 1
        if used + cost + len(marker) + 1 > max_chars:
            blocked[turn] = True
        else:
            used += cost
            if turn == 0:
                head += 1
            else:
                tail += 1
        turn = 1 - turn
    return head, tail


def _assemble(lines, groups):
    """Render the kept [start, end) line groups with an elision marker for
    every gap, including a leading or trailing one."""
    total = len(lines)
    parts = []
    prev = 0
    for start, end in groups:
        if start > prev:
            parts.append(LINE_MARKER.format(start - prev))
        parts.extend(lines[start:end])
        prev = end
    if prev < total:
        parts.append(LINE_MARKER.format(total - prev))
    return "\n".join(parts)


def _group(indexes):
    """Turn a sorted list of line numbers into contiguous [start, end) runs."""
    groups = []
    for i in indexes:
        if groups and groups[-1][1] == i:
            groups[-1][1] = i + 1
        else:
            groups.append([i, i + 1])
    return [(g[0], g[1]) for g in groups]


def head_tail(text, max_chars):
    """Keep the beginning and the end, elide the middle."""
    lines = text.split("\n")
    total = len(lines)
    if total < 2:
        out = _truncate_chars(text, max_chars)
        return out, "single line of {:,} chars; kept {:,}".format(
            len(text), len(out))
    head, tail = _pick_ends(lines, max_chars)
    if not head and not tail:
        out = _truncate_chars(text, max_chars)
        return out, ("no whole line fits in {:,} chars; kept {:,} of "
                     "{:,} chars".format(max_chars, len(out), len(text)))
    groups = []
    if head:
        groups.append((0, head))
    if tail:
        groups.append((total - tail, total))
    out = _assemble(lines, groups)
    dropped = total - head - tail
    note = ("kept first {:,} and last {:,} lines of {:,}; dropped "
            "{:,}".format(head, tail, total, dropped))
    return out, note


def errors(text, max_chars):
    """Keep the error-ish lines and their immediate neighbours, in order.
    Falls back to head_tail when nothing matches or nothing fits."""
    lines = text.split("\n")
    total = len(lines)
    hits = [i for i, line in enumerate(lines) if _looks_error(line)]
    if not hits:
        out, note = head_tail(text, max_chars)
        return out, _join_notes("no error-like line found, used head_tail",
                                note)
    reserve = len(LINE_MARKER.format(total)) + 1
    keep = set()
    used = 0
    for i in hits:
        start = max(0, i - ERROR_CONTEXT)
        end = min(total, i + ERROR_CONTEXT + 1)
        fresh = [j for j in range(start, end) if j not in keep]
        cost = 0
        for j in fresh:
            cost += len(lines[j]) + 1
        if used + cost + reserve > max_chars:
            continue
        keep.update(fresh)
        used += cost
    if not keep:
        out, note = head_tail(text, max_chars)
        return out, _join_notes(
            "no error line fits in {:,} chars, used head_tail".format(
                max_chars), note)
    groups = _group(sorted(keep))
    out = _assemble(lines, groups)
    # Each gap costs a marker, and the count of gaps is only known once the
    # groups are chosen, so shed whole groups from the end until it fits.
    while len(out) > max_chars and len(groups) > 1:
        groups.pop()
        out = _assemble(lines, groups)
    kept = 0
    for start, end in groups:
        kept += end - start
    note = ("kept {:,} of {:,} lines around {:,} error match(es); dropped "
            "{:,}".format(kept, total, len(hits), total - kept))
    if len(out) > max_chars:
        out = _truncate_chars(out, max_chars)
        note = _join_notes(note, "clipped to {:,} chars".format(max_chars))
    return out, note


def unique(text, max_chars):
    """Collapse runs of same-shaped lines into "<line> (x N)". The first line
    of each run is the one kept, so the surviving text is real output rather
    than a reconstruction. If the collapsed text is still too long, head_tail
    finishes the job."""
    lines = text.split("\n")
    total = len(lines)
    out_lines = []
    i = 0
    while i < total:
        key = _norm(lines[i])
        j = i + 1
        while j < total and _norm(lines[j]) == key:
            j += 1
        run = j - i
        if run > 1:
            out_lines.append("{} (x {:,})".format(lines[i], run))
        else:
            out_lines.append(lines[i])
        i = j
    out = "\n".join(out_lines)
    note = "collapsed {:,} lines into {:,} run(s)".format(total,
                                                          len(out_lines))
    if len(out) <= max_chars:
        return out, note
    tail_out, tail_note = head_tail(out, max_chars)
    return tail_out, _join_notes(note, tail_note)


def _shrink(value, str_limit, seq_limit):
    """Rebuild a decoded JSON value with long leaves clipped. Clipped strings
    and dropped entries say so inline, so the reader is never misled into
    thinking a truncated value is the whole value."""
    if isinstance(value, str):
        if len(value) <= str_limit:
            return value
        return value[:str_limit] + "...(+{:,} chars)".format(
            len(value) - str_limit)
    if isinstance(value, dict):
        limit = max(seq_limit, 1)
        items = list(value.items())
        out = {}
        for key, val in items[:limit]:
            out[str(key)] = _shrink(val, str_limit, seq_limit)
        if len(items) > limit:
            out["..."] = "(+{:,} more keys)".format(len(items) - limit)
        return out
    if isinstance(value, (list, tuple)):
        items = list(value)
        out = [_shrink(v, str_limit, seq_limit) for v in items[:seq_limit]]
        if len(items) > seq_limit:
            out.append("...(+{:,} more items)".format(
                len(items) - seq_limit))
        return out
    return value


def structure(text, max_chars):
    """Keep the shape of a JSON document and clip the bulk. Invalid JSON is
    not an error here: the caller handed over whatever a tool printed, so an
    unparseable body falls back to head_tail instead of raising."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        out, note = head_tail(text, max_chars)
        return out, _join_notes("not valid JSON, used head_tail", note)
    out = ""
    for str_limit, seq_limit in JSON_STEPS:
        out = json.dumps(_shrink(data, str_limit, seq_limit), indent=1,
                         default=str)
        if len(out) <= max_chars:
            return out, ("kept JSON shape, strings clipped to {:,} chars and "
                         "sequences to {:,} items; {:,} of {:,} chars"
                         .format(str_limit, seq_limit, len(out), len(text)))
    tail_out, tail_note = head_tail(out, max_chars)
    return tail_out, _join_notes("JSON shape alone does not fit, used "
                                 "head_tail", tail_note)


STRATEGIES = {
    "head_tail": head_tail,
    "errors": errors,
    "unique": unique,
    "structure": structure,
}


def choose_mode(text):
    """Pick a strategy from the shape of the text. Deliberately crude, in
    this order:

      1. JSON shaped, meaning the first non-space character is { or [ and the
         whole body parses, goes to `structure`.
      2. Build or test shaped, meaning at least one error-ish line and those
         lines a minority of the output, goes to `errors`. A failing run is a
         wall of noise with a few loud lines in it; when nearly every line is
         loud, the match carries no information and we fall through.
      3. Repetitive, meaning the normalized line shapes collapse to under
         half the line count, goes to `unique`. That is a progress log or a
         retry loop.
      4. Anything else goes to `head_tail`, which is never wrong, only blunt.
    """
    if text.lstrip()[:1] in ("{", "["):
        try:
            json.loads(text)
            return "structure"
        except (ValueError, TypeError):
            pass
    lines = text.split("\n")
    total = len(lines)
    hits = 0
    for line in lines:
        if _looks_error(line):
            hits += 1
    if hits and (total < MIN_LINES_FOR_SHAPE
                 or hits <= total * ERROR_MAJORITY):
        return "errors"
    if total >= MIN_LINES_FOR_SHAPE:
        shapes = set()
        for line in lines:
            shapes.add(_norm(line))
        if len(shapes) <= total * REPEAT_RATIO:
            return "unique"
    return "head_tail"


def condense(text, max_chars, mode="auto"):
    """Shorten `text` to at most `max_chars` characters and return
    (text, note). `note` is an empty string when nothing was dropped, and
    otherwise says plainly what went, for example "kept first 40 and last 40
    lines of 5,120; dropped 5,040". `mode` is one of head_tail, errors,
    unique, structure, or auto to pick by content shape."""
    if text is None:
        text = ""
    if max_chars is None or max_chars < 0:
        max_chars = 0
    if len(text) <= max_chars:
        return text, ""
    if mode == "auto":
        mode = choose_mode(text)
    fn = STRATEGIES.get(mode)
    if fn is None:
        raise ValueError("unknown condense mode: {!r}".format(mode))
    out, note = fn(text, max_chars)
    # Belt and braces: whatever a strategy did, the budget is the contract.
    if len(out) > max_chars:
        out = _truncate_chars(out, max_chars)
        note = _join_notes(note, "clamped to {:,} chars".format(max_chars))
    return out, note


def _self_test():
    def tag(i):
        """Unique alphabetic label, so test lines have no digits in them and
        therefore no accidental repeated shape."""
        out = ""
        n = i + 1
        while n:
            n, r = divmod(n - 1, 26)
            out = chr(97 + r) + out
        return out

    words = ("alpha", "bravo", "cherry", "delta", "echo", "fig", "grape")
    plain = "\n".join(
        "{} {}".format(tag(i),
                       " ".join(words[(i + j) % len(words)]
                                for j in range(6)))
        for i in range(300))
    build = ["{} compiling module".format(tag(i)) for i in range(200)]
    build[100] = "ERROR: undefined symbol in module zz"
    build[150] = "Traceback (most recent call last):"
    buildlog = "\n".join(build)
    repeated = "\n".join("downloading chunk {} of 400".format(i)
                         for i in range(400))
    blob = json.dumps({"name": "x" * 500,
                       "items": list(range(300)),
                       "flag": True,
                       "nested": {"a": [{"b": "y" * 200} for _ in range(10)]}})
    oneline = "x" * 5000

    # text already under the limit comes back untouched, with no note at all
    for mode in ("auto", "head_tail", "errors", "unique", "structure"):
        out, note = condense("already short enough", 100, mode)
        assert out == "already short enough", (mode, out)
        assert note == "", (mode, note)
        # an empty string is under every limit, including zero
        for cap in (0, 1, 100):
            assert condense("", cap, mode) == ("", ""), (mode, cap)

    # the budget is honoured for every mode, every sample, every size,
    # including caps far smaller than the elision marker itself
    samples = (plain, buildlog, repeated, blob, oneline, "{not json at all",
               "{}", "[]", "a\nb", "\n\n\n", "x", "x" * 40)
    for mode in ("auto", "head_tail", "errors", "unique", "structure"):
        for sample in samples:
            for cap in (0, 1, 2, 5, 12, 27, 40, 200, 1000, 4000):
                out, note = condense(sample, cap, mode)
                assert len(out) <= cap, (mode, cap, len(out), len(sample))
                if len(sample) > cap:
                    assert note, (mode, cap, "expected a note")

    # a negative budget is treated as zero rather than slicing backwards
    out, note = condense(plain, -5)
    assert out == "", out
    assert note, note

    # auto picks by shape
    assert choose_mode(blob) == "structure", choose_mode(blob)
    assert choose_mode(buildlog) == "errors", choose_mode(buildlog)
    assert choose_mode(repeated) == "unique", choose_mode(repeated)
    assert choose_mode(plain) == "head_tail", choose_mode(plain)

    # head_tail keeps both ends and says how much it dropped
    out, note = condense(plain, 1200, "head_tail")
    assert out.startswith(plain.split("\n")[0]), out[:60]
    assert out.rstrip("\n").endswith(plain.split("\n")[-1]), out[-60:]
    assert "lines elided" in out, out
    assert "kept first" in note and "dropped" in note, note

    # errors keeps the loud lines, not just the first and last screens
    out, note = condense(buildlog, 600, "errors")
    assert "ERROR: undefined symbol in module zz" in out, out
    assert "Traceback (most recent call last):" in out, out
    assert "error match(es)" in note, note

    # errors with nothing to match degrades to head_tail and says so
    out, note = condense(plain, 600, "errors")
    assert "no error-like line found" in note, note
    assert "kept first" in note, note

    # unique collapses a progress log to one line plus a count
    out, note = condense(repeated, 400, "unique")
    assert out == "downloading chunk 0 of 400 (x 400)", out
    assert note == "collapsed 400 lines into 1 run(s)", note

    # unique on text that does not repeat still respects the budget
    out, note = condense(plain, 300, "unique")
    assert len(out) <= 300, len(out)
    assert "collapsed" in note, note

    # structure keeps parseable JSON with the top-level keys intact
    out, note = condense(blob, 1500, "structure")
    back = json.loads(out)
    assert set(back) == {"name", "items", "flag", "nested"}, sorted(back)
    assert back["flag"] is True, back["flag"]
    assert back["name"].endswith("chars)"), back["name"][-20:]
    assert "...(+" in json.dumps(back["items"]), back["items"][-1]
    assert "kept JSON shape" in note, note

    # invalid JSON in structure mode falls back instead of raising
    out, note = condense("{not json at all " + "z" * 500, 200, "structure")
    assert "not valid JSON" in note, note
    assert len(out) <= 200, len(out)

    # valid JSON too big for even the harshest clip still falls back cleanly
    out, note = condense(blob, 30, "structure")
    assert len(out) <= 30, len(out)
    assert note, note

    # a single line with no newline anywhere gets clipped in the middle
    out, note = condense(oneline, 100)
    assert len(out) == 100, len(out)
    assert "chars elided" in out, out
    assert "single line of 5,000 chars" in note, note

    # a budget smaller than the elision marker drops the marker, not the
    # guarantee
    marker = CHAR_MARKER.format(4999)
    out, note = condense(oneline, len(marker) - 1)
    assert len(out) == len(marker) - 1, (len(out), len(marker))
    assert "elided" not in out, out
    assert note, note

    # an unknown mode is a programming error, not a silent fallback
    try:
        condense(plain, 100, "telepathy")
    except ValueError as exc:
        assert "telepathy" in str(exc), str(exc)
    else:
        raise AssertionError("unknown mode should raise ValueError")

    print("hearth-condense self-test OK")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="hearth-condense",
        description="shrink a tool result to fit a small context window")
    p.add_argument("path", nargs="?",
                   help="file to condense (default: stdin)")
    p.add_argument("--max-chars", type=int, default=4000,
                   help="character budget for the result (default 4000)")
    p.add_argument("--mode", default="auto",
                   choices=["auto"] + sorted(STRATEGIES),
                   help="strategy, or auto to pick by content shape")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    if a.path:
        with open(a.path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()
    out, note = condense(text, a.max_chars, a.mode)
    sys.stdout.write(out)
    if out and not out.endswith("\n"):
        sys.stdout.write("\n")
    # The note goes to stderr so a pipeline gets only the condensed text.
    if note:
        sys.stderr.write("[condense] {}\n".format(note))
    return 0


if __name__ == "__main__":
    sys.exit(main())
