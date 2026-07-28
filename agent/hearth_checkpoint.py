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
  ":(exclude,literal)path" pathspec, which is a hard "do not even look here"
  filter, not a mere ignore pattern. Nested repos are reported, never
  silently swallowed; see the "sub_repos" field on checkpoint()'s return
  value.

  Secrets. In a workspace with no .gitignore, a plain `git add -A` would pull
  .env straight into Hearth's own data directory. The shadow store ships a
  built-in $GIT_DIR/info/exclude covering common secret file patterns and the
  usual heavy directories. Anything excluded there is therefore NOT
  restorable: that tradeoff is deliberate and is spelled out in
  scope_limits().

  Excluded-file change detection. The exclusion above has a sharp edge: an
  agent turn can freely overwrite or delete a file like .env, and a plain
  restore() silently cannot put it back, with nothing in the result to say
  so. checkpoint() now also records a lightweight manifest -- relative path,
  size, and mtime, nothing more -- of every file that currently exists and
  matches the shadow store's SECRET file patterns specifically (.env*,
  *.pem, *.key, id_rsa*, *.pfx, credentials*), written to a small sidecar
  JSON file next to the shadow store, keyed by that checkpoint's commit sha.
  This deliberately does NOT cover the heavy-directory excludes
  (node_modules/, .venv/, __pycache__/, target/, dist/, build/, .git/):
  those are large, ordinarily reproducible dependency/build trees rather
  than irreplaceable user data, and stat-ing their contents on every
  checkpoint (this runs before every agent turn) would trade a real,
  recurring performance cost for very little safety benefit. restore()
  reads the target checkpoint's manifest, if any, and compares it against
  the same secret-pattern scan run again after the restore completes:
  anything whose size/mtime changed, that vanished, or that is newly
  present is reported in the "excluded_changed" field, as
  {"path", "status"} entries with status one of "modified", "deleted",
  "created" -- machine-readable, because a UI has to render it, not prose
  buried in a warning string. A checkpoint taken before this feature
  existed has no sidecar manifest; restore() reports an empty list for it
  rather than guessing, because "we do not know what existed before" and
  "nothing existed before" are not the same claim. Size+mtime is a
  heuristic, not a content hash: it is the same class of lightweight signal
  update-index --refresh already leans on elsewhere in this module, chosen
  because a checkpoint runs before every agent turn and a hash of
  potentially large secret files (a .pfx bundle, for instance) is not a
  cost this module should pay on that hot path. See the module docstring's
  "Performance" note above and scope_limits() for how this is disclosed.

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

  A partial read-tree failure. `read-tree -u --reset` is the only call in
  this module that writes to the worktree. If it fails partway through (a
  file locked by an antivirus scanner or an open editor on Windows, a full
  disk, or GIT_TIMEOUT_S killing the child mid-checkout), some paths may
  already have been overwritten while others were not: the tree can end up
  matching neither the checkpoint nor its pre-restore state, and git gives
  this module no way to ask which paths it reached before it died. restore()
  never reports this as "restored": [] the way a clean no-op restore would,
  because that reads as "nothing was touched" and may be false. Instead it
  returns "worktree_state": "unknown" plus "attempted", the list of paths the
  diff said would change, so a caller can tell "nothing happened" apart from
  "something happened and we cannot say exactly what."

  Concurrency. Git's own index and ref lockfiles protect the shadow object
  database, but nothing in git covers the worktree writes read-tree -u makes.
  Two concurrent restores, or a checkpoint hashing files while a restore
  rewrites them underneath it, is a real race. This module closes that gap
  itself with a simple lockfile (see _StoreLock) in the store's own
  directory, held for the whole staging+commit or staging+diff+read-tree
  critical section of checkpoint() and restore() against the same workspace,
  rather than leaning on git's unrelated locking or leaving the race
  undocumented.

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

import fnmatch
import hashlib
import json
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

MAX_EXCLUDED_MANIFEST_FILES = 100  # bound on how many secret-pattern files
                                    # checkpoint() will record into the
                                    # excluded-file manifest. Real secret
                                    # counts (.env, a couple of key files,
                                    # credentials.json) are almost always
                                    # under 10; this exists so a pathological
                                    # tree cannot slow checkpoint() or bloat
                                    # the sidecar manifest file without
                                    # bound. Excess is reported truncated,
                                    # never silently dropped.
MAX_EXCLUDED_MANIFEST_BYTES = 8192  # a second, independent cap on the
                                     # manifest's serialised size, so a
                                     # handful of unusually long paths
                                     # cannot do what a large file count
                                     # would.

_LOCK_TIMEOUT_S = 60.0   # how long a caller will wait for another
                          # checkpoint/restore on the same workspace before
                          # giving up, rather than blocking forever.
_LOCK_POLL_S = 0.05
_LOCK_STALE_SECONDS = 600  # a lock file older than this is assumed to be
                            # left behind by a crashed process (nothing
                            # legitimate holds it this long, given
                            # GIT_TIMEOUT_S=180 on any single git call) and is
                            # removed rather than blocking forever.

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


def _validate_workspace(workspace):
    """Resolve and validate `workspace`, raising ValueError for anything that
    is not a genuine, non-empty path.

    os.path.realpath("") resolves to the process's own current working
    directory, not an error. Left unguarded, a caller bug that passes ""
    (a default that was never filled in, a mis-joined path) would silently
    point the shadow store at Hearth's own working directory, and a later
    restore()'s read-tree -u --reset would rewrite files there instead of
    failing loudly right away. Every public entry point below calls this
    before doing anything else.
    """
    if not isinstance(workspace, str) or not workspace.strip():
        raise ValueError("workspace must be a non-empty path, got {!r}".format(workspace))
    return os.path.realpath(workspace)


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


