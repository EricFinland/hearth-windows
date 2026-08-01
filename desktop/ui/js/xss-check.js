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
];

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
