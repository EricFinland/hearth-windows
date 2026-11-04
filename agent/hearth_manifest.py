#!/usr/bin/env python3
"""hearth manifest: prove what an unattended run actually touched.

The pitch is that a hearth run is contained and audited. The honest
question after a four hour marathon is "what did it change?", and the
honest answer today is "read the log and hope". A log says what the agent
meant to do; a manifest says what the filesystem now holds. This module
takes a hash manifest of a workspace before a run and after it, and diffs
the two, so the answer is a fact instead of a narrative.

It is deliberately independent of git. The workspace handed to an agent
may not be a repository at all, the agent may have initialised one, and a
run that rewrites `.gitignore`d files (caches, env files, build output,
credentials dropped in place) is exactly the case an operator most wants
to see. `git status` is blind there by design; this is not.

What it deliberately does NOT do:

  No git.         Nothing here shells out to git or reads .git. The
                  default ignore list skips .git as noise, not as truth.
  No content.     It records sha256, size and mtime, never file bodies,
                  so a manifest is safe to keep and to ship to an
                  operator even when the workspace held secrets.
  No line diffs.  "modified" means the hash moved. Producing a readable
                  diff of the change is a separate job for a separate
                  tool.
  No blame.       It cannot say which tool call or which agent wrote a
                  file, only that the bytes differ between two moments.
  No watching.    Two snapshots, taken when the operator asks. There is
                  no daemon, no inotify, no background thread.
  No backup.      A manifest cannot restore anything. Checkpointing is
                  hearth_checkpoint's job.
  No empty dirs.  A manifest lists files. A directory that holds nothing
                  leaves no trace in it, which keeps a manifest
                  comparable across machines that scaffold differently.
  No symlink following. Walks never descend through a link, and a link
                  is recorded with a null hash rather than resolved, so a
                  link pointing out of the workspace can never pull half
                  the disk into the manifest.

The mtime versus hash distinction is the whole point of compare(). Build
tools, formatters and editors rewrite files with byte identical content
constantly. A file whose mtime moved but whose hash did not has NOT been
changed, and reporting it as changed would bury the three edits that
matter under three hundred that do not.

Paths in a manifest are relative and forward slashed on every platform,
so a snapshot taken on Windows compares cleanly against one taken on
Linux. File reads are injectable (open_fn) so unreadable files are
testable with no need to mangle real permissions. Standard library only.
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile

# Noise an operator never asked about, and in the case of .git a parallel
# source of truth this module refuses to depend on. Overridable per call:
# the point of the tool is lost if it cannot be pointed at everything.
DEFAULT_IGNORE = (".git", "__pycache__", ".venv", "venv", "env",
                  ".env.d", "node_modules", ".mypy_cache",
                  ".pytest_cache", ".ruff_cache", ".tox", ".eggs")

READ_CHUNK = 262144  # hash in chunks so a huge artifact cannot blow up RAM
SIGILS = {"added": "+", "removed": "-", "modified": "~"}


def _reason(exc):
    """A short, stable description of why a file could not be read."""
    detail = getattr(exc, "strerror", None) or str(exc) or "unreadable"
    return "{}: {}".format(type(exc).__name__, detail)


def _entry_for(path, open_fn):
    """Describe one file: sha256, size, mtime. A file that cannot be
    stat'd or read is recorded with a null hash and a reason rather than
    aborting the walk, because a permission denied file in the middle of
    a workspace is a finding, not a crash."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        return {"sha256": None, "size": None, "mtime": None,
                "reason": _reason(exc)}
    entry = {"sha256": None, "size": st.st_size,
             "mtime": round(st.st_mtime, 3)}
    if os.path.islink(path):
        # Recorded, never resolved: see the module docstring.
        entry["reason"] = "symlink"
        return entry
    digest = hashlib.sha256()
    try:
        with open_fn(path, "rb") as handle:
            while True:
                chunk = handle.read(READ_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        entry["reason"] = _reason(exc)
        return entry
    entry["sha256"] = digest.hexdigest()
    return entry


def snapshot(root, ignore=None, open_fn=None):
    """Walk `root` and return {relative_path: {sha256, size, mtime}}.

    Keys are forward slashed and relative to `root` on every platform.
    `ignore` replaces DEFAULT_IGNORE and is matched against plain file
    and directory names at any depth. A root that does not exist returns
    an empty manifest instead of raising: an operator comparing a
    workspace that a run deleted outright should get that answer as a
    diff, not as a traceback."""
    names = set(DEFAULT_IGNORE if ignore is None else ignore)
    opener = open_fn or open
    root = os.path.abspath(root)
    if not os.path.isdir(root) or os.path.islink(root):
        return {}
    out = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in names
            and not os.path.islink(os.path.join(dirpath, d)))
        for name in sorted(filenames):
            if name in names:
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out[rel] = _entry_for(full, opener)
    return out