class _StoreLock:
    """A mutex over one workspace's shadow store.

    Git's own index and ref lockfiles protect the shadow object database,
    but read-tree -u's worktree writes are not covered by anything git does.
    Two concurrent restores, or a checkpoint hashing files while a restore
    rewrites them, is a real race; this closes it with a plain lockfile
    rather than leaving it undocumented (see the module docstring's
    "Concurrency" note).

    Implemented with os.open(..., O_CREAT | O_EXCL), which is atomic on both
    Windows and POSIX, so this needs no non-stdlib dependency and no
    platform-specific fcntl/msvcrt branch. A lock older than
    _LOCK_STALE_SECONDS is treated as abandoned by a crashed process and is
    removed rather than blocking a legitimate caller forever.
    """

    def __init__(self, store_root, timeout=_LOCK_TIMEOUT_S):
        self.store_root = store_root
        self.path = os.path.join(store_root, ".lock")
        self.timeout = timeout
        self._fd = None

    def __enter__(self):
        os.makedirs(self.store_root, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode("ascii"))
                return self
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(self.path)
                    if age > _LOCK_STALE_SECONDS:
                        os.remove(self.path)
                        continue
                except OSError:
                    continue  # lock vanished between the check and now; retry
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "checkpoint store is locked by another operation "
                        "(timed out after {}s waiting for {})".format(self.timeout, self.path)
                    )
                time.sleep(_LOCK_POLL_S)

    def __exit__(self, exc_type, exc, tb):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
            try:
                os.remove(self.path)
            except OSError:
                pass
        return False


