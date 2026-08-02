/* Untrusted-content regression check for desktop/ui.
 *
 * Model output, tool output, file bodies, and tool-call arguments are all
 * attacker-influenceable: a local model can be induced to emit markup, and a
 * file the agent reads can simply contain it. This drives real payloads
 * through every rendering path the UI has for that content and then inspects
 * the resulting DOM, rather than asserting the property by reading the source.
 *
 * A surface passes only when all three hold:
 *   1. no element the payload named exists in the rendered subtree,
 *   2. no attribute whose name starts with "on" exists there,
 *   3. the payload's exact characters ARE present as text (so a pass cannot be
 *      earned by silently dropping the content instead of rendering it), and
 *      window.__xssFired is still false.
 *
 * THE SECOND CLASS, which this file used to miss entirely. Text that cannot
 * become structure can still lie about itself. A U+202E RIGHT-TO-LEFT OVERRIDE
 * inside a tool argument reorders the glyphs on screen while leaving every
 * executed byte alone, so the approval card -- the user's only defence against
 * a hostile or prompt-injected model -- displays a harmless command and runs a
 * hostile one. The bidi section below drives those payloads through the same
 * render paths and asserts on GEOMETRY: it takes a Range over each character,
 * reads its on-screen x, and compares the resulting reading order against the
 * string. Asserting on textContent alone cannot detect this, because
 * textContent is the half of the discrepancy that is telling the truth.
 *
 * The same section checks that the fix did not overreach. Hebrew and Arabic
 * are measured the same way and must still come out right to left, because the
 * Unicode bidi ALGORITHM is not the problem and is left alone; only the
 * explicit override controls are neutralized.
 *
 * Open http://127.0.0.1:4173/xss-check.html with the dev host running.
 */

import { renderProse, blob, labelledBlob } from "./safe-text.js";
import { Transcript } from "./transcript.js";
import { el } from "./dom.js";
import {
  renderModelCard, renderQuantRow, renderDownloadRow, renderDownloads,
  renderSourceNotice, renderHardware, renderVerdict,
} from "./shop.js";
import {
  account as loopAccount, LoopRunBar, LoopConfigPanel, blindSpots,
} from "./loop.js";
import {
  account as swarmAccount, SwarmRunBar, roleCard,
  blindSpots as swarmBlindSpots,
} from "./swarm.js";

// Any payload that manages to execute sets this. It must stay false.
window.__xssFired = false;

const PAYLOADS = [
  `<img src=x onerror=alert(1)>`,
  `<img src=x onerror="window.__xssFired=true">`,
  `<script>window.__xssFired=true</` + `script>`,
  `<svg onload="window.__xssFired=true"></svg>`,
  `<iframe src="javascript:window.__xssFired=true"></iframe>`,
  `"><img src=x onerror=alert(1)><span a="`,
  `</p></div><img src=x onerror=alert(1)><div><p>`,
  `<a href="javascript:window.__xssFired=true">click</a>`,
  `<style>body{display:none}</style>`,
  `<object data="javascript:alert(1)"></object>`,
];

const BANNED_TAGS = new Set(["IMG", "SCRIPT", "IFRAME", "OBJECT", "EMBED", "STYLE", "LINK", "A", "FORM"]);

/** An element counts as injected if the payload named it, or if it carries any
 *  event-handler attribute. The icons this UI builds itself are <svg>/<use>
 *  carrying only class, aria-hidden, and href, so they never trip this. */
function findInjected(root) {
  const bad = [];
  for (const node of root.querySelectorAll("*")) {
    if (BANNED_TAGS.has(node.tagName)) { bad.push(node.tagName.toLowerCase()); continue; }
    for (const attr of node.attributes) {
      if (attr.name.toLowerCase().startsWith("on")) bad.push(`${node.tagName.toLowerCase()}[${attr.name}]`);
    }
  }
  return bad;
}

const rows = document.getElementById("rows");
const probes = document.getElementById("probes");
const stage = document.getElementById("transcript");
const transcript = new Transcript(stage);

let failures = 0;

function check(surface, payload, container) {
  const injected = findInjected(container);
  const literal = container.textContent.includes(payload);
  const ok = injected.length === 0 && literal && window.__xssFired === false;
  if (!ok) failures++;
  const detail = injected.length ? `created ${injected.join(", ")}`
    : !literal ? "payload text missing from the DOM (altered or dropped)"
    : window.__xssFired ? "a payload executed"
    : "inert text";
  rows.appendChild(el("tr", {}, [
    el("td", { text: surface }),
    el("td", { class: "p", text: payload }),
    el("td", { class: "r " + (ok ? "ok" : "bad"), text: (ok ? "PASS" : "FAIL") + " - " + detail }),
  ]));
}

/** Render one payload into its own attached-but-hidden host, so the inspection
 *  sees exactly what that one call produced. Attached rather than detached
 *  because an <img> only actually fetches (and fires onerror) once it is in the
 *  document, and this check should fail loudly if that ever becomes possible. */
function probe(build) {
  const host = el("div", { class: "probe" });
  probes.appendChild(host);
  build(host);
  return host;
}

for (const payload of PAYLOADS) {
  check("renderProse", payload, probe((host) => host.appendChild(renderProse(payload))));
  check("blob", payload, probe((host) => host.appendChild(blob(payload))));
  check("fenced code block", payload,
    probe((host) => host.appendChild(renderProse("```html\n" + payload + "\n```"))));
  check("inline code span", payload,
    probe((host) => host.appendChild(renderProse("look: `" + payload + "` done"))));
}

// ---------------------------------------------------------------------------
// The model shop.
//
// Everything a shop listing carries is chosen by whoever uploaded the
// repository: the repo id, its label, the author, the tags, every GGUF
// filename, and -- when the Hub returns an error for a repository -- the error
// string too. hearth_shop passes all of it through untouched, correctly, so
// this UI is the only place it can be made inert. Each payload is driven
// through the same render functions shop.js's live view calls, not through
// lookalikes, and it is planted in every one of those fields at once so a
// single missed `text:` shows up as a failure rather than being masked by the
// nine neighbouring fields that were done right.
// ---------------------------------------------------------------------------

