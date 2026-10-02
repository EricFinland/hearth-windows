#!/usr/bin/env python3
"""hearth diff preview: what a file write would actually change, as data an
approval card can draw.

An approval card for write_file used to show the tool's raw arguments: the
entire new file body, with nothing to say which three lines of a four hundred
line file are different. A person asked to approve that either reads all four
hundred lines or, far more often, does not read any of them, and the second
habit is exactly the one the approval gate exists to prevent. This module
works out the change itself -- the lines that go, the lines that arrive, a few
lines of context around each -- so the card can show that instead.

WHERE IT IS COMPUTED, AND WHY HERE. Only the sidecar can do this correctly.
The page does not have the file on disk, and a diff computed from what the
page happens to remember of a file would be a guess presented as a fact. So
the engine calls preview_tool_call() at the gate, with the REAL arguments the
tool is about to run with, and the result rides on the approval_request event
as `diff`. The checkpoint store feeds the same shape through build_preview()
for GET /checkpoints/diff, so "what will this write do" and "what will this
restore do" are drawn by one component and mean the same thing.

MIRRORING THE TOOLS EXACTLY. A preview that resolves a path differently from
the tool shows the user one file and writes another. Every function below
walks the same steps in the same order as hearth_tools: safe_join, then the
.hearthignore self-protection, then the .hearthignore exclusion, then (for
write_file only) the case-only-rename refusal. Wherever the tool would refuse,
this returns no preview at all: the tool's own result will say why, and a
preview of a write that is never going to happen is noise. edit_file whose
`find` text is absent gets a file entry with a note instead, because "this
will not match" is the single most useful thing to know before approving it.

SECRETS. The new content is only half of what a diff shows. Its context lines
come from the file already on disk, and that file may hold a credential the
new content never mentions: approving a one-line edit to settings.py must not
put the database password three lines above it onto the screen, into the
event log, and into session state. So:

  - A path matching the checkpoint store's secret-file patterns (.env*,
    *.pem, *.key, id_rsa*, *.pfx, credentials*) is never shown at all, only
    named, with line counts and a plain reason.
  - Every other file is scanned with hearth_secrets.scan() on BOTH sides,
    old and new, and every finding is redacted before a single line is
    emitted -- every finding, not only those at the engine's surfacing
    threshold, because hearth_secrets already drops placeholders and a
    preview has no reason to show a medium-confidence key either.
  - Redaction keeps line breaks, so a multi-line PEM block collapses to one
    marker without shifting every later line number.
  - The diff itself is computed on the UNREDACTED lines and only the text it
    emits is taken from the redacted copy. Diffing redacted text would make a
    rotated key (old value -> new value) look like no change at all, which
    is precisely the change a person most needs to see happen.
  - hearth_secrets scans a bounded head-and-tail window of a large file, and
    its PEM detector needs a key's BEGIN and END markers both inside what it
    reads. So, independent of that window, one linear pass over the WHOLE of
    each side finds every private-key block (BEGIN through the matching END,
    or through the run of key material after a BEGIN that has no END) and
    redacts every line of base64 key material in it, wherever the file's
    size puts it. See _pem_block_findings.
  - The lines of each hunk are then scanned again, cut to about the length
    they will be shown at (later, for a shown line whose token crosses the
    display cut), in windows small enough that scan() reads every character
    of them, by scan() and by a check that also finds a known-format token
    (a JWT, an sk- key) too long for scan() to report. That catches a
    single-line secret anywhere in the file; it does not see past the
    hunk's own lines, which is why the PEM pass above exists. See
    _rescan_hunk.
  - A line identical on both sides is redacted if either side redacts it.
  - A finding too long for hearth_secrets to redact (MAX_REDACT_SPAN) hides
    the whole file rather than being shown; see _redact_keep_lines.

SIZE. The approval event is persisted (session_state keeps a tail of recent
events) and replayed over SSE on every reconnect, so it must stay small no
matter how large the write is. A preview is capped at MAX_DIFF_LINES emitted
lines and MAX_DIFF_BYTES serialised in total (paths, notes and hunk headers
included, not only line text), a single line at MAX_LINE_CHARS, and an input
file at MAX_INPUT_CHARS / MAX_INPUT_LINES. Every cap that bites is reported
-- `truncated`, `cut` on a line, `hidden_reason` on a file, `files_omitted`
-- never applied silently.

TIME. The preview is worked out at the approval gate, before the card is
raised, so it must stay quick whatever the file looks like. Comparing is
bounded in work and in wall-clock time, not only in input size; see the
comment above _unique_anchors. Past the bound a stretch is shown as whole
blocks out and in rather than compared line by line, and the file says so.

SHAPE. Structured lines, not a unified-diff string, so the page never parses
anything:

    {"files": [{"path", "status": "added|modified|deleted|unchanged",
                "added": N, "removed": M,
                "hunks": [{"old_start", "old_count", "new_start", "new_count",
                           "lines": [[tag, text] or [tag, text, cut], ...]}],
                "truncated": bool,
                "hidden_reason": str (only when content is not shown),
                "notes": [str, ...] (only when there is something to say),
                "redacted_lines": N (only when something was redacted)}],
     "added": N, "removed": M, "truncated": bool,
     "files_total": N, "files_omitted": N}

tag is "+", "-" or " ". Line numbers are 1-based; a hunk side with no lines
(count 0, as in the old side of a new file) starts at the line before it,
so 0 at the top of a file, as unified diffs write it. `added`/`removed` count the
whole change even when the lines shown were cut short; they are None for a
file whose content could not be compared at all.

This is a preview, computed at the moment the card is raised. A file that
changes between the card appearing and the click landing is written by the
tool as it then is; the card says what was true when it was drawn.

Standard library only. No network. Reads files; never writes any.
"""

import bisect
import difflib
import fnmatch
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hearth_checkpoint  # noqa: E402
import hearth_contain  # noqa: E402
import hearth_paths  # noqa: E402
import hearth_secrets  # noqa: E402
import hearth_tools  # noqa: E402

WRITE_TOOLS = ("write_file", "edit_file", "replace_in_files")

CONTEXT_LINES = 8          # context sent either side of a change. The page shows
                           # three and folds the rest behind an expand button, so
                           # this is how far "expand" can reach.
MAX_DIFF_LINES = 2000      # emitted lines across every file in one preview
MAX_DIFF_BYTES = 64 * 1024  # serialised text across every file in one preview
MAX_LINE_CHARS = 1000      # one line, past which the rest is counted, not sent
MAX_FILES = 20             # files given a full diff in one preview
MAX_LISTED_FILES = 500     # files named at all; beyond this only a count
MAX_INPUT_CHARS = 2_000_000  # the same 2 MB replace_in_files itself skips past
MAX_INPUT_LINES = 50_000
LISTING_RESERVE = 16 * 1024  # of MAX_DIFF_BYTES, kept back from line text for file names
MATCH_CALL_CELLS = 1_000_000   # one SequenceMatcher call: old lines * new lines it compares
MATCH_TOTAL_CELLS = 8_000_000  # every such call in one preview, all files together
PREVIEW_SECONDS = 2.0          # wall-clock belt over the comparing for one preview
RESCAN_SLACK = 256             # kept past MAX_LINE_CHARS for the second scan, so a
                               # secret straddling the cut is still recognised
RESCAN_WINDOW = hearth_secrets.MAX_HEAD_CHARS  # under scan()'s window: read in full
RESCAN_OVERLAP = 2 * hearth_secrets.MAX_REDACT_SPAN  # repeated between windows, so a
                               # multi-line key across a window edge is seen whole
RESCAN_LONG_LINE = 20_000      # how far past the display cut a shown line is read
                               # when a run of non-space text crosses the cut; small
                               # enough that two such lines and RESCAN_OVERLAP still
                               # fit one RESCAN_WINDOW, so the windows keep moving

# The private-key block markers, matched over the whole of a file whatever its
# size (see _pem_block_findings). Any label naming a PRIVATE KEY: RSA, EC, DSA,
# OPENSSH, ENCRYPTED, PGP ... BLOCK, and the four-dash SSH2 form.
_PEM_BEGIN_RE = re.compile(r"-{4,5} ?BEGIN ((?:[A-Z0-9]+ )*?PRIVATE KEY(?: [A-Z0-9]+)*) ?-{4,5}")
_PEM_B64_RE = re.compile(r"[A-Za-z0-9+/=]+")
_PEM_ESCAPED_EOL_RE = re.compile(r"\\+[nr]")  # a key kept on one line, as JSON does
_PEM_HEADER_RE = re.compile(r"(?:Proc-Type|DEK-Info|Comment): ")
_NON_SPACE_RUN_RE = re.compile(r"\S*")

HIDDEN_SECRET_FILE = ("this looks like a secrets file (.env, a key, credentials), "
                      "so its content is not shown")
HIDDEN_IGNORED = "excluded by .hearthignore, so its content is not shown"
HIDDEN_BINARY = "not UTF-8 text (a binary file?), so there is nothing to show line by line"
HIDDEN_TOO_LARGE = "too large to preview line by line"
HIDDEN_UNREADABLE = "the file on disk could not be read, so the change cannot be shown"
HIDDEN_NOT_PREVIEWED = "not previewed: this change touches more files than one preview shows"
HIDDEN_BUDGET = "not shown: the preview reached its size limit before this file"
HIDDEN_UNSAFE = ("hidden: a credential was found and could not be redacted cleanly, "
                 "so nothing from this file is shown")