def init_store(workspace):
    """Ensure a shadow git store exists for `workspace`, returning its GIT_DIR.

    Idempotent and cheap to call before every checkpoint(): the expensive
    setup (git init --bare, config, info/attributes, info/exclude) only runs
    once per workspace. The store lives under hearth_paths.checkpoints_dir(),
    keyed by a hash of the workspace's real path, and is never created inside
    the workspace itself.
    """
    ws = _validate_workspace(workspace)
    store = _store_root(ws)
    gitdir = _gitdir_for(ws)
    os.makedirs(store, exist_ok=True)
    _find_git()  # raise early and clearly if git is missing

    with _StoreLock(store):
        if os.path.isdir(os.path.join(gitdir, "objects")):
            # Already initialised. Re-assert core.worktree in case the
            # workspace moved on disk since the store was created; cheap,
            # and self-healing.
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

    Returns (found, truncated). truncated is True when MAX_SCAN_DIRS was hit
    before the walk finished, meaning some part of the tree was never
    examined and a nested repo past that point could exist unreported: a
    caller must surface that, not let it pass as "nothing else was found."
    checkpoint() does this via its "sub_repos_truncated" and "warning"
    fields.
    """
    ws = _validate_workspace(workspace)
    walk_root = hearth_paths.long_path(ws)
    found = []
    visited = 0
    truncated = False
    for dirpath, dirs, files in os.walk(walk_root):
        visited += 1
        if visited > MAX_SCAN_DIRS:
            truncated = True
            break
        if dirpath != walk_root and (".git" in dirs or ".git" in files):
            rel = os.path.relpath(dirpath, walk_root).replace(os.sep, "/")
            found.append(rel)
            dirs[:] = []  # it is its own repository; do not look inside it
            continue
        hearth_contain.prune(dirpath, dirs, hearth_contain.SKIP_DIRS)
    return found, truncated


def _add_all(gitdir, ws, timeout=GIT_TIMEOUT_S):
    """Stage the whole worktree into the shadow index, excluding the
    workspace's own top-level .git (if the workspace is itself a git repo)
    and every nested sub-repository found by sub_repos().

    Returns (rc, stdout, stderr, sub_repo_list, sub_repos_truncated).

    The exclusion uses pathspec ':(exclude,literal)path' rather than relying
    on info/exclude's '.git/' pattern alone: a directory that is itself a
    git repository is captured as a gitlink even when an ignore pattern
    names it, because git's repository-boundary detection runs ahead of the
    ordinary ignore check. Pathspec exclusion is a hard filter applied to
    what add even considers, and reliably avoids that path. The 'literal'
    magic is required, not cosmetic: without it, a plain ':(exclude)path'
    lets git interpret *, ?, and [...] in the excluded path as pathspec
    globs instead of literal characters, so a vendored directory whose name
    happens to contain one of those would not actually be excluded.

    The workspace's own top-level .git existence check goes through
    hearth_paths.long_path(), same as sub_repos()'s walk: on a deep
    workspace path with Windows long-path support disabled, an unwrapped
    os.path.exists() can return False for a .git that really exists, which
    would let the user's own repository be captured as an unreported
    gitlink instead of excluded.
    """
    subs, subs_truncated = sub_repos(ws)
    exclude_paths = list(subs)
    if os.path.exists(hearth_paths.long_path(os.path.join(ws, ".git"))):
        exclude_paths.append(".git")
    pathspecs = [":(exclude,literal){}".format(p) for p in exclude_paths]
    rc, out, err = _git(gitdir, ws, ["add", "-A", "--", "."] + pathspecs, timeout=timeout)
    return rc, out, err, subs, subs_truncated


def _excluded_manifest_dir(store_root):
    return os.path.join(store_root, "excluded_manifest")


def _excluded_manifest_path(store_root, sha):
    return os.path.join(_excluded_manifest_dir(store_root), "{}.json".format(sha))


def _secret_file_patterns():
    """The subset of _BUILTIN_EXCLUDES that names individual files (no
    trailing '/'), i.e. the secrets patterns, not the heavy directories.
    Derived from _BUILTIN_EXCLUDES itself, not hand-copied, so the two
    cannot silently drift apart if a pattern is ever added or removed."""
    patterns = []
    for line in _BUILTIN_EXCLUDES.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.endswith("/"):
            continue
        patterns.append(line)
    return patterns


def _scan_excluded_secrets(gitdir, ws):
    """Every currently-existing file under `ws` that matches the shadow
    store's secret file patterns, as a list of [relative-posix-path, size,
    mtime_ns], capped at MAX_EXCLUDED_MANIFEST_FILES entries and
    MAX_EXCLUDED_MANIFEST_BYTES serialised bytes. Returns (manifest,
    truncated).

    Uses `git ls-files --others --ignored --exclude-standard --directory`
    rather than a Python os.walk: with --directory, a directory that is
    wholly ignored (node_modules/, .venv/, target/, dist/, build/) is
    reported as a single entry instead of being recursed into, so this call
    does not pay to stat every file inside a dependency tree. What is left
    is the same class of single, fast, git-side tree walk `_add_all`'s own
    `git add -A` already performs every checkpoint, not a second slow
    Python walk stacked on top of it. A failure here (git error, timeout)
    is treated as truncated with an empty manifest rather than raised: a
    checkpoint or restore must not fail outright because this best-effort
    detection could not run.
    """
    rc, out, err = _git(gitdir, ws, [
        "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z",
    ])
    if rc != 0:
        return [], True
    patterns = _secret_file_patterns()
    manifest = []
    truncated = False
    for rel in out.split("\x00"):
        if not rel or rel.endswith("/"):
            continue  # blank split artefact, or a collapsed wholly-ignored directory
        name = rel.rsplit("/", 1)[-1]
        if not any(fnmatch.fnmatch(name, pat) for pat in patterns):
            continue
        if len(manifest) >= MAX_EXCLUDED_MANIFEST_FILES:
            truncated = True
            break
        full = hearth_paths.long_path(os.path.join(ws, rel.replace("/", os.sep)))
        try:
            st = os.stat(full)
        except OSError:
            continue  # vanished between listing and stat; nothing to record
        manifest.append([rel, st.st_size, st.st_mtime_ns])
    manifest.sort(key=lambda e: e[0])
    encoded = json.dumps(manifest, separators=(",", ":"))
    while len(encoded.encode("utf-8")) > MAX_EXCLUDED_MANIFEST_BYTES and manifest:
        manifest.pop()
        truncated = True
        encoded = json.dumps(manifest, separators=(",", ":"))
    return manifest, truncated


def _write_excluded_manifest(store_root, sha, manifest, truncated):
    manifest_dir = _excluded_manifest_dir(store_root)
    os.makedirs(manifest_dir, exist_ok=True)
    payload = {"files": manifest, "truncated": truncated}
    path = _excluded_manifest_path(store_root, sha)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    os.replace(tmp, path)


def _read_excluded_manifest(store_root, sha):
    """The manifest recorded for checkpoint `sha`, or None if it was never
    written (either the checkpoint predates this feature, or the manifest
    write itself failed). None is distinct from an empty list: an empty
    list means "checkpointed, zero secret files existed then"; None means
    "we genuinely do not know what existed then", and restore() must not
    guess by treating it as zero."""
    path = _excluded_manifest_path(store_root, sha)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        return None
    return files


def _diff_excluded_manifest(before, after):
    """Compare two excluded-secret-file manifests (each a list of
    [path, size, mtime_ns]), returning {"path", "status"} entries for every
    path that differs: "deleted" (was recorded, no longer exists),
    "modified" (size or mtime_ns differs), or "created" (exists now, was
    not recorded before). Size+mtime is a heuristic, not a content hash;
    see the module docstring's "Excluded-file change detection" note for
    why that tradeoff was made."""
    before_map = {e[0]: (e[1], e[2]) for e in before}
    after_map = {e[0]: (e[1], e[2]) for e in after}
    changed = []
    for path, stat_before in before_map.items():
        if path not in after_map:
            changed.append({"path": path, "status": "deleted"})
        elif after_map[path] != stat_before:
            changed.append({"path": path, "status": "modified"})
    for path in after_map:
        if path not in before_map:
            changed.append({"path": path, "status": "created"})
    return sorted(changed, key=lambda e: e["path"])


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
    excluded this run, see sub_repos()), sub_repos_truncated (True if
    MAX_SCAN_DIRS was hit and sub_repos may be incomplete),
    excluded_secrets_tracked (how many secret-pattern files this checkpoint
    recorded a size/mtime manifest for, so a later restore() can detect
    changes to them; see the module docstring's "Excluded-file change
    detection" note), excluded_manifest_truncated (True if
    MAX_EXCLUDED_MANIFEST_FILES or MAX_EXCLUDED_MANIFEST_BYTES was hit), and
    workspace. A "warning" key is present when the tree is large or slow
    enough, the sub-repo scan was truncated, or the excluded-secrets
    manifest was truncated, that the caller should surface it to the user
    rather than let any of those conditions pass unnoticed.
    """
    ws = _validate_workspace(workspace)
    gitdir = init_store(ws)
    store_root = _store_root(ws)
    t0 = time.perf_counter()

    with _StoreLock(store_root):
        rc, out, err, subs, subs_truncated = _add_all(gitdir, ws)
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

        excluded_manifest, excluded_manifest_truncated = _scan_excluded_secrets(gitdir, ws)
        try:
            _write_excluded_manifest(store_root, sha, excluded_manifest, excluded_manifest_truncated)
        except OSError:
            # Best-effort bookkeeping: a failure to write the sidecar manifest
            # must not fail an otherwise-successful checkpoint. restore()
            # already treats a missing manifest file as "unknown, not zero"
            # (see _read_excluded_manifest), so this degrades to exactly the
            # same behaviour as a pre-feature checkpoint, not a silent lie.
            excluded_manifest_truncated = True

    elapsed = time.perf_counter() - t0
    result = {
        "id": sha,
        "label": clean_label,
        "timestamp": ts,
        "file_count": file_count,
        "elapsed_seconds": elapsed,
        "sub_repos": subs,
        "sub_repos_truncated": subs_truncated,
        "excluded_secrets_tracked": len(excluded_manifest),
        "excluded_manifest_truncated": excluded_manifest_truncated,
        "workspace": ws,
    }
    warnings = []
    if file_count > LARGE_TREE_WARNING_FILES or elapsed > LARGE_TREE_WARNING_SECONDS:
        warnings.append(
            "large or slow checkpoint: {} files in {:.2f}s; consider excluding more paths "
            "via the shadow store's info/exclude".format(file_count, elapsed)
        )
    if subs_truncated:
        warnings.append(
            "sub-repo scan stopped after {} directories; some nested git repositories "
            "past that point may not have been detected or excluded".format(MAX_SCAN_DIRS)
        )
    if excluded_manifest_truncated:
        warnings.append(
            "excluded-secrets manifest was truncated; a later restore may not detect "
            "every excluded file that changed"
        )
    if warnings:
        result["warning"] = " ".join(warnings)
    return result


def _extract_trailer(body, key):
    m = re.search(r"^{}:\s*(.*)$".format(re.escape(key)), body, re.MULTILINE)
    return m.group(1).strip() if m else None