function hostileQuant(payload, extra = {}) {
  return {
    name: payload,
    path: payload,
    quant: payload,
    size_bytes: 4_400_000_000,
    complete: true,
    recommended: true,
    split_part_count: null,
    projector: false,
    disk_ok: true,
    local: { present: false, partial_bytes: 1_100_000_000, parts_present: 0 },
    alternate_editions: [],
    verdict: {
      verdict: payload,           // an unrecognised grade must render as text
      message: payload,
      requested_context_tokens: 8192,
      max_context_tokens: 4424,
      required_bytes: 5_000_000_000,
      headroom_bytes: -600_000_000,
      vram_approximate: true,
    },
    ...extra,
  };
}

function hostileEntry(payload) {
  const quant = hostileQuant(payload);
  return {
    source: "live",
    repo_id: payload,
    label: payload,
    author: payload,
    description: payload,
    focus: payload,
    downloads: 12345,
    likes: 67,
    params_b: 7,
    gated: false,
    gated_mode: payload,
    downloadable: true,
    quants_loaded: true,
    quants: [quant, hostileQuant(payload, { recommended: false })],
    best_quant: quant,
    verdict: quant.verdict,
    files_error: payload,
  };
}

function hostileJob(payload, status) {
  return {
    id: "dl-x",
    repo_id: payload,
    filename: payload,
    label: payload,
    quant: payload,
    status,
    bytes_done: 1_100_000_000,
    bytes_total: 4_400_000_000,
    fraction: 0.25,
    speed_bytes_per_sec: 18_000_000,
    eta_seconds: 160,
    part_index: 1,
    part_count: 3,
    resumed_from: 900_000_000,
    queue_position: null,
    path: payload,
    error: payload,
    error_kind: payload,
    verified: false,
    verification: payload,
    already_present: false,
    cancellable: status === "downloading",
  };
}

for (const payload of PAYLOADS) {
  check("shop model card", payload,
    probe((host) => host.appendChild(renderModelCard(hostileEntry(payload), {}))));
  check("shop quantisation row", payload,
    probe((host) => host.appendChild(
      renderQuantRow(hostileEntry(payload), hostileQuant(payload), {}))));
  check("shop verdict", payload,
    probe((host) => host.appendChild(renderVerdict(hostileQuant(payload).verdict))));
  for (const status of ["downloading", "error", "cancelled", "done"]) {
    check(`shop download row (${status})`, payload,
      probe((host) => host.appendChild(renderDownloadRow(hostileJob(payload, status), {}))));
  }
  check("shop downloads panel", payload,
    probe((host) => host.appendChild(
      renderDownloads([hostileJob(payload, "downloading")], {}))));
  check("shop fallback banner", payload,
    probe((host) => host.appendChild(renderSourceNotice({
      source: "fallback", notice: payload, error: payload,
    }))));
  // A gated repository renders a different branch (no download button, a
  // licence explanation) and has to be inert on that branch too.
  check("shop gated model card", payload,
    probe((host) => host.appendChild(renderModelCard(
      { ...hostileEntry(payload), gated: true, downloadable: false, gated_mode: "manual" }, {}))));
  // hearth_shop's fallback listing: entries whose repo_id is null and whose
  // description and label come from the built-in catalog, rendered by the
  // same card with a different badge.
  check("shop reference-catalog card", payload,
    probe((host) => host.appendChild(renderModelCard(
      { ...hostileEntry(payload), source: "fallback", repo_id: null,
        downloadable: false, quants_loaded: false, quants: [], best_quant: null }, {}))));
}

// The hardware bar is rendered from the sidecar's own hardware probe rather
// than from the Hub, but it crosses the same JSON boundary, so a string where
// a number belongs must still be inert rather than trusted.
check("shop hardware bar", PAYLOADS[0],
  probe((host) => host.appendChild(renderHardware({
    gpu_detected: true, gpu_vendor: PAYLOADS[0], vram_bytes: 16 * 1024 ** 3,
    ram_bytes: 24 * 1024 ** 3, free_disk_bytes: 500 * 1024 ** 3, context_tokens: 8192,
    vram_approximate: true,
  }))));

// ---------------------------------------------------------------------------
// The work loop.
//
// Every string a loop surface renders is written by something that is not the
// user: the GOAL is echoed back from a run that may have been resumed out of a
// journal on disk, the stop detail and the recurring-error list are built from
// tool output and from the completion command's stdout, the file lists are
// paths the model chose, and the notices are the loop's own prose about what
// it found. The `pending` card is the worst of them -- its goal, its refusal
// text and its turn counts are read off a JSONL file in the user's data
// directory that the agent's own write_file can reach.
//
// As with the shop, the payload is planted in every field at once, so one
// missed `text:` fails rather than being masked by its neighbours.
// ---------------------------------------------------------------------------

function hostileReport(payload) {
  return {
    run_id: payload, goal: payload, model: payload, workspace: payload,
    mode: "auto", stop_reason: "stalled", stop_detail: payload,
    turns: 6, elapsed: 91.5, tokens_in: 10, tokens_out: 20, tokens: 30,
    writes: 4, tool_calls: 9, gates_denied: 2,
    denied_tools: { [payload]: 2 },
    checkpoints: [payload], checkpoint_error: payload,
    ceilings: { max_turns: 40, max_seconds: 7200, max_tokens: 1000000,
                max_writes: 200, max_tool_calls: 400 },
    stall_policy: { window: 8, repeat_actions: 4, repeat_errors: 8,
                    oscillations: 3, min_turns: 3 },
    // Which patience setting judged this run. Comes back out of a journal on
    // disk on a resumed run, so it is model-reachable like everything else
    // here.
    patience: payload,
    verdict: { stalled: true, reason: payload,
               detectors: [{ name: payload, detail: payload }] },
    recurring_errors: [[4, payload], [2, payload]],
    last_error: payload,
    completion: { done: false, available: true, detail: payload, output: payload },
    created: [payload], modified: [payload], deleted: [payload],
    turn_log: [{ turn: 1, new_state: false, summary: payload, errors: [payload] }],
    notices: [payload], live_workers: 2,
  };
}

