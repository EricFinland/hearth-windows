/* The diff component: a file change drawn as lines of text, never as markup.
 *
 * Two places use it, and they mean the same thing by it. An approval card for
 * write_file / edit_file / replace_in_files gets a `diff` on its
 * approval_request event (agent/hearth_diff.py, computed by the sidecar
 * against the file actually on disk), and the restore dialog fetches one from
 * GET /checkpoints/diff. Both arrive in one shape, already structured into
 * hunks and tagged lines, so nothing here parses a diff: there is no unified
 * diff string anywhere on this side, and so no "@@" line a hostile file could
 * forge to make the card misnumber itself.
 *
 * Every string a file contributes (its path, every line, every note) goes
 * through dom.js's el()/textNode(), which set textContent and neutralize bidi
 * and control characters. That matters more here than almost anywhere else:
 * this IS the approval card's body now, and a U+202E inside a line of code
 * would otherwise show the user one line and write another. xss-check.html
 * drives hostile payloads and every bidi control through this file.
 *
 * Line numbers and the +/- sign are drawn by CSS from data attributes
 * (diff.css), not as text, so selecting and copying a run of lines copies the
 * code and nothing else.
 *
 * Big diffs stay fast because most of a diff is never drawn until asked for:
 *   - unchanged context beyond SHOW_CONTEXT lines either side of a change is
 *     folded behind a "show N unchanged lines" button (the sidecar sends a
 *     few more lines than are shown, which is how far expanding can reach);
 *   - at most INITIAL_ROWS rows are drawn across the whole diff, then each
 *     file offers its remainder in MORE_ROWS steps.
 * The sidecar caps what it sends as well; when it did, the summary says so.
 */

import { el, appendAll } from "./dom.js";

const SHOW_CONTEXT = 3;
const INITIAL_ROWS = 400;
const MORE_ROWS = 400;

const STATUS_LABEL = {
  added: "new file",
  deleted: "deleted",
  modified: "modified",
  unchanged: "no change",
};

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function toCount(value) {
  return Number.isInteger(value) && value >= 0 ? value : null;
}

/** One hunk's lines as numbered row objects. Anything malformed in the event
 *  degrades to a context line rather than throwing: a card that fails to draw
 *  is a card that cannot be approved or denied. */
function hunkLines(hunk) {
  let oldNo = toCount(hunk.old_start) ?? 1;
  let newNo = toCount(hunk.new_start) ?? 1;
  const items = [];
  for (const raw of Array.isArray(hunk.lines) ? hunk.lines : []) {
    if (!Array.isArray(raw)) continue;
    const tag = raw[0] === "+" || raw[0] === "-" ? raw[0] : " ";
    const text = typeof raw[1] === "string" ? raw[1] : String(raw[1] ?? "");
    const cut = toCount(raw[2]) ?? 0;
    items.push({
      kind: "line", tag, text, cut,
      oldNo: tag === "+" ? null : oldNo,
      newNo: tag === "-" ? null : newNo,
    });
    if (tag !== "+") oldNo += 1;
    if (tag !== "-") newNo += 1;
  }
  return { items, oldEnd: oldNo };
}

/** Fold runs of unchanged lines down to SHOW_CONTEXT either side of a change.
 *  A fold that would hide a single line is not worth a button, so short runs
 *  are left alone. */
function foldContext(items) {
  const out = [];
  let i = 0;
  while (i < items.length) {
    if (items[i].tag !== " ") { out.push(items[i]); i += 1; continue; }
    let j = i;
    while (j < items.length && items[j].tag === " ") j += 1;
    const keepHead = i === 0 ? 0 : SHOW_CONTEXT;
    const keepTail = j === items.length ? 0 : SHOW_CONTEXT;
    const hidden = (j - i) - keepHead - keepTail;
    if (hidden >= 2) {
      out.push(...items.slice(i, i + keepHead));
      out.push({ kind: "fold", lines: items.slice(i + keepHead, j - keepTail) });
      out.push(...items.slice(j - keepTail, j));
    } else {
      out.push(...items.slice(i, j));
    }
    i = j;
  }
  return out;
}

