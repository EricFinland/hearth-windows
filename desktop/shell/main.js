"use strict";
/* Hearth's desktop shell.
 *
 * What it is responsible for, and nothing else:
 *
 *   1. Start the Python sidecar with the interpreter shipped beside it, and
 *      read the one-line handshake off its stdout.
 *   2. Serve desktop/ui/ on a loopback origin that also forwards the
 *      sidecar's routes, because the sidecar refuses a page served from
 *      anywhere else (see origin.js for the full reason).
 *   3. Hand {origin, token} to the renderer over IPC, and nothing else.
 *   4. Make sure that when this process stops existing, so does the sidecar,
 *      and therefore so does llama-server and the several gigabytes of VRAM
 *      it may be holding (see sidecar.js).
 *
 * Everything a user actually sees lives in desktop/ui/, which has no npm
 * dependencies and no build step, and everything Hearth actually does lives
 * in the Python sidecar. This file is deliberately thin: it is the part a
 * Tauri port replaces, and the less of the product lives here the cheaper
 * that port is. The three things it would have to reimplement are exactly
 * the numbered list above.
 */

const { app, BrowserWindow, ipcMain, dialog, shell, session } = require("electron");
const { existsSync, mkdirSync, createReadStream, statSync } = require("node:fs");
const { join, resolve, sep } = require("node:path");
const { createHash } = require("node:crypto");
const { spawn } = require("node:child_process");
const { request: httpRequest } = require("node:http");

const { startOrigin } = require("./origin");
const { startSidecar, stopSidecar } = require("./sidecar");

const APP_ID = "com.hearthlocal.hearth";

/* ------------------------------------------------------------------ layout
 *
 * Packaged, the payload sits beside the Electron runtime:
 *
 *   Hearth.exe
 *   resources/app.asar          this file, preload.js, origin.js, sidecar.js
 *   resources/python/           the vendored CPython (scripts/vendor_python.py)
 *   resources/hearth/agent/     the agent modules, unchanged
 *   resources/hearth/desktop/server/
 *   resources/hearth/ui/
 *   resources/hearth/vendor/llama/llama-server.exe
 *
 * That layout is not arbitrary. agent/hearth_llama.app_root() resolves to
 * the parent of the agent directory and then looks for vendor/llama beneath
 * it, so putting the payload together this way makes find_server() locate
 * the bundled engine with no packaging-specific code in the agent at all.
 *
 * Unpackaged (npm start), the payload is the checkout itself, so the shell
 * can be run and debugged against the working tree before anything is built.
 */
const PACKAGED = app.isPackaged;
const PAYLOAD = PACKAGED
  ? join(process.resourcesPath, "hearth")
  : resolve(__dirname, "..", "..");
const UI_DIR = PACKAGED ? join(PAYLOAD, "ui") : join(PAYLOAD, "desktop", "ui");
const SERVER_DIR = join(PAYLOAD, "desktop", "server");

/** The interpreter to run the sidecar with.
 *
 *  Packaged, this is the only candidate: the vendored CPython inside the
 *  install. There is deliberately no fallback to a Python on PATH, because a
 *  user's own Python is a different version with different site-packages and
 *  "it worked on the machine that had Python" is exactly the failure this
 *  whole exercise exists to remove. Unpackaged, the vendored interpreter is
 *  still preferred so development exercises what ships.
 */
function pythonExecutable() {
  if (PACKAGED) return join(process.resourcesPath, "python", "python.exe");
  const vendored = join(PAYLOAD, "vendor", "python", "python.exe");
  if (existsSync(vendored)) return vendored;
  return process.env.HEARTH_PYTHON || (process.platform === "win32" ? "python" : "python3");
}

/** Where a session starts if the user has not chosen anywhere.
 *
 *  A folder in the user's home directory, created on demand. It is only a
 *  default: the workspace is whatever the user types or picks, and the
 *  sidecar's containment boundary is what enforces it, not this.
 */
function defaultWorkspace() {
  const dir = join(app.getPath("home"), "Hearth");
  try {
    mkdirSync(dir, { recursive: true });
  } catch {
    return app.getPath("home");
  }
  return dir;
}

