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
const { existsSync, mkdirSync } = require("node:fs");
const { join, resolve } = require("node:path");

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