NOTE_FIND_MISSING = ("the find text does not appear in this file, so the edit will "
                     "fail and change nothing")
NOTE_IDENTICAL = "the new content is identical to what is on disk"
NOTE_TO_CRLF = "line endings change from LF to CRLF on every line"
NOTE_TO_LF = "line endings change from CRLF to LF on every line"
NOTE_ADDS_FINAL_NEWLINE = "adds a newline at the end of the file"
NOTE_DROPS_FINAL_NEWLINE = "removes the newline at the end of the file"
NOTE_COARSE = ("part of this change was too large to compare line by line, so it is "
               "shown as whole blocks removed and added, and the counts may be high")


class _Budget:
    """The shared allowances for one whole preview: lines, bytes and compute.

    Bytes are charged at JSON-encoded size, which is what the preview
    actually costs in the persisted event (a non-ASCII character serialises
    as \\uXXXX). Line text and hunk headers may use the byte allowance less
    LISTING_RESERVE; file entries, with their paths, reasons and notes, are
    charged against the whole of it. The reserve is what lets a
    preview whose diffs filled their share still name the files it did not
    diff, and charging the names at all is what makes MAX_DIFF_BYTES a bound
    on the whole preview rather than on its line text alone.

    Compute is counted in "cells": a SequenceMatcher call over n old lines
    and m new lines is charged n * m, roughly its worst case. The deadline is
    a wall-clock belt over all of it; see _matching_blocks."""

    def __init__(self, max_lines=MAX_DIFF_LINES, max_bytes=MAX_DIFF_BYTES,
                 max_cells=MATCH_TOTAL_CELLS, seconds=None):
        self.lines_left = max_lines
        self.bytes_left = max_bytes
        self.line_bytes_left = max(0, max_bytes - LISTING_RESERVE)
        self.cells_left = max_cells
        # Read at call time, not bound as a default, so the module-wide
        # belt can be tuned (the self-test sets it to nothing).
        self.deadline = time.monotonic() + (PREVIEW_SECONDS if seconds is None else seconds)
        self.exhausted = False

    def take(self, line):
        """Charge one emitted [tag, text] or [tag, text, cut] line."""
        cost = len(json.dumps(line)) + 2  # and the ", " that separates it from the next
        if self.lines_left <= 0 or cost > self.line_bytes_left or cost > self.bytes_left:
            self.exhausted = True
            return False
        self.lines_left -= 1
        self.line_bytes_left -= cost
        self.bytes_left -= cost
        return True

    def charge(self, obj, diff_share=False):
        """Charge the serialised size of `obj` (an entry or hunk with its
        lines left out) against the whole byte allowance, or with
        `diff_share` (a hunk header, part of the diff itself) against the
        line text's share as well. False, and nothing charged, when it does
        not fit."""
        cost = len(json.dumps(obj)) + 2
        if cost > self.bytes_left or (diff_share and cost > self.line_bytes_left):
            self.exhausted = True
            return False
        self.bytes_left -= cost
        if diff_share:
            self.line_bytes_left -= cost
        return True

    def spend_cells(self, cells):
        if cells > self.cells_left or self.out_of_time():
            return False
        self.cells_left -= cells
        return True

    def out_of_time(self):
        return time.monotonic() > self.deadline


def is_secret_path(path):
    """True when `path`'s file name matches the checkpoint store's secret-file
    patterns. Read from hearth_checkpoint rather than copied, so "never
    captured by undo" and "never shown in a diff" cannot drift apart."""
    name = (path or "").replace("\\", "/").rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, pat) for pat in hearth_checkpoint._secret_file_patterns())


def _dominant_eol(text):
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf == 0 and lf == 0:
        return ""
    return "\r\n" if crlf > lf else "\n"


def _split(text):
    """Lines of `text` with CRLF folded to LF, and whether it ended in a
    newline. Folding first is what keeps a CRLF file from diffing as every
    line changed; a genuine line-ending change is reported as a note
    instead (see _eol_notes)."""
    norm = text.replace("\r\n", "\n")
    lines = norm.split("\n")
    final_newline = norm.endswith("\n")
    if final_newline:
        lines.pop()
    elif lines == [""]:
        lines = []
    return norm, lines, final_newline


def _redact_keep_lines(norm, findings, line_count):
    """`norm` with every finding's span replaced by a marker, as a list of
    lines exactly as long as the unredacted one. A finding spanning several
    lines (a PEM body) keeps its line breaks after the marker, so nothing
    below it moves. Returns (lines, set of line indexes touched), or
    (None, None) when the text cannot be shown safely: the line count could
    not be kept, or a finding is longer than hearth_secrets.MAX_REDACT_SPAN.

    The second is a deliberate break from hearth_secrets.redact(), which
    leaves an oversized span in place rather than blank an unbounded run of
    content being written. Here the run may be a context line from a file on
    disk that nobody asked to see, which is exactly what this module exists
    to keep off the screen, so the caller hides the whole file (or blanks the
    whole hunk) instead of showing the span or guessing at a partial cut.
    A finding marked "uncapped" (one line of key material from
    _pem_block_findings, or a token from _long_token_findings: by
    construction nothing but a fixed alphabet with no spaces) is exempt: it
    can always be redacted cleanly, however long it is."""
    spans = sorted(
        (f["start"], f["end"], f["kind"], bool(f.get("uncapped"))) for f in findings
        if 0 <= f["start"] < f["end"] <= len(norm))
    # Overlapping findings merge into one span reaching the furthest end, so
    # a second finding that starts inside the first and runs past it is not
    # left half shown.
    merged = []
    for start, end, kind, uncapped in spans:
        if not uncapped and end - start > hearth_secrets.MAX_REDACT_SPAN:
            return None, None
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
            continue
        merged.append([start, end, kind])
    out = []
    touched = set()
    cursor = 0
    line_at_cursor = 0  # counted on from the last span, never from the top:
                        # a key body is thousands of spans, one per line
    for start, end, kind in merged:
        out.append(norm[cursor:start])
        first_line = line_at_cursor + norm.count("\n", cursor, start)
        breaks = norm.count("\n", start, end)
        line_at_cursor = first_line + breaks
        touched.update(range(first_line, first_line + breaks + 1))
        # One marker per line the finding covers, not one marker and then
        # blank lines: a run of empty context lines would claim the file has
        # empty lines there, which is its own small lie.
        out.append("\n".join(["[REDACTED:{}]".format(kind)] * (breaks + 1)))
        cursor = end
    out.append(norm[cursor:])
    lines = "".join(out).split("\n")
    if norm.endswith("\n"):
        lines.pop()
    elif lines == [""]:
        lines = []
    if len(lines) != line_count:
        return None, None
    return lines, touched


def _pem_block_findings(norm):
    """Findings for every line of key material inside a private-key block
    anywhere in `norm`, read in one linear pass over the whole text.

    hearth_secrets.scan() reads only a head and a tail of a large text, and
    its PEM detector needs both the BEGIN and the END marker inside what it
    reads. The second scan in _rescan_hunk reads only the lines of one hunk.
    Neither can see a key in the middle of a large file whose BEGIN or END
    line sits outside the hunk being shown, so an edit next to one marker
    would show half the key and two edits either side of it all of it. This
    pass does not depend on either window: it finds each BEGIN marker whose
    label names a PRIVATE KEY, the END marker with the same label after it,
    and marks every line between them that is key material.

    Key material means what hearth_secrets means by it: a line that is pure
    base64 once trimmed (a key kept on one line with escaped "\\n" between
    its rows, as JSON stores one, counts too). The markers stay visible, as
    does any line holding anything else, so a block cannot be used to hide a
    command or a sentence from the approval card; only base64 is ever
    covered. The text after a BEGIN marker on its own line and before an END
    marker on its own line is judged the same way.

    A BEGIN with no matching END covers the contiguous run of key material
    after it (blank lines and Proc-Type / DEK-Info / Comment headers do not
    end the run), up to the end of the file. It stops at the first other
    line, so a source file that only quotes a BEGIN marker does not lose
    every short word after it to redaction.

    Each finding is one line's material and is marked "uncapped"; see
    _redact_keep_lines. Linear in len(norm): substring and regex searches
    that only move forward, plus one look at each line inside a block."""
    if "PRIVATE KEY" not in norm:
        return []
    kind = hearth_secrets.KIND_PRIVATE_KEY_PEM
    findings = []
    end_res = {}
    n = len(norm)
    pos = 0
    while True:
        begin = _PEM_BEGIN_RE.search(norm, pos)
        if begin is None:
            break
        label = begin.group(1)
        end_re = end_res.get(label)
        if end_re is None:
            end_re = end_res[label] = re.compile(
                r"-{4,5} ?END " + re.escape(label) + r" ?-{4,5}")
        end = end_re.search(norm, begin.end())
        body_end = end.start() if end is not None else n
        cursor = begin.end()
        while cursor <= body_end:
            newline = norm.find("\n", cursor, body_end)
            seg_end = body_end if newline == -1 else newline
            piece = norm[cursor:seg_end]
            stripped = piece.strip()
            material = _PEM_ESCAPED_EOL_RE.sub("", stripped)
            if material and _PEM_B64_RE.fullmatch(material):
                lead = len(piece) - len(piece.lstrip())
                findings.append({"start": cursor + lead, "end": cursor + lead + len(stripped),
                                 "kind": kind, "uncapped": True})
            elif end is None and stripped and not _PEM_HEADER_RE.match(stripped):
                # An unterminated block's run of key material is over (or,
                # on the BEGIN line itself, the marker is only quoted).
                break
            if newline == -1:
                break
            cursor = newline + 1
        if end is None:
            break
        pos = end.end()
    return findings


