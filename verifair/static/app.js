const popover = document.getElementById("vf_popover");
const QUOTE_PREVIEW_MAX = 300;
const QUOTE_MAX = 2000;
const CONTEXT = 32;
let pending = null;

function sectionOf(node) {
  const el = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
  return el && el.closest("section .content") ? el.closest("section") : null;
}

function collapse(text) {
  return text.replace(/\s+/g, " ");
}

function context(section, range, quote) {
  const content = section.querySelector(".content");
  if (!range || !content) return ["", ""];
  const text = indexText(content).text;
  const before = document.createRange();
  before.setStart(content, 0);
  before.setEnd(range.startContainer, range.startOffset);
  const lead = range.toString().match(/^\s*/)[0];
  const at = Array.from(collapse(before.toString() + lead).trimStart()).length;
  const end = at + Array.from(collapse(quote)).length;
  const chars = Array.from(text);
  return [chars.slice(Math.max(0, at - CONTEXT), at).join(""), chars.slice(end, end + CONTEXT).join("")];
}

function openForm(section, quote, range) {
  const form = section.querySelector("form.cform");
  if (!form) return;
  quote = quote.slice(0, QUOTE_MAX);
  form.quote.value = quote;
  [form.prefix.value, form.suffix.value] = context(section, range, quote);
  const preview = form.querySelector(".q");
  const shown = quote.length > QUOTE_PREVIEW_MAX ? `${quote.slice(0, QUOTE_PREVIEW_MAX)}…` : quote;
  preview.textContent = quote ? `"${shown}"` : "";
  preview.hidden = !quote;
  form.hidden = false;
  form.body.focus();
}

function placePopover(rect) {
  popover.hidden = false;
  const margin = 8;
  let top = rect.top - popover.offsetHeight - margin;
  if (top < margin) top = rect.bottom + margin;
  const maxLeft = document.documentElement.clientWidth - popover.offsetWidth - margin;
  const left = Math.max(margin, Math.min(rect.left, maxLeft));
  popover.style.top = `${top + window.scrollY}px`;
  popover.style.left = `${left + window.scrollX}px`;
}

document.addEventListener("mouseup", (event) => {
  if (!popover || popover.contains(event.target)) return;
  const selection = window.getSelection();
  const text = selection.toString().trim();
  const section = selection.rangeCount && sectionOf(selection.anchorNode);
  if (!text || !section || section !== sectionOf(selection.focusNode)) {
    popover.hidden = true;
    return;
  }
  pending = { section, text, range: selection.getRangeAt(0).cloneRange() };
  placePopover(selection.getRangeAt(0).getBoundingClientRect());
});

popover?.querySelector("button").addEventListener("click", () => {
  popover.hidden = true;
  window.getSelection().removeAllRanges();
  if (pending) openForm(pending.section, pending.text, pending.range);
});

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-comment-section]");
  if (button) openForm(button.closest("section"), "");
  const cancel = event.target.closest("[data-cancel]");
  if (cancel) cancel.closest("form").hidden = true;
});

function indexText(root) {
  const chars = [];
  const points = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    for (let i = 0; i < node.data.length;) {
      let ch = String.fromCodePoint(node.data.codePointAt(i));
      const end = i + ch.length;
      const start = i;
      i = end;
      if (/\s/.test(ch)) {
        if (!chars.length || chars[chars.length - 1] === " ") continue;
        ch = " ";
      }
      chars.push(ch);
      points.push([node, start, end]);
    }
  }
  if (chars[chars.length - 1] === " ") {
    chars.pop();
    points.pop();
  }
  return { text: chars.join(""), chars, points };
}

// The server may have matched with whitespace ignored (e.g. across table cells), so the quote is
// compared to the text at matchStart without spaces.
function lastIndexAt(text, at, quote) {
  const bare = Array.from(quote.replace(/\s/g, ""));
  let seen = 0;
  for (let i = at; bare.length && i < text.length; i++) {
    if (text[i] === " ") continue;
    if (text[i] !== bare[seen]) return -1;
    if (++seen === bare.length) return i;
  }
  return -1;
}

function findRange(root, quote, matchStart) {
  const needle = collapse(quote).trim();
  if (!needle) return null;
  const { text, chars, points } = indexText(root);
  let at = matchStart === undefined ? -1 : Number(matchStart);
  let last = at >= 0 ? lastIndexAt(chars, at, needle) : -1;
  if (last < 0) {
    const offset = text.indexOf(needle);
    if (offset < 0) return null;
    at = Array.from(text.slice(0, offset)).length;
    last = at + Array.from(needle).length - 1;
  }
  const [startNode, startOffset] = points[at];
  const [endNode, , endOffset] = points[last];
  const range = document.createRange();
  range.setStart(startNode, startOffset);
  range.setEnd(endNode, endOffset);
  return range;
}

