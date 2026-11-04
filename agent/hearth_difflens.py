#!/usr/bin/env python3
"""hearth difflens: risk-scored diff review.

An approval prompt that shows a unified diff makes the human do the
dangerous part of the work: scanning forty green lines for the one that
adds a subprocess call, swaps an endpoint, or quietly deletes the test
that would have caught it. The diff is ordered by file position, which is
not the order anyone cares about. This module reorders it by risk, so the
prompt can lead with the worst thing in the change instead of the first
thing.

Two entry points, both returning a plain dict so a caller can embed the
result in a prompt, a log line, or a CI gate:

  review(old_text, new_text, path="")   two in-memory versions of one file
  review_patch(unified_diff_text)       a whole patch, many files

The dict carries lines_added / lines_removed / lines_changed, the cosmetic
classification (pure_whitespace, pure_comment), the net size delta in
lines and bytes, a `risk` score in 0..100, and `flags`: a list, worst
first, each with a name, a severity, and one human sentence that can be
printed straight into an approval prompt.

Flags detected: mass_deletion (more than DELETE_LINES_LIMIT lines gone, or
more than half the file), credential_change, url_change, dangerous_call
(a new eval / exec / subprocess / os.system / popen / shell=True),
test_weakened (an assert or a whole test function removed),
shebang_or_mode, dependency_change, license_change, giant_line (a single
inserted line long enough to be minified or generated), and file_fanout
(a patch touching an unusually high number of distinct files).

Weighting, and why it is monotonic
----------------------------------
risk = min(100, churn_term + sum(severity weight of each distinct flag))

  severity weight   low 6, medium 15, high 30, critical 45
  churn_term        min(12, round(2 * sqrt(lines_added + lines_removed)))
  cosmetic change   churn_term is forced to 0

Every term is non-negative and nothing subtracts, so for a fixed
classification the score never falls when a flag is added or when churn
grows: a strictly worse diff always scores at least as high. The churn
term is deliberately capped at 12 and square-rooted because size is the
weakest signal in a diff. A 900-line vendored-library import is a boring
change; a 3-line change that shells out is not. That ordering is asserted
directly in --self-test: a whitespace reformat that touches every line
scores 0, and a three-line change adding `subprocess` outranks it.

What this deliberately does NOT do
----------------------------------
  * No AST, no language awareness. It is line and regex based, which is
    why it works on a diff of any language, and why it will sometimes
    flag a string that merely looks like a secret. For a review aid,
    false positives are cheap and silence is expensive.
  * No judgement about intent, and no verdict. It ranks and explains; the
    human still decides. Nothing here blocks, rewrites, or fixes a diff.
  * No I/O beyond reading the patch the CLI was handed. No git, no
    network, no config file, no repository lookup. Scoring is a pure
    function of the text given, so the same diff always scores the same.
  * No attempt to be a secret scanner or a vulnerability scanner. It has
    no entropy model and no rule feed, and it is not a substitute for
    either.
  * No third-party imports. Standard library only.

Patch mode caveat: a unified diff shows only its hunks, so the
"more than half the file" test is evaluated against the old-side lines
visible in the patch, not against the real file length.
"""

import argparse
import collections
import difflib
import json
import math
import re
import sys

# Thresholds. These are the "N" in the flag descriptions above.
DELETE_LINES_LIMIT = 25     # more removed lines than this is a mass deletion
DELETE_RATIO = 0.5          # or more than this share of the visible old file
RATIO_MIN_LINES = 4         # ratio test is noise on a file shorter than this
GIANT_LINE_CHARS = 400      # one inserted line this long reads as generated
HUGE_LINE_CHARS = 2000      # and this long is certainly not hand written
FILE_FANOUT_LIMIT = 10      # distinct files above this is an unusual blast
FILE_FANOUT_HIGH = 25
CHURN_CAP = 12              # size contributes at most this much risk
DEFAULT_THRESHOLD = 60      # CLI exits nonzero above this

SEVERITY_WEIGHT = {"low": 6, "medium": 15, "high": 30, "critical": 45}

