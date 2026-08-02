#!/usr/bin/env python3
"""hearth agent tools: a small pluggable registry. A tool is a dict with a name,
a description, a JSON-schema for its parameters, and a `fn(args, workspace)` that
runs it and returns a short string result. Adding a capability means adding a
tool here (or registering one at runtime). Standard library only.

All file tools operate inside the per-run workspace and refuse paths that escape
it. On NixOS this sits behind a systemd sandbox and an nftables egress wall. On
Windows there is no such backstop, so hearth_contain is the boundary itself.
run_command is scoped by working directory, which is not a boundary; what
constrains it on Windows is hearth_sandbox, and only as far as that module's
docstring claims (writes, at the opt-in level; never reads, never the network).
"""

import fnmatch
import glob
import html as _htmlmod
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hearth_contain
import hearth_paths
import hearth_proc
import hearth_sandbox


def _bin(name, fallback):
    """Resolve a system binary by PATH, falling back to its NixOS stable path
    (agents may run as a systemd unit with a minimal PATH)."""
    return shutil.which(name) or fallback


HEARTH_REPO = os.environ.get("HEARTH_REPO", "/home/operator/hearth-desktop")
_SYSTEM_PROFILES = "/nix/var/nix/profiles"


def _repo_join(path):
    """Resolve a path inside HEARTH_REPO. Kept for the Linux self-config tools,
    which operate on hearth's own flake rather than on a run workspace."""
    return hearth_contain.safe_join(HEARTH_REPO, path)


def _meminfo_summary(text):
    """Parse /proc/meminfo into (used_mb, total_mb)."""
    vals = {}
    for ln in (text or "").splitlines():
        if ":" in ln:
            k, v = ln.split(":", 1)
            vals[k.strip()] = v.strip()

    def kb(key):
        try:
            return int(vals.get(key, "0").split()[0])
        except (ValueError, IndexError):
            return 0

    total = kb("MemTotal")
    avail = kb("MemAvailable")
    return (max(total - avail, 0)) // 1024, total // 1024


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "head", "noscript"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0 and data.strip():
            self.parts.append(data)


def _html_to_text(html_text):
    p = _TextExtractor()
    try:
        p.feed(html_text or "")
    except Exception:  # noqa: BLE001 - malformed HTML must not raise
        pass
    return " ".join(" ".join(part.split()) for part in p.parts if part.strip())


COMMAND_TIMEOUT = 120
# 120s was too short for a real build (a cold `npm install` or a full test
# suite routinely runs past it). 1800s (30 minutes) is the ceiling a model can
# ask for via the per-call `timeout` argument; COMMAND_TIMEOUT stays the
# default for a call that does not specify one.
MAX_COMMAND_TIMEOUT = 1800
HTTP_TIMEOUT = 30
MAX_OUT = 4000