_TOKEN_JUDGE_WINDOW = 120  # hearth_secrets judges PEM bodies in windows this long


def _token_is_placeholder(value):
    """is_placeholder(), as the known-format detectors call it, judged on
    each _TOKEN_JUDGE_WINDOW slice of `value`: a placeholder only if every
    slice is one. hearth_secrets judges a known-format value whole, and one
    of its checks (distinct characters over length) cannot pass for a long
    value drawn from a small alphabet: a 200-character JWT, or an sk- key of
    that length, is always called a placeholder and never found. Judged in
    slices, the check works at the length it was tuned for, the same remedy
    hearth_secrets applies to PEM bodies (_pem_body_is_placeholder)."""
    for i in range(0, len(value), _TOKEN_JUDGE_WINDOW):
        window = value[i:i + _TOKEN_JUDGE_WINDOW]
        if i and len(window) < 20:
            continue  # a trailing scrap too short to judge on its own
        if not hearth_secrets.is_placeholder(window, check_entropy=False):
            return False
    return True


def _long_token_findings(text):
    """Findings for known-format tokens (JWT, sk-, github_pat_, Stripe, ...)
    in `text`, judged by _token_is_placeholder. Every match hearth_secrets
    itself would report is reported here too; the difference is a long
    token, which it misses. The values are a fixed alphabet with no spaces,
    so nothing readable can hide behind one of these redactions."""
    out = []
    formats = [(kind, regex) for kind, _sev, _conf, regex, _why in hearth_secrets._KNOWN_FORMATS]
    formats.append((hearth_secrets.KIND_STRIPE_KEY, hearth_secrets._STRIPE_RE))
    for kind, regex in formats:
        for m in regex.finditer(text):
            if not _token_is_placeholder(m.group(1)):
                out.append({"start": m.start(1), "end": m.end(1), "kind": kind,
                            "uncapped": True})
    return out


def _redacted_lines(norm, line_count):
    """_redact_keep_lines over hearth_secrets.scan(norm)'s findings and
    _pem_block_findings(norm)'s. Returns (None, set()) when there is nothing
    to redact, so the caller can keep using the lines it already has."""
    findings = hearth_secrets.scan(norm)["findings"] + _pem_block_findings(norm)
    if not findings:
        return None, set()
    return _redact_keep_lines(norm, findings, line_count)


def _rescan_windows(texts):
    """[(offset, chunk), ...] covering "\n".join(texts), each chunk whole
    lines and at most RESCAN_WINDOW characters (one line longer than that is a
    chunk of its own; callers cut lines to at most RESCAN_LONG_LINE first),
    consecutive chunks sharing at least RESCAN_OVERLAP characters of lines."""
    starts = []
    pos = 0
    for text in texts:
        starts.append(pos)
        pos += len(text) + 1
    starts.append(pos)  # one past the end, so starts[j] works for j == len(texts)
    windows = []
    i, n = 0, len(texts)
    while i < n:
        j = i + 1
        while j < n and starts[j + 1] - starts[i] - 1 <= RESCAN_WINDOW:
            j += 1
        windows.append((starts[i], "\n".join(texts[i:j])))
        if j >= n:
            break
        # Step back so the next window repeats the tail of this one, but
        # always forward of where this one began.
        k = j
        while k - 1 > i and starts[j] - starts[k] < RESCAN_OVERLAP:
            k -= 1
        i = k
    return windows


def _rescan_hunk(lines, emit):
    """Second scan over the lines of one hunk, then the final cut.

    `lines` holds [tag, text] pairs at full length, already redacted by the
    whole-file pass in file_diff. The first `emit` are to be shown. Any after
    them are lines of the same hunk the budget could not take; up to
    RESCAN_OVERLAP characters of those are read as context for the scan and
    then dropped unshown. Nothing outside the hunk is read: no lines before
    its first, none after its last.

    What this catches is a secret that lies wholly inside the hunk's text: a
    single-line credential anywhere in a file, including the middle of one
    too large for hearth_secrets.scan() to read in full, and a multi-line
    key whose BEGIN and END lines are both in the hunk. It does NOT see a
    key that starts above the hunk or ends below it, because its markers are
    not in the text scanned. Private-key blocks are therefore found by
    _pem_block_findings over the whole file before this runs, and that pass,
    not this one, is what keeps a key's body out of a hunk that shows only
    part of it.

    scan() reads only a head and a tail of a long text, so scanning the hunk
    joined at full length would skip the middle of it when its lines are long
    (a log, a CSV, minified data). Instead each line is first cut to
    MAX_LINE_CHARS plus RESCAN_SLACK, and the cut lines are scanned in
    _rescan_windows chunks, each of which scan() reads in full. A shown line
    whose run of non-space text crosses the display cut is cut later
    instead, at the end of that run plus RESCAN_SLACK (at most
    RESCAN_LONG_LINE): a token that starts just before the cut and is longer
    than the slack (a long JWT) would otherwise be scanned cut short, fail
    to match, and show its first characters. Each window is read by scan()
    and by _long_token_findings, which also finds a known-format token too
    long for scan() to report. Every finding is redacted, the
    list is trimmed to `emit`, and only then is each line cut to
    MAX_LINE_CHARS, with the characters not sent counted as [tag, text, cut].

    Mutates `lines` in place; returns how many shown lines were redacted."""
    keep = MAX_LINE_CHARS + RESCAN_SLACK
    dropped = []
    for index, entry in enumerate(lines):
        text = entry[1]
        cut = keep
        if index < emit and len(text) > keep:
            run = _NON_SPACE_RUN_RE.match(text, MAX_LINE_CHARS, RESCAN_LONG_LINE)
            cut = min(len(text), RESCAN_LONG_LINE, max(keep, run.end() + RESCAN_SLACK))
        dropped.append(max(0, len(text) - cut))
        if len(text) > cut:
            entry[1] = text[:cut]
    # Context read past the shown lines, up to RESCAN_OVERLAP characters.
    end = emit
    extra = 0
    while end < len(lines) and extra < RESCAN_OVERLAP:
        extra += len(lines[end][1]) + 1
        end += 1
    texts = [entry[1] for entry in lines[:end]]
    findings = []
    for offset, chunk in _rescan_windows(texts):
        for f in hearth_secrets.scan(chunk)["findings"] + _long_token_findings(chunk):
            findings.append({"start": f["start"] + offset, "end": f["end"] + offset,
                             "kind": f["kind"]})
    del lines[emit:]
    touched_shown = 0
    if findings:
        joined = "\n".join(texts)
        redacted, touched = _redact_keep_lines(joined, findings, len(texts))
        if redacted is None:
            # Could not keep the alignment: blank every line rather than show any.
            for entry in lines:
                entry[1] = "[REDACTED]"
            touched = set(range(len(lines)))
        else:
            for i in touched:
                if i < len(lines):
                    lines[i][1] = redacted[i]
        touched_shown = sum(1 for i in touched if i < len(lines))
    for entry, lost in zip(lines, dropped):
        # How long the line would be, redacted and whole: what is left of it
        # plus what the first cut already took off.
        full = len(entry[1]) + lost
        if full > MAX_LINE_CHARS:
            entry[1] = entry[1][:MAX_LINE_CHARS]
            entry.append(full - MAX_LINE_CHARS)
    return touched_shown


def _fits(lines, room, line_bytes):
    """How many of `lines` ([tag, text] pairs) the budget could still take,
    each costed at the length it will be shown at before any redaction. An
    estimate (a redaction marker can be shorter or longer than what it
    replaces), and that is fine in either direction: a line past it is
    dropped unshown by _rescan_hunk, never emitted without its scan, and
    budget.take() still has the final word on the lines inside it."""
    n = 0
    spent = 0
    for entry in lines:
        if n >= room:
            break
        spent += len(json.dumps([entry[0], entry[1][:MAX_LINE_CHARS]])) + 2
        if spent > line_bytes:
            break
        n += 1
    return n


def _eol_notes(old, new, old_final, new_final):
    notes = []
    if not old or not new:
        return notes  # a new, deleted or empty file has no line ending to change
    before, after = _dominant_eol(old), _dominant_eol(new)
    if before == "\n" and after == "\r\n":
        notes.append(NOTE_TO_CRLF)
    elif before == "\r\n" and after == "\n":
        notes.append(NOTE_TO_LF)
    if not old_final and new_final:
        notes.append(NOTE_ADDS_FINAL_NEWLINE)
    elif old_final and not new_final:
        notes.append(NOTE_DROPS_FINAL_NEWLINE)
    return notes


def _status(old, new):
    if old is None:
        return "added"
    if new is None:
        return "deleted"
    return "modified" if old != new else "unchanged"


