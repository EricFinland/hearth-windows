/* The agent swarm's three surfaces, in the order a person meets them: the
 * bounds form in the sidebar (before), the run bar above the transcript
 * (during), and the account in the transcript (after).
 *
 * A pure render module, like loop.js: it fetches nothing and subscribes to
 * nothing. app.js owns the network and hands whole snapshots to render().
 *
 * THREE THINGS THIS SCREEN MUST NOT SAY
 * =====================================
 *
 * 1. It must not draw one budget per role. The ceilings are shared by the
 *    whole relay (hearth_swarmloop._residual), so there is ONE row of meters
 *    and the per-phase list reports spend, never budget. A user who counts
 *    three roles and assumes three times the tokens has been misled by this
 *    file, not by the engine.
 *
 * 2. It must not show a tick for a reviewer's approval. `reviewer_approved`
 *    and `verified` arrive as separate fields and stay separate all the way
 *    to the pixels: the first is one 7B's opinion of another 7B's work, the
 *    second is a command that exited 0. Merging them would tell someone their
 *    code passes when nothing ran it. The approval renders as a WARNING when
 *    it is the only evidence there is.
 *
 * 3. It must not imply the roles run at once. They do not, and cannot: one
 *    model is resident at a time and only one role holds the write lease. The
 *    phase list is drawn as a sequence, with the active role marked, because
 *    that is what actually happens.
 *
 * Every string that reaches the screen goes through dom.js's `el`/`setText`
 * (which neutralize) or safe-text.js's `blob`. Role names, goals, critiques,
 * plans and stop details are all model output and all untrusted.
 */

import { el, icon, appendAll, clear, setText } from "./dom.js";
import { blob } from "./safe-text.js";

/* The shared budget. `max_cycles` is deliberately in the same table as the
 * rest: it is a ceiling in exactly the same sense, and putting it elsewhere
 * would suggest the relay is bounded by something other than these numbers. */
const CEILING_FIELDS = [
  { key: "max_turns", label: "Turns", unit: "turns", scale: 1,
    hint: "Across every role, not each. One turn is one model reply plus its tool calls." },
  { key: "max_seconds", label: "Wall clock", unit: "minutes", scale: 60,
    hint: "Measured at turn boundaries, so a long turn can overshoot. Model swaps count." },
  { key: "max_tokens", label: "Tokens", unit: "tokens", scale: 1,
    hint: "One budget for the whole relay. Three roles do not get three budgets." },
  { key: "max_writes", label: "Unattended file writes", unit: "writes", scale: 1,
    scary: true,
    hint: "Only the implementer can spend these. The other roles cannot write at all." },
  { key: "max_tool_calls", label: "Tool calls", unit: "calls", scale: 1,
    hint: "Every tool any role invokes, including refused ones." },
  { key: "max_cycles", label: "Relay cycles", unit: "cycles", scale: 1,
    hint: "How many times implementer and reviewer may hand off before it gives up." },
];

const STALL_FIELDS = [
  { key: "window", label: "Turns without a new workspace state" },
  { key: "repeat_actions", label: "Identical actions in a row" },
  { key: "repeat_errors", label: "Turns with the same error" },
  { key: "oscillations", label: "Returns to an already-seen state" },
  { key: "min_turns", label: "Turns before any of this is judged" },
];

const STOP_TONE = {
  completed: "ok", ceiling: "warn", exhausted: "warn",
  cancelled: "warn", blocked: "warn", error: "error",
};

function count(n) {
  return Number(n || 0).toLocaleString();
}

