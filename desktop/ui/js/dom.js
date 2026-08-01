/* Element construction helpers.
 *
 * The single hard rule of this UI: nothing that came from the model, from a
 * file, from a tool result, or from any sidecar response is ever parsed as
 * HTML. `innerHTML` does not appear anywhere in desktop/ui/js. Every string
 * that reaches the page goes through `textContent` (here, or through the
 * tokenizer in safe-text.js, which also only ever sets textContent).
 *
 * `el()` deliberately has no "html" or "raw" option. Adding one later would
 * silently reopen the hole, so it is simply not expressible.
 */

/** Create an element. `props.text` sets textContent; everything else is an
 *  attribute set via setAttribute (or a direct property for the small set of
 *  form properties where the attribute is only the initial value). */
export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "text") {
      node.textContent = String(value);
    } else if (key === "class") {
      node.className = String(value);
    } else if (key === "dataset") {
      for (const [dk, dv] of Object.entries(value)) node.dataset[dk] = String(dv);
    } else if (key === "on") {
      for (const [ev, fn] of Object.entries(value)) node.addEventListener(ev, fn);
    } else if (key === "disabled" || key === "hidden" || key === "checked" || key === "open") {
      node[key] = Boolean(value);
    } else if (key === "value") {
      node.value = String(value);
    } else if (value === true) {
      node.setAttribute(key, "");
    } else {
      node.setAttribute(key, String(value));
    }
  }
  appendAll(node, children);
  return node;
}

export function appendAll(node, children) {
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

/** An <svg><use href="#id"> reference into the sprite defined in index.html.
 *  The id is chosen by this codebase, never by remote content. */
export function icon(id, className = "i") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", className);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#" + id);
  svg.appendChild(use);
  return svg;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function $(selector, root = document) {
  return root.querySelector(selector);
}