/* --------------------------------------------------------------- updating
 *
 * This is the only code in Hearth that executes a downloaded file, and it is
 * here rather than in the sidecar on purpose: agent/hearth_update.py decides
 * whether bytes are trustworthy, and a module that makes that decision must
 * not also be able to act on it. The sidecar fetches a release manifest,
 * verifies its Ed25519 signature against the key pinned in release/trust.json,
 * refuses downgrades and expired manifests, downloads the installer, checks
 * its SHA-256 against the SIGNED manifest, and stops. Then this runs.
 *
 * Three checks happen HERE, and none of them is ceremony:
 *
 *   1. The path must be inside the staging directory, which this process
 *      derives itself from the same rule agent/hearth_paths.py uses, rather
 *      than believing the path it was handed. A response that could name any
 *      file would turn "install the update" into "run that".
 *   2. The size and SHA-256 are recomputed from the file on disk. The sidecar
 *      already verified it, but a verified installer then SITS on disk for as
 *      long as the user takes to click, and every other process running as
 *      that user can write to it in that window. Hashing immediately before
 *      the spawn narrows the window to the width of one syscall. This is the
 *      check that Authenticode will one day duplicate at the OS level; until
 *      a certificate exists it is the only one.
 *   3. The version must be strictly greater than app.getVersion(). That value
 *      comes out of the asar, and EnableEmbeddedAsarIntegrityValidation (see
 *      verify-fuses.js) makes the asar tamper-evident, so it is a better
 *      answer than any file in the install directory. Downgrade protection
 *      that only lived in the sidecar could be undone by editing a file next
 *      to it.
 *
 * Then the user is shown what will run, and only a yes proceeds.
 */

/** Where verified installers are staged, derived here rather than trusted.
 *
 *  Mirrors agent/hearth_paths.data_dir(): HEARTH_DATA_DIR when set, else
 *  %LOCALAPPDATA%\Hearth on Windows. Deriving it independently is the whole
 *  point -- a containment check that asks the thing being checked where the
 *  boundary is checks nothing.
 */
function stagingRoot() {
  if (process.env.HEARTH_UPDATE_DIR) {
    return resolve(process.env.HEARTH_UPDATE_DIR, "staged");
  }
  const data = process.env.HEARTH_DATA_DIR
    || (process.platform === "win32"
      ? join(process.env.LOCALAPPDATA || join(app.getPath("home"), "AppData", "Local"), "Hearth")
      : join(app.getPath("home"), ".local", "share", "hearth"));
  return resolve(data, "update", "staged");
}

/** [major, minor, patch] or null. Same strictness as hearth_update.parse_version:
 *  exactly three dotted decimals, because the comparison downgrade protection
 *  rests on has to be a total order with no surprises. */
function parseVersion(text) {
  const match = /^(\d{1,6})\.(\d{1,6})\.(\d{1,6})$/.exec(String(text || "").trim());
  return match ? [Number(match[1]), Number(match[2]), Number(match[3])] : null;
}

function newerThan(a, b) {
  for (let i = 0; i < 3; i++) {
    if (a[i] !== b[i]) return a[i] > b[i];
  }
  return false;
}

function sha256OfFile(path) {
  return new Promise((done, fail) => {
    const hash = createHash("sha256");
    const stream = createReadStream(path);
    stream.on("error", fail);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("end", () => done(hash.digest("hex")));
  });
}

/** GET a sidecar route from the main process, with the bearer token.
 *  Bounded: an update snapshot is a few hundred bytes and anything past
 *  64 KiB is a malfunction, not a snapshot. */
function sidecarGet(path) {
  return new Promise((done, fail) => {
    const req = httpRequest({
      host: "127.0.0.1",
      port: handshake.port,
      method: "GET",
      path,
      headers: {
        host: `127.0.0.1:${handshake.port}`,
        authorization: `Bearer ${handshake.token}`,
        accept: "application/json",
      },
    }, (res) => {
      let body = "";
      res.setEncoding("utf-8");
      res.on("data", (chunk) => {
        body += chunk;
        if (body.length > 65536) {
          req.destroy();
          fail(new Error("the sidecar's reply is implausibly large"));
        }
      });
      res.on("end", () => {
        try { done(JSON.parse(body)); } catch (err) { fail(err); }
      });
    });
    req.on("error", fail);
    req.end();
  });
}

/** Verify and run the staged installer. Resolves {error} or never resolves,
 *  because success means this process is on its way out. */
