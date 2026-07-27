#!/usr/bin/env python3
"""hearth contained subprocess execution.

Running a child correctly is platform work, so it lives here rather than in the
tool layer. Three things this module exists to guarantee:

  1. Output is decoded as UTF-8 with replacement, never the host ANSI codepage.
     A decode error inside subprocess's reader thread produces returncode 0 with
     stdout None, which reads to a model as "the command produced no output".
  2. A timeout kills the whole process tree. With shell=True the direct child is
     the shell; grandchildren inherit the pipe handles and keep the call blocked
     long past the deadline.
  3. A command longer than the shell's command-line limit is spilled to a script
     file rather than truncated. Windows caps a command line near 8191
     characters, and Unix-trained models emit long heredocs constantly.

This module does NOT sandbox the child. The working directory is not a security
boundary. See the spec section on run_command containment.

Standard library only.
"""

import os
import subprocess
import sys
import tempfile
import threading

import hearth_paths


MAX_INLINE_COMMAND = 7000  # Windows caps a command line near 8191 characters.

_KEEP_ENV = (
    "PATH", "Path", "SYSTEMROOT", "SystemRoot", "COMSPEC", "ComSpec",
    "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "LC_ALL",
    "PROGRAMFILES", "ProgramFiles", "PROGRAMDATA", "APPDATA", "LOCALAPPDATA",
    "WINDIR", "PATHEXT", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
)


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
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        if hearth_paths.is_windows():
            fh.write("@echo off\r\n" + cmd.replace("\n", "\r\n") + "\r\n")
        else:
            fh.write("#!/bin/sh\n" + cmd + "\n")
    if not hearth_paths.is_windows():
        os.chmod(path, 0o700)
    return ([path] if hearth_paths.is_windows() else ["/bin/sh", path]), path


def _kill_tree(proc):
    """Kill the child and everything it spawned.

    proc.kill() reaches only the direct child, which with a shell is the shell
    itself. On Windows taskkill /T walks the tree; on POSIX the process group
    does, which is why the child is started in its own group.
    """
    try:
        if hearth_paths.is_windows():
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=15,
            )
        else:
            os.killpg(os.getpgid(proc.pid), 9)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_contained(cmd, cwd, timeout=120, env=None):
    """Run `cmd` in `cwd`, returning (returncode, stdout, stderr, timed_out).

    Output is decoded as UTF-8 with replacement so a byte outside the host
    codepage never silently empties the result. On timeout the whole process
    tree is killed and whatever was captured so far is returned.
    """
    argv, spill = _shell_argv(cmd, cwd)
    popen_kw = {
        "cwd": cwd,
        "env": env or child_env(),
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
        proc = subprocess.Popen(argv, **popen_kw)
    except OSError as exc:
        if spill:
            try:
                os.unlink(spill)
            except OSError:
                pass
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
    if timed_out:
        _kill_tree(proc)
        t.join(10)

    if spill:
        try:
            os.unlink(spill)
        except OSError:
            pass

    out = captured.get("out") or ""
    err = captured.get("err") or ""
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
        import time
        slow = "ping -n 20 127.0.0.1 >nul" if hearth_paths.is_windows() else "sleep 20"
        t0 = time.time()
        rc, out, err, to = run_contained(slow, ws, timeout=3)
        elapsed = time.time() - t0
        assert to is True, "timeout not reported"
        assert elapsed < 12, "timeout took {:.1f}s, process tree was not killed".format(elapsed)

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