COMMENT_RE = re.compile(r"^\s*(#|//|;|--|/\*|\*/|\*|<!--)")

CRED_RE = re.compile(
    r"(?i)(password|passwd|secret|api[-_ ]?key|apikey|auth[-_]?token"
    r"|access[-_]?token|refresh[-_]?token|bearer\s+[A-Za-z0-9._\-]{8}"
    r"|private[-_]?key|client[-_]?secret|credential|AKIA[0-9A-Z]{12}"
    r"|BEGIN\s+[A-Z ]*PRIVATE KEY)")

URL_RE = re.compile(
    r"(?i)\b(https?|ftp|ftps|ssh|postgres(?:ql)?|mysql|redis|mongodb"
    r"|amqp|s3)://")

DANGER_PATTERNS = (
    ("eval()", re.compile(r"(?<![\w.])eval\s*\(")),
    ("exec()", re.compile(r"(?<![\w.])exec\s*\(")),
    ("os.system()", re.compile(r"os\.system\s*\(")),
    ("subprocess", re.compile(r"(?<![\w.])subprocess\b")),
    ("popen", re.compile(r"(?i)(?<![\w.])(os\.)?popen\s*\(")),
    ("shell=True", re.compile(r"shell\s*=\s*True")),
    ("__import__()", re.compile(r"__import__\s*\(")),
    ("child_process", re.compile(r"child_process")),
)

ASSERT_RE = re.compile(r"^\s*(assert\b|self\.assert\w*\s*\(|expect\s*\()")
TEST_DEF_RE = re.compile(
    r"^\s*((async\s+)?def\s+test\w*|it\s*\(|test\s*\(|describe\s*\()")

SHEBANG_RE = re.compile(r"^#!")
PERM_RE = re.compile(r"(?i)(chmod|os\.chmod|stat\.S_I|0o[0-7]{3}\b)")

DEP_FILES = (
    "requirements.txt", "requirements-dev.txt", "constraints.txt",
    "pyproject.toml", "setup.py", "setup.cfg", "pipfile", "pipfile.lock",
    "poetry.lock", "package.json", "package-lock.json", "yarn.lock",
    "pnpm-lock.yaml", "go.mod", "go.sum", "cargo.toml", "cargo.lock",
    "gemfile", "gemfile.lock", "composer.json",
)
# Outside a manifest only an explicit install or declaration counts. A bare
# ">=" in source code is an ordinary comparison, not a version pin, and
# flagging it would make this whole flag worthless.
DEP_LINE_RE = re.compile(
    r"(?i)(pip3?\s+install|npm\s+(i|install|add)\b|yarn\s+add\b"
    r"|pnpm\s+(add|install)\b|poetry\s+add\b|cargo\s+add\b|go\s+get\b"
    r"|install_requires|dependencies\s*=\s*\[|\"dependencies\"\s*:"
    r"|apt(-get)?\s+install)")
PIN_RE = re.compile(
    r"^\s*[\"']?[A-Za-z0-9_.\-\[\]]+[\"']?\s*(==|~=|>=|<=|!=|\^|~)\s*v?\d")

LICENSE_RE = re.compile(
    r"(?i)(copyright\s|\(c\)\s*\d{4}|spdx-license-identifier"
    r"|licen[sc]ed under|mit license|apache license|gnu general public"
    r"|bsd licen[sc]e|all rights reserved)")

MODE_RE = re.compile(r"^(old|new) mode \d+\s*$")


def _collapse(line):
    """Whitespace-insensitive form of a line, for telling a reindent apart
    from a real edit."""
    return re.sub(r"\s+", "", line)


def _is_comment(line):
    return not line.strip() or bool(COMMENT_RE.match(line))