function hostileRun(payload, state) {
  return {
    run_id: payload, goal: payload, model: payload, workspace: payload,
    mode: "auto", state, resumed: true, started_at: Date.now() / 1000,
    elapsed: 12, turn: 3,
    spend: { turns: 3, elapsed: 12, tokens: 40, writes: 2, tool_calls: 5 },
    ceilings: { max_turns: 40, max_seconds: 7200, max_tokens: 1000000,
                max_writes: 200, max_tool_calls: 400 },
    stall: { window: 6, repeat_actions: 4, repeat_errors: 5,
             oscillations: 3, min_turns: 3 },
    patience: payload,
    gate_policy: "ask", gate_timeout: 300, allowed_tools: [payload],
    done_command: payload, required_artifacts: [payload], verified: true,
    gates_allowed: 1, gates_denied: 1, gates_timed_out: 1, checkpoints: 3,
    last_progress: { turn: 3, new_state: false, changed: 0, errors: 1 },
    last_tool: payload, stop_requested: state === "stopping",
    stop_reason: state === "stopped" ? "stalled" : undefined,
    stop_detail: state === "stopped" ? payload : undefined,
    live_workers: 2,
  };
}

/* GET /loop's `defaults`: everything the bounds form is drawn from. The
 * patience list is the newest part of it -- a name, a description and the
 * price of choosing it, per setting -- and all three reach the page as text
 * from the sidecar. */
function hostileLoopDefaults(payload) {
  return {
    config: {
      ceilings: { max_turns: 40, max_seconds: 7200, max_tokens: 1000000,
                  max_writes: 200, max_tool_calls: 400 },
      stall: { window: 8, repeat_actions: 4, repeat_errors: 8,
               oscillations: 3, min_turns: 3 },
      patience: payload, gate_policy: "deny", gate_timeout: 300,
      allowed_tools: [payload], done_command: null, required_artifacts: null,
    },
    tools: [{ name: payload, risk: "edit", effect: { auto: "allow", plan: "deny" } }],
    gate_policies: ["deny", "stop", "ask"],
    modes: ["plan", "auto"],
    patience: [
      { name: payload, label: payload, summary: payload, cost: payload,
        settings: { window: 4, repeat_actions: 3, repeat_errors: 4,
                    oscillations: 2, min_turns: 2 } },
      { name: "balanced", label: payload, summary: payload, cost: payload,
        settings: { window: 8, repeat_actions: 4, repeat_errors: 8,
                    oscillations: 3, min_turns: 3 } },
    ],
    default_patience: "balanced",
    limits: {
      ceilings: { max_turns: [1, 5000], max_seconds: [1, 604800],
                  max_tokens: [1, 1000000000], max_writes: [1, 20000],
                  max_tool_calls: [1, 100000] },
      stall: { window: [0, 1000], repeat_actions: [0, 1000],
               repeat_errors: [0, 1000], oscillations: [0, 1000],
               min_turns: [0, 1000] },
      gate_timeout_seconds: [5, 3600],
    },
  };
}

function hostilePending(payload, resumable) {
  return {
    run_id: payload, goal: payload, completed_turns: 4, interrupted_turn: 5,
    resumable, refusal: payload, journal_goal_differs: true,
  };
}

const HOSTILE_BLIND = [{ headline: PAYLOADS[0], means: PAYLOADS[1], remedy: PAYLOADS[2] }];

for (const payload of PAYLOADS) {
  check("work loop account", payload,
    probe((host) => host.appendChild(
      loopAccount(hostileReport(payload), payload, HOSTILE_BLIND))));
  for (const state of ["running", "stopping", "stopped"]) {
    check(`work loop run bar (${state})`, payload,
      probe((host) => new LoopRunBar(host).render({
        run: hostileRun(payload, state), pending: null,
        blind_spots: HOSTILE_BLIND,
      })));
  }
  // The inherited-run card, both branches: one that offers to resume and one
  // that refuses with a reason lifted straight out of the journal header.
  for (const resumable of [true, false]) {
    check(`work loop pending card (resumable=${resumable})`, payload,
      probe((host) => new LoopRunBar(host).render({
        run: null, pending: hostilePending(payload, resumable),
        blind_spots: HOSTILE_BLIND,
      })));
  }
  check("work loop blind spots", payload,
    probe((host) => host.appendChild(blindSpots(
      [{ headline: payload, means: payload, remedy: payload }]))));

  // The bounds form itself. Its labels and its patience descriptions arrive
  // from the sidecar over the same HTTP the rest of this file distrusts, and
  // a run restored from a journal on disk supplies `config.patience` -- a
  // string the agent's own write_file could have put there. The form is what
  // a person reads before leaving a model running unattended on their files,
  // so it is exactly the wrong place for text that can lie about itself.
  check("work loop config panel", payload,
    probe((host) => {
      const panel = new LoopConfigPanel(host);
      panel.render(hostileLoopDefaults(payload), HOSTILE_BLIND);
      panel.applyConfig({
        ceilings: { max_turns: 5, max_seconds: 60, max_tokens: 100,
                    max_writes: 2, max_tool_calls: 9 },
        stall: { window: 3, repeat_actions: 3, repeat_errors: 3,
                 oscillations: 3, min_turns: 3 },
        patience: payload, gate_policy: "deny", gate_timeout: 300,
        allowed_tools: [payload], done_command: payload,
        required_artifacts: [payload],
      });
    }));

  // ---- the agent swarm. Every field here is model output: a relay's plan
  // and its reviewer's critique are prose a local model wrote, the role names
  // and phase stop reasons ride back out of a journal on disk, and the
  // account is what a person reads to decide whether to trust the result.
  check("swarm account", payload,
    probe((host) => host.appendChild(swarmAccount(
      hostileSwarmReport(payload), payload, HOSTILE_BLIND, HOSTILE_BLIND))));
  for (const state of ["running", "stopping", "stopped"]) {
    check("swarm run bar (" + state + ")", payload,
      probe((host) => new SwarmRunBar(host).render({
        run: hostileSwarmRun(payload, state), pending: null,
        blind_spots: HOSTILE_BLIND,
      })));
  }
  for (const resumable of [true, false]) {
    check("swarm pending card (resumable=" + resumable + ")", payload,
      probe((host) => new SwarmRunBar(host).render({
        run: null, pending: hostileSwarmPending(payload, resumable),
        blind_spots: HOSTILE_BLIND,
      })));
  }
  check("swarm role card", payload,
    probe((host) => host.appendChild(roleCard(hostileRoles(payload)))));
  check("swarm blind spots", payload,
    probe((host) => host.appendChild(swarmBlindSpots(
      [{ headline: payload, means: payload, remedy: payload }]))));
}

