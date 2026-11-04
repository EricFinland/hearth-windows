/* Untrusted-content check for the Tools screen (js/mcp.js).
 *
 * Everything that screen shows is someone else's text. A server's name,
 * command, arguments and environment names come from mcp.json, which anything
 * running as this user can write; tool names, descriptions, the server's own
 * description of itself and every error string come from the server process.
 * So hostile values are planted in ALL of those fields at once and driven
 * through the same exported render functions McpPanel uses, never through
 * lookalikes, and a single missed `text:` shows up as a failure rather than
 * being hidden by the fields around it that were done right.
 *
 * Two properties per surface, the same two xss-check.js checks:
 *   1. INERT: no element the payload named exists, no on* attribute exists,
 *      the payload's characters are present as text, nothing executed.
 *   2. HONEST: a bidi override or zero-width character inside a server's
 *      command shows as a visible <U+XXXX> marker and is not present raw.
 *      That matters most on the "let this program run?" dialog, which is the
 *      one place the user decides whether a program runs at all.
 *
 * Its own section, its own verdict and window.__xssCheckMcp, so this file can
 * be added and removed without touching the main harness.
 */

import {
  renderServerCard, renderTestResult, renderToolList, renderAcknowledgeBody,
  renderCommandLine, renderEnvList, ServerEditor,
} from "./mcp.js";
import { el } from "./dom.js";

window.__xssFired = window.__xssFired || false;

const PAYLOADS = [
  `<img src=x onerror="window.__xssFired=true">`,
  `<script>window.__xssFired=true</` + `script>`,
  `<svg onload="window.__xssFired=true"></svg>`,
  `"><img src=x onerror=alert(1)><span a="`,
  `</code></div><iframe src="javascript:window.__xssFired=true"></iframe>`,
  `<a href="javascript:window.__xssFired=true">click</a>`,
  `<style>body{display:none}</style>`,
];

const BANNED_TAGS = new Set(["IMG", "SCRIPT", "IFRAME", "OBJECT", "EMBED", "STYLE", "LINK", "A", "FORM"]);

const wrap = document.querySelector(".wrap") || document.body;
const section = el("section", { class: "mcp-xss" });
const verdict = el("div", { class: "verdict", text: "running..." });
const rows = el("tbody");
const stage = el("div", { class: "probe" });
section.appendChild(el("h2", { text: "Tools screen (MCP servers)" }));
section.appendChild(verdict);
section.appendChild(el("table", {}, [
  el("thead", {}, [el("tr", {}, [el("th", { text: "Surface" }), el("th", { text: "Payload" }), el("th", { text: "Result" })])]),
  rows,
]));
section.appendChild(stage);
wrap.appendChild(section);

let failures = 0;

function report(surface, payload, ok, detail) {
  if (!ok) failures++;
  rows.appendChild(el("tr", {}, [
    el("td", { text: surface }),
    el("td", { class: "p", text: payload }),
    el("td", { class: "r " + (ok ? "ok" : "bad"), text: (ok ? "PASS" : "FAIL") + " - " + detail }),
  ]));
}

function injected(root) {
  const bad = [];
  for (const node of root.querySelectorAll("*")) {
    if (BANNED_TAGS.has(node.tagName)) { bad.push(node.tagName.toLowerCase()); continue; }
    for (const attr of node.attributes) {
      if (attr.name.toLowerCase().startsWith("on")) bad.push(`${node.tagName.toLowerCase()}[${attr.name}]`);
    }
  }
  return bad;
}

function probe(build) {
  const host = el("div");
  stage.appendChild(host);
  build(host);
  return host;
}

function checkInert(surface, payload, host, { expectText = true } = {}) {
  const bad = injected(host);
  const literal = !expectText || host.textContent.includes(payload);
  const ok = bad.length === 0 && literal && window.__xssFired === false;
  report(surface, payload, ok, bad.length ? `created ${bad.join(", ")}`
    : !literal ? "payload text missing from the DOM (altered or dropped)"
      : window.__xssFired ? "a payload executed" : "inert text");
}

/** A server whose every text field carries the payload. */
function hostileServer(p) {
  return {
    key: p, name: "x", prefix: "mcp__" + p + "__", valid: true, problem: null,
    command: p, args: [p, { masked: true, hint: p.slice(0, 2), length: 40, index: 1 }],
    env: [{ name: p, masked: false, value: p }, { name: "TOKEN", masked: true, hint: p.slice(0, 2), length: 9 }],
    enabled: true, cwd: p, timeout: 30, risk_overrides: 1,
    warnings: [p], status: "error", error: p, guarded: true, tool_count: 1,
    tools: [{ name: p, full_name: p, risk: "dangerous", derived: "safe", description: p }],
    test: {
      state: "ok", ok: true, elapsed: 0.4, protocol: p, guarded: false, tool_count: 1,
      server_info: { name: p, version: p }, instructions: p,
      tools: [{ name: p, full_name: p, risk: p, derived: p, description: p }],
    },
  };
}