# ---------------------------------------------------------------------------
# Comparing. difflib.SequenceMatcher alone is roughly quadratic in the lines
# it is given when the matches are many and small (a file where every other
# line changed): measured at about 4 s for 8,000 lines and 20 s for 16,000,
# and this runs at the approval gate, before the card is raised, where Cancel
# cannot reach it. So it is only ever given a bounded stretch:
#
#   1. The common prefix and suffix are matched by a linear walk first. Most
#      edits touch a small part of a file, and this alone shrinks them to it.
#   2. A stretch small enough (MATCH_CALL_CELLS, against a per-preview total
#      of MATCH_TOTAL_CELLS) goes to SequenceMatcher as it is.
#   3. A larger one is split on lines that occur exactly once on each side,
#      kept in order by a longest-increasing-subsequence pass (the "patience"
#      idea): linear to find, n log n to order. Source files are mostly
#      unique lines, so this splits a big file into many small stretches,
#      each of which goes back through 1-3.
#   4. A stretch that is still too large, has no such lines, or is reached
#      after the wall-clock belt has run out is not compared further: it is
#      shown as its old lines removed and its new lines added. That is still
#      a correct diff, only not the smallest one, and the file says so
#      (NOTE_COARSE).
# ---------------------------------------------------------------------------


def _unique_anchors(a, alo, ahi, b, blo, bhi):
    """Pairs (i, j), increasing in both, of lines that occur exactly once in
    a[alo:ahi] and exactly once in b[blo:bhi] and agree in order."""
    in_a = {}
    for i in range(alo, ahi):
        line = a[i]
        in_a[line] = -1 if line in in_a else i
    in_b = {}
    for j in range(blo, bhi):
        line = b[j]
        if in_a.get(line, -1) >= 0:
            in_b[line] = -1 if line in in_b else j
    pairs = sorted((in_a[line], j) for line, j in in_b.items() if j >= 0)
    # Longest run increasing in j among pairs already sorted by i (patience
    # sorting): tails[k] is the pair ending the best run of length k + 1.
    tails, tail_js, back = [], [], [None] * len(pairs)
    for index, (_i, j) in enumerate(pairs):
        k = bisect.bisect_left(tail_js, j)
        back[index] = tails[k - 1] if k else None
        if k == len(tails):
            tails.append(index)
            tail_js.append(j)
        else:
            tails[k] = index
            tail_js[k] = j
    out = []
    index = tails[-1] if tails else None
    while index is not None:
        out.append(pairs[index])
        index = back[index]
    out.reverse()
    return out


def _matching_blocks(a, b, budget):
    """(blocks, coarse): sorted (i, j, size) runs of equal lines, and whether
    any stretch was left uncompared. See the comment block above."""
    blocks = []
    coarse = False
    stack = [(0, len(a), 0, len(b))]
    while stack:
        alo, ahi, blo, bhi = stack.pop()
        k = 0
        while alo + k < ahi and blo + k < bhi and a[alo + k] == b[blo + k]:
            k += 1
        if k:
            blocks.append((alo, blo, k))
            alo += k
            blo += k
        k = 0
        while ahi - k > alo and bhi - k > blo and a[ahi - k - 1] == b[bhi - k - 1]:
            k += 1
        if k:
            blocks.append((ahi - k, bhi - k, k))
            ahi -= k
            bhi -= k
        if alo == ahi or blo == bhi:
            continue
        if ahi - alo == 1 or bhi - blo == 1:
            # One line against many: the first equal line is the whole
            # answer, and a SequenceMatcher per such stretch would be most of
            # the cost of a file where every other line changed.
            if ahi - alo == 1:
                try:
                    blocks.append((alo, b.index(a[alo], blo, bhi), 1))
                except ValueError:
                    pass
            else:
                try:
                    blocks.append((a.index(b[blo], alo, ahi), blo, 1))
                except ValueError:
                    pass
            continue
        cells = (ahi - alo) * (bhi - blo)
        if cells <= MATCH_CALL_CELLS and budget.spend_cells(cells):
            matcher = difflib.SequenceMatcher(None, a[alo:ahi], b[blo:bhi])
            blocks.extend((alo + i, blo + j, n) for i, j, n in matcher.get_matching_blocks() if n)
            continue
        anchors = [] if budget.out_of_time() else _unique_anchors(a, alo, ahi, b, blo, bhi)
        if not anchors:
            coarse = True
            continue
        prev_i, prev_j = alo, blo
        for i, j in anchors:
            stack.append((prev_i, i, prev_j, j))
            blocks.append((i, j, 1))
            prev_i, prev_j = i + 1, j + 1
        stack.append((prev_i, ahi, prev_j, bhi))
    blocks.sort()
    merged = []
    for i, j, n in blocks:
        if merged and merged[-1][0] + merged[-1][2] == i and merged[-1][1] + merged[-1][2] == j:
            merged[-1][2] += n
        else:
            merged.append([i, j, n])
    return merged, coarse


def _opcodes(a, b, budget):
    """(opcodes, coarse), opcodes in SequenceMatcher.get_opcodes()'s format."""
    blocks, coarse = _matching_blocks(a, b, budget)
    codes = []
    i = j = 0
    for ai, bj, size in blocks + [[len(a), len(b), 0]]:
        if i < ai and j < bj:
            codes.append(("replace", i, ai, j, bj))
        elif i < ai:
            codes.append(("delete", i, ai, j, bj))
        elif j < bj:
            codes.append(("insert", i, ai, j, bj))
        i, j = ai + size, bj + size
        if size:
            codes.append(("equal", ai, i, bj, j))
    return codes, coarse


def _grouped(codes, n):
    """Hunks from opcodes, `n` lines of context either side: the same
    grouping SequenceMatcher.get_grouped_opcodes() does, over opcodes this
    module computed itself."""
    if not codes:
        return []
    codes = list(codes)
    if codes[0][0] == "equal":
        tag, i1, i2, j1, j2 = codes[0]
        codes[0] = tag, max(i1, i2 - n), i2, max(j1, j2 - n), j2
    if codes[-1][0] == "equal":
        tag, i1, i2, j1, j2 = codes[-1]
        codes[-1] = tag, i1, min(i2, i1 + n), j1, min(j2, j1 + n)
    groups, group = [], []
    for tag, i1, i2, j1, j2 in codes:
        if tag == "equal" and i2 - i1 > 2 * n:
            group.append((tag, i1, min(i2, i1 + n), j1, min(j2, j1 + n)))
            groups.append(group)
            group = []
            i1, j1 = max(i1, i2 - n), max(j1, j2 - n)
        group.append((tag, i1, i2, j1, j2))
    if group and not (len(group) == 1 and group[0][0] == "equal"):
        groups.append(group)
    return [g for g in groups if any(op[0] != "equal" for op in g)]


def file_diff(path, old, new, budget=None, context=CONTEXT_LINES, hidden_reason=None):
    """One file's entry in a preview. `old` is the current text (None when the
    file does not exist yet), `new` the text it will have (None when it will
    be deleted). `hidden_reason` names the file without showing content.

    `path` is display text the caller has already made workspace-relative;
    nothing here touches the filesystem. A caller that passes a reason has
    usually not loaded the content either (a binary or ignored file), so no
    line counts are claimed for it.

    Line text and hunk headers are charged to `budget` here; the entry's own
    fields are charged by build_preview once the entry is complete."""
    budget = budget if budget is not None else _Budget()
    entry = {"path": path, "status": _status(old, new), "added": None, "removed": None,
             "hunks": [], "truncated": False}
    if hidden_reason is not None:
        entry["hidden_reason"] = hidden_reason
        return entry
    old_text = "" if old is None else old
    new_text = "" if new is None else new

    too_big = (len(old_text) > MAX_INPUT_CHARS or len(new_text) > MAX_INPUT_CHARS
               or old_text.count("\n") > MAX_INPUT_LINES or new_text.count("\n") > MAX_INPUT_LINES)
    if too_big:
        entry["hidden_reason"] = HIDDEN_TOO_LARGE
        return entry

    old_norm, a, old_final = _split(old_text)
    new_norm, b, new_final = _split(new_text)
    # One comparison per file: the counts and the hunks come from the same
    # opcodes.
    codes, coarse = _opcodes(a, b, budget)
    added = removed = 0
    for tag, i1, i2, j1, j2 in codes:
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    entry["added"], entry["removed"] = added, removed
    notes = _eol_notes(old, new, old_final, new_final)
    if entry["status"] == "unchanged":
        notes.append(NOTE_IDENTICAL)
    if coarse:
        notes.append(NOTE_COARSE)
    if notes:
        entry["notes"] = notes

    if is_secret_path(path):
        # Counted, never shown: "+1 -1 in .env" is worth knowing, and says
        # nothing about what the lines hold.
        entry["hidden_reason"] = HIDDEN_SECRET_FILE
        return entry
    if budget.exhausted:
        entry["truncated"] = True
        entry["hidden_reason"] = HIDDEN_BUDGET
        return entry

    a_shown, a_touched = _redacted_lines(old_norm, len(a))
    b_shown, b_touched = _redacted_lines(new_norm, len(b))
    if a_touched is None or b_touched is None:
        entry["hidden_reason"] = HIDDEN_UNSAFE
        return entry
    a_shown = a_shown if a_shown is not None else a
    b_shown = b_shown if b_shown is not None else b

    redacted = 0
    for group in _grouped(codes, context):
        first, last = group[0], group[-1]
        old_count, new_count = last[2] - first[1], last[4] - first[3]
        # An empty side starts at the line before it, 0 at the top of a file,
        # as unified diffs write it (the old side of a new file is "-0,0").
        hunk = {"old_start": first[1] + (1 if old_count else 0), "old_count": old_count,
                "new_start": first[3] + (1 if new_count else 0), "new_count": new_count,
                "lines": []}
        if not budget.charge(hunk, diff_share=True):
            entry["truncated"] = True
            break
        # Text comes from the redacted copies at the same indexes; see the
        # module docstring for why the opcodes themselves come from the raw
        # lines. Never more than the budget could still take, so a coarse
        # whole-file block is not built out in full only to be cut.
        room = max(budget.lines_left, 0)
        raw = []
        for tag, i1, i2, j1, j2 in group:
            if len(raw) > room:
                break
            if tag == "equal":
                # The same raw line on both sides, but it may be a secret on
                # only one: a BEGIN marker the edit adds above existing
                # base64 lines makes them key material in the new file
                # alone. Redacted on either side is redacted.
                for i in range(i1, min(i2, i1 + room + 1)):
                    j = j1 + (i - i1)
                    if i not in a_touched and j in b_touched:
                        raw.append([" ", b_shown[j], True])
                    else:
                        raw.append([" ", a_shown[i], i in a_touched])
                continue
            if tag in ("replace", "delete"):
                raw.extend(["-", a_shown[i], i in a_touched] for i in range(i1, min(i2, i1 + room + 1)))
            if tag in ("replace", "insert"):
                raw.extend(["+", b_shown[j], j in b_touched] for j in range(j1, min(j2, j1 + room + 1)))
        if len(raw) > room:
            entry["truncated"] = True
        lines = [[tag, text] for tag, text, _hit in raw]
        # Only the lines the budget could take are shown; the rest stay in
        # `lines` as context for the second scan and are dropped unshown.
        emit = _fits(lines, room, budget.line_bytes_left)
        if emit < min(len(raw), room):
            entry["truncated"] = True
            budget.exhausted = True
        redacted += sum(1 for _tag, _text, hit in raw[:emit] if hit)
        redacted += _rescan_hunk(lines, emit)
        for line in lines:
            if not budget.take(line):
                entry["truncated"] = True
                break
            hunk["lines"].append(line)
        if hunk["lines"]:
            entry["hunks"].append(hunk)
        if budget.exhausted:
            entry["truncated"] = True
            break
    if redacted:
        entry["redacted_lines"] = redacted
    return entry