/* A relay report with the payload in every untrusted field at once, so one
 * missed `text:` fails rather than being masked by its neighbours. */
function hostileSwarmReport(payload) {
  return {
    run_id: payload, goal: payload, model: payload, workspace: payload,
    mode: "auto", stop_reason: "exhausted", stop_detail: payload,
    cycles: 2, turns: 9, elapsed: 90, tokens: 500, writes: 3, tool_calls: 12,
    ceilings: { max_turns: 40, max_seconds: 7200, max_tokens: 1000000,
                max_writes: 200, max_tool_calls: 400, max_cycles: 3 },
    phases: [{ index: 1, cycle: 1, role: payload, model: payload, turns: 2,
               tokens: 100, stop_reason: payload, bound_by: "role" }],
    plan: payload, last_review: payload, reviewer_approved: true,
    verified: false,
    completion: { done: false, verified: true, detail: payload, output: payload },
    created: [payload], modified: [payload], deleted: [payload],
    notices: [payload], live_workers: 2, swaps: 1, swap_seconds: 3,
    lease_refusals: 1,
  };
}

function hostileSwarmRun(payload, state) {
  return {
    run_id: payload, goal: payload, model: payload, workspace: payload,
    mode: "auto", state, resumed: true, started_at: Date.now() / 1000,
    elapsed: 12, cycle: 1, phase: 2, role: payload, turn: 3,
    spend: { turns: 3, elapsed: 12, tokens: 40, writes: 2, tool_calls: 5 },
    ceilings: { max_turns: 40, max_seconds: 7200, max_tokens: 1000000,
                max_writes: 200, max_tool_calls: 400, max_cycles: 3 },
    phases: [{ index: 1, cycle: 1, role: payload, model: payload,
               state: "done", turns: 2, tokens: 90, stop_reason: payload,
               bound_by: "global", writes: true }],
    verified: false, reviewer_approved: true,
    swaps: 1, swap_seconds: 4, lease_refusals: 2,
    stop_reason: state === "stopped" ? "exhausted" : undefined,
    stop_detail: state === "stopped" ? payload : undefined,
    live_workers: 2,
  };
}

function hostileSwarmPending(payload, resumable) {
  return {
    run_id: payload, goal: payload, completed_phases: 4, interrupted_phase: 5,
    resumable, refusal: payload, journal_goal_differs: true,
  };
}

function hostileRoles(payload) {
  return [
    { name: payload, purpose: payload, writes: true, max_turns: 12,
      tools: [{ name: payload, risk: "edit", effect: { auto: "allow" } }] },
    { name: payload, purpose: payload, writes: false, max_turns: 3,
      tools: [{ name: payload, risk: "safe", effect: { auto: "allow" } }] },
  ];
}

// A completed run takes a different headline branch, and an unverified
// completion adds a sentence -- both must be inert too.
for (const payload of [PAYLOADS[0], PAYLOADS[5]]) {
  check("work loop account (completed, unverified)", payload,
    probe((host) => host.appendChild(loopAccount({
      ...hostileReport(payload), stop_reason: "completed",
      completion: { done: true, verified: false, detail: payload },
    }, payload, HOSTILE_BLIND))));
}

// The live transcript surfaces, rendered into the visible stage so the result
// can be read by eye as well as asserted.
transcript.addUser(PAYLOADS[0]);

// Assistant text now arrives as a token stream, so it is fed here the way the
// sidecar feeds it: many small deltas carrying stream_id and index, with the
// payloads deliberately CUT ACROSS delta boundaries. That is the new thing
// streaming introduced and the reason this file drives the transcript rather
// than only safe-text.js -- a payload that is inert when rendered whole could
// in principle escape if the renderer ever reassembled fragments through
// anything but a text node. Splitting at 7 characters guarantees every
// payload below is torn apart, including across the code fence.
const AGENT_MESSAGE =
  "Here is what I found:\n\n```html\n" + PAYLOADS[1] + "\n```\n\nAnd inline " + PAYLOADS[5];
const CHUNK = 7;
let at = 0;
while (at < AGENT_MESSAGE.length) {
  transcript.appendAgent(AGENT_MESSAGE.slice(at, at + CHUNK), { streamId: 1, index: at });
  at += CHUNK;
}
// A reconnect replays deltas the page has already drawn. Re-feeding the tail
// with its original index must be a no-op, not a second copy -- and it must
// not be a no-op achieved by dropping the payload, which the literal-text
// assertions below would catch either way.
transcript.appendAgent(AGENT_MESSAGE.slice(AGENT_MESSAGE.length - CHUNK),
                       { streamId: 1, index: AGENT_MESSAGE.length - CHUNK });
transcript.closeAgent();
transcript.addToolCall({
  tool: "write_file",
  args: { path: "evil" + PAYLOADS[6], content: PAYLOADS[2] },
  decision: "allow",
});
transcript.addToolResult({ tool: "write_file", output: PAYLOADS[3] });
transcript.addApproval({
  id: "appr-test",
  tool: "write_file",
  args: { path: "notes.md", content: PAYLOADS[4] },
  injection_finding: {
    severity: "high", category: "instruction_override",
    matched: PAYLOADS[7], explanation: PAYLOADS[8],
  },
  secrets_finding: {
    severity: "high", kind: "test", masked: "ab****yz",
    context: PAYLOADS[9], reason: PAYLOADS[0],
  },
}, () => {});
transcript.addNotice("warn", PAYLOADS[0], PAYLOADS[2], PAYLOADS[4]);

// Nothing here is deferred any more -- the paced reveal this used to wait out
// is gone, and every render path above is synchronous. One turn of the event
// loop, so a failure here can never be blamed on timing.
await new Promise((done) => setTimeout(done, 0));

for (const payload of [PAYLOADS[0], PAYLOADS[1], PAYLOADS[2], PAYLOADS[3],
                       PAYLOADS[4], PAYLOADS[6], PAYLOADS[7], PAYLOADS[9]]) {
  check("live transcript (message, tool card, approval card, notice)", payload, stage);
}