async function installStagedUpdate() {
  let snapshot;
  try {
    snapshot = await sidecarGet("/update");
  } catch (err) {
    return { error: `Hearth could not read its own update state: ${err.message}` };
  }
  const staged = snapshot && snapshot.staged;
  if (!staged || !staged.path) {
    return { error: "There is no verified update ready to install." };
  }

  const target = resolve(staged.path);
  const root = stagingRoot();
  if (target !== root && !target.startsWith(root + sep)) {
    return { error: "Refusing to run an installer from outside Hearth's update folder." };
  }
  if (!/\.exe$/i.test(target) || !existsSync(target)) {
    return { error: "The staged installer is not where it should be. Check for updates again." };
  }

  const installed = parseVersion(app.getVersion());
  const offered = parseVersion(staged.version);
  if (!installed || !offered) {
    return { error: "Refusing an update whose version cannot be read." };
  }
  if (!newerThan(offered, installed)) {
    // The sidecar refuses rollbacks too, using a floor in the user's data
    // directory. This is the second, independent refusal, and it is the one
    // that reads the running version out of the integrity-checked asar.
    return {
      error: `Refusing to "update" from ${app.getVersion()} to ${staged.version}. `
        + "That is a downgrade, not an update.",
    };
  }

  let size;
  let digest;
  try {
    size = statSync(target).size;
    digest = await sha256OfFile(target);
  } catch (err) {
    return { error: `Hearth could not read the staged installer: ${err.message}` };
  }
  if (size !== staged.size_bytes || digest !== staged.sha256) {
    return {
      error: "The staged installer changed on disk after it was verified, so it "
        + "will not be run. Nothing has been installed. Check for updates again.",
    };
  }

  const answer = await dialog.showMessageBox(mainWindow, {
    type: "question",
    buttons: ["Install and restart", "Not now"],
    defaultId: 0,
    cancelId: 1,
    title: "Install Hearth " + staged.version,
    message: `Install Hearth ${staged.version}?`,
    detail:
      `Hearth ${app.getVersion()} will close and the installer will run.\n\n`
      + `This installer was signed by ${staged.signed_by || "the release key"} `
      + "and its contents match that signature.\n\n"
      + `SHA-256: ${digest}`,
    noLink: true,
  });
  if (answer.response !== 0) return { error: null, cancelled: true };

  try {
    // Detached, so it outlives this process: the installer's first job is to
    // replace the executable that started it. /S is electron-builder's NSIS
    // silent switch -- the user has already been asked, here, with the
    // version and the hash in front of them, and asking twice trains people
    // to click through.
    const child = spawn(target, ["/S"], {
      detached: true,
      stdio: "ignore",
      windowsHide: false,
    });
    child.unref();
  } catch (err) {
    return { error: `The installer would not start: ${err.message}` };
  }

  // Quit immediately rather than waiting: the sidecar (and with it
  // llama-server, and with it several gigabytes of VRAM) has to be gone
  // before the installer can replace the files it is running from.
  setImmediate(() => app.quit());
  return { started: true, version: staged.version };
}

// ------------------------------------------------------------------- state

let mainWindow = null;
let sidecarChild = null;
let handshake = null;
let originServer = null;
let quitting = false;
const logLines = [];

function log(line) {
  // Bounded: a sidecar that spews for an hour must not grow the main
  // process without limit. Kept only so a startup failure can be shown to
  // the user; never written to disk, and the handshake line never reaches it.
  logLines.push(line);
  if (logLines.length > 400) logLines.shift();
  if (!PACKAGED) process.stderr.write("[sidecar] " + line + "\n");
}

// ------------------------------------------------------------------ window

function createWindow(origin) {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    show: false,
    backgroundColor: "#14100d",
    title: "Hearth",
    autoHideMenuBar: true,
    webPreferences: {
      preload: join(__dirname, "preload.js"),
      // The three that matter, stated explicitly rather than left to
      // defaults, because a later Electron upgrade changing a default must
      // not silently change this app's threat model.
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      spellcheck: false,
      devTools: !PACKAGED || process.env.HEARTH_DEVTOOLS === "1",
    },
  });

  mainWindow.once("ready-to-show", () => mainWindow.show());
  mainWindow.on("closed", () => { mainWindow = null; });

  // The renderer is a local application UI, not a browser. It may not
  // navigate anywhere but its own origin, may not open windows, and may not
  // ask for a single permission. Links that want a browser get the user's
  // real browser instead.
  const contents = mainWindow.webContents;
  contents.on("will-navigate", (event, url) => {
    if (!url.startsWith(origin + "/") && url !== origin && url !== origin + "/") {
      event.preventDefault();
      if (/^https?:\/\//.test(url)) shell.openExternal(url);
    }
  });
  contents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  contents.on("will-attach-webview", (event) => event.preventDefault());

  mainWindow.loadURL(origin + "/");
  return mainWindow;
}