def _diff_lines(old_lines, new_lines):
    """Return (added, removed, paired_changed) for two line lists.

    Lines inside a replaced block are counted in added and in removed, and
    the paired count is reported separately as lines_changed, so the three
    numbers describe the same edit from three angles rather than
    partitioning it. autojunk is off so the result does not depend on how
    many repeated lines the file happens to contain."""
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines,
                                      autojunk=False)
    added, removed, changed = [], [], 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "replace":
            removed.extend(old_lines[i1:i2])
            added.extend(new_lines[j1:j2])
            changed += min(i2 - i1, j2 - j1)
        elif tag == "delete":
            removed.extend(old_lines[i1:i2])
        elif tag == "insert":
            added.extend(new_lines[j1:j2])
    return added, removed, changed


def _effective(added, removed):
    """Drop added/removed lines that are the same line with different
    whitespace, so a reformat does not read as content churn."""
    pool = collections.Counter(_collapse(line) for line in removed)
    eff_added = []
    for line in added:
        key = _collapse(line)
        if pool[key] > 0:
            pool[key] -= 1
        else:
            eff_added.append(line)
    pool = collections.Counter(_collapse(line) for line in added)
    eff_removed = []
    for line in removed:
        key = _collapse(line)
        if pool[key] > 0:
            pool[key] -= 1
        else:
            eff_removed.append(line)
    return eff_added, eff_removed


def _flag(name, severity, message, path=""):
    return {"name": name, "severity": severity, "message": message,
            "path": path}


def _basename(path):
    return (path or "").replace("\\", "/").rsplit("/", 1)[-1].lower()


def _matches(lines, pattern):
    return [line for line in lines if pattern.search(line)]


def _collect_flags(eff_added, eff_removed, path, old_total, mode_change):
    """Build the flag list for one file. At most one flag per name, holding
    the worst severity found, so the score cannot be inflated by repetition
    of the same finding."""
    flags = []
    touched = eff_added + eff_removed
    code_added = [line for line in eff_added if not _is_comment(line)]

    # mass deletion
    gone = len(eff_removed)
    ratio = (float(gone) / old_total) if old_total else 0.0
    big_count = gone > DELETE_LINES_LIMIT
    big_ratio = old_total >= RATIO_MIN_LINES and ratio > DELETE_RATIO
    if big_count or big_ratio:
        if big_count and big_ratio:
            sev = "critical"
        else:
            sev = "high"
        flags.append(_flag(
            "mass_deletion", sev,
            "Deletes {} lines ({:.0f}% of the file as shown); check nothing "
            "load bearing goes with them.".format(gone, ratio * 100), path))

    # credentials
    hits = _matches(touched, CRED_RE)
    if hits:
        flags.append(_flag(
            "credential_change", "critical",
            "Touches {} line(s) that look like a credential or key, "
            "starting with: {}".format(len(hits), _snippet(hits[0])), path))

    # URLs and endpoints
    hits = _matches(touched, URL_RE)
    if hits:
        flags.append(_flag(
            "url_change", "medium",
            "Changes {} line(s) containing a URL or endpoint, starting "
            "with: {}".format(len(hits), _snippet(hits[0])), path))

    # newly introduced execution
    found = []
    for label, pattern in DANGER_PATTERNS:
        if any(pattern.search(line) for line in code_added):
            found.append(label)
    if found:
        flags.append(_flag(
            "dangerous_call", "critical",
            "Adds code that runs other code: {}.".format(", ".join(found)),
            path))

    # weakened tests
    lost_asserts = len(_matches(eff_removed, ASSERT_RE))
    lost_tests = len(_matches(eff_removed, TEST_DEF_RE))
    if lost_asserts or lost_tests:
        sev = "high" if lost_tests else "medium"
        flags.append(_flag(
            "test_weakened", sev,
            "Removes {} assertion(s) and {} test definition(s); the change "
            "loses its own safety net.".format(lost_asserts, lost_tests),
            path))

    # shebang or file mode
    shebangs = len(_matches(touched, SHEBANG_RE))
    perms = len(_matches(touched, PERM_RE))
    if mode_change or shebangs or perms:
        flags.append(_flag(
            "shebang_or_mode", "medium",
            "Changes how the file is executed: {} shebang line(s), {} "
            "permission line(s){}.".format(
                shebangs, perms,
                ", plus a git file mode change" if mode_change else ""),
            path))

    # dependencies and version pins
    is_manifest = (_basename(path) in DEP_FILES
                   or "requirements" in _basename(path))
    dep_hits = _matches(touched, DEP_LINE_RE)
    if is_manifest:
        dep_hits = dep_hits + _matches(touched, PIN_RE)
    if (is_manifest and touched) or dep_hits:
        flags.append(_flag(
            "dependency_change", "medium",
            "Changes dependencies or version pins ({} line(s)); new code "
            "arrives that nobody in this diff wrote.".format(
                len(dep_hits) or len(touched)), path))

    # license header
    hits = _matches(touched, LICENSE_RE)
    if hits:
        flags.append(_flag(
            "license_change", "high",
            "Edits a license or copyright line: {}".format(
                _snippet(hits[0])), path))

    # minified or generated insertion
    longest = max([len(line) for line in eff_added] or [0])
    if longest > GIANT_LINE_CHARS:
        sev = "high" if longest > HUGE_LINE_CHARS else "medium"
        flags.append(_flag(
            "giant_line", sev,
            "Inserts a single line of {} characters, too long to have been "
            "reviewed by eye.".format(longest), path))

    return flags