def _identity(entry):
    """What has to move for a file to count as modified. The hash, and
    nothing else: mtime is explicitly not part of this."""
    digest = (entry or {}).get("sha256")
    if digest:
        return digest
    return "unreadable:{}".format((entry or {}).get("reason"))


def snapshot_changed(diff):
    """True when the diff holds anything an operator should look at."""
    return bool(diff.get("added") or diff.get("removed")
                or diff.get("modified"))


def compare(before, after):
    """Diff two manifests into added / removed / modified / unchanged.

    A file present in both whose hash is identical is unchanged, however
    far its mtime moved. A file that became unreadable, or stopped being
    unreadable, counts as modified, since that is a real change in what
    the workspace will hand to the next run."""
    before = before or {}
    after = after or {}
    added = sorted(k for k in after if k not in before)
    removed = sorted(k for k in before if k not in after)
    common = [k for k in after if k in before]
    modified = sorted(k for k in common
                      if _identity(after[k]) != _identity(before[k]))
    return {"added": added,
            "removed": removed,
            "modified": modified,
            "unchanged_count": len(common) - len(modified)}


def format_report(diff):
    """Render a diff as the paragraph an operator reads after a run."""
    lines = ["hearth manifest diff"]
    for key in ("added", "removed", "modified"):
        paths = diff.get(key) or []
        lines.append("  {} {}".format(len(paths), key))
        for path in paths:
            lines.append("    {} {}".format(SIGILS[key], path))
    lines.append("  {} unchanged".format(diff.get("unchanged_count", 0)))
    if snapshot_changed(diff):
        total = (len(diff.get("added") or [])
                 + len(diff.get("removed") or [])
                 + len(diff.get("modified") or []))
        lines.append("verdict: {} path(s) changed".format(total))
    else:
        lines.append("verdict: workspace unchanged")
    return "\n".join(lines)


