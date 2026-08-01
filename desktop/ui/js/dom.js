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
 *
 * The second hard rule, and the reason `neutralize` below sits in front of
 * every text sink: what the page displays must be the characters that are
 * actually there. Not parsing HTML stops a string from becoming structure; it
 * does nothing about a string that stays text and still lies about itself.
 * See neutralize's own comment for what that attack is and what is done to it.
 */

/* Characters that make displayed text disagree with the text itself.
 *
 * THE ATTACK. `U+202E RIGHT-TO-LEFT OVERRIDE` inside a tool argument reorders
 * the glyphs on screen without changing a single byte of what runs. The bytes
 *
 *     git status # <RLO>dda/ ssap resu ten ;      (<RLO> is one U+202E)
 *
 * are displayed by every correct Unicode implementation, Chromium included, as
 *
 *     git status # ; net user pass /add
 *
 * The approval card is the user's only defence against a hostile or
 * prompt-injected model. A card that shows something other than what will
 * execute does not weaken the permission system, it inverts it: the user
 * approves the harmless thing they read and the harmful thing runs. Measured
 * in a real renderer by taking a Range over each character and reading its
 * on-screen x, which is the only honest way to ask what order the glyphs are
 * in; textContent cannot tell you, because textContent is the half of the
 * discrepancy that is not lying.
 *
 * WHAT IS NEUTRALIZED, and why each group.
 *   - U+202A..U+202E, U+2066..U+2069: the explicit bidi overrides, embeddings
 *     and isolates. These are the attack. Note U+202C (POP DIRECTIONAL
 *     FORMATTING) and U+2069 are in the list too: leaving the terminators
 *     while removing the openers would be its own kind of wrong, and a stray
 *     terminator can end an isolate the surrounding markup opened.
 *   - U+200E, U+200F, U+061C: the invisible directional marks. Weaker than an
 *     override but they still move neutral characters (`;`, `/`, `#`, quotes,
 *     spaces) across each other, which is enough to reorder a command.
 *   - U+0000..U+001F except tab/LF/CR, U+007F, U+0080..U+009F: C0 and C1
 *     controls. A backspace or a carriage-return-only line in a terminal, an
 *     ESC that starts an ANSI sequence in anything the text is later pasted
 *     into. Tab, newline and carriage return are excluded because they are
 *     the normal shape of code and command output, which this UI renders with
 *     white-space: pre-wrap precisely so they show.
 *   - U+200B, U+2060, U+FEFF: zero-width space, word joiner, BOM. They occupy
 *     no width, so a command containing them displays shorter than it is.
 *
 * WHAT IS DELIBERATELY NOT TOUCHED.
 *   - Arabic, Hebrew, Thaana, and every other right-to-left script. Their
 *     correct rendering comes from the Unicode bidi ALGORITHM, which reads the
 *     inherent direction of ordinary letters. That algorithm is not the
 *     problem and is left alone: a Hebrew file path still reads correctly.
 *     It is the explicit OVERRIDE controls, which exist only to contradict the
 *     algorithm, that are removed.
 *   - U+200C ZERO WIDTH NON-JOINER and U+200D ZERO WIDTH JOINER. They are
 *     invisible, but they are required for correct spelling in Persian, Hindi
 *     and others, and they carry no directional force. Removing them would
 *     corrupt real words to defend against nothing.
 *
 * MADE VISIBLE, NOT SILENTLY STRIPPED. Each one becomes the literal text
 * `<U+202E>`. Stripping would fix the display and hide the event: a model that
 * emits an RLO into a shell command is a model that is being attacked or has
 * been turned, and that is exactly the moment the user should be told
 * something is wrong rather than shown a clean card. The marker is plain text,
 * so it survives being copied out of the page, which a CSS-only defence does
 * not. Nothing parses it, because nothing in this UI parses anything.
 */
const NEUTRALIZED = new RegExp(
  "[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F"   // C0 and C1 controls, keeping \t \n \r
  + "\u061C\u200E\u200F"                                     // ALM, LRM, RLM
  + "\u200B\u2060\uFEFF"                                     // zero-width space, word joiner, BOM
  + "\u202A-\u202E"                                          // LRE RLE PDF LRO RLO
  + "\u2066-\u2069]",                                        // LRI RLI FSI PDI
  "g");

/** Replace every display-deceiving character in `value` with a visible
 *  `<U+XXXX>` marker. Everything else, including all right-to-left text, is
 *  returned unchanged. */
export function neutralize(value) {
  const text = value === null || value === undefined ? "" : String(value);
  // The regex is a plain character class with no alternation, quantifier or
  // backtracking, so this is linear in the length of the text.
  return text.replace(NEUTRALIZED, (ch) =>
    "<U+" + ch.codePointAt(0).toString(16).toUpperCase().padStart(4, "0") + ">");
}

/** A text node whose glyphs are the characters it contains. The only way this
 *  file makes one, so a bidi override cannot enter the page through a text
 *  node that someone built by hand. */
export function textNode(value) {
  return document.createTextNode(neutralize(value));
}

/** Set an existing node's text. Same guarantee as textNode, for the places
 *  that assign to a live node rather than building a new one. */
export function setText(node, value) {
  node.textContent = neutralize(value);
  return node;
}

/** Create an element. `props.text` sets textContent; everything else is an
 *  attribute set via setAttribute (or a direct property for the small set of
 *  form properties where the attribute is only the initial value). */
export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "text") {
      node.textContent = neutralize(value);
    } else if (key === "class") {
      node.className = String(value);
    } else if (key === "dataset") {
      for (const [dk, dv] of Object.entries(value)) node.dataset[dk] = String(dv);
    } else if (key === "on") {
      for (const [ev, fn] of Object.entries(value)) node.addEventListener(ev, fn);
    } else if (key === "disabled" || key === "hidden" || key === "checked" || key === "open") {
      node[key] = Boolean(value);
    } else if (key === "value") {
      // NOT neutralized: this is a form field's own content, which is round
      // tripped back to the sidecar (the shop's search box) and belongs to
      // whoever typed it. Rewriting characters here would change what is
      // searched for. Nothing model-controlled is ever put in a form field.
      node.value = String(value);
    } else if (value === true) {
      node.setAttribute(key, "");
    } else {
      // Attributes are neutralized too, because `title`, `placeholder` and
      // `aria-label` are displayed text by another name -- a tooltip can carry
      // an override just as well as a card can.
      node.setAttribute(key, neutralize(value));
    }
  }
  appendAll(node, children);
  return node;
}

export function appendAll(node, children) {
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === "string" ? textNode(child) : child);
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
