/* The work loop's face: the form that bounds a run before it starts, the
 * meters that make it legible while it runs, and the account that explains it
 * afterwards.
 *
 * WHAT THIS SCREEN IS FOR
 * ----------------------
 * A person is about to leave a model running unsupervised on their own files,
 * possibly overnight. Three things decide whether that is a reasonable thing
 * to do or a slot machine, and all three are this module's job:
 *
 *   1. BEFORE. What bounds it, and what it may do, are on screen and editable
 *      -- not defaults buried behind an "advanced" chevron. The form's fields,
 *      their hard minimums and maximums, the tool list, and what each tool
 *      would actually be allowed to do all arrive from GET /loop's `defaults`,
 *      computed by permissions.decide on the server. Nothing here re-derives
 *      a permission rule in JavaScript, so the card cannot describe an
 *      authority the run does not have.
 *
 *   2. DURING. Which turn it is on, against which ceiling, with the write
 *      budget drawn as a meter because "18 of 200 unattended writes" is the
 *      number a person actually wants when they walk back to the machine.
 *      Those numbers come from hearth_workloop._spend, the same expression
 *      the ceiling check reads -- a meter fed by a second tally would
 *      eventually disagree with enforcement, and the visible one is the one
 *      that would be believed.
 *
 *   3. AFTER. The account. "Stopped: no progress in 6 turns; the same 3 tests
 *      failed each time; last error was X" is the whole product here, so it is
 *      rendered as the terminal thing in the transcript rather than folded
 *      into a one-line notice.
 *
 * THE DISCLAIMER IS NOT DECORATION
 * --------------------------------
 * hearth_workloop documents that steady progress toward a WRONG answer trips
 * no detector, and that work outside the workspace reads as a stall. A user
 * who reads "no stall detected" as "it worked" has been misled by this
 * product, so the limits ship as structured data (GET /loop's `blind_spots`)
 * and are rendered in two places on purpose: next to the form, where they can
 * still change what someone configures, and at the foot of every account,
 * where they qualify the verdict being read. They are not behind a tooltip
 * and not dismissable.
 *
 * The single strongest remedy for the worst blind spot is a completion check,
 * so the form nags for one: with no done_command and no required artifacts,
 * "finished" is the model's own opinion about its own work, and the form says
 * exactly that, before the run rather than in the report afterwards.
 *
 * UNTRUSTED TEXT
 * --------------
 * Goals, stop details, error text, tool names, file paths and journal
 * contents are all model- or file-authored. Every one of them reaches the
 * page through dom.js (el/setText/textNode/blob), which neutralizes bidi
 * overrides and control characters into visible markers. This file never
 * builds a string of markup and never touches innerHTML.
 */

import { el, icon, appendAll, clear, setText } from "./dom.js";
import { blob } from "./safe-text.js";

/* Which ceilings appear, in which order, with the label and unit a person
 * thinks in. `scale` converts the displayed number to what POST /session
 * takes, so wall clock is typed in minutes and sent in seconds without the
 * page ever holding two different meanings for one field. */
const CEILING_FIELDS = [
  { key: "max_turns", label: "Turns", unit: "turns", scale: 1,
    hint: "One turn is one model reply plus its tool calls." },
  { key: "max_seconds", label: "Wall clock", unit: "minutes", scale: 60,
    hint: "Measured at turn boundaries, so a long turn can overshoot." },
  { key: "max_tokens", label: "Tokens", unit: "tokens", scale: 1,
    hint: "Counted at turn boundaries. Runaway generation inside one turn is capped separately." },
  { key: "max_writes", label: "Unattended file writes", unit: "writes", scale: 1,
    scary: true,
    hint: "How many times it may change a file with nobody approving it." },
  { key: "max_tool_calls", label: "Tool calls", unit: "calls", scale: 1,
    hint: "Every tool the model invokes, including refused ones." },
];

const STALL_FIELDS = [
  { key: "window", label: "Turns without a new workspace state" },
  { key: "repeat_actions", label: "Identical tool calls in a row" },
  { key: "repeat_errors", label: "Identical errors in a row" },
  { key: "oscillations", label: "Returns to an already-seen state" },
  { key: "min_turns", label: "Never judge before turn" },
];

/* What the panel calls a set of thresholds matching no preset. The same word
 * hearth_workloop.patience_of returns, and like it, derived from the numbers
 * rather than remembered beside them: the label on screen is computed from the
 * five inputs every time they change, so it can never describe a run as
 * "Balanced" while the run is something else. */
const CUSTOM_PATIENCE = "custom";

