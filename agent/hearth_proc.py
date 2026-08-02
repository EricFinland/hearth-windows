#!/usr/bin/env python3
"""hearth contained subprocess execution.

Running a child correctly is platform work, so it lives here rather than in the
tool layer. Three things this module exists to guarantee:

  1. Output is decoded as UTF-8 with replacement, never the host ANSI codepage.
     A decode error inside subprocess's reader thread produces returncode 0 with
     stdout None, which reads to a model as "the command produced no output".
  2. A timeout kills the whole process tree. With shell=True the direct child is
     the shell; grandchildren inherit the pipe handles and keep the call blocked
     long past the deadline. A tree that still has not confirmed death after two
     kill attempts is reported through stderr rather than treated as a clean exit.
  3. A command longer than the shell's command-line limit is spilled to a script
     file rather than truncated. Windows caps a command line near 8191
     characters, and Unix-trained models emit long heredocs constantly.

Containment is a fourth thing, and it lives in hearth_sandbox rather than here,
because it is all Windows security API and this module is about running a child
correctly on any platform. What this module owns is the decision of WHICH path a
command takes: the plain subprocess path, or a contained one. `run_contained`
takes a `level` and routes accordingly, so every caller of this function gets
whatever containment is configured without knowing the mechanism.

The working directory is still not a security boundary at any level. At the
`workspace` level a mandatory integrity label is, for writes; reads and network
access remain open. hearth_sandbox's docstring is the honest account.

Standard library only.
"""

import os
import subprocess
import sys
import tempfile
import threading
import time

import hearth_paths
import hearth_sandbox


MAX_INLINE_COMMAND = 7000  # Windows caps a command line near 8191 characters.

# taskkill is given a bounded timeout so a single _kill_tree() call cannot
# silently balloon. The self-test's timeout probe runs run_contained() with a
# 3s timeout and asserts the WHOLE call returns in under 12s, so
# 3 (probe timeout) + _TASKKILL_TIMEOUT_S must stay comfortably under 12,
# leaving headroom for process teardown and scheduling jitter. If either the
# probe's numbers or this constant change, keep that inequality true, or the
# self-test will fail for a reason unrelated to what it is testing.
_TASKKILL_TIMEOUT_S = 5

# How long to wait, after each kill attempt, for the pump thread to notice
# the tree actually died. Only consulted on the timeout path; a normal run
# never reaches it, and in the normal timeout case the wait ends almost
# immediately once the kill lands, well inside this ceiling.
_KILL_GRACE_S = 10

_KEEP_ENV = (
    "PATH", "Path", "SYSTEMROOT", "SystemRoot", "COMSPEC", "ComSpec",
    "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "LC_ALL",
    "PROGRAMFILES", "ProgramFiles", "PROGRAMDATA", "APPDATA", "LOCALAPPDATA",
    "WINDIR", "PATHEXT", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    # NixOS runtime plumbing (measured against the real hearth-grow systemd
    # unit on this host): without these a child cannot find locales, timezone
    # data, or anything installed through the user's Nix profile, which is
    # most of what a shell command on this host actually needs.
    "LOCALE_ARCHIVE", "TZDIR", "NIX_PATH", "NIX_PROFILES",
    "USER", "SHELL", "TERM", "XDG_RUNTIME_DIR",
)

# Deliberately NOT in the allow list: HEARTH_DB, HEARTH_REPO,
# HEARTH_DAILY_TOKEN_CAP, and every other HEARTH_* variable. Those carry the
# audit DB path, spend caps, and other sidecar-only state that is present in
# the real unit's environment; keeping them out of an agent-issued command's
# environment is the entire point of using an allow list here instead of a
# deny list. Do not add them back.


