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
  - hearth_secrets scans a bounded head-and-tail window of a large file. The
    hunks actually shown are therefore scanned again on their own, which
    covers a secret sitting in the unscanned middle of a big file as long as
    it lies within one hunk.

SIZE. The approval event is persisted (session_state keeps a tail of recent
events) and replayed over SSE on every reconnect, so it must stay small no
matter how large the write is. A preview is capped at MAX_DIFF_LINES emitted
lines and MAX_DIFF_BYTES of serialised text in total, a single line at
MAX_LINE_CHARS, and an input file at MAX_INPUT_CHARS / MAX_INPUT_LINES (past
which SequenceMatcher's worst case stops being cheap). Every cap that bites
is reported -- `truncated`, `cut` on a line, `hidden_reason` on a file --
never applied silently.

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

tag is "+", "-" or " ". Line numbers are 1-based. `added`/`removed` count the
whole change even when the lines shown were cut short; they are None for a
file whose content could not be compared at all.

This is a preview, computed at the moment the card is raised. A file that
changes between the card appearing and the click landing is written by the
tool as it then is; the card says what was true when it was drawn.

Standard library only. No network. Reads files; never writes any.
"""

import difflib
import fnmatch
import json
import os
import sys

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

HIDDEN_SECRET_FILE = ("this looks like a secrets file (.env, a key, credentials), "
                      "so its content is not shown")
HIDDEN_IGNORED = "excluded by .hearthignore, so its content is not shown"
HIDDEN_BINARY = "not UTF-8 text (a binary file?), so there is nothing to show line by line"
HIDDEN_TOO_LARGE = "too large to preview line by line"
HIDDEN_UNREADABLE = "the file on disk could not be read, so the change cannot be shown"
HIDDEN_NOT_PREVIEWED = "not previewed: this change touches more files than one preview shows"
HIDDEN_BUDGET = "not shown: the preview reached its size limit before this file"

NOTE_FIND_MISSING = ("the find text does not appear in this file, so the edit will "
                     "fail and change nothing")
NOTE_IDENTICAL = "the new content is identical to what is on disk"
NOTE_TO_CRLF = "line endings change from LF to CRLF on every line"
NOTE_TO_LF = "line endings change from CRLF to LF on every line"
NOTE_ADDS_FINAL_NEWLINE = "adds a newline at the end of the file"
NOTE_DROPS_FINAL_NEWLINE = "removes the newline at the end of the file"


class _Budget:
    """The shared line and byte allowance for one whole preview. Charged per
    emitted line at its JSON-encoded size, which is what it actually costs in
    the persisted event (a non-ASCII character serialises as \\uXXXX)."""

    def __init__(self, max_lines=MAX_DIFF_LINES, max_bytes=MAX_DIFF_BYTES):
        self.lines_left = max_lines
        self.bytes_left = max_bytes
        self.exhausted = False

    def take(self, text):
        cost = len(json.dumps(text)) + 8
        if self.lines_left <= 0 or cost > self.bytes_left:
            self.exhausted = True
            return False
        self.lines_left -= 1
        self.bytes_left -= cost
        return True


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
    (None, None) if the line count could not be kept, which the caller treats
    as "do not show this text" rather than risk misaligned lines."""
    spans = sorted(
        (f["start"], f["end"], f["kind"]) for f in findings
        if 0 <= f["start"] < f["end"] <= len(norm))
    out = []
    touched = set()
    cursor = 0
    for start, end, kind in spans:
        if start < cursor:
            continue  # overlapping finding; the earlier span already covers it
        if end - start > hearth_secrets.MAX_REDACT_SPAN:
            continue  # same rule as hearth_secrets.redact: never hide an unbounded run
        out.append(norm[cursor:start])
        first_line = norm.count("\n", 0, start)
        breaks = norm.count("\n", start, end)
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


def _redacted_lines(norm, line_count):
    """_redact_keep_lines over hearth_secrets.scan(norm)'s findings. Returns
    (None, set()) when there is nothing to redact, so the caller can keep
    using the lines it already has."""
    findings = hearth_secrets.scan(norm)["findings"]
    if not findings:
        return None, set()
    return _redact_keep_lines(norm, findings, line_count)


def _rescan_hunk(lines):
    """Second pass over the lines one hunk will actually show. See the module
    docstring: covers the middle of a file too large for scan()'s window.
    Mutates `lines` ([tag, text] pairs) in place; returns how many changed."""
    joined = "\n".join(entry[1] for entry in lines)
    findings = hearth_secrets.scan(joined)["findings"]
    if not findings:
        return 0
    redacted, touched = _redact_keep_lines(joined, findings, len(lines))
    if redacted is None:
        # Could not keep the alignment: blank every line rather than show any.
        for entry in lines:
            entry[1] = "[REDACTED]"
        return len(lines)
    for i in touched:
        if i < len(lines):
            lines[i][1] = redacted[i]
    return len(touched)


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


def _counts(a, b):
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def file_diff(path, old, new, budget=None, context=CONTEXT_LINES, hidden_reason=None):
    """One file's entry in a preview. `old` is the current text (None when the
    file does not exist yet), `new` the text it will have (None when it will
    be deleted). `hidden_reason` names the file without showing content.

    `path` is display text the caller has already made workspace-relative;
    nothing here touches the filesystem."""
    budget = budget if budget is not None else _Budget()
    entry = {"path": path, "status": _status(old, new), "added": None, "removed": None,
             "hunks": [], "truncated": False}
    old_text = "" if old is None else old
    new_text = "" if new is None else new

    too_big = (len(old_text) > MAX_INPUT_CHARS or len(new_text) > MAX_INPUT_CHARS
               or old_text.count("\n") > MAX_INPUT_LINES or new_text.count("\n") > MAX_INPUT_LINES)
    if too_big:
        entry["hidden_reason"] = hidden_reason or HIDDEN_TOO_LARGE
        return entry

    old_norm, a, old_final = _split(old_text)
    new_norm, b, new_final = _split(new_text)
    entry["added"], entry["removed"] = _counts(a, b)
    notes = _eol_notes(old, new, old_final, new_final)
    if entry["status"] == "unchanged":
        notes.append(NOTE_IDENTICAL)
    if notes:
        entry["notes"] = notes

    if hidden_reason is None and is_secret_path(path):
        hidden_reason = HIDDEN_SECRET_FILE
    if hidden_reason is not None:
        entry["hidden_reason"] = hidden_reason
        return entry
    if budget.exhausted:
        entry["truncated"] = True
        entry["hidden_reason"] = HIDDEN_BUDGET
        return entry

    a_shown, a_touched = _redacted_lines(old_norm, len(a))
    b_shown, b_touched = _redacted_lines(new_norm, len(b))
    if a_touched is None or b_touched is None:
        entry["hidden_reason"] = HIDDEN_SECRET_FILE
        return entry
    a_shown = a_shown if a_shown is not None else a
    b_shown = b_shown if b_shown is not None else b

    redacted = 0
    matcher = difflib.SequenceMatcher(None, a, b)
    for group in matcher.get_grouped_opcodes(context):
        first, last = group[0], group[-1]
        hunk = {"old_start": first[1] + 1, "old_count": last[2] - first[1],
                "new_start": first[3] + 1, "new_count": last[4] - first[3], "lines": []}
        # Text comes from the redacted copies at the same indexes; see the
        # module docstring for why the opcodes themselves come from the raw
        # lines.
        raw = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                raw.extend([" ", a_shown[i], i in a_touched] for i in range(i1, i2))
                continue
            if tag in ("replace", "delete"):
                raw.extend(["-", a_shown[i], i in a_touched] for i in range(i1, i2))
            if tag in ("replace", "insert"):
                raw.extend(["+", b_shown[j], j in b_touched] for j in range(j1, j2))
        if len(raw) > budget.lines_left:
            raw = raw[:max(budget.lines_left, 0)]
            entry["truncated"] = True
        lines = [[tag, text] for tag, text, _hit in raw]
        redacted += sum(1 for _tag, _text, hit in raw if hit)
        redacted += _rescan_hunk(lines)
        for line in lines:
            text = line[1]
            if len(text) > MAX_LINE_CHARS:
                line[1] = text[:MAX_LINE_CHARS]
                line.append(len(text) - MAX_LINE_CHARS)
            if not budget.take(line[1]):
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
    MAX_LISTED_FILES, are named with their status; anything beyond that is
    only counted."""
    budget = budget if budget is not None else _Budget()
    files = []
    total_added = total_removed = 0
    truncated = False
    for index, item in enumerate(entries[:MAX_LISTED_FILES]):
        if item.get("skip_content") or index >= max_files:
            status = item.get("status") or _status(item.get("old"), item.get("new"))
            files.append({"path": item["path"], "status": status, "added": None,
                          "removed": None, "hunks": [], "truncated": True,
                          "hidden_reason": item.get("hidden_reason") or HIDDEN_NOT_PREVIEWED})
            truncated = True
            continue
        entry = file_diff(item["path"], item.get("old"), item.get("new"), budget,
                          hidden_reason=item.get("hidden_reason"))
        if item.get("status"):
            entry["status"] = item["status"]
        if item.get("note"):
            # The caller's note explains the outcome itself ("the find text
            # is missing"), so a generic "identical" beside it is noise.
            entry["notes"] = [item["note"]] + [
                n for n in entry.get("notes", []) if n != NOTE_IDENTICAL]
        total_added += entry["added"] or 0
        total_removed += entry["removed"] or 0
        truncated = truncated or entry["truncated"]
        files.append(entry)
    omitted = max(0, len(entries) - MAX_LISTED_FILES)
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
    entries = []
    for dirpath, dirs, files in os.walk(base):
        hearth_contain.prune(dirpath, dirs, hearth_tools._TREE_SKIP, root=root, ignore_spec=spec)
        for fn in sorted(files):
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
    return build_preview(entries)


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
        assert nf["hunks"][0]["new_start"] == 1, nf

        # -- write_file over an existing file diffs against what is there. --
        over = preview_tool_call("write_file", {"path": "a.txt",
                                                "content": body.replace("line 5\n", "")}, ws)
        assert (over["files"][0]["added"], over["files"][0]["removed"]) == (0, 1), over
        same = preview_tool_call("write_file", {"path": "a.txt", "content": body}, ws)
        assert same["files"][0]["status"] == "unchanged", same
        assert NOTE_IDENTICAL in same["files"][0]["notes"], same

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
        assert len(json.dumps(big)) < MAX_DIFF_BYTES + 4096, len(json.dumps(big))
        assert bf["added"] == 5000, bf["added"]
        long_line = preview_tool_call("write_file", {"path": "long.txt",
                                                     "content": "q" * 5000 + "\n"}, ws)
        ll = long_line["files"][0]["hunks"][0]["lines"][0]
        assert len(ll[1]) == MAX_LINE_CHARS and ll[2] == 5000 - MAX_LINE_CHARS, ll[2:]
        too_big = file_diff("x.txt", None, "a\n" * (MAX_INPUT_LINES + 5))
        assert too_big["hidden_reason"] == HIDDEN_TOO_LARGE and too_big["added"] is None, too_big

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
