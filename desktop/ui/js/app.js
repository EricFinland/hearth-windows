/* hearth desktop UI controller.
 *
 * Wires the sidecar (desktop/server) to the transcript and the side panels.
 * Plain ES modules, no build step, no dependencies, no network fetches beyond
 * the sidecar itself.
 */

import { Sidecar, HttpError, readHandshake, pickFolder, hasShellBridge } from "./api.js";
import { Transcript } from "./transcript.js";
import { el, icon, appendAll, clear, $ } from "./dom.js";
import { blob } from "./safe-text.js";
import { ShopView } from "./shop.js";

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
  connect: $("#btn-connect"),
  sessionNote: $("#session-note"),
  setupBody: $("#setup-body"),
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
};

// ---------------------------------------------------------------------- views

/** Chat and the shop are two panes over one sidebar, not two pages: the
 *  download stream, the session and the event stream all belong to the page,
 *  so switching views must never tear any of them down. That is also what
 *  makes "downloads survive navigating between chat and shop" true by
 *  construction rather than by bookkeeping. */
function setView(name) {
  state.view = name;
  const shop = name === "shop";
  ui.chatView.hidden = shop;
  ui.shopView.hidden = !shop;
  ui.tabChat.classList.toggle("is-active", !shop);
  ui.tabShop.classList.toggle("is-active", shop);
  ui.tabChat.setAttribute("aria-pressed", String(!shop));
  ui.tabShop.setAttribute("aria-pressed", String(shop));
  if (shop) shopView?.focus();
}

// ---------------------------------------------------------------- connection

function setConn(kind, label) {
  ui.conn.dataset.state = kind;
  ui.connLabel.textContent = label;
}