def list_checkpoints(workspace):
    """Every checkpoint recorded for `workspace`, newest first.

    Returns [] in exactly two "there is genuinely no history yet" cases:
    the workspace has no shadow store at all (checkpoint() has never been
    called for it), or the store exists but has no commit yet (init_store()
    ran but no checkpoint() ever completed against it, for example a prior
    checkpoint() call that failed partway through _add_all before it ever
    reached the commit step). Both are a normal, expected state, never an
    error.

    Any OTHER `git log` failure -- a corrupt object database, GIT_TIMEOUT_S
    expiring, git missing from PATH after the store was already created --
    raises RuntimeError instead of returning []. Collapsing that into an
    empty list would present a broken store to the user as "you have no
    checkpoints", in a module whose own standard (see the module docstring)
    is that nothing here is silently swallowed.
    """
    ws = _validate_workspace(workspace)
    gitdir = _gitdir_for(ws)
    if not os.path.isdir(os.path.join(gitdir, "objects")):
        return []
    rc, out, err = _git(gitdir, ws, ["log", "--format=%H%x1f%ct%x1f%B%x1e"])
    if rc != 0:
        # git's own wording for "the store exists but nothing has been
        # committed to it yet" (a bare repo with no HEAD commit) -- the one
        # non-zero exit that is still a legitimate empty history, not a
        # failure. Every other non-zero exit is a real problem and must not
        # be reported as if it were this one.
        if "does not have any commits yet" in err or "bad default revision" in err:
            return []
        raise RuntimeError(
            "could not read checkpoint history for {}: {}".format(
                ws, (err or out or "git log failed").strip()
            )
        )
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
    skipped_gitlinks (sub-repo paths this call could not touch either way,
    plus any legacy gitlink entries found in the diff itself), and
    excluded_changed: {"path", "status"} entries (status "modified",
    "deleted", or "created") for every secret-pattern excluded file that
    changed since the checkpoint and that this restore therefore could NOT
    put back, because it was never captured in the first place. This is the
    signal that makes the gap between hearth_contain's containment (the
    agent can write anywhere in the workspace) and this module's deliberate
    secrets exclusion visible at the one moment it matters: restore()
    reporting "restored": [...] on its own reads as a complete, trustworthy
    undo even when a .env the agent corrupted was silently left corrupted.
    excluded_changed is always a list, empty when there is nothing to
    report; excluded_manifest_available is False when the checkpoint being
    restored to predates this feature (no manifest was ever recorded for
    it), in which case excluded_changed is necessarily [] because there is
    no baseline to compare against, not because nothing changed -- see the
    module docstring's "Excluded-file change detection" note. On failure,
    such as an unknown checkpoint id, no store for this workspace, or a git
    error, returns a dict with an "error" key instead of raising: a failed
    restore is an expected outcome the caller needs to show the user, not a
    programming error.

    Two failure shapes need to be told apart, because both happen after the
    worktree may already have started changing:

      - The `git diff --raw` step (used only to compute the "restored" list
        this function reports, not to decide what read-tree will do) fails.
        This function refuses to proceed to the destructive read-tree step
        in that case and returns an error instead: it will not silently
        report an empty "restored" list and then go reset the tree anyway,
        because a caller cannot tell "nothing changed" apart from "reporting
        broke and the whole tree just got overwritten." Nothing is touched
        on this path.

      - The `read-tree -u --reset` step itself fails partway. This is the
        only call that writes to the worktree, and a partial failure (a
        locked file, a full disk, a timeout) can leave it matching neither
        the checkpoint nor its pre-restore state, with no reliable way to
        ask git which paths it reached. The return in that case omits
        "restored" and instead carries "worktree_state": "unknown" plus
        "attempted" (what the diff said would change), so a caller does not
        mistake this for a clean no-op.
    """
    ws = _validate_workspace(workspace)
    gitdir = _gitdir_for(ws)
    store_root = _store_root(ws)
    if not os.path.isdir(os.path.join(gitdir, "objects")):
        return {
            "error": "no checkpoint store for this workspace",
            "restored": [], "skipped_gitlinks": [], "excluded_changed": [],
        }

    with _StoreLock(store_root):
        rc, out, err = _git(gitdir, ws, ["rev-parse", "--verify", "{}^{{commit}}".format(checkpoint_id)])
        if rc != 0:
            return {
                "error": "unknown checkpoint: {}".format(checkpoint_id),
                "restored": [], "skipped_gitlinks": [], "excluded_changed": [],
            }
        target = out.strip()

        rc, out, err, subs, subs_truncated = _add_all(gitdir, ws)
        if rc != 0:
            return {
                "error": "could not capture current state before restore: {}".format(err.strip()),
                "restored": [], "skipped_gitlinks": [], "excluded_changed": [],
            }

        rc, before_tree, err = _git(gitdir, ws, ["write-tree"])
        if rc != 0:
            return {
                "error": "could not snapshot current state: {}".format(err.strip()),
                "restored": [], "skipped_gitlinks": [], "excluded_changed": [],
            }
        before_tree = before_tree.strip()

        rc, raw, err = _git(gitdir, ws, ["diff", "--raw", "--no-renames", before_tree, target])
        if rc != 0:
            # Do not downgrade "we could not compute what would change" to
            # "nothing changed" and reset anyway: refuse the destructive
            # step instead. See the docstring above.
            return {
                "error": "could not compute what would change; refusing to reset the "
                         "worktree without being able to report it: {}".format(err.strip()),
                "checkpoint_id": target, "restored": [], "skipped_gitlinks": [],
                "excluded_changed": [],
            }
        restored, skipped = _parse_raw_diff(raw)

        rc, out, err = _git(gitdir, ws, ["read-tree", "-u", "--reset", target])
        if rc != 0:
            # read-tree -u is the only worktree-mutating call. A partial
            # failure here may have already written some of `restored`'s
            # paths; "restored": [] would falsely claim nothing happened, so
            # it is omitted in favour of an explicit unknown-state signal.
            return {
                "error": "restore failed partway through updating the worktree: {}".format(err.strip()),
                "checkpoint_id": target,
                "attempted": restored,
                "worktree_state": "unknown",
                "skipped_gitlinks": skipped,
                "excluded_changed": [],
            }

        # A file rewritten with byte-identical content still gets a fresh
        # mtime, which git would otherwise report as "modified" on the next
        # status or diff even though nothing actually differs. Refresh
        # before anyone checks.
        _git(gitdir, ws, ["update-index", "--refresh"])

        # The gap this closes: read-tree -u never touches a secret-pattern
        # excluded file (it was never in the shadow index to begin with), so
        # if the agent corrupted or deleted one, "restored" above reports a
        # clean, complete-looking undo while that damage silently persists.
        # Compare the manifest recorded at checkpoint time (if any) against
        # the same secret-pattern scan run again now, after the reset.
        before_manifest = _read_excluded_manifest(store_root, target)
        excluded_manifest_available = before_manifest is not None
        if excluded_manifest_available:
            after_manifest, _after_truncated = _scan_excluded_secrets(gitdir, ws)
            excluded_changed = _diff_excluded_manifest(before_manifest, after_manifest)
        else:
            # No manifest for this checkpoint: it predates this feature (or
            # the write failed at checkpoint time). Reporting [] here means
            # "we have no baseline", never "nothing changed" -- see
            # excluded_manifest_available in the return value.
            excluded_changed = []

    return {
        "checkpoint_id": target,
        "restored": restored,
        "skipped_gitlinks": sorted(set(skipped) | set(subs)),
        "sub_repos_excluded": subs,
        "sub_repos_truncated": subs_truncated,
        "excluded_changed": excluded_changed,
        "excluded_manifest_available": excluded_manifest_available,
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
        "therefore never restorable. That exclusion is not silent, though: "
        "checkpoint() records a lightweight size/mtime manifest of the "
        "secret-pattern files (not the heavy directories, for cost reasons) "
        "that exist at checkpoint time, and restore() reports any of them "
        "that were modified, deleted, or newly created since, in the "
        "'excluded_changed' field, so a restore that could not undo real "
        "damage says so instead of looking clean. A checkpoint taken before "
        "this manifest existed has no baseline to compare against; restore() "
        "signals that with 'excluded_manifest_available': False rather than "
        "claiming nothing changed.",
        "Does not detect case-only renames on a case-insensitive filesystem "
        "(the Windows and macOS default): git's diff-index cannot see a rename "
        "that only changes case.",
        "Never reads from or writes to the workspace's own .git, if it has "
        "one: its HEAD, branches, staging area, stash, and reflog are always "
        "left exactly as the user left them.",
        "read-tree -u --reset is the only call that writes to the workspace's "
        "files, and it is the one part of a restore that cannot be made "
        "atomic: if it fails partway (a file locked by another program, a "
        "full disk, or a timeout), the workspace can end up matching neither "
        "the checkpoint nor its pre-restore state. restore() reports this "
        "case with worktree_state: 'unknown' rather than an empty restored "
        "list, since there is no general way to know exactly which paths "
        "were written before the failure.",
        "Concurrent checkpoint/restore calls against the same workspace are "
        "serialised by a lockfile in the shadow store; a call that cannot "
        "acquire it within the timeout raises rather than racing another "
        "one's worktree writes.",
    ]


def _self_test():
    import tempfile
    import shutil as _shutil

    # Declared here (module-function scope requires it before first use, not
    # just before assignment) because two tests below temporarily monkeypatch
    # _git and MAX_SCAN_DIRS to force error paths that are otherwise very
    # hard to reach deterministically (a genuine git-diff or read-tree
    # failure, or a directory-count limit without creating 20000 dirs).
    global _git, MAX_SCAN_DIRS, MAX_EXCLUDED_MANIFEST_FILES

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
        # A workspace with no excluded secret files at all still gets a
        # (trivially empty) manifest recorded, so restore reports
        # "we checked, nothing excluded changed", not "we don't know".
        assert result["excluded_manifest_available"] is True, result
        assert result["excluded_changed"] == [], result

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

        # -- 4. a nested sub-repo, WITH A REAL COMMIT IN IT (not just `git
        #    init`), is detected and reported at checkpoint time, its
        #    content is genuinely not restored, and restore() reports it in
        #    skipped_gitlinks. A repo with zero commits fails loudly on its
        #    own via git's "does not have a commit checked out" error even
        #    without the exclude logic present, which would make the
        #    assertions after it unreachable in a broken scenario and leave
        #    a silent-capture regression undetected; committing here is what
        #    makes this test exercise the real, load-bearing case. ----------
        ws4 = os.path.join(base, "ws4")
        os.makedirs(os.path.join(ws4, "vendor", "lib"))
        _write(os.path.join(ws4, "top.txt"), "top level file\n")
        _user_git(os.path.join(ws4, "vendor", "lib"), ["init", "--quiet"])
        _user_git(os.path.join(ws4, "vendor", "lib"), ["config", "user.name", "Test User"])
        _user_git(os.path.join(ws4, "vendor", "lib"), ["config", "user.email", "test@example.com"])
        _write(os.path.join(ws4, "vendor", "lib", "inner.txt"), "nested repo content\n")
        _user_git(os.path.join(ws4, "vendor", "lib"), ["add", "inner.txt"])
        rc, _o, nested_commit_err = _user_git(os.path.join(ws4, "vendor", "lib"), ["commit", "--quiet", "-m", "nested initial"])
        assert rc == 0, "fixture setup: nested repo commit failed: {}".format(nested_commit_err)

        subs4, subs4_truncated = sub_repos(ws4)
        assert subs4 == ["vendor/lib"], subs4
        assert subs4_truncated is False, subs4_truncated

        cp4 = checkpoint(ws4, label="with nested repo")
        assert cp4["sub_repos"] == ["vendor/lib"], cp4
        assert cp4["sub_repos_truncated"] is False, cp4
        ws4_real = os.path.realpath(ws4)
        rc, shadow_ls, _e = _git(_gitdir_for(ws4_real), ws4_real, ["ls-files"])
        assert "top.txt" in shadow_ls
        assert "vendor/lib/inner.txt" not in shadow_ls, "nested repo content leaked into the shadow store"
        rc, shadow_tree, _e = _git(_gitdir_for(ws4_real), ws4_real, ["ls-tree", "-r", "HEAD"])
        assert "160000" not in shadow_tree, "a gitlink entry leaked into the shadow tree: {}".format(shadow_tree)

        # Now prove restore() itself does the right thing with the nested
        # repo present: top-level content restores normally, the nested
        # repo's real content is left completely alone (it was never
        # captured, so restore has nothing to put back and must not touch
        # it), and the sub-repo is reported in skipped_gitlinks so a caller
        # knows restore did not cover it.
        _write(os.path.join(ws4, "top.txt"), "top level file CHANGED\n")
        _write(os.path.join(ws4, "vendor", "lib", "inner.txt"), "nested repo content CHANGED\n")
        r4 = restore(ws4, cp4["id"])
        assert "error" not in r4, r4
        assert _read_bytes(os.path.join(ws4, "top.txt")) == b"top level file\n", \
            "top-level restore should still work with a nested repo present"
        assert _read_bytes(os.path.join(ws4, "vendor", "lib", "inner.txt")) == b"nested repo content CHANGED\n", \
            "nested repo content must not be touched by restore: it is a gitlink boundary, not ours to manage"
        assert "vendor/lib" in r4["skipped_gitlinks"], r4

        # -- 5. an excluded secret is genuinely not captured -----------------
        ws5 = os.path.join(base, "ws5")
        os.makedirs(ws5)
        _write(os.path.join(ws5, ".env"), "SECRET_KEY=do-not-checkpoint-me\n")
        _write(os.path.join(ws5, "app.py"), "print('hello')\n")
        cp5 = checkpoint(ws5, label="with secret")
        ws5_real = os.path.realpath(ws5)
        rc, shadow_ls5, _e = _git(_gitdir_for(ws5_real), ws5_real, ["ls-files"])
        names5 = shadow_ls5.splitlines()
        assert ".env" not in names5, names5
        assert "app.py" in names5, names5

        # -- 5b. THE GAP THIS ITERATION CLOSES, end to end: restore() must
        #     name the excluded file that changed while it could not restore
        #     it, and must still restore the ordinary file byte-exact. This
        #     is the scenario from the task: checkpoint a workspace with a
        #     real excluded file, damage it, restore, and assert the new
        #     field names it while ordinary files come back exact. ----------
        _write(os.path.join(ws5, ".env"), "SECRET_KEY=DAMAGED-BY-AGENT\n")
        _write(os.path.join(ws5, "app.py"), "print('DAMAGED')\n")
        r5 = restore(ws5, cp5["id"])
        assert "error" not in r5, r5
        assert _read_bytes(os.path.join(ws5, "app.py")) == b"print('hello')\n", \
            "an ordinary tracked file must still restore byte-exact even when an " \
            "excluded file changed alongside it"
        # The excluded file itself is untouched by restore (it was never
        # captured, so there is nothing to put back) -- still showing the
        # damage, proving restore genuinely left it alone rather than by
        # coincidence matching the original.
        assert _read_bytes(os.path.join(ws5, ".env")) == b"SECRET_KEY=DAMAGED-BY-AGENT\n"
        assert r5["excluded_manifest_available"] is True, r5
        excluded5 = {e["path"]: e["status"] for e in r5["excluded_changed"]}
        assert excluded5.get(".env") == "modified", (
            "restore() must name '.env' as changed but not restorable, not silently "
            "report a clean 'restored' list: {}".format(r5)
        )
        # Ordinary restored files must not leak into excluded_changed, and
        # vice versa: the two fields describe disjoint sets of paths.
        assert "app.py" not in excluded5, r5
        assert ".env" not in {r["path"] for r in r5["restored"]}, r5

        # -- 5c. a secret file DELETED since the checkpoint is reported too,
        #     not just a modified one: restore cannot know a file used to
        #     exist unless the checkpoint-time manifest said so. ------------
        ws5d = os.path.join(base, "ws5d")
        os.makedirs(ws5d)
        _write(os.path.join(ws5d, "id_rsa"), "-----BEGIN OPENSSH PRIVATE KEY-----\n")
        _write(os.path.join(ws5d, "ok.txt"), "fine\n")
        cp5d = checkpoint(ws5d, label="with key")
        os.remove(os.path.join(ws5d, "id_rsa"))
        r5d = restore(ws5d, cp5d["id"])
        assert "error" not in r5d, r5d
        excluded5d = {e["path"]: e["status"] for e in r5d["excluded_changed"]}
        assert excluded5d.get("id_rsa") == "deleted", r5d

        # -- 5e. a secret file CREATED since the checkpoint (did not exist
        #     before) is reported too. -----------------------------------------
        ws5e = os.path.join(base, "ws5e")
        os.makedirs(ws5e)
        _write(os.path.join(ws5e, "ok.txt"), "fine\n")
        cp5e = checkpoint(ws5e, label="no secrets yet")
        _write(os.path.join(ws5e, "credentials.json"), '{"token": "new"}\n')
        r5e = restore(ws5e, cp5e["id"])
        assert "error" not in r5e, r5e
        excluded5e = {e["path"]: e["status"] for e in r5e["excluded_changed"]}
        assert excluded5e.get("credentials.json") == "created", r5e
        # A brand-new secret file created after the checkpoint is real user
        # data now; restore must not delete it even though it is reported.
        assert os.path.exists(os.path.join(ws5e, "credentials.json"))

        # -- 5f. a checkpoint predating this feature (no manifest sidecar on
        #     disk) reports excluded_manifest_available: False and an empty
        #     excluded_changed, never a false "nothing changed". ------------
        ws5f = os.path.join(base, "ws5f")
        os.makedirs(ws5f)
        _write(os.path.join(ws5f, ".env"), "SECRET\n")
        cp5f = checkpoint(ws5f, label="pretend legacy")
        manifest_path5f = _excluded_manifest_path(_store_root(os.path.realpath(ws5f)), cp5f["id"])
        assert os.path.exists(manifest_path5f), manifest_path5f
        os.remove(manifest_path5f)
        _write(os.path.join(ws5f, ".env"), "SECRET-DAMAGED\n")
        r5f = restore(ws5f, cp5f["id"])
        assert "error" not in r5f, r5f
        assert r5f["excluded_manifest_available"] is False, r5f
        assert r5f["excluded_changed"] == [], \
            "no manifest means no baseline, and must report [] rather than guess"

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

        # -- 7. a glob character in an excluded nested-repo directory name is
        #    still excluded, because the exclude pathspec uses ':(exclude,
        #    literal)'. Without the 'literal' magic, git would interpret the
        #    '[' and ']' in "vend[1]" as a pathspec character class matching
        #    a single character, the pathspec would not match the actual
        #    directory, and the nested repo would leak into the shadow store
        #    as a gitlink instead of being excluded. ---------------------------
        ws9 = os.path.join(base, "ws9")
        nested9 = os.path.join(ws9, "vend[1]")
        os.makedirs(nested9)
        _write(os.path.join(ws9, "top.txt"), "top\n")
        _user_git(nested9, ["init", "--quiet"])
        _user_git(nested9, ["config", "user.name", "Test User"])
        _user_git(nested9, ["config", "user.email", "test@example.com"])
        _write(os.path.join(nested9, "inner.txt"), "nested\n")
        _user_git(nested9, ["add", "inner.txt"])
        _user_git(nested9, ["commit", "--quiet", "-m", "nested"])

        subs9, _t9 = sub_repos(ws9)
        assert subs9 == ["vend[1]"], subs9

        cp9 = checkpoint(ws9, label="glob-char nested repo")
        assert cp9["sub_repos"] == ["vend[1]"], cp9
        ws9_real = os.path.realpath(ws9)
        rc, shadow_tree9, _e = _git(_gitdir_for(ws9_real), ws9_real, ["ls-tree", "-r", "HEAD"])
        assert "160000" not in shadow_tree9, (
            "a gitlink entry leaked into the shadow tree because the exclude pathspec was "
            "interpreted as a glob instead of a literal path: {}".format(shadow_tree9))
        assert "inner.txt" not in shadow_tree9, shadow_tree9

        # The above proves the behaviour is currently correct, but on the git
        # version this test runs against, a bare (no trailing wildcard)
        # directory-exclude pathspec turns out to use a literal shortcut for
        # its "select everything under this directory" check regardless of
        # glob magic, which makes the assertions above pass even if the
        # 'literal' magic were removed: verified by hand, this scenario alone
        # does not reliably move between git versions/builds. So also assert
        # directly on the argv this module hands to git, which is the actual
        # thing the fix changed and cannot silently stop mattering under a
        # different git build: every exclude pathspec must carry ':(exclude,
        # literal)', not the glob-interpretable ':(exclude)'.
        captured_add_argv = []
        _real_git3 = _git
        def _capture_add_argv(gitdir, worktree, args, timeout=GIT_TIMEOUT_S):
            if args and args[0] == "add":
                captured_add_argv.append(list(args))
            return _real_git3(gitdir, worktree, args, timeout=timeout)
        _git = _capture_add_argv
        try:
            checkpoint(ws9, label="capture add argv")
        finally:
            _git = _real_git3
        assert captured_add_argv, "expected at least one 'add' call to be captured"
        exclude_specs = [a for a in captured_add_argv[-1] if a.startswith(":(exclude")]
        assert exclude_specs, captured_add_argv[-1]
        assert all(spec.startswith(":(exclude,literal)") for spec in exclude_specs), (
            "every exclude pathspec must use ':(exclude,literal)' magic, not the glob-"
            "interpretable ':(exclude)', or a vendored directory whose name contains "
            "*, ?, or [...] could fail to be excluded on some git version: {}".format(exclude_specs)
        )

        # -- 8. the workspace's own .git existence check goes through
        #    hearth_paths.long_path, the same wrapper sub_repos()'s walk
        #    already uses, so it cannot silently miss a real .git on a long,
        #    unwrapped path. Actual >260-character paths are not portable to
        #    construct in a self-test on every platform/CI, so this proves
        #    the wiring structurally: the check must consult long_path() for
        #    exactly the workspace's own .git path. -----------------------------
        long_path_calls = []
        _real_long_path = hearth_paths.long_path
        def _tracking_long_path(p):
            long_path_calls.append(p)
            return _real_long_path(p)
        hearth_paths.long_path = _tracking_long_path
        try:
            checkpoint(ws2, label="long-path-check")
        finally:
            hearth_paths.long_path = _real_long_path
        own_git = os.path.join(os.path.realpath(ws2), ".git")
        assert any(os.path.normcase(c) == os.path.normcase(own_git) for c in long_path_calls), (
            "_add_all's own-.git check must call hearth_paths.long_path on the workspace's "
            ".git path, same as sub_repos()'s walk, or a real .git beyond MAX_PATH on Windows "
            "with long paths disabled could be missed and captured as an unreported gitlink; "
            "calls seen: {}".format(long_path_calls))

        # -- 9. a git-diff failure during restore aborts BEFORE the
        #    destructive read-tree step, instead of silently downgrading
        #    "could not compute the report" to "nothing changed" and
        #    resetting the tree anyway. The worktree must be left completely
        #    untouched. --------------------------------------------------------
        ws8 = os.path.join(base, "ws8")
        os.makedirs(ws8)
        _write(os.path.join(ws8, "f.txt"), "original\n")
        cp8 = checkpoint(ws8, label="pre-diff-failure")
        _write(os.path.join(ws8, "f.txt"), "changed after checkpoint\n")

        _real_git = _git
        def _fail_on_diff(gitdir, worktree, args, timeout=GIT_TIMEOUT_S):
            if args and args[0] == "diff":
                return 1, "", "simulated diff failure"
            return _real_git(gitdir, worktree, args, timeout=timeout)
        _git = _fail_on_diff
        try:
            r8 = restore(ws8, cp8["id"])
        finally:
            _git = _real_git
        assert "error" in r8, r8
        assert r8.get("restored") == [], r8
        assert _read_bytes(os.path.join(ws8, "f.txt")) == b"changed after checkpoint\n", \
            "a diff failure must abort before the destructive read-tree, leaving the worktree untouched"

        # -- 10. a read-tree failure partway through a restore does not claim
        #     "restored": [] (which would read as "nothing was touched" and
        #     may be false): it reports worktree_state: "unknown" plus
        #     "attempted", the paths the diff said would change. -------------
        ws11 = os.path.join(base, "ws11")
        os.makedirs(ws11)
        _write(os.path.join(ws11, "f.txt"), "original\n")
        cp11 = checkpoint(ws11, label="pre-read-tree-failure")
        _write(os.path.join(ws11, "f.txt"), "changed after checkpoint\n")

        _real_git2 = _git
        def _fail_on_read_tree(gitdir, worktree, args, timeout=GIT_TIMEOUT_S):
            if args and args[0] == "read-tree":
                return 1, "", "simulated read-tree failure"
            return _real_git2(gitdir, worktree, args, timeout=timeout)
        _git = _fail_on_read_tree
        try:
            r11 = restore(ws11, cp11["id"])
        finally:
            _git = _real_git2
        assert "error" in r11, r11
        assert "restored" not in r11, r11
        assert r11.get("worktree_state") == "unknown", r11
        assert any(a["path"] == "f.txt" for a in r11.get("attempted", [])), r11

        # -- 11. two operations against the same shadow store cannot
        #     interleave: git's own lockfiles cover the object database and
        #     refs, not read-tree -u's worktree writes, so this module adds
        #     its own mutex. Directly exercises _StoreLock rather than going
        #     through checkpoint()/restore(), so the assertion is about the
        #     lock itself, not timing-dependent behaviour of the git calls
        #     around it. ------------------------------------------------------
        import threading
        lock_store = os.path.join(base, "lock-test-store")
        os.makedirs(lock_store, exist_ok=True)
        active = [0]
        overlap = []
        overlap_lock = threading.Lock()
        def _hold():
            with _StoreLock(lock_store, timeout=10):
                with overlap_lock:
                    active[0] += 1
                    overlap.append(active[0])
                time.sleep(0.15)
                with overlap_lock:
                    active[0] -= 1
        threads = [threading.Thread(target=_hold) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert max(overlap) == 1, "two threads held the store lock at the same time: {}".format(overlap)

        # -- 12. sub_repos()'s scan reports when MAX_SCAN_DIRS truncated it,
        #     instead of silently under-reporting a pathological tree; and
        #     checkpoint() surfaces that as sub_repos_truncated plus a
        #     warning. Uses a small MAX_SCAN_DIRS rather than actually
        #     creating 20000 directories. ---------------------------------------
        _real_max_scan_dirs = MAX_SCAN_DIRS
        MAX_SCAN_DIRS = 2
        try:
            ws10 = os.path.join(base, "ws10")
            os.makedirs(os.path.join(ws10, "d1", "d2", "d3"))
            _write(os.path.join(ws10, "d1", "d2", "d3", "f.txt"), "x\n")
            subs10, truncated10 = sub_repos(ws10)
            assert truncated10 is True, "a scan that exceeds MAX_SCAN_DIRS must report truncated=True"

            cp10 = checkpoint(ws10, label="truncated scan")
            assert cp10["sub_repos_truncated"] is True, cp10
            assert "warning" in cp10, cp10
            assert "sub-repo scan" in cp10["warning"], cp10
        finally:
            MAX_SCAN_DIRS = _real_max_scan_dirs

        # -- 12b. the excluded-secrets manifest reports truncation instead of
        #     silently under-recording a pathological number of secret-
        #     pattern files, the same discipline as the sub-repo scan above.
        #     Uses a small MAX_EXCLUDED_MANIFEST_FILES rather than actually
        #     creating 100+ files. --------------------------------------------
        _real_max_manifest_files = MAX_EXCLUDED_MANIFEST_FILES
        MAX_EXCLUDED_MANIFEST_FILES = 1
        try:
            ws10b = os.path.join(base, "ws10b")
            os.makedirs(ws10b)
            _write(os.path.join(ws10b, "a.pem"), "1\n")
            _write(os.path.join(ws10b, "b.pem"), "2\n")
            cp10b = checkpoint(ws10b, label="truncated manifest")
            assert cp10b["excluded_manifest_truncated"] is True, cp10b
            assert cp10b["excluded_secrets_tracked"] == 1, cp10b
            assert "warning" in cp10b, cp10b
            assert "manifest was truncated" in cp10b["warning"], cp10b
        finally:
            MAX_EXCLUDED_MANIFEST_FILES = _real_max_manifest_files

        # -- 13. every public entry point rejects an empty workspace path
        #     instead of letting os.path.realpath("") silently resolve to
        #     Hearth's own process cwd. ------------------------------------------
        for bad_fn, bad_args in (
            (checkpoint, ("",)),
            (restore, ("", "deadbeef")),
            (list_checkpoints, ("",)),
            (sub_repos, ("",)),
            (init_store, ("",)),
        ):
            try:
                bad_fn(*bad_args)
                assert False, "{} accepted an empty workspace path".format(bad_fn.__name__)
            except ValueError:
                pass

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

        # -- 15. list_checkpoints distinguishes "no checkpoints yet" from a
        #     real git failure. A store that was initialised but never
        #     successfully committed to (mirroring a checkpoint() call that
        #     failed partway through _add_all, before it ever reached the
        #     commit step) is STILL a legitimate empty history, not an
        #     error -- git's own "does not have any commits yet" is the one
        #     non-zero exit from `git log` that means that. Any OTHER git
        #     log failure (a corrupt store, a timeout) must raise instead of
        #     silently reporting "you have no checkpoints", which would hide
        #     real breakage from the user. --------------------------------------
        ws12 = os.path.join(base, "ws12")
        os.makedirs(ws12)
        init_store(ws12)  # store created, but nothing has ever been committed
        assert list_checkpoints(ws12) == [], \
            "an initialised-but-never-committed store is a normal empty history, not an error"

        _write(os.path.join(ws12, "f.txt"), "one\n")
        checkpoint(ws12, label="only")
        assert len(list_checkpoints(ws12)) == 1

        _real_git4 = _git

        def _fail_on_log(gitdir, worktree, args, timeout=GIT_TIMEOUT_S):
            if args and args[0] == "log":
                return 1, "", "fatal: object database corrupted (simulated)"
            return _real_git4(gitdir, worktree, args, timeout=timeout)

        _git = _fail_on_log
        try:
            try:
                list_checkpoints(ws12)
                assert False, \
                    "a genuine git log failure must raise, not silently report an empty history"
            except RuntimeError as exc:
                assert "corrupted" in str(exc), exc
        finally:
            _git = _real_git4

        # the un-mocked path still works after restoring the real _git.
        assert len(list_checkpoints(ws12)) == 1
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