def _snippet(line, width=60):
    text = " ".join(line.split())
    if len(text) > width:
        text = text[:width - 3] + "..."
    return text


def _score(churn, cosmetic, flags):
    """Additive, clipped, monotonic. See the module docstring."""
    base = 0
    if not cosmetic:
        base = min(CHURN_CAP, int(round(2.0 * math.sqrt(churn))))
    worst = {}
    for flag in flags:
        weight = SEVERITY_WEIGHT[flag["severity"]]
        if weight > worst.get(flag["name"], -1):
            worst[flag["name"]] = weight
    return min(100, base + sum(worst.values()))


def _sort_flags(flags):
    return sorted(flags, key=lambda f: (-SEVERITY_WEIGHT[f["severity"]],
                                        f["name"], f["path"]))


def _review_lines(old_lines, new_lines, path, size_delta, old_total,
                  mode_change=False):
    added, removed, changed = _diff_lines(old_lines, new_lines)
    eff_added, eff_removed = _effective(added, removed)
    touched = bool(added or removed)
    same_text = ("".join(_collapse(line) for line in old_lines)
                 == "".join(_collapse(line) for line in new_lines))
    whitespace_only = touched and not eff_added and not eff_removed \
        and same_text
    comment_only = (not whitespace_only
                    and bool(eff_added or eff_removed)
                    and all(_is_comment(line)
                            for line in eff_added + eff_removed))
    cosmetic = whitespace_only or comment_only
    flags = _collect_flags(eff_added, eff_removed, path, old_total,
                           mode_change)
    risk = _score(len(added) + len(removed), cosmetic, flags)
    return {
        "path": path,
        "lines_added": len(added),
        "lines_removed": len(removed),
        "lines_changed": changed,
        "pure_whitespace": whitespace_only,
        "pure_comment": comment_only,
        "line_delta": len(new_lines) - len(old_lines),
        "size_delta": size_delta,
        "file_count": 1,
        "risk": risk,
        "flags": _sort_flags(flags),
        "files": [],
    }


def review(old_text, new_text, path=""):
    """Score the change from old_text to new_text for one file."""
    old_text = old_text or ""
    new_text = new_text or ""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    return _review_lines(old_lines, new_lines, path,
                         len(new_text) - len(old_text), len(old_lines))