function hms(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}:${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

/** A labelled meter. Numbers always shown as numbers, never only as a bar --
 *  someone deciding whether to let a relay continue needs the figures. */
function meter(label, value, limit, { format = count, scary = false } = {}) {
  const ratio = limit > 0 ? Math.min(1, Number(value || 0) / limit) : 0;
  const fill = el("i", { class: "lm-fill" });
  fill.style.width = `${Math.round(ratio * 100)}%`;
  const track = appendAll(el("div", { class: "lm-track" }), [fill]);
  const head = appendAll(el("div", { class: "lm-head" }), [
    el("span", { class: "lm-label", text: label }),
    el("span", { class: "lm-value", text: `${format(value)} of ${format(limit)}` }),
  ]);
  const node = appendAll(el("div", { class: "lm" }), [head, track]);
  if (ratio >= 0.8) node.classList.add("is-near");
  if (scary) node.classList.add("is-scary");
  return node;
}

/** The blind-spot list, rendered from the server's own data. Its own class
 *  prefix so a test asserting on swarm blind spots cannot match a loop's. */
export function blindSpots(list, { title = "What this cannot tell you" } = {}) {
  const items = (list || []).map((b) => appendAll(el("li", { class: "lbs-item" }), [
    el("strong", { text: b.headline || "" }),
    el("span", { class: "lbs-means", text: b.means || "" }),
    b.remedy ? el("span", { class: "lbs-remedy", text: b.remedy }) : null,
  ]));
  return appendAll(el("div", { class: "lbs swm-blind" }), [
    appendAll(el("div", { class: "lbs-title" }), [icon("i-alert"), el("span", { text: title })]),
    appendAll(el("ul", { class: "lbs-list" }), items),
  ]);
}

/** The roles, as a read-only card: who they are and which one can write.
 *
 *  Not configurable, and the card says so. The engine refuses a `roles` field
 *  over the transport precisely so that "exactly one role can change a file"
 *  is a property of the build rather than of a request, and a form offering
 *  to edit it would imply otherwise. */
export function roleCard(roles) {
  const rows = (roles || []).map((r) => appendAll(el("li", { class: "swm-role" }), [
    appendAll(el("div", { class: "swm-role-head" }), [
      el("span", { class: "swm-role-name", text: r.name || "" }),
      r.writes
        ? el("span", { class: "swm-tag is-scary", text: "can change files" })
        : el("span", { class: "swm-tag is-ok", text: "read-only" }),
      el("span", { class: "swm-tag", text: `up to ${count(r.max_turns)} turns` }),
    ]),
    el("span", { class: "swm-role-purpose", text: r.purpose || "" }),
    appendAll(el("div", { class: "swm-role-tools" }),
      (r.tools || []).map((t) => el("code", {
        class: "mono swm-tool"
             + (t.effect && t.effect.auto ? ` is-${t.effect.auto}` : ""),
        text: typeof t === "string" ? t : (t.name || ""),
      }))),
  ]));
  return appendAll(el("div", { class: "swm-roles" }), [
    appendAll(el("ul", { class: "swm-role-list" }), rows),
    el("p", {
      class: "panel-note",
      text: "The roles are fixed. Exactly one of them can change a file, and "
          + "they take turns: one model fits in this machine's memory at a "
          + "time, so they never run at once.",
    }),
  ]);
}

/* ------------------------------------------------------------------ form */

/** The sidebar panel that bounds a relay before it starts. Owns no state
 *  beyond the DOM: read() walks the live inputs. */
export class SwarmConfigPanel {
  constructor(root) {
    this.root = root;
    this.defaults = null;
    this._liveKey = null;
    this._rendered = false;
    this.inputs = new Map();
    this.toolBoxes = new Map();
    this.mode = "auto";
  }

  setMode(mode) {
    this.mode = mode || "auto";
    this._applyModeEffects();
  }

  /** Draw the form from GET /swarm's `defaults`. Idempotent: the snapshot
   *  stream delivers `defaults` on every frame, and a form that reset itself
   *  mid-typing would be unusable. */
  render(defaults, blind) {
    if (!defaults) return;
    const same = this._rendered
      && JSON.stringify(defaults) === JSON.stringify(this.defaults);
    this.defaults = defaults;
    if (same) { this._applyModeEffects(); return; }
    this.blind = blind;
    this._rendered = true;
    clear(this.root);
    this.inputs.clear();
    this.toolBoxes.clear();
    // Dropped along with everything else clear() just removed. Without this
    // the stale node stays referenced, _applyModeEffects sees a truthy
    // this.modeNote and never re-appends it, and the "every role is read-only
    // in plan mode" warning silently disappears after a re-render. A warning
    // that vanishes on its own is worse than one that was never shown.
    this.modeNote = null;

    const cfg = defaults.config || {};
    const limits = defaults.limits || {};

    this.root.appendChild(el("p", {
      class: "panel-note",
      text: "One budget, shared by every role. These bound the whole relay, "
          + "not each agent in it. Set them before you start; they cannot be "
          + "changed once it is going, and there is no way to ask for an "
          + "unbounded run.",
    }));

    const ceilBox = el("div", { class: "loop-fields" });
    for (const f of CEILING_FIELDS) {
      const [low, high] = (limits.ceilings || {})[f.key] || [1, 1e9];
      const value = Math.max(1, Math.round((cfg.ceilings || {})[f.key] / f.scale));
      const input = el("input", {
        class: "input", type: "number", id: `swarm-${f.key}`,
        min: String(Math.max(1, Math.ceil(low / f.scale))),
        max: String(Math.floor(high / f.scale)),
        step: "1", value: String(value),
      });
      this.inputs.set(f.key, { input, scale: f.scale });
      ceilBox.appendChild(appendAll(el("label", {
        class: "field loop-field" + (f.scary ? " is-scary" : ""),
        for: `swarm-${f.key}`,
      }), [
        appendAll(el("span", { class: "field-label" }), [
          el("span", { text: f.label }),
          el("span", { class: "loop-unit", text: f.unit }),
        ]),
        input,
        el("span", { class: "loop-hint", text: f.hint }),
      ]));
    }
    this.root.appendChild(ceilBox);

    // ---- the roles, read-only ----
    this.root.appendChild(appendAll(el("details", { class: "loop-details" }), [
      el("summary", { text: "The roles" }),
      roleCard(defaults.roles),
    ]));

    // ---- the completion check: the only thing that can finish a relay ----
    const doneCmd = el("input", {
      class: "input mono", type: "text", id: "swarm-done-command",
      placeholder: "pytest -q",
      value: (cfg.done_command || ""),
    });
    this.doneCommand = doneCmd;
    const artifacts = el("input", {
      class: "input mono", type: "text", id: "swarm-artifacts",
      placeholder: "src/parser.py, tests/test_parser.py",
      value: (cfg.required_artifacts || []).join(", "),
    });
    this.artifacts = artifacts;
    this.root.appendChild(appendAll(el("details", { class: "loop-details" }), [
      el("summary", { text: "Completion check" }),
      el("p", {
        class: "loop-hint",
        text: "A command that exits 0 when the goal is done. This is the ONLY "
            + "thing that can finish a relay. The reviewer cannot: it is the "
            + "same kind of model as the implementer and it cannot run code.",
      }),
      appendAll(el("label", { class: "field", for: "swarm-done-command" }), [
        el("span", { class: "field-label", text: "Command" }), doneCmd,
      ]),
      appendAll(el("label", { class: "field", for: "swarm-artifacts" }), [
        el("span", { class: "field-label", text: "Files that must exist" }), artifacts,
      ]),
      el("p", {
        class: "panel-note is-warn",
        text: "Without one of these, 'finished' is the model's own opinion of "
            + "its own work, and the account will say so.",
      }),
    ]));

    // ---- tools: narrowing only, and it applies to every role ----
    const toolBox = el("div", { class: "loop-tools" });
    for (const name of defaults.all_tools || []) {
      const box = el("input", { class: "loop-tool-box", type: "checkbox",
                                id: `swarm-tool-${name}` });
      box.checked = true;
      this.toolBoxes.set(name, { box });
      toolBox.appendChild(appendAll(el("label", {
        class: "loop-tool", for: `swarm-tool-${name}`,
      }), [
        box,
        el("span", { class: "loop-tool-name mono", text: name }),
      ]));
    }
    this.root.appendChild(appendAll(el("details", { class: "loop-details" }), [
      el("summary", { text: "What any role may use" }),
      el("p", {
        class: "loop-hint",
        text: "Unticking a tool removes it from every role. It can only ever "
            + "take capability away: no setting here can give a read-only "
            + "role the ability to write.",
      }),
      toolBox,
    ]));

    // ---- gates ----
    const gateSelect = el("select", { class: "input", id: "swarm-gate" });
    for (const p of defaults.gate_policies || []) {
      gateSelect.appendChild(el("option", { value: p, text: p }));
    }
    gateSelect.value = cfg.gate_policy || "deny";
    this.gateSelect = gateSelect;
    const gateTimeout = el("input", {
      class: "input", type: "number", id: "swarm-gate-timeout",
      min: String((limits.gate_timeout_seconds || [5])[0]),
      max: String((limits.gate_timeout_seconds || [5, 3600])[1]),
      value: String(cfg.gate_timeout || 300),
    });
    this.gateTimeout = gateTimeout;
    gateSelect.addEventListener("change", () => this._applyGateVisibility());
    this.gateRow = appendAll(el("label", { class: "field", for: "swarm-gate-timeout" }), [
      el("span", { class: "field-label", text: "Seconds to wait for an answer" }),
      gateTimeout,
    ]);
    this.root.appendChild(appendAll(el("details", { class: "loop-details" }), [
      el("summary", { text: "Tools that need approval" }),
      el("p", {
        class: "loop-hint",
        text: "Nobody is awake during an unattended relay. 'deny' refuses and "
            + "tells the role why, 'stop' ends the run at the first one, and "
            + "'ask' raises a real card that gives up after a deadline.",
      }),
      appendAll(el("label", { class: "field", for: "swarm-gate" }), [
        el("span", { class: "field-label", text: "When a tool needs approval" }),
        gateSelect,
      ]),
      this.gateRow,
    ]));

    // ---- stall detection: here it is a HANDOFF signal, not an ending ----
    const stallBox = el("div", { class: "loop-fields" });
    for (const f of STALL_FIELDS) {
      const [low, high] = (limits.stall || {})[f.key] || [0, 1000];
      const input = el("input", {
        class: "input", type: "number", id: `swarm-stall-${f.key}`,
        min: String(low), max: String(high), step: "1",
        value: String((cfg.stall || {})[f.key] ?? 0),
      });
      this.inputs.set("stall." + f.key, { input, scale: 1 });
      stallBox.appendChild(appendAll(el("label", {
        class: "field loop-field", for: `swarm-stall-${f.key}`,
      }), [
        el("span", { class: "field-label", text: f.label }), input,
      ]));
    }
    this.root.appendChild(appendAll(el("details", { class: "loop-details" }), [
      el("summary", { text: "When a role has stopped getting anywhere" }),
      el("p", {
        class: "loop-hint",
        text: "In a relay this is a handoff signal, not an ending: a role that "
            + "has stopped changing anything is exactly when the next one "
            + "should look. Set to 0 to switch a detector off.",
      }),
      stallBox,
    ]));

    this.root.appendChild(blindSpots(blind));
    this._applyGateVisibility();
    this._applyModeEffects();
  }

  _applyGateVisibility() {
    if (this.gateRow) this.gateRow.hidden = this.gateSelect.value !== "ask";
  }

  /** In plan mode every role is read-only, which makes the relay an
   *  investigation rather than a change. Said out loud, because a user who
   *  picked plan and expected edits would otherwise watch three roles spend a
   *  budget achieving nothing and have no idea why. */
  _applyModeEffects() {
    if (!this._rendered) return;
    if (!this.modeNote) {
      this.modeNote = el("p", { class: "panel-note is-warn" });
      this.root.appendChild(this.modeNote);
    }
    const planning = this.mode === "plan";
    this.modeNote.hidden = !planning;
    if (planning) {
      setText(this.modeNote,
        "In 'plan' mode every role is read-only, including the implementer. "
        + "The relay will investigate and report, and will not change a file.");
    }
  }

  /** Write a LIVE session's real bounds over the defaults, so a reloaded page
   *  shows the ceilings the running relay actually has. Guarded so it cannot
   *  fight someone typing. */
  applyConfig(config) {
    if (!config || !this._rendered) return;
    const key = JSON.stringify(config);
    if (key === this._liveKey) return;
    this._liveKey = key;
    for (const f of CEILING_FIELDS) {
      const entry = this.inputs.get(f.key);
      const value = (config.ceilings || {})[f.key];
      if (entry && typeof value === "number") {
        entry.input.value = String(Math.max(1, Math.round(value / f.scale)));
      }
    }
    if (this.doneCommand && typeof config.done_command === "string") {
      this.doneCommand.value = config.done_command;
    }
  }

  /** The `swarm` object for POST /session, read off the live inputs. Every
   *  number is sent explicitly rather than omitted-when-default, so what the
   *  operator read on screen is literally what the relay is given. */
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
    if (this.gateSelect.value === "ask") {
      out.gate_timeout_seconds = Math.round(Number(this.gateTimeout.value));
    }
    // Sent only when it is a genuine narrowing. Sending the full list would
    // be harmless but would make the request claim an intent it does not have.
    if (tools.length && tools.length < this.toolBoxes.size) out.allowed_tools = tools;
    const cmd = this.doneCommand.value.trim();
    if (cmd) out.done_command = cmd;
    const arts = this.artifacts.value.split(/[,\n]/).map((a) => a.trim()).filter(Boolean);
    if (arts.length) out.required_artifacts = arts;
    return out;
  }
}