// ===========================================================================
// Display fidelity: bidi controls and C0/C1 controls.
//
// Everything above asks whether untrusted text can become STRUCTURE. This asks
// whether the text on screen is the text that is there, which is a separate
// property and the one the approval card actually depends on. It is asserted
// on geometry rather than on textContent, because in this attack textContent
// is the honest half: the bytes are unchanged and only the glyph order lies.
// ===========================================================================

const bidiProbes = document.getElementById("bidi-probes");

/** The left-to-right reading order of what is painted.
 *
 *  Takes a Range over each character in turn and reads the rectangle the
 *  engine gave it, then sorts by line and then by x. That is the order a
 *  person's eye goes in, and it is the only way to observe it: a reordered
 *  string and a correct one have identical textContent by construction.
 *  Zero-advance characters are skipped, since a rectangle with no width has
 *  no position to compare. */
function visualOrder(node) {
  const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
  const glyphs = [];
  let cursor;
  while ((cursor = walker.nextNode())) {
    const data = cursor.data;
    for (let i = 0; i < data.length; i++) {
      const range = document.createRange();
      range.setStart(cursor, i);
      range.setEnd(cursor, i + 1);
      const rect = range.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) continue;
      glyphs.push({ ch: data[i], x: rect.x, line: Math.round(rect.y) });
    }
  }
  glyphs.sort((a, b) => (a.line - b.line) || (a.x - b.x));
  return glyphs.map((g) => g.ch).join("");
}

/** Drop the two zero-width joiners before comparing orders. They are
 *  deliberately NOT neutralized (Persian and Hindi spelling need them) and
 *  they have no width, so their x is ambiguous and carries no information
 *  about whether anything was reordered. */
function spacing(text) {
  return text.replace(/[\u200C\u200D]/g, "");
}

/* One entry per character class the fix covers. `text` is what a model or a
 * Hugging Face field could contain; `ch` is the character that must not
 * survive as itself; `marker` is what the user must see instead. */
const BIDI_PAYLOADS = [
  { name: "U+202E RLO (the Trojan Source command)", ch: "\u202E", marker: "<U+202E>",
    text: "git status # \u202Edda/ ssap resu ten ;" },
  { name: "U+202D LRO", ch: "\u202D", marker: "<U+202D>",
    text: "rm -rf \u202D/tmp/safe" },
  { name: "U+202B RLE", ch: "\u202B", marker: "<U+202B>",
    text: "echo \u202Bhello world" },
  { name: "U+202A LRE", ch: "\u202A", marker: "<U+202A>",
    text: "echo \u202Ahello world" },
  { name: "U+202C PDF (a lone terminator)", ch: "\u202C", marker: "<U+202C>",
    text: "cat notes.txt\u202C && whoami" },
  { name: "U+2067 RLI isolate", ch: "\u2067", marker: "<U+2067>",
    text: "npm test \u2067dda/ ssap resu ten ;" },
  { name: "U+2069 PDI (a lone terminator)", ch: "\u2069", marker: "<U+2069>",
    text: "ls -la\u2069 /etc/shadow" },
  { name: "U+200F RIGHT-TO-LEFT MARK", ch: "\u200F", marker: "<U+200F>",
    text: "curl example.com\u200F ; sudo sh" },
  { name: "U+061C ARABIC LETTER MARK", ch: "\u061C", marker: "<U+061C>",
    text: "git push\u061C --force" },
  { name: "U+001B ESC (starts an ANSI sequence)", ch: "\u001B", marker: "<U+001B>",
    text: "build ok \u001B[2K\u001B[1G and nothing else happened" },
  { name: "U+0008 BACKSPACE", ch: "\u0008", marker: "<U+0008>",
    text: "safe_command\u0008\u0008\u0008\u0008rm -rf /" },
  { name: "U+0000 NUL", ch: "\u0000", marker: "<U+0000>",
    text: "read_file config\u0000.bak" },
  { name: "U+0085 NEL (a C1 control)", ch: "\u0085", marker: "<U+0085>",
    text: "line one\u0085line two" },
  { name: "U+200B ZERO WIDTH SPACE", ch: "\u200B", marker: "<U+200B>",
    text: "rm\u200B -rf /home/user" },
  { name: "U+FEFF BOM", ch: "\uFEFF", marker: "<U+FEFF>",
    text: "\uFEFF#!/bin/sh" },
];

/* A shop listing carrying `text` in every attacker-controlled field at once.
 * Same shape as hostileEntry above, kept separate so the selectors below can
 * pick out one field at a time. */
function bidiEntry(text) {
  const quant = {
    name: text, path: text, quant: text, size_bytes: 4400000000,
    complete: true, recommended: true, disk_ok: true,
    local: { present: false, partial_bytes: 0 },
    verdict: { verdict: "great", message: text, requested_context_tokens: 8192 },
  };
  return {
    source: "live", repo_id: text, label: text, author: text, description: text,
    downloads: 42, likes: 7, gated: false, downloadable: true,
    quants_loaded: true, quants: [quant], best_quant: quant, files_error: text,
  };
}

function bidiJob(text) {
  return {
    id: "dl-b", repo_id: text, filename: text, label: text, status: "error",
    bytes_done: 1000, bytes_total: 4000, fraction: 0.25,
    error: text, error_kind: text, cancellable: false,
  };
}

/* Every render path the neutralisation had to be threaded through. `build`
 * renders into a host; `selector` names the single element that is supposed to
 * carry the string, so the geometry assertion measures one field rather than a
 * whole card's layout. */
