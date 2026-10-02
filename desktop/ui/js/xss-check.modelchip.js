/* Untrusted-content check for the model chip (js/model-chip.js).
 *
 * The chip shows a model's name, and that name is a GGUF file name or an
 * Ollama tag: whoever published the model chose it, and a downloaded file
 * can be called anything the filesystem allows. The panel also shows
 * sentences from the sidecar (the busy reason, the Ollama note, an error
 * body), which carry text from further away still. So hostile strings are
 * planted in every one of those fields at once and driven through the REAL
 * exported render functions, renderChip and renderPanel, and the resulting
 * DOM is inspected.
 *
 * Two properties, the same two xss-check.js holds every other surface to:
 *   1. markup stays text: no element the payload named exists, no on*
 *      attribute exists, nothing fired, and the payload's characters are
 *      present (so a pass cannot be earned by dropping the content);
 *   2. text does not lie: a U+202E in a model name reaches the page as the
 *      visible marker "<U+202E>", in the text AND in the tooltip, never as
 *      the raw control character.
 *
 * Self-contained: its own section, its own verdict, its own global
 * (window.__xssCheckModelChip), so it neither depends on nor disturbs the
 * main harness. Open xss-check.html with the dev host running.
 */

import { el } from "./dom.js";
import { renderChip, renderPanel } from "./model-chip.js";

window.__xssFired = window.__xssFired || false;

const PAYLOADS = [
  `<img src=x onerror="window.__xssFired=true">`,
  `<script>window.__xssFired=true</` + `script>`,
  `<svg onload="window.__xssFired=true"></svg>`,
  `"><img src=x onerror=alert(1)><span a="`,
  `<a href="javascript:window.__xssFired=true">click</a>`,
];
const BANNED = new Set(["IMG", "SCRIPT", "IFRAME", "OBJECT", "EMBED", "STYLE", "LINK", "A", "FORM"]);
const RLO = "‮";

const section = el("section", { class: "mchip-xss" }, [
  el("h2", { text: "model chip" }),
]);
const verdict = el("div", { class: "verdict", id: "verdict-modelchip", text: "running..." });
const rows = el("tbody");
section.appendChild(verdict);
section.appendChild(el("table", {}, [
  el("thead", {}, [el("tr", {}, [
    el("th", { text: "Surface" }), el("th", { text: "Payload" }), el("th", { text: "Result" }),
  ])]),
  rows,
]));
const host = el("div", { class: "probe" });
section.appendChild(host);
(document.querySelector(".wrap") || document.body).appendChild(section);

let failures = 0;

function injected(root) {
  const bad = [];
  for (const node of [root, ...root.querySelectorAll("*")]) {
    if (BANNED.has(node.tagName)) bad.push(node.tagName.toLowerCase());
    for (const attr of node.attributes) {
      if (attr.name.toLowerCase().startsWith("on")) bad.push(`${node.tagName.toLowerCase()}[${attr.name}]`);
    }
  }
  return bad;
}

function allTitles(root) {
  return [root, ...root.querySelectorAll("[title]")]
    .map((node) => node.getAttribute("title") || "").join("\n");
}

function record(surface, payload, ok, detail) {
  if (!ok) failures++;
  rows.appendChild(el("tr", {}, [
    el("td", { text: surface }),
    el("td", { class: "p", text: payload }),
    el("td", { class: "r " + (ok ? "ok" : "bad"), text: (ok ? "PASS" : "FAIL") + " - " + detail }),
  ]));
}

function hostile(text) {
  return {
    backend: "llama", managed_by: "hearth", model: text, ref: "gguf:" + text,
    status: "loaded", loaded: true, loading: false,
    memory: { vram_bytes: 4 * 1024 ** 3, rss_bytes: 1024 ** 3, approximate: true },
    inflight: 0, busy: true, busy_reason: text,
    auto_unload_minutes: 15, auto_unload_options: [5, 15, text, null],
    last_used_at: Date.now() / 1000 - 120, unload_at: Date.now() / 1000 + 600,
    note: text,
  };
}

function render(snapshot) {
  const box = el("div");
  host.appendChild(box);
  box.appendChild(renderChip(snapshot));
  box.appendChild(renderPanel(snapshot, { running: false, error: snapshot.note }));
  return box;
}

for (const payload of PAYLOADS) {
  const box = render(hostile(payload));
  const bad = injected(box);
  const inText = box.textContent.includes(payload);
  const inTitle = allTitles(box).includes(payload);
  const ok = bad.length === 0 && inText && inTitle && window.__xssFired === false;
  record("chip + panel", payload, ok,
    bad.length ? `created ${bad.join(", ")}`
      : !inText ? "payload missing from the text (altered or dropped)"
      : !inTitle ? "payload missing from the tooltip"
      : window.__xssFired ? "a payload executed" : "inert text");
}

{
  const name = `model${RLO}fuggg.exe`;
  const box = render(hostile(name));
  const text = box.textContent + "\n" + allTitles(box);
  const marked = text.includes("model<U+202E>fuggg.exe");
  const raw = text.includes(RLO);
  record("bidi override in a model name", "model<U+202E>fuggg.exe", marked && !raw,
    raw ? "the raw U+202E reached the page" : marked ? "shown as a visible marker" : "marker missing");
}

{
  // An unrecognised status from the wire is shown as "Unknown", never echoed.
  const snapshot = hostile("m.gguf");
  snapshot.status = PAYLOADS[0];
  const box = render(snapshot);
  const ok = injected(box).length === 0 && box.textContent.includes("Unknown");
  record("unrecognised status", PAYLOADS[0], ok, ok ? "rendered as Unknown" : "status echoed or injected");
}

verdict.className = "verdict " + (failures === 0 ? "pass" : "fail");
verdict.textContent = failures === 0
  ? "model chip: every hostile string stayed inert text"
  : `model chip: ${failures} check(s) FAILED`;

window.__xssCheckModelChip = { failures, fired: window.__xssFired };
