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
 * Open http://127.0.0.1:4173/xss-check.html with the dev host running.
 */

import { renderProse, blob } from "./safe-text.js";
import { Transcript } from "./transcript.js";
import { el } from "./dom.js";
import {
  renderModelCard, renderQuantRow, renderDownloadRow, renderDownloads,
  renderSourceNotice, renderHardware, renderVerdict,
} from "./shop.js";

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

const verdict = document.getElementById("verdict");
verdict.className = "verdict " + (failures === 0 ? "pass" : "fail");
verdict.textContent = failures === 0
  ? `PASS - ${rows.childElementCount} checks, every payload inert, window.__xssFired === false`
  : `FAIL - ${failures} of ${rows.childElementCount} checks let markup through`;

// Read by the harness that drives this page.
window.__xssCheck = { failures, total: rows.childElementCount, fired: window.__xssFired };