/* --------------------------------------------------------------- run bar */

/** The strip above the transcript while a relay is running. */
export class SwarmRunBar {
  constructor(root) {
    this.root = root;
  }

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
    const nodes = [
      appendAll(el("div", { class: "lrb-title" }), [
        icon("i-alert"),
        el("strong", { text: "A relay from a previous session never finished" }),
      ]),
      el("div", { class: "lrb-goal", text: p.goal || "" }),
      el("div", {
        class: "lrb-sub",
        text: `${count(p.completed_phases)} phase(s) completed`
            + (p.interrupted_phase
              ? `; phase ${count(p.interrupted_phase)} was interrupted and will not be resumed`
              : ""),
      }),
    ];
    if (p.resumable) {
      nodes.push(el("div", {
        class: "lrb-detail is-ok",
        text: "Send 'resume' to continue it. Any other goal starts a new relay "
            + "and leaves this one's journal and checkpoints alone.",
      }));
    } else {
      nodes.push(el("div", {
        class: "lrb-detail is-warn",
        text: `It cannot be resumed: ${p.refusal || "its journal is not trustworthy"}`,
      }));
    }
    if (p.journal_goal_differs) {
      nodes.push(el("div", {
        class: "lrb-warn",
        text: "Its journal records a different goal than this session does. "
            + "One of the two has been edited.",
      }));
    }
    return appendAll(el("div", { class: "lrb-card lrb-pending" }), nodes);
  }

  _run(run) {
    const ceilings = run.ceilings || {};
    const spend = run.spend || {};
    const stopping = run.state === "stopping";
    const stopped = run.state === "stopped";

    const stateLabel = stopping ? "stopping"
      : stopped ? `stopped: ${run.stop_reason || "unknown"}`
      : `${run.role || "starting"} working, cycle ${count(run.cycle)}, turn ${count(run.turn)}`;

    // A tick means "the completion check passed", and nothing else. A relay
    // that was bounded, exhausted or merely approved by its own reviewer
    // reached none of that.
    const head = appendAll(el("div", { class: "lrb-title" }), [
      icon(run.stop_reason === "completed" ? "i-check"
        : stopped ? "i-alert" : "i-clock"),
      el("strong", { text: stateLabel }),
      run.resumed ? el("span", { class: "lrb-tag", text: "resumed" }) : null,
      el("span", { class: "lrb-tag", text: run.mode }),
      run.verified
        ? el("span", { class: "lrb-tag is-ok", text: "completion checked" })
        : el("span", { class: "lrb-tag is-warn", text: "no completion check" }),
    ]);

    const nodes = [head, el("div", { class: "lrb-goal", text: run.goal || "" })];

    nodes.push(this._phases(run));

    // ONE row of meters, for the whole relay. See this file's header.
    nodes.push(appendAll(el("div", { class: "lrb-meters" }), [
      meter("Turns (all roles)", spend.turns, ceilings.max_turns),
      meter("Wall clock", spend.elapsed ?? run.elapsed, ceilings.max_seconds,
            { format: hms }),
      meter("Tokens (all roles)", spend.tokens, ceilings.max_tokens),
      meter("Unattended writes", spend.writes, ceilings.max_writes, { scary: true }),
      meter("Tool calls", spend.tool_calls, ceilings.max_tool_calls),
      meter("Relay cycles", run.cycle, ceilings.max_cycles),
    ]));
    nodes.push(el("div", {
      class: "lrb-sub",
      text: "One budget, shared by every role.",
    }));

    if (run.swaps > 0) {
      nodes.push(el("div", {
        class: "lrb-sub",
        text: `${count(run.swaps)} model swap(s), ${hms(run.swap_seconds)} of wall `
            + "clock spent loading models rather than working.",
      }));
    }
    if (run.lease_refusals > 0) {
      nodes.push(el("div", {
        class: "lrb-detail is-warn",
        text: `${count(run.lease_refusals)} attempt(s) by a read-only role to change `
            + "a file were refused by the write lease.",
      }));
    }
    if (run.stop_detail) {
      nodes.push(el("div", {
        class: "lrb-detail " + (run.stop_reason === "completed" ? "is-ok" : "is-warn"),
        text: run.stop_detail,
      }));
    }
    // The approval, if that is all there is, is a warning and not a result.
    if (run.reviewer_approved && !run.verified) {
      nodes.push(el("div", {
        class: "lrb-warn",
        text: "The reviewer did not object, but nothing ran this. A reviewer is "
            + "the same kind of model as the implementer and cannot execute code.",
      }));
    }
    if (run.live_workers > 0) {
      nodes.push(el("div", {
        class: "lrb-warn",
        text: `${count(run.live_workers)} abandoned tool call(s) may still be running `
            + "against this workspace.",
      }));
    }

    const card = appendAll(el("div", { class: "lrb-card" }), nodes);
    card.dataset.state = run.state || "";
    if (run.stop_reason) card.dataset.reason = run.stop_reason;
    return card;
  }

  /** The relay as a sequence. Drawn as a strip of steps rather than a table
   *  because the ORDER and the HANDOFFS are the thing being explained. */
  _phases(run) {
    const phases = run.phases || [];
    const steps = phases.map((p) => {
      const active = p.state === "running";
      const node = appendAll(el("li", { class: "swm-step" }), [
        el("span", { class: "swm-step-role", text: p.role || "" }),
        el("span", {
          class: "swm-step-meta",
          text: active
            ? "working"
            : `${count(p.turns)} turn(s), ${count(p.tokens)} tok`,
        }),
        p.bound_by === "role"
          ? el("span", { class: "swm-step-why", text: "handed off" })
          : p.bound_by === "global"
            ? el("span", { class: "swm-step-why", text: "hit the shared budget" })
            : p.stop_reason
              ? el("span", { class: "swm-step-why", text: String(p.stop_reason) })
              : null,
      ]);
      node.dataset.state = p.state || "";
      if (p.writes) node.dataset.writes = "1";
      return node;
    });
    return appendAll(el("ol", { class: "swm-steps" }), steps);
  }
}