const GATE_TEXT = {
  deny: "Refuse it, tell the model why, and keep going. A model that keeps "
      + "trying the same refused thing looks identical every turn, so stall "
      + "detection ends the run with a readable account.",
  stop: "End the run at the first gated tool.",
  ask: "Raise a real approval card. Nobody may be awake to answer it, so an "
     + "unanswered card is refused after the timeout below and the run "
     + "continues as if it had been denied.",
};

const STOP_TONE = {
  completed: "ok",
  ceiling: "warn",
  stalled: "warn",
  cancelled: "warn",
  blocked: "warn",
  error: "error",
};

function hms(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}:${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function count(n) {
  return Number(n || 0).toLocaleString();
}

/** A labelled meter. `value` and `limit` are always both shown as numbers,
 *  never only as a bar: a bar answers "roughly how far", and someone deciding
 *  whether to let a run continue needs the actual figures. */
function meter(label, value, limit, { format = count, scary = false } = {}) {
  const ratio = limit > 0 ? Math.min(1, Number(value || 0) / limit) : 0;
  const near = ratio >= 0.8;
  const fill = el("i", { class: "lm-fill" });
  fill.style.width = `${Math.round(ratio * 100)}%`;
  const track = appendAll(el("div", { class: "lm-track" }), [fill]);
  const head = appendAll(el("div", { class: "lm-head" }), [
    el("span", { class: "lm-label", text: label }),
    el("span", { class: "lm-value", text: `${format(value)} of ${format(limit)}` }),
  ]);
  const node = appendAll(el("div", { class: "lm" }), [head, track]);
  if (near) node.classList.add("is-near");
  if (scary) node.classList.add("is-scary");
  return node;
}

/** The blind-spot list, rendered from the server's own data. Used next to the
 *  form and at the foot of every account. */
export function blindSpots(list, { title = "What this cannot tell you" } = {}) {
  const items = (list || []).map((b) => appendAll(el("li", { class: "lbs-item" }), [
    el("strong", { text: b.headline || "" }),
    el("span", { class: "lbs-means", text: b.means || "" }),
    b.remedy ? el("span", { class: "lbs-remedy", text: b.remedy }) : null,
  ]));
  return appendAll(el("div", { class: "lbs" }), [
    appendAll(el("div", { class: "lbs-title" }), [icon("i-alert"), el("span", { text: title })]),
    appendAll(el("ul", { class: "lbs-list" }), items),
  ]);
}

/* ------------------------------------------------------------------ form */

/** The sidebar panel that bounds a run before it starts.
 *
 *  Owns no state of its own beyond the DOM: read() walks the live inputs, so
 *  what is sent is exactly what is on screen at the moment the button is
 *  pressed. */
export class LoopConfigPanel {
  constructor(root) {
    this.root = root;
    this.defaults = null;
    this._liveKey = null;   // the last live session config written into the form
    this.inputs = new Map();     // key -> input element
    this.toolBoxes = new Map();  // tool name -> checkbox
    this.mode = "auto";
    this.onChange = null;
    this._rendered = false;
  }

  /** Draw the form from GET /loop's `defaults`. Idempotent: called again with
   *  the same payload it keeps whatever the user has already typed, because
   *  the snapshot stream delivers `defaults` on every frame and a form that
   *  reset itself mid-typing would be unusable. */
  render(defaults, blind) {
    if (!defaults) return;
    const same = this._rendered
      && JSON.stringify(defaults) === JSON.stringify(this.defaults);
    this.defaults = defaults;
    if (same) { this._applyGateVisibility(); this._applyModeEffects(); return; }
    this.blind = blind;
    this._rendered = true;
    clear(this.root);
    this.inputs.clear();
    this.toolBoxes.clear();

    const cfg = defaults.config || {};
    const limits = defaults.limits || {};

    // ---- ceilings: the headline of the panel, never behind a chevron ----
    this.root.appendChild(el("p", {
      class: "panel-note",
      text: "These bound the run. Set them before you start; they cannot be "
          + "changed once it is going, and there is no way to ask for an "
          + "unbounded run.",
    }));
    const ceilBox = el("div", { class: "loop-fields" });
    for (const f of CEILING_FIELDS) {
      const [low, high] = (limits.ceilings || {})[f.key] || [1, 1e9];
      const value = Math.max(1, Math.round((cfg.ceilings || {})[f.key] / f.scale));
      const input = el("input", {
        class: "input", type: "number", id: `loop-${f.key}`,
        min: String(Math.max(1, Math.ceil(low / f.scale))),
        max: String(Math.floor(high / f.scale)),
        step: "1", value: String(value),
      });
      input.addEventListener("input", () => this._changed());
      this.inputs.set(f.key, { input, scale: f.scale });
      const label = appendAll(el("label", {
        class: "field loop-field" + (f.scary ? " is-scary" : ""),
        for: `loop-${f.key}`,
      }), [
        appendAll(el("span", { class: "field-label" }), [
          el("span", { text: f.label }),
          el("span", { class: "loop-unit", text: f.unit }),
        ]),
        input,
        el("span", { class: "loop-hint", text: f.hint }),
      ]);
      ceilBox.appendChild(label);
    }
    this.root.appendChild(ceilBox);

    // ---- patience: one choice, beside the ceilings, never behind a chevron
    // The five stall thresholds below are meaningless without the benchmark
    // that produced them, so the decision a person can actually make is
    // "how long should it keep trying". It sits here, in the open, next to
    // the bounds, because it changes what the run costs -- and its price is
    // printed under it rather than left in the docs: the most patient setting
    // really can spend the whole budget on something that was never going to
    // work. The names, the descriptions and the price all arrive from the
    // server (hearth_workloop.patience_choices), so this page cannot describe
    // a setting more kindly than the module implementing it.
    this.patienceChoices = defaults.patience || [];
    this.patienceSelect = el("select", { class: "input", id: "loop-patience" });
    for (const p of this.patienceChoices) {
      this.patienceSelect.appendChild(el("option", { value: p.name, text: p.label }));
    }
    this.patienceSelect.appendChild(el("option", {
      value: CUSTOM_PATIENCE, text: "Custom (your own thresholds)",
    }));
    this.patienceSelect.value = cfg.patience
      || defaults.default_patience || CUSTOM_PATIENCE;
    this.patienceSelect.addEventListener("change", () => this._applyPatience());
    this.patienceSummary = el("p", { class: "loop-hint" });
    this.patienceCost = el("p", { class: "panel-note is-warn" });
    this.root.appendChild(appendAll(el("div", { class: "loop-patience" }), [
      appendAll(el("label", { class: "field", for: "loop-patience" }), [
        el("span", { class: "field-label", text: "How long it keeps trying" }),
        this.patienceSelect,
      ]),
      this.patienceSummary,
      this.patienceCost,
    ]));

    // ---- what it may do -------------------------------------------------
    const toolWrap = el("div", { class: "loop-tools" });
    this.toolsNote = el("p", { class: "loop-hint" });
    for (const t of defaults.tools || []) {
      const box = el("input", {
        class: "loop-tool-box", type: "checkbox", id: `loop-tool-${t.name}`,
        checked: true,
      });
      // `wanted` is the user's intent, kept apart from `checked`, which also
      // encodes availability. Changing the permission mode disables the tools
      // that mode denies, and re-enabling them must restore what the user
      // asked for -- not leave everything unticked, which would send an empty
      // manifest and mean "a run that can only talk to itself".
      const entry = { box, tool: t, wanted: true };
      box.addEventListener("change", () => {
        entry.wanted = box.checked;
        this._changed();
      });
      this.toolBoxes.set(t.name, entry);
      const badge = el("span", { class: "loop-tool-eff", text: "" });
      toolWrap.appendChild(appendAll(el("label", {
        class: "loop-tool", for: `loop-tool-${t.name}`,
      }), [box, el("span", { class: "loop-tool-name mono", text: t.name }), badge]));
      this.toolBoxes.get(t.name).badge = badge;
    }
    this.root.appendChild(appendAll(el("details", { class: "loop-details", open: true }), [
      el("summary", { text: "What it is allowed to do" }),
      this.toolsNote,
      toolWrap,
    ]));

    // ---- the completion check, and the nag for one -----------------------
    this.doneCommand = el("input", {
      class: "input mono", type: "text", id: "loop-done-command",
      placeholder: "python -m pytest -q", spellcheck: false, autocomplete: "off",
    });
    this.doneCommand.addEventListener("input", () => this._changed());
    this.artifacts = el("input", {
      class: "input mono", type: "text", id: "loop-artifacts",
      placeholder: "out/report.md, dist/app.js", spellcheck: false, autocomplete: "off",
    });
    this.artifacts.addEventListener("input", () => this._changed());
    this.doneWarning = el("p", { class: "panel-note is-warn" });
    this.root.appendChild(appendAll(el("details", { class: "loop-details", open: true }), [
      el("summary", { text: "Completion check" }),
      el("p", {
        class: "loop-hint",
        text: "A command whose exit status decides whether the goal is met, "
            + "and files that must exist. This is the only thing that can "
            + "catch a run that is confidently wrong.",
      }),
      appendAll(el("label", { class: "field", for: "loop-done-command" }), [
        el("span", { class: "field-label", text: "Command" }), this.doneCommand,
      ]),
      appendAll(el("label", { class: "field", for: "loop-artifacts" }), [
        el("span", { class: "field-label", text: "Files that must exist" }), this.artifacts,
      ]),
      this.doneWarning,
    ]));

    // ---- gates -----------------------------------------------------------
    this.gateSelect = el("select", { class: "input", id: "loop-gate" });
    for (const p of defaults.gate_policies || []) {
      this.gateSelect.appendChild(el("option", { value: p, text: p }));
    }
    this.gateSelect.value = cfg.gate_policy || "deny";
    this.gateSelect.addEventListener("change", () => {
      this._applyGateVisibility();
      this._changed();
    });
    const [gLow, gHigh] = limits.gate_timeout_seconds || [5, 3600];
    this.gateTimeout = el("input", {
      class: "input", type: "number", id: "loop-gate-timeout",
      min: String(gLow), max: String(gHigh), step: "5",
      value: String(cfg.gate_timeout || cfg.gate_timeout_seconds || 300),
    });
    this.gateTimeout.addEventListener("input", () => this._changed());
    this.gateNote = el("p", { class: "loop-hint" });
    this.gateTimeoutField = appendAll(el("label", {
      class: "field", for: "loop-gate-timeout",
    }), [
      appendAll(el("span", { class: "field-label" }), [
        el("span", { text: "Answer within" }),
        el("span", { class: "loop-unit", text: "seconds" }),
      ]),
      this.gateTimeout,
      el("span", {
        class: "loop-hint",
        text: "An unanswered card is refused when this runs out, so an "
            + "unattended run cannot hang on one forever.",
      }),
    ]);
    this.root.appendChild(appendAll(el("details", { class: "loop-details" }), [
      el("summary", { text: "Gated tools" }),
      appendAll(el("label", { class: "field", for: "loop-gate" }), [
        el("span", { class: "field-label", text: "When a tool needs approval" }),
        this.gateSelect,
      ]),
      this.gateNote,
      this.gateTimeoutField,
    ]));

    // ---- stall detection --------------------------------------------------
    const stallBox = el("div", { class: "loop-fields" });
    for (const f of STALL_FIELDS) {
      const [low, high] = (limits.stall || {})[f.key] || [0, 1000];
      const input = el("input", {
        class: "input", type: "number", id: `loop-stall-${f.key}`,
        min: String(low), max: String(high), step: "1",
        value: String((cfg.stall || {})[f.key]),
      });
      input.addEventListener("input", () => this._changed());
      this.inputs.set("stall." + f.key, { input, scale: 1 });
      stallBox.appendChild(appendAll(el("label", {
        class: "field loop-field", for: `loop-stall-${f.key}`,
      }), [
        el("span", { class: "field-label", text: f.label }), input,
      ]));
    }
    this.stallWarning = el("p", { class: "panel-note is-warn" });
    this.root.appendChild(appendAll(el("details", { class: "loop-details" }), [
      el("summary", { text: "Stall detection, in detail" }),
      el("p", {
        class: "loop-hint",
        text: "The thresholds behind the setting above. Change one and the "
            + "setting becomes Custom. Zero switches a detector off. Ceilings "
            + "still bound the run either way.",
      }),
      stallBox,
      this.stallWarning,
    ]));

    // ---- and the limits, where they can still change a decision ----------
    this.root.appendChild(blindSpots(this.blind, {
      title: "Before you leave it running",
    }));

    this._applyGateVisibility();
    this._applyModeEffects();
    this._changed();
  }

  /** Write a LIVE session's actual bounds into the form.
   *
   *  The form is built from the server's defaults, which is right before a
   *  session exists and wrong the moment one does: a page reloaded (or a
   *  sidecar restarted) into a session running at 30 turns would otherwise
   *  show 40, and the numbers on screen would not be the numbers bounding
   *  the run. Applied once per distinct config, so it cannot fight someone
   *  typing.
   *
   *  `config` is the engine's own manifest, read off the live object by
   *  app.py, not an echo of whatever this page last sent. */
  applyConfig(config) {
    if (!config || !this._rendered) return;
    const key = JSON.stringify(config);
    if (key === this._liveKey) return;
    this._liveKey = key;
    for (const f of CEILING_FIELDS) {
      const entry = this.inputs.get(f.key);
      const value = (config.ceilings || {})[f.key];
      if (entry && Number.isFinite(value)) {
        entry.input.value = String(Math.max(1, Math.round(value / f.scale)));
      }
    }
    for (const f of STALL_FIELDS) {
      const entry = this.inputs.get("stall." + f.key);
      const value = (config.stall || {})[f.key];
      if (entry && Number.isFinite(value)) entry.input.value = String(value);
    }
    if (config.gate_policy) this.gateSelect.value = config.gate_policy;
    if (Number.isFinite(config.gate_timeout)) {
      this.gateTimeout.value = String(config.gate_timeout);
    }
    this.doneCommand.value = config.done_command || "";
    this.artifacts.value = (config.required_artifacts || []).join(", ");
    const allowed = new Set(config.allowed_tools || []);
    for (const [name, entry] of this.toolBoxes) entry.wanted = allowed.has(name);
    this._applyGateVisibility();
    this._changed();
  }

  /** The permission mode the session will use. Changes what each tool is
   *  allowed to do, so the tool list is redrawn from the server's own
   *  per-mode verdicts rather than from a rule this file invents. */
  setMode(mode) {
    this.mode = mode;
    this._applyModeEffects();
  }

  _applyModeEffects() {
    if (!this.toolBoxes.size) return;
    let unattendedWrites = 0;
    let gated = 0;
    let active = 0;
    for (const entry of this.toolBoxes.values()) {
      const { box, tool, badge } = entry;
      const effect = (tool.effect || {})[this.mode] || "deny";
      const label = effect === "allow"
        ? (tool.risk === "edit" ? "writes, unattended" : "reads")
        : effect === "gate" ? "needs approval" : "not available in this mode";
      setText(badge, label);
      badge.className = "loop-tool-eff is-" + effect;
      box.disabled = effect === "deny";
      box.checked = entry.wanted && !box.disabled;
      if (!box.checked) continue;
      active += 1;
      if (effect === "allow" && tool.risk === "edit") unattendedWrites += 1;
      if (effect === "gate") gated += 1;
    }
    // The sentence a person actually needs before leaving this running: how
    // many of these change their files with nobody approving each one.
    const gatedTail = gated
      ? `, and ${gated} ${gated === 1 ? "needs" : "need"} approval.` : ".";
    setText(this.toolsNote, active === 0
      ? "Nothing is selected. A run with no tools can only talk to itself."
      : unattendedWrites
        ? `In ${this.mode} mode, ${unattendedWrites} of the ${active} selected `
          + `tools change files with nobody approving each one${gatedTail}`
        : `In ${this.mode} mode none of the ${active} selected tools changes a `
          + `file without approval${gatedTail}`);
    this.toolsNote.classList.toggle("is-warn", unattendedWrites > 0);
  }

  _applyGateVisibility() {
    const policy = this.gateSelect ? this.gateSelect.value : "deny";
    setText(this.gateNote, GATE_TEXT[policy] || "");
    if (this.gateTimeoutField) this.gateTimeoutField.hidden = policy !== "ask";
  }

  /** Picking a named setting writes its thresholds into the five inputs, so
   *  what is sent is still literally what is on screen -- the chooser is a
   *  shortcut for filling the form, never a second, invisible source of
   *  truth. Choosing "Custom" changes nothing: it is a description of the
   *  numbers, not an instruction. */
  _applyPatience() {
    const chosen = (this.patienceChoices || []).find(
      (p) => p.name === this.patienceSelect.value);
    if (chosen) {
      for (const f of STALL_FIELDS) {
        const entry = this.inputs.get("stall." + f.key);
        const value = (chosen.settings || {})[f.key];
        if (entry && Number.isFinite(value)) entry.input.value = String(value);
      }
    }
    this._changed();
  }

  /** The name for whatever is currently in the five inputs, derived the same
   *  way hearth_workloop.patience_of derives it: by comparing the numbers.
   *  Nothing here trusts the select's own value, because a user who edits a
   *  threshold by hand has stopped running the preset that was showing. */
  _patienceName() {
    const now = {};
    for (const f of STALL_FIELDS) {
      const entry = this.inputs.get("stall." + f.key);
      if (!entry) return CUSTOM_PATIENCE;
      now[f.key] = Math.round(Number(entry.input.value));
    }
    const match = (this.patienceChoices || []).find(
      (p) => STALL_FIELDS.every((f) => Number((p.settings || {})[f.key]) === now[f.key]));
    return match ? match.name : CUSTOM_PATIENCE;
  }

  _syncPatience() {
    if (!this.patienceSelect) return CUSTOM_PATIENCE;
    const name = this._patienceName();
    this.patienceSelect.value = name;
    const chosen = (this.patienceChoices || []).find((p) => p.name === name);
    setText(this.patienceSummary, chosen ? chosen.summary
      : "Your own thresholds. What stops this run is whatever you typed below.");
    // The price of the choice, in the place the choice is made. Not a
    // tooltip, not the docs: a run that cannot succeed spends real time and
    // real electricity, and the setting that decides how much is this one.
    setText(this.patienceCost, chosen ? chosen.cost || "" : "");
    this.patienceCost.hidden = !(chosen && chosen.cost);
    return name;
  }

  _changed() {
    const verified = Boolean(this.doneCommand && (this.doneCommand.value.trim()
      || (this.artifacts && this.artifacts.value.trim())));
    if (this.doneWarning) {
      setText(this.doneWarning, verified ? "" :
        "With no completion check, \"finished\" is the model's own claim about "
        + "its own work. Nothing will verify it.");
      this.doneWarning.hidden = verified;
    }
    this._syncPatience();
    if (this.stallWarning) {
      const off = STALL_FIELDS.filter((f) => {
        const entry = this.inputs.get("stall." + f.key);
        return entry && Number(entry.input.value) === 0;
      });
      const allOff = off.length >= STALL_FIELDS.length - 1;
      setText(this.stallWarning, off.length === 0 ? "" : allOff
        ? "Stall detection is effectively off. This run can only end at a "
          + "ceiling, at the completion check, or when you stop it."
        : `${off.length} detector(s) switched off.`);
      this.stallWarning.hidden = off.length === 0;
    }
    this._applyModeEffects();
    if (this.onChange) this.onChange();
  }

  /** The `loop` object for POST /session, read off the live inputs.
   *  Every number is sent explicitly rather than omitted-when-default, so
   *  what the operator read on screen is literally what the run is given. */
  read() {
    const ceilings = {};
    for (const f of CEILING_FIELDS) {
      const entry = this.inputs.get(f.key);
      if (!entry) continue;
      ceilings[f.key] = Math.round(Number(entry.input.value) * f.scale);
    }
    const stall = {};
    for (const f of STALL_FIELDS) {
      const entry = this.inputs.get("stall." + f.key);
      if (!entry) continue;
      stall[f.key] = Math.round(Number(entry.input.value));
    }
    const tools = [];
    for (const [name, { box }] of this.toolBoxes) {
      if (box.checked && !box.disabled) tools.push(name);
    }
    const out = { ceilings, stall, gate_policy: this.gateSelect.value };
    // The name goes with the numbers, and only when it actually describes
    // them. The server re-derives the label from the thresholds anyway, so a
    // name that disagreed with what is on screen would be dropped there --
    // but it must never be sent in the first place.
    const patience = this._patienceName();
    if (patience !== CUSTOM_PATIENCE) out.patience = patience;
    if (this.gateSelect.value === "ask") {
      out.gate_timeout_seconds = Math.round(Number(this.gateTimeout.value));
    }
    if (tools.length) out.allowed_tools = tools;
    const cmd = this.doneCommand.value.trim();
    if (cmd) out.done_command = cmd;
    const arts = this.artifacts.value.split(/[,\n]/).map((a) => a.trim()).filter(Boolean);
    if (arts.length) out.required_artifacts = arts;
    return out;
  }
}

/* ------------------------------------------------------------- the run bar */

/** The strip above the transcript while a run is alive: what it is doing,
 *  what it has spent, and whether "stopped" actually means quiet. */
export class LoopRunBar {
  constructor(root) {
    this.root = root;
  }

  /** Render a GET /loop snapshot. Returns true if anything is shown. */
  render(snapshot) {
    const snap = snapshot || {};
    const run = snap.run;
    const pending = snap.pending;
    clear(this.root);
    if (!run && !pending) {
      this.root.hidden = true;
      return false;
    }
    this.root.hidden = false;
    if (pending) this.root.appendChild(this._pending(pending));
    if (run) this.root.appendChild(this._run(run));
    return true;
  }

  _pending(p) {
    const bits = [
      `${count(p.completed_turns)} turn(s) completed`,
      p.interrupted_turn ? `turn ${p.interrupted_turn} was interrupted and will not be resumed` : null,
    ].filter(Boolean).join(" · ");
    const lines = [
      appendAll(el("div", { class: "lrb-title" }), [
        icon("i-undo"),
        el("strong", { text: "An unfinished run was inherited from before the restart." }),
      ]),
      el("div", { class: "lrb-goal", text: p.goal || "(no goal recorded)" }),
      el("div", { class: "lrb-sub", text: bits }),
    ];
    if (p.journal_goal_differs) {
      lines.push(el("div", {
        class: "lrb-warn",
        text: "Its journal on disk records a different goal than the session "
            + "did. The goal shown here is the session's. Treat the journal "
            + "as untrusted.",
      }));
    }
    lines.push(el("div", {
      class: p.resumable ? "lrb-sub" : "lrb-warn",
      text: p.resumable
        ? "Send \"resume\" to continue it from the last completed turn, or "
          + "give a new goal to leave it alone."
        : `It cannot be resumed: ${p.refusal || "its journal is not trustworthy"}. `
          + "Its workspace and checkpoints are untouched.",
    }));
    return appendAll(el("div", { class: "lrb-card lrb-pending" }), lines);
  }

  _run(run) {
    const ceilings = run.ceilings || {};
    const spend = run.spend || {};
    const stopping = run.state === "stopping";
    const stopped = run.state === "stopped";

    const stateLabel = stopping ? "stopping"
      : stopped ? `stopped: ${run.stop_reason || "unknown"}`
      : `working, turn ${count(run.turn)}`;
    // A tick means "it did the thing", and nothing else. A run that was
    // stopped, bounded or stalled reached none of that, and a green check
    // beside "stopped: stalled" is the exact reassurance this screen must
    // never give.
    const head = appendAll(el("div", { class: "lrb-title" }), [
      icon(run.stop_reason === "completed" ? "i-check"
        : stopped ? "i-alert" : "i-clock"),
      el("strong", { text: stateLabel }),
      run.resumed ? el("span", { class: "lrb-tag", text: "resumed" }) : null,
      el("span", { class: "lrb-tag", text: run.mode }),
      // Which patience setting will decide whether this run is called
      // stalled, while it still matters rather than only in the account.
      run.patience ? el("span", { class: "lrb-tag", text: `patience: ${run.patience}` }) : null,
      run.verified
        ? el("span", { class: "lrb-tag is-ok", text: "completion checked" })
        : el("span", { class: "lrb-tag is-warn", text: "no completion check" }),
    ]);

    const meters = appendAll(el("div", { class: "lrb-meters" }), [
      meter("Turns", spend.turns, ceilings.max_turns),
      meter("Wall clock", spend.elapsed ?? run.elapsed, ceilings.max_seconds, { format: hms }),
      meter("Tokens", spend.tokens, ceilings.max_tokens),
      meter("Unattended writes", spend.writes, ceilings.max_writes, { scary: true }),
      meter("Tool calls", spend.tool_calls, ceilings.max_tool_calls),
    ]);

    const nodes = [
      head,
      el("div", { class: "lrb-goal", text: run.goal || "" }),
      meters,
    ];

    const gateBits = [];
    if (run.gates_allowed) gateBits.push(`${count(run.gates_allowed)} approved`);
    if (run.gates_denied) gateBits.push(`${count(run.gates_denied)} refused`);
    if (run.gates_timed_out) {
      gateBits.push(`${count(run.gates_timed_out)} unanswered and therefore refused`);
    }
    const facts = [
      run.done_command ? `completion check: ${run.done_command}` : null,
      gateBits.length ? `gated tools: ${gateBits.join(", ")}` : null,
      run.checkpoints ? `${count(run.checkpoints)} checkpoint(s)` : null,
      run.last_progress && run.last_progress.new_state === false
        ? "the last turn left the workspace in a state it had already been in"
        : null,
    ].filter(Boolean);
    if (facts.length) nodes.push(el("div", { class: "lrb-sub", text: facts.join(" · ") }));

    if (stopped && run.stop_detail) {
      nodes.push(el("div", {
        class: "lrb-detail is-" + (STOP_TONE[run.stop_reason] || "warn"),
        text: run.stop_detail,
      }));
    }

    // The one thing a stopped run must never quietly claim.
    if (run.live_workers > 0) {
      nodes.push(el("div", {
        class: "lrb-warn",
        text: `${count(run.live_workers)} tool call(s) abandoned by the stop are `
            + "STILL RUNNING against this workspace. Stopping ends the loop's "
            + "waiting; it cannot kill a call already in flight. Do not restore "
            + "a checkpoint or start new work here until this reaches zero.",
      }));
    } else if (stopped) {
      nodes.push(el("div", {
        class: "lrb-sub is-ok",
        text: "Nothing is still running against this workspace.",
      }));
    }

    const card = appendAll(el("div", { class: "lrb-card" }), nodes);
    card.dataset.state = run.state || "";
    if (run.stop_reason) card.dataset.reason = run.stop_reason;
    return card;
  }
}

/* ------------------------------------------------------------- the account */

/** The account, as the terminal thing in the transcript.
 *
 *  render() returns a node; app.js appends it. The headline and the numbers
 *  are pulled out of the report so they can be read at a glance, and the
 *  loop's own rendered text is kept verbatim below them -- it is the artifact
 *  hearth_workloop was written to produce and reformatting it here would mean
 *  two places that decide what an account says. */
export function account(report, accountText, blind) {
  const r = report || {};
  const tone = STOP_TONE[r.stop_reason] || "warn";
  const ceilings = r.ceilings || {};

  const head = appendAll(el("div", { class: "lac-head" }), [
    icon(r.stop_reason === "completed" ? "i-check" : "i-alert"),
    el("strong", { text: headline(r) }),
  ]);

  const nodes = [head];
  if (r.stop_detail) nodes.push(el("div", { class: "lac-detail", text: r.stop_detail }));

  // Why it stopped, in the terms the detector itself used.
  const detectors = ((r.verdict || {}).detectors) || [];
  if (detectors.length) {
    // Its own class, not just "lac-list": these two lists carry different
    // untrusted strings from different sources (a detector's own prose, and
    // the model's error output), and anything asserting on one must not be
    // able to match the other by accident.
    nodes.push(appendAll(el("ul", { class: "lac-list lac-detectors" }), detectors.map((d) =>
      appendAll(el("li", {}), [
        el("code", { class: "mono", text: d.name || "" }),
        el("span", { text: " " + (d.detail || "") }),
      ]))));
  }

  // A stall is a judgement made at a threshold this user chose, not a fact
  // about the work, and the account says which setting made it. Without this
  // line "stopped making progress" reads as a property of the run, and the
  // one thing the reader could change about it stays invisible.
  if (r.stop_reason === "stalled" && r.patience) {
    nodes.push(el("div", {
      class: "lac-sub lac-patience",
      text: `Judged at the "${r.patience}" patience setting`
          + (Number.isFinite(r.turns) && Number.isFinite(ceilings.max_turns)
            ? `, with ${Math.max(0, ceilings.max_turns - r.turns)} of `
              + `${ceilings.max_turns} turns unspent.` : "."),
    }));
  }

  // "the same 3 tests failed each time" -- the quantified evidence.
  const recurring = r.recurring_errors || [];
  if (recurring.length) {
    nodes.push(el("div", { class: "lac-sub", text: "Errors seen more than once:" }));
    nodes.push(appendAll(el("ul", { class: "lac-list lac-recurring" }), recurring.map(([n, text]) =>
      appendAll(el("li", {}), [
        el("span", { class: "lac-count", text: `${count(n)}x` }),
        el("span", { class: "mono", text: String(text).slice(0, 400) }),
      ]))));
  }
  if (r.last_error) {
    nodes.push(el("div", { class: "lac-sub", text: "Last error, verbatim:" }));
    nodes.push(blob(String(r.last_error)));
  }

  const completion = r.completion || {};
  if (completion.detail) {
    nodes.push(el("div", {
      class: "lac-sub",
      text: `Completion check: ${completion.detail}`,
    }));
    if (completion.output && !completion.done) nodes.push(blob(String(completion.output)));
  }

  nodes.push(appendAll(el("div", { class: "lac-spend" }), [
    meter("Turns", r.turns, ceilings.max_turns),
    meter("Wall clock", r.elapsed, ceilings.max_seconds, { format: hms }),
    meter("Tokens", r.tokens, ceilings.max_tokens),
    meter("Unattended writes", r.writes, ceilings.max_writes, { scary: true }),
    meter("Tool calls", r.tool_calls, ceilings.max_tool_calls),
  ]));

  for (const [label, items] of [["created", r.created], ["modified", r.modified],
                                ["deleted", r.deleted]]) {
    if (items && items.length) {
      nodes.push(appendAll(el("div", { class: "lac-files" }), [
        el("span", { class: "lac-files-label", text: label }),
        el("span", { class: "mono", text: items.slice(0, 20).join(", ")
          + (items.length > 20 ? ` (+${items.length - 20} more)` : "") }),
      ]));
    }
  }

  for (const note of r.notices || []) {
    nodes.push(el("div", { class: "lac-note", text: note }));
  }

  if (r.live_workers > 0) {
    nodes.push(el("div", {
      class: "lac-warn",
      text: `${count(r.live_workers)} abandoned tool call(s) were still running `
          + "against this workspace when the loop stopped waiting for them.",
    }));
  }

  if (accountText) {
    nodes.push(appendAll(el("details", { class: "lac-full" }), [
      el("summary", { text: "Full account" }),
      blob(String(accountText)),
    ]));
  }

  nodes.push(blindSpots(blind));

  return appendAll(el("div", { class: "lac lac-" + tone }), nodes);
}

export function headline(r) {
  switch (r.stop_reason) {
    case "completed":
      return (r.completion && r.completion.verified === false)
        ? `Finished in ${count(r.turns)} turn(s), but nothing verified it`
        : `Finished the goal in ${count(r.turns)} turn(s)`;
    case "ceiling": return "Stopped at a ceiling";
    case "stalled": return "Stopped making progress";
    case "cancelled": return "Stopped by you";
    case "blocked": return "Stopped needing permission";
    case "error": return "Stopped on an error";
    default: return "Stopped";
  }
}