def child_env(extra=None):
    """A minimal environment for a child process.

    The sidecar's own environment carries the audit DB path, the bearer token,
    and any credentials it holds. None of that should reach an agent-issued
    command, so build the child's environment from an allow list instead of
    inheriting wholesale.
    """
    env = {k: os.environ[k] for k in _KEEP_ENV if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if extra:
        env.update(extra)
    return env


def _shell_argv(cmd, cwd):
    """Return (argv, cleanup_path) for running `cmd`.

    A command longer than the shell's limit is written to a script file and the
    script is executed, because Windows truncates or rejects an over-long
    command line and a Unix-trained model emits long heredocs constantly.
    """
    if len(cmd) <= MAX_INLINE_COMMAND:
        if hearth_paths.is_windows():
            comspec = os.environ.get("COMSPEC") or "cmd.exe"
            # Build the full command line ourselves instead of handing Popen a
            # list. Popen's list2cmdline would quote `cmd` as a single argument
            # and backslash-escape any double quotes inside it, but cmd.exe's
            # /S /C parsing does not undo that escaping: it forwards the
            # literal backslash-quote text to the child, which corrupts any
            # command that itself contains a quoted argument (for example
            # python -c "..."). Passing a pre-built string bypasses list2cmdline
            # entirely, so the inner quotes reach cmd.exe unescaped, which is
            # what /S /C expects.
            return '"{}" /d /s /c "{}"'.format(comspec, cmd), None
        return ["/bin/sh", "-c", cmd], None

    suffix = ".cmd" if hearth_paths.is_windows() else ".sh"
    fd, path = tempfile.mkstemp(prefix=".hearth-cmd", suffix=suffix, dir=cwd)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            if hearth_paths.is_windows():
                # If cmd already contains \r\n (Windows-authored input), this
                # doubles the \r before it. cmd.exe tolerates a \r\r\n line
                # ending in a script file, so the double-up is harmless.
                fh.write("@echo off\r\n" + cmd.replace("\n", "\r\n") + "\r\n")
            else:
                fh.write("#!/bin/sh\n" + cmd + "\n")
        if not hearth_paths.is_windows():
            os.chmod(path, 0o700)
    except Exception:
        # mkstemp already created the file; do not leave it behind if
        # writing or chmod-ing it failed partway (disk full, permission
        # race). The caller never receives `path` in that case, so nothing
        # else can clean this up.
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    # The shebang and exec bit above are not consulted by the invocation
    # below (we always run the script explicitly via /bin/sh), but they are
    # kept anyway: they make the spilled file runnable on its own if someone
    # inspects the workspace and reruns it by hand.
    return ([path] if hearth_paths.is_windows() else ["/bin/sh", path]), path


def _kill_tree(proc):
    """Kill the child and everything it spawned.

    proc.kill() reaches only the direct child, which with a shell is the shell
    itself. On Windows taskkill /T walks the tree; on POSIX the process group
    does, which is why the child is started in its own group.

    A sandboxed child is killed through its Job object instead, which is
    strictly more reliable: taskkill walks parent-pid links that a grandchild
    can break, while a Job cannot be escaped. hearth_llama still passes plain
    Popen objects here and must keep working, so this dispatches on the object
    rather than on a flag.
    """
    if hasattr(proc, "kill_tree"):
        proc.kill_tree()
        return
    try:
        if hearth_paths.is_windows():
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=_TASKKILL_TIMEOUT_S,
            )
        else:
            os.killpg(os.getpgid(proc.pid), 9)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _unlink_with_retries(path, attempts=5, delay=0.2):
    """Remove `path`, tolerating a briefly-still-open handle.

    A spilled script that was just force-killed can keep its own file handle
    open for a moment after the kill signal is sent (the OS has not finished
    tearing the process down yet), so a single unlink attempt right after a
    kill can fail even though nothing is actually wrong. Retry a few times
    with a short backoff before giving up. A leftover .hearth-cmd* file in
    the user's workspace is a bug the module's own self-test checks for, so
    this is worth a bit of patience rather than a single swallowed OSError.
    """
    for attempt in range(attempts):
        try:
            os.unlink(path)
            return
        except OSError:
            if attempt == attempts - 1:
                return
            time.sleep(delay)


_sandbox_notice_shown = set()


