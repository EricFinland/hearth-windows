"use strict";
/* Starting, reading and stopping the Python sidecar.
 *
 * The handshake
 * -------------
 * desktop/server/main.py binds 127.0.0.1 on an ephemeral port, mints a bearer
 * token, and prints exactly one line of JSON on stdout:
 *
 *     {"port": <int>, "token": "<str>", "pid": <int>}
 *
 * That line is the only place the token is ever emitted. It is read here, in
 * the main process, and goes to the renderer over IPC. It is never written to
 * a file, never put on a command line (any local process can read another
 * process's argv), and never served over HTTP.
 *
 * Nothing may be orphaned
 * -----------------------
 * The sidecar can be holding a llama-server child with several gigabytes of
 * VRAM. Two mechanisms keep that from outliving the app, and they cover
 * different failures:
 *
 *   1. llama-server is inside a kill-on-close Windows Job Object owned by the
 *      Python process (agent/hearth_llama._win_job). When Python dies, by any
 *      means including TerminateProcess, the kernel closes the last handle to
 *      that job and kills what is inside it. This shell must not break that
 *      chain, which means it must not put itself between them: it starts
 *      Python directly and lets Python own its own child.
 *
 *   2. Python is started with --watch-parent and a piped stdin that this
 *      process holds open. If this process stops existing -- quit, Task
 *      Manager, crash -- the write end of that pipe is closed by the OS,
 *      Python reads EOF and exits, which triggers (1). This is what makes a
 *      killed shell safe; without it the sidecar would simply be reparented
 *      and keep the model resident. Measured: killing this process kills the
 *      sidecar within two seconds.
 *
 *   3. A newline written on that pipe every HEARTBEAT_MS, because (2) is a
 *      guarantee about handle ownership that this process cannot actually
 *      make. A parent that leaks an inheritable copy of the write handle
 *      into the child produces no EOF at all, ever -- that is not
 *      hypothetical, it is what CPython's own subprocess module does on
 *      Windows, and it was found by testing this exact sidecar under a
 *      Python parent instead of a Node one. libuv, which is what spawn()
 *      uses here, does not do it. The heartbeat means the sidecar does not
 *      have to take that on trust: it shuts down when the beats stop,
 *      whatever the pipe thinks. See desktop/server/main.py's watch_parent.
 *
 * stop() below is the polite version of the same thing: close stdin, wait,
 * and only then kill. The kill is the fallback, not the mechanism.
 */

const { spawn } = require("node:child_process");
const { createInterface } = require("node:readline");

const HANDSHAKE_TIMEOUT_MS = 30000;
const STOP_GRACE_MS = 4000;

/* Comfortably inside the sidecar's PARENT_HEARTBEAT_TIMEOUT of 60s, so a
 * momentarily busy event loop cannot be mistaken for a dead shell. */
const HEARTBEAT_MS = 15000;

/** Start the sidecar and resolve once it has printed its handshake.
 *
 *  Resolves with {child, handshake}. Rejects if the process cannot start, if
 *  it exits first, or if it says nothing for HANDSHAKE_TIMEOUT_MS.
 *
 *  onLog receives the sidecar's diagnostic output line by line. Handshake
 *  fields never reach it: the line that parses as a handshake is consumed
 *  here and not forwarded, so a log sink cannot become a token sink.
 */
function startSidecar({ python, serverDir, onLog = () => {}, onExit = () => {} }) {
  return new Promise((resolvePromise, rejectPromise) => {
    let child;
    try {
      child = spawn(python, ["main.py", "--watch-parent"], {
        cwd: serverDir,
        // stdin is a pipe this process holds open and never writes to. It is
        // the liveness handle --watch-parent keys on; see the note above.
        stdio: ["pipe", "pipe", "pipe"],
        windowsHide: true,
        env: { ...process.env, PYTHONUNBUFFERED: "1" },
      });
    } catch (err) {
      rejectPromise(new Error(`could not start the sidecar: ${err.message}`));
      return;
    }

    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      try { child.kill(); } catch { /* already gone */ }
      rejectPromise(new Error("the sidecar printed no handshake within 30s"));
    }, HANDSHAKE_TIMEOUT_MS);

    child.on("error", (err) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      rejectPromise(new Error(`could not start ${python}: ${err.message}`));
    });

    createInterface({ input: child.stderr }).on("line", (line) => onLog(line));

    const lines = createInterface({ input: child.stdout });
    lines.on("line", (line) => {
      if (settled) { onLog(line); return; }
      let handshake;
      try { handshake = JSON.parse(line); } catch { onLog(line); return; }
      if (!handshake || typeof handshake.port !== "number" || typeof handshake.token !== "string") {
        onLog(line);
        return;
      }
      settled = true;
      clearTimeout(timer);
      startHeartbeat(child);
      resolvePromise({ child, handshake });
    });

    child.on("exit", (code, signal) => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        rejectPromise(new Error(
          `the sidecar exited with code ${code} before printing a handshake`));
        return;
      }
      onExit(code, signal);
    });
  });
}

/** Write a newline down the sidecar's stdin every HEARTBEAT_MS.
 *
 *  The bytes carry nothing: the sidecar reads and discards them, and has no
 *  stdin protocol at all. Their arrival is the whole message. The interval
 *  is unref'd so it never keeps the event loop alive on its own, and a
 *  failed write is ignored: the child being gone is exactly the case where
 *  there is nothing left to tell.
 */
function startHeartbeat(child) {
  const timer = setInterval(() => {
    if (child.exitCode !== null || child.signalCode !== null) {
      clearInterval(timer);
      return;
    }
    try { child.stdin.write("\n"); } catch { /* the child is going away */ }
  }, HEARTBEAT_MS);
  if (timer.unref) timer.unref();
  child.once("exit", () => clearInterval(timer));
  return timer;
}

/** Stop the sidecar and wait for it to actually be gone.
 *
 *  Closing stdin is the request; killing is the fallback if it is ignored.
 *  Resolves when the process has exited, or after the grace period, at which
 *  point it has been killed and its job object has taken llama-server with
 *  it either way.
 */
function stopSidecar(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return Promise.resolve();
  return new Promise((done) => {
    let finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      done();
    };
    child.once("exit", finish);
    try { child.stdin.end(); } catch { /* already closed */ }
    const timer = setTimeout(() => {
      try { child.kill(); } catch { /* already gone */ }
      // Give the kill itself a moment to be reported before giving up.
      setTimeout(finish, 500).unref?.();
    }, STOP_GRACE_MS);
    if (timer.unref) timer.unref();
  });
}

module.exports = { startSidecar, stopSidecar, HANDSHAKE_TIMEOUT_MS, STOP_GRACE_MS };
