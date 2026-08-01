"use strict";
/* The entire surface the renderer gets from the shell.
 *
 * Two functions and one string. No Node, no filesystem, no child processes,
 * no ipcRenderer itself -- only these three named things cross the bridge,
 * and each one is a request the main process validates before answering.
 * contextIsolation is on and nodeIntegration is off (see main.js), so this
 * runs in its own world and the page cannot reach anything it does not
 * export here.
 *
 * Why the token comes through here rather than over HTTP: dev-host.mjs
 * serves the sidecar's bearer token at GET /__hearth/handshake to any
 * process on loopback, which its own comments give as the reason it is not
 * shippable. ipcRenderer.invoke is not reachable from outside this
 * application, so the token goes from the sidecar's stdout, to the main
 * process, to this renderer, and nowhere else.
 *
 * `shell` names the host so desktop/ui/ can stay shell-agnostic. A Tauri
 * build exposes the same three names with the same shapes and nothing in
 * desktop/ui/ has to know which one it is talking to.
 */

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("hearth", {
  shell: "electron",

  /** {origin, token, defaultWorkspace} for the sidecar this shell started. */
  handshake: () => ipcRenderer.invoke("hearth:handshake"),

  /** Native folder chooser. Resolves {path} or {error}. */
  pickFolder: () => ipcRenderer.invoke("hearth:pick-folder"),
});