def _read_text(full):
    """Read a text file as UTF-8 without translating line endings.

    newline='' keeps CRLF as CRLF so an edit round-trip does not rewrite every
    line of the file, which would make an approval diff unreviewable.
    """
    with open(hearth_paths.long_path(full), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _copy_mode(target, tmp):
    """Best-effort: carry the target's existing permissions onto the temp
    file before the atomic replace. A brand-new temp file otherwise gets
    default permissions (umask-derived on POSIX), so an edit of a
    deliberately-restricted file would silently widen or narrow it on every
    write. A missing target (first write) has nothing to copy, so the temp
    file just keeps the OS default. Never lets a permissions problem break
    the write itself: a successful write with default permissions beats a
    failed write.

    On Windows, this call does set the read-only attribute on tmp to match
    the target (verified directly, see _self_test), but that effect is then
    unobservable through the normal write path: os.replace() itself refuses
    to overwrite a read-only destination (WinError 5) regardless of the
    source file's mode, so a read-only target's chmod'd-to-match tmp file
    never actually gets used, and a writable target's tmp file was already
    writable by construction. In other words the call does something, it
    just never changes the outcome of tool_write_file/tool_edit_file on
    Windows.
    """
    try:
        mode = os.stat(target).st_mode
    except OSError:
        return
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass


def _write_text_atomic(full, content, newline=""):
    """Write UTF-8 text via a uniquely-named temp file in the same directory,
    then os.replace onto the target.

    Atomic so a failure part-way through cannot leave a truncated file. The
    unpatched code opened the target with mode 'w' (truncating it) before
    encoding, so an encoding error destroyed the original.

    The temp name is derived from the target's own basename plus a uuid4
    hex suffix, created with O_EXCL, so two concurrent writes to different
    files in the same directory from the same process never share a temp
    path. A name keyed only on the pid does: one write's temp file would
    clobber the other's before its os.replace runs, which surfaces as a
    loud WinError 32 on Windows (mandatory locking) but succeeds silently
    on POSIX, cross-contaminating both destinations with no error at all.

    The temp file is opened with mode 0o666, which the OS masks by the
    process umask exactly like a normal open() call, so a brand-new target
    ends up with the ordinary umask-derived default (0o644 under a typical
    022 umask) rather than something else. tempfile.mkstemp was tried first
    for the same collision-safety guarantee, but it hardcodes mode 0o600 on
    the file it creates regardless of umask, which silently narrowed the
    permissions of every newly-written file; os.open's mode argument does
    not have that problem, so there is no umask-reading dance needed here.

    Returns the number of BYTES written, which is what the caller should report.
    """
    full = hearth_paths.long_path(full)
    parent = os.path.dirname(full) or "."
    data = content.encode("utf-8")
    tmp = os.path.join(parent, ".hearth-tmp-{}-{}".format(os.path.basename(full), uuid.uuid4().hex))
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as fh:
            fh.write(content)
        _copy_mode(full, tmp)
        _retry_io(lambda: os.replace(tmp, full))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return len(data)


def _retry_io(fn, attempts=3, delay=0.1):
    """Call fn, retrying briefly on a sharing violation.

    Windows refuses to replace or delete a file another process holds open
    (WinError 32) or that is read-only (WinError 5). Antivirus scanners, the
    Search Indexer, editors, and OneDrive sync all hold files open for short
    windows, so a single attempt fails for reasons that resolve on their own.
    """
    last = None
    for i in range(attempts):
        try:
            return fn()
        except PermissionError as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(delay * (i + 1))
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (5, 32):
                raise
            last = exc
            if i < attempts - 1:
                time.sleep(delay * (i + 1))
    raise last


def _glob_match(name, pattern):
    """Case-sensitive glob matching with identical semantics on every platform.

    fnmatch.fnmatch normcases both arguments, so '*.py' matches 'App.PY' on
    Windows but not on Linux. An agent's file filter should not change meaning
    with the host operating system.
    """
    return fnmatch.fnmatchcase(name, pattern)


def _case_conflict(requested_path, full):
    """Return the existing on-disk name if it differs from the requested one
    only by case.

    NTFS is case-insensitive, so opening 'readme.md' when 'README.md' exists
    writes the existing file while the tool reports a name that does not
    exist on disk. Surface that instead of merging the two silently.

    Takes the caller's original requested path as well as the resolved
    `full`, not just `full` alone: hearth_contain.safe_join resolves through
    os.path.realpath, which on Windows silently normalises an *existing*
    target to its actual on-disk spelling regardless of what was asked for
    (verified directly: os.path.realpath(".../readme.md") returns
    ".../README.md" once README.md exists). By the time a tool holds `full`,
    the mismatch is no longer visible by inspecting `full` in isolation; a
    directory listing keyed on full's own basename would never find a
    same-case-folded sibling either, since NTFS refuses to hold two entries
    that differ only by case. The only place the original spelling still
    exists is the caller's request, so that is what gets compared.
    """
    if not hearth_paths.is_windows():
        return None
    requested_base = os.path.basename((requested_path or "").replace("\\", "/"))
    if not requested_base:
        return None
    actual_base = os.path.basename(full)
    if requested_base != actual_base and requested_base.lower() == actual_base.lower():
        return actual_base
    return None


def _dominant_newline(text):
    """The line ending a file mostly uses, so an edit can preserve it.

    Returns "\r\n", "\n", or "" for a file with no line break at all.
    """
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf == 0 and lf == 0:
        return ""
    return "\r\n" if crlf > lf else "\n"


def _explain_oserror(exc):
    """Turn a Windows OSError into something a model can act on."""
    winerr = getattr(exc, "winerror", None)
    if winerr == 32:
        return "the file is open in another program, close it and retry"
    if winerr == 5:
        return "access denied (the file may be read-only)"
    if winerr == 3 or winerr == 206:
        return "the path is too long or does not exist"
    return str(exc)


def tool_run_command(args, workspace):
    """Run a shell command via hearth_proc.run_contained, with workspace as cwd.

    This runs through a real shell (cmd.exe or /bin/sh). The workspace is the
    working directory, and a working directory has never been a security
    boundary: `cd ..`, an absolute path, or a command that touches no path at
    all all sit outside it. What now stands behind that is hearth_sandbox,
    whose level decides how much of the machine the shell can still reach. At
    the default Windows level ("limits") the answer is honestly "all of it,
    minus privileges, orphans and resource exhaustion"; at "workspace" the
    child cannot WRITE outside the workspace but can still read it and reach
    the network. Read hearth_sandbox's docstring before believing anything
    stronger. On NixOS this whole call additionally sits behind systemd
    isolation and an nftables egress wall, which remain the stronger boundary.

    The sandbox level is NOT taken from `args`. Everything in `args` came from
    the model, and a model that can be prompt-injected into asking for a
    weaker sandbox has no sandbox at all; the level comes from the process
    environment (HEARTH_SANDBOX), which only the user sets.

    On timeout the whole process tree is killed (not just the shell) and
    whatever partial output was captured is returned, distinguished from a
    non-zero exit rather than folded into it.
    """
    cmd = args.get("command", "")
    if not cmd:
        return "error: no command"
    try:
        timeout = int(args.get("timeout") or COMMAND_TIMEOUT)
    except (TypeError, ValueError):
        timeout = COMMAND_TIMEOUT
    timeout = max(1, min(timeout, MAX_COMMAND_TIMEOUT))
    rc, out, err, timed_out = hearth_proc.run_contained(cmd, workspace, timeout=timeout)
    if timed_out:
        tail = (out or "")[-1000:]
        return "error: command timed out after {}s (process tree killed)\npartial stdout:\n{}".format(
            timeout, tail)
    return "exit={}\nstdout:\n{}\nstderr:\n{}{}".format(
        rc, (out or "")[-MAX_OUT:], (err or "")[-2000:], _sandbox_hint(rc, out, err))


def _sandbox_hint(rc, out, err):
    """A one-line explanation when the sandbox, not the command, said no.

    Without this a model at the `workspace` level sees a bare "Access is
    denied." for a write it was perfectly entitled to attempt and does the
    thing weak local models always do: retries the identical command, then
    tries it with more quoting, then gives up ten tool calls later. Naming the
    cause lets it choose a path inside the workspace instead.
    """
    if rc == 0:
        return ""
    level = hearth_sandbox.resolve_level()
    if level != "workspace":
        return ""
    blob = ((out or "") + (err or "")).lower()
    if "access is denied" not in blob and "permission denied" not in blob:
        return ""
    return ("\nhint: the sandbox is at the 'workspace' level, which blocks every "
            "write outside the workspace directory. Write inside the workspace, "
            "or ask the user to change the sandbox setting.")


def _ignored_error(workspace, full, requested_path):
    """Return an error string if `full` is excluded by the workspace's
    `.hearthignore`, else None.

    Called only AFTER hearth_contain.safe_join has already approved `full`
    -- every call site below follows that order -- so this can only narrow
    which already-contained paths a tool is willing to touch. It never
    participates in resolving a path itself, so it cannot be the thing
    that lets a path out of the workspace; see hearth_contain.is_ignored's
    docstring for the full guarantee.

    The message names .hearthignore explicitly (rather than a generic
    "not found" or "permission denied") because a model that does not
    understand why a path was refused will just retry it.
    """
    root = os.path.realpath(workspace)
    if hearth_contain.is_ignored(root, full):
        return "error: '{}' is excluded by .hearthignore".format(requested_path)
    return None


_HEARTHIGNORE_FILENAME = ".hearthignore"


def _hearthignore_protected(workspace, full):
    """True if `full` (an already safe_join-resolved absolute path) IS the
    workspace's own .hearthignore file -- not merely a path it excludes,
    the scoping file itself.

    .hearthignore does not ignore itself: nothing in hearth_contain's
    matching logic ever adds the file's own name to its own exclusion set,
    so without this check `write_file(".hearthignore", "")` succeeds like
    any other write, silently erasing every rule that scoped the run --
    and in 'auto' mode that write is auto-allowed, so it happens with no
    approval prompt at all. This check is independent of the file's own
    contents (a hostile .hearthignore cannot exempt itself by, say,
    negating its own path) and independent of is_ignored() entirely: it
    fires whether or not the file currently ignores anything, including
    when it does not exist yet (so the agent cannot create one via
    edit_file/replace_in_files' rename-through-content tricks either --
    those tools only ever touch a path that already resolved to this
    exact file). A user editing .hearthignore themselves, outside the
    agent, is unaffected -- this only ever gates the three tool functions
    that call it, not the filesystem itself."""
    root = os.path.realpath(workspace)
    hi = os.path.realpath(os.path.join(root, _HEARTHIGNORE_FILENAME))
    return os.path.normcase(os.path.realpath(full)) == os.path.normcase(hi)


def _hearthignore_protected_error(workspace, full):
    if _hearthignore_protected(workspace, full):
        return ("error: .hearthignore is always protected from write_file, edit_file, "
                "and replace_in_files, regardless of its own contents; edit it directly "
                "outside the agent if the workspace's scoping needs to change")
    return None


def tool_write_file(args, workspace):
    try:
        full = hearth_contain.safe_join(workspace, args.get("path"))
    except ValueError as exc:
        return "error: {}".format(exc)
    protected = _hearthignore_protected_error(workspace, full)
    if protected:
        return protected
    blocked = _ignored_error(workspace, full, args.get("path"))
    if blocked:
        return blocked
    conflict = _case_conflict(args.get("path"), full)
    if conflict:
        return ("error: {} already exists as {} (this filesystem is "
                "case-insensitive); use the existing name or rename it first").format(
                    os.path.basename(args.get("path") or ""), conflict)
    content = args.get("content", "")
    if os.path.exists(hearth_paths.long_path(full)):
        try:
            existing_nl = _dominant_newline(_read_text(full))
            if existing_nl == "\r\n":
                content = content.replace("\r\n", "\n").replace("\n", "\r\n")
        except (OSError, UnicodeDecodeError):
            pass
    parent = os.path.dirname(full)
    if parent:
        try:
            os.makedirs(hearth_paths.long_path(parent), exist_ok=True)
        except OSError as exc:
            return "error: cannot create directory: {}".format(exc)
    try:
        n = _write_text_atomic(full, content)
    except OSError as exc:
        return "error: {}".format(_explain_oserror(exc))
    return "wrote {} ({} bytes)".format(args.get("path"), n)


def tool_read_file(args, workspace):
    try:
        full = hearth_contain.safe_join(workspace, args.get("path"))
    except ValueError as exc:
        return "error: {}".format(exc)
    blocked = _ignored_error(workspace, full, args.get("path"))
    if blocked:
        return blocked
    try:
        return _read_text(full)[:MAX_OUT]
    except UnicodeDecodeError:
        return "error: {} is not valid UTF-8 text (binary file?)".format(args.get("path"))
    except OSError as exc:
        return "error: {}".format(_explain_oserror(exc))


def tool_list_files(args, workspace):
    try:
        full = hearth_contain.safe_join(workspace, args.get("path", "."))
    except ValueError as exc:
        return "error: {}".format(exc)
    blocked = _ignored_error(workspace, full, args.get("path", "."))
    if blocked:
        return blocked
    root = os.path.realpath(workspace)
    spec = hearth_contain.load_ignore(root)
    try:
        names = os.listdir(full)
    except OSError as exc:
        return "error: {}".format(exc)
    visible = []
    for name in names:
        entry = os.path.join(full, name)
        rel = os.path.relpath(entry, root)
        try:
            is_dir = os.path.isdir(hearth_paths.long_path(entry))
        except OSError:
            is_dir = False
        if spec.is_ignored(rel, is_dir=is_dir):
            continue
        visible.append(name)
    return "\n".join(sorted(visible)) or "(empty)"


# Sourced from hearth_contain.SKIP_DIRS, the one authoritative list, rather
# than redefined here. The old local copy was missing ".venv", which let
# replace_in_files walk into a virtualenv and rewrite site-packages -- files
# the checkpoint store never captures, so that damage was not undoable. See
# hearth_contain.SKIP_DIRS's docstring comment for the full story.
_TREE_SKIP = hearth_contain.SKIP_DIRS


def tool_search_files(args, workspace):
    """Search files under a path for a regex (or literal) query. Returns
    relpath:lineno: line for each match, capped."""
    query = args.get("query", "")
    if not query:
        return "error: no query"
    try:
        base = hearth_contain.safe_join(workspace, args.get("path", "."))
    except ValueError as exc:
        return "error: {}".format(exc)
    blocked = _ignored_error(workspace, base, args.get("path", "."))
    if blocked:
        return blocked
    try:
        rx = re.compile(query)
    except re.error:
        rx = re.compile(re.escape(query))
    pattern = args.get("glob")
    root = os.path.realpath(workspace)
    spec = hearth_contain.load_ignore(root)
    hits = []
    for dirpath, dirs, files in os.walk(base):
        hearth_contain.prune(dirpath, dirs, _TREE_SKIP, root=root, ignore_spec=spec)
        for fn in sorted(files):
            if pattern and not _glob_match(fn, pattern):
                continue
            fp = os.path.join(dirpath, fn)
            try:
                hearth_contain.safe_join(root, os.path.relpath(fp, root))
            except ValueError:
                continue  # a link or an odd name that resolves outside the workspace
            if spec.is_ignored(os.path.relpath(fp, root), is_dir=False):
                continue
            try:
                if os.path.getsize(hearth_paths.long_path(fp)) > 2_000_000:
                    continue
                with open(hearth_paths.long_path(fp), "r", encoding="utf-8", errors="replace", newline="") as fh:
                    for i, line in enumerate(fh, 1):
                        if rx.search(line):
                            hits.append("{}:{}: {}".format(os.path.relpath(fp, root), i, line.rstrip()[:200]))
                            if len(hits) >= 200:
                                return "\n".join(hits) + "\n... (truncated at 200 matches)"
            except OSError:
                continue
    return "\n".join(hits) if hits else "no matches"


def tool_list_tree(args, workspace):
    """Render an indented directory tree under a path (common build/VCS dirs skipped)."""
    try:
        base = hearth_contain.safe_join(workspace, args.get("path", "."))
    except ValueError as exc:
        return "error: {}".format(exc)
    blocked = _ignored_error(workspace, base, args.get("path", "."))
    if blocked:
        return blocked
    try:
        max_depth = max(1, int(args.get("max_depth", 4) or 4))
    except (TypeError, ValueError):
        max_depth = 4
    root = os.path.realpath(workspace)
    spec = hearth_contain.load_ignore(root)
    base_depth = base.rstrip(os.sep).count(os.sep)
    lines = []
    for dirpath, dirs, files in os.walk(base):
        depth = dirpath.rstrip(os.sep).count(os.sep) - base_depth
        if depth >= max_depth:
            dirs[:] = []
            continue
        dirs[:] = sorted(hearth_contain.prune(dirpath, dirs, _TREE_SKIP, root=root, ignore_spec=spec))
        rel = os.path.relpath(dirpath, root)
        indent = "  " * depth
        lines.append("{}{}/".format(indent, "." if rel == "." else os.path.basename(dirpath)))
        for fn in sorted(files):
            if spec.is_ignored(os.path.relpath(os.path.join(dirpath, fn), root), is_dir=False):
                continue
            lines.append("{}  {}".format(indent, fn))
        if len(lines) >= 400:
            lines.append("... (truncated)")
            break
    return "\n".join(lines) or "(empty)"


def tool_edit_file(args, workspace):
    """Edit a file by replacing exact text: find -> replace. Replaces the first
    match, or every match when all=true. Errors (no change) if find is absent, so
    the model knows to re-read the file. More reliable for local models than diffs."""
    try:
        full = hearth_contain.safe_join(workspace, args.get("path"))
    except ValueError as exc:
        return "error: {}".format(exc)
    protected = _hearthignore_protected_error(workspace, full)
    if protected:
        return protected
    blocked = _ignored_error(workspace, full, args.get("path"))
    if blocked:
        return blocked
    find = args.get("find")
    if not find:
        return "error: no 'find' text given"
    replace = args.get("replace", "")
    try:
        content = _read_text(full)
    except (OSError, UnicodeDecodeError) as exc:
        return "error: {}".format(exc)
    count = content.count(find)
    if count == 0:
        return "error: 'find' text not found in {}; re-read the file and try again (no changes made)".format(args.get("path"))
    if args.get("all"):
        content = content.replace(find, replace)
        n = count
    else:
        content = content.replace(find, replace, 1)
        n = 1
    try:
        _write_text_atomic(full, content, newline="")
    except OSError as exc:
        return "error: {}".format(_explain_oserror(exc))
    return "edited {}: {} replacement{}".format(args.get("path"), n, "s" if n != 1 else "")


EGRESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS egress_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id TEXT, ts TEXT, tool TEXT, host TEXT, url TEXT, allowed INTEGER
);
"""

# A honeyfile decoy embeds a canary token; reading it anywhere trips the alarm.
# The token is random per file, so detection is a plain regex over tool output
# with no shared registry to keep in sync. See hearth_loop for the trip handling.
CANARY_RE = re.compile(r"HEARTH-CANARY-[0-9a-f]{16}")

# Relpath -> a template that renders convincing bait with the canary embedded.
_DECOY_TEMPLATES = {
    ".aws/credentials": (
        "[default]\naws_access_key_id = AKIA{tok8}\n"
        "aws_secret_access_key = {canary}\nregion = us-east-1\n"),
    ".env.production": (
        "# production secrets, do not commit\n"
        "DATABASE_URL=postgres://admin:{canary}@db.internal:5432/app\n"
        "STRIPE_SECRET_KEY=sk_live_{canary}\n"),
    "secrets/api_keys.txt": (
        "internal API keys\n----\nbilling: {canary}\npaging: hearth-{tok8}\n"),
}


def _new_canary():
    import uuid as _uuid
    return "HEARTH-CANARY-" + _uuid.uuid4().hex[:16]


def plant_decoys(workspace):
    """Plant honeyfile decoys in a fresh workspace. Each looks like a plausible
    secrets file and embeds a unique canary token. A well-behaved agent never
    reads unrequested credentials; one that does (often a prompt-injected or
    confused run) trips the alarm. Returns the set of planted relpaths so the
    loop can also flag a direct read by path, not only a content match."""
    planted = set()
    for rel, template in _DECOY_TEMPLATES.items():
        try:
            full = hearth_contain.safe_join(workspace, rel)
        except ValueError:
            continue
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        canary = _new_canary()
        body = template.format(canary=canary, tok8=canary[-8:])
        try:
            # Explicit UTF-8 via the same helper every manifest tool writes
            # through, not a bare open() on the host's ANSI codepage: a
            # decoy body containing a non-ASCII byte would otherwise fail
            # (or mojibake) on a cp1252 machine instead of writing cleanly.
            _write_text_atomic(full, body)
            planted.add(rel)
        except OSError:
            continue
    return planted


def find_canary(text):
    """Return the first canary token found in text, or None."""
    m = CANARY_RE.search(text or "")
    return m.group(0) if m else None


def _host_allowed(host):
    """Check a hostname against the run's egress allowlist (HEARTH_ALLOWED_HOSTS,
    comma-separated). Unset or empty means no allowlist: everything is allowed
    (back-compat, same semantics as HEARTH_ALLOWED_CREDS). Loopback is always
    allowed (Ollama and local APIs). An entry matches itself and its subdomains:
    'github.com' allows both 'github.com' and 'api.github.com'."""
    host = (host or "").lower().strip("[]")
    if host in ("localhost", "::1") or host.startswith("127."):
        return True
    raw = os.environ.get("HEARTH_ALLOWED_HOSTS", "")
    entries = [e.strip().lower() for e in raw.split(",") if e.strip()]
    if not entries:
        return True
    return any(host == e or host.endswith("." + e) for e in entries)


def _log_egress(tool, host, url, allowed):
    """Record an outbound network attempt (allowed or blocked) to the audit db.
    Best-effort: an unwritable db must never break the tool."""
    db = hearth_paths.db_path()
    agent_id = os.environ.get("HEARTH_AGENT_ID", "unknown")
    try:
        con = sqlite3.connect(db, timeout=10)
        try:
            con.executescript(EGRESS_SCHEMA)
            con.execute(
                "INSERT INTO egress_log (agent_id, ts, tool, host, url, allowed) "
                "VALUES (?,?,?,?,?,?)",
                (agent_id, datetime.now(timezone.utc).isoformat(), tool,
                 host, (url or "")[:500], 1 if allowed else 0))
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        pass


def _egress_check(url, tool):
    """Gate an outbound request on the run's egress allowlist and log the
    attempt. Returns None when allowed, or an error string the model can learn
    from when blocked."""
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    allowed = _host_allowed(host)
    _log_egress(tool, host, url, allowed)
    if allowed:
        return None
    return ("error: egress blocked: {} is not in this run's allowed hosts; "
            "use an allowed host or finish without it".format(host or "?"))


def _resolve_cred(name):
    """Read a stored credential by name from the systemd credentials directory.
    The credentials file is a simple `NAME=VALUE` per line. Returns "" if not
    available, so an agent can never read the raw store directly."""
    allowed = os.environ.get("HEARTH_ALLOWED_CREDS")
    if allowed is not None and name not in [a.strip() for a in allowed.split(",") if a.strip()]:
        return ""
    creds_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if not creds_dir:
        return ""
    path = os.path.join(creds_dir, "creds")
    try:
        with open(path) as fh:
            for line in fh:
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        return ""
    return ""


def tool_web_fetch(args, workspace):
    url = args.get("url")
    if not url:
        return "error: no url"
    blocked = _egress_check(url, "web_fetch")
    if blocked:
        return blocked
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (hearth-agent)"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read(2000000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return "error: {}".format(exc)
    text = _html_to_text(raw)
    return text[:MAX_OUT] if text else "(no readable text)"


def _ddg_real_url(href):
    """DDG result links are redirects like //duckduckgo.com/l/?uddg=<encoded>.
    Return the decoded target, or the href unchanged if already direct."""
    if "uddg=" in href:
        try:
            q = urllib.parse.urlparse("https:" + href if href.startswith("//") else href).query
            uddg = urllib.parse.parse_qs(q).get("uddg")
            if uddg:
                return urllib.parse.unquote(uddg[0])
        except (ValueError, KeyError):
            pass
    if href.startswith("//"):
        return "https:" + href
    return href


def _parse_ddg_results(html_text, max_results):
    """Extract [{title, url, snippet}] from DuckDuckGo HTML results."""
    results = []
    link_re = re.compile(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
    snip_re = re.compile(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', re.S)
    links = link_re.findall(html_text or "")
    snippets = snip_re.findall(html_text or "")

    def clean(s):
        return " ".join(_htmlmod.unescape(re.sub(r"<[^>]+>", "", s)).split())

    for i, (href, title) in enumerate(links[:max_results]):
        results.append({
            "title": clean(title),
            "url": _ddg_real_url(href),
            "snippet": clean(snippets[i]) if i < len(snippets) else "",
        })
    return results


def tool_web_search(args, workspace):
    query = args.get("query") or args.get("q")
    if not query:
        return "error: no query"
    max_results = int(args.get("max_results") or 5)
    blocked = _egress_check("https://html.duckduckgo.com/html/", "web_search")
    if blocked:
        return blocked + " (web_search needs duckduckgo.com in the allowlist)"
    # The DDG HTML endpoint serves results only for a POST with the query as form
    # data; a GET query string just returns the DDG home page.
    data = urllib.parse.urlencode({"q": query}).encode()
    req = urllib.request.Request(
        "https://html.duckduckgo.com/html/", data=data,
        headers={"User-Agent": "Mozilla/5.0 (hearth-agent)",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read(2000000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return "error: {}".format(exc)
    results = _parse_ddg_results(body, max_results)
    if not results:
        return "(no results)"
    return "\n".join("{}. {}\n   {}\n   {}".format(i + 1, r["title"], r["url"], r["snippet"])
                     for i, r in enumerate(results))[:MAX_OUT]


def tool_http_request(args, workspace):
    url = args.get("url")
    if not url:
        return "error: no url"
    blocked = _egress_check(url, "http_request")
    if blocked:
        return blocked
    method = (args.get("method") or "GET").upper()
    headers = {k: (_resolve_cred(v[5:]) if isinstance(v, str) and v.startswith("cred:") else v)
               for k, v in (args.get("headers") or {}).items()}
    body = args.get("body")
    data = body.encode() if isinstance(body, str) else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            text = resp.read().decode("utf-8", "replace")[:MAX_OUT]
            return "status={}\n{}".format(resp.status, text)
    except urllib.error.HTTPError as exc:
        return "status={}\n{}".format(exc.code, exc.read().decode("utf-8", "replace")[:2000])
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return "error: {}".format(exc)


def _parse_generation(target):
    """Extract the generation number from a system profile link target like
    'system-123-link'. Returns 'unknown' if it does not match."""
    m = re.search(r"system-(\d+)-link", target or "")
    return m.group(1) if m else "unknown"


def tool_current_generation(args, workspace):
    """Report hearth's active NixOS generation number and its build date.
    Read-only introspection: follows /nix/var/nix/profiles/system."""
    profile = "/nix/var/nix/profiles/system"
    try:
        target = os.readlink(profile)
    except OSError as exc:
        return "error: cannot read the system profile ({})".format(exc)
    gen = _parse_generation(target)
    link = target if os.path.isabs(target) else os.path.join(os.path.dirname(profile), target)
    try:
        from datetime import datetime, timezone
        built = datetime.fromtimestamp(os.lstat(link).st_mtime, timezone.utc).isoformat()
    except OSError:
        built = "unknown"
    return "generation={}\nbuilt={}".format(gen, built)


def tool_list_generations(args, workspace):
    """List NixOS system generations (number + build date), newest first,
    marking the active one with a star."""
    from datetime import datetime, timezone
    try:
        current = _parse_generation(os.readlink(os.path.join(_SYSTEM_PROFILES, "system")))
    except OSError:
        current = "unknown"
    rows = []
    for link in glob.glob(os.path.join(_SYSTEM_PROFILES, "system-*-link")):
        num = _parse_generation(os.path.basename(link))
        if num == "unknown":
            continue
        try:
            ts = datetime.fromtimestamp(os.lstat(link).st_mtime, timezone.utc).isoformat()
        except OSError:
            ts = "unknown"
        rows.append((int(num), ts))
    if not rows:
        return "error: no system generations found (not a NixOS host?)"
    rows.sort(reverse=True)
    return "\n".join("{}{}  {}".format("* " if str(n) == current else "  ", n, ts)
                     for n, ts in rows)


def tool_system_health(args, workspace):
    """Report system health: systemd status, failed units, disk, memory, GPU."""
    parts = []
    systemctl = _bin("systemctl", "/run/current-system/sw/bin/systemctl")
    try:
        st = subprocess.run([systemctl, "is-system-running"],
                            capture_output=True, text=True, timeout=5)
        parts.append("system: {}".format((st.stdout or st.stderr or "?").strip()))
    except (OSError, subprocess.SubprocessError):
        parts.append("system: unknown")
    try:
        failed = subprocess.run([systemctl, "--failed", "--no-legend", "--plain"],
                                capture_output=True, text=True, timeout=5).stdout
        names = [ln.split()[0] for ln in failed.splitlines() if ln.strip()]
        parts.append("failed units: {}{}".format(
            len(names), (" (" + ", ".join(names[:5]) + ")") if names else ""))
    except (OSError, subprocess.SubprocessError):
        parts.append("failed units: unknown")
    try:
        du = shutil.disk_usage("/")
        parts.append("disk /: {}/{} GB used".format(du.used // (1024**3), du.total // (1024**3)))
    except OSError:
        pass
    try:
        with open("/proc/meminfo") as fh:
            used, total = _meminfo_summary(fh.read())
        parts.append("mem: {}/{} MB used".format(used, total))
    except OSError:
        pass
    nvidia = shutil.which("nvidia-smi")
    if nvidia:
        try:
            g = subprocess.run(
                [nvidia, "--query-gpu=utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5).stdout.strip()
            if g:
                parts.append("gpu: " + g.splitlines()[0])
        except (OSError, subprocess.SubprocessError):
            pass
    return "\n".join(parts)


def tool_read_self_config(args, workspace):
    """Read a file from hearth's own configuration repo (the flake source)."""
    try:
        full = _repo_join(args.get("path", ""))
    except ValueError as exc:
        return "error: {}".format(exc)
    try:
        return _read_text(full)[:MAX_OUT]
    except UnicodeDecodeError:
        return "error: {} is not valid UTF-8 text (binary file?)".format(args.get("path"))
    except OSError as exc:
        return "error: {}".format(_explain_oserror(exc))