def build_preview(entries, max_files=MAX_FILES, budget=None):
    """A whole preview from a list of entry dicts: {"path", "old", "new"} plus
    optionally "hidden_reason", "note" (one extra note to attach) or
    "skip_content" (name the file and its status, no content read -- used for
    files past `max_files`, which the caller should not even load).

    The first `max_files` entries get a diff; the rest, up to
    MAX_LISTED_FILES and for as long as the byte budget lasts, are named with
    their status; anything beyond that is only counted (`files_omitted`).
    Every entry is charged to the same budget as the line text, so
    MAX_DIFF_BYTES bounds the whole preview, names included."""
    budget = budget if budget is not None else _Budget()
    files = []
    total_added = total_removed = 0
    truncated = False
    for index, item in enumerate(entries[:MAX_LISTED_FILES]):
        if item.get("skip_content") or index >= max_files:
            status = item.get("status") or _status(item.get("old"), item.get("new"))
            entry = {"path": item["path"], "status": status, "added": None,
                     "removed": None, "hunks": [], "truncated": True,
                     "hidden_reason": item.get("hidden_reason") or HIDDEN_NOT_PREVIEWED}
        else:
            entry = file_diff(item["path"], item.get("old"), item.get("new"), budget,
                              hidden_reason=item.get("hidden_reason"))
            if item.get("status"):
                entry["status"] = item["status"]
            if item.get("note"):
                # The caller's note explains the outcome itself ("the find
                # text is missing"), so a generic "identical" beside it is
                # noise.
                entry["notes"] = [item["note"]] + [
                    n for n in entry.get("notes", []) if n != NOTE_IDENTICAL]
        # Charged once complete, lines left out (they were charged as they
        # were emitted). Line text never reaches into LISTING_RESERVE, so a
        # diffed file's own entry fits; the name that does not fit is where
        # the listing stops, and everything from it on is only counted.
        if not budget.charge(dict(entry, hunks=[])):
            truncated = True
            break
        total_added += entry["added"] or 0
        total_removed += entry["removed"] or 0
        truncated = truncated or entry["truncated"]
        files.append(entry)
    omitted = len(entries) - len(files)
    return {"files": files, "added": total_added, "removed": total_removed,
            "truncated": truncated or omitted > 0, "files_total": len(entries),
            "files_omitted": omitted}


# ---------------------------------------------------------------------------
# Tool calls. Each of these mirrors one hearth_tools function step for step;
# see the module docstring.
# ---------------------------------------------------------------------------


def _rel(root, full):
    return os.path.relpath(full, root).replace(os.sep, "/")


def _resolve_for_write(workspace, path):
    """The tool's own path checks, in the tool's own order. Returns the
    resolved absolute path, or None where the tool would refuse."""
    if not isinstance(path, str) and path is not None:
        return None
    try:
        full = hearth_contain.safe_join(workspace, path)
    except ValueError:
        return None
    if hearth_tools._hearthignore_protected_error(workspace, full):
        return None
    if hearth_tools._ignored_error(workspace, full, path):
        return None
    return full


def _read_existing(full):
    """(text, hidden_reason). text is None with no reason when the file does
    not exist."""
    if not os.path.exists(hearth_paths.long_path(full)):
        return None, None
    try:
        return hearth_tools._read_text(full), None
    except UnicodeDecodeError:
        return "", HIDDEN_BINARY
    except OSError:
        return "", HIDDEN_UNREADABLE


def _preview_write_file(args, workspace):
    full = _resolve_for_write(workspace, args.get("path"))
    if full is None:
        return None
    if hearth_tools._case_conflict(args.get("path"), full):
        return None
    content = args.get("content", "")
    if not isinstance(content, str):
        return None
    if os.path.isdir(hearth_paths.long_path(full)):
        return None
    old, hidden = _read_existing(full)
    if old is not None and hidden is None:
        # tool_write_file converts the new content to CRLF when the file it
        # replaces is mostly CRLF; preview what will be written, not what
        # was asked for.
        if hearth_tools._dominant_newline(old) == "\r\n":
            content = content.replace("\r\n", "\n").replace("\n", "\r\n")
    root = os.path.realpath(workspace)
    item = {"path": _rel(root, full), "old": old, "new": content}
    if hidden:
        item["hidden_reason"] = hidden
    return build_preview([item])


def _preview_edit_file(args, workspace):
    full = _resolve_for_write(workspace, args.get("path"))
    if full is None:
        return None
    find = args.get("find")
    if not find or not isinstance(find, str):
        return None
    replace = args.get("replace", "")
    if not isinstance(replace, str):
        return None
    try:
        content = hearth_tools._read_text(full)
    except (OSError, UnicodeDecodeError):
        return None
    root = os.path.realpath(workspace)
    if content.count(find) == 0:
        return build_preview([{"path": _rel(root, full), "old": content, "new": content,
                               "status": "unchanged", "note": NOTE_FIND_MISSING}])
    if args.get("all"):
        new = content.replace(find, replace)
    else:
        new = content.replace(find, replace, 1)
    return build_preview([{"path": _rel(root, full), "old": content, "new": new}])


def _preview_replace_in_files(args, workspace):
    find = args.get("find")
    if not find or not isinstance(find, str):
        return None
    replace = args.get("replace", "")
    if not isinstance(replace, str):
        return None
    pattern = args.get("glob")
    try:
        base = hearth_contain.safe_join(workspace, args.get("path", "."))
    except ValueError:
        return None
    if hearth_tools._ignored_error(workspace, base, args.get("path", ".")):
        return None
    root = os.path.realpath(workspace)
    spec = hearth_contain.load_ignore(root)
    budget = _Budget()
    entries = []
    for dirpath, dirs, files in os.walk(base):
        hearth_contain.prune(dirpath, dirs, hearth_tools._TREE_SKIP, root=root, ignore_spec=spec)
        for fn in sorted(files):
            if budget.out_of_time():
                # The walk shares the preview's wall-clock belt. A tree too
                # big to search in that time gets no preview (the card shows
                # the raw arguments, as before this module existed) rather
                # than a count of matching files that is quietly short.
                return None
            if pattern and not hearth_tools._glob_match(fn, pattern):
                continue
            fp = os.path.join(dirpath, fn)
            try:
                hearth_contain.safe_join(root, os.path.relpath(fp, root))
            except ValueError:
                continue
            if spec.is_ignored(os.path.relpath(fp, root), is_dir=False):
                continue
            if hearth_tools._hearthignore_protected(root, fp):
                continue
            try:
                if os.path.getsize(hearth_paths.long_path(fp)) > 2_000_000:
                    continue
                content = hearth_tools._read_text(fp)
            except (OSError, UnicodeDecodeError):
                continue
            if find not in content:
                continue
            if len(entries) < MAX_FILES:
                entries.append({"path": _rel(root, fp), "old": content,
                                "new": content.replace(find, replace)})
            else:
                # Counted and named, not diffed: the walk still has to find
                # every file to say how many there are, but holding the
                # contents of hundreds of them for a preview that will not
                # show them is waste.
                entries.append({"path": _rel(root, fp), "status": "modified",
                                "skip_content": True})
    if not entries:
        return None
    return build_preview(entries, budget=budget)


_PREVIEWERS = {
    "write_file": _preview_write_file,
    "edit_file": _preview_edit_file,
    "replace_in_files": _preview_replace_in_files,
}