/** Every row a file draws, in order: gaps between hunks, folds, lines. */
function fileRows(file) {
  const rows = [];
  let previousEnd = null;
  for (const hunk of Array.isArray(file.hunks) ? file.hunks : []) {
    if (!hunk || typeof hunk !== "object") continue;
    const start = toCount(hunk.old_start) ?? 1;
    const gap = previousEnd === null ? start - 1 : start - previousEnd;
    if (gap > 0 && file.status !== "added") rows.push({ kind: "gap", count: gap });
    const { items, oldEnd } = hunkLines(hunk);
    rows.push(...foldContext(items));
    const oldCount = toCount(hunk.old_count);
    previousEnd = oldCount === null ? oldEnd : start + oldCount;
  }
  return rows;
}

function rowWeight(row) {
  return row.kind === "fold" ? row.lines.length : row.kind === "line" ? 1 : 0;
}

function lineNode(item) {
  const kind = item.tag === "+" ? "dv-add" : item.tag === "-" ? "dv-del" : "dv-ctx";
  const line = el("div", { class: "dv-line " + kind }, [
    el("span", { class: "dv-ln", "data-ln": item.oldNo ?? "", "aria-hidden": "true" }),
    el("span", { class: "dv-ln", "data-ln": item.newNo ?? "", "aria-hidden": "true" }),
    el("span", { class: "dv-sign", "aria-hidden": "true" }),
    el("span", { class: "dv-text", text: item.text }),
  ]);
  if (item.cut > 0) {
    line.appendChild(el("span", {
      class: "dv-cut",
      text: `(${plural(item.cut, "more character")} on this line, not shown)`,
    }));
  }
  return line;
}

function rowNode(row) {
  if (row.kind === "line") return lineNode(row);
  if (row.kind === "gap") {
    return el("div", { class: "dv-gap", text: `${plural(row.count, "unchanged line")} not shown` });
  }
  const button = el("button", {
    class: "dv-fold", type: "button",
    text: `Show ${plural(row.lines.length, "unchanged line")}`,
  });
  button.addEventListener("click", () => {
    const frag = document.createDocumentFragment();
    for (const item of row.lines) frag.appendChild(lineNode(item));
    button.replaceWith(frag);
  });
  return button;
}

/** Draw rows[from..] into `body` until `allowance` lines are spent. Returns the
 *  index of the first row not drawn. */
function drawRows(body, rows, from, allowance) {
  const frag = document.createDocumentFragment();
  let i = from;
  let spent = 0;
  while (i < rows.length && (spent < allowance || rowWeight(rows[i]) === 0)) {
    frag.appendChild(rowNode(rows[i]));
    spent += Math.max(rowWeight(rows[i]), 1);
    i += 1;
  }
  body.appendChild(frag);
  return i;
}

function remainingLines(rows, from) {
  let n = 0;
  for (let i = from; i < rows.length; i++) n += rowWeight(rows[i]);
  return n;
}

/** A "show more" control that keeps drawing this file's rows in steps. */
function moreButton(body, rows, from, label) {
  const button = el("button", { class: "dv-more", type: "button", text: label });
  button.addEventListener("click", () => {
    button.remove();
    const next = drawRows(body, rows, from, MORE_ROWS);
    if (next < rows.length) {
      body.appendChild(moreButton(body, rows, next,
        `Show ${plural(remainingLines(rows, next), "more line")}`));
    }
  });
  return button;
}

function countsNode(file) {
  const added = toCount(file.added);
  const removed = toCount(file.removed);
  if (added === null || removed === null) return el("span", { class: "dv-counts" });
  return appendAll(el("span", { class: "dv-counts" }), [
    el("span", { class: "dv-plus", text: `+${added}` }),
    el("span", { class: "dv-minus", text: `-${removed}` }),
  ]);
}

