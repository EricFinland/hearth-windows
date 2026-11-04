/* The model chip: what model is resident, what it costs, and freeing it.
 *
 * A loaded model holds several GB of VRAM (or RAM) for as long as the
 * bundled engine keeps it, and nothing on screen used to say so. This chip
 * sits in the titlebar and answers three questions at a glance: is a model
 * loaded, which one, and roughly how much memory it holds. Clicking it opens
 * a small panel with the Unload button and the idle auto-unload delay.
 *
 * POLLED, NOT STREAMED. The page already holds five event streams open for
 * its whole life, and a sixth starves every fetch in WebView2 (see the note
 * in app.js above watchEngine). So this asks GET /model with a plain request
 * on a timer: every 10 s normally, every second while a turn is running and
 * the model is not loaded yet (that is what turns the composer's "Working"
 * into "Loading model..." during a reload), and not at all while the window
 * is hidden.
 *
 * ESCAPE. app.js cancels the running turn on any Escape that reaches the
 * document. While the panel is open, a capture-phase listener on window
 * takes Escape first, closes the panel and stops it there, so dismissing
 * this panel can never also stop someone's turn.
 *
 * Every string from the sidecar (model names come from file names and
 * Ollama tags, reasons from the server) is rendered through dom.js's
 * el({text}) / setText, which neutralize bidi overrides; nothing here builds
 * markup from data.
 */

import { el, icon, clear, setText, $ } from "./dom.js";

const POLL_IDLE_MS = 10000;
const POLL_FAST_MS = 1000;
const POLL_OPEN_MS = 4000;
const POLL_ERROR_MS = 15000;

export const LOADING_HINT = "Loading model...";

const STATUS_LABEL = {
  loaded: "Loaded",
  loading: LOADING_HINT,
  not_loaded: "Not loaded",
  unknown: "Unknown",
};

const DELAY_LABEL = (minutes) => (minutes === null ? "Never" : minutes === 60 ? "1 hour" : `${minutes} min`);

/** "4.1 GB" / "640 MB", or "" for nothing known. Binary units, labelled the
 *  way Windows labels them, because that is what Task Manager will show
 *  next to it. */