def preview_tool_call(tool, args, workspace):
    """The diff a gated write would make, or None when `tool` does not write
    files, or the tool itself would refuse the call. Raises only on a bug;
    use approval_preview() at a call site that must never fail."""
    previewer = _PREVIEWERS.get(tool)
    if previewer is None or not isinstance(args, dict) or not workspace:
        return None
    return previewer(args, workspace)


def approval_preview(tool, args, workspace):
    """preview_tool_call(), but never raises. A preview is an aid to the
    approval card, never a precondition for it: a bug or an unreadable tree
    here must leave the card exactly as it was before this module existed
    (raw arguments), not take the gate down with it."""
    try:
        return preview_tool_call(tool, args, workspace)
    except Exception:  # noqa: BLE001 - see the docstring
        return None


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _self_test():
    import shutil
    import tempfile

    base = os.path.realpath(tempfile.mkdtemp(prefix="hearth-diff-selftest-"))
    saved_data = os.environ.get("HEARTH_DATA_DIR")
    os.environ["HEARTH_DATA_DIR"] = os.path.join(base, "data")

    def write(rel, text):
        full = os.path.join(ws, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def tagged(preview, index=0):
        return [(line[0], line[1]) for hunk in preview["files"][index]["hunks"]
                for line in hunk["lines"]]

    try:
        ws = os.path.join(base, "ws")
        os.makedirs(ws)
        body = "".join("line {}\n".format(i) for i in range(1, 41))
        write("a.txt", body)

        # -- edit_file, first match only: one line out, one line in, and the
        #    surrounding context comes from the file. ----------------------
        p = preview_tool_call("edit_file", {"path": "a.txt", "find": "line 20\n",
                                            "replace": "line twenty\n"}, ws)
        f = p["files"][0]
        assert f["path"] == "a.txt" and f["status"] == "modified", f
        assert (f["added"], f["removed"]) == (1, 1), f
        lines = tagged(p)
        assert ("-", "line 20") in lines and ("+", "line twenty") in lines, lines
        assert (" ", "line 19") in lines and (" ", "line 21") in lines, lines
        hunk = f["hunks"][0]
        assert hunk["old_start"] == 20 - CONTEXT_LINES, hunk
        assert len([x for x in hunk["lines"] if x[0] == " "]) == 2 * CONTEXT_LINES, hunk
        assert p["added"] == 1 and p["removed"] == 1 and not p["truncated"], p

        # -- edit_file, first vs all: exactly the tool's own replace count. -
        write("rep.txt", "x = 1\ny = 2\nx = 1\n")
        first = preview_tool_call("edit_file", {"path": "rep.txt", "find": "x = 1",
                                                "replace": "x = 9"}, ws)
        every = preview_tool_call("edit_file", {"path": "rep.txt", "find": "x = 1",
                                                "replace": "x = 9", "all": True}, ws)
        assert first["files"][0]["added"] == 1, first
        assert every["files"][0]["added"] == 2, every

        # -- edit_file whose find text is absent: a note, never a diff. -----
        miss = preview_tool_call("edit_file", {"path": "a.txt", "find": "nope",
                                               "replace": "x"}, ws)
        mf = miss["files"][0]
        assert mf["status"] == "unchanged" and mf["hunks"] == [], mf
        assert NOTE_FIND_MISSING in mf["notes"], mf
        # ... and a call the tool would refuse outright gets no preview.
        assert preview_tool_call("edit_file", {"path": "a.txt", "find": ""}, ws) is None
        assert preview_tool_call("edit_file", {"path": "missing.txt", "find": "a"}, ws) is None

        # -- write_file to a new file: every line added. --------------------
        new = preview_tool_call("write_file", {"path": "sub/new.py",
                                               "content": "print(1)\nprint(2)\n"}, ws)
        nf = new["files"][0]
        assert nf["status"] == "added" and nf["path"] == "sub/new.py", nf
        assert tagged(new) == [("+", "print(1)"), ("+", "print(2)")], tagged(new)
        # The empty old side is "-0,0", as a unified diff writes a new file.
        assert (nf["hunks"][0]["old_start"], nf["hunks"][0]["old_count"]) == (0, 0), nf
        assert (nf["hunks"][0]["new_start"], nf["hunks"][0]["new_count"]) == (1, 2), nf

        # -- write_file over an existing file diffs against what is there. --
        over = preview_tool_call("write_file", {"path": "a.txt",
                                                "content": body.replace("line 5\n", "")}, ws)
        assert (over["files"][0]["added"], over["files"][0]["removed"]) == (0, 1), over
        same = preview_tool_call("write_file", {"path": "a.txt", "content": body}, ws)
        assert same["files"][0]["status"] == "unchanged", same
        assert NOTE_IDENTICAL in same["files"][0]["notes"], same
        # ... and over a file that is not text: named, no fake line counts.
        with open(os.path.join(ws, "blob.bin"), "wb") as fh:
            fh.write(b"\xff\xfe\x00binary")
        binp = preview_tool_call("write_file", {"path": "blob.bin", "content": "text\n"}, ws)
        bin_entry = binp["files"][0]
        assert bin_entry["hidden_reason"] == HIDDEN_BINARY and bin_entry["added"] is None, bin_entry

        # -- CRLF: the tool converts LF content to CRLF for a CRLF file, so
        #    the preview must not show every line changed; a real line-ending
        #    change (LF file, CRLF content) is a note, not 40 changed lines. -
        write("crlf.txt", "one\r\ntwo\r\nthree\r\n")
        crlf = preview_tool_call("write_file", {"path": "crlf.txt",
                                                "content": "one\nTWO\nthree\n"}, ws)
        cf = crlf["files"][0]
        assert (cf["added"], cf["removed"]) == (1, 1), cf
        assert "notes" not in cf, cf
        assert all("\r" not in line[1] for line in cf["hunks"][0]["lines"]), cf
        to_crlf = preview_tool_call("write_file", {"path": "a.txt",
                                                   "content": body.replace("\n", "\r\n")}, ws)
        tf = to_crlf["files"][0]
        assert (tf["added"], tf["removed"]) == (0, 0) and NOTE_TO_CRLF in tf["notes"], tf
        nl = preview_tool_call("write_file", {"path": "a.txt", "content": body.rstrip("\n")}, ws)
        assert NOTE_DROPS_FINAL_NEWLINE in nl["files"][0]["notes"], nl

        # -- caps: a huge new file stays inside the line and byte budget and
        #    says it was cut. ----------------------------------------------
        huge = "".join("row {} {}\n".format(i, "z" * 60) for i in range(5000))
        big = preview_tool_call("write_file", {"path": "big.txt", "content": huge}, ws)
        bf = big["files"][0]
        shown = sum(len(h["lines"]) for h in bf["hunks"])
        assert bf["truncated"] and big["truncated"], bf["truncated"]
        assert 0 < shown <= MAX_DIFF_LINES, shown
        # Slack is the top-level keys only; everything inside is charged.
        assert len(json.dumps(big)) <= MAX_DIFF_BYTES + 256, len(json.dumps(big))
        assert bf["added"] == 5000, bf["added"]
        long_line = preview_tool_call("write_file", {"path": "long.txt",
                                                     "content": "q" * 5000 + "\n"}, ws)
        ll = long_line["files"][0]["hunks"][0]["lines"][0]
        assert len(ll[1]) == MAX_LINE_CHARS and ll[2] == 5000 - MAX_LINE_CHARS, ll[2:]
        too_big = file_diff("x.txt", None, "a\n" * (MAX_INPUT_LINES + 5))
        assert too_big["hidden_reason"] == HIDDEN_TOO_LARGE and too_big["added"] is None, too_big

        # -- the byte cap covers names too: hundreds of long paths, a few of
        #    them diffed in full, still serialise within MAX_DIFF_BYTES, and
        #    the names that did not fit are counted, not dropped silently. --
        long_dir = "deep/" + "d" * 180 + "/"
        named = [{"path": long_dir + "f{:03d}.txt".format(i),
                  "old": huge if i < 3 else None, "new": huge.replace("z", "y") if i < 3 else None,
                  "status": "modified", "skip_content": i >= 3} for i in range(MAX_LISTED_FILES)]
        listed = build_preview(named)
        assert len(json.dumps(listed)) <= MAX_DIFF_BYTES + 256, len(json.dumps(listed))
        assert listed["files_omitted"] > 0 and listed["truncated"], listed["files_omitted"]
        assert len(listed["files"]) + listed["files_omitted"] == MAX_LISTED_FILES, listed["files_omitted"]
        # ... and the reserve kept back from line text means files past the
        # diffed ones are still named, not crowded out by the diffs.
        assert len(listed["files"]) > 3 + LISTING_RESERVE // 512, len(listed["files"])

        # -- time: the comparison is bounded in work, not just input size.
        #    Every other line changed is SequenceMatcher's slow case (it took
        #    minutes at this size before the work was bounded); it must now
        #    finish quickly and still give exact counts. -------------------
        n = MAX_INPUT_LINES - 10
        plain = "".join("line {}\n".format(i) for i in range(n))
        every_other = "".join(("line {}\n" if i % 2 else "changed {}\n").format(i) for i in range(n))
        write("interleaved.txt", plain)
        started = time.monotonic()
        inter = preview_tool_call("write_file", {"path": "interleaved.txt",
                                                 "content": every_other}, ws)
        took = time.monotonic() - started
        assert took < 5.0, "an interleaved change took {:.1f}s to preview".format(took)
        itf = inter["files"][0]
        assert (itf["added"], itf["removed"]) == (n // 2, n // 2), itf
        assert NOTE_COARSE not in itf.get("notes", []), itf
        assert len(json.dumps(inter)) <= MAX_DIFF_BYTES + 256, len(json.dumps(inter))
        # A stretch with no line unique to it (a repetitive file) is shown
        # as whole blocks, quickly, and says so; the blocks still rebuild
        # the new file exactly.
        rep_a = ["}" if i % 2 else "x" for i in range(20000)]
        rep_b = ["}" if i % 3 else "y" for i in range(20000)]
        started = time.monotonic()
        codes, coarse = _opcodes(rep_a, rep_b, _Budget())
        assert time.monotonic() - started < 5.0 and coarse, coarse
        rebuilt = []
        for tag, i1, i2, j1, j2 in codes:
            if tag == "equal":
                assert rep_a[i1:i2] == rep_b[j1:j2], (i1, j1)
            rebuilt.extend(rep_b[j1:j2])
        assert rebuilt == rep_b
        rep = file_diff("rep.txt", "\n".join(rep_a), "\n".join(rep_b))
        assert NOTE_COARSE in rep["notes"], rep.get("notes")
        # Out of time: nothing more is compared, the result is still correct.
        late = _Budget(seconds=-1)
        codes, coarse = _opcodes(["a", "b", "c", "d"], ["a", "x", "y", "d"], late)
        assert coarse and codes[0][0] == "equal" and codes[-1][0] == "equal", codes
        # And small inputs give the same counts difflib itself would.
        for old_l, new_l in ((list("abcabba"), list("cbabac")), (list("xaybzc"), list("abc")),
                             ([], list("ab")), (list("ab"), [])):
            mine = _opcodes(old_l, new_l, _Budget())[0]
            ref = difflib.SequenceMatcher(None, old_l, new_l).get_opcodes()
            changed = lambda cs: sum(i2 - i1 + j2 - j1 for t, i1, i2, j1, j2 in cs if t != "equal")
            assert changed(mine) <= changed(ref), (old_l, new_l, mine, ref)

        # -- secrets. Built at runtime like hearth_secrets' own fixtures, so
        #    this file never holds the matching string as a literal. --------
        key = hearth_secrets._build_real_secret_fixtures()["aws"]
        # (1) a secret in the NEW content is redacted in the + line.
        sec = preview_tool_call("write_file", {"path": "conf.ini",
                                               "content": "aws_access_key_id = " + key + "\n"}, ws)
        assert key not in json.dumps(sec), "raw key reached the preview"
        assert "[REDACTED:" in json.dumps(sec) and sec["files"][0]["redacted_lines"] == 1, sec
        # (2) a secret ONLY in an unchanged context line of the existing file:
        #     the edit never mentions it, the preview must still hide it.
        write("settings.py", "A = 1\nKEY = '" + key + "'\nB = 2\nC = 3\n")
        ctx = preview_tool_call("edit_file", {"path": "settings.py", "find": "C = 3",
                                              "replace": "C = 4"}, ws)
        assert key not in json.dumps(ctx), "a context line leaked the existing file's secret"
        assert any(line[0] == " " and "[REDACTED:" in line[1] for line in
                   ctx["files"][0]["hunks"][0]["lines"]), ctx
        # (3) a rotated key still shows as a change, not as nothing.
        key2 = hearth_secrets._build_real_secret_fixtures()["aws"]
        rot = preview_tool_call("edit_file", {"path": "settings.py", "find": key,
                                              "replace": key2}, ws)
        rf = rot["files"][0]
        assert (rf["added"], rf["removed"]) == (1, 1), rf
        assert key not in json.dumps(rot) and key2 not in json.dumps(rot), rot
        # (4) a secrets-pattern file is named, never shown.
        write(".env.local", "TOKEN=abc\n")
        env = preview_tool_call("write_file", {"path": ".env.local", "content": "TOKEN=def\n"}, ws)
        ef = env["files"][0]
        assert ef["hidden_reason"] == HIDDEN_SECRET_FILE and ef["hunks"] == [], ef
        assert "abc" not in json.dumps(env) and "def" not in json.dumps(env), env
        # (5) a multi-line PEM collapses to one marker without moving the
        #     line numbers of anything after it, and no line of key material
        #     reaches the preview.
        pem = hearth_secrets._build_real_secret_fixtures().get("pem")
        if pem:
            write("pem.txt", "head\n" + pem + "\nmiddle\ntail\n")
            pp = preview_tool_call("edit_file", {"path": "pem.txt", "find": "tail",
                                                 "replace": "TAIL"}, ws)
            dumped = json.dumps(pp)
            body_lines = [ln for ln in pem.split("\n") if not ln.startswith("-----")]
            assert body_lines and not any(ln in dumped for ln in body_lines), pp
            assert all(line[1] for line in pp["files"][0]["hunks"][0]["lines"]), pp
            # the changed line is still numbered where it really is
            want = ("head\n" + pem + "\nmiddle\n").count("\n") + 1
            changed = pp["files"][0]["hunks"][0]
            n_old = changed["old_start"]
            for line in changed["lines"]:
                if line[0] == "-":
                    break
                n_old += 1
            assert n_old == want, (n_old, want)
        # (6) the second pass: a secret in the middle of a file far larger
        #     than scan()'s window is still caught inside the hunk shown.
        filler = "".join("pad {}\n".format(i) for i in range(12000))
        write("huge.py", filler + "TOKEN = '" + key + "'\nkeep = 1\n" + filler)
        mid = preview_tool_call("edit_file", {"path": "huge.py", "find": "keep = 1",
                                              "replace": "keep = 2"}, ws)
        assert key not in json.dumps(mid), "the hunk re-scan missed a mid-file secret"
        # (6b) ... including when the hunk around it holds very long lines.
        #     Scanned at full length, the 70k and 30k lines push the key out
        #     of both of scan()'s windows over the hunk as well as over the
        #     file; the rescan must read the lines as they will be shown.
        filler = "".join("p{}\n".format(i) for i in range(20000))
        layout = (filler + "x" * 70000 + "\nTOKEN = '" + key + "'\n" + "y" * 30000
                  + "\nkeep = 1\n" + filler)
        write("long_lines.jsonl", layout)
        wide = preview_tool_call("edit_file", {"path": "long_lines.jsonl", "find": "keep = 1",
                                               "replace": "keep = 2"}, ws)
        assert key not in json.dumps(wide), "a long-line hunk leaked a context-line secret"
        wl = wide["files"][0]
        assert (wl["added"], wl["removed"]) == (1, 1), wl
        assert any("[REDACTED:" in line[1] for h in wl["hunks"] for line in h["lines"]), wl
        assert any(len(line) == 3 and line[2] == 70000 - MAX_LINE_CHARS
                   for h in wl["hunks"] for line in h["lines"]), "a long line lost its cut count"
        assert key not in json.dumps(file_diff("long_lines.jsonl", layout,
                                               layout.replace("keep = 1", "keep = 2")))
        # ... and the windows the rescan reads cover every character, each
        # inside scan()'s full-read size, overlapping where they meet.
        texts = ["z" * 1256] * 200
        joined = "\n".join(texts)
        wins = _rescan_windows(texts)
        assert all(len(c) <= RESCAN_WINDOW for _o, c in wins), [len(c) for _o, c in wins]
        assert all(joined[o:o + len(c)] == c for o, c in wins)
        assert wins[0][0] == 0 and wins[-1][0] + len(wins[-1][1]) == len(joined), wins[-1][0]
        for (o1, c1), (o2, _c2) in zip(wins, wins[1:]):
            assert o1 < o2 and o1 + len(c1) - o2 >= RESCAN_OVERLAP - 1257, (o1, o2)
        # (7) a finding too long for hearth_secrets to redact, sitting in an
        #     existing file's unchanged lines: the whole file is hidden, not
        #     shown because redact() would have left it in place.
        over = "\n".join(hearth_secrets._rand_alnum(64)
                         for _ in range(hearth_secrets.MAX_REDACT_SPAN // 64 + 40))
        write("big_key.txt", "top\n-----BEGIN PRIVATE KEY-----\n" + over
              + "\n-----END PRIVATE KEY-----\nbottom\n")
        ov = preview_tool_call("edit_file", {"path": "big_key.txt", "find": "bottom",
                                             "replace": "BOTTOM"}, ws)
        of = ov["files"][0]
        assert of["hidden_reason"] == HIDDEN_UNSAFE and of["hunks"] == [], of
        assert over.split("\n")[0] not in json.dumps(ov), "an oversized key body was shown"
        assert (of["added"], of["removed"]) == (1, 1), of
        # (8) overlapping findings: the second one's tail past the first is
        #     covered too, not left showing.
        merged_lines, merged_touched = _redact_keep_lines(
            "0123456789\nabc\n", [{"start": 2, "end": 6, "kind": "a"},
                                  {"start": 4, "end": 9, "kind": "b"}], 2)
        assert merged_lines == ["01[REDACTED:a]9", "abc"] and merged_touched == {0}, merged_lines

        # (9) a private key in the middle of a file far larger than scan()'s
        #     window, so scan() of the file never sees it, and the hunk shows
        #     only part of the block: next to its BEGIN line, next to its
        #     END line, both (two hunks, half the key each), and a BEGIN with
        #     no END at all. Not one line of key material may be shown.
        rows = [hearth_secrets._rand_alnum(64) for _ in range(26)]
        begin, end = "-----BEGIN RSA " + "PRIVATE KEY-----", "-----END RSA " + "PRIVATE KEY-----"
        pad = "".join("pad {}\n".format(i) for i in range(14000))
        mid_key = pad + "a = 1\n" + begin + "\n" + "\n".join(rows) + "\n" + end + "\nb = 1\n" + pad
        assert len(mid_key) > 250_000, len(mid_key)
        assert not any(f["kind"] == hearth_secrets.KIND_PRIVATE_KEY_PEM
                       for f in hearth_secrets.scan(mid_key)["findings"]), "layout no longer tests the gap"
        unterminated = pad + "a = 1\n" + begin + "\n" + "\n".join(rows) + "\nb = 1\n" + pad
        layouts = {
            "next to BEGIN": (mid_key, mid_key.replace("a = 1", "a = 2")),
            "next to END": (mid_key, mid_key.replace("b = 1", "b = 2")),
            "two edits": (mid_key, mid_key.replace("a = 1", "a = 2").replace("b = 1", "b = 2")),
            "unterminated": (unterminated, unterminated.replace("a = 1", "a = 2")),
            # and as the NEW side, a key the write itself brings in
            "written": (pad + "a = 1\n" + pad, mid_key.replace("a = 1", "a = 2")),
        }
        for name, (old_t, new_t) in layouts.items():
            res = file_diff("keys.txt", old_t, new_t)
            dumped = json.dumps(res)
            leaked = [r for r in rows if r[:24] in dumped]
            assert not leaked, "{}: {} key rows shown".format(name, len(leaked))
            rf = res["files"][0] if "files" in res else res
            assert rf["hunks"] and rf.get("redacted_lines"), (name, rf.get("redacted_lines"))
            # the markers stay, so the card still says a key is there
            assert begin in dumped or end in dumped, name
            # redaction kept every line in place: each hunk's own counts hold
            for h in rf["hunks"]:
                assert sum(1 for x in h["lines"] if x[0] != "+") == h["old_count"], (name, h)
                assert sum(1 for x in h["lines"] if x[0] != "-") == h["new_count"], (name, h)
        # (9b) a key kept on one line with escaped newlines, as JSON stores one.
        one_line = ('"private_key": "' + begin + "\\n" + "\\n".join(rows) + "\\n" + end + '\\n",')
        js_old = pad + "{\n" + one_line + "\n\"id\": 1\n}\n" + pad
        js = file_diff("svc.json", js_old, js_old.replace('"id": 1', '"id": 2'))
        assert not any(r[:24] in json.dumps(js) for r in rows), "a one-line JSON key was shown"
        # (9c) only key material is covered: a line inside a block that is not
        #     base64 stays visible (a block cannot hide a command from the
        #     card), and a source file that only quotes a BEGIN marker keeps
        #     its later lines.
        trap = (pad + "a = 1\n" + begin + "\n" + rows[0] + "\nrm -rf /tmp/victim\n" + rows[1]
                + "\n" + end + "\n" + pad)
        tr = json.dumps(file_diff("trap.txt", trap, trap.replace("a = 1", "a = 2")))
        assert "rm -rf /tmp/victim" in tr and rows[0] not in tr and rows[1] not in tr, tr[:400]
        quoted = 'x = "' + begin + '\\n"\npass\nreturn\nkeep = 1\n'
        qd = file_diff("quote.py", quoted, quoted.replace("keep = 1", "keep = 2"))
        assert [ln[1] for h in qd["hunks"] for ln in h["lines"]][1:3] == ["pass", "return"], qd
        assert "redacted_lines" not in qd, qd
        # (9d) the write adds a BEGIN above base64 lines already on disk: the
        #     unchanged lines are key material in the new file only, and are
        #     redacted though the old side would have shown them.
        bare = pad + "a = 1\n" + "\n".join(rows) + "\n" + pad
        added = file_diff("bare.txt", bare, bare.replace("a = 1\n", "a = 1\n" + begin + "\n"))
        assert not any(r[:24] in json.dumps(added) for r in rows), "context from the old side leaked"
        # (9e) the whole-file pass stays quick on the largest input allowed:
        #     an unterminated BEGIN followed by key material to the end.
        huge_key = begin + "\n" + "\n".join(hearth_secrets._rand_alnum(64) for _ in range(29000)) + "\n"
        assert len(huge_key) < MAX_INPUT_CHARS
        started = time.monotonic()
        hk = file_diff("huge.key.txt", huge_key, huge_key.replace(begin, begin + "\nx"))
        assert time.monotonic() - started < 5.0, time.monotonic() - started
        assert huge_key.split("\n")[5] not in json.dumps(hk) and hk["hunks"], hk.get("hidden_reason")

        # (10) a long token starting just before the display cut, in the
        #     middle of a large file: scanned cut at the usual length it would
        #     not match, and scan() calls any JWT that long a placeholder, so
        #     neither half may be relied on. None of it may be shown.
        jwt = ("eyJ" + hearth_secrets._rand_alnum(200) + "." + hearth_secrets._rand_alnum(200)
               + "." + hearth_secrets._rand_alnum(40))
        assert not hearth_secrets.scan(jwt)["findings"], "scan() now finds long JWTs; revisit"
        for lead in (MAX_LINE_CHARS - 9, MAX_LINE_CHARS - 200, 0):
            jl = pad + "x" * (lead - 1) + " " + jwt + " tail\nkeep = 1\n" + pad if lead else (
                pad + jwt + "\nkeep = 1\n" + pad)
            jd = json.dumps(file_diff("tok.txt", jl, jl.replace("keep = 1", "keep = 2")))
            assert jwt[:9] not in jd and jwt[-20:] not in jd, "a long JWT was shown (lead {})".format(lead)
        long_sk = "sk-" + hearth_secrets._rand_alnum(300)
        sk_file = pad + "OPENAI = '" + long_sk + "'\nkeep = 1\n" + pad
        skd = json.dumps(file_diff("sk.py", sk_file, sk_file.replace("keep = 1", "keep = 2")))
        assert long_sk[:30] not in skd, "a long sk- key was shown"
        # A long token judged in slices is still a placeholder when it is one.
        assert _token_is_placeholder("eyJ" + "a" * 300)

        # -- .hearthignore and containment: the tool would refuse, so there
        #    is no preview at all. ------------------------------------------
        write(".hearthignore", "private/\n")
        write("private/x.txt", "hidden\n")
        assert preview_tool_call("write_file", {"path": "private/x.txt", "content": "y"}, ws) is None
        assert preview_tool_call("edit_file", {"path": "private/x.txt", "find": "hidden",
                                               "replace": "y"}, ws) is None
        assert preview_tool_call("write_file", {"path": ".hearthignore", "content": ""}, ws) is None
        assert preview_tool_call("write_file", {"path": "../escape.txt", "content": "y"}, ws) is None
        assert preview_tool_call("write_file", {"path": "a.txt", "content": 5}, ws) is None
        assert preview_tool_call("read_file", {"path": "a.txt"}, ws) is None
        assert approval_preview("write_file", "not a dict", ws) is None

        # -- replace_in_files: per file, skipping what the tool skips, and
        #    the file cap names the rest without diffing them. --------------
        write("r/one.txt", "alpha beta\n")
        write("r/two.txt", "beta gamma\n")
        write("r/three.md", "beta\n")
        write("private/beta.txt", "beta\n")
        multi = preview_tool_call("replace_in_files", {"path": "r", "find": "beta",
                                                       "replace": "BETA", "glob": "*.txt"}, ws)
        assert [f["path"] for f in multi["files"]] == ["r/one.txt", "r/two.txt"], multi
        assert multi["added"] == 2 and multi["files_total"] == 2, multi
        allws = preview_tool_call("replace_in_files", {"find": "beta", "replace": "B"}, ws)
        assert "private/beta.txt" not in [f["path"] for f in allws["files"]], allws
        assert preview_tool_call("replace_in_files", {"find": "zzz-not-here"}, ws) is None
        # A tree that cannot be searched within the time belt gets no
        # preview at all (raw arguments), never a quietly short file count.
        global PREVIEW_SECONDS
        saved_seconds, PREVIEW_SECONDS = PREVIEW_SECONDS, -1.0
        try:
            assert preview_tool_call("replace_in_files", {"path": "r", "find": "beta",
                                                          "replace": "B"}, ws) is None
        finally:
            PREVIEW_SECONDS = saved_seconds
        for i in range(MAX_FILES + 3):
            write("many/f{:02d}.txt".format(i), "needle\n")
        many = preview_tool_call("replace_in_files", {"path": "many", "find": "needle",
                                                      "replace": "pin"}, ws)
        assert many["files_total"] == MAX_FILES + 3 and many["truncated"], many
        skipped = [f for f in many["files"] if f.get("hidden_reason") == HIDDEN_NOT_PREVIEWED]
        assert len(skipped) == 3 and all(f["added"] is None for f in skipped), skipped

        # -- the event stays small: the whole preview of a huge write is
        #    JSON-serialisable and bounded. --------------------------------
        json.dumps(big)
    finally:
        if saved_data is None:
            os.environ.pop("HEARTH_DATA_DIR", None)
        else:
            os.environ["HEARTH_DATA_DIR"] = saved_data
        shutil.rmtree(base, ignore_errors=True)

    print("hearth-diff self-test OK")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    print("usage: hearth_diff.py --self-test", file=sys.stderr)
    sys.exit(2)