def _parse_patch(text):
    """Split a unified diff into per-file old/new line lists.

    Tolerant by design: anything it does not recognise is skipped rather
    than raising, because a review aid that dies on an odd patch is worse
    than one that under-reports."""
    files = []
    index = {}

    def entry(path):
        if path not in index:
            index[path] = {"path": path, "old": [], "new": [],
                           "mode_change": False}
            files.append(index[path])
        return index[path]

    cur = None
    pending = ""
    in_hunk = False
    for raw in (text or "").splitlines():
        if raw.startswith("diff --git "):
            cur, in_hunk = None, False
            parts = raw.split(" ")
            pending = _strip_prefix(parts[-1]) if len(parts) >= 4 else ""
            continue
        if MODE_RE.match(raw):
            entry(pending)["mode_change"] = True
            continue
        if raw.startswith("--- "):
            in_hunk = False
            old = _strip_prefix(raw[4:].split("\t")[0].strip())
            if old and old != "/dev/null":
                pending = old
            continue
        if raw.startswith("+++ "):
            in_hunk = False
            new = _strip_prefix(raw[4:].split("\t")[0].strip())
            if new and new != "/dev/null":
                pending = new
            cur = entry(pending)
            continue
        if raw.startswith("@@"):
            cur = cur or entry(pending)
            in_hunk = True
            continue
        if not in_hunk or cur is None:
            continue
        if raw.startswith("\\"):      # "\ No newline at end of file"
            continue
        if raw.startswith("+"):
            cur["new"].append(raw[1:])
        elif raw.startswith("-"):
            cur["old"].append(raw[1:])
        elif raw.startswith(" ") or raw == "":
            cur["old"].append(raw[1:])
            cur["new"].append(raw[1:])
        else:
            in_hunk = False
    return files


def _strip_prefix(path):
    for prefix in ("a/", "b/", "./"):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def review_patch(unified_diff_text):
    """Score a whole unified diff, worst finding first.

    The returned dict has the same shape as review(), with the counts
    summed across files, a merged flag list, and "files" holding each
    file's own summary (also worst first)."""
    parsed = _parse_patch(unified_diff_text)
    subs = []
    for item in parsed:
        old_text = "\n".join(item["old"])
        new_text = "\n".join(item["new"])
        subs.append(_review_lines(
            item["old"], item["new"], item["path"],
            len(new_text) - len(old_text), len(item["old"]),
            mode_change=item["mode_change"]))

    merged = []
    churn = 0
    for sub in subs:
        merged.extend(sub["flags"])
        churn += sub["lines_added"] + sub["lines_removed"]
    if len(subs) > FILE_FANOUT_LIMIT:
        sev = "high" if len(subs) > FILE_FANOUT_HIGH else "medium"
        merged.append(_flag(
            "file_fanout", sev,
            "Touches {} distinct files; a change this wide is hard to hold "
            "in one head.".format(len(subs))))

    cosmetic = bool(subs) and all(s["pure_whitespace"] or s["pure_comment"]
                                 for s in subs)
    return {
        "path": subs[0]["path"] if len(subs) == 1 else "",
        "lines_added": sum(s["lines_added"] for s in subs),
        "lines_removed": sum(s["lines_removed"] for s in subs),
        "lines_changed": sum(s["lines_changed"] for s in subs),
        "pure_whitespace": bool(subs) and all(s["pure_whitespace"]
                                              for s in subs),
        "pure_comment": cosmetic and any(s["pure_comment"] for s in subs),
        "line_delta": sum(s["line_delta"] for s in subs),
        "size_delta": sum(s["size_delta"] for s in subs),
        "file_count": len(subs),
        "risk": _score(churn, cosmetic, merged),
        "flags": _sort_flags(merged),
        "files": sorted(subs, key=lambda s: (-s["risk"], s["path"])),
    }


def _signed(n):
    return "{}{}".format("+" if n >= 0 else "", n)


def format_summary(summary):
    """One-screen, worst-first rendering for an approval prompt."""
    where = summary["path"] or "{} file(s)".format(summary["file_count"])
    lines = ["risk {}/100  {}".format(summary["risk"], where),
             "  +{}/-{} lines, {} changed (net {} lines, {} bytes)".format(
                 summary["lines_added"], summary["lines_removed"],
                 summary["lines_changed"], _signed(summary["line_delta"]),
                 _signed(summary["size_delta"]))]
    if summary["pure_whitespace"]:
        lines.append("  whitespace only")
    elif summary["pure_comment"]:
        lines.append("  comments only")
    if not summary["flags"]:
        lines.append("  no risk flags")
    for flag in summary["flags"]:
        tail = " [{}]".format(flag["path"]) if flag["path"] else ""
        lines.append("  [{}] {}{}".format(flag["severity"].upper(),
                                          flag["message"], tail))
    return "\n".join(lines)