const BIDI_SURFACES = [
  { label: "renderProse paragraph", selector: ".prose p",
    build: (host, text) => host.appendChild(renderProse(text)) },
  { label: "blob (tool output, file bodies)", selector: "pre.blob",
    build: (host, text) => host.appendChild(blob(text)) },
  { label: "labelled blob", selector: "pre.blob",
    build: (host, text) => host.appendChild(labelledBlob("content", text)) },
  { label: "fenced code block", selector: "pre.code-block code",
    build: (host, text) => host.appendChild(renderProse("```sh\n" + text + "\n```")) },
  { label: "inline code span", selector: ".code-inline",
    build: (host, text) => host.appendChild(renderProse("run `" + text + "` now")) },
  { label: "user message bubble", selector: ".msg-user .bubble",
    build: (host, text) => new Transcript(host).addUser(text) },
  { label: "approval card subtitle", selector: ".card-approval .card-sub",
    build: (host, text) => new Transcript(host).addApproval(
      { id: "b", tool: "run_command", args: { command: text } }, () => {}) },
  { label: "approval card command payload", selector: ".card-approval pre.blob",
    build: (host, text) => new Transcript(host).addApproval(
      { id: "b", tool: "run_command", args: { command: text } }, () => {}) },
  { label: "approval card finding excerpt", selector: ".finding pre.blob",
    build: (host, text) => new Transcript(host).addApproval({
      id: "b", tool: "write_file", args: { path: "a.txt", content: "x" },
      injection_finding: { severity: "high", matched: text, explanation: text },
    }, () => {}) },
  { label: "tool card subtitle", selector: ".card:not(.card-approval) .card-sub",
    build: (host, text) => new Transcript(host).addToolCall(
      { tool: "run_command", args: { command: text }, decision: "allow" }) },
  { label: "tool card argument row", selector: ".kv-v",
    build: (host, text) => new Transcript(host).addToolCall(
      { tool: "read_file", args: { path: text }, decision: "allow" }) },
  { label: "tool result output", selector: ".card pre.blob",
    build: (host, text) => {
      const t = new Transcript(host);
      t.addToolCall({ tool: "read_file", args: { path: "a.txt" }, decision: "allow" });
      t.addToolResult({ tool: "read_file", output: text });
    } },
  { label: "notice", selector: ".notice-text",
    build: (host, text) => new Transcript(host).addNotice("warn", "heads up", text) },
  { label: "streaming deltas, mid stream", selector: ".msg-agent .prose p",
    build: (host, text) => {
      const t = new Transcript(host);
      for (let at = 0; at < text.length; at += 3) {
        t.appendAgent(text.slice(at, at + 3), { streamId: 9, index: at });
      }
    } },
  { label: "streaming deltas, message closed", selector: ".msg-agent .prose p",
    build: (host, text) => {
      const t = new Transcript(host);
      for (let at = 0; at < text.length; at += 3) {
        t.appendAgent(text.slice(at, at + 3), { streamId: 9, index: at });
      }
      t.closeAgent();
    } },
  { label: "shop model name", selector: ".model-name",
    build: (host, text) => host.appendChild(renderModelCard(bidiEntry(text), {})) },
  { label: "shop repository id", selector: ".model-repo",
    build: (host, text) => host.appendChild(renderModelCard(bidiEntry(text), {})) },
  { label: "shop model description", selector: ".model-desc",
    build: (host, text) => host.appendChild(renderModelCard(bidiEntry(text), {})) },
  { label: "shop files_error note", selector: ".model-note.is-error",
    build: (host, text) => host.appendChild(renderModelCard(bidiEntry(text), {})) },
  { label: "shop quantisation name", selector: ".quant-name",
    build: (host, text) => host.appendChild(
      renderQuantRow(bidiEntry(text), bidiEntry(text).best_quant, {})) },
  { label: "shop quantisation filename", selector: ".quant-file",
    build: (host, text) => host.appendChild(
      renderQuantRow(bidiEntry(text), bidiEntry(text).best_quant, {})) },
  { label: "shop verdict message", selector: ".verdict-msg",
    build: (host, text) => host.appendChild(
      renderVerdict(bidiEntry(text).best_quant.verdict)) },
  { label: "shop download name", selector: ".dl-name",
    build: (host, text) => host.appendChild(renderDownloadRow(bidiJob(text), {})) },
  { label: "shop download detail", selector: ".dl-detail",
    build: (host, text) => host.appendChild(renderDownloadRow(bidiJob(text), {})) },
  { label: "shop fallback banner", selector: ".shop-banner p",
    build: (host, text) => host.appendChild(
      renderSourceNotice({ source: "fallback", notice: text, error: text })) },
  { label: "shop hardware bar", selector: ".hw-v",
    build: (host, text) => host.appendChild(renderHardware({
      gpu_detected: true, gpu_vendor: text, context_tokens: 8192 })) },
  // The work loop. The account is what a person reads to decide whether to
  // trust hours of unsupervised work, and the run bar's goal line is echoed
  // back from a run that may have been resumed out of a file on disk. A stop
  // detail whose glyphs are reordered would misreport why a run ended;
  // a reordered goal would misreport what it was doing at all.
  { label: "loop account stop detail", selector: ".lac-detail",
    build: (host, text) => host.appendChild(loopAccount(
      { ...bidiReport(text) }, "", [])) },
  { label: "loop account detector detail", selector: ".lac-detectors li span",
    build: (host, text) => host.appendChild(loopAccount(bidiReport(text), "", [])) },
  { label: "loop account recurring error", selector: ".lac-recurring li .mono",
    build: (host, text) => host.appendChild(loopAccount(bidiReport(text), "", [])) },
  { label: "loop account last error", selector: ".lac pre.blob",
    build: (host, text) => host.appendChild(loopAccount(bidiReport(text), "", [])) },
  { label: "loop account created files", selector: ".lac-files .mono",
    build: (host, text) => host.appendChild(loopAccount(bidiReport(text), "", [])) },
  { label: "loop account notice", selector: ".lac-note",
    build: (host, text) => host.appendChild(loopAccount(bidiReport(text), "", [])) },
  { label: "loop account full text", selector: ".lac-full pre.blob",
    build: (host, text) => host.appendChild(loopAccount(bidiReport(text), text, [])) },
  { label: "loop run bar goal", selector: ".lrb-goal",
    build: (host, text) => new LoopRunBar(host).render(
      { run: bidiRun(text), pending: null }) },
  { label: "loop run bar stop detail", selector: ".lrb-detail",
    build: (host, text) => new LoopRunBar(host).render(
      { run: bidiRun(text), pending: null }) },
  { label: "loop pending goal (read off a journal on disk)", selector: ".lrb-goal",
    build: (host, text) => new LoopRunBar(host).render(
      { run: null, pending: bidiPending(text) }) },
  { label: "loop pending refusal", selector: ".lrb-pending .lrb-warn",
    build: (host, text) => new LoopRunBar(host).render(
      { run: null, pending: bidiPending(text) }) },
  { label: "loop blind spot headline", selector: ".lbs-item strong",
    build: (host, text) => host.appendChild(blindSpots(
      [{ headline: text, means: text, remedy: text }])) },
  // The agent swarm. A reordered plan or critique would misreport what a role
  // actually said, and the phase strip is the only place "which role did what"
  // is answered -- a reordered role name there attributes work to the wrong
  // agent, which is precisely the account this feature exists to provide.
  { label: "swarm account stop detail", selector: ".lac-detail",
    build: (host, text) => host.appendChild(swarmAccount(
      bidiSwarmReport(text), "", [], [])) },
  { label: "swarm account plan", selector: ".lac pre.blob",
    build: (host, text) => host.appendChild(swarmAccount(
      bidiSwarmReport(text), "", [], [])) },
  { label: "swarm account phase attribution", selector: ".swm-phases li span",
    build: (host, text) => host.appendChild(swarmAccount(
      bidiSwarmReport(text), "", [], [])) },
  { label: "swarm account created files", selector: ".lac-files .mono",
    build: (host, text) => host.appendChild(swarmAccount(
      bidiSwarmReport(text), "", [], [])) },
  { label: "swarm account notice", selector: ".lac-note",
    build: (host, text) => host.appendChild(swarmAccount(
      bidiSwarmReport(text), "", [], [])) },
  { label: "swarm account full text", selector: ".lac-full pre.blob",
    build: (host, text) => host.appendChild(swarmAccount(
      bidiSwarmReport(text), text, [], [])) },
  { label: "swarm run bar goal", selector: ".lrb-goal",
    build: (host, text) => new SwarmRunBar(host).render(
      { run: bidiSwarmRun(text), pending: null }) },
  { label: "swarm run bar active role", selector: ".lrb-title strong",
    build: (host, text) => new SwarmRunBar(host).render(
      { run: bidiSwarmRun(text), pending: null }) },
  { label: "swarm phase strip role", selector: ".swm-step-role",
    build: (host, text) => new SwarmRunBar(host).render(
      { run: bidiSwarmRun(text), pending: null }) },
  { label: "swarm pending goal (read off a journal on disk)", selector: ".lrb-goal",
    build: (host, text) => new SwarmRunBar(host).render(
      { run: null, pending: bidiSwarmPending(text) }) },
  { label: "swarm pending refusal", selector: ".lrb-pending .lrb-detail",
    build: (host, text) => new SwarmRunBar(host).render(
      { run: null, pending: bidiSwarmPending(text) }) },
  { label: "swarm role name", selector: ".swm-role-name",
    build: (host, text) => host.appendChild(roleCard(
      [{ name: text, purpose: text, writes: true, max_turns: 3, tools: [] }])) },
  { label: "swarm role purpose", selector: ".swm-role-purpose",
    build: (host, text) => host.appendChild(roleCard(
      [{ name: text, purpose: text, writes: false, max_turns: 3, tools: [] }])) },
  { label: "swarm blind spot headline", selector: ".swm-blind .lbs-item strong",
    build: (host, text) => host.appendChild(swarmBlindSpots(
      [{ headline: text, means: text, remedy: text }])) },
];