def _run_git_argv(argv, cwd, timeout=60):
    """Run a git argv list directly with subprocess.run: no shell involved.

    This is the fix for a confirmed RCE. tool_git_status and tool_git_diff
    used to build a shell command STRING and hand it to
    hearth_proc.run_contained, which runs it through cmd.exe /d /s /c or
    /bin/sh -c. The model-supplied 'path' argument to git_diff was
    interpolated into that string with only space-aware quoting (_q, now
    deleted), so a path like 'x&whoami' was not quoted at all and '&' ran
    'whoami' as a second command. Both tools were classified "safe" in
    permissions.RISK, so this ran with no approval prompt, in 'plan' mode,
    which every doc describes as read-only.

    An argv list has no such hole: the OS hands each element to the child
    process exactly as given, with no shell parsing step in between, so
    there is nothing for '&', ';', '|', backticks, or '$()' to mean. A path
    containing a space (e.g. 'C:\\Program Files\\Git\\...') also just works,
    with no quoting needed at all, which is what made _q exist in the first
    place and is exactly why it could never be fixed by quoting harder: see
    the module-level notes in permissions.py's _command_head docstring for
    why string-level defenses against a real shell cannot be made complete.

    Output is decoded as UTF-8 with replacement, matching hearth_proc's own
    behaviour, so a byte outside the host codepage does not crash this call
    or silently read as empty output.
    """
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "git command timed out after {}s".format(timeout)
    except OSError as exc:
        return 127, "", str(exc)
    out = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace")
    return proc.returncode, out, err


def tool_git_status(args, workspace):
    """Show git status of the run workspace. Read-only.

    Runs via argv (see _run_git_argv), never a shell string, and scoped to
    the run workspace (not HEARTH_REPO)."""
    git = shutil.which("git")
    if not git:
        return "git is not installed or not on PATH"
    rc, out, err = _run_git_argv([git, "status", "--short", "--branch"], workspace, timeout=60)
    if rc != 0:
        # Return code is checked, not masked: a git failure (not a repo, a
        # corrupt index, etc.) is reported as an error rather than silently
        # printing "(clean)" as if status had actually succeeded.
        return "error: {}".format((err or out or "git failed").strip()[:500])
    return out.strip() or "(clean)"


def tool_git_diff(args, workspace):
    """Show git diff of the run workspace (optionally for one path). Read-only.

    Runs via argv (see _run_git_argv): the model-supplied 'path' reaches git
    as a single argv element, exactly as typed, with no shell in between to
    interpret it. Scoped to the run workspace (not HEARTH_REPO)."""
    git = shutil.which("git")
    if not git:
        return "git is not installed or not on PATH"
    argv = [git, "diff", "--stat"]
    if args.get("path"):
        argv += ["--", args["path"]]
    rc, out, err = _run_git_argv(argv, workspace, timeout=60)
    if rc != 0:
        # Return code is checked, not masked: a git failure is reported as
        # an error rather than silently printing "(no changes)".
        return "error: {}".format((err or out or "git failed").strip()[:500])
    return out.strip() or "(no changes)"


def tool_write_self_config(args, workspace):
    """Write (create or overwrite) a file inside hearth's own configuration repo
    (HEARTH_REPO). Used by self-evolution to edit the flake. Refuses paths that
    escape the repo."""
    try:
        full = _repo_join(args.get("path", ""))
    except ValueError as exc:
        return "error: {}".format(exc)
    content = args.get("content", "")
    parent = os.path.dirname(full)
    if parent:
        try:
            os.makedirs(hearth_paths.long_path(parent), exist_ok=True)
        except OSError as exc:
            return "error: cannot create directory: {}".format(exc)
    try:
        n = _write_text_atomic(full, content)
    except OSError as exc:
        return "error: {}".format(_explain_oserror(exc))
    return "wrote {} ({} bytes) into the hearth repo".format(args.get("path"), n)


def tool_nix_check(args, workspace):
    """Validate hearth's flake by running `nix flake check --no-build` locally on
    HEARTH_REPO. Read-only (evaluates, does not change the system). Returns a line
    'nix_check PASS' or 'nix_check FAIL' plus the tail of the output, so an agent
    can see eval errors and fix them. The repo must be a flake (a flake.nix at its
    root)."""
    nix = _bin("nix", "/run/current-system/sw/bin/nix")
    try:
        r = subprocess.run(
            [nix, "--extra-experimental-features", "nix-command flakes",
             "flake", "check", "--no-build", HEARTH_REPO],
            capture_output=True, text=True, timeout=1200)
    except (OSError, subprocess.SubprocessError) as exc:
        return "error: {}".format(exc)
    tail = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()[-MAX_OUT:]
    return "nix_check {}\n{}".format("PASS" if r.returncode == 0 else "FAIL", tail)