function clearHighlights() {
  document.querySelectorAll("mark[id^=vf_m]").forEach((mark) => {
    const parent = mark.parentNode;
    mark.replaceWith(...mark.childNodes);
    parent.normalize();
  });
  if (window.CSS?.highlights) CSS.highlights.delete("vf-quote");
}

function highlight() {
  clearHighlights();
  const spanning = [];
  document.querySelectorAll('.thread[data-quote][data-found="true"]').forEach((thread) => {
    const content = document.querySelector(`#${CSS.escape(thread.dataset.sectionSlug)} .content`);
    const range = content && findRange(content, thread.dataset.quote, thread.dataset.matchStart);
    if (!range) return;
    if (range.startContainer.parentNode === range.endContainer.parentNode) {
      const mark = document.createElement("mark");
      mark.id = thread.id.replace("vf_t", "vf_m");
      range.surroundContents(mark);
    } else {
      spanning.push(range);
    }
  });
  if (spanning.length && window.CSS?.highlights) {
    CSS.highlights.set("vf-quote", new Highlight(...spanning));
  }
}

function errorAnchor(event) {
  // When the swap removes the source element, htmx re-fires afterRequest on an ancestor and
  // detail.elt points there; requestConfig.elt is still the element that sent the request.
  const elt = event.detail.requestConfig?.elt || event.detail.elt;
  const anchor = elt.closest("form") || elt;
  if (!anchor.isConnected) return anchor.id ? document.getElementById(anchor.id) : null;
  return anchor === document.body ? document.querySelector("main") || anchor : anchor;
}

function holdsError(anchor) {
  return anchor.matches(".verdict, main, body");
}

function clearRequestError(anchor) {
  const previous = holdsError(anchor)
    ? anchor.querySelector(":scope > p.vf-req-error")
    : anchor.nextElementSibling;
  if (previous?.matches("p.vf-req-error")) previous.remove();
}

function errorCopy(status) {
  if (status === 401 || status === 403) return "Your access changed. Reload the page.";
  if (status === 422) return "Check the text and try again.";
  if (status === 404) return "This item no longer exists. Reload the page.";
  return `Something went wrong (${status}). Reload and try again.`;
}

function showRequestError(event, message) {
  const anchor = errorAnchor(event);
  if (!anchor) return;
  clearRequestError(anchor);
  const line = document.createElement("p");
  line.className = "error vf-req-error";
  line.setAttribute("role", "alert");
  line.textContent = message;
  // The verdict bar is position: fixed, so a line after it would be hidden under it.
  if (anchor.matches(".verdict")) anchor.prepend(line);
  else if (holdsError(anchor)) anchor.append(line);
  else anchor.after(line);
}

function showNavForNameError() {
  const nav = document.getElementById("vf_nav");
  if (nav?.querySelector("details.me[open]") && getComputedStyle(nav).display === "none") {
    nav.showPopover?.();
  }
}

highlight();
showNavForNameError();
document.body.addEventListener("htmx:afterSwap", highlight);
document.body.addEventListener("htmx:responseError", (event) => showRequestError(event, errorCopy(event.detail.xhr.status)));
document.body.addEventListener("htmx:sendError", (event) =>
  showRequestError(event, "Connection lost. Your text is still here, try again."),
);
document.body.addEventListener("htmx:afterRequest", (event) => {
  const anchor = event.detail.successful && errorAnchor(event);
  if (anchor) clearRequestError(anchor);
});

(() => {
  const nav = document.querySelector("[data-status-nav]");
  const active = nav?.querySelector('[aria-current="page"]');
  if (active && nav.scrollWidth > nav.clientWidth) {
    nav.scrollLeft = Math.max(0, active.offsetLeft - 16);
  }
  document.querySelectorAll("[data-copy-identity], [data-copy-target]").forEach(button => {
    const field = button.dataset.copyTarget && document.getElementById(button.dataset.copyTarget);
    button.addEventListener("click", async () => {
      const result = button.parentElement.querySelector("[data-copy-result]");
      try {
        await navigator.clipboard.writeText(field ? field.value ?? field.textContent : button.dataset.copyIdentity);
        result.textContent = field?.tagName === "PRE" ? "Copied." : field ? "Token copied." : "Identity copied.";
      } catch {
        field?.select?.();
        result.textContent = field?.tagName === "PRE" ? "Select the text above to copy it."
          : field ? "Select the token above to copy it." : "Select the ID above to copy it.";
      }
    });
  });
  // Browsers skip autofocus when the URL has a fragment, and scrolling to the fragment moves
  // focus, so focus the field once that is done
  addEventListener("load", () => document.querySelector("[autofocus]")?.focus());
  document.addEventListener("submit", event => {
    const message = event.submitter?.dataset.confirm;
    if (message && !confirm(message)) event.preventDefault();
  });
})();