function bidiSwarmReport(text) {
  return {
    run_id: "r", goal: text, model: "m", workspace: "w", mode: "auto",
    stop_reason: "exhausted", stop_detail: text, cycles: 1, turns: 3,
    elapsed: 10, tokens: 5, writes: 1, tool_calls: 2,
    ceilings: { max_turns: 40, max_seconds: 7200, max_tokens: 1000,
                max_writes: 200, max_tool_calls: 400, max_cycles: 3 },
    phases: [{ index: 1, cycle: 1, role: text, turns: 1, tokens: 5,
               stop_reason: text, bound_by: "role" }],
    plan: text, last_review: "", reviewer_approved: false, verified: false,
    completion: {}, created: [text], modified: [], deleted: [],
    notices: [text], live_workers: 0, swaps: 0, lease_refusals: 0,
  };
}

function bidiSwarmRun(text) {
  return {
    run_id: "r", goal: text, model: "m", workspace: "w", mode: "auto",
    state: "running", resumed: false, started_at: Date.now() / 1000,
    elapsed: 5, cycle: 1, phase: 1, role: text, turn: 1,
    spend: { turns: 1, elapsed: 5, tokens: 9, writes: 0, tool_calls: 1 },
    ceilings: { max_turns: 40, max_seconds: 7200, max_tokens: 1000,
                max_writes: 200, max_tool_calls: 400, max_cycles: 3 },
    phases: [{ index: 1, cycle: 1, role: text, state: "running", turns: 1,
               tokens: 9, writes: true }],
    verified: false, reviewer_approved: false, swaps: 0, lease_refusals: 0,
    live_workers: 0,
  };
}

/* resumable:false deliberately. The refusal string is the untrusted one --
 * it is lifted verbatim out of a journal header on disk -- and it is only
 * rendered on the NOT-resumable branch. With resumable:true the selector
 * matches the fixed "send resume" line instead, and the check would pass
 * while testing a constant. */
function bidiSwarmPending(text) {
  return {
    run_id: "r", goal: text, completed_phases: 1, interrupted_phase: 2,
    resumable: false, refusal: text, journal_goal_differs: false,
  };
}

function bidiReport(text) {
  return {
    run_id: "r", goal: text, model: "m", workspace: "w", mode: "auto",
    stop_reason: "stalled", stop_detail: text, turns: 3, elapsed: 10,
    tokens: 5, writes: 1, tool_calls: 2,
    ceilings: { max_turns: 40, max_seconds: 7200, max_tokens: 1000,
                max_writes: 200, max_tool_calls: 400 },
    verdict: { detectors: [{ name: "repeat_action", detail: text }] },
    recurring_errors: [[3, text]], last_error: text,
    completion: {}, created: [text], modified: [], deleted: [],
    notices: [text], live_workers: 0,
  };
}

function bidiRun(text) {
  return {
    run_id: "r", goal: text, mode: "auto", state: "stopped",
    stop_reason: "stalled", stop_detail: text, turn: 3,
    spend: { turns: 3, elapsed: 10, tokens: 5, writes: 1, tool_calls: 2 },
    ceilings: { max_turns: 40, max_seconds: 7200, max_tokens: 1000,
                max_writes: 200, max_tool_calls: 400 },
    allowed_tools: [], verified: false, live_workers: 0, checkpoints: 0,
  };
}