function setChip(node, value, mono) {
  node.querySelector(".chip-text").textContent = value;
  node.classList.toggle("mono", Boolean(mono));
  node.title = value;
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
  ui.modalTitle.textContent = title;
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
    ui.setupBody.appendChild(el("p", {
      class: "setup-message",
      text: `Ollama is reachable at ${diagnosis.base_url ?? "the configured URL"}.`,
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
    ui.sessionNote.textContent = "Model list unavailable: " + errorText(err);
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
    ui.sessionNote.textContent = "No model is installed yet. Open the model shop to download one.";
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
    ui.tabShopBadge.textContent = `${Math.round(active[0].fraction * 100)}%`;
  } else {
    ui.tabShopBadge.textContent = String(active.length);
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
    ui.sessionNote.textContent =
      "The download finished, but the bundled engine did not list it. Reload the model list, or check that llama-server is installed.";
    return;
  }
  ui.model.value = matched;
  ui.sessionNote.className = "panel-note";
  ui.sessionNote.textContent = state.session
    ? "Model selected. Press Restart session to use it."
    : "Model selected. Choose a workspace and press Start session.";
  ui.connect.focus({ preventScroll: true });
}

// ---------------------------------------------------------------- checkpoints

async function refreshCheckpoints() {
  if (!state.session) {
    clear(ui.cpList);
    ui.cpNote.className = "panel-note";
    ui.cpNote.textContent = "No session yet.";
    return;
  }
  // GET /checkpoints runs `git log` against the workspace's shadow store, and
  // this is refreshed the moment a `checkpoint` event arrives, which is exactly
  // when that store is being written. Losing that race is a transient 500, not
  // a broken history, so retry once before reporting anything.
  let list;
  for (let attempt = 0; ; attempt++) {
    try {
      const body = await sidecar.checkpoints();
      list = Array.isArray(body.checkpoints) ? body.checkpoints : [];
      break;
    } catch (err) {
      if (attempt === 0) { await sleep(700); continue; }
      clear(ui.cpList);
      ui.cpNote.className = "panel-note is-error";
      ui.cpNote.textContent = "Could not read checkpoint history: " + errorText(err);
      return;
    }
  }
  state.checkpoints = list;
  clear(ui.cpList);
  if (!list.length) {
    ui.cpNote.className = "panel-note";
    ui.cpNote.textContent = "No checkpoints yet. One is taken automatically at the start of every turn.";
    return;
  }
  ui.cpNote.className = "panel-note";
  ui.cpNote.textContent = `${list.length} checkpoint${list.length === 1 ? "" : "s"}, newest first.`;

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
 * The sidecar exposes no route that previews a checkpoint diff, so this does
 * not invent a per-file list it cannot know. It states the operation exactly
 * (hearth_checkpoint.restore resets the workspace's tracked content to this
 * snapshot), says how many later checkpoints it undoes, and warns about the
 * one gap that module documents: files matching its secret-exclusion patterns
 * were never captured, so they cannot be put back. The per-file list of what
 * actually changed comes back in the restore response and is rendered into the
 * transcript afterwards.
 */
function confirmRestore(cp, index) {
  const when = formatTime(cp.timestamp ?? cp.commit_time);
  const body = [
    el("p", { text: `Restore the workspace to "${cp.label || cp.id.slice(0, 12)}"${when ? ` from ${when}` : ""}.` }),
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

  ui.connect.textContent = "Restart session";
  ui.sessionNote.className = "panel-note";
  ui.sessionNote.textContent = "Restarting replaces the session and clears its transcript.";

  updateTurnUi();
  if (isNew) refreshCheckpoints();
}

async function startSession() {
  const workspace = ui.workspace.value.trim();
  const model = ui.model.value;
  const mode = ui.mode.value;

  if (!workspace) {
    ui.sessionNote.className = "panel-note is-error";
    ui.sessionNote.textContent = "A workspace path is required.";
    ui.workspace.focus();
    return;
  }
  if (!model) {
    ui.sessionNote.className = "panel-note is-error";
    ui.sessionNote.textContent = "Pick a model. If the list is empty, download one from the model shop.";
    return;
  }

  ui.connect.disabled = true;
  // POST /session is refused with 409 if the workspace it is replacing is
  // still busy, so the composer must not be able to start a turn on the
  // outgoing session while the new one is being created.
  setComposerEnabled(false, "Starting session...");
  ui.sessionNote.className = "panel-note";
  ui.sessionNote.textContent = "Starting...";
  try {
    stopEventStream();
    const session = await sidecar.createSession({ workspace, model, mode });
    state.lastEventId = 0;
    transcript.reset();
    transcript.showPlaceholder("Session ready", `${session.mode} mode in ${session.workspace}`);
    applySession(session);
    rememberWorkspace(session.workspace);
    startEventStream();
    await refreshCheckpoints();
  } catch (err) {
    ui.sessionNote.className = "panel-note is-error";
    ui.sessionNote.textContent = errorText(err);
    updateTurnUi();
  } finally {
    ui.connect.disabled = false;
  }
}

// ------------------------------------------------------------------ composer

function setComposerEnabled(enabled, statusText) {
  ui.composer.disabled = !enabled;
  ui.send.disabled = !enabled || !ui.composer.value.trim();
  if (statusText !== undefined) ui.composerStatus.textContent = statusText;
}

function updateTurnUi() {
  const hasSession = Boolean(state.session);
  ui.stop.hidden = !state.running;
  if (!hasSession) {
    setComposerEnabled(false, "Start a session to begin.");
    setConn(state.backendHealthy ? "ok" : "warn", state.backendHealthy ? "ready" : "backend not ready");
    return;
  }
  if (state.running) {
    setComposerEnabled(false, "Working. Press Esc or the stop button to interrupt.");
    setConn("busy", "running");
  } else {
    setComposerEnabled(true, "Ready.");
    setConn("ok", "connected");
  }
}

function autosize() {
  ui.composer.style.height = "auto";
  ui.composer.style.height = Math.min(ui.composer.scrollHeight, 220) + "px";
}

async function send() {
  const message = ui.composer.value.trim();
  if (!message || !state.session || state.running) return;
  ui.composer.value = "";
  autosize();
  transcript.addUser(message);
  state.running = true;
  updateTurnUi();
  try {
    await sidecar.prompt(message);
  } catch (err) {
    state.running = false;
    updateTurnUi();
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
      transcript.addNotice("quiet", "Some earlier events were dropped.",
        "The session's event buffer wrapped while this window was disconnected.");
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
      transcript.addNotice("warn", "Turn cancelled.",
        "A tool call already in flight may still finish in the background.");
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
      ui.sessionNote.textContent = "";
    } else if (body.error) {
      ui.sessionNote.className = "panel-note";
      ui.sessionNote.textContent = body.error + " Type the path instead.";
    }
  } catch {
    ui.sessionNote.className = "panel-note";
    ui.sessionNote.textContent = "No folder picker available here. Type the path instead.";
  } finally {
    ui.browse.disabled = false;
  }
}

// ------------------------------------------------------------------ bindings

ui.tabChat.addEventListener("click", () => setView("chat"));
ui.tabShop.addEventListener("click", () => setView("shop"));
ui.connect.addEventListener("click", startSession);
ui.browse.addEventListener("click", browseForFolder);
ui.reloadModels.addEventListener("click", refreshModels);
ui.reloadSetup.addEventListener("click", refreshSetup);
ui.reloadCheckpoints.addEventListener("click", refreshCheckpoints);
ui.send.addEventListener("click", send);
ui.stop.addEventListener("click", cancel);

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
    ui.setupBody.textContent = "";
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
