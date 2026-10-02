/* hearth desktop UI controller.
 *
 * Wires the sidecar (desktop/server) to the transcript and the side panels.
 * Plain ES modules, no build step, no dependencies, no network fetches beyond
 * the sidecar itself.
 */

import { Sidecar, HttpError, readHandshake, pickFolder, hasShellBridge, installUpdate } from "./api.js";
import { Transcript } from "./transcript.js";
import { HistoryPanel } from "./history.js";
import { el, icon, appendAll, clear, setText, neutralize, $ } from "./dom.js";
import { blob } from "./safe-text.js";
import { renderDiff } from "./diff.js";
import { ShopView } from "./shop.js";
import { startModelChip, modelLoadingHint } from "./model-chip.js";
import { LoopConfigPanel, LoopRunBar, account as loopAccount } from "./loop.js";
import { initAttachments, takeAttachments, returnAttachments } from "./attach.js";
import { SwarmConfigPanel, SwarmRunBar, account as swarmAccount } from "./swarm.js";
import { renderUpdateBanner, showUpdateBannerAgain } from "./update-banner.js";
import { renderUpdate } from "./update.js";
import { McpPanel } from "./mcp.js";

const RECENTS_KEY = "hearth.recentWorkspaces"; // workspace paths only; the bearer token is never stored
const MAX_RECENTS = 8;

const sidecar = new Sidecar("");
const transcript = new Transcript($("#transcript"));

const ui = {
  conn: $("#conn"),
  connLabel: $("#conn-label"),
  chipWorkspace: $("#chip-workspace"),
  chipModel: $("#chip-model"),
  chipMode: $("#chip-mode"),
  workspace: $("#in-workspace"),
  workspaceList: $("#dl-workspaces"),
  browse: $("#btn-browse"),
  model: $("#in-model"),
  reloadModels: $("#btn-models"),
  mode: $("#in-mode"),
  engine: $("#in-engine"),
  loopPanel: $("#loop-panel"),
  loopConfig: $("#loop-config"),
  loopRunBar: $("#loop-runbar"),
  swarmPanel: $("#swarm-panel"),
  swarmConfig: $("#swarm-config"),
  swarmRunBar: $("#swarm-runbar"),
  connect: $("#btn-connect"),
  sessionNote: $("#session-note"),
  setupBody: $("#setup-body"),
  engineBody: $("#engine-body"),
  updateBody: $("#update-body"),
  reloadSetup: $("#btn-setup"),
  cpList: $("#cp-list"),
  cpNote: $("#cp-note"),
  reloadCheckpoints: $("#btn-checkpoints"),
  composer: $("#composer"),
  send: $("#btn-send"),
  stop: $("#btn-stop"),
  composerStatus: $("#composer-status"),
  scrim: $("#modal-scrim"),
  modalTitle: $("#modal-title"),
  modalBody: $("#modal-body"),
  modalActions: $("#modal-actions"),
  chatView: $(".chat"),
  shopView: $("#shop"),
  tabChat: $("#tab-chat"),
  tabShop: $("#tab-shop"),
  tabShopBadge: $("#tab-shop-badge"),
};

const state = {
  handshake: null,
  session: null,      // last known GET /session body
  running: false,
  lastEventId: 0,
  streamGeneration: 0,
  streamAbort: null,
  checkpoints: [],
  backendHealthy: false,
  view: "chat",
  // How many models GET /models listed. null means the list could not be
  // read at all, which is not the same as zero: only zero is a first run.
  modelCount: null,
  // The last GET /loop snapshot. The work loop's run state is a gauge on its
  // own versioned stream, not folded out of the transcript -- see loop.js and
  // loop_engine.py's "TWO SHAPES". Held whole and replaced whole, which is
  // what makes a reconnect an hour into a run immediately correct.
  loop: null,
  loopStream: null,
  loopGeneration: 0,
  // The last GET /swarm snapshot. Its own gauge and its own stream, for the
  // reason app.py keeps two: a relay's state carries phases and roles that a
  // loop has no notion of, and one merged shape would be half empty whichever
  // engine was running.
  swarm: null,
  swarmStream: null,
  swarmGeneration: 0,
  // True once the account for the CURRENT run has been put in the transcript,
  // so a reconnect that replays the terminal event does not print it twice.
  accountShownFor: null,
  // The swarm keeps its OWN marker rather than sharing accountShownFor. Both
  // gauges stream for the life of the page, so a shared marker lets one
  // engine clear the other's: a finished loop's account would be re-appended
  // to the transcript every time a running relay's snapshot set the marker
  // back to null. Two runs, two markers.
  swarmAccountShownFor: null,
};

const loopConfigPanel = new LoopConfigPanel(ui.loopConfig);
const loopRunBar = new LoopRunBar(ui.loopRunBar);
const swarmConfigPanel = new SwarmConfigPanel(ui.swarmConfig);
const swarmRunBar = new SwarmRunBar(ui.swarmRunBar);

// ---------------------------------------------------------------------- views

/** Chat, the shop and the Tools screen are panes over one sidebar, not
 *  separate pages: the download stream, the session and the event stream all
 *  belong to the page, so switching views must never tear any of them down.
 *  That is also what makes "downloads survive navigating between chat and
 *  shop" true by construction rather than by bookkeeping. The Tools screen
 *  holds no stream at all; it polls only while it is the one showing. */
let mcpPanel = null;

function setView(name) {
  const views = {
    chat: [ui.chatView, ui.tabChat],
    shop: [ui.shopView, ui.tabShop],
    tools: [$("#tools"), $("#tab-tools")],
  };
  if (!views[name]) name = "chat";
  state.view = name;
  for (const [key, [pane, tab]] of Object.entries(views)) {
    const on = key === name;
    pane.hidden = !on;
    tab.classList.toggle("is-active", on);
    tab.setAttribute("aria-pressed", String(on));
  }
  if (name === "shop") shopView?.focus();
  if (name === "tools") mcpPanel?.show();
}

// ---------------------------------------------------------------- connection

function setConn(kind, label) {
  ui.conn.dataset.state = kind;
  setText(ui.connLabel, label);
}

function setChip(node, value, mono) {
  setText(node.querySelector(".chip-text"), value);
  node.classList.toggle("mono", Boolean(mono));
  // A tooltip is displayed text too, and a model name arrives from whoever
  // published the repository it was downloaded from.
  node.title = neutralize(value);
}

// ------------------------------------------------------------------- helpers

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function errorText(err) {
  if (err instanceof HttpError) return err.message;
  if (err && err.message) return err.message;
  return String(err);
}