def _names(summary):
    return [f["name"] for f in summary["flags"]]


def _self_test():
    # 1. no change at all
    s = review("a = 1\n", "a = 1\n", "x.py")
    assert s["risk"] == 0, s
    assert (s["lines_added"], s["lines_removed"]) == (0, 0), s
    assert s["pure_whitespace"] is False and s["pure_comment"] is False, s
    assert s["flags"] == [] and s["size_delta"] == 0, s

    # 2. empty input is a no-op, not a crash
    assert review("", "", "")["risk"] == 0
    assert review(None, None)["risk"] == 0

    # 3. a whitespace reformat that touches every line scores zero
    old = "def f():\n  x = 1\n  y = 2\n  return x+y\n"
    new = "def f():\n    x = 1\n    y = 2\n    return x + y\n"
    ws = review(old, new, "x.py")
    assert ws["lines_added"] == 3 and ws["lines_removed"] == 3, ws
    assert ws["pure_whitespace"] is True, ws
    assert ws["flags"] == [], ws
    assert ws["risk"] == 0, ws

    # 4. three lines that add subprocess must outrank that reformat
    old = "def f():\n    return 1\n"
    new = ("import subprocess\n\n\ndef f():\n"
           "    return subprocess.run(['ls'])\n")
    sub = review(old, new, "x.py")
    assert "dangerous_call" in _names(sub), sub
    assert sub["flags"][0]["name"] == "dangerous_call", sub["flags"]
    assert sub["flags"][0]["severity"] == "critical", sub["flags"][0]
    assert sub["risk"] >= 40, sub["risk"]
    assert sub["risk"] > ws["risk"], (sub["risk"], ws["risk"])

    # 5. CRLF and a missing trailing newline are not content changes
    assert review("a\r\nb\r\n", "a\nb\n", "x.py")["risk"] == 0
    tail = review("a\nb", "a\nb\n", "x.py")
    assert tail["lines_added"] == 0 and tail["size_delta"] == 1, tail
    assert tail["risk"] == 0, tail

    # 6. comment-only edits are cosmetic and near zero
    com = review("# old note\nx = 1\n", "# new note\nx = 1\n", "x.py")
    assert com["pure_comment"] is True and com["pure_whitespace"] is False
    assert com["risk"] == 0, com

    # 7. but a license header edit is still a license header edit
    lic = review("# Copyright 2020 Acme\nx = 1\n",
                 "# Copyright 2026 Someone Else\nx = 1\n", "x.py")
    assert lic["pure_comment"] is True, lic
    assert _names(lic) == ["license_change"], lic
    assert lic["risk"] == 30, lic

    # 8. a comment that merely mentions subprocess is not a new call
    talk = review("x = 1\n", "x = 1\n# we used to call subprocess here\n",
                  "x.py")
    assert "dangerous_call" not in _names(talk), talk

    # 9. mass deletion, by count and by ratio
    big_old = "".join("line {}\n".format(i) for i in range(40))
    big = review(big_old, "line 0\n", "x.py")
    assert "mass_deletion" in _names(big), big
    assert big["flags"][0]["severity"] == "critical", big["flags"][0]
    half = review("a\nb\nc\nd\ne\nf\n", "a\nb\n", "x.py")
    assert "mass_deletion" in _names(half), half
    assert half["flags"][0]["severity"] == "high", half["flags"][0]
    # a short file losing one line is not a mass deletion
    assert "mass_deletion" not in _names(review("a\nb\nc\n", "a\nb\n"))
    # a brand new file deletes nothing
    assert "mass_deletion" not in _names(review("", big_old, "x.py"))

    # 10. credentials and endpoints
    cred = review("TOKEN = read()\n", 'API_KEY = "abc123xyz"\n', "x.py")
    assert "credential_change" in _names(cred), cred
    assert cred["risk"] >= 45, cred
    url = review("HOST = 'a'\n", "HOST = 'https://evil.example/hook'\n")
    assert "url_change" in _names(url), url

    # 11. tests being weakened
    lost = review("def test_a():\n    assert f() == 1\n", "", "test_x.py")
    assert "test_weakened" in _names(lost), lost
    one = review("    assert x == 1\n    return x\n", "    return x\n")
    assert "test_weakened" in _names(one), one
    assert [f["severity"] for f in one["flags"]
            if f["name"] == "test_weakened"] == ["medium"], one

    # 12. shebang and permission lines
    she = review("#!/usr/bin/env python2\nx = 1\n",
                 "#!/usr/bin/env python3\nx = 1\n", "x.py")
    assert "shebang_or_mode" in _names(she), she
    perm = review("mode = 1\n", "os.chmod(p, 0o777)\n", "x.py")
    assert "shebang_or_mode" in _names(perm), perm

    # 13. dependency pins, and no false positive on a comparison
    dep = review("requests==2.0.0\n", "requests==9.9.9\n",
                 "agent/requirements.txt")
    assert "dependency_change" in _names(dep), dep
    inst = review("run()\n", "run('pip install evil==1.0')\n", "x.py")
    assert "dependency_change" in _names(inst), inst
    plain = review("if n > 2:\n", "if n >= 3:\n", "x.py")
    assert "dependency_change" not in _names(plain), plain

    # 14. generated or minified insertion
    giant = review("x = 1\n", "x = 1\nDATA = '{}'\n".format("z" * 3000),
                   "x.py")
    assert "giant_line" in _names(giant), giant
    assert [f["severity"] for f in giant["flags"]
            if f["name"] == "giant_line"] == ["high"], giant

    # 15. monotonic: adding a second finding never lowers the score
    worse = review(old, new + 'PASSWORD = "hunter2"\n', "x.py")
    assert worse["risk"] >= sub["risk"], (worse["risk"], sub["risk"])
    assert len(worse["flags"]) > len(sub["flags"]), worse["flags"]
    # and more churn never lowers it either, at a fixed classification
    small = review("a\n", "a\nb = 1\n", "x.py")
    bigger = review("a\n", "a\n" + "".join(
        "b{} = 1\n".format(i) for i in range(30)), "x.py")
    assert bigger["risk"] >= small["risk"], (bigger["risk"], small["risk"])
    # flags are ordered worst first
    weights = [SEVERITY_WEIGHT[f["severity"]] for f in worse["flags"]]
    assert weights == sorted(weights, reverse=True), weights

    # 16. patch mode: a real unified diff
    patch = (
        "diff --git a/agent/run.py b/agent/run.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/agent/run.py\n"
        "+++ b/agent/run.py\n"
        "@@ -1,4 +1,5 @@\n"
        " import os\n"
        "-value = 1\n"
        "+import subprocess\n"
        "+value = 2\n"
        " print(value)\n"
        " # end\n")
    p = review_patch(patch)
    assert p["file_count"] == 1 and p["path"] == "agent/run.py", p
    assert p["lines_added"] == 2 and p["lines_removed"] == 1, p
    assert "dangerous_call" in _names(p), p
    assert p["flags"][0]["path"] == "agent/run.py", p["flags"][0]
    assert len(p["files"]) == 1 and p["files"][0]["risk"] == p["risk"], p

    # 17. patch mode: a git mode change with no content change
    modes = ("diff --git a/run.sh b/run.sh\n"
             "old mode 100644\n"
             "new mode 100755\n")
    m = review_patch(modes)
    assert m["file_count"] == 1 and "shebang_or_mode" in _names(m), m
    assert m["lines_added"] == 0 and m["risk"] == 15, m

    # 18. patch mode: a whitespace-only patch is still near zero
    ws_patch = ("--- a/x.py\n"
                "+++ b/x.py\n"
                "@@ -1,2 +1,2 @@\n"
                "-  x = 1\n"
                "-  y = 2\n"
                "+    x = 1\n"
                "+    y = 2\n")
    wp = review_patch(ws_patch)
    assert wp["pure_whitespace"] is True, wp
    assert wp["risk"] == 0 and wp["flags"] == [], wp

    # 19. patch mode: fan-out across many files
    wide = ""
    for i in range(12):
        wide += ("diff --git a/f{0}.py b/f{0}.py\n"
                 "--- a/f{0}.py\n"
                 "+++ b/f{0}.py\n"
                 "@@ -1 +1 @@\n"
                 "-a = {0}\n"
                 "+a = {1}\n").format(i, i + 1)
    w = review_patch(wide)
    assert w["file_count"] == 12, w
    assert "file_fanout" in _names(w), w
    assert w["path"] == "" and len(w["files"]) == 12, w
    narrow = review_patch(wide[:wide.index("diff --git a/f3.py")])
    assert narrow["file_count"] == 3, narrow
    assert "file_fanout" not in _names(narrow), narrow

    # 20. patch mode: new file and deleted file paths resolve
    created = ("diff --git a/new.py b/new.py\n"
               "new file mode 100644\n"
               "--- /dev/null\n"
               "+++ b/new.py\n"
               "@@ -0,0 +1,2 @@\n"
               "+import os\n"
               "+print(os.name)\n")
    c = review_patch(created)
    assert c["path"] == "new.py" and c["lines_added"] == 2, c
    assert c["lines_removed"] == 0, c
    killed = ("diff --git a/gone.py b/gone.py\n"
              "deleted file mode 100644\n"
              "--- a/gone.py\n"
              "+++ /dev/null\n"
              "@@ -1,6 +0,0 @@\n"
              "-a = 1\n-b = 2\n-c = 3\n-d = 4\n-e = 5\n-f = 6\n")
    k = review_patch(killed)
    assert k["path"] == "gone.py" and k["lines_removed"] == 6, k
    assert "mass_deletion" in _names(k), k

    # 21. junk and empty input do not raise
    for junk in ("", "not a patch at all\n", "@@ garbage @@\n",
                 "+++ \n--- \n@@\n+x\n"):
        j = review_patch(junk)
        assert 0 <= j["risk"] <= 100, (junk, j)

    # 22. the score is bounded even for a diff that trips everything
    everything = review(
        big_old + "def test_a():\n    assert 1\n",
        'API_KEY = "x"\nimport subprocess\nurl = "https://e.example"\n'
        "# Copyright 2026 Me\n" + "q = '{}'\n".format("z" * 3000),
        "requirements.txt")
    assert everything["risk"] == 100, everything["risk"]
    assert len(everything["flags"]) >= 6, everything["flags"]

    # 23. rendering leads with the worst line
    text = format_summary(everything)
    assert text.startswith("risk 100/100"), text
    assert "[CRITICAL]" in text.split("\n")[3], text
    assert "no risk flags" in format_summary(review("a\n", "a\n")), text

    print("hearth-difflens self-test OK")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="hearth-difflens",
        description="Summarise a unified diff and score what is risky "
                    "about it, worst first.")
    p.add_argument("patch", nargs="?",
                   help="unified diff file, or - for stdin (default stdin)")
    p.add_argument("--json", action="store_true",
                   help="print the raw summary dict instead of the report")
    p.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD,
                   help="exit nonzero when risk exceeds this (default "
                        "{})".format(DEFAULT_THRESHOLD))
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    if a.patch and a.patch != "-":
        with open(a.patch, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    else:
        if a.patch is None and sys.stdin.isatty():
            p.error("no patch given (pass a file, pipe one in, or "
                    "use --self-test)")
        text = sys.stdin.read()
    summary = review_patch(text)
    if a.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(format_summary(summary))
    return 1 if summary["risk"] > a.threshold else 0


if __name__ == "__main__":
    sys.exit(main())