def tool_remember(args, workspace):
    """Record a lesson into hearth's self-learning memory."""
    import hearth_memory
    db = hearth_paths.db_path()
    rid = hearth_memory.remember(db, args.get("insight", ""), kind=args.get("kind", "lesson"),
                                 tags=args.get("tags", ""), source="tool")
    return "remembered (id {})".format(rid) if rid else "error: nothing to remember"


def tool_recall(args, workspace):
    """Recall lessons from hearth's memory relevant to a query."""
    import hearth_memory
    db = hearth_paths.db_path()
    hits = hearth_memory.recall(db, args.get("query", ""), limit=int(args.get("limit") or 8))
    if not hits:
        return "(no relevant lessons yet)"
    return "\n".join("[{}] {}".format(h["kind"], h["insight"]) for h in hits)


def _kb_db():
    return hearth_paths.db_path()


def tool_kb_add(args, workspace):
    """Add a document to the local knowledge base. Provide source plus either text
    or a workspace path to read from."""
    import hearth_knowledge
    source = (args.get("source") or "").strip()
    if not source:
        return "error: a 'source' name is required"
    text = args.get("text")
    if not text and args.get("path"):
        try:
            # Same helper every manifest tool reads through (explicit UTF-8),
            # not a bare open() on the host's ANSI codepage. safe_join can
            # raise ValueError (containment) and _read_text can raise
            # UnicodeDecodeError (not valid UTF-8, e.g. a binary file);
            # UnicodeDecodeError is itself a ValueError subclass, so this one
            # except clause already covered it before this change too.
            text = _read_text(hearth_contain.safe_join(workspace, args.get("path")))
        except (OSError, ValueError) as exc:
            return "error: {}".format(exc)
    if not (text or "").strip():
        return "error: nothing to ingest (give 'text' or a readable 'path')"
    n = hearth_knowledge.ingest(_kb_db(), source, text, embed_fn=hearth_knowledge.make_embedder())
    return "added '{}' to the knowledge base ({} chunk{})".format(source, n, "s" if n != 1 else "")


def tool_replace_in_files(args, workspace):
    """Find/replace exact text across every matching file under a path (a
    multi-file refactor). Optional glob filters filenames. Returns a summary."""
    find = args.get("find")
    if not find:
        return "error: no 'find' text given"
    replace = args.get("replace", "")
    pattern = args.get("glob")
    try:
        base = hearth_contain.safe_join(workspace, args.get("path", "."))
    except ValueError as exc:
        return "error: {}".format(exc)
    blocked = _ignored_error(workspace, base, args.get("path", "."))
    if blocked:
        return blocked
    root = os.path.realpath(workspace)
    spec = hearth_contain.load_ignore(root)
    files_changed = 0
    total = 0
    for dirpath, dirs, files in os.walk(base):
        hearth_contain.prune(dirpath, dirs, _TREE_SKIP, root=root, ignore_spec=spec)
        for fn in sorted(files):
            if pattern and not _glob_match(fn, pattern):
                continue
            fp = os.path.join(dirpath, fn)
            try:
                hearth_contain.safe_join(root, os.path.relpath(fp, root))
            except ValueError:
                continue  # a link or an odd name that resolves outside the workspace
            if spec.is_ignored(os.path.relpath(fp, root), is_dir=False):
                continue
            if _hearthignore_protected(root, fp):
                continue  # .hearthignore is always protected, regardless of its own contents
            try:
                if os.path.getsize(hearth_paths.long_path(fp)) > 2_000_000:
                    continue
                content = _read_text(fp)
            except (OSError, UnicodeDecodeError):
                continue  # unreadable or not text: never rewrite it
            c = content.count(find)
            if c:
                try:
                    _write_text_atomic(fp, content.replace(find, replace))
                except OSError:
                    continue
                files_changed += 1
                total += c
    if not files_changed:
        return "no occurrences of that text found (no changes)"
    return "replaced {} occurrence(s) across {} file(s)".format(total, files_changed)


def tool_fetch_to_kb(args, workspace):
    """Fetch a web page's readable text and ingest it into the knowledge base in
    one step. Provide url (and optional source name)."""
    import hearth_knowledge
    url = args.get("url")
    if not url:
        return "error: no url"
    text = tool_web_fetch({"url": url}, workspace)
    if text.startswith("error:") or text == "(no readable text)":
        return "could not fetch: {}".format(text)
    source = args.get("source") or url
    n = hearth_knowledge.ingest(_kb_db(), source, text, embed_fn=hearth_knowledge.make_embedder())
    return "fetched and added '{}' to the knowledge base ({} chunk{})".format(source, n, "s" if n != 1 else "")


def tool_index_dir(args, workspace):
    """Index a directory (under the workspace) into the knowledge base so it can
    be searched. Provide name and path; optional comma-separated globs."""
    import hearth_project
    name = (args.get("name") or "").strip()
    if not name:
        return "error: a project 'name' is required"
    try:
        root = hearth_contain.safe_join(workspace, args.get("path", "."))
    except ValueError as exc:
        return "error: {}".format(exc)
    globs = None
    if args.get("globs"):
        globs = [g.strip() for g in str(args["globs"]).split(",") if g.strip()]
    import hearth_knowledge
    res = hearth_project.index_dir(_kb_db(), name, root, embed_fn=hearth_knowledge.make_embedder(), globs=globs)
    if res.get("error"):
        return "error: {}".format(res["error"])
    return ("indexed '{}': {} files, {} chunks ({} skipped){}").format(
        name, res["files"], res["chunks"], res["skipped"], ", truncated" if res["truncated"] else "")


def tool_kb_search(args, workspace):
    """Search the local knowledge base for chunks relevant to a query."""
    import hearth_knowledge
    hits = hearth_knowledge.search(_kb_db(), args.get("query", ""), limit=int(args.get("limit") or 5),
                                   embed_fn=hearth_knowledge.make_embedder())
    if not hits:
        return "(no relevant knowledge found)"
    return "\n\n".join("[{} #{}] (score {})\n{}".format(h["source"], h["chunk"], h["score"], h["text"])
                       for h in hits)


# The capability manifest for the Windows desktop app. Passed as `allowed_tools`,
# which permissions.decide treats as a hard cap in every mode including bypass.
#
# Deliberately excluded: the NixOS tools (nix_check, current_generation,
# list_generations, read_self_config, write_self_config, system_health) which
# have no Windows meaning and burn a turn each when a small model tries them;
# the knowledge and memory tools, which are M5; and the network tools
# (web_fetch, web_search, http_request, fetch_to_kb), which are portable but not
# part of a local coding agent's job and which would reopen the egress question.
WINDOWS_TOOLS = (
    "read_file", "write_file", "edit_file", "list_files", "list_tree",
    "search_files", "replace_in_files", "run_command", "git_status", "git_diff",
)

TOOLS = [
    {
        "name": "run_command",
        "description": "Run a shell command in the workspace. Use for building, testing, and inspecting code.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "the shell command"},
            "timeout": {"type": "integer", "description": "seconds to allow, default 120, max 1800"}},
            "required": ["command"]},
        "fn": tool_run_command,
    },
    {
        "name": "write_file",
        "description": "Write (create or overwrite) a file in the workspace.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]},
        "fn": tool_write_file,
    },
    {
        "name": "read_file",
        "description": "Read a file from the workspace.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]},
        "fn": tool_read_file,
    },
    {
        "name": "list_files",
        "description": "List files in a workspace directory.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        "fn": tool_list_files,
    },
    {
        "name": "list_tree",
        "description": "Show an indented directory tree under a path (skips .git, __pycache__, node_modules). Use to understand a project's layout.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "max_depth": {"type": "integer"}}},
        "fn": tool_list_tree,
    },
    {
        "name": "search_files",
        "description": "Search files under a path for a regex or text query (like grep). Returns path:line: text. Optional glob filters filenames (e.g. *.py).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"}},
            "required": ["query"]},
        "fn": tool_search_files,
    },
    {
        "name": "edit_file",
        "description": "Edit a file by exact text replacement: replaces 'find' with 'replace' (first match, or all=true for every match). Errors without changing anything if 'find' is not present.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "find": {"type": "string"},
            "replace": {"type": "string"}, "all": {"type": "boolean"}},
            "required": ["path", "find"]},
        "fn": tool_edit_file,
    },
    {
        "name": "web_fetch",
        "description": "Fetch a web page and return its readable text (HTML tags stripped). Provide url.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}},
                       "required": ["url"]},
        "fn": tool_web_fetch,
    },
    {
        "name": "web_search",
        "description": "Search the web and return top result titles, URLs, and snippets. Provide query.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "max_results": {"type": "integer"}},
            "required": ["query"]},
        "fn": tool_web_search,
    },
    {
        "name": "http_request",
        "description": "Make an HTTP request to an external API. Provide url, optional method, headers, body.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}, "method": {"type": "string"},
            "headers": {"type": "object"}, "body": {"type": "string"}},
            "required": ["url"]},
        "fn": tool_http_request,
    },
    {
        "name": "current_generation",
        "description": "Report hearth's active NixOS generation number and its build date. Read-only.",
        "parameters": {"type": "object", "properties": {}},
        "fn": tool_current_generation,
    },
    {
        "name": "list_generations",
        "description": "List NixOS system generations (number and build date), marking the active one. Read-only.",
        "parameters": {"type": "object", "properties": {}},
        "fn": tool_list_generations,
    },
    {
        "name": "system_health",
        "description": "Report system health: systemd status, failed units, disk, memory, GPU. Read-only.",
        "parameters": {"type": "object", "properties": {}},
        "fn": tool_system_health,
    },
    {
        "name": "read_self_config",
        "description": "Read a file from hearth's own NixOS configuration repo (the flake source). Provide path relative to the repo root.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]},
        "fn": tool_read_self_config,
    },
    {
        "name": "git_status",
        "description": "Show git status of the workspace. Read-only.",
        "parameters": {"type": "object", "properties": {}},
        "fn": tool_git_status,
    },
    {
        "name": "git_diff",
        "description": "Show git diff (stat) of the workspace, optionally for one path. Read-only.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        "fn": tool_git_diff,
    },
    {
        "name": "write_self_config",
        "description": "Write (create or overwrite) a file inside hearth's own NixOS configuration repo, to propose a change to the system. Provide path (relative to the repo root) and content.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]},
        "fn": tool_write_self_config,
    },
    {
        "name": "nix_check",
        "description": "Validate hearth's flake by running nix flake check locally (no build). Use after editing the config to confirm it still evaluates. Read-only.",
        "parameters": {"type": "object", "properties": {}},
        "fn": tool_nix_check,
    },
    {
        "name": "remember",
        "description": "Record a lesson into hearth's long-term memory so future runs can recall it. Provide insight (the lesson), optional kind and tags.",
        "parameters": {"type": "object", "properties": {
            "insight": {"type": "string"}, "kind": {"type": "string"}, "tags": {"type": "string"}},
            "required": ["insight"]},
        "fn": tool_remember,
    },
    {
        "name": "recall",
        "description": "Recall lessons from hearth's long-term memory relevant to a query.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        "fn": tool_recall,
    },
    {
        "name": "kb_add",
        "description": "Add a document to the local knowledge base for later retrieval. Provide a source name and either text or a workspace path to read.",
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string"}, "text": {"type": "string"}, "path": {"type": "string"}},
            "required": ["source"]},
        "fn": tool_kb_add,
    },
    {
        "name": "kb_search",
        "description": "Search the local knowledge base for the chunks most relevant to a query, to ground an answer in ingested documents.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"]},
        "fn": tool_kb_search,
    },
    {
        "name": "index_dir",
        "description": "Index a directory of code/text files (under the workspace) into the knowledge base so you can search it. Provide a project name and path. Use before kb_search to learn a codebase.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}, "path": {"type": "string"}, "globs": {"type": "string"}},
            "required": ["name", "path"]},
        "fn": tool_index_dir,
    },
    {
        "name": "replace_in_files",
        "description": "Find/replace exact text across all matching files under a path (a multi-file refactor). Optional glob filters filenames (e.g. *.py).",
        "parameters": {"type": "object", "properties": {
            "find": {"type": "string"}, "replace": {"type": "string"},
            "path": {"type": "string"}, "glob": {"type": "string"}},
            "required": ["find"]},
        "fn": tool_replace_in_files,
    },
    {
        "name": "fetch_to_kb",
        "description": "Fetch a web page and add its readable text to the knowledge base in one step. Provide url (and optional source name).",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}, "source": {"type": "string"}},
            "required": ["url"]},
        "fn": tool_fetch_to_kb,
    },
]

_BY_NAME = {t["name"]: t for t in TOOLS}