function formatTime(seconds) {
  if (!Number.isFinite(seconds)) return "";
  const d = new Date(seconds * 1000);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return sameDay ? time : `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

function readRecents() {
  try {
    const parsed = JSON.parse(localStorage.getItem(RECENTS_KEY) || "[]");
    return Array.isArray(parsed) ? parsed.filter((v) => typeof v === "string") : [];
  } catch { return []; }
}

function rememberWorkspace(path) {
  const next = [path, ...readRecents().filter((p) => p !== path)].slice(0, MAX_RECENTS);
  try { localStorage.setItem(RECENTS_KEY, JSON.stringify(next)); } catch { /* private mode */ }
  paintRecents();
}

function paintRecents() {
  clear(ui.workspaceList);
  for (const path of readRecents()) ui.workspaceList.appendChild(el("option", { value: path }));
}

// --------------------------------------------------------------------- modal

let modalDismiss = null;

function openModal(title, bodyNodes, actions) {
  setText(ui.modalTitle, title);
  clear(ui.modalBody);
  appendAll(ui.modalBody, bodyNodes);
  clear(ui.modalActions);
  for (const action of actions) {
    const button = el("button", { class: "btn " + (action.variant || ""), type: "button", text: action.label });
    button.addEventListener("click", () => { closeModal(); action.run?.(); });
    ui.modalActions.appendChild(button);
  }
  ui.scrim.hidden = false;
  modalDismiss = () => closeModal();
  const first = ui.modalActions.querySelector(".btn-primary, .btn-danger, .btn");
  if (first) first.focus({ preventScroll: true });
}

function closeModal() {
  ui.scrim.hidden = true;
  modalDismiss = null;
}

ui.scrim.addEventListener("click", (event) => { if (event.target === ui.scrim) closeModal(); });

// --------------------------------------------------------------- setup panel

async function refreshSetup() {
  clear(ui.setupBody);
  ui.setupBody.appendChild(el("p", { class: "panel-note", text: "Checking..." }));
  let diagnosis;
  try {
    diagnosis = await sidecar.setup();
  } catch (err) {
    state.backendHealthy = false;
    clear(ui.setupBody);
    ui.setupBody.appendChild(el("p", {
      class: "panel-note is-error",
      text: "Could not reach the sidecar: " + errorText(err),
    }));
    setConn("down", "sidecar unreachable");
    return;
  }

  const healthy = diagnosis.healthy === true;
  state.backendHealthy = healthy;
  engineBackend = typeof diagnosis.backend === "string" ? diagnosis.backend : null;
  renderEngine();
  clear(ui.setupBody);

  ui.setupBody.appendChild(appendAll(el("div", { class: "setup-head " + (healthy ? "is-ok" : "is-bad") }), [
    icon(healthy ? "i-check" : "i-alert"),
    el("span", {
      class: "setup-status " + (healthy ? "is-ok" : "is-bad"),
      text: String(diagnosis.status ?? "unknown").replace(/_/g, " "),
    }),
  ]));

  const next = diagnosis.next_action;
  if (next) {
    ui.setupBody.appendChild(el("p", { class: "setup-message", text: String(next.message ?? "") }));
    if (next.remedy) ui.setupBody.appendChild(el("pre", { class: "setup-remedy", text: String(next.remedy) }));
  } else {
    // Worded from the backend that was actually diagnosed. The bundled
    // engine has no URL to be reachable at: Hearth starts it on an
    // ephemeral loopback port it picks itself, and base_url is null on
    // that path, so the old unconditional "Ollama is reachable at the
    // configured URL" was a sentence about the wrong engine.
    ui.setupBody.appendChild(el("p", {
      class: "setup-message",
      text: engineBackend === "ollama"
        ? `Ollama is reachable at ${diagnosis.base_url ?? "the configured URL"}.`
        : "Hearth's own engine is ready.",
    }));
  }

  const findings = Array.isArray(diagnosis.findings) ? diagnosis.findings : [];
  if (findings.length) {
    const fold = el("details", { class: "fold" });
    fold.appendChild(el("summary", { text: `${findings.length} check${findings.length === 1 ? "" : "s"}` }));
    const lines = findings
      .map((f) => `${String(f.status ?? "?").toUpperCase().padEnd(8)} ${f.check}: ${f.message}`)
      .join("\n");
    fold.appendChild(blob(lines));
    ui.setupBody.appendChild(fold);
  }

  if (!state.session) setConn(healthy ? "ok" : "warn", healthy ? "ready" : "backend not ready");
}

// -------------------------------------------------------------- engine (GPU)

// The last snapshot GET /engine or its event stream delivered. Held here
// rather than re-fetched inside refreshSetup so that the Backend panel can
// be redrawn from a live stream frame without a second round trip.
let engineSnapshot = null;
let engineStream = null;
// Which engine GET /setup last said is active. The GPU engine row is about
// Hearth's own bundled llama.cpp, so it is hidden when the user is running
// on Ollama instead: Ollama manages its own GPU, and a row saying "vulkan on
// your RTX 5080" beside an Ollama session would describe an engine that is
// not answering anything. null means "not asked yet", which still shows the
// row, because on a first launch the bundled engine is what will run.
let engineBackend = null;

const ENGINE_LABEL = {
  idle: "not started",
  planning: "checking your GPU",
  downloading: "downloading",
  installing: "installing",
  verifying: "verifying",
  active: "active",
  skipped: "not needed",
  failed: "unavailable",
};

/** One line in the Backend panel saying what engine is really running.
 *
 *  The rule this obeys: never claim GPU acceleration that is not in effect.
 *  "active" is rendered from the sidecar's own `active` pointer, which only
 *  exists after the binary was downloaded against a pinned hash AND ran on
 *  this machine AND reported a GPU device; every other state renders as the
 *  CPU truth plus what is being done about it. */
function renderEngine() {
  const parent = ui.engineBody;
  clear(parent);
  const snap = engineSnapshot;
  if (!snap) return;
  if (engineBackend && engineBackend !== "llama") return;
  const state = String(snap.state ?? "idle");
  const active = snap.active && typeof snap.active === "object" ? snap.active : null;
  const row = el("div", { class: "engine-row is-" + state });

  if (state === "active" && active) {
    row.appendChild(el("span", {
      class: "engine-state is-ok",
      text: `${String(active.backend ?? "GPU")} on ${String(active.device ?? "your GPU")}`,
    }));
  } else {
    row.appendChild(el("span", {
      class: "engine-state",
      text: `CPU engine · GPU ${ENGINE_LABEL[state] ?? state}`,
    }));
  }

  const total = Number(snap.bytes_total) || 0;
  const done = Number(snap.bytes_done) || 0;
  if (state === "downloading" && total > 0) {
    const pct = Math.max(0, Math.min(100, (done / total) * 100));
    const fill = el("div", { class: "bar-fill" });
    fill.style.width = `${pct.toFixed(1)}%`;
    row.appendChild(el("div", { class: "bar" }, [fill]));
    row.appendChild(el("span", {
      class: "engine-pct",
      text: `${pct.toFixed(0)}% of ${(total / 1e6).toFixed(0)} MB`,
    }));
  }

  if (snap.message) {
    row.appendChild(el("p", { class: "engine-message", text: String(snap.message) }));
  }

  // A retry is offered exactly where it is useful: a fetch that failed, or
  // one that never ran. "Not needed" gets no button, because there is
  // nothing to retry on a machine with no GPU.
  if (state === "failed" || state === "idle") {
    const btn = el("button", {
      class: "btn btn-ghost btn-sm",
      type: "button",
      text: state === "failed" ? "Try again" : "Enable GPU",
    });
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        engineSnapshot = await sidecar.fetchEngine(state === "failed");
      } catch (err) {
        engineSnapshot = { ...(engineSnapshot ?? {}), state: "failed", message: errorText(err) };
      }
      renderEngine();
      refreshSetup();
    });
    row.appendChild(btn);
  }
  parent.appendChild(row);
}

/** Follow GET /engine/events for as long as the page is open.
 *
 *  Opened once at startup, before anything else needs the sidecar: the
 *  whole point of the design is that a user can work on the CPU engine
 *  while the GPU one arrives, so the panel has to be able to change under
 *  them without a reload. A dropped stream is not an error worth showing;
 *  the snapshot GET on the next refreshSetup covers it. */
async function watchEngine() {
  try {
    engineSnapshot = await sidecar.engine();
  } catch {
    return; // the sidecar is not up yet; refreshSetup will report that
  }
  renderEngine();
  if (engineStream) engineStream.abort();
  engineStream = new AbortController();
  const since = Number(engineSnapshot?.version) || 0;
  try {
    await sidecar.streamEngine({
      since,
      signal: engineStream.signal,
      onSnapshot: (snap) => {
        const before = engineSnapshot?.state;
        engineSnapshot = snap;
        // The engine row redraws on every frame: it is four elements and
        // reads only this snapshot. The full diagnosis is re-run only when
        // the state actually CHANGES, because it costs two subprocess
        // probes of llama-server and a byte counter ticking every 250ms
        // must not pay for that.
        renderEngine();
        if (snap.state !== before) refreshSetup();
      },
    });
  } catch { /* the stream ended; the next refreshSetup re-reads the snapshot */ }
}

// -------------------------------------------------------------------- updates

let updateSnapshot = null;
let updatePollTimer = null;

/* WHY THIS PANEL POLLS AND EVERY OTHER GAUGE STREAMS.
 *
 * Chromium allows six simultaneous HTTP/1.1 connections per origin, and this
 * page already holds five open forever: GET /events, /downloads/events,
 * /engine/events, /loop/events and /swarm/events. A sixth permanent stream
 * takes the last socket, and then no ordinary request can start at all --
 * not a prompt, not a model list, not the Cancel button on the very download
 * the sixth stream was watching. Measured on the packaged application:
 * `netstat` showed exactly six ESTABLISHED sockets from the renderer to the
 * origin server, and every fetch after that hung until it was aborted, while
 * the same request from the main process answered in 3 ms.
 *
 * So the Updates panel is the one gauge that does not get a stream. It reads
 * GET /update once at startup and then once a second only while a check or a
 * download is actually in flight, which leaves the sixth socket free between
 * polls. GET /update/events still exists and is still tested: it is the right
 * shape for a client that can afford a connection, and a Tauri shell (which
 * has no such limit, because there is no browser origin) can use it.
 *
 * The real fix is to stop having five permanent streams, by multiplexing the
 * gauges onto one. That is a change to four other files and is not this
 * change.
 */
const UPDATE_POLL_MS = 1000;

/** True while the sidecar is doing something whose progress is worth watching. */
function updateInFlight(snap) {
  // `running` as well as the state: POST /update answers the instant the
  // worker thread starts, which can be before that thread has set
  // "checking" or "downloading". Reading only the state there would stop
  // polling on a snapshot that is already out of date, and the launch
  // check's answer would never be drawn.
  return Boolean(snap) && (snap.state === "checking" || snap.state === "downloading"
    || snap.running === true);
}

function scheduleUpdatePoll() {
  if (updatePollTimer) {
    clearTimeout(updatePollTimer);
    updatePollTimer = null;
  }
  if (!updateInFlight(updateSnapshot)) return;
  updatePollTimer = setTimeout(async () => {
    updatePollTimer = null;
    try {
      updateSnapshot = await sidecar.update();
    } catch {
      return; // the panel keeps showing the last snapshot
    }
    renderUpdatePanel();
    scheduleUpdatePoll();
  }, UPDATE_POLL_MS);
}

/** Redraw the Updates panel from whatever the last snapshot said.
 *
 *  Every button here is a request to the sidecar and nothing more. "Install"
 *  is the exception and it is not an exception in this file either: it calls
 *  the shell, which reads the verified receipt from the sidecar itself,
 *  re-hashes the staged file, refuses anything outside the staging directory
 *  or any version that is not newer than its own, and asks the user again
 *  with the hash in front of them. This page cannot name a file and cannot
 *  make anything run. */
function renderUpdatePanel() {
  renderUpdateBanner(updateSnapshot, {
    onInstall: (button) => {
      button.disabled = true;
      installFromBanner();
    },
  });
  // "Install now" on an update that still had to be downloaded: the click
  // asked for the whole thing, so once the download is verified and staged,
  // hand straight on to the shell (which still asks, with the hash shown).
  if (updateInstallPending && updateSnapshot) {
    if (updateSnapshot.state === "ready" && updateSnapshot.staged) {
      updateInstallPending = false;
      runUpdateInstall();
    } else if (updateSnapshot.state !== "downloading" && updateSnapshot.state !== "available") {
      updateInstallPending = false; // failed or cancelled; the panel says which
    }
  }
  renderUpdate(ui.updateBody, updateSnapshot, {
    onCheck: async (button) => {
      button.disabled = true;
      try {
        updateSnapshot = await sidecar.checkForUpdate(true);
      } catch (err) {
        updateSnapshot = { ...(updateSnapshot ?? {}), state: "failed", error: errorText(err) };
      }
      renderUpdatePanel();
      scheduleUpdatePoll();
    },
    onDownload: async (button) => {
      button.disabled = true;
      try {
        updateSnapshot = await sidecar.downloadUpdate();
      } catch (err) {
        updateSnapshot = { ...(updateSnapshot ?? {}), state: "failed", error: errorText(err) };
      }
      renderUpdatePanel();
      scheduleUpdatePoll();
    },
    onCancel: async () => {
      try { updateSnapshot = await sidecar.cancelUpdate(); } catch { /* the next poll will say */ }
      renderUpdatePanel();
      scheduleUpdatePoll();
    },
    onDismiss: async () => {
      try { updateSnapshot = await sidecar.dismissUpdate(); } catch { /* the next poll will say */ }
      renderUpdatePanel();
      scheduleUpdatePoll();
    },
    onAutoCheck: async (enabled) => {
      try { updateSnapshot = await sidecar.setUpdateAutoCheck(enabled); } catch { /* ignore */ }
      renderUpdatePanel();
    },
    onInstall: async (button) => {
      button.disabled = true;
      const cancelled = await runUpdateInstall();
      if (cancelled) button.disabled = false;
    },
  });
}

/** Set by the banner's "Install now" while the download it started is still
 *  running; renderUpdatePanel hands over to runUpdateInstall once the
 *  snapshot says the installer is verified and staged. */
let updateInstallPending = false;

/** True from the moment an install is asked for until the shell answers,
 *  and for good once it has started the installer (this window is about to
 *  close). The shell's dialog is not modal to this page, and every redraw
 *  while it is open puts a fresh, enabled Install button in the banner and
 *  the panel, so without this a second click opened a second dialog and, if
 *  both were accepted, started the installer twice. */
let updateInstallRunning = false;

/** Ask the shell to run the staged installer. Returns true when the user
 *  said "Not now" in the shell's own dialog. */
async function runUpdateInstall() {
  if (updateInstallRunning) return false;
  updateInstallRunning = true;
  let outcome = "failed";
  try {
    outcome = await askShellToInstall();
  } finally {
    if (outcome !== "started") updateInstallRunning = false;
  }
  return outcome === "cancelled";
}

/** One install_update call: "started", "cancelled" or "failed". */
async function askShellToInstall() {
  const result = await installUpdate();
  if (result && result.error) {
    updateSnapshot = { ...(updateSnapshot ?? {}), state: "failed", failure: "error",
      error: result.error };
    renderUpdatePanel();
    return "failed";
  }
  if (result && result.cancelled) {
    renderUpdatePanel();
    return "cancelled";
  }
  // Success means this window is about to close. Say so rather than
  // leaving a dead button behind.
  updateSnapshot = { ...(updateSnapshot ?? {}), state: "ready",
    message: "Closing Hearth and starting the installer…" };
  renderUpdatePanel();
  return "started";
}

/** The banner's "Install now": install a staged update at once, or download
 *  one first and install it when it is verified. Same requests as the
 *  panel's two buttons, in sequence; nothing here can run a file. */
async function installFromBanner() {
  showUpdateBannerAgain();
  if (updateSnapshot && updateSnapshot.state === "ready" && updateSnapshot.staged) {
    await runUpdateInstall();
    return;
  }
  updateInstallPending = true;
  try {
    updateSnapshot = await sidecar.downloadUpdate();
  } catch (err) {
    updateInstallPending = false;
    updateSnapshot = { ...(updateSnapshot ?? {}), state: "failed", failure: "error",
      error: errorText(err) };
  }
  renderUpdatePanel();
  scheduleUpdatePoll();
}

/** Read the updater's state once, kick off the one automatic check per
 *  launch, and then poll only for as long as something is happening.
 *
 *  The check is automatic; nothing else is. `auto_check` is a persisted
 *  setting and the sidecar itself is what honours the interval, so a page
 *  reloaded ten times does not make ten network requests. */
async function watchUpdates() {
  try {
    updateSnapshot = await sidecar.update();
  } catch {
    return; // the sidecar is not up yet
  }
  renderUpdatePanel();
  if (updateSnapshot.configured && updateSnapshot.auto_check !== false) {
    // force=false, so the sidecar's own interval decides whether this
    // actually opens the network.
    try {
      updateSnapshot = await sidecar.checkForUpdate(false);
      renderUpdatePanel();
    } catch { /* the panel keeps showing the last snapshot */ }
  }
  scheduleUpdatePoll();
}

// --------------------------------------------------------------------- models

async function refreshModels() {
  const previous = ui.model.value;
  clear(ui.model);
  ui.model.appendChild(el("option", { value: "auto", text: "auto (router picks per turn)" }));
  let installed = [];
  try {
    const body = await sidecar.models();
    installed = Array.isArray(body.installed) ? body.installed : [];
  } catch (err) {
    ui.model.appendChild(el("option", { value: "", text: "could not list models", disabled: true }));
    ui.sessionNote.className = "panel-note is-error";
    setText(ui.sessionNote, "Model list unavailable: " + errorText(err));
    // null, not 0: "the list could not be read" is a different thing from
    // "the list is empty", and only the second one means a first run.
    state.modelCount = null;
    return null;
  }
  // Every entry names the backend that runs it, and carries the exact "ref"
  // string POST /session expects. The value sent back is that ref, never a
  // display name: a bare name has to be guessed at, and guessing which
  // engine owns a model is what broke before. The label shows the backend so
  // a picker holding both kinds is readable rather than a mixed list.
  const entries = installed
    .filter((m) => m && (m.ref || m.name))
    .map((m) => ({
      value: m.ref || m.name,
      label: m.backend ? `${m.name} (${m.backend})` : m.name,
    }))
    .sort((a, b) => a.label.localeCompare(b.label));
  for (const e of entries) ui.model.appendChild(el("option", { value: e.value, text: e.label }));
  if (!entries.length) {
    ui.model.appendChild(el("option", { value: "", text: "no models available on either engine", disabled: true }));
    ui.sessionNote.className = "panel-note";
    setText(ui.sessionNote, "No model is installed yet. Open the model shop to download one.");
  }
  const values = entries.map((e) => e.value);
  if (previous && values.includes(previous)) ui.model.value = previous;
  else if (state.session?.model && values.includes(state.session.model)) ui.model.value = state.session.model;
  else if (values.length) ui.model.value = values[0];
  state.modelCount = entries.length;
  return entries.length;
}

// ---------------------------------------------------------------------- shop

let shopView = null;

/** The titlebar badge, so a download in flight is visible from the chat view
 *  too. Percent when one thing is downloading, a count when several are. */
function paintDownloadBadge(jobs) {
  const active = jobs.filter((j) => j.cancellable);
  if (!active.length) {
    ui.tabShopBadge.hidden = true;
    return;
  }
  ui.tabShopBadge.hidden = false;
  if (active.length === 1 && Number.isFinite(active[0].fraction)) {
    setText(ui.tabShopBadge, `${Math.round(active[0].fraction * 100)}%`);
  } else {
    setText(ui.tabShopBadge, String(active.length));
  }
}

function normalizePath(p) {
  return String(p ?? "").replace(/\\/g, "/").toLowerCase().split("/").filter(Boolean);
}

/** Do these two strings name the same GGUF?
 *
 * They cannot simply be compared. A download's path comes back from
 * hearth_hf, which builds it through hearth_contain.safe_join and therefore
 * RESOLVES it; GET /models' path comes from the bundled engine walking the
 * store root, which does not. On an ordinary install those are the same
 * string. They stop being the same string the moment anything between the
 * drive and the model store is a junction, a symlink, or a Windows
 * app-container redirect -- and pointing the store at another drive with a
 * junction is a normal thing to do when the models are tens of gigabytes.
 *
 * Only the PREFIX can differ that way, so the tail is the stable part.
 * Three segments is the store's own layout (<store>/<repo dir>/<file>, or
 * <repo dir>/<subdir>/<file> for the repositories that use folders), which
 * makes it both unique and equal on either side of any redirect. Exact
 * equality is still tried first, so nothing about the common case changes.
 */
function samePath(a, b) {
  if (!a || !b) return false;
  const left = normalizePath(a);
  const right = normalizePath(b);
  if (left.join("/") === right.join("/")) return true;
  const depth = Math.min(3, left.length, right.length);
  if (depth < 2) return false;
  return left.slice(-depth).join("/") === right.slice(-depth).join("/");
}

/** A download finished. The bundled engine enumerates the model store on
 *  every GET /models call, so the new file is selectable immediately; this
 *  only has to reload the picker, not restart anything. */
async function onModelReady(job) {
  await refreshModels();
  transcript.addNotice("ok", "Model downloaded.",
    `${job.label} from ${job.repo_id} is ready to use. It is in the model list now.`);
}

/** Select a just-downloaded model and hand the user back to the chat view.
 *  Matches on the GGUF path rather than on a display name: GET /models hands
 *  back a round-trippable "ref" per entry, and guessing which engine owns a
 *  model from its name is exactly what broke before. */
async function useDownloadedModel(job) {
  await refreshModels();
  let matched = null;
  for (const option of ui.model.options) {
    const value = option.value || "";
    if (value.startsWith("gguf:") && samePath(value.slice("gguf:".length), job.path)) {
      matched = value;
      break;
    }
  }
  setView("chat");
  if (!matched) {
    ui.sessionNote.className = "panel-note is-error";
    setText(ui.sessionNote,
      "The download finished, but the bundled engine did not list it. "
      + "Reload the model list, or check that llama-server is installed.");
    return;
  }
  ui.model.value = matched;
  ui.sessionNote.className = "panel-note";
  setText(ui.sessionNote, state.session
    ? "Model selected. Press Restart session to use it."
    : "Model selected. Choose a workspace and press Start session.");
  ui.connect.focus({ preventScroll: true });
}

// ---------------------------------------------------------------- checkpoints

async function refreshCheckpoints() {
  if (!state.session) {
    clear(ui.cpList);
    ui.cpNote.className = "panel-note";
    setText(ui.cpNote, "No session yet.");
    return;
  }
  // GET /checkpoints reads the workspace's shadow store under the same lock a
  // checkpoint or restore holds, so it never sees a half-written store. If one
  // holds it for longer than the sidecar will wait, the answer is a 503
  // checkpoint_store_busy: ask once more after a moment. Any other failure is
  // real and is reported straight away rather than retried into hiding.
  let list;
  for (let attempt = 0; ; attempt++) {
    try {
      const body = await sidecar.checkpoints();
      list = Array.isArray(body.checkpoints) ? body.checkpoints : [];
      break;
    } catch (err) {
      if (attempt === 0 && err?.status === 503) { await sleep(700); continue; }
      clear(ui.cpList);
      ui.cpNote.className = "panel-note is-error";
      setText(ui.cpNote, "Could not read checkpoint history: " + errorText(err));
      return;
    }
  }
  state.checkpoints = list;
  clear(ui.cpList);
  if (!list.length) {
    ui.cpNote.className = "panel-note";
    setText(ui.cpNote, "No checkpoints yet. One is taken automatically at the start of every turn.");
    return;
  }
  ui.cpNote.className = "panel-note";
  setText(ui.cpNote, `${list.length} checkpoint${list.length === 1 ? "" : "s"}, newest first.`);

  list.forEach((cp, index) => {
    const restore = el("button", { class: "btn btn-ghost btn-icon btn-sm", type: "button", title: "Restore this checkpoint" });
    restore.appendChild(icon("i-undo"));
    restore.addEventListener("click", () => confirmRestore(cp, index));
    ui.cpList.appendChild(appendAll(el("li", { class: "cp-item" }), [
      icon("i-clock"),
      appendAll(el("div", { class: "cp-main" }), [
        el("div", { class: "cp-label", text: cp.label || cp.id.slice(0, 12) }),
        el("div", { class: "cp-time", text: formatTime(cp.timestamp ?? cp.commit_time) }),
      ]),
      restore,
    ]));
  });
}

/** Show what a restore will do before doing it.
 *
 * The dialog states the operation (hearth_checkpoint.restore resets the
 * workspace's tracked content to this snapshot), says how many later
 * checkpoints it undoes, and shows the actual per-file diff from
 * GET /checkpoints/diff, which runs the same comparison restore makes and
 * stops before writing anything. The diff is drawn in restore's direction:
 * "-" lines are what is on disk now and will go, "+" lines are what the
 * checkpoint puts back. It also names any excluded secrets files (.env and
 * similar) that changed since the checkpoint, the one gap restore documents:
 * they were never captured, so they cannot be put back.
 *
 * The preview is fetched after the dialog opens and never gates it. Restore
 * stays clickable while it loads and when it fails, because the preview is an
 * aid to the decision, and the restore response still reports exactly what
 * changed in the transcript afterwards. While a turn is live in the workspace
 * the sidecar refuses the preview, as it refuses the restore itself, and the
 * dialog says to wait rather than calling it a failure.
 */
function confirmRestore(cp, index) {
  const when = formatTime(cp.timestamp ?? cp.commit_time);
  const preview = el("div", {}, [
    el("p", { class: "panel-note", text: "Working out what this restore would change..." }),
  ]);
  const body = [
    el("p", { text: `Restore the workspace to "${cp.label || cp.id.slice(0, 12)}"${when ? ` from ${when}` : ""}.` }),
    preview,
    el("p", { text: "Every tracked file in the workspace is reset to its contents at this checkpoint. Files created since then are removed. This is not itself undoable, though a fresh checkpoint is taken at the start of every turn." }),
    el("p", { text: index > 0
      ? `This undoes ${index} later checkpoint${index === 1 ? "" : "s"}.`
      : "This is the newest checkpoint, so it undoes only changes made since the last turn started." }),
    el("p", { text: "Files the checkpoint excluded as possible secrets (.env and similar) were never captured and cannot be restored. If any of them changed, the restore response will say so." }),
    el("p", { text: "Workspace:" }),
    blob(state.session?.workspace ?? "(unknown)"),
  ];
  openModal("Restore checkpoint", body, [
    { label: "Cancel", variant: "btn-ghost" },
    { label: "Restore", variant: "btn-danger", run: () => doRestore(cp) },
  ]);
  loadRestorePreview(cp, preview);
}

/** Fill `holder` with the restore preview, unless the dialog it lives in has
 *  closed (or been replaced) by the time the answer arrives. */
async function loadRestorePreview(cp, holder) {
  let result;
  try {
    result = await sidecar.request("GET", "/checkpoints/diff?" + new URLSearchParams({ id: cp.id }));
  } catch (err) {
    if (!holder.isConnected) return;
    clear(holder);
    holder.appendChild(el("p", {
      class: "panel-note is-error",
      text: err instanceof HttpError && err.status === 503
        ? "A checkpoint is being written right now, so the preview is not available. Reopen this in a moment to see it; Restore itself still works."
        : err instanceof HttpError && err.body?.workspace_busy
          ? "A turn is still working in this workspace, so there is nothing settled to preview yet. Restore waits for it too; reopen this once the turn has finished."
          : "Could not preview this restore (" + errorText(err) + "). Restore itself still works, and its result lists every file it changed.",
    }));
    return;
  }
  if (!holder.isConnected) return;
  clear(holder);
  holder.appendChild(el("p", {
    class: "panel-note",
    text: "What restoring changes: lines marked - are on disk now and will go, lines marked + come back from the checkpoint.",
  }));
  holder.appendChild(renderDiff(result, {
    emptyText: "No tracked file differs from this checkpoint, so restoring it would change nothing.",
  }));
  const excluded = Array.isArray(result?.excluded_changed) ? result.excluded_changed : [];
  if (excluded.length) {
    holder.appendChild(el("p", {
      class: "panel-note is-warn",
      text: "These files match the checkpoint's secret-exclusion patterns and changed since it was taken. They were never captured, so restore cannot put them back:",
    }));
    holder.appendChild(blob(excluded.map((e) => `${e.status}  ${e.path}`).join("\n")));
  }
  const skipped = Array.isArray(result?.skipped_gitlinks) ? result.skipped_gitlinks : [];
  if (skipped.length) {
    holder.appendChild(el("p", {
      class: "panel-note",
      text: "These are nested git repositories, which restore leaves alone:",
    }));
    holder.appendChild(blob(skipped.join("\n")));
  }
}

async function doRestore(cp) {
  let result;
  try {
    result = await sidecar.restore(cp.id);
  } catch (err) {
    transcript.addNotice("error", "Restore failed.", errorText(err));
    return;
  }
  const restored = Array.isArray(result.restored) ? result.restored : [];
  const skipped = Array.isArray(result.skipped_gitlinks) ? result.skipped_gitlinks : [];
  const excluded = Array.isArray(result.excluded_changed) ? result.excluded_changed : [];

  const detail = restored.length
    ? `${restored.length} file${restored.length === 1 ? "" : "s"} reverted.`
    : "No tracked file differed from the checkpoint.";
  const lines = restored.map((r) => `${r.status}  ${r.path}`).join("\n");
  transcript.addNotice("ok", `Restored to ${cp.label || cp.id.slice(0, 12)}.`, detail, lines || null);

  if (skipped.length) {
    transcript.addNotice("warn", "Nested git repositories were not touched.",
      "These paths are their own repositories, so the restore skipped them.", skipped.join("\n"));
  }
  if (excluded.length) {
    transcript.addNotice("warn", "Some excluded files changed and could not be put back.",
      "These matched the checkpoint's secret-exclusion patterns, so they were never captured.",
      excluded.map((e) => `${e.status}  ${e.path}`).join("\n"));
  }
  await refreshCheckpoints();
}

// ----------------------------------------------------------------- work loop

/** True when the live session runs a work loop rather than a chat. Read off
 *  GET /session's own answer (which app.py derives from the live engine
 *  object), never from what this page last asked for: a session restored
 *  after a restart, or replaced by another window, is still the truth. */
function isLoopSession() {
  return state.session ? state.session.engine === "loop" : ui.engine.value === "loop";
}

/** Show or hide the bounds form. The form is only meaningful before a run,
 *  so it is shown whenever the engine selector says "loop" -- including
 *  while a session is live, because a person reading the numbers back is
 *  exactly as important as one typing them in. */
function updateLoopPanel() {
  const wanted = ui.engine.value === "loop" || isLoopSession();
  ui.loopPanel.hidden = !wanted;
  loopConfigPanel.setMode(ui.mode.value);
}

function applyLoopSnapshot(snapshot) {
  state.loop = snapshot;
  loopConfigPanel.render(snapshot.defaults, snapshot.blind_spots);
  // A live loop session's OWN bounds win over the defaults the form was
  // built from. Otherwise a reloaded page (or a restarted sidecar) shows the
  // default ceilings beside a run that is bounded by different ones.
  loopConfigPanel.applyConfig(snapshot.config);
  updateLoopPanel();
  loopRunBar.render(isLoopSession() || snapshot.run || snapshot.pending ? snapshot : null);

  // The account is the product. Put it in the transcript exactly once per
  // run, driven by the gauge rather than by the terminal event, so it also
  // appears for a page that connected after the run had already ended.
  const run = snapshot.run;
  if (run && run.state === "stopped" && snapshot.report
      && state.accountShownFor !== run.run_id) {
    state.accountShownFor = run.run_id;
    transcript.clearPlaceholder();
    transcript.closeAgent();
    transcript.append(loopAccount(snapshot.report, snapshot.account,
                                  snapshot.blind_spots));
  }
  if (run && run.state !== "stopped") state.accountShownFor = null;
}

function stopLoopStream() {
  state.loopGeneration += 1;
  if (state.loopStream) {
    state.loopStream.abort();
    state.loopStream = null;
  }
}

/** Watch GET /loop/events. Runs for the life of the page, not the life of a
 *  run: an inherited unfinished run has to be visible before anything is
 *  started, and the account of the last run has to stay readable after. */
function startLoopStream() {
  const generation = ++state.loopGeneration;
  (async () => {
    let backoff = 400;
    while (generation === state.loopGeneration) {
      const controller = new AbortController();
      state.loopStream = controller;
      try {
        await sidecar.streamLoop({
          since: state.loop ? state.loop.version : 0,
          signal: controller.signal,
          onSnapshot: (snapshot) => {
            if (generation !== state.loopGeneration) return;
            backoff = 400;
            applyLoopSnapshot(snapshot);
          },
        });
        if (generation !== state.loopGeneration) return;
        await sleep(150);
      } catch (err) {
        if (controller.signal.aborted || generation !== state.loopGeneration) return;
        await sleep(backoff);
        backoff = Math.min(backoff * 2, 5000);
      }
    }
  })();
}

function isSwarmSession() {
  return state.session ? state.session.engine === "swarm" : ui.engine.value === "swarm";
}

/** Show or hide the swarm bounds form, on the same terms as the loop's: it is
 *  meaningful before a run AND while one is live, because reading the numbers
 *  back matters as much as typing them in. */
function updateSwarmPanel() {
  const wanted = ui.engine.value === "swarm" || isSwarmSession();
  ui.swarmPanel.hidden = !wanted;
  swarmConfigPanel.setMode(ui.mode.value);
}

function applySwarmSnapshot(snapshot) {
  state.swarm = snapshot;
  swarmConfigPanel.render(snapshot.defaults, snapshot.blind_spots);
  swarmConfigPanel.applyConfig(snapshot.config);
  updateSwarmPanel();
  swarmRunBar.render(
    isSwarmSession() || snapshot.run || snapshot.pending ? snapshot : null);

  // The account is the product. Put it in the transcript exactly once per
  // run, driven by the gauge rather than by the terminal event, so it also
  // appears for a page that connected after the relay had already ended.
  const run = snapshot.run;
  if (run && run.state === "stopped" && snapshot.report
      && state.swarmAccountShownFor !== run.run_id) {
    state.swarmAccountShownFor = run.run_id;
    transcript.clearPlaceholder();
    transcript.closeAgent();
    transcript.append(swarmAccount(snapshot.report, snapshot.account,
                                   snapshot.blind_spots, snapshot.loop_blind_spots));
  }
  if (run && run.state !== "stopped") state.swarmAccountShownFor = null;
}

function stopSwarmStream() {
  state.swarmGeneration += 1;
  if (state.swarmStream) {
    state.swarmStream.abort();
    state.swarmStream = null;
  }
}

/** Watch GET /swarm/events, for the life of the page. Same reasoning as
 *  startLoopStream: an inherited unfinished relay has to be visible before
 *  anything is started, and the last account has to stay readable after. */
function startSwarmStream() {
  const generation = ++state.swarmGeneration;
  (async () => {
    let backoff = 400;
    while (generation === state.swarmGeneration) {
      const controller = new AbortController();
      state.swarmStream = controller;
      try {
        await sidecar.streamSwarm({
          since: state.swarm ? state.swarm.version : 0,
          signal: controller.signal,
          onSnapshot: (snapshot) => {
            if (generation !== state.swarmGeneration) return;
            backoff = 400;
            applySwarmSnapshot(snapshot);
          },
        });
        if (generation !== state.swarmGeneration) return;
        await sleep(150);
      } catch (err) {
        if (controller.signal.aborted || generation !== state.swarmGeneration) return;
        await sleep(backoff);
        backoff = Math.min(backoff * 2, 5000);
      }
    }
  })();
}

// ------------------------------------------------------------------- session

async function loadSession({ quiet = false } = {}) {
  try {
    const session = await sidecar.getSession();
    applySession(session);
    return session;
  } catch (err) {
    if (err instanceof HttpError && err.status === 404) {
      state.session = null;
      if (!quiet) {
        transcript.showPlaceholder("No session yet",
          "Choose a workspace folder and a model in the sidebar, then start a session.");
      }
      setComposerEnabled(false, "Start a session to begin.");
      return null;
    }
    throw err;
  }
}

function applySession(session) {
  const isNew = !state.session || state.session.workspace !== session.workspace;
  state.session = session;
  state.running = session.status === "running";

  setChip(ui.chipWorkspace, session.workspace, true);
  setChip(ui.chipModel, session.model || "auto");
  setChip(ui.chipMode, session.mode);

  if (!ui.workspace.value) ui.workspace.value = session.workspace;
  if (session.mode) ui.mode.value = session.mode;
  if (session.engine) ui.engine.value = session.engine;
  updateLoopPanel();

  setText(ui.connect, "Restart session");
  ui.sessionNote.className = "panel-note";
  setText(ui.sessionNote,
    "Restarting starts a new chat with these settings. The current one stays under Chats.");

  updateTurnUi();
  if (isNew) refreshCheckpoints();
}

// The Chats sidebar (history.js). Built in boot(), once the sidecar answers.
let historyPanel = null;

/** Make `session` the one this page shows: a session just started from the
 *  form, a new chat, or a saved conversation reopened from Chats. Every one
 *  of those is a different session with its own event log, so the old stream
 *  is torn down, the transcript cleared, and the new log replayed from its
 *  first event. That replay is the whole transcript of a reopened chat. */
function adoptSession(session, placeholderTitle, placeholderBody) {
  stopEventStream();
  state.lastEventId = 0;
  state.accountShownFor = null;
  state.swarmAccountShownFor = null;
  localEchoes.length = 0;
  transcript.reset();
  transcript.showPlaceholder(placeholderTitle, placeholderBody);
  // The form follows the open chat, so "Restart session" and the next "New
  // chat" describe the session on screen rather than the one before it.
  ui.workspace.value = session.workspace;
  if ([...ui.model.options].some((option) => option.value === session.model)) {
    ui.model.value = session.model;
  }
  // Treated as new even in the same workspace, so applySession re-reads the
  // checkpoint list for it.
  state.session = null;
  applySession(session);
  startEventStream();
}

/** The open conversation was deleted, which ended the session with it. */
function clearSession() {
  stopEventStream();
  state.session = null;
  state.running = false;
  state.lastEventId = 0;
  localEchoes.length = 0;
  transcript.showPlaceholder("No chat open",
    "Start a new chat, or open one from Chats. The chat you deleted is gone; "
    + "the files in its workspace were not touched.");
  setChip(ui.chipWorkspace, "no workspace", false);
  setChip(ui.chipModel, "no model");
  setText(ui.connect, "Start session");
  ui.sessionNote.className = "panel-note";
  setText(ui.sessionNote, "");
  updateTurnUi();
  refreshCheckpoints();
}

async function startSession() {
  const workspace = ui.workspace.value.trim();
  const model = ui.model.value;
  const mode = ui.mode.value;
  const engine = ui.engine.value;

  // A loop has nobody awake to answer its approval cards, so 'edit' (which
  // gates every single write) would deny its own first write and grind to a
  // stall by turn four. Say so here, where it can still be changed, rather
  // than letting the sidecar refuse the session with the same sentence.
  if (engine === "loop" && !["auto", "plan"].includes(mode)) {
    ui.sessionNote.className = "panel-note is-error";
    setText(ui.sessionNote,
      "A work loop needs 'auto' (it may read and write unattended; anything "
      + "dangerous is gated) or 'plan' (read only). 'edit' gates every write "
      + "and nobody is awake to approve them.");
    ui.mode.focus();
    return;
  }
  if (engine === "swarm" && !["auto", "plan"].includes(mode)) {
    ui.sessionNote.className = "panel-note is-error";
    setText(ui.sessionNote,
      "A swarm needs 'auto' (the implementer may read and write unattended; "
      + "anything dangerous is gated) or 'plan' (every role read-only). "
      + "'edit' gates every write and nobody is awake to approve them.");
    ui.mode.focus();
    return;
  }

  if (!workspace) {
    ui.sessionNote.className = "panel-note is-error";
    setText(ui.sessionNote, "A workspace path is required.");
    ui.workspace.focus();
    return;
  }
  if (!model) {
    ui.sessionNote.className = "panel-note is-error";
    setText(ui.sessionNote, "Pick a model. If the list is empty, download one from the model shop.");
    return;
  }

  ui.connect.disabled = true;
  // POST /session is refused with 409 if the workspace it is replacing is
  // still busy, so the composer must not be able to start a turn on the
  // outgoing session while the new one is being created.
  setComposerEnabled(false, "Starting session...");
  ui.sessionNote.className = "panel-note";
  setText(ui.sessionNote, "Starting...");
  try {
    stopEventStream();
    const body = { workspace, model, mode, engine };
    // Read off the live form at the moment the button is pressed, so what is
    // sent is exactly what the operator has on screen.
    if (engine === "loop") body.loop = loopConfigPanel.read();
    if (engine === "swarm") body.swarm = swarmConfigPanel.read();
    const session = await sidecar.createSession(body);
    // A new session is a new conversation; the one it replaced stays in Chats.
    adoptSession(session,
      session.engine === "loop" ? "Work loop ready" : "Session ready",
      session.engine === "loop"
        ? `Give it one goal. It will keep working until it is done, hits a `
          + `ceiling, stops making progress, or you stop it. ${session.mode} mode `
          + `in ${session.workspace}.`
        : `${session.mode} mode in ${session.workspace}`);
    rememberWorkspace(session.workspace);
    historyPanel?.refresh();
  } catch (err) {
    ui.sessionNote.className = "panel-note is-error";
    setText(ui.sessionNote, errorText(err));
    updateTurnUi();
  } finally {
    ui.connect.disabled = false;
  }
}

// ------------------------------------------------------------------ composer

function setComposerEnabled(enabled, statusText) {
  ui.composer.disabled = !enabled;
  ui.send.disabled = !enabled || !ui.composer.value.trim();
  if (statusText !== undefined) setText(ui.composerStatus, statusText);
}

function updateTurnUi() {
  const hasSession = Boolean(state.session);
  ui.stop.hidden = !state.running;
  if (!hasSession) {
    setComposerEnabled(false, "Start a session to begin.");
    setConn(state.backendHealthy ? "ok" : "warn", state.backendHealthy ? "ready" : "backend not ready");
    return;
  }
  const loop = isLoopSession();
  if (state.running) {
    setComposerEnabled(false, loop
      ? "The work loop is running. Press Esc or Stop to end it."
      : (modelLoadingHint() || "Working. Press Esc or the stop button to interrupt."));
    setConn("busy", loop ? "work loop running" : "running");
  } else {
    const pending = state.loop && state.loop.pending;
    setComposerEnabled(true, loop
      ? (pending && pending.resumable
          ? "Send a goal to start a run, or \"resume\" to continue the inherited one."
          : "Send one goal. It will work until it is done, bounded, stalled, or stopped.")
      : "Ready.");
    setConn("ok", "connected");
  }
}

function autosize() {
  ui.composer.style.height = "auto";
  ui.composer.style.height = Math.min(ui.composer.scrollHeight, 220) + "px";
}

/* Prompts this page has already drawn, waiting for the sidecar's own
 * `user_prompt` echo of them. POST /prompt records every prompt in the event
 * log so a replay (a reload, a restart, a reopened chat) shows both sides of
 * the conversation; the page that sent it has drawn it already, so the echo
 * of its own prompt is skipped exactly once. */
const localEchoes = [];

function takeLocalEcho(data) {
  const text = typeof data.text === "string" ? data.text : "";
  const i = localEchoes.findIndex((sent) => sent === text
    || (data.truncated && sent.startsWith(text)));
  if (i === -1) return false;
  localEchoes.splice(i, 1);
  return true;
}

async function send() {
  const message = ui.composer.value.trim();
  if (!message || !state.session || state.running) return;
  const attached = takeAttachments();
  ui.composer.value = "";
  autosize();
  transcript.addUser(message);
  localEchoes.push(message);
  state.running = true;
  updateTurnUi();
  if (attached.length) transcript.addUserAttachments(attached);
  try {
    await sidecar.prompt(message, attached.map((a) => a.path));
  } catch (err) {
    const i = localEchoes.indexOf(message);
    if (i !== -1) localEchoes.splice(i, 1);
    state.running = false;
    updateTurnUi();
    // Give the words back with the files, so a refusal (files that no
    // longer fit the context, a 413) costs nothing to retry and the tray's
    // hint can say what to change rather than asking for a message.
    if (!ui.composer.value.trim()) {
      ui.composer.value = message;
      autosize();
    }
    returnAttachments(attached);
    transcript.addNotice("error", "Could not submit that prompt.", errorText(err));
  }
}

async function cancel() {
  if (!state.session || !state.running) return;
  ui.stop.disabled = true;
  try {
    const result = await sidecar.cancel();
    if (result && result.cancelled === false) {
      transcript.addNotice("quiet", "Nothing to cancel.", "The turn had already finished.");
      state.running = false;
      updateTurnUi();
    }
  } catch (err) {
    transcript.addNotice("error", "Cancel failed.", errorText(err));
  } finally {
    ui.stop.disabled = false;
  }
}

async function decideApproval(id, decision) {
  const entry = transcript.approvals.get(id);
  if (entry) { entry.allowBtn.disabled = true; entry.denyBtn.disabled = true; }
  try {
    await sidecar.approve(id, decision);
    transcript.resolveApproval(id, decision);
  } catch (err) {
    if (entry) { entry.allowBtn.disabled = false; entry.denyBtn.disabled = false; }
    transcript.addNotice("error", "Could not record that decision.", errorText(err));
  }
}

// -------------------------------------------------------------- event stream

function stopEventStream() {
  state.streamGeneration += 1;
  if (state.streamAbort) {
    state.streamAbort.abort();
    state.streamAbort = null;
  }
}

function startEventStream() {
  const generation = ++state.streamGeneration;
  (async () => {
    let backoff = 400;
    while (generation === state.streamGeneration) {
      const controller = new AbortController();
      state.streamAbort = controller;
      try {
        await sidecar.streamEvents({
          since: state.lastEventId,
          signal: controller.signal,
          onOpen: () => { backoff = 400; if (!state.running) updateTurnUi(); },
          onEvent: (event) => {
            if (generation !== state.streamGeneration) return;
            if (Number.isFinite(event.id)) state.lastEventId = Math.max(state.lastEventId, event.id);
            handleEvent(event);
          },
        });
        // The sidecar sends Connection: close on GET /events, so a clean end of
        // stream is normal; reconnect promptly and resume from lastEventId.
        if (generation !== state.streamGeneration) return;
        await sleep(150);
      } catch (err) {
        if (controller.signal.aborted || generation !== state.streamGeneration) return;
        if (err instanceof HttpError && err.status === 404) {
          // No session on the sidecar any more.
          state.session = null;
          updateTurnUi();
          return;
        }
        setConn("down", "reconnecting");
        await sleep(backoff);
        backoff = Math.min(backoff * 2, 5000);
      }
    }
  })();
}

function handleEvent(event) {
  const data = event.data || {};
  switch (event.kind) {
    // The user's own prompt, recorded by the sidecar. Drawn on replay; the
    // live copy this page drew in send() is not drawn twice.
    case "user_prompt":
      // A first prompt is what names a chat in the sidebar.
      historyPanel?.refreshSoon();
      if (takeLocalEcho(data)) break;
      transcript.addUser(data.truncated
        ? `${data.text || ""}\n\n(shortened: the full prompt was sent to the model)`
        : data.text || "");
      // The files sent with it, as the chips send() drew. A saved chat is a
      // file the agent's own commands can rewrite, so this is untrusted
      // too: only strings go through, and addUserAttachments renders them
      // as text.
      if (Array.isArray(data.attachments)) {
        transcript.addUserAttachments(data.attachments
          .filter((f) => f && typeof f === "object")
          .map((f) => ({
            name: typeof f.name === "string" ? f.name : "attachment",
            path: typeof f.path === "string" ? f.path : "",
            plan: typeof f.inlined === "string" ? f.inlined : "none",
          })));
      }
      break;

    // A delta is a fragment of assistant text, emitted by engine.py as
    // tokens arrive (coalesced on a short window, see its module docstring's
    // point 7). stream_id names which assistant message it belongs to and
    // index is its offset within that message; both are forwarded so a
    // stream resumed after a reconnect is rendered without duplicating or
    // losing text. A sidecar that predates them simply sends neither, and
    // the transcript falls back to plain append.
    case "delta":
      transcript.appendAgent(data.text || "", {
        streamId: data.stream_id,
        index: data.index,
      });
      break;

    case "tool_call":
      transcript.addToolCall(data);
      break;

    case "tool_result":
      transcript.addToolResult(data);
      break;

    case "approval_request":
      state.running = true;
      updateTurnUi();
      transcript.addApproval(data, decideApproval);
      break;

    case "approval_abandoned":
      transcript.resolveAllPending("deny", "Abandoned when the sidecar restarted.");
      transcript.addNotice("warn", "An approval was abandoned.", data.reason || "");
      break;

    case "turn_interrupted":
      state.running = false;
      updateTurnUi();
      transcript.addNotice("warn", "A turn was interrupted.", data.reason || "");
      break;

    case "checkpoint":
      transcript.addNotice("quiet", "Checkpoint taken.",
        `${data.label || data.id} · ${data.file_count ?? "?"} files`);
      if (data.warning) transcript.addNotice("warn", "Checkpoint warning.", data.warning);
      refreshCheckpoints();
      break;

    case "checkpoint_error":
      transcript.addNotice("warn", "Checkpoint failed.",
        (data.message || "") + " The turn continues, but undo will not cover it.");
      break;

    case "model_selected": {
      const bits = [data.model];
      if (data.tier) bits.push(`tier ${data.tier}`);
      if (data.escalated) bits.push("escalated");
      if (data.hardware_limited) bits.push("hardware limited");
      transcript.addNotice("quiet", "Model: " + bits.join(" · "), data.reason || "");
      if (data.model) setChip(ui.chipModel, data.model);
      break;
    }

    case "secrets_finding": {
      const f = data.finding || {};
      transcript.addNotice("warn",
        `Possible credential written by ${data.tool}.`,
        [f.kind, f.masked, f.reason].filter(Boolean).join(" · "),
        f.context || null);
      break;
    }

    case "events_dropped":
      // `restored` marks the front of a saved conversation's history: only
      // its most recent part is kept on disk (session_state.persisted_tail),
      // and a chat that starts mid-way must say so rather than pass for whole.
      // `gap` marks a hole in the middle of one instead: a stretch between
      // two saves that outran the live event buffer (session_state.merge_tail).
      if (data.restored && data.gap) {
        transcript.addNotice("quiet", "Part of this chat was not saved.",
          "A long stretch of activity happened between two saves and only its end "
          + "was kept. The model's own context was saved separately and is not affected.");
        break;
      }
      if (data.restored) {
        transcript.addNotice("quiet", "Earlier messages are not shown.",
          "Only the most recent part of a saved conversation's activity is kept. "
          + "The model's own context was saved separately and is not affected.");
        break;
      }
      transcript.addNotice("quiet", "Some earlier events were dropped.",
        "The session's event buffer wrapped while this window was disconnected.");
      break;

    // ---- work loop -------------------------------------------------------
    // The transcript carries the loop's NARRATIVE. Its running totals live on
    // GET /loop instead (see loop.js), so nothing here tries to keep a
    // counter: a number folded out of a log that drops its oldest 500 entries
    // would quietly go wrong on a long run.
    case "loop_start":
      transcript.addNotice("quiet", "Work loop started.",
        `${data.mode} mode · ${(data.allowed_tools || []).length} tools available`);
      break;

    case "turn_start":
      transcript.closeAgent();
      transcript.addNotice("quiet", `Turn ${data.turn}`, null);
      break;

    case "progress": {
      const spend = data.spend || {};
      transcript.addNotice(data.new_state ? "quiet" : "warn",
        data.new_state ? "The workspace changed." : "Nothing new changed.",
        [`turn ${data.turn}`,
         `${data.changed ?? 0} file(s)`,
         data.errors ? `${data.errors} error(s)` : null,
         spend.writes !== undefined ? `${spend.writes} unattended write(s) so far` : null,
        ].filter(Boolean).join(" · "));
      break;
    }

    case "notice":
      transcript.addNotice("warn", "Work loop note.", data.detail || "");
      break;

    case "loop_resuming":
      transcript.addNotice("warn", "Resuming the run that was interrupted.",
        `${data.completed_turns ?? 0} turn(s) already completed. Turn `
        + `${data.interrupted_turn} was interrupted and will NOT be resumed: there `
        + "is no way to know whether its last tool call reached the workspace.");
      break;

    case "loop_abandoned":
      transcript.addNotice("warn", "An unfinished run was left alone.",
        data.reason || "");
      break;

    case "approval_timeout":
      transcript.resolveApproval(data.id, "deny", "Nobody answered in time.");
      transcript.addNotice("warn", `Nobody approved ${data.tool}.`,
        data.reason || "It was refused and the run continued.");
      break;

    case "loop_report":
      // The account itself is rendered from GET /loop, so it appears for a
      // page that connected after the run ended too. Nothing to draw here.
      refreshCheckpoints();
      break;

    case "loop_stop":
      break;

    // ---- agent swarm -----------------------------------------------------
    // Same division as the loop: the transcript carries the NARRATIVE and
    // GET /swarm carries the totals. What is extra here is that every entry
    // names the role, because "which role did what" is the whole thing a
    // relay has to be able to explain.
    case "swarm_start":
      transcript.addNotice("quiet", "Agent swarm started.",
        `${data.mode} mode · ${(data.roles || []).map((r) => r.name).join(" → ")}`
        + " · one budget shared by every role");
      break;

    case "phase_start":
      transcript.closeAgent();
      transcript.addNotice("quiet", `${data.role} is working.`,
        [`cycle ${data.cycle}`,
         data.writes ? "may change files" : "read-only",
         `on ${data.model}`,
        ].filter(Boolean).join(" · "));
      break;

    case "phase_end":
      transcript.addNotice(
        data.bound_by === "global" ? "warn" : "quiet",
        `${data.role} handed off.`,
        [`ended: ${data.stop_reason || "unknown"}`,
         data.bound_by === "role" ? "used its own turn budget" : null,
         data.bound_by === "global" ? "hit the SHARED budget" : null,
        ].filter(Boolean).join(" · "));
      break;

    case "swarm_swap":
      transcript.addNotice("quiet", `Loaded ${data.model} for the ${data.role}.`,
        `${data.seconds}s of wall clock spent swapping models.`);
      break;

    case "lease_refused":
      // The write lease firing is worth saying out loud: it means a read-only
      // role tried to change a file and was stopped.
      transcript.addNotice("warn",
        `The ${data.role} tried to use ${data.tool} and was refused.`,
        "Only the implementer holds the write lease. Nothing was changed.");
      break;

    case "swarm_resuming":
      transcript.addNotice("warn", "Resuming the relay that was interrupted.",
        `${data.completed_phases ?? 0} phase(s) already completed. Phase `
        + `${data.interrupted_phase} was interrupted and will NOT be resumed: there `
        + "is no way to know whether its last tool call reached the workspace.");
      break;

    case "swarm_abandoned":
      transcript.addNotice("warn", "An unfinished relay was left alone.",
        data.reason || "");
      break;

    case "swarm_report":
      // The account is rendered from GET /swarm, so it appears for a page
      // that connected after the relay ended too. Nothing to draw here.
      refreshCheckpoints();
      break;

    case "swarm_stop":
      break;

    case "done":
      transcript.closeAgent();
      state.running = false;
      updateTurnUi();
      transcript.resolveAllPending("deny", "The turn ended before this was answered.");
      if (data.tokens_in || data.tokens_out) {
        transcript.addNotice("quiet", "Turn complete.",
          `${data.tokens_in ?? 0} in · ${data.tokens_out ?? 0} out`);
      }
      refreshCheckpoints();
      break;

    case "cancelled":
      transcript.closeAgent();
      state.running = false;
      updateTurnUi();
      transcript.resolveAllPending("deny", "The turn was cancelled.");
      // Never claim a clean stop. Cancellation abandons an in-flight tool
      // call; it cannot kill one. When the sidecar tells us how many are
      // still live, say the number rather than the vague warning.
      transcript.addNotice("warn", "Stopped.",
        data.live_workers > 0
          ? `${data.live_workers} tool call(s) are still running against this `
            + "workspace and cannot be killed. Watch the counter above."
          : "A tool call already in flight may still finish in the background.");
      break;

    case "error":
      transcript.closeAgent();
      state.running = false;
      updateTurnUi();
      transcript.resolveAllPending("deny", "The turn ended with an error.");
      transcript.addNotice("error", data.message || "The turn failed.", null, data.remedy || null);
      if (data.setup_status) refreshSetup();
      break;

    default:
      transcript.addNotice("quiet", event.kind, JSON.stringify(data));
  }
}

// ------------------------------------------------------------- folder picker

async function browseForFolder() {
  ui.browse.disabled = true;
  try {
    const body = await pickFolder();
    if (body.path) {
      ui.workspace.value = body.path;
      ui.sessionNote.className = "panel-note";
      setText(ui.sessionNote, "");
    } else if (body.error) {
      ui.sessionNote.className = "panel-note";
      setText(ui.sessionNote, body.error + " Type the path instead.");
    }
  } catch {
    ui.sessionNote.className = "panel-note";
    setText(ui.sessionNote, "No folder picker available here. Type the path instead.");
  } finally {
    ui.browse.disabled = false;
  }
}

// ------------------------------------------------------------------ bindings

ui.tabChat.addEventListener("click", () => setView("chat"));
ui.tabShop.addEventListener("click", () => setView("shop"));
$("#tab-tools").addEventListener("click", () => setView("tools"));
ui.connect.addEventListener("click", startSession);
ui.browse.addEventListener("click", browseForFolder);
ui.reloadModels.addEventListener("click", refreshModels);
ui.reloadSetup.addEventListener("click", refreshSetup);
ui.reloadCheckpoints.addEventListener("click", refreshCheckpoints);
ui.send.addEventListener("click", send);
ui.stop.addEventListener("click", cancel);
initAttachments({ sidecar, getSession: () => state.session, isRunning: () => state.running });

// The bounds form appears the moment "work loop" is chosen, not after a
// session exists: a person deciding whether to run one unattended needs to
// see what would bound it while they are still deciding.
ui.engine.addEventListener("change", () => {
  if (ui.engine.value === "swarm" && ui.mode.value === "edit") {
    // Same rescue as the loop's below, and for the same reason: 'edit' gates
    // every write and a relay has nobody to answer those gates.
    ui.mode.value = "auto";
    ui.sessionNote.className = "panel-note";
    setText(ui.sessionNote,
      "Switched to 'auto': a swarm cannot use 'edit', which gates every write "
      + "with nobody awake to approve them. Check what each role allows below "
      + "before starting.");
  }
  if (ui.engine.value === "loop" && ui.mode.value === "edit") {
    // 'edit' gates every write and a loop has nobody to answer those gates.
    // Move to the mode that actually works, visibly, rather than failing at
    // the moment the button is pressed. BEFORE the repaint below, so the tool
    // list describes the mode the session will actually use -- painting it
    // from the old mode would show every tool as unavailable, which is both
    // wrong and exactly the kind of quiet inaccuracy this panel exists to
    // avoid.
    ui.mode.value = "auto";
    ui.sessionNote.className = "panel-note";
    setText(ui.sessionNote,
      "Switched to 'auto': a work loop cannot use 'edit', which gates every "
      + "write with nobody awake to approve them. Check what 'auto' allows "
      + "below before starting.");
  }
  updateLoopPanel();
  updateSwarmPanel();
  updateTurnUi();
});
ui.mode.addEventListener("change", () => { updateLoopPanel(); updateSwarmPanel(); });

ui.composer.addEventListener("input", () => {
  autosize();
  ui.send.disabled = ui.composer.disabled || !ui.composer.value.trim();
});

ui.composer.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    send();
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (modalDismiss) { modalDismiss(); return; }
  if (state.running) { event.preventDefault(); cancel(); }
});

// ----------------------------------------------------------------- bootstrap

async function boot() {
  paintRecents();
  transcript.showPlaceholder("Connecting", "Asking the shell where the Hearth service is.");
  setConn("busy", "connecting");

  try {
    state.handshake = await readHandshake();
    sidecar.setOrigin(state.handshake.origin);
    sidecar.setToken(state.handshake.token);
  } catch (err) {
    setConn("down", "no handshake");
    transcript.showPlaceholder("Cannot reach the Hearth service",
      hasShellBridge()
        ? "The shell started but did not hand over a handshake. Restarting Hearth should fix this."
        : "The dev host did not return a handshake. Start it with: node desktop/ui/dev-host.mjs");
    setText(ui.setupBody, "");
    ui.setupBody.appendChild(el("p", { class: "panel-note is-error", text: errorText(err) }));
    return;
  }

  try {
    await sidecar.health();
  } catch (err) {
    setConn("down", "sidecar down");
    transcript.showPlaceholder("The sidecar is not answering", errorText(err));
    return;
  }

  ui.workspace.value = state.handshake.default_workspace || readRecents()[0] || "";

  // The shop is built once and lives for the page. Its download stream starts
  // immediately, before any session exists, because the first thing a user
  // with no model at all needs is a download -- which is exactly why
  // downloads have their own stream rather than riding on GET /events.
  shopView = new ShopView(ui.shopView, {
    sidecar,
    onModelReady,
    onUseModel: useDownloadedModel,
    onDownloadsChanged: paintDownloadBadge,
  });
  shopView.startDownloadStream();

  // The Tools screen (MCP servers). Built now so its tab works from the first
  // click; it reads nothing until it is shown, and never starts a server.
  mcpPanel = new McpPanel($("#tools"), {
    sidecar, openModal, closeModal, isRunning: () => state.running,
  });

  // Not awaited: the GPU engine fetch runs for as long as it runs, and the
  // whole point is that nothing waits for it. watchEngine paints the panel
  // from the first snapshot and keeps repainting it from the stream.
  watchEngine();

  // Likewise the updater. Nothing here blocks a session, and nothing here
  // downloads or installs anything on its own: the automatic part is one
  // signed-JSON GET, and only if the user has left that on.
  watchUpdates();

  // The titlebar model chip; it polls GET /model and never opens a stream.
  startModelChip({ sidecar, isRunning: () => state.running, onChange: updateTurnUi });

  // The work loop gauge, likewise started before any session exists. Two
  // things depend on that: an unfinished run inherited from a restart has to
  // be on screen BEFORE the user types anything (starting something else is
  // what forfeits the chance to resume it), and the bounds form is built from
  // this snapshot's `defaults`, which a person needs while deciding whether
  // to run one at all.
  try {
    applyLoopSnapshot(await sidecar.loop());
  } catch (err) {
    void err;  // the stream below retries; a first-read failure is not fatal
  }
  startLoopStream();

  // The swarm gauge, on the same terms and for the same reasons: an inherited
  // unfinished relay must be on screen before the user types anything, since
  // starting something else is what forfeits the chance to resume it.
  try {
    applySwarmSnapshot(await sidecar.swarm());
  } catch (err) {
    void err;
  }
  startSwarmStream();

  await Promise.all([refreshSetup(), refreshModels()]);

  const session = await loadSession().catch((err) => {
    transcript.addNotice("error", "Could not read the session.", errorText(err));
    return null;
  });

  if (session) {
    transcript.showPlaceholder("Session restored",
      `${session.mode} mode in ${session.workspace}. Earlier events replay below.`);
    startEventStream();
  } else if (state.modelCount === 0) {
    // A brand new install: the engine is bundled but no model is, because a
    // model is gigabytes and which one to fetch depends on the machine. So
    // the first screen is not an empty chat with a dead dropdown; it is the
    // three things that have to happen, and the button that starts them.
    transcript.showFirstRun(
      "Welcome to Hearth",
      "Everything Hearth needs to run a model is already installed. The one "
      + "thing missing is a model, because they are several gigabytes each and "
      + "which one fits depends on your machine.",
      [
        "Open the model shop and search Hugging Face.",
        "Download a model. Hearth suggests sizes that fit your hardware.",
        "Come back here, pick a folder to work in, and start chatting.",
      ],
      { label: "Open the model shop", onClick: () => setView("shop") },
    );
  }

  // Saved conversations, in a sidebar on the Chat tab. Built after the
  // session is read, so its first paint already knows which one is open.
  historyPanel = new HistoryPanel($("#history"), {
    sidecar,
    openModal,
    closeModal,
    isRunning: () => state.running,
    hasSession: () => Boolean(state.session),
    onSwitched: adoptSession,
    onCleared: clearSession,
    startFromForm: startSession,
  });

  // A light poll keeps `status` honest even if an event is missed: the sidecar
  // is the authority on whether a turn is running, not this page's bookkeeping.
  setInterval(async () => {
    if (!sidecar.authenticated) return;
    try {
      const current = await sidecar.getSession();
      const wasRunning = state.running;
      state.session = current;
      state.running = current.status === "running";
      if (wasRunning !== state.running) updateTurnUi();
    } catch { /* transient; the stream loop reports real outages */ }
  }, 4000);
}

boot();
