/* Model output -> DOM, without ever parsing HTML.
 *
 * A local model can be induced to emit markup, and tool results carry file
 * contents that routinely contain markup on purpose. So this module never
 * produces an HTML string and never hands one to the parser. It tokenizes the
 * text itself and builds real elements whose content is set with
 * `textContent`. `<img src=x onerror=alert(1)>` therefore lands in the page as
 * eleven visible characters of literal text, because it is only ever a string
 * assigned to a text node. There is no escaping step to get wrong, because
 * there is no un-escaping step anywhere in the pipeline.
 *
 * What is recognized, deliberately a very small set:
 *   - fenced code blocks:  ```lang ... ```
 *   - inline code spans:   `like this`
 *   - blank-line paragraph breaks
 *
 * Bold, italics, headings, lists, and links are NOT rendered. They would add
 * grammar without adding much for a coding agent's output, and every added
 * rule is another place a mistake could turn text into structure.
 *
 * See xss-check.html for the hostile payloads this is actually tested with.
 */

import { el, appendAll } from "./dom.js";

const FENCE = /^[ \t]*```([^\n`]*)$/;

/** Split `source` into an ordered list of {type: "text"|"code", body, lang}. */
export function tokenize(source) {
  const blocks = [];
  const lines = String(source ?? "").split("\n");
  let buffer = [];
  let code = null; // {lang, lines} while inside a fence

  const flushText = () => {
    if (buffer.length) {
      const body = buffer.join("\n");
      if (body.trim()) blocks.push({ type: "text", body });
      buffer = [];
    }
  };

  for (const line of lines) {
    const fence = FENCE.exec(line);
    if (code) {
      if (fence) {
        blocks.push({ type: "code", lang: code.lang, body: code.lines.join("\n") });
        code = null;
      } else {
        code.lines.push(line);
      }
      continue;
    }
    if (fence) {
      flushText();
      code = { lang: fence[1].trim(), lines: [] };
      continue;
    }
    buffer.push(line);
  }

  if (code) {
    // Unterminated fence: still show it as code rather than dropping it.
    blocks.push({ type: "code", lang: code.lang, body: code.lines.join("\n") });
  }
  flushText();
  return blocks;
}

/** Build the DOM for one text block, turning `inline code` into spans. */
function renderTextBlock(body) {
  const p = el("p");
  const parts = body.split("`");
  // Odd indices sit between a matched pair of backticks. An unmatched trailing
  // backtick leaves an even-indexed final part, which stays plain text.
  const paired = parts.length % 2 === 1;
  parts.forEach((part, i) => {
    if (part === "") return;
    if (paired && i % 2 === 1) {
      p.appendChild(el("span", { class: "code-inline", text: part }));
    } else {
      p.appendChild(document.createTextNode(i > 0 && !paired ? "`" + part : part));
    }
  });
  return p;
}

function renderCodeBlock(block) {
  const pre = el("pre", { class: "code-block" });
  if (block.lang) pre.appendChild(el("span", { class: "code-lang", text: block.lang }));
  pre.appendChild(el("code", { text: block.body }));
  return pre;
}

/** Render `source` into a fresh .prose container. */
export function renderProse(source) {
  const wrap = el("div", { class: "prose" });
  const blocks = tokenize(source);
  if (!blocks.length) {
    wrap.appendChild(el("p", { text: String(source ?? "") }));
    return wrap;
  }
  for (const block of blocks) {
    if (block.type === "code") {
      wrap.appendChild(renderCodeBlock(block));
    } else {
      // Paragraph breaks on blank lines; single newlines stay inside a
      // paragraph, which CSS renders with white-space: pre-wrap.
      for (const para of block.body.split(/\n{2,}/)) {
        if (para.trim()) wrap.appendChild(renderTextBlock(para.replace(/^\n+|\n+$/g, "")));
      }
    }
  }
  return wrap;
}

/** A scrollable, monospace block of literal text (tool output, file bodies,
 *  command output). Always textContent, never parsed. */
export function blob(value, { tall = false } = {}) {
  const node = el("pre", { class: tall ? "blob is-tall" : "blob" });
  node.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return node;
}

/** A labelled blob, used for "content", "output", and similar. */
export function labelledBlob(label, value, opts) {
  return appendAll(document.createDocumentFragment(), [
    el("span", { class: "blob-label", text: label }),
    blob(value, opts),
  ]);
}