export function formatBytes(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n <= 0) return "";
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1)} GB`;
  return `${Math.max(1, Math.round(n / 1024 ** 2))} MB`;
}

/** The one memory figure the chip shows: VRAM when the GPU query answered,
 *  the process's RAM otherwise. {text, kind} or null. */
export function memoryFigure(snapshot) {
  const memory = (snapshot && snapshot.memory) || {};
  const vram = formatBytes(memory.vram_bytes);
  if (vram) return { text: vram, kind: "VRAM" };
  const ram = formatBytes(memory.rss_bytes);
  if (ram) return { text: ram, kind: "RAM" };
  return null;
}

function relative(epochSeconds, nowMs = Date.now()) {
  if (typeof epochSeconds !== "number") return "";
  const diffMs = epochSeconds * 1000 - nowMs;
  const minutes = Math.round(Math.abs(diffMs) / 60000);
  if (minutes < 1) return diffMs > 0 ? "in under a minute" : "just now";
  const span = minutes >= 60 ? `${Math.round(minutes / 60)} h` : `${minutes} min`;
  return diffMs > 0 ? `in ${span}` : `${span} ago`;
}

function statusOf(snapshot) {
  if (!snapshot) return "unknown";
  return STATUS_LABEL[snapshot.status] ? snapshot.status : "unknown";
}

/** Why the Unload button is disabled right now, or "" when it is not. */
export function unloadBlocker(snapshot, running) {
  if (!snapshot) return "Model status is not available yet.";
  if (snapshot.busy && snapshot.busy_reason) return `Unavailable while ${snapshot.busy_reason}.`;
  if (running) return "Unavailable while a turn is running.";
  if (snapshot.loading) return "Unavailable while the model is loading.";
  if (snapshot.status === "not_loaded") return "No model is loaded.";
  return "";
}

/** The chip button's contents for one snapshot. Pure, so the XSS harness can
 *  drive it with hostile model names. */
export function renderChip(snapshot, { expanded = false } = {}) {
  const status = statusOf(snapshot);
  const figure = status === "loaded" ? memoryFigure(snapshot) : null;
  const name = snapshot && snapshot.model ? snapshot.model : "";
  const title = [
    name ? `Model: ${name}` : "No model loaded",
    STATUS_LABEL[status],
    figure ? `about ${figure.text} of ${figure.kind}` : "",
  ].filter(Boolean).join(". ") + ". Click for options.";
  return el("button", {
    class: "mchip-btn", type: "button", title,
    "aria-haspopup": "dialog", "aria-expanded": expanded ? "true" : "false",
    "aria-controls": "mchip-pop",
  }, [
    el("span", { class: "mchip-dot", dataset: { state: status }, "aria-hidden": "true" }),
    el("span", { class: "mchip-label", text: STATUS_LABEL[status] }),
    figure ? el("span", { class: "mchip-mem", text: figure.text }) : null,
  ]);
}

/** The panel body for one snapshot. Pure apart from the handlers it wires:
 *  onUnload(), onDelay(minutes). */
export function renderPanel(snapshot, { running = false, error = "", pending = false,
  onUnload, onDelay } = {}) {
  const status = statusOf(snapshot);
  const facts = el("dl", { class: "mchip-facts" });
  const fact = (label, value) => {
    if (!value) return;
    facts.appendChild(el("dt", { text: label }));
    facts.appendChild(el("dd", { text: value }));
  };
  fact("Status", STATUS_LABEL[status]);
  const figure = memoryFigure(snapshot);
  if (status === "loaded" && figure) fact("Memory", `about ${figure.text} of ${figure.kind}`);
  if (snapshot && snapshot.last_used_at) fact("Last used", relative(snapshot.last_used_at));
  if (snapshot && snapshot.unload_at) fact("Unloads", relative(snapshot.unload_at));

  const notes = [];
  if (snapshot && snapshot.note) notes.push(snapshot.note);
  else if (status === "loaded") notes.push("Unloading frees this memory. The next prompt loads the model again, which takes a few seconds.");
  else if (status === "not_loaded") notes.push("The model loads on the next prompt.");

  const minutes = snapshot ? snapshot.auto_unload_minutes : undefined;
  const options = (snapshot && Array.isArray(snapshot.auto_unload_options))
    ? snapshot.auto_unload_options : [5, 15, 30, 60, null];
  const delay = el("div", { class: "mchip-delay", role: "group", "aria-label": "Unload when idle for" },
    options.map((value) => el("button", {
      class: "mchip-opt", type: "button",
      "aria-pressed": value === minutes ? "true" : "false",
      disabled: pending,
      text: DELAY_LABEL(value),
      on: { click: () => { if (value !== minutes && onDelay) onDelay(value); } },
    })));

  const blocker = pending ? "Working..." : unloadBlocker(snapshot, running);
  const unload = el("button", {
    class: "btn btn-sm mchip-unload", type: "button", disabled: Boolean(blocker),
    on: { click: () => { if (!blocker && onUnload) onUnload(); } },
  }, [icon("i-eject"), el("span", { text: "Unload now" })]);

  return el("div", { class: "mchip-panel" }, [
    el("div", { class: "mchip-head" }, [
      el("span", { class: "mchip-kicker", text: "Model in memory" }),
      snapshot && snapshot.model
        ? el("span", { class: "mchip-name", text: snapshot.model })
        : el("span", { class: "mchip-name is-empty", text: "Nothing loaded" }),
    ]),
    facts,
    ...notes.map((text) => el("p", { class: "mchip-note", text })),
    el("div", { class: "mchip-section" }, [
      el("span", { class: "mchip-section-label", text: "Unload when idle for" }),
      delay,
      snapshot && snapshot.managed_by === "ollama"
        ? el("p", { class: "mchip-note", text: "Applies to Hearth's own engine. Ollama keeps its own schedule." })
        : null,
    ]),
    // The title lives on this wrapper, not the button: a disabled button gets
    // no pointer events, so its own tooltip would never show.
    el("div", { class: "mchip-actions" }, [
      el("span", { class: "mchip-why", title: blocker || "Free this model's memory now" }, [unload]),
      blocker && blocker !== "Working..." ? el("span", { class: "mchip-why-text", text: blocker }) : null,
    ]),
    error ? el("p", { class: "mchip-error", role: "alert", text: error }) : null,
  ]);
}

// ------------------------------------------------------------------ runtime

const live = {
  ctx: null,          // { sidecar, isRunning, onChange }
  root: null,
  pop: null,
  snapshot: null,
  open: false,
  error: "",
  pending: false,
  timer: null,
  nextAt: 0,
  polling: false,
  lastPollAt: 0,
  failed: false,
  hint: "",
};

function running() {
  try { return Boolean(live.ctx && live.ctx.isRunning && live.ctx.isRunning()); } catch { return false; }
}

function loadingNow() {
  const snap = live.snapshot;
  if (!snap) return false;
  return Boolean(snap.loading) || snap.status === "not_loaded";
}

const COMPOSER_HINT = "Loading model... Press Esc or the stop button to interrupt.";

function computeHint() {
  return running() && loadingNow() ? COMPOSER_HINT : "";
}

/** The composer's status text while a turn is waiting on a model load, or ""
 *  when it is not. app.js calls this from updateTurnUi on every turn state
 *  change, which is also how a starting turn switches polling to fast. */
export function modelLoadingHint() {
  if (running()) wantFast();
  live.hint = computeHint();
  return live.hint;
}

function delayFor() {
  if (live.failed) return POLL_ERROR_MS;
  if (running() && loadingNow()) return POLL_FAST_MS;
  if (live.snapshot && live.snapshot.loading) return POLL_FAST_MS * 2;
  if (live.open) return POLL_OPEN_MS;
  return POLL_IDLE_MS;
}

function schedule(ms) {
  clearTimeout(live.timer);
  live.nextAt = Date.now() + ms;
  live.timer = setTimeout(poll, ms);
}

function wantFast() {
  if (live.polling || !live.ctx) return;
  const soonest = Math.max(0, POLL_FAST_MS - (Date.now() - live.lastPollAt));
  if (live.nextAt - Date.now() > soonest + 250) schedule(soonest);
}

async function poll() {
  live.timer = null;
  // Hidden windows do not poll (visibilitychange restarts it), except for
  // the very first read, so a window that boots minimized still has a chip.
  if (!live.ctx || (document.hidden && live.lastPollAt)) return;
  live.polling = true;
  live.lastPollAt = Date.now();
  try {
    apply(await live.ctx.sidecar.model());
    live.failed = false;
  } catch {
    // Keep the last good picture; the chip is advisory and the next poll
    // retries. A sidecar that is down is already said by the connection dot.
    live.failed = true;
  } finally {
    live.polling = false;
    schedule(delayFor());
  }
}

function apply(snapshot) {
  if (snapshot && typeof snapshot === "object") live.snapshot = snapshot;
  paint();
  const hint = computeHint();
  if (hint !== live.hint) {
    live.hint = hint;
    try { live.ctx.onChange && live.ctx.onChange(); } catch { /* a repaint is best effort */ }
  }
}

function paint() {
  const root = live.root;
  if (!root) return;
  root.hidden = !live.snapshot;
  const button = renderChip(live.snapshot, { expanded: live.open });
  button.addEventListener("click", (event) => { event.stopPropagation(); toggle(); });
  const old = root.querySelector(".mchip-btn");
  if (old) {
    const hadFocus = document.activeElement === old;
    root.replaceChild(button, old);
    if (hadFocus) button.focus({ preventScroll: true });
  } else {
    root.insertBefore(button, root.firstChild);
  }
  if (live.open) paintPanel();
}

function paintPanel() {
  const pop = live.pop;
  // Keep keyboard focus on the same control across a repaint, so a poll
  // landing while someone tabs through the options does not throw them out.
  const focused = pop.contains(document.activeElement) ? document.activeElement : null;
  const focusKey = focused ? (focused.textContent || "") + "|" + focused.className : "";
  clear(pop);
  pop.appendChild(renderPanel(live.snapshot, {
    running: running(), error: live.error, pending: live.pending,
    onUnload: doUnload, onDelay: doDelay,
  }));
  if (focusKey) {
    for (const node of pop.querySelectorAll("button")) {
      if ((node.textContent || "") + "|" + node.className === focusKey && !node.disabled) {
        node.focus({ preventScroll: true });
        break;
      }
    }
  }
}

function onKey(event) {
  if (event.key !== "Escape" || !live.open) return;
  event.preventDefault();
  event.stopPropagation();
  close(true);
}

function onOutside(event) {
  if (live.open && live.root && !live.root.contains(event.target)) close(false);
}

function toggle() {
  if (live.open) close(false); else openPanel();
}

function openPanel() {
  live.open = true;
  live.error = "";
  live.pop.hidden = false;
  window.addEventListener("keydown", onKey, true);
  document.addEventListener("pointerdown", onOutside, true);
  paint();
  // Fresh numbers for whoever just asked.
  if (!live.polling) schedule(0);
}

function close(returnFocus) {
  live.open = false;
  live.pop.hidden = true;
  clear(live.pop);
  window.removeEventListener("keydown", onKey, true);
  document.removeEventListener("pointerdown", onOutside, true);
  paint();
  if (returnFocus) {
    const button = live.root.querySelector(".mchip-btn");
    if (button) button.focus({ preventScroll: true });
  }
}

function errorText(err) {
  const body = err && err.body;
  if (body && typeof body.error === "string") return body.error;
  return (err && err.message) || "The request failed.";
}

async function doUnload() {
  live.pending = true;
  live.error = "";
  paint();
  try {
    apply(await live.ctx.sidecar.unloadModel());
  } catch (err) {
    live.error = err && err.status === 409
      ? `Not unloaded: ${errorText(err)}.`
      : `Could not unload: ${errorText(err)}`;
  } finally {
    live.pending = false;
    paint();
  }
}

async function doDelay(minutes) {
  live.pending = true;
  live.error = "";
  paint();
  try {
    apply(await live.ctx.sidecar.setModelAutoUnload(minutes));
  } catch (err) {
    live.error = `Could not save that: ${errorText(err)}`;
  } finally {
    live.pending = false;
    paint();
  }
}

/** Build the chip in #model-chip and start polling. Called once from boot.
 *  `isRunning` and `onChange` are app.js's: the first says whether a turn is
 *  in flight, the second repaints the composer when modelLoadingHint()'s
 *  answer changes. */
export function startModelChip({ sidecar, isRunning, onChange, container } = {}) {
  const root = container || $("#model-chip");
  if (!root || !sidecar || live.ctx) return;
  live.ctx = { sidecar, isRunning, onChange };
  live.root = root;
  live.pop = el("div", {
    class: "mchip-pop", id: "mchip-pop", role: "dialog", "aria-label": "Model in memory", hidden: true,
  });
  root.appendChild(live.pop);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !live.polling) schedule(0);
  });
  schedule(0);
}