function bidiPending(text) {
  return {
    run_id: "r", goal: text, completed_turns: 2, interrupted_turn: 3,
    resumable: false, refusal: text, journal_goal_differs: false,
  };
}

function bidiHost(label) {
  const host = el("div", { class: "bidi-probe" }, [el("div", { class: "bidi-label", text: label })]);
  bidiProbes.appendChild(host);
  return host;
}

let bidiRows = 0;

function bidiRow(surfaceLabel, payloadLabel, ok, detail) {
  if (!ok) failures++;
  bidiRows++;
  rows.appendChild(el("tr", {}, [
    el("td", { text: surfaceLabel }),
    el("td", { class: "p", text: payloadLabel }),
    el("td", { class: "r " + (ok ? "ok" : "bad"), text: (ok ? "PASS" : "FAIL") + " - " + detail }),
  ]));
}

for (const surface of BIDI_SURFACES) {
  for (const payload of BIDI_PAYLOADS) {
    const host = bidiHost(surface.label + " -- " + payload.name);
    surface.build(host, payload.text);
    const node = host.querySelector(surface.selector);
    let ok = false;
    let detail;
    if (!node) {
      detail = "selector matched nothing: " + surface.selector;
    } else {
      const shown = node.textContent;
      const order = visualOrder(node);
      const rawSurvived = shown.includes(payload.ch);
      const markerShown = shown.includes(payload.marker);
      // Everything either side of the control character must still be there, so
      // a pass cannot be earned by dropping the string instead of showing it.
      const contextKept = payload.text.split(payload.ch)
        .every((part) => part === "" || shown.includes(part));
      const orderHonest = spacing(order) === spacing(shown);
      ok = !rawSurvived && markerShown && contextKept && orderHonest;
      // Every failed condition is reported, not just the first. The geometry
      // assertion is the one that cannot be replaced by reading textContent,
      // so it has to be visible in the failure text rather than short
      // circuited away by a textContent check that happened to fail first.
      const faults = [];
      if (rawSurvived) faults.push("the raw control character reached the page");
      if (!markerShown) faults.push("silently stripped: no " + payload.marker + " for the user to see");
      if (!contextKept) faults.push("the surrounding text was dropped");
      if (!orderHonest) faults.push("DISPLAY ORDER DIFFERS FROM THE TEXT: seen " + JSON.stringify(order));
      detail = ok ? "shown as " + payload.marker + ", display order matches the text"
        : faults.join("; ");
    }
    bidiRow(surface.label, payload.name, ok, detail);
  }
}

/* The other half of "do not regress legitimate use".
 *
 * A fix that forced everything left to right, or that stripped anything that
 * looked unusual, would sail through the section above and wreck real content.
 * These are measured with the same ruler. Left-to-right samples must read in
 * byte order; right-to-left samples must NOT, because the bidi algorithm is
 * supposed to reverse them and is deliberately left alone. */
const LEGITIMATE = [
  { name: "windows path", rtl: false,
    text: "C:\\Users\\eric\\OneDrive\\Desktop\\hearth\\agent\\hearth_loop.py" },
  { name: "C source line", rtl: false,
    text: "if (n < 3 && m > 4) { printf(\"%s\\n\", &p[i]); } // a=b?c:d" },
  { name: "shell command with quotes and pipes", rtl: false,
    text: "grep -rn 'def main' . | head -20 && echo \"done\"" },
  { name: "hebrew", rtl: true, text: "\u05e9\u05dc\u05d5\u05dd \u05e2\u05d5\u05dc\u05dd" },
  { name: "arabic", rtl: true,
    text: "\u0645\u0631\u062d\u0628\u0627 \u0628\u0627\u0644\u0639\u0627\u0644\u0645" },
  { name: "persian needing U+200C ZWNJ", rtl: true,
    text: "\u0645\u06cc\u200c\u0631\u0648\u062f" },
];

const LEGIT_SURFACE_LABELS = new Set([
  "renderProse paragraph", "blob (tool output, file bodies)",
  "approval card subtitle", "approval card command payload",
  "tool card argument row", "shop model name", "shop model description",
]);

for (const surface of BIDI_SURFACES.filter((s) => LEGIT_SURFACE_LABELS.has(s.label))) {
  for (const sample of LEGITIMATE) {
    const host = bidiHost(surface.label + " -- " + sample.name + " (must be unharmed)");
    surface.build(host, sample.text);
    const node = host.querySelector(surface.selector);
    let ok = false;
    let detail;
    if (!node) {
      detail = "selector matched nothing: " + surface.selector;
    } else {
      const shown = node.textContent;
      const order = visualOrder(node);
      const intact = shown.includes(sample.text);   // nothing rewritten or dropped
      const forward = spacing(order) === spacing(shown);
      const reversed = spacing(order).includes(spacing([...sample.text].reverse().join("")));
      ok = intact && (sample.rtl ? reversed : forward);
      detail = !intact ? "the text was altered: " + JSON.stringify(shown)
        : sample.rtl
          ? (reversed ? "still reads right to left, the bidi algorithm is intact"
                      : "RIGHT-TO-LEFT TEXT NO LONGER REVERSES: seen " + JSON.stringify(order))
          : (forward ? "reads in byte order, unchanged"
                     : "REORDERED: seen " + JSON.stringify(order));
    }
    bidiRow(surface.label, "legitimate: " + sample.name, ok, detail);
  }
}

const verdict = document.getElementById("verdict");
verdict.className = "verdict " + (failures === 0 ? "pass" : "fail");
verdict.textContent = failures === 0
  ? `PASS - ${rows.childElementCount} checks (${bidiRows} of them display-order), `
    + `every payload inert, every glyph where its bytes say it is, `
    + `window.__xssFired === false`
  : `FAIL - ${failures} of ${rows.childElementCount} checks let markup through, `
    + `or let the screen disagree with the text`;

// Read by the harness that drives this page.
window.__xssCheck = {
  failures, total: rows.childElementCount, bidi: bidiRows, fired: window.__xssFired,
};