def _prepare_level(cwd, level):
    """Resolve the sandbox level for this call and make `cwd` usable at it.

    Returns the level actually in force. Raises hearth_sandbox.SandboxError
    when a level that promises a boundary cannot deliver one, because silently
    dropping to a weaker level is exactly how a security control becomes a
    label on a settings screen.
    """
    level = hearth_sandbox.resolve_level(level)
    if level != "workspace":
        return level
    note = hearth_sandbox.prepare_workspace(cwd, level)
    if note and note not in _sandbox_notice_shown:
        _sandbox_notice_shown.add(note)
        print("[{}]".format(note), file=sys.stderr)
    return level


def run_contained(cmd, cwd, timeout=120, env=None, level=None):
    """Run `cmd` in `cwd`, returning (returncode, stdout, stderr, timed_out).

    Output is decoded as UTF-8 with replacement so a byte outside the host
    codepage never silently empties the result. On timeout the whole process
    tree is killed and whatever was captured so far is returned.

    `level` is a hearth_sandbox level ("off", "limits", "workspace"); None
    means "whatever HEARTH_SANDBOX or the platform default says". At a
    contained level the child runs under a restricted token in a Job object;
    see hearth_sandbox for what that does and does not stop.
    """
    argv, spill = _shell_argv(cmd, cwd)
    child_environment = env or child_env()

    try:
        level = _prepare_level(cwd, level)
    except hearth_sandbox.SandboxError as exc:
        if spill:
            _unlink_with_retries(spill)
        return 126, "", (
            "hearth-sandbox: refusing to run at the 'workspace' level because "
            "containment could not be established: {}. Fix the workspace, or "
            "set HEARTH_SANDBOX=limits to run without a write boundary."
        ).format(exc), False

    proc = None
    if level in ("limits", "workspace"):
        try:
            proc = hearth_sandbox.spawn(argv, cwd, child_environment, level)
        except hearth_sandbox.SandboxError as exc:
            if level == "workspace":
                # This level's whole promise is that a command cannot write
                # outside the workspace. Running it anyway, uncontained,
                # would break that promise without telling anyone.
                if spill:
                    _unlink_with_retries(spill)
                return 126, "", (
                    "hearth-sandbox: refusing to run uncontained at the "
                    "'workspace' level: {}".format(exc)), False
            # `limits` claims resource caps and no orphans, not a boundary.
            # Degrading to the plain path costs the user nothing they were
            # relying on, and a machine where the Job API fails should not be
            # a machine where Hearth cannot run a command at all.
            print("[hearth-sandbox: {}; running without the Job limits]".format(exc),
                  file=sys.stderr)
            level = "off"

    popen_kw = {
        "cwd": cwd,
        "env": child_environment,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if hearth_paths.is_windows():
        popen_kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kw["start_new_session"] = True

    captured = {}

    try:
        if proc is None:
            proc = subprocess.Popen(argv, **popen_kw)
    except Exception as exc:
        # Broad on purpose: a non-OSError here (still possible on some
        # platforms/launchers) must not skip spill-file cleanup the same way
        # an OSError would.
        if spill:
            _unlink_with_retries(spill)
        return 127, "", str(exc), False

    def _pump():
        try:
            captured["out"], captured["err"] = proc.communicate()
        except Exception as exc:  # decode or pipe failure must not vanish
            captured["out"], captured["err"] = "", "output capture failed: {}".format(exc)

    t = threading.Thread(target=_pump, daemon=True)
    t.start()
    t.join(timeout)

    timed_out = t.is_alive()
    kill_incomplete = False
    if timed_out:
        _kill_tree(proc)
        t.join(_KILL_GRACE_S)
        if t.is_alive():
            # The tree did not confirm termination within the grace period:
            # taskkill/killpg can be missing, under-privileged, slow under
            # antivirus load, or a grandchild can have evaded the tree walk
            # entirely. Try once more, but do not wait forever for it (this
            # call is timeout-bound) and do not report a clean kill if it
            # still has not finished; the caller and the model need to know
            # the tree may still be alive rather than reading rc=-1 as an
            # ordinary exit.
            _kill_tree(proc)
            t.join(_KILL_GRACE_S)
            kill_incomplete = t.is_alive()

    if spill:
        _unlink_with_retries(spill)

    out = captured.get("out") or ""
    err = captured.get("err") or ""
    if kill_incomplete:
        note = (
            "hearth-proc: process tree did not confirm termination after "
            "repeated kill attempts; some processes or file handles may "
            "still be alive"
        )
        err = (err + "\n" + note) if err else note
    rc = proc.returncode if proc.returncode is not None else -1
    return rc, out, err, timed_out


def _self_test():
    import shutil

    ws = os.path.realpath(tempfile.mkdtemp(prefix="hearth-proc-"))
    try:
        # Basic execution and exit codes.
        rc, out, err, to = run_contained("echo hello", ws, timeout=30)
        assert rc == 0, (rc, out, err)
        assert "hello" in out, repr(out)
        assert to is False

        rc, out, err, to = run_contained("exit 3", ws, timeout=30)
        assert rc == 3, rc

        # A timeout returns promptly and reports itself, rather than blocking
        # until the grandchildren finish.
        slow = "ping -n 20 127.0.0.1 >nul" if hearth_paths.is_windows() else "sleep 20"
        t0 = time.time()
        rc, out, err, to = run_contained(slow, ws, timeout=3)
        elapsed = time.time() - t0
        assert to is True, "timeout not reported"
        assert elapsed < 12, "timeout took {:.1f}s, process tree was not killed".format(elapsed)

        # A tree that never confirms termination is reported through stderr,
        # not silently treated as a clean kill (regression test for a bug
        # found in review: the recovery path used to fall through and return
        # as though cleanup had worked). _kill_tree is replaced with a no-op
        # so the underlying process is guaranteed to still be alive through
        # both grace windows, and _KILL_GRACE_S is shrunk so the test stays
        # fast; the real process is cleaned up for real afterward using the
        # unpatched _kill_tree.
        from unittest import mock
        real_kill_tree = _kill_tree
        leaked = {}

        def _fake_kill_tree(proc):
            leaked["proc"] = proc  # record it; deliberately do not kill it

        stubborn = "ping -n 30 127.0.0.1 >nul" if hearth_paths.is_windows() else "sleep 30"
        try:
            with mock.patch.object(sys.modules[__name__], "_kill_tree", _fake_kill_tree), \
                 mock.patch.object(sys.modules[__name__], "_KILL_GRACE_S", 0.3):
                rc2, out2, err2, to2 = run_contained(stubborn, ws, timeout=1)
            assert to2 is True, "timeout not reported for the stubborn tree"
            assert "did not confirm termination" in err2, repr(err2)
        finally:
            proc = leaked.get("proc")
            if proc is not None:
                real_kill_tree(proc)
                try:
                    proc.wait(timeout=10)
                except Exception:
                    pass

        # Non-UTF-8 output never collapses to an empty string.
        if hearth_paths.is_windows():
            probe = 'python -c "import sys; sys.stdout.buffer.write(b\'A\\xff\\xfeB\')"'
        else:
            probe = "printf 'A\\xff\\xfeB'"
        rc, out, err, to = run_contained(probe, ws, timeout=30)
        assert out is not None
        assert "A" in out and "B" in out, repr(out)

        # A command past the inline limit still runs.
        big = "echo " + ("x" * (MAX_INLINE_COMMAND + 500))
        rc, out, err, to = run_contained(big, ws, timeout=60)
        assert rc == 0, (rc, err[:200])
        assert "x" * 100 in out, "long command was truncated"

        # No script spill files are left behind.
        leftovers = [f for f in os.listdir(ws) if f.startswith(".hearth-cmd")]
        assert not leftovers, leftovers

        # child_env does not leak the parent's whole environment but keeps what
        # a build needs.
        e = child_env()
        assert "PATH" in e or "Path" in e
        assert e.get("PYTHONIOENCODING") == "utf-8"
        assert e.get("PYTHONUTF8") == "1"
    finally:
        shutil.rmtree(ws, ignore_errors=True)
    print("hearth-proc self-test OK")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