function fatal(title, detail) {
  dialog.showErrorBox(title, detail);
  app.exit(1);
}

// --------------------------------------------------------------------- IPC

/** True when this IPC call came from our own window, loaded from our own
 *  origin. Electron already isolates IPC from anything outside the app, so
 *  this is the second lock rather than the first, but the thing behind the
 *  door is the sidecar's bearer token and it costs one comparison. */
function fromOurRenderer(event, origin) {
  if (!mainWindow || event.sender !== mainWindow.webContents) return false;
  const url = event.senderFrame ? event.senderFrame.url : event.sender.getURL();
  return typeof url === "string" && url.startsWith(origin + "/");
}

function registerIpc(origin) {
  ipcMain.handle("hearth:handshake", (event) => {
    if (!fromOurRenderer(event, origin)) throw new Error("refused");
    return {
      origin,
      token: handshake.token,
      defaultWorkspace: defaultWorkspace(),
    };
  });

  ipcMain.handle("hearth:install-update", async (event) => {
    if (!fromOurRenderer(event, origin)) throw new Error("refused");
    return installStagedUpdate();
  });

  ipcMain.handle("hearth:pick-folder", async (event) => {
    if (!fromOurRenderer(event, origin)) throw new Error("refused");
    const result = await dialog.showOpenDialog(mainWindow, {
      title: "Choose a workspace folder for Hearth",
      properties: ["openDirectory", "createDirectory"],
      buttonLabel: "Use this folder",
    });
    if (result.canceled || !result.filePaths.length) return { error: "No folder chosen." };
    return { path: result.filePaths[0] };
  });
}

// -------------------------------------------------------------------- boot

async function boot() {
  session.defaultSession.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
  session.defaultSession.setPermissionCheckHandler(() => false);

  const python = pythonExecutable();
  if (PACKAGED && !existsSync(python)) {
    fatal("Hearth is missing part of itself",
      `The bundled Python interpreter is not where it should be:\n\n${python}\n\n` +
      "Reinstalling Hearth should fix this.");
    return;
  }

  let started;
  try {
    started = await startSidecar({
      python,
      serverDir: SERVER_DIR,
      // The running version, read from the asar rather than from a file in
      // the install directory. See sidecar.js and hearth_update.current_version.
      extraEnv: { HEARTH_APP_VERSION: app.getVersion() },
      onLog: log,
      onExit: (code) => {
        if (quitting) return;
        fatal("Hearth stopped unexpectedly",
          `The Hearth service exited with code ${code}.\n\n` +
          logLines.slice(-12).join("\n"));
      },
    });
  } catch (err) {
    fatal("Hearth could not start",
      `${err.message}\n\nInterpreter: ${python}\nService: ${SERVER_DIR}\n\n` +
      logLines.slice(-12).join("\n"));
    return;
  }

  sidecarChild = started.child;
  handshake = started.handshake;

  originServer = await startOrigin({ uiDir: UI_DIR, sidecarPort: handshake.port });
  registerIpc(originServer.origin);
  createWindow(originServer.origin);
}

// ---------------------------------------------------------------- lifecycle

if (!app.requestSingleInstanceLock()) {
  // A second launch must not start a second sidecar: two of them would each
  // hold their own llama-server and their own copy of the model in memory.
  app.exit(0);
} else {
  app.setAppUserModelId(APP_ID);

  app.on("second-instance", () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  });

  app.on("window-all-closed", () => app.quit());

  app.on("before-quit", (event) => {
    if (quitting) return;
    quitting = true;
    event.preventDefault();
    // Stop the sidecar before leaving, and wait for it: exiting first would
    // work too (its stdin would close and --watch-parent would fire) but
    // waiting means a normal quit is deterministic rather than racy.
    Promise.resolve()
      .then(() => stopSidecar(sidecarChild))
      .then(() => (originServer ? originServer.close() : null))
      .catch(() => {})
      .then(() => app.exit(0));
  });

  app.whenReady().then(boot).catch((err) => {
    fatal("Hearth could not start", String(err && err.stack ? err.stack : err));
  });
}