function fileNode(file, budget) {
  const status = Object.prototype.hasOwnProperty.call(STATUS_LABEL, file.status)
    ? file.status : "modified";
  const head = appendAll(el("summary", { class: "dv-file-head" }), [
    el("span", { class: "dv-status dv-status-" + status, text: STATUS_LABEL[status] }),
    el("span", { class: "dv-path", text: String(file.path ?? "(unnamed file)") }),
    countsNode(file),
  ]);
  const section = el("details", { class: "dv-file", open: true }, [head]);

  for (const note of Array.isArray(file.notes) ? file.notes : []) {
    section.appendChild(el("p", { class: "dv-note", text: String(note) }));
  }
  if (file.hidden_reason) {
    section.appendChild(el("p", { class: "dv-hidden", text: String(file.hidden_reason) }));
  }
  const redacted = toCount(file.redacted_lines);
  if (redacted) {
    section.appendChild(el("p", {
      class: "dv-note dv-note-key",
      text: `${plural(redacted, "line")} had a credential-shaped value replaced with [REDACTED] in this preview. The write itself is not changed.`,
    }));
  }

  const rows = fileRows(file);
  if (rows.length) {
    // Gutter as wide as this file's largest line number and no wider, so a
    // ten-line file does not give up six characters of code width to it.
    let widest = 0;
    for (const row of rows) {
      for (const item of row.kind === "fold" ? row.lines : row.kind === "line" ? [row] : []) {
        widest = Math.max(widest, item.oldNo ?? 0, item.newNo ?? 0);
      }
    }
    section.style.setProperty("--dv-ln-w", `${Math.max(2, String(widest).length) + 1}ch`);
    const body = el("div", { class: "dv-body" });
    section.appendChild(body);
    if (budget.left > 0) {
      const next = drawRows(body, rows, 0, budget.left);
      budget.left -= remainingLines(rows, 0) - remainingLines(rows, next);
      if (next < rows.length) {
        body.appendChild(moreButton(body, rows, next,
          `Show ${plural(remainingLines(rows, next), "more line")}`));
      }
    } else {
      body.appendChild(moreButton(body, rows, 0,
        `Show this file's ${plural(remainingLines(rows, 0), "line")}`));
    }
  }
  if (file.truncated && !file.hidden_reason) {
    section.appendChild(el("p", {
      class: "dv-note",
      text: "This file's preview was cut short to keep it small. The change itself is not.",
    }));
  }
  return section;
}

function summaryNode(diff, files) {
  const added = files.reduce((n, f) => n + (toCount(f.added) ?? 0), 0);
  const removed = files.reduce((n, f) => n + (toCount(f.removed) ?? 0), 0);
  const total = toCount(diff.files_total) ?? files.length;
  const parts = [
    el("span", { class: "dv-plus", text: `+${toCount(diff.added) ?? added}` }),
    el("span", { class: "dv-minus", text: `-${toCount(diff.removed) ?? removed}` }),
    el("span", { class: "dv-summary-files", text: total === 1 ? "in 1 file" : `across ${total} files` }),
  ];
  const omitted = toCount(diff.files_omitted) ?? 0;
  if (omitted) parts.push(el("span", { class: "dv-summary-warn", text: `${omitted} not listed` }));
  if (diff.truncated === true) {
    parts.push(el("span", { class: "dv-summary-warn", text: "preview shortened" }));
  }
  return appendAll(el("div", { class: "dv-summary" }), parts);
}

/** Render a hearth_diff preview. `opts.scroll` bounds the height (for an
 *  approval card inside the transcript); `opts.emptyText` is what to say when
 *  there are no files at all. Returns a detached element. */
export function renderDiff(diff, opts = {}) {
  const root = el("div", { class: opts.scroll ? "dv-root dv-scroll" : "dv-root" });
  const files = diff && typeof diff === "object" && Array.isArray(diff.files)
    ? diff.files.filter((f) => f && typeof f === "object") : [];
  if (!files.length) {
    root.appendChild(el("p", { class: "dv-note", text: opts.emptyText || "No differences." }));
    return root;
  }
  root.appendChild(summaryNode(diff, files));
  const budget = { left: INITIAL_ROWS };
  for (const file of files) root.appendChild(fileNode(file, budget));
  return root;
}

/** True when `diff` looks like something renderDiff can draw. */
export function hasDiff(diff) {
  return Boolean(diff && typeof diff === "object" && Array.isArray(diff.files));
}