/* --------------------------------------------------------------- account */

/** The terminal entry in the transcript: what the relay did and what it is
 *  worth. This is the product. */
export function account(report, accountText, blind, loopBlind) {
  const r = report || {};
  const tone = STOP_TONE[r.stop_reason] || "warn";
  const ceilings = r.ceilings || {};

  const head = appendAll(el("div", { class: "lac-head" }), [
    icon(r.stop_reason === "completed" ? "i-check" : "i-alert"),
    el("strong", { text: headline(r) }),
  ]);

  const nodes = [head];
  if (r.stop_detail) nodes.push(el("div", { class: "lac-detail", text: r.stop_detail }));

  // ---- who did what. The brief's "explain itself" requirement, mostly. ----
  const phases = r.phases || [];
  if (phases.length) {
    nodes.push(el("div", { class: "lac-sub", text: "Who did what, in order:" }));
    nodes.push(appendAll(el("ul", { class: "lac-list swm-phases" }), phases.map((p) =>
      appendAll(el("li", {}), [
        el("code", { class: "mono", text: `${p.role || ""}` }),
        el("span", {
          text: ` cycle ${count(p.cycle)}, ${count(p.turns)} turn(s), `
              + `${count(p.tokens)} tok, ended ${p.stop_reason || "unknown"}`
              + (p.bound_by === "role" ? " (handed off)" : ""),
        }),
      ]))));
  }

  // ---- the plan and the critique, both untrusted model prose ----
  if (r.plan) {
    nodes.push(el("div", { class: "lac-sub", text: "The plan the planner produced:" }));
    nodes.push(blob(String(r.plan)));
  }
  if (r.last_review) {
    nodes.push(el("div", { class: "lac-sub", text: "What the reviewer said last:" }));
    nodes.push(blob(String(r.last_review)));
  }

  const completion = r.completion || {};
  if (completion.detail) {
    nodes.push(el("div", {
      class: "lac-sub", text: `Completion check: ${completion.detail}`,
    }));
    if (completion.output && !completion.done) nodes.push(blob(String(completion.output)));
  }

  // ---- ONE row of meters ----
  nodes.push(appendAll(el("div", { class: "lac-spend" }), [
    meter("Turns (all roles)", r.turns, ceilings.max_turns),
    meter("Wall clock", r.elapsed, ceilings.max_seconds, { format: hms }),
    meter("Tokens (all roles)", r.tokens, ceilings.max_tokens),
    meter("Unattended writes", r.writes, ceilings.max_writes, { scary: true }),
    meter("Tool calls", r.tool_calls, ceilings.max_tool_calls),
    meter("Relay cycles", r.cycles, ceilings.max_cycles),
  ]));

  if (r.swaps > 0) {
    nodes.push(el("div", {
      class: "lac-note",
      text: `${count(r.swaps)} model swap(s) cost ${hms(r.swap_seconds)} of wall clock.`,
    }));
  }
  if (r.lease_refusals > 0) {
    nodes.push(el("div", {
      class: "lac-note",
      text: `${count(r.lease_refusals)} write attempt(s) by a read-only role were `
          + "refused by the write lease.",
    }));
  }

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
          + "against this workspace when the relay stopped waiting for them.",
    }));
  }

  // The honesty line. Rendered BEFORE the blind spots, because it is the one
  // that applies to this specific run rather than to the shape in general.
  if (r.reviewer_approved && !r.verified) {
    nodes.push(el("div", {
      class: "lac-warn",
      text: "REVIEWED BUT NOT VERIFIED. A reviewer role read this work and did "
          + "not object. Nothing ran it. The reviewer is the same kind of model "
          + "as the implementer, reading code it cannot execute, so its approval "
          + "is an opinion and not a check.",
    }));
  }
  if (completion.done && completion.verified === false) {
    nodes.push(el("div", {
      class: "lac-warn",
      text: "NOTHING VERIFIED THIS. No completion check was configured, so "
          + "'finished' here is the model's own claim about its own work.",
    }));
  }

  if (accountText) {
    nodes.push(appendAll(el("details", { class: "lac-full" }), [
      el("summary", { text: "Full account" }),
      blob(String(accountText)),
    ]));
  }

  nodes.push(blindSpots(blind));
  if (loopBlind && loopBlind.length) {
    nodes.push(blindSpots(loopBlind, {
      title: "And what progress detection cannot tell you, in every role",
    }));
  }

  return appendAll(el("div", { class: "lac lac-" + tone }), nodes);
}

export function headline(r) {
  switch (r.stop_reason) {
    case "completed":
      return (r.completion && r.completion.verified === false)
        ? `Finished in ${count(r.turns)} turn(s), but nothing verified it`
        : `Finished the goal in ${count(r.turns)} turn(s) across ${count(r.cycles)} cycle(s)`;
    case "ceiling": return "Stopped at the shared ceiling";
    case "exhausted": return "Every role had its turn and it is still not done";
    case "cancelled": return "Stopped by you";
    case "blocked": return "Stopped needing permission";
    case "error": return "Stopped on an error";
    default: return "Stopped";
  }
}