def save(manifest, path):
    """Write a manifest as JSON, atomically. A half written manifest is
    worse than none at all, so the bytes land in a sibling temp file that
    is flushed and fsync'd before a single os.replace puts it in place."""
    path = os.path.abspath(path)
    parent = os.path.dirname(path) or "."
    if not os.path.isdir(parent):
        os.makedirs(parent)
    handle = tempfile.mkstemp(prefix=".hearth-manifest-", dir=parent)
    fd, tmp = handle
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True,
                      ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def load(path):
    """Read a manifest written by save()."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and isinstance(data.get("files"), dict):
        return data["files"]  # tolerate a wrapped manifest
    return data


def _write(path, text):
    """Self-test helper: create a file and every directory above it."""
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _self_test():
    # Non-ASCII names are assembled with chr() so this source stays pure
    # ASCII while the walk still has real non-ASCII paths to carry.
    uni_dir = "dossier caf" + chr(0xE9)
    uni_name = "notes " + chr(0xF1) + ".txt"
    uni_file = uni_dir + "/" + uni_name
    with tempfile.TemporaryDirectory(prefix="hearth-manifest-test-") as box:
        root = os.path.join(box, "work space")
        _write(os.path.join(root, "a.txt"), "alpha")
        _write(os.path.join(root, "nested", "b.txt"), "beta")
        _write(os.path.join(root, "nested", "deep", "c.txt"), "gamma")
        _write(os.path.join(root, uni_dir, uni_name), "unicode")
        os.makedirs(os.path.join(root, "hollow"))
        # noise the default ignore list must keep out of the manifest
        _write(os.path.join(root, ".git", "config"), "[core]")
        _write(os.path.join(root, "node_modules", "p", "i.js"), "x")
        _write(os.path.join(root, "__pycache__", "a.pyc"), "x")
        _write(os.path.join(root, ".venv", "pyvenv.cfg"), "x")
        _write(os.path.join(root, "nested", ".git", "HEAD"), "x")

        before = snapshot(root)
        assert sorted(before) == sorted(["a.txt", "nested/b.txt",
                                         "nested/deep/c.txt",
                                         uni_file]), sorted(before)
        # forward slashes everywhere, regardless of platform
        assert all("\\" not in k for k in before), sorted(before)
        # an empty directory leaves no trace
        assert not [k for k in before if k.startswith("hollow")], before
        for entry in before.values():
            assert len(entry["sha256"]) == 64, entry
            assert entry["size"] >= 0 and entry["mtime"] > 0, entry
        assert before["a.txt"]["size"] == 5, before["a.txt"]

        # a root that does not exist is an empty manifest, not a raise
        assert snapshot(os.path.join(box, "no-such-dir")) == {}
        # so is a directory with nothing in it
        empty_root = os.path.join(box, "empty")
        os.makedirs(empty_root)
        assert snapshot(empty_root) == {}

        # ignore is overridable, and matches at any depth
        trimmed = snapshot(root, ignore=["nested", ".git", "node_modules",
                                         "__pycache__", ".venv"])
        assert sorted(trimmed) == sorted(["a.txt", uni_file]), sorted(trimmed)

        # an unreadable file is recorded, and the walk carries on
        def deny_a(path, mode):
            if path.endswith("a.txt"):
                raise PermissionError(13, "Permission denied")
            return open(path, mode)

        guarded = snapshot(root, open_fn=deny_a)
        assert len(guarded) == 4, sorted(guarded)
        assert guarded["a.txt"]["sha256"] is None, guarded["a.txt"]
        assert guarded["a.txt"]["reason"].startswith("PermissionError"), \
            guarded["a.txt"]
        assert guarded["nested/b.txt"]["sha256"], guarded["nested/b.txt"]

        # save / load round trip, including the non-ASCII key
        out = os.path.join(box, "manifests", "before.json")
        save(before, out)
        assert load(out) == before
        assert load(out)[uni_file]["sha256"] == before[uni_file]["sha256"]
        # the atomic write leaves no temp file behind
        leftovers = [n for n in os.listdir(os.path.dirname(out))
                     if n.startswith(".hearth-manifest-")]
        assert leftovers == [], leftovers
        # and overwriting in place keeps exactly one file there
        save(before, out)
        assert os.listdir(os.path.dirname(out)) == ["before.json"]

        # nothing touched: no diff at all
        same = compare(before, snapshot(root))
        assert same == {"added": [], "removed": [], "modified": [],
                        "unchanged_count": 4}, same
        assert not snapshot_changed(same)
        assert "workspace unchanged" in format_report(same)

        # the distinction that matters: a.txt is rewritten with byte
        # identical content and a moved mtime. That is NOT a change.
        a_path = os.path.join(root, "a.txt")
        _write(a_path, "alpha")
        os.utime(a_path, (1000000000, 1000000000))
        # nested/b.txt really changes
        _write(os.path.join(root, "nested", "b.txt"), "beta and more")
        # c.txt is deleted, d.txt appears
        os.unlink(os.path.join(root, "nested", "deep", "c.txt"))
        _write(os.path.join(root, "nested", "deep", "d.txt"), "delta")

        after = snapshot(root)
        assert after["a.txt"]["mtime"] != before["a.txt"]["mtime"], "mtime"
        assert after["a.txt"]["sha256"] == before["a.txt"]["sha256"], "hash"
        diff = compare(before, after)
        assert diff["added"] == ["nested/deep/d.txt"], diff
        assert diff["removed"] == ["nested/deep/c.txt"], diff
        assert diff["modified"] == ["nested/b.txt"], diff
        # a.txt and the non-ASCII file: moved mtime, same bytes, unchanged
        assert diff["unchanged_count"] == 2, diff
        assert snapshot_changed(diff)

        report = format_report(diff)
        assert "+ nested/deep/d.txt" in report, report
        assert "- nested/deep/c.txt" in report, report
        assert "~ nested/b.txt" in report, report
        assert "2 unchanged" in report, report
        assert "3 path(s) changed" in report, report

        # a file becoming unreadable is a real change
        broke = compare(before, guarded)
        assert broke["modified"] == ["a.txt"], broke

        # empty against populated: everything is an addition
        fresh = compare({}, before)
        assert fresh["added"] == sorted(before), fresh
        assert fresh["removed"] == [] and fresh["modified"] == [], fresh
        assert fresh["unchanged_count"] == 0, fresh
        # and the reverse, for a workspace a run wiped out
        wiped = compare(before, {})
        assert wiped["removed"] == sorted(before), wiped
        assert wiped["unchanged_count"] == 0, wiped

        # compare tolerates manifests straight off disk
        after_out = os.path.join(box, "manifests", "after.json")
        save(after, after_out)
        assert compare(load(out), load(after_out)) == diff

        # deterministic: the same tree hashed twice is the same manifest
        assert snapshot(root) == after

    print("hearth-manifest self-test OK")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="hearth-manifest",
        description="hash manifest of a workspace, and the diff of two")
    p.add_argument("--snapshot", metavar="ROOT",
                   help="walk ROOT and write a manifest")
    p.add_argument("--out", metavar="FILE",
                   help="where --snapshot writes its manifest")
    p.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"),
                   help="diff two manifests; exits 1 if anything changed")
    p.add_argument("--ignore", action="append", metavar="NAME",
                   help="replace the default ignore list (repeatable)")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)

    if a.self_test:
        return _self_test()

    if a.snapshot:
        if not a.out:
            p.error("--snapshot needs --out FILE")
        manifest = snapshot(a.snapshot, ignore=a.ignore)
        path = save(manifest, a.out)
        print("{} file(s) -> {}".format(len(manifest), path))
        return 0

    if a.compare:
        diff = compare(load(a.compare[0]), load(a.compare[1]))
        print(format_report(diff))
        return 1 if snapshot_changed(diff) else 0

    p.error("nothing to do (use --self-test, --snapshot or --compare)")


if __name__ == "__main__":
    sys.exit(main())