for (const p of PAYLOADS) {
  // Every fold open, so the tool lists inside them are rendered and checked.
  const open = new Set(["live:" + p, "test:" + p]);
  checkInert("server card (every field)", p, probe((h) => h.appendChild(renderServerCard(hostileServer(p), {}, open))));
  checkInert("invalid entry card", p, probe((h) => h.appendChild(renderServerCard(
    { key: p, valid: false, problem: p, command: p, args: [], env: [], status: "invalid", prefix: p }, {}, new Set()))));
  checkInert("test result (ok)", p, probe((h) => h.appendChild(renderTestResult(hostileServer(p).test, new Set(["test:k"]), "k"))));
  checkInert("test result (failed)", p, probe((h) => h.appendChild(renderTestResult({ state: "failed", error: p, elapsed: 2 }))));
  checkInert("tool list", p, probe((h) => h.appendChild(renderToolList(hostileServer(p).tools))));
  checkInert("command line pills", p, probe((h) => h.appendChild(renderCommandLine(p, [p, p]))));
  checkInert("environment list", p, probe((h) => h.appendChild(renderEnvList([{ name: p, value: p }]))));
  checkInert("run-a-program dialog", p, probe((h) => {
    const { nodes } = renderAcknowledgeBody({ command: p, args: [p], env_names: [p], warnings: [p], create: true });
    for (const n of nodes) h.appendChild(n);
  }));
  // The editor puts stored values into form fields, where they are a field's
  // value rather than displayed text; the check is that no element appears
  // inside it. Inspected from the editor's own <form>, which is legitimately
  // a form and is not counted against it.
  const editor = new ServerEditor(hostileServer(p));
  probe((h) => h.appendChild(editor.root));
  checkInert("edit form", p, editor.root, { expectText: false });
}

/* Display honesty on the surfaces that decide whether a program runs. */
const BIDI = [
  { name: "RLO inside a command", text: "C:\\tools\\server\u202Eexe.txt", marker: "<U+202E>", raw: "\u202E" },
  { name: "RLI isolate in an argument", text: "--root \u2067C:\\\u2069", marker: "<U+2067>", raw: "\u2067" },
  { name: "zero-width space in a name", text: "git\u200Bhub", marker: "<U+200B>", raw: "\u200B" },
];

for (const b of BIDI) {
  const surfaces = [
    ["run-a-program dialog", (h) => { for (const n of renderAcknowledgeBody({ command: b.text, args: [b.text], env_names: [b.text] }).nodes) h.appendChild(n); }],
    ["server card", (h) => h.appendChild(renderServerCard(hostileServer(b.text), {}, new Set(["live:" + b.text]))) ],
    ["tool list", (h) => h.appendChild(renderToolList([{ name: b.text, risk: "safe", description: b.text }]))],
  ];
  for (const [label, build] of surfaces) {
    const host = probe(build);
    const shown = host.textContent;
    const ok = shown.includes(b.marker) && !shown.includes(b.raw);
    report(label + " (display order)", b.name, ok, ok ? "shown as " + b.marker
      : shown.includes(b.raw) ? "the raw control character reached the page" : "no visible marker");
  }
}

/* The editor must never display a masked value, because it never has one. */
{
  const server = hostileServer("ok");
  const editor = new ServerEditor(server);
  const body = editor.read();
  const keptArg = body.args.some((a) => a && typeof a === "object" && a.keep === 1);
  const keptEnv = body.env.TOKEN && body.env.TOKEN.keep === true;
  report("edit form (masked values)", "a masked arg and env value", keptArg && keptEnv,
    keptArg && keptEnv ? "saved back as keep sentinels, never as text" : "a masked value was not kept: " + JSON.stringify(body));
}

const total = rows.childElementCount;
verdict.className = "verdict " + (failures === 0 ? "pass" : "fail");
verdict.textContent = failures === 0
  ? `PASS - ${total} checks on the Tools screen, every payload inert, every control character visible`
  : `FAIL - ${failures} of ${total} Tools screen checks let markup through or hid a control character`;

// Read by the harness that drives this page.
window.__xssCheckMcp = { failures, total, fired: window.__xssFired };
