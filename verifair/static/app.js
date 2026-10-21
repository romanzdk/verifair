const popover = document.getElementById("vf_popover");
const QUOTE_PREVIEW_MAX = 300;
let pending = null;

function sectionOf(node) {
  const el = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
  return el && el.closest("section .content") ? el.closest("section") : null;
}

function openForm(section, quote) {
  const form = section.querySelector("form.cform");
  if (!form) return;
  form.quote.value = quote;
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
  pending = { section, text };
  placePopover(selection.getRangeAt(0).getBoundingClientRect());
});

popover?.querySelector("button").addEventListener("click", () => {
  popover.hidden = true;
  window.getSelection().removeAllRanges();
  if (pending) openForm(pending.section, pending.text);
});

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-comment-section]");
  if (button) openForm(button.closest("section"), "");
  const cancel = event.target.closest("[data-cancel]");
  if (cancel) cancel.closest("form").hidden = true;
});

// Mirrors render.normalize on the server: text nodes joined, whitespace runs collapsed.
function indexText(root) {
  const chars = [];
  const points = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    for (let i = 0; i < node.data.length; i++) {
      let ch = node.data[i];
      if (/\s/.test(ch)) {
        if (chars[chars.length - 1] === " ") continue;
        ch = " ";
      }
      chars.push(ch);
      points.push([node, i]);
    }
  }
  return { text: chars.join(""), points };
}

function findRange(root, quote) {
  const needle = quote.replace(/\s+/g, " ").trim();
  if (!needle) return null;
  const { text, points } = indexText(root);
  const at = text.indexOf(needle);
  if (at < 0) return null;
  const [startNode, startOffset] = points[at];
  const [endNode, endOffset] = points[at + needle.length - 1];
  const range = document.createRange();
  range.setStart(startNode, startOffset);
  range.setEnd(endNode, endOffset + 1);
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
    const range = content && findRange(content, thread.dataset.quote);
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
  const elt = event.detail.elt;
  return elt.closest("form") || elt;
}

function clearRequestError(anchor) {
  const previous = anchor.matches(".verdict")
    ? anchor.querySelector("p.vf-req-error")
    : anchor.nextElementSibling;
  if (previous?.matches("p.vf-req-error")) previous.remove();
}

function showRequestError(event, reason) {
  const anchor = errorAnchor(event);
  clearRequestError(anchor);
  const line = document.createElement("p");
  line.className = "error vf-req-error";
  line.setAttribute("role", "alert");
  line.textContent = `Something went wrong: ${reason}. Reload and try again.`;
  // The verdict bar is position: fixed, so a line after it would be hidden under it.
  if (anchor.matches(".verdict")) anchor.prepend(line);
  else anchor.after(line);
}

highlight();
document.body.addEventListener("htmx:afterSwap", highlight);
document.body.addEventListener("htmx:responseError", (event) => showRequestError(event, event.detail.xhr.status));
document.body.addEventListener("htmx:sendError", (event) => showRequestError(event, "network error"));
document.body.addEventListener("htmx:afterRequest", (event) => {
  if (event.detail.successful && event.detail.elt.isConnected) clearRequestError(errorAnchor(event));
});
