#!/usr/bin/env python3
r"""hearth git-backed checkpoint and undo: cheap, trustworthy snapshots of a
workspace before an agent turn, so a bad turn can be reverted with one click.

Local models get things wrong often. Undo has to be a headline feature, not a
nicety, and it has to be boring and reliable: the one thing it must never do
is surprise the user by touching THEIR OWN git state. Their commits,
branches, staging area, stash, and reflog must be exactly as they left them,
whether or not the workspace happens to be a git repository at all.

DESIGN: a shadow git store. Every workspace gets its own GIT_DIR, owned by
Hearth, living under hearth_paths.checkpoints_dir() rather than inside the
workspace. core.worktree points that shadow repository at the workspace, so
`git add -A` there walks the user's real files, but the user's own .git (if
any) is never opened, read, or written by this module. Every call passes
--git-dir and --work-tree explicitly on the command line, in addition to
setting them in the shadow store's own config, because explicit --git-dir
disables git's normal upward repository discovery: even running with cwd
inside the user's real repository, a command that names our shadow GIT_DIR
never touches theirs. This is proven, not just asserted, by the self-test
below, which diffs the user's real HEAD/branch/status/stash/reflog before and
after a full checkpoint-and-restore round trip.

A checkpoint is a plain `git add -A` followed by a commit, not a diff of
files some edit tool touched. That means undo genuinely covers changes made
by shell commands too, not only calls that went through an edit tool: a
`rm`, a `sed -i`, a build script that rewrites a config file, all of it is
captured the same way, because capture is whole-tree, not an interception of
specific write calls. That is a real advantage over undo systems that only
track their own edits.

KNOWN CORRECTNESS HOLES, HANDLED HERE, NOT LEFT FOR SOMEONE TO DISCOVER:

  Nested git repositories. A directory inside the workspace that has its own
  .git is, by git's own repository-boundary detection, captured as a gitlink
  (mode 160000, just a commit sha) even when an ignore pattern names that
  directory: repository-boundary detection runs ahead of the ordinary
  gitignore check. `read-tree -u --reset` still exits 0 with a gitlink
  present, silently leaving that sub-tree's real content neither checkpointed
  nor restored. sub_repos() finds these with a bounded, prune-aware scan, and
  _add_all() excludes each one from every add -A with an explicit
  ":(exclude)path" pathspec, which is a hard "do not even look here" filter,
  not a mere ignore pattern. Nested repos are reported, never silently
  swallowed; see the "sub_repos" field on checkpoint()'s return value.

  Secrets. In a workspace with no .gitignore, a plain `git add -A` would pull
  .env straight into Hearth's own data directory. The shadow store ships a
  built-in $GIT_DIR/info/exclude covering common secret file patterns and the
  usual heavy directories. Anything excluded there is therefore NOT
  restorable: that tradeoff is deliberate and is spelled out in
  scope_limits().

  Not a git repo. The workspace very often will not be one. The shadow store
  does not care, because it owns its own GIT_DIR; init_store() works
  identically whether or not the workspace has ever seen "git init".

  Uncommitted user work. If the workspace IS a git repo with staged changes,
  an untracked file, or a stash entry, none of that is read from or written
  to by this module. The shadow store's index and the user's real index are
  different files in different directories; they never interact.

  Case-only renames cannot be detected by diff-index on a case-insensitive
  filesystem (the Windows and macOS default): a rename from Foo.txt to
  foo.txt looks like no change at all to git on such a filesystem. This is
  accepted and documented, not silently mishandled.

  Performance. A cold first checkpoint over a very large tree can take tens
  of seconds; warm ones are a fraction of a second because git only rehashes
  what actually changed. checkpoint() gates its "warning" field primarily on
  file count (see LARGE_TREE_WARNING_FILES) so the caller can surface a
  warning instead of the operation appearing to hang.

GIT PLUMBING NOTES:

  The shadow store is initialised with `git init --bare`, then core.worktree
  is set to the workspace, and every subsequent invocation ALSO passes
  --work-tree explicitly on the command line. This is the same pattern many
  "dotfiles as a bare git repo" setups use, and it sidesteps any ambiguity
  about whether a bare repository's core.worktree config is honoured for a
  given command: passing --work-tree directly is honoured unconditionally.

  $GIT_DIR/info/attributes is written as `* -text -filter -ident`. This is
  not optional. Without -text, git's CRLF normalisation on checkout can
  silently corrupt content on restore. Without -filter, a workspace using
  git-lfs would have its filter driver capture a small pointer file instead
  of the real content, and a restore would then overwrite real assets with
  that pointer text.

  core.longpaths is set to true on the shadow store. On Windows this is the
  difference between a silent failure and a working restore for paths beyond
  the historical 260-character MAX_PATH limit. It lets git itself handle
  those paths; it does not help Explorer, cmd.exe, or bundled msys tooling,
  which is exactly why hearth_paths.long_path() exists as a separate concern
  for this module's own os.walk() calls during sub-repo detection.

  Before a restore is reported as complete, `update-index --refresh` is run.
  Without it, a file rewritten with byte-identical content is reported as
  modified purely because its mtime changed, which would tell a user to
  close files that are not even open.

  Every diff this module reports filters out gitlink entries (mode 160000),
  per the nested-repository note above.

Standard library only. Shelling out to `git` is expected and is the whole
point; git plumbing commands are invoked with explicit argv lists (never a
shell string), because commit messages, labels, and workspace paths can
contain spaces, quotes, or Unicode that a shell-string command line would
have to escape, and get wrong, in exactly this kind of code.
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time

import hearth_contain
import hearth_paths


GIT_TIMEOUT_S = 180  # generous ceiling; a cold checkpoint over a huge tree is
                      # still expected to take tens of seconds, not minutes.

LARGE_TREE_WARNING_FILES = 5000  # checkpoint() gates its "warning" field on
                                  # file count first, per the module's
                                  # performance note, plus a wall-clock
                                  # fallback below.
LARGE_TREE_WARNING_SECONDS = 5.0

MAX_SCAN_DIRS = 20000  # bound on sub_repos()'s directory walk, so a
                        # pathological tree cannot hang detection.

_HEARTH_COMMITTER_NAME = "Hearth Checkpoint"
_HEARTH_COMMITTER_EMAIL = "checkpoint@hearth.local"

# Built-in excludes for the shadow store's own $GIT_DIR/info/exclude.
# Anything matched here is never captured by add -A, and is therefore never
# restorable. That tradeoff is deliberate: see the module docstring and
# scope_limits().
_BUILTIN_EXCLUDES = """\
# Hearth checkpoint store: built-in excludes.
# Anything matched here is never captured, and therefore never restorable.
.env*
*.pem
*.key
id_rsa*
*.pfx
credentials*
node_modules/
.venv/
__pycache__/
target/
dist/
build/
.git/
"""


def is_git_available():
    """True if a `git` executable is on PATH. Every other function in this
    module raises RuntimeError if called without one; callers that want to
    degrade gracefully (for example, a self-test) should check this first."""
    return shutil.which("git") is not None


def _find_git():
    exe = shutil.which("git")
    if not exe:
        raise RuntimeError(
            "git is not installed or not on PATH; the checkpoint store requires it"
        )
    return exe


def _run_git(argv, cwd, timeout=GIT_TIMEOUT_S):
    """Run a fully-formed git argv list, returning (rc, stdout, stderr) as text.

    No shell is involved: argv reaches the process exactly as given, so paths,
    labels, and commit messages containing spaces, quotes, or Unicode never
    need escaping and can never be misparsed by cmd.exe or /bin/sh. Output is
    decoded as UTF-8 with replacement so a byte outside the host codepage
    never silently reads as empty output.
    """
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "git command timed out after {}s: {}".format(timeout, argv)
    except OSError as exc:
        return 127, "", str(exc)
    out = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace")
    return proc.returncode, out, err


def _git(gitdir, worktree, args, timeout=GIT_TIMEOUT_S):
    """Run git against the shadow store, always with an explicit --git-dir and
    --work-tree, so the workspace's own repository (if it has one) can never
    be discovered or touched, no matter what cwd the child inherits."""
    exe = _find_git()
    argv = [exe, "--git-dir={}".format(gitdir), "--work-tree={}".format(worktree)] + list(args)
    return _run_git(argv, cwd=worktree, timeout=timeout)


def _store_key(ws_real):
    """A stable, filesystem-safe key for a workspace's shadow store.

    normcase folds case on Windows (NTFS is case-insensitive), so two
    spellings of the same path share one store rather than silently forking
    into two.
    """
    normalized = os.path.normcase(ws_real)
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:16]


def _store_root(ws_real):
    return os.path.join(hearth_paths.checkpoints_dir(), _store_key(ws_real))


def _gitdir_for(ws_real):
    return os.path.join(_store_root(ws_real), "gitdir")


def init_store(workspace):
    """Ensure a shadow git store exists for `workspace`, returning its GIT_DIR.

    Idempotent and cheap to call before every checkpoint(): the expensive
    setup (git init --bare, config, info/attributes, info/exclude) only runs
    once per workspace. The store lives under hearth_paths.checkpoints_dir(),
    keyed by a hash of the workspace's real path, and is never created inside
    the workspace itself.
    """
    ws = os.path.realpath(workspace)
    store = _store_root(ws)
    gitdir = _gitdir_for(ws)
    os.makedirs(store, exist_ok=True)
    _find_git()  # raise early and clearly if git is missing

    if os.path.isdir(os.path.join(gitdir, "objects")):
        # Already initialised. Re-assert core.worktree in case the workspace
        # moved on disk since the store was created; cheap, and self-healing.
        _git(gitdir, ws, ["config", "core.worktree", ws])
        return gitdir

    exe = _find_git()
    rc, out, err = _run_git([exe, "init", "--bare", "--quiet", gitdir], cwd=store)
    if rc != 0:
        raise RuntimeError(
            "failed to initialise checkpoint store at {}: {}".format(gitdir, err.strip())
        )

    for key, value in (
        ("core.worktree", ws),
        ("core.longpaths", "true"),
        ("core.autocrlf", "false"),
        ("user.name", _HEARTH_COMMITTER_NAME),
        ("user.email", _HEARTH_COMMITTER_EMAIL),
        ("gc.auto", "0"),
    ):
        rc, out, err = _git(gitdir, ws, ["config", key, value])
        if rc != 0:
            raise RuntimeError(
                "failed to configure checkpoint store ({}={}): {}".format(key, value, err.strip())
            )

    info_dir = os.path.join(gitdir, "info")
    os.makedirs(info_dir, exist_ok=True)
    with open(os.path.join(info_dir, "attributes"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("* -text -filter -ident\n")
    with open(os.path.join(info_dir, "exclude"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_BUILTIN_EXCLUDES)
    with open(os.path.join(store, "workspace.txt"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(ws + "\n")

    return gitdir


def sub_repos(workspace):
    """Nested git repositories found under `workspace`, as relative,
    forward-slash paths. Does not include the workspace root's own .git, if
    it has one: that is the ordinary "workspace is a git repo" case, handled
    separately, not a surprise to report.

    A bounded, prune-aware scan: hearth_contain.SKIP_DIRS (node_modules,
    .venv, __pycache__, and the rest) and every reparse point are never
    descended into, and the walk gives up after MAX_SCAN_DIRS directories
    rather than running unbounded on a pathological tree.

    Every path returned here must be excluded from add -A (see _add_all):
    left alone, git captures it as a gitlink holding only a commit sha,
    checkpointing and restoring none of its actual content. See the module
    docstring's "known correctness holes" section.
    """
    ws = os.path.realpath(workspace)
    walk_root = hearth_paths.long_path(ws)
    found = []
    visited = 0
    for dirpath, dirs, files in os.walk(walk_root):
        visited += 1
        if visited > MAX_SCAN_DIRS:
            break
        if dirpath != walk_root and (".git" in dirs or ".git" in files):
            rel = os.path.relpath(dirpath, walk_root).replace(os.sep, "/")
            found.append(rel)
            dirs[:] = []  # it is its own repository; do not look inside it
            continue
        hearth_contain.prune(dirpath, dirs, hearth_contain.SKIP_DIRS)
    return found


def _add_all(gitdir, ws, timeout=GIT_TIMEOUT_S):
    """Stage the whole worktree into the shadow index, excluding the
    workspace's own top-level .git (if the workspace is itself a git repo)
    and every nested sub-repository found by sub_repos().

    Returns (rc, stdout, stderr, sub_repo_list).

    The exclusion uses pathspec ':(exclude)path' rather than relying on
    info/exclude's '.git/' pattern alone: a directory that is itself a git
    repository is captured as a gitlink even when an ignore pattern names it,
    because git's repository-boundary detection runs ahead of the ordinary
    ignore check. Pathspec exclusion is a hard filter applied to what add
    even considers, and reliably avoids that path.
    """
    subs = sub_repos(ws)
    exclude_paths = list(subs)
    if os.path.exists(os.path.join(ws, ".git")):
        exclude_paths.append(".git")
    pathspecs = [":(exclude){}".format(p) for p in exclude_paths]
    rc, out, err = _git(gitdir, ws, ["add", "-A", "--", "."] + pathspecs, timeout=timeout)
    return rc, out, err, subs


def checkpoint(workspace, label=None, timestamp=None):
    """Take a whole-tree snapshot of `workspace` into its shadow git store.

    This is a plain `git add -A` plus a commit, not a diff of files some edit
    tool touched: a shell command that edits, deletes, or creates files is
    captured exactly the same way a dedicated edit-tool call would be. That
    is a genuine advantage over undo systems that only track their own edit
    calls; see the module docstring.

    `timestamp` is supplied by the caller (for example the desktop app's own
    per-turn clock) so this module never has to guess what "now" means for
    the caller's own bookkeeping; if omitted, time.time() is used.

    Returns a dict with at least: id (the shadow commit sha), label,
    timestamp, file_count, elapsed_seconds, sub_repos (nested repos found and
    excluded this run, see sub_repos()), and workspace. A "warning" key is
    present when the tree is large or slow enough that the caller should
    surface it to the user rather than let the call appear to hang.
    """
    ws = os.path.realpath(workspace)
    gitdir = init_store(ws)
    t0 = time.perf_counter()

    rc, out, err, subs = _add_all(gitdir, ws)
    if rc != 0:
        raise RuntimeError("checkpoint failed while staging {}: {}".format(ws, err.strip()))

    ts = timestamp if timestamp is not None else time.time()
    clean_label = (label or "checkpoint").replace("\r", " ").replace("\n", " ").strip() or "checkpoint"
    message = "{}\n\nHearth-Timestamp: {}\nHearth-Label: {}\n".format(clean_label, ts, clean_label)
    rc, out, err = _git(gitdir, ws, ["commit", "--allow-empty", "--quiet", "-m", message])
    if rc != 0:
        raise RuntimeError("checkpoint failed while committing {}: {}".format(ws, err.strip()))

    rc, out, err = _git(gitdir, ws, ["rev-parse", "HEAD"])
    if rc != 0:
        raise RuntimeError("checkpoint committed but HEAD could not be read: {}".format(err.strip()))
    sha = out.strip()

    rc, out, err = _git(gitdir, ws, ["ls-files"])
    file_count = len([line for line in out.splitlines() if line.strip()]) if rc == 0 else -1

    elapsed = time.perf_counter() - t0
    result = {
        "id": sha,
        "label": clean_label,
        "timestamp": ts,
        "file_count": file_count,
        "elapsed_seconds": elapsed,
        "sub_repos": subs,
        "workspace": ws,
    }
    if file_count > LARGE_TREE_WARNING_FILES or elapsed > LARGE_TREE_WARNING_SECONDS:
        result["warning"] = (
            "large or slow checkpoint: {} files in {:.2f}s; consider excluding more paths "
            "via the shadow store's info/exclude".format(file_count, elapsed)
        )
    return result


def _extract_trailer(body, key):
    m = re.search(r"^{}:\s*(.*)$".format(re.escape(key)), body, re.MULTILINE)
    return m.group(1).strip() if m else None


def list_checkpoints(workspace):
    """Every checkpoint recorded for `workspace`, newest first.

    Returns [] if the workspace has no shadow store yet, meaning checkpoint()
    has never been called for it. Never raises for that reason: an empty
    history is a normal, expected state, not an error.
    """
    ws = os.path.realpath(workspace)
    gitdir = _gitdir_for(ws)
    if not os.path.isdir(os.path.join(gitdir, "objects")):
        return []
    rc, out, err = _git(gitdir, ws, ["log", "--format=%H%x1f%ct%x1f%B%x1e"])
    if rc != 0:
        return []
    checkpoints = []
    for rec in out.split("\x1e"):
        rec = rec.strip("\n").strip()
        if not rec:
            continue
        parts = rec.split("\x1f")
        if len(parts) < 3:
            continue
        sha, commit_time, body = parts[0], parts[1], parts[2]
        ts_str = _extract_trailer(body, "Hearth-Timestamp")
        label = _extract_trailer(body, "Hearth-Label")
        if label is None:
            body_lines = body.splitlines()
            label = body_lines[0] if body_lines else ""
        checkpoints.append({
            "id": sha,
            "commit_time": int(commit_time) if commit_time.strip().lstrip("-").isdigit() else None,
            "timestamp": float(ts_str) if ts_str else None,
            "label": label,
        })
    return checkpoints


def _parse_raw_diff(raw):
    """Parse `git diff --raw --no-renames` output into (restored, skipped).

    restored is a list of {"path": ..., "status": ...} for ordinary entries.
    skipped is a list of paths where either side of the diff has mode 160000
    (a gitlink): those are filtered out of any diff this module reports, per
    the module docstring's nested-repository note.
    """
    restored = []
    skipped = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith(":"):
            continue
        meta, sep, rest = line[1:].partition("\t")
        if not sep:
            continue
        fields = meta.split()
        if len(fields) < 5:
            continue
        old_mode, new_mode, _old_sha, _new_sha, status = fields[0], fields[1], fields[2], fields[3], fields[4]
        path = rest.split("\t")[0]
        if old_mode == "160000" or new_mode == "160000":
            skipped.append(path)
            continue
        restored.append({"path": path, "status": status})
    return restored, skipped


def restore(workspace, checkpoint_id):
    """Reset `workspace`'s tracked content to the snapshot at `checkpoint_id`.

    Before resetting, the current full state of the workspace, including
    anything created since the last checkpoint and never captured, is staged
    into the shadow index with the same exclusions as checkpoint(). That is
    what makes undo cover an entire turn's worth of shell-command fallout,
    not only what a prior checkpoint() call happened to see. That staged
    state is never committed; it exists only long enough to compute the diff
    reported below, and to give `read-tree -u --reset` an index that already
    matches the worktree byte for byte, which is what lets it update files in
    place without a spurious "local changes would be overwritten" refusal.

    Returns a dict with checkpoint_id, restored (list of {"path", "status"}),
    and skipped_gitlinks (sub-repo paths this call could not touch either
    way, plus any legacy gitlink entries found in the diff itself). On
    failure, such as an unknown checkpoint id, no store for this workspace,
    or a git error, returns a dict with an "error" key instead of raising: a
    failed restore is an expected outcome the caller needs to show the user,
    not a programming error.
    """
    ws = os.path.realpath(workspace)
    gitdir = _gitdir_for(ws)
    if not os.path.isdir(os.path.join(gitdir, "objects")):
        return {"error": "no checkpoint store for this workspace", "restored": [], "skipped_gitlinks": []}

    rc, out, err = _git(gitdir, ws, ["rev-parse", "--verify", "{}^{{commit}}".format(checkpoint_id)])
    if rc != 0:
        return {"error": "unknown checkpoint: {}".format(checkpoint_id), "restored": [], "skipped_gitlinks": []}
    target = out.strip()

    rc, out, err, subs = _add_all(gitdir, ws)
    if rc != 0:
        return {
            "error": "could not capture current state before restore: {}".format(err.strip()),
            "restored": [], "skipped_gitlinks": [],
        }

    rc, before_tree, err = _git(gitdir, ws, ["write-tree"])
    if rc != 0:
        return {
            "error": "could not snapshot current state: {}".format(err.strip()),
            "restored": [], "skipped_gitlinks": [],
        }
    before_tree = before_tree.strip()

    rc, raw, err = _git(gitdir, ws, ["diff", "--raw", "--no-renames", before_tree, target])
    restored, skipped = _parse_raw_diff(raw) if rc == 0 else ([], [])

    rc, out, err = _git(gitdir, ws, ["read-tree", "-u", "--reset", target])
    if rc != 0:
        return {
            "error": "restore failed: {}".format(err.strip()),
            "checkpoint_id": target, "restored": [], "skipped_gitlinks": skipped,
        }

    # A file rewritten with byte-identical content still gets a fresh mtime,
    # which git would otherwise report as "modified" on the next status or
    # diff even though nothing actually differs. Refresh before anyone checks.
    _git(gitdir, ws, ["update-index", "--refresh"])

    return {
        "checkpoint_id": target,
        "restored": restored,
        "skipped_gitlinks": sorted(set(skipped) | set(subs)),
        "sub_repos_excluded": subs,
    }


def scope_limits():
    """Human-readable list of what checkpoint and restore do and do not
    cover. Meant to be shown to the user, for example the first time they
    open undo settings, or alongside a restore result that reports skipped
    paths."""
    return [
        "Covers changes made by shell commands as well as file-edit tools: "
        "capture is a whole-tree snapshot, not an intercept of specific edit calls.",
        "Does not cover content inside nested git repositories found under the "
        "workspace (vendored repos, submodules, worktrees); see the 'sub_repos' "
        "field on checkpoint() and the 'sub_repos_excluded' field on restore() "
        "for what was detected and excluded.",
        "Does not cover files matched by the shadow store's built-in secret "
        "excludes (.env*, *.pem, *.key, id_rsa*, *.pfx, credentials*) or heavy "
        "directories (node_modules/, .venv/, __pycache__/, target/, dist/, "
        "build/, .git/): anything excluded there is never captured and "
        "therefore never restorable.",
        "Does not detect case-only renames on a case-insensitive filesystem "
        "(the Windows and macOS default): git's diff-index cannot see a rename "
        "that only changes case.",
        "Never reads from or writes to the workspace's own .git, if it has "
        "one: its HEAD, branches, staging area, stash, and reflog are always "
        "left exactly as the user left them.",
    ]


def _self_test():
    import tempfile
    import shutil as _shutil

    if not is_git_available():
        print("hearth-checkpoint self-test SKIPPED: git not found on PATH")
        return 0

    base = os.path.realpath(tempfile.mkdtemp(prefix="hearth-ckpt-selftest-"))
    old_data_dir = os.environ.get("HEARTH_DATA_DIR")
    os.environ["HEARTH_DATA_DIR"] = os.path.join(base, "data")

    def _write(path, content):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)

    def _write_bytes(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)

    def _read_bytes(path):
        with open(path, "rb") as fh:
            return fh.read()

    def _user_git(cwd, args):
        exe = shutil.which("git")
        proc = subprocess.run([exe] + args, cwd=cwd, capture_output=True, text=True)
        return proc.returncode, proc.stdout, proc.stderr

    try:
        # -- 1. checkpoint, destructive change, restore returns exact bytes,
        #    and undoes a shell-created file too, not just an edited one. ----
        ws1 = os.path.join(base, "ws1")
        os.makedirs(ws1)
        _write(os.path.join(ws1, "a.txt"), "version one\n")
        cp1 = checkpoint(ws1, label="first")
        assert cp1["file_count"] == 1, cp1
        assert cp1["id"], cp1

        _write(os.path.join(ws1, "a.txt"), "DESTROYED\n")
        _write(os.path.join(ws1, "junk.txt"), "should not survive restore\n")

        result = restore(ws1, cp1["id"])
        assert "error" not in result, result
        assert _read_bytes(os.path.join(ws1, "a.txt")) == b"version one\n", \
            "restore did not return the exact original bytes"
        assert not os.path.exists(os.path.join(ws1, "junk.txt")), \
            "restore left behind a file created after the checkpoint"
        paths_restored = {r["path"] for r in result["restored"]}
        assert "a.txt" in paths_restored and "junk.txt" in paths_restored, result

        # -- 2. the user's own git repo is left exactly as it was -----------
        ws2 = os.path.join(base, "ws2")
        os.makedirs(ws2)
        _user_git(ws2, ["init", "--quiet"])
        _user_git(ws2, ["config", "user.name", "Test User"])
        _user_git(ws2, ["config", "user.email", "test@example.com"])
        _write(os.path.join(ws2, "tracked.txt"), "committed content\n")
        _user_git(ws2, ["add", "tracked.txt"])
        _user_git(ws2, ["commit", "--quiet", "-m", "initial"])
        _write(os.path.join(ws2, "staged.txt"), "staged content\n")
        _user_git(ws2, ["add", "staged.txt"])
        _write(os.path.join(ws2, "tracked.txt"), "modified but not staged\n")
        _user_git(ws2, ["stash", "push", "--quiet", "-m", "wip"])

        rc, head_before, _e = _user_git(ws2, ["rev-parse", "HEAD"])
        rc, branch_before, _e = _user_git(ws2, ["rev-parse", "--abbrev-ref", "HEAD"])
        rc, status_before, _e = _user_git(ws2, ["status", "--porcelain=2", "--branch"])
        rc, stash_before, _e = _user_git(ws2, ["stash", "list"])
        rc, reflog_before, _e = _user_git(ws2, ["reflog", "show", "--all"])

        cp2 = checkpoint(ws2, label="second")
        ws2_real = os.path.realpath(ws2)
        rc, shadow_files, _e = _git(_gitdir_for(ws2_real), ws2_real, ["ls-files"])
        assert not any(p == ".git" or p.startswith(".git/") for p in shadow_files.splitlines()), shadow_files

        _write(os.path.join(ws2, "tracked.txt"), "changed after checkpoint\n")
        r2 = restore(ws2, cp2["id"])
        assert "error" not in r2, r2

        rc, head_after, _e = _user_git(ws2, ["rev-parse", "HEAD"])
        rc, branch_after, _e = _user_git(ws2, ["rev-parse", "--abbrev-ref", "HEAD"])
        rc, status_after, _e = _user_git(ws2, ["status", "--porcelain=2", "--branch"])
        rc, stash_after, _e = _user_git(ws2, ["stash", "list"])
        rc, reflog_after, _e = _user_git(ws2, ["reflog", "show", "--all"])
        assert head_before == head_after, (head_before, head_after)
        assert branch_before == branch_after, (branch_before, branch_after)
        assert status_before == status_after, (status_before, status_after)
        assert stash_before == stash_after, (stash_before, stash_after)
        assert reflog_before == reflog_after, (reflog_before, reflog_after)
        assert os.path.realpath(_gitdir_for(ws2_real)) != os.path.realpath(os.path.join(ws2, ".git"))

        # -- 3. a non-git workspace works end to end -------------------------
        ws3 = os.path.join(base, "ws3")
        os.makedirs(ws3)
        _write(os.path.join(ws3, "note.txt"), "no git here\n")
        assert not os.path.isdir(os.path.join(ws3, ".git"))
        cp3 = checkpoint(ws3, label="plain")
        _write(os.path.join(ws3, "note.txt"), "overwritten\n")
        r3 = restore(ws3, cp3["id"])
        assert "error" not in r3, r3
        assert _read_bytes(os.path.join(ws3, "note.txt")) == b"no git here\n"
        assert not os.path.isdir(os.path.join(ws3, ".git")), \
            "checkpointing a plain directory must not turn it into a git repo"

        # -- 4. a nested sub-repo is detected and reported, not silently
        #    swallowed as a gitlink ------------------------------------------
        ws4 = os.path.join(base, "ws4")
        os.makedirs(os.path.join(ws4, "vendor", "lib"))
        _write(os.path.join(ws4, "top.txt"), "top level file\n")
        _user_git(os.path.join(ws4, "vendor", "lib"), ["init", "--quiet"])
        _write(os.path.join(ws4, "vendor", "lib", "inner.txt"), "nested repo content\n")

        subs4 = sub_repos(ws4)
        assert subs4 == ["vendor/lib"], subs4

        cp4 = checkpoint(ws4, label="with nested repo")
        assert cp4["sub_repos"] == ["vendor/lib"], cp4
        ws4_real = os.path.realpath(ws4)
        rc, shadow_ls, _e = _git(_gitdir_for(ws4_real), ws4_real, ["ls-files"])
        assert "top.txt" in shadow_ls
        assert "vendor/lib/inner.txt" not in shadow_ls, "nested repo content leaked into the shadow store"
        rc, shadow_tree, _e = _git(_gitdir_for(ws4_real), ws4_real, ["ls-tree", "-r", "HEAD"])
        assert "160000" not in shadow_tree, "a gitlink entry leaked into the shadow tree: {}".format(shadow_tree)

        # -- 5. an excluded secret is genuinely not captured -----------------
        ws5 = os.path.join(base, "ws5")
        os.makedirs(ws5)
        _write(os.path.join(ws5, ".env"), "SECRET_KEY=do-not-checkpoint-me\n")
        _write(os.path.join(ws5, "app.py"), "print('hello')\n")
        checkpoint(ws5, label="with secret")
        ws5_real = os.path.realpath(ws5)
        rc, shadow_ls5, _e = _git(_gitdir_for(ws5_real), ws5_real, ["ls-files"])
        names5 = shadow_ls5.splitlines()
        assert ".env" not in names5, names5
        assert "app.py" in names5, names5

        # -- 6. CRLF content survives a round trip byte for byte -------------
        ws6 = os.path.join(base, "ws6")
        os.makedirs(ws6)
        crlf_bytes = b"line one\r\nline two\r\nline three\r\n"
        _write_bytes(os.path.join(ws6, "crlf.txt"), crlf_bytes)
        cp6 = checkpoint(ws6, label="crlf")
        _write_bytes(os.path.join(ws6, "crlf.txt"), b"replaced\n")
        r6 = restore(ws6, cp6["id"])
        assert "error" not in r6, r6
        assert _read_bytes(os.path.join(ws6, "crlf.txt")) == crlf_bytes, \
            "CRLF content was not restored byte for byte"

        # -- list_checkpoints and scope_limits sanity -------------------------
        listed = list_checkpoints(ws1)
        assert len(listed) == 1 and listed[0]["id"] == cp1["id"], listed
        assert listed[0]["label"] == "first", listed
        assert listed[0]["timestamp"] is not None, listed

        limits = scope_limits()
        assert isinstance(limits, list) and limits
        assert all(isinstance(s, str) for s in limits)

        # restoring an unknown checkpoint id reports an error rather than
        # raising, and touches nothing.
        bad = restore(ws1, "0" * 40)
        assert "error" in bad, bad

        # a workspace with no checkpoint ever taken reports an empty history
        # and a clean "no store" error from restore, never an exception.
        ws7 = os.path.join(base, "ws7")
        os.makedirs(ws7)
        assert list_checkpoints(ws7) == []
        never = restore(ws7, "deadbeef")
        assert "error" in never, never
    finally:
        if old_data_dir is None:
            os.environ.pop("HEARTH_DATA_DIR", None)
        else:
            os.environ["HEARTH_DATA_DIR"] = old_data_dir
        _shutil.rmtree(base, ignore_errors=True)

    print("hearth-checkpoint self-test OK")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
