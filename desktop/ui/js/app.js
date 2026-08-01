/* hearth desktop UI controller.
 *
 * Wires the sidecar (desktop/server) to the transcript and the side panels.
 * Plain ES modules, no build step, no dependencies, no network fetches beyond
 * the sidecar itself.
 */

import { Sidecar, HttpError, fetchHandshake } from "./api.js";
import { Transcript } from "./transcript.js";
import { el, icon, appendAll, clear, $ } from "./dom.js";
import { blob } from "./safe-text.js";

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
};

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
    return;
  }
  const names = installed.map((m) => m.name).filter(Boolean).sort();
  for (const name of names) ui.model.appendChild(el("option", { value: name, text: name }));
  if (!names.length) {
    ui.model.appendChild(el("option", { value: "", text: "no models pulled on this Ollama", disabled: true }));
  }
  if (previous && [...ui.model.options].some((o) => o.value === previous)) ui.model.value = previous;
  else if (state.session?.model && names.includes(state.session.model)) ui.model.value = state.session.model;
  else if (names.length) ui.model.value = names[0];
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
    ui.sessionNote.textContent = "Pick a model. If the list is empty, pull one with `ollama pull` first.";
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
    case "delta":
      transcript.appendAgent(data.text || "");
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
    const response = await fetch("/__hearth/pick-folder", {
      method: "POST",
      cache: "no-store",
      credentials: "omit",
    });
    const body = await response.json();
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
  transcript.showPlaceholder("Connecting", "Reading the sidecar handshake from the dev host.");
  setConn("busy", "connecting");

  try {
    state.handshake = await fetchHandshake("");
    sidecar.setToken(state.handshake.token);
  } catch (err) {
    setConn("down", "no handshake");
    transcript.showPlaceholder("Cannot reach the sidecar",
      "The dev host did not return a handshake. Start it with: node desktop/ui/dev-host.mjs");
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

  await Promise.all([refreshSetup(), refreshModels()]);

  const session = await loadSession().catch((err) => {
    transcript.addNotice("error", "Could not read the session.", errorText(err));
    return null;
  });

  if (session) {
    transcript.showPlaceholder("Session restored",
      `${session.mode} mode in ${session.workspace}. Earlier events replay below.`);
    startEventStream();
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