def ollama_tool_specs(allowed=None):
    """The tools in Ollama's chat tool format. When allowed (a collection of tool
    names, the run's capability manifest) is given, only those tools are
    advertised so the model never sees what it cannot use."""
    return [{"type": "function", "function": {
        "name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
        for t in TOOLS if allowed is None or t["name"] in allowed]


def execute_tool(name, args, workspace):
    tool = _BY_NAME.get(name)
    if tool is None:
        return "error: unknown tool {}".format(name)
    try:
        return tool["fn"](args or {}, workspace)
    except Exception as exc:  # noqa: BLE001 - a tool error must not crash the loop
        return "error: {}: {}".format(type(exc).__name__, exc)


def _self_test():
    import tempfile
    ws = tempfile.mkdtemp(prefix="hearth-tools-")
    assert "wrote" in execute_tool("write_file", {"path": "a/b.txt", "content": "hi"}, ws)
    assert execute_tool("read_file", {"path": "a/b.txt"}, ws) == "hi"
    assert "b.txt" in execute_tool("list_files", {"path": "a"}, ws)
    out = execute_tool("run_command", {"command": "echo hello"}, ws)
    assert "hello" in out and "exit=0" in out, out
    assert "escapes workspace" in execute_tool("write_file", {"path": "../evil", "content": "x"}, ws)
    assert len(ollama_tool_specs()) == len(TOOLS)
    # capability manifest filtering: only listed tools are advertised
    caps = ollama_tool_specs(allowed={"read_file", "write_file"})
    assert sorted(s["function"]["name"] for s in caps) == ["read_file", "write_file"], caps
    assert ollama_tool_specs(allowed=set()) == []
    # list_tree shows the layout; search_files greps; edit_file does find/replace.
    execute_tool("write_file", {"path": "src/app.py", "content": "x = 1\nprint(x)\n"}, ws)
    tree = execute_tool("list_tree", {}, ws)
    assert "src/" in tree and "app.py" in tree, tree
    hits = execute_tool("search_files", {"query": "print", "glob": "*.py"}, ws)
    assert "app.py" in hits and "print(x)" in hits, hits
    assert execute_tool("search_files", {"query": "nonexistent_zzz"}, ws) == "no matches"
    ed = execute_tool("edit_file", {"path": "src/app.py", "find": "x = 1", "replace": "x = 42"}, ws)
    assert "1 replacement" in ed, ed
    assert "x = 42" in execute_tool("read_file", {"path": "src/app.py"}, ws)
    # --- Finding 1, pinned with the actual consequence: a walk must never
    # descend into .venv. The three skip-directory lists (hearth_contain,
    # hearth_tools, hearth_project) used to diverge, and hearth_tools's own
    # copy (_TREE_SKIP) was the one missing ".venv" -- so search_files,
    # list_tree, and replace_in_files would all walk into a virtualenv and
    # replace_in_files could rewrite site-packages there. That damage is not
    # undoable: the checkpoint store's own built-in excludes (see
    # hearth_checkpoint._BUILTIN_EXCLUDES) never capture .venv/ either, so a
    # bad replace_in_files run had no undo path. _TREE_SKIP is now sourced
    # from hearth_contain.SKIP_DIRS, which does include ".venv".
    assert ".venv" in hearth_contain.SKIP_DIRS, "the authoritative list must cover .venv"
    assert ".venv" in _TREE_SKIP, "_TREE_SKIP must be sourced from hearth_contain.SKIP_DIRS, not a stale local copy"
    execute_tool("write_file", {"path": ".venv/lib/site-packages/evil.py", "content": "SENTINEL_VENV = 1\n"}, ws)
    venv_tree = execute_tool("list_tree", {}, ws)
    assert ".venv" not in venv_tree, ("list_tree walked into .venv", venv_tree)
    venv_hits = execute_tool("search_files", {"query": "SENTINEL_VENV"}, ws)
    assert venv_hits == "no matches", ("search_files walked into .venv", venv_hits)
    venv_rep = execute_tool("replace_in_files", {"find": "SENTINEL_VENV", "replace": "PWNED"}, ws)
    assert venv_rep == "no occurrences of that text found (no changes)", \
        ("replace_in_files rewrote a file inside .venv", venv_rep)
    assert execute_tool("read_file", {"path": ".venv/lib/site-packages/evil.py"}, ws) == "SENTINEL_VENV = 1\n", \
        "a file inside .venv must survive a replace_in_files run untouched"

    miss = execute_tool("edit_file", {"path": "src/app.py", "find": "not there", "replace": "y"}, ws)
    assert "not found" in miss, ("edit_file errors cleanly when find absent", miss)
    # knowledge base: add docs (inline + from a workspace file), then search.
    import tempfile as _tf2
    kbdb = os.path.join(_tf2.mkdtemp(prefix="hearth-kbtool-"), "a.db")
    old_db = os.environ.get("HEARTH_DB")
    old_embed = os.environ.get("HEARTH_EMBED_MODEL")
    os.environ["HEARTH_DB"] = kbdb
    os.environ["HEARTH_EMBED_MODEL"] = ""  # force deterministic offline TF-IDF in the test
    try:
        assert "added" in execute_tool("kb_add", {"source": "nix", "text": "NixOS rolls back atomically from a flake."}, ws)
        execute_tool("write_file", {"path": "notes.txt", "content": "Ollama serves local models on the GPU."}, ws)
        assert "added" in execute_tool("kb_add", {"source": "notes", "path": "notes.txt"}, ws)
        # kb_add now reads via _read_text (explicit UTF-8, strict), the same
        # helper every manifest tool reads through, instead of a bare
        # open(..., errors="replace") on the host codepage that silently
        # mangled bad bytes into replacement characters and ingested the
        # corrupted result without complaint. A genuinely undecodable file
        # must now surface as an error instead.
        badenc = os.path.join(ws, "bad_enc.txt")
        with open(badenc, "wb") as fh:
            fh.write(b"before \xff\xfe after")
        bad_res = execute_tool("kb_add", {"source": "badenc", "path": "bad_enc.txt"}, ws)
        assert bad_res.startswith("error:"), \
            ("kb_add must surface an undecodable file as an error, not silently ingest replacement characters", bad_res)
        found = execute_tool("kb_search", {"query": "rollback flake"}, ws)
        assert "nix" in found and "atomically" in found, found
        # index_dir: ingest a workspace subtree, then it is searchable
        execute_tool("write_file", {"path": "proj/readme.md", "content": "The widget service handles billing events."}, ws)
        idx = execute_tool("index_dir", {"name": "proj", "path": "proj"}, ws)
        assert "indexed 'proj'" in idx and "1 files" in idx, idx
        assert "billing" in execute_tool("kb_search", {"query": "widget billing events"}, ws)
        assert "no relevant" in execute_tool("kb_search", {"query": "zzz_nothing_qqq"}, ws)
        # replace_in_files: multi-file find/replace
        execute_tool("write_file", {"path": "r/a.py", "content": "VER = 1\n"}, ws)
        execute_tool("write_file", {"path": "r/b.py", "content": "x = VER\nVER = 2\n"}, ws)
        rep = execute_tool("replace_in_files", {"find": "VER", "replace": "VERSION", "path": "r", "glob": "*.py"}, ws)
        assert "3 occurrence" in rep and "2 file" in rep, rep
        assert "VERSION = 1" in execute_tool("read_file", {"path": "r/a.py"}, ws)
        assert execute_tool("replace_in_files", {"find": "nope_zzz"}, ws) == "no occurrences of that text found (no changes)"
    finally:
        if old_db is None:
            os.environ.pop("HEARTH_DB", None)
        else:
            os.environ["HEARTH_DB"] = old_db
        if old_embed is None:
            os.environ.pop("HEARTH_EMBED_MODEL", None)
        else:
            os.environ["HEARTH_EMBED_MODEL"] = old_embed
    # web_fetch: the HTML-to-text helper strips tags and collapses whitespace.
    sample = "<html><head><style>x{}</style><script>var a=1;</script></head>" \
             "<body><h1>Title</h1><p>Hello   world</p><p>Line two</p></body></html>"
    txt = _html_to_text(sample)
    assert "Title" in txt and "Hello world" in txt and "Line two" in txt, txt
    assert "var a=1" not in txt and "x{}" not in txt, ("script/style stripped", txt)
    # web_search: the DDG result parser extracts title/url/snippet from result HTML.
    ddg = ('<div class="result"><a class="result__a" '
           'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fnixos.org%2F&rut=x">NixOS</a>'
           '<a class="result__snippet">Reproducible builds and deployments.</a></div>')
    res = _parse_ddg_results(ddg, 5)
    assert res and res[0]["title"] == "NixOS", res
    assert res[0]["url"] == "https://nixos.org/", res
    assert "Reproducible" in res[0]["snippet"], res
    # Credential scoping: HEARTH_ALLOWED_CREDS limits which creds resolve.
    import tempfile as _tf
    cdir = _tf.mkdtemp(prefix="hearth-creds-")
    with open(os.path.join(cdir, "creds"), "w") as fh:
        fh.write("alpha=secretA\nbravo=secretB\n")
    old_cd = os.environ.get("CREDENTIALS_DIRECTORY")
    old_allow = os.environ.get("HEARTH_ALLOWED_CREDS")
    os.environ["CREDENTIALS_DIRECTORY"] = cdir
    try:
        os.environ.pop("HEARTH_ALLOWED_CREDS", None)
        assert _resolve_cred("alpha") == "secretA", "no allow-list: all resolve"
        assert _resolve_cred("bravo") == "secretB", "no allow-list: all resolve"
        os.environ["HEARTH_ALLOWED_CREDS"] = "alpha"
        assert _resolve_cred("alpha") == "secretA", "allowed cred resolves"
        assert _resolve_cred("bravo") == "", "disallowed cred is withheld"
    finally:
        if old_cd is None:
            os.environ.pop("CREDENTIALS_DIRECTORY", None)
        else:
            os.environ["CREDENTIALS_DIRECTORY"] = old_cd
        if old_allow is None:
            os.environ.pop("HEARTH_ALLOWED_CREDS", None)
        else:
            os.environ["HEARTH_ALLOWED_CREDS"] = old_allow

    # Egress allowlist: HEARTH_ALLOWED_HOSTS gates outbound tools; loopback is
    # always allowed; attempts (allowed and blocked) land in egress_log.
    import tempfile as _tfe2
    _edb = os.path.join(_tfe2.mkdtemp(prefix="hearth-egress-"), "a.db")
    _old_edb = os.environ.get("HEARTH_DB")
    _old_hosts = os.environ.get("HEARTH_ALLOWED_HOSTS")
    _old_aid = os.environ.get("HEARTH_AGENT_ID")
    os.environ["HEARTH_DB"] = _edb
    os.environ["HEARTH_AGENT_ID"] = "egress-test"
    try:
        os.environ.pop("HEARTH_ALLOWED_HOSTS", None)
        assert _host_allowed("example.com"), "no allowlist: everything allowed"
        os.environ["HEARTH_ALLOWED_HOSTS"] = "example.com, github.com"
        assert _host_allowed("example.com"), "exact match"
        assert _host_allowed("api.github.com"), "subdomain of an entry"
        assert not _host_allowed("evil.com"), "unlisted host blocked"
        assert not _host_allowed("notgithub.com"), "suffix must be a subdomain boundary"
        assert _host_allowed("127.0.0.1") and _host_allowed("localhost"), "loopback always allowed"
        # the check runs before any network I/O: a blocked host returns the
        # egress error (never a DNS/network error) and logs a blocked row
        out = tool_http_request({"url": "https://evil.com/x"}, ws)
        assert out.startswith("error: egress blocked"), out
        out2 = tool_web_fetch({"url": "https://evil.com/x"}, ws)
        assert out2.startswith("error: egress blocked"), out2
        os.environ["HEARTH_ALLOWED_HOSTS"] = "example.com"
        outs = tool_web_search({"query": "x"}, ws)
        assert "egress blocked" in outs and "duckduckgo" in outs, outs
        con = sqlite3.connect(_edb)
        rows = con.execute("SELECT tool, host, allowed FROM egress_log "
                           "WHERE agent_id='egress-test' ORDER BY id").fetchall()
        con.close()
        assert ("http_request", "evil.com", 0) in rows, rows
        assert ("web_fetch", "evil.com", 0) in rows, rows
        assert ("web_search", "html.duckduckgo.com", 0) in rows, rows
    finally:
        if _old_edb is None:
            os.environ.pop("HEARTH_DB", None)
        else:
            os.environ["HEARTH_DB"] = _old_edb
        if _old_hosts is None:
            os.environ.pop("HEARTH_ALLOWED_HOSTS", None)
        else:
            os.environ["HEARTH_ALLOWED_HOSTS"] = _old_hosts
        if _old_aid is None:
            os.environ.pop("HEARTH_AGENT_ID", None)
        else:
            os.environ["HEARTH_AGENT_ID"] = _old_aid

    # Honeyfile decoys: plant them, confirm they carry a unique canary token, and
    # that find_canary detects a token in text (and only a real token).
    import tempfile as _tfd
    dws = _tfd.mkdtemp(prefix="hearth-decoy-")
    planted = plant_decoys(dws)
    assert ".aws/credentials" in planted and ".env.production" in planted, planted
    tokens = set()
    for rel in planted:
        with open(os.path.join(dws, rel)) as fh:
            body = fh.read()
        tok = find_canary(body)
        assert tok and tok.startswith("HEARTH-CANARY-"), (rel, body)
        tokens.add(tok)
    assert len(tokens) == len(planted), ("each decoy gets a unique token", tokens)

    # plant_decoys now writes via _write_text_atomic (explicit UTF-8), not a
    # bare open(full, "w") on the host's ANSI codepage, matching the ten
    # manifest tools. Proven with a template containing non-ASCII text: on a
    # cp1252-default host, a bare open()'s implicit encoding would raise
    # UnicodeEncodeError (or mangle the bytes, depending on the character),
    # rather than writing the real UTF-8 bytes this asserts on.
    _old_templates = dict(_DECOY_TEMPLATES)
    _DECOY_TEMPLATES["unicode.txt"] = "canary={canary} café ✓ 日本語\n"
    try:
        planted_u = plant_decoys(dws)
        assert "unicode.txt" in planted_u, planted_u
        with open(os.path.join(dws, "unicode.txt"), "rb") as fh:
            raw = fh.read()
        assert "café ✓ 日本語".encode("utf-8") in raw, \
            ("plant_decoys must write explicit UTF-8, not the host codepage", raw)
    finally:
        _DECOY_TEMPLATES.clear()
        _DECOY_TEMPLATES.update(_old_templates)

    assert find_canary("nothing to see here") is None
    assert find_canary("leak: HEARTH-CANARY-0123456789abcdef done") == "HEARTH-CANARY-0123456789abcdef"
    # a decoy is a real readable file that a shell "cat"-equivalent would
    # surface. Windows has no cat on a clean PATH, so use the platform's
    # native file-dump command; either way this exercises the shell path,
    # not tool_read_file.
    _cat_cmd = "type .env.production" if hearth_paths.is_windows() else "cat .env.production"
    catout = execute_tool("run_command", {"command": _cat_cmd}, dws)
    assert find_canary(catout), ("shell cat surfaces the canary", catout)

    # current_generation: parse the generation number, and confirm the tool
    # never crashes even where the system profile is absent (dev/non-NixOS).
    assert _parse_generation("system-123-link") == "123"
    assert _parse_generation("garbage") == "unknown"
    cg = execute_tool("current_generation", {}, ws)
    assert isinstance(cg, str) and cg, cg

    # self-knowledge tools: parsers are exact; tools never crash off-NixOS.
    assert _meminfo_summary("MemTotal: 16384000 kB\nMemAvailable: 8192000 kB\n") == (8000, 16000)
    try:
        _repo_join("../../etc/passwd")
        assert False, "expected escape guard to raise"
    except ValueError:
        pass
    for name in ("list_generations", "system_health", "git_status", "git_diff"):
        out = execute_tool(name, {}, ws)
        assert isinstance(out, str) and out, (name, out)
    assert isinstance(execute_tool("read_self_config", {"path": "flake.nix"}, ws), str)

    # self-evolution tools: write into the repo (guarded), and nix_check never crashes.
    import tempfile as _tfe
    repo = _tfe.mkdtemp(prefix="hearth-selfrepo-")
    _old_repo = globals()["HEARTH_REPO"]
    globals()["HEARTH_REPO"] = repo
    try:
        out = execute_tool("write_self_config", {"path": "sub/x.nix", "content": "# hi\n"}, ws)
        assert "wrote" in out, out
        with open(os.path.join(repo, "sub", "x.nix")) as fh:
            assert fh.read() == "# hi\n"
        assert "escapes" in execute_tool("write_self_config", {"path": "../evil", "content": "x"}, ws)
        nc = execute_tool("nix_check", {}, ws)
        assert isinstance(nc, str) and nc, nc  # on dev (no nix) it returns an error string, never crashes
    finally:
        globals()["HEARTH_REPO"] = _old_repo

    # memory tools round-trip via the shared store (uses HEARTH_DB env).
    import tempfile as _tfm2
    _mdb = os.path.join(_tfm2.mkdtemp(prefix="hearth-memtool-"), "a.db")
    _oldhdb = os.environ.get("HEARTH_DB")
    os.environ["HEARTH_DB"] = _mdb
    try:
        assert "remembered" in execute_tool("remember", {"insight": "always run nix_check before merging"}, ws)
        assert "nix_check" in execute_tool("recall", {"query": "nix_check merging"}, ws)
    finally:
        if _oldhdb is None:
            os.environ.pop("HEARTH_DB", None)
        else:
            os.environ["HEARTH_DB"] = _oldhdb

    # Containment is delegated to hearth_contain, and the old private
    # helper is gone. A tool must refuse an escaping path rather than
    # resolving it.
    assert not hasattr(sys.modules[__name__], "_safe_join"), "_safe_join should be deleted"

    import shutil as _shutil
    import tempfile as _tempfile
    _ws = os.path.realpath(_tempfile.mkdtemp(prefix="hearth-tools-"))
    try:
        assert "escapes workspace" in tool_write_file({"path": "../evil.txt", "content": "x"}, _ws)
        assert "refusing path" in tool_write_file({"path": "NUL", "content": "x"}, _ws)
        assert "refusing path" in tool_write_file({"path": "a.txt:hidden", "content": "x"}, _ws)
        assert "escapes workspace" in tool_read_file({"path": "../../etc/passwd"}, _ws)
        assert "escapes workspace" in tool_list_files({"path": ".."}, _ws)
        assert "escapes workspace" in tool_search_files({"query": "x", "path": ".."}, _ws)
    finally:
        _shutil.rmtree(_ws, ignore_errors=True)

    # A junction inside the workspace must not be walked. Creating one needs no
    # admin rights, and the agent can create one through run_command, so the
    # walking tools cannot assume the workspace tree is free of them.
    _ws2 = os.path.realpath(_tempfile.mkdtemp(prefix="hearth-walk-"))
    _out = os.path.realpath(_tempfile.mkdtemp(prefix="hearth-secret-"))
    try:
        with open(os.path.join(_out, "secret.txt"), "w", encoding="utf-8") as fh:
            fh.write("SENTINEL_VALUE_DO_NOT_LEAK\n")
        with open(os.path.join(_ws2, "inside.txt"), "w", encoding="utf-8") as fh:
            fh.write("ordinary\n")

        _linked = False
        _link = os.path.join(_ws2, "escape")
        if hearth_paths.is_windows():
            _linked = os.system('mklink /J "{}" "{}" >nul 2>&1'.format(_link, _out)) == 0
        else:
            os.symlink(_out, _link)
            _linked = True

        if _linked:
            hits = tool_search_files({"query": "SENTINEL_VALUE_DO_NOT_LEAK"}, _ws2)
            assert "SENTINEL" not in hits, "search_files walked through the link: {}".format(hits)

            tree = tool_list_tree({"path": "."}, _ws2)
            assert "secret.txt" not in tree, "list_tree walked through the link"

            res = tool_replace_in_files({"find": "SENTINEL_VALUE_DO_NOT_LEAK", "replace": "PWNED"}, _ws2)
            with open(os.path.join(_out, "secret.txt"), encoding="utf-8") as fh:
                assert "SENTINEL_VALUE_DO_NOT_LEAK" in fh.read(), \
                    "replace_in_files rewrote a file outside the workspace: {}".format(res)
        else:
            print("warning: link creation failed, skipping containment assertions")
    finally:
        _shutil.rmtree(_ws2, ignore_errors=True)
        _shutil.rmtree(_out, ignore_errors=True)

    # Unicode round-trips regardless of the host ANSI codepage. On a cp1252
    # machine the unpatched code cannot write a checkmark at all.
    _ws3 = os.path.realpath(_tempfile.mkdtemp(prefix="hearth-enc-"))
    try:
        sample = "café ✓ 日本語\n"
        res = tool_write_file({"path": "u.txt", "content": sample}, _ws3)
        assert "error" not in res, res
        back = tool_read_file({"path": "u.txt"}, _ws3)
        assert back == sample, repr(back)

        # The byte count reported is real bytes on disk, not characters.
        assert "({} bytes)".format(len(sample.encode("utf-8"))) in res, res

        # A successful write leaves no temp file behind.
        target = os.path.join(_ws3, "keep.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("PRECIOUS")
        tool_write_file({"path": "keep.txt", "content": "REPLACED"}, _ws3)
        with open(target, encoding="utf-8") as fh:
            assert fh.read() == "REPLACED"
        assert not [f for f in os.listdir(_ws3) if f.startswith(".hearth-tmp")], "temp file leaked"

        # A failed write must not destroy the existing file, must not leave a
        # temp file behind, and must surface a useful error string rather
        # than crash. Inject a real failure at the atomic-replace step (not
        # just a bystander directory that has no bearing on the outcome).
        target2 = os.path.join(_ws3, "keep2.txt")
        with open(target2, "w", encoding="utf-8") as fh:
            fh.write("PRECIOUS2")
        _real_replace = os.replace

        def _boom_replace(*_a, **_kw):
            raise OSError(32, "simulated: file in use")

        os.replace = _boom_replace
        try:
            out = tool_write_file({"path": "keep2.txt", "content": "REPLACED2"}, _ws3)
        finally:
            os.replace = _real_replace
        assert out.startswith("error:"), ("a failed write must report an error, not crash", out)
        with open(target2, encoding="utf-8") as fh:
            assert fh.read() == "PRECIOUS2", "a failed write destroyed the original file"
        assert not [f for f in os.listdir(_ws3) if f.startswith(".hearth-tmp")], \
            "a failed write left a temp file behind"

        # Permissions on the destination survive an atomic replace, and a
        # brand-new file gets the ordinary umask default rather than some
        # hardcoded value (the earlier mkstemp-based implementation got this
        # wrong: it hardcoded 0o600 on every temp file regardless of umask,
        # which happened to equal a naive "existing file" test's expected
        # value too, so that test passed whether or not preservation code
        # ran at all). Deliberately avoid 0o600 and any value that might
        # coincide with the live umask default, so both assertions can only
        # pass if the code is actually doing the right thing.
        if not hearth_paths.is_windows():
            # Existing-file case: the mode must be preserved exactly.
            permf = os.path.join(_ws3, "restricted.txt")
            with open(permf, "w", encoding="utf-8") as fh:
                fh.write("secret")
            os.chmod(permf, 0o640)
            tool_write_file({"path": "restricted.txt", "content": "secret2"}, _ws3)
            assert (os.stat(permf).st_mode & 0o777) == 0o640, \
                "permissions were not preserved across an atomic replace"

            # New-file case: must get the live umask default, not a
            # hardcoded mode. Read the umask via the standard get-and-restore
            # dance (os.umask has no read-only peek); fine in a
            # single-threaded self-test, not safe to do under threads.
            _um = os.umask(0)
            os.umask(_um)
            expected_new = 0o666 & ~_um
            newf = os.path.join(_ws3, "brand_new.txt")
            tool_write_file({"path": "brand_new.txt", "content": "fresh"}, _ws3)
            assert (os.stat(newf).st_mode & 0o777) == expected_new, (
                "a new file must get the umask-derived default, not a hardcoded mode",
                oct(os.stat(newf).st_mode & 0o777), oct(expected_new))
        else:
            # NOTE: this branch does not exercise _copy_mode. Verified by
            # direct experiment that it cannot: os.replace() on Windows
            # refuses to overwrite a read-only destination (WinError 5)
            # regardless of the source file's mode, and a writable
            # destination's temp file is already writable by construction
            # (os.open's mode 0o666 default). There is no Windows-reachable
            # state where deleting the _copy_mode call changes any outcome
            # a test could observe -- confirmed by removing the call and
            # re-running self-test, which still passed (see the fix report).
            # What IS worth asserting on Windows is the safety property the
            # platform actually gives us: a read-only file is refused, not
            # silently corrupted or widened.
            import stat as _stat
            permf = os.path.join(_ws3, "restricted_win.txt")
            with open(permf, "w", encoding="utf-8") as fh:
                fh.write("secret")
            os.chmod(permf, _stat.S_IREAD)
            out = tool_write_file({"path": "restricted_win.txt", "content": "secret2"}, _ws3)
            assert out.startswith("error:"), ("a read-only file must refuse the write, not crash", out)
            with open(permf, encoding="utf-8") as fh:
                assert fh.read() == "secret", "a refused write must not touch the original file"
            assert not (os.stat(permf).st_mode & _stat.S_IWRITE), \
                "the read-only attribute must survive a refused write"

        # _copy_mode itself, called directly rather than through
        # tool_write_file. Going through os.replace (as above) cannot prove
        # anything on Windows, since a read-only destination blocks replace
        # regardless of the source's mode, and a writable destination's temp
        # file was already writable by construction -- either way the outer
        # test would pass whether or not _copy_mode ran. Calling it directly
        # on a standalone pair of files sidesteps that and is mutation-
        # sensitive on both platforms.
        import stat as _stat3
        _cw = os.path.realpath(_tempfile.mkdtemp(prefix="hearth-copymode-"))
        try:
            cm_target = os.path.join(_cw, "cm_target.txt")
            cm_tmp = os.path.join(_cw, "cm_tmp.txt")
            with open(cm_target, "w", encoding="utf-8") as fh:
                fh.write("t")
            with open(cm_tmp, "w", encoding="utf-8") as fh:
                fh.write("u")
            if hearth_paths.is_windows():
                os.chmod(cm_target, _stat3.S_IREAD)
                _copy_mode(cm_target, cm_tmp)
                assert not (os.stat(cm_tmp).st_mode & _stat3.S_IWRITE), \
                    "_copy_mode did not carry the read-only attribute onto the temp file"
            else:
                os.chmod(cm_target, 0o640)
                _copy_mode(cm_target, cm_tmp)
                assert (os.stat(cm_tmp).st_mode & 0o777) == 0o640, \
                    "_copy_mode did not carry the target's mode onto the temp file"
        finally:
            _shutil.rmtree(_cw, ignore_errors=True)

        # An undecodable file is skipped, never truncated.
        binf = os.path.join(_ws3, "bin.dat")
        with open(binf, "wb") as fh:
            fh.write(b"\x00\xff\xfe binary \x00")
        before = open(binf, "rb").read()
        tool_replace_in_files({"find": "binary", "replace": "text"}, _ws3)
        assert open(binf, "rb").read() == before, "replace_in_files corrupted a binary file"
    finally:
        _shutil.rmtree(_ws3, ignore_errors=True)

    # An edit must not rewrite line endings across the whole file.
    _ws4 = os.path.realpath(_tempfile.mkdtemp(prefix="hearth-eol-"))
    try:
        lf = os.path.join(_ws4, "lf.txt")
        with open(lf, "wb") as fh:
            fh.write(b"one\ntwo\nthree\n")
        tool_edit_file({"path": "lf.txt", "find": "two", "replace": "TWO"}, _ws4)
        assert open(lf, "rb").read() == b"one\nTWO\nthree\n", open(lf, "rb").read()

        crlf = os.path.join(_ws4, "crlf.txt")
        with open(crlf, "wb") as fh:
            fh.write(b"one\r\ntwo\r\nthree\r\n")
        tool_edit_file({"path": "crlf.txt", "find": "two", "replace": "TWO"}, _ws4)
        assert open(crlf, "rb").read() == b"one\r\nTWO\r\nthree\r\n", open(crlf, "rb").read()

        # A find string spanning a CRLF line break must match a CRLF file.
        r = tool_edit_file({"path": "crlf.txt", "find": "one\r\nTWO", "replace": "X"}, _ws4)
        assert "not found" not in r, r

        assert _dominant_newline("a\r\nb\r\n") == "\r\n"
        assert _dominant_newline("a\nb\n") == "\n"
        assert _dominant_newline("a\r\nb\nc\n") == "\n"
        assert _dominant_newline("no newlines") == ""
    finally:
        _shutil.rmtree(_ws4, ignore_errors=True)

    # The Windows manifest is exactly the ten tools a coding agent needs.
    assert set(WINDOWS_TOOLS) == {
        "read_file", "write_file", "edit_file", "list_files", "list_tree",
        "search_files", "replace_in_files", "run_command", "git_status", "git_diff",
    }, WINDOWS_TOOLS
    # Every name in the manifest is a registered tool. (TOOLS is a list of dicts,
    # not names, so the check goes through the name->tool registry _BY_NAME.)
    for _t in WINDOWS_TOOLS:
        assert _t in _BY_NAME, "manifest names an unregistered tool: {}".format(_t)
    # NixOS-only tools are absent.
    for _t in ("nix_check", "current_generation", "list_generations", "read_self_config"):
        assert _t not in WINDOWS_TOOLS
    # The manifest filters the specs handed to the model.
    _specs = ollama_tool_specs(WINDOWS_TOOLS)
    assert len(_specs) == len(WINDOWS_TOOLS), len(_specs)

    # git tools read the run workspace, not HEARTH_REPO.
    _ws6 = os.path.realpath(_tempfile.mkdtemp(prefix="hearth-git-"))
    try:
        r = tool_git_status({}, _ws6)
        assert "/home/operator" not in r, r
        assert "not a git repository" in r.lower() or "git is not installed" in r.lower(), r
    finally:
        _shutil.rmtree(_ws6, ignore_errors=True)

    # --- RCE regression: tool_git_diff/tool_git_status run git via argv,
    # never a shell string. This is a direct regression test for a confirmed,
    # reachable RCE: git_diff's 'path' argument used to be interpolated into
    # a shell command string with only space-aware quoting, so
    # permissions.decide('plan', 'git_diff', {'path': 'x&whoami'}) returned
    # "allow" (git_diff is RISK-classified "safe") and the tool then ran
    # 'whoami' as a second shell command with no approval prompt.
    #
    # Assert on an OBSERVABLE SIDE EFFECT (a marker file failing to appear),
    # not just the returned string: a broken implementation could still
    # return a plausible-looking "error: pathspec ... did not match" string
    # while having executed the payload first (shell command chaining runs
    # left to right regardless of whether the final git invocation
    # succeeds), so checking only the return value would not have caught
    # this bug.
    if shutil.which("git"):
        _ws_rce = os.path.realpath(_tempfile.mkdtemp(prefix="hearth-git-rce-"))
        try:
            subprocess.run(["git", "init", "--quiet"], cwd=_ws_rce, capture_output=True)
            subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=_ws_rce, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=_ws_rce, capture_output=True)
            with open(os.path.join(_ws_rce, "f.txt"), "w") as fh:
                fh.write("one\n")
            subprocess.run(["git", "add", "f.txt"], cwd=_ws_rce, capture_output=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "init"], cwd=_ws_rce, capture_output=True)
            with open(os.path.join(_ws_rce, "f.txt"), "w") as fh:
                fh.write("two\n")

            marker = os.path.join(_ws_rce, "PWNED.txt")
            # Deliberately no spaces in any payload. The old vulnerable _q()
            # only quoted a value that CONTAINED a space, so a payload with
            # one (e.g. "touch PWNED.txt", "type nul") would get wrapped in
            # quotes and accidentally neutralized -- a false negative that
            # would prove nothing. "echo." (Windows) and a bare redirection
            # (POSIX) create the marker with no space anywhere, matching the
            # task's own reproduction ('x&echo.>PWNED.txt').
            if hearth_paths.is_windows():
                payloads = [
                    "x&echo.>PWNED.txt",
                    "x&&echo.>PWNED.txt",
                    "x|echo.>PWNED.txt",
                    "x\necho.>PWNED.txt",
                ]
            else:
                payloads = [
                    "x;>PWNED.txt",
                    "x|>PWNED.txt",
                    "x`>PWNED.txt`",
                    "x$(>PWNED.txt)",
                    "x\n>PWNED.txt",
                ]
            for payload in payloads:
                tool_git_diff({"path": payload}, _ws_rce)
                assert not os.path.exists(marker), (
                    "tool_git_diff let a shell-metacharacter payload reach a shell "
                    "(marker file created): path={!r}".format(payload))
                assert not os.path.exists(marker)  # re-check: paranoia costs nothing here

            # tool_git_status takes no model-controlled argument today, but
            # prove the argv-only property directly on the plumbing it now
            # shares with tool_git_diff (_run_git_argv), so a future change
            # that starts threading arguments through tool_git_status trips
            # this immediately rather than silently reopening the bug class.
            rc_probe, out_probe, err_probe = _run_git_argv(
                [shutil.which("git"), "status", "--short", "x&whoami"], _ws_rce, timeout=10)
            assert not os.path.exists(marker)

            # A path containing a space must still work: this is the entire
            # reason the old (now-deleted) _q() quoting helper existed, and
            # argv handles it with no quoting at all.
            _spaced_dir = os.path.join(_ws_rce, "has space")
            os.makedirs(_spaced_dir, exist_ok=True)
            _spaced_file = os.path.join(_spaced_dir, "f.txt")
            with open(_spaced_file, "w") as fh:
                fh.write("one\n")
            subprocess.run(["git", "add", "has space/f.txt"], cwd=_ws_rce, capture_output=True)
            with open(_spaced_file, "w") as fh:
                fh.write("two\n")
            out_spaced = tool_git_diff({"path": "has space/f.txt"}, _ws_rce)
            assert not out_spaced.startswith("error:"), out_spaced
            assert "f.txt" in out_spaced, out_spaced

            # Return code is checked, not masked: a genuine git failure (an
            # invalid pathspec, here) must be reported as an error, never
            # silently printed as "(no changes)" the way a swallowed
            # non-zero exit would read. A merely nonexistent path is NOT
            # this case: git diff on a pathspec that matches nothing exits 0
            # with empty output, which is correctly "(no changes)".
            bogus = tool_git_diff({"path": ":(icase,zzz)f.txt"}, _ws_rce)
            assert bogus.startswith("error:"), bogus
            assert bogus.strip() != "(no changes)", bogus
        finally:
            _shutil.rmtree(_ws_rce, ignore_errors=True)
    else:
        print("warning: git not on PATH, skipping git RCE regression tests")

    # --- Property test: every WINDOWS_TOOLS tool classified "safe" in
    # permissions.RISK must never let its arguments reach a shell.
    #
    # This is the regression guard that actually matters, because the class
    # of bug found here is "a safe-classified tool gained a shell": a tool
    # marked "safe" is auto-allowed in plan mode with no approval prompt, so
    # if any future edit routes a safe tool's arguments through a shell
    # string (the exact mistake tool_git_diff made), this trips immediately
    # instead of waiting for someone to notice in production.
    #
    # Generic over WINDOWS_TOOLS's own parameter schema (TOOLS[i]["parameters"])
    # rather than hardcoding which tools take which arguments, so a newly
    # added safe tool is covered automatically without editing this test.
    import permissions as _permissions
    _marker_name = "HEARTH_SHELL_PROBE_MARKER.txt"
    # No spaces in any payload -- see the comment on the RCE regression test
    # above for why a spaced payload (e.g. "touch X") would be a false
    # negative against the old _q()-style "quote only if it has a space"
    # defense.
    if hearth_paths.is_windows():
        _shell_payloads = [
            "x&echo.>{}".format(_marker_name),
            "x&&echo.>{}".format(_marker_name),
            "x|echo.>{}".format(_marker_name),
            "x\necho.>{}".format(_marker_name),
        ]
    else:
        _shell_payloads = [
            "x;>{}".format(_marker_name),
            "x|>{}".format(_marker_name),
            "x`>{}`".format(_marker_name),
            "x$(>{})".format(_marker_name),
            "x\n>{}".format(_marker_name),
        ]
    _safe_windows_tools = [t for t in WINDOWS_TOOLS if _permissions.risk_of(t) == "safe"]
    assert _safe_windows_tools, "expected at least one safe-classified Windows tool to check"
    assert "git_diff" in _safe_windows_tools and "git_status" in _safe_windows_tools, _safe_windows_tools
    for _tname in _safe_windows_tools:
        _tspec = _BY_NAME[_tname]
        _string_params = [p for p, s in _tspec["parameters"].get("properties", {}).items()
                          if s.get("type") == "string"]
        for _payload in _shell_payloads:
            _ws_probe = os.path.realpath(_tempfile.mkdtemp(prefix="hearth-shellprobe-"))
            try:
                _call_args = {p: _payload for p in _string_params}
                execute_tool(_tname, _call_args, _ws_probe)
                _marker_path = os.path.join(_ws_probe, _marker_name)
                assert not os.path.exists(_marker_path), (
                    "safe-classified tool {!r} let a shell-metacharacter payload reach a "
                    "shell (marker file created): args={}".format(_tname, _call_args))
            finally:
                _shutil.rmtree(_ws_probe, ignore_errors=True)

    # run_command reports a timeout promptly rather than blocking on
    # grandchildren, and never reports exit=0 with empty output for a command
    # that wrote bytes outside the host codepage.
    _ws5 = os.path.realpath(_tempfile.mkdtemp(prefix="hearth-cmd-"))
    try:
        r = tool_run_command({"command": "echo marker123"}, _ws5)
        assert "exit=0" in r and "marker123" in r, r

        import time as _time
        _slow = "ping -n 20 127.0.0.1 >nul" if hearth_paths.is_windows() else "sleep 20"
        _t0 = _time.time()
        r = tool_run_command({"command": _slow, "timeout": 3}, _ws5)
        assert "timed out" in r, r
        assert _time.time() - _t0 < 12, "run_command blocked past its timeout"

        # The hint layer must not tell a Windows user about a message cmd.exe
        # never emits.
        if hearth_paths.is_windows():
            r = tool_run_command({"command": "definitelynotarealbinary"}, _ws5)
            assert "not recognized" in r or "exit=" in r, r

        # The sandbox level is the user's setting, never the model's argument.
        # Every key in `args` arrived from a model that can be prompt-injected,
        # so a payload that asks for a weaker sandbox has to be ignored rather
        # than honoured. Proven by running the containment's own failure case
        # (a write outside the workspace) with every plausible spelling of
        # "turn the sandbox off" present in args.
        if hearth_paths.is_windows():
            _old_level = os.environ.get("HEARTH_SANDBOX")
            _probe = os.path.join(os.environ["USERPROFILE"], "hearth-tools-sandbox-probe.txt")
            try:
                os.environ["HEARTH_SANDBOX"] = "workspace"
                hearth_sandbox.label_tree(_ws5)
                r = tool_run_command({
                    "command": 'echo pwned> "{}"'.format(_probe),
                    "sandbox": "off", "level": "off", "HEARTH_SANDBOX": "off",
                }, _ws5)
                assert not os.path.exists(_probe), (
                    "a model-supplied argument turned the sandbox off: {}".format(r))
                assert "hint: the sandbox is at the 'workspace' level" in r, r
            finally:
                try:
                    hearth_sandbox.unlabel_tree(_ws5)
                except Exception:  # noqa: BLE001 - cleanup must not mask a failure
                    pass
                try:
                    os.unlink(_probe)
                except OSError:
                    pass
                if _old_level is None:
                    os.environ.pop("HEARTH_SANDBOX", None)
                else:
                    os.environ["HEARTH_SANDBOX"] = _old_level
    finally:
        _shutil.rmtree(_ws5, ignore_errors=True)

    # A transient lock is retried rather than surfaced immediately. Antivirus
    # and sync clients hold files open for tens of milliseconds at a time.
    _calls = {"n": 0}

    def _flaky():
        _calls["n"] += 1
        if _calls["n"] < 3:
            raise PermissionError(13, "in use")
        return "ok"

    assert _retry_io(_flaky, attempts=4, delay=0.01) == "ok"
    assert _calls["n"] == 3

    _always = {"n": 0}

    def _stuck():
        _always["n"] += 1
        raise PermissionError(13, "in use")

    try:
        _retry_io(_stuck, attempts=3, delay=0.01)
        raise AssertionError("a permanently locked file should still raise")
    except PermissionError:
        pass
    assert _always["n"] == 3, _always

    # Glob filters behave identically on both platforms.
    assert _glob_match("App.PY", "*.py") is False
    assert _glob_match("app.py", "*.py") is True
    assert _glob_match("app.py", "*.PY") is False

    # Writing a differently-cased name is reported, not silently merged.
    _ws7 = os.path.realpath(_tempfile.mkdtemp(prefix="hearth-case-"))
    try:
        tool_write_file({"path": "README.md", "content": "A"}, _ws7)
        if hearth_paths.is_windows():
            r = tool_write_file({"path": "readme.md", "content": "B"}, _ws7)
            assert "already exists as" in r, r
            with open(os.path.join(_ws7, "README.md"), encoding="utf-8") as fh:
                assert fh.read() == "A", "the original was overwritten"
        else:
            r = tool_write_file({"path": "readme.md", "content": "B"}, _ws7)
            assert "wrote" in r, r
            with open(os.path.join(_ws7, "README.md"), encoding="utf-8") as fh:
                assert fh.read() == "A", "case-insensitivity check must not touch a case-sensitive filesystem"
        # Rewriting the same name with the same case is fine.
        r = tool_write_file({"path": "README.md", "content": "C"}, _ws7)
        assert "wrote" in r, r
    finally:
        _shutil.rmtree(_ws7, ignore_errors=True)

    # --- .hearthignore: every file tool must respect it. ---
    #
    # A walk must not descend into an ignored directory, asserted on REAL
    # ON-DISK STATE (the sentinel file's content and mtime), not merely on
    # the string a tool returns -- a broken implementation could still
    # print a plausible "no matches" while having opened the file first.
    _wsi = os.path.realpath(_tempfile.mkdtemp(prefix="hearth-ignore-walk-"))
    try:
        with open(os.path.join(_wsi, ".hearthignore"), "w", encoding="utf-8") as fh:
            fh.write("secrets/\n*.secret\n")
        os.makedirs(os.path.join(_wsi, "secrets"))
        secret_path = os.path.join(_wsi, "secrets", "creds.txt")
        with open(secret_path, "w", encoding="utf-8") as fh:
            fh.write("SENTINEL_IGNORE_WALK\n")
        loose_secret = os.path.join(_wsi, "top.secret")
        with open(loose_secret, "w", encoding="utf-8") as fh:
            fh.write("SENTINEL_LOOSE_SECRET\n")
        with open(os.path.join(_wsi, "visible.txt"), "w", encoding="utf-8") as fh:
            fh.write("ordinary\n")

        hits = execute_tool("search_files", {"query": "SENTINEL_IGNORE_WALK"}, _wsi)
        assert hits == "no matches", ("search_files walked into an ignored directory", hits)
        hits2 = execute_tool("search_files", {"query": "SENTINEL_LOOSE_SECRET"}, _wsi)
        assert hits2 == "no matches", ("search_files matched a file excluded by *.secret", hits2)

        tree = execute_tool("list_tree", {}, _wsi)
        assert "secrets" not in tree and "creds.txt" not in tree and "top.secret" not in tree, \
            ("list_tree showed an ignored path", tree)
        assert "visible.txt" in tree, tree

        listing = execute_tool("list_files", {}, _wsi)
        assert "secrets" not in listing and "top.secret" not in listing, listing
        assert "visible.txt" in listing, listing

        before_secret_mtime = os.stat(secret_path).st_mtime
        before_secret_body = open(secret_path, encoding="utf-8").read()
        rep = execute_tool("replace_in_files", {"find": "SENTINEL", "replace": "PWNED"}, _wsi)
        assert os.stat(secret_path).st_mtime == before_secret_mtime, \
            "replace_in_files touched a file inside an ignored directory (mtime changed)"
        assert open(secret_path, encoding="utf-8").read() == before_secret_body, \
            "replace_in_files rewrote a file inside an ignored directory"
        assert open(loose_secret, encoding="utf-8").read() == "SENTINEL_LOOSE_SECRET\n", \
            "replace_in_files rewrote a file excluded by *.secret"

        # Direct single-path tools error clearly, naming .hearthignore, so a
        # model that does not understand the refusal does not just retry it.
        r = execute_tool("read_file", {"path": "secrets/creds.txt"}, _wsi)
        assert "excluded by .hearthignore" in r, r
        r2 = execute_tool("read_file", {"path": "top.secret"}, _wsi)
        assert "excluded by .hearthignore" in r2, r2
        w = execute_tool("write_file", {"path": "secrets/creds.txt", "content": "x"}, _wsi)
        assert "excluded by .hearthignore" in w, w
        w2 = execute_tool("write_file", {"path": "secrets/new_file.txt", "content": "x"}, _wsi)
        assert "excluded by .hearthignore" in w2, \
            ("writing a NEW file inside an ignored directory must still be refused", w2)
        assert not os.path.exists(os.path.join(_wsi, "secrets", "new_file.txt")), \
            "a refused write must not create the file anyway"
        e = execute_tool("edit_file", {"path": "top.secret", "find": "SENTINEL", "replace": "X"}, _wsi)
        assert "excluded by .hearthignore" in e, e

        # A directory-level assertion directly against hearth_contain.prune
        # (not through a tool's formatted output): 'secrets' must be
        # dropped from the walk's own dirs list before os.walk ever visits
        # it, and an unrelated sibling directory must survive.
        _root_i = os.path.realpath(_wsi)
        _spec_i = hearth_contain.load_ignore(_root_i)
        _dirlist = ["secrets", "visible_dir"]
        hearth_contain.prune(_wsi, _dirlist, _TREE_SKIP, root=_root_i, ignore_spec=_spec_i)
        assert _dirlist == ["visible_dir"], _dirlist

        # Absence changes nothing: delete .hearthignore and the same paths
        # become visible and writable again -- proves the earlier refusals
        # came from the ignore file, not from some unrelated bug.
        os.remove(os.path.join(_wsi, ".hearthignore"))
        tree2 = execute_tool("list_tree", {}, _wsi)
        assert "creds.txt" in tree2 and "top.secret" in tree2, \
            ("removing .hearthignore must restore visibility", tree2)
        r3 = execute_tool("read_file", {"path": "top.secret"}, _wsi)
        assert r3.rstrip("\r\n") == "SENTINEL_LOOSE_SECRET", r3
    finally:
        _shutil.rmtree(_wsi, ignore_errors=True)

    # === Finding 4: the agent cannot erase its own .hearthignore scope. ===
    # === Reproduces the review's exact sequence: a read is refused, an ====
    # === attempted write_file(".hearthignore", "") is refused too (not ====
    # === silently accepted), and the SAME read is refused again ==========
    # === afterward -- proving scope was never actually erased, not just ==
    # === asserting the fix exists in isolation. ============================
    _wsp = os.path.realpath(_tempfile.mkdtemp(prefix="hearth-ignore-selfprotect-"))
    try:
        _hi_path = os.path.join(_wsp, ".hearthignore")
        with open(_hi_path, "w", encoding="utf-8") as fh:
            fh.write("secrets/\n")
        os.makedirs(os.path.join(_wsp, "secrets"))
        with open(os.path.join(_wsp, "secrets", "creds.txt"), "w", encoding="utf-8") as fh:
            fh.write("SENTINEL_STILL_SCOPED\n")
        _hi_before = open(_hi_path, encoding="utf-8").read()

        # THE PIN: a read the ignore file currently scopes out is refused
        # before the attack.
        r_before = execute_tool("read_file", {"path": "secrets/creds.txt"}, _wsp)
        assert "excluded by .hearthignore" in r_before, r_before

        # The attack: try to erase .hearthignore via each of the three
        # tools that can place new text into a file. Every one must be
        # refused, and the file on disk must be untouched byte-for-byte --
        # this is checked directly against the filesystem, not just the
        # tool's return string, the same discipline the walk assertions
        # above use.
        w = execute_tool("write_file", {"path": ".hearthignore", "content": ""}, _wsp)
        assert "protected" in w, w
        assert open(_hi_path, encoding="utf-8").read() == _hi_before, \
            "write_file(\".hearthignore\", \"\") must not have touched the file on disk"

        e = execute_tool("edit_file", {"path": ".hearthignore", "find": "secrets/", "replace": ""}, _wsp)
        assert "protected" in e, e
        assert open(_hi_path, encoding="utf-8").read() == _hi_before, \
            "edit_file must not have touched .hearthignore on disk"

        rp = execute_tool("replace_in_files", {"find": "secrets/", "replace": ""}, _wsp)
        assert open(_hi_path, encoding="utf-8").read() == _hi_before, \
            "replace_in_files must not have rewritten .hearthignore on disk: {}".format(rp)

        # THE PIN, the actual non-vacuous proof: the SAME read that was
        # refused before the attack is refused again afterward -- the
        # attack did not silently succeed even though every individual
        # tool call above reported failure.
        r_after = execute_tool("read_file", {"path": "secrets/creds.txt"}, _wsp)
        assert "excluded by .hearthignore" in r_after, \
            ("scope was erased despite every write attempt reporting failure", r_after)

        # A brand-new .hearthignore is protected too, even though it does
        # not exist yet -- the agent must not be able to create one via
        # write_file where none was there before.
        _wsp2 = os.path.realpath(_tempfile.mkdtemp(prefix="hearth-ignore-selfprotect-new-"))
        try:
            assert not os.path.exists(os.path.join(_wsp2, ".hearthignore"))
            w2 = execute_tool("write_file", {"path": ".hearthignore", "content": "*\n"}, _wsp2)
            assert "protected" in w2, w2
            assert not os.path.exists(os.path.join(_wsp2, ".hearthignore")), \
                "a refused write must not create .hearthignore anyway"
        finally:
            _shutil.rmtree(_wsp2, ignore_errors=True)

        # A user editing .hearthignore themselves, outside the agent
        # entirely, is unaffected: this protection only ever gates the
        # three tool functions above, never the filesystem itself.
        with open(_hi_path, "a", encoding="utf-8") as fh:
            fh.write("more/\n")
        assert open(_hi_path, encoding="utf-8").read() == _hi_before + "more/\n"
    finally:
        _shutil.rmtree(_wsp, ignore_errors=True)

    print("hearth-tools self-test OK")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
