document.documentElement.classList.add("js");
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
  const chars = Array.from(quote).slice(0, QUOTE_MAX);
  quote = chars.join("");
  form.quote.value = quote;
  [form.prefix.value, form.suffix.value] = context(section, range, quote);
  const preview = form.querySelector(".q");
  const shown = chars.length > QUOTE_PREVIEW_MAX ? `${chars.slice(0, QUOTE_PREVIEW_MAX).join("")}…` : quote;
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

// A triple-click on the last block of a section ends the selection at the start of whatever
// follows it, often the next section. Pull the end back when that adds no visible text; a drag
// into the next section adds its heading, so it still spans two sections.
function sectionRange(selection) {
  const range = selection.getRangeAt(0);
  const section = sectionOf(range.startContainer);
  if (!section) return null;
  const content = section.querySelector(".content");
  if (content.contains(range.endContainer)) return { section, range: range.cloneRange() };
  const clamped = range.cloneRange();
  clamped.setEnd(content, content.childNodes.length);
  const bare = (text) => text.replace(/\s/g, "");
  return bare(selection.toString()) === bare(clamped.toString()) ? { section, range: clamped } : null;
}

document.addEventListener("mouseup", (event) => {
  if (!popover || popover.contains(event.target)) return;
  const selection = window.getSelection();
  const text = selection.toString().trim();
  const within = text && selection.rangeCount ? sectionRange(selection) : null;
  if (!within) {
    popover.hidden = true;
    return;
  }
  pending = { section: within.section, text, range: within.range };
  placePopover(within.range.getBoundingClientRect());
});

popover?.querySelector("button").addEventListener("click", () => {
  popover.hidden = true;
  window.getSelection().removeAllRanges();
  if (pending) openForm(pending.section, pending.text, pending.range);
});

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-comment-section]");
  if (button) {
    event.preventDefault();
    openForm(button.closest("section"), "");
  }
  const label = event.target.closest("[data-label]");
  if (label) setLabel(label);
  const cancel = event.target.closest("[data-cancel]");
  if (cancel) {
    const form = cancel.closest("form");
    if (form.closest(".general")) form.reset();
    else form.hidden = true;
  }
});

function setLabel(button) {
  const form = button.closest("form");
  const buttons = Array.from(form.querySelectorAll("[data-label]"));
  const known = new RegExp(`^\\s*(${buttons.map((b) => b.dataset.label).join("|")})\\s*(\\([^)\\n]*\\))?\\s*:\\s*`, "i");
  const on = button.getAttribute("aria-pressed") !== "true";
  const rest = form.body.value.replace(known, "");
  form.body.value = on ? `${button.dataset.label}: ${rest}` : rest;
  buttons.forEach((b) => b.setAttribute("aria-pressed", String(on && b === button)));
  if (on) form.blocking.checked = !button.hasAttribute("data-blocks");
  form.body.focus();
}

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
  const anchor = elt.closest("form, .rev-item") || elt;
  if (!anchor.isConnected) return anchor.id ? document.getElementById(anchor.id) : null;
  return anchor === document.body ? document.querySelector("main") || anchor : anchor;
}

function holdsError(anchor) {
  return anchor.matches(".verdict, main, body, .rev-item");
}

function clearRequestError(anchor) {
  const previous = holdsError(anchor)
    ? anchor.querySelector(":scope > p.vf-req-error")
    : anchor.nextElementSibling;
  if (previous?.matches("p.vf-req-error")) previous.remove();
}

function errorCopy(status, body = "") {
  if (status === 401 || status === 403) return "Your access changed. Reload the page.";
  if (status === 422 && body.includes("quote not found")) {
    return "The selected text was not found. Cancel and select it again.";
  }
  if (status === 422 && body.includes("comment body is empty")) {
    return "The comment is empty. Write something first.";
  }
  if (status === 422) return "Check the text and try again.";
  if (status === 404) return "This item no longer exists. Reload the page.";
  return `Something went wrong (${status}). Reload and try again.`;
}

function showRequestError(event, message) {
  const anchor = errorAnchor(event);
  if (!anchor) return;
  clearRequestError(anchor);
  anchor.closest("#vf_sidebar")?.querySelectorAll("p.vf-req-error").forEach((line) => line.remove());
  const line = document.createElement("p");
  line.className = "error vf-req-error";
  line.setAttribute("role", "alert");
  line.textContent = message;
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

function closeNav() {
  const nav = document.getElementById("vf_nav");
  if (nav?.hidePopover && nav.matches(":popover-open")) nav.hidePopover();
}

const wide = window.matchMedia?.("(min-width: 1024px)");
wide?.addEventListener("change", () => {
  if (wide.matches && !document.querySelector(".app.drawer")) closeNav();
});
// focus stays on the menu button when the drawer opens, and Tab from there skips the drawer
document.getElementById("vf_nav")?.addEventListener("toggle", (event) => {
  if (event.newState === "open" && !event.target.querySelector("details.me[open]")) {
    event.target.querySelector("a[href]")?.focus();
  }
});
document.addEventListener("focusout", (event) => {
  const to = event.relatedTarget;
  if (to && event.target.closest("#vf_nav") && !to.closest("#vf_nav, [popovertarget=vf_nav]")) closeNav();
});

highlight();
showNavForNameError();
document.body.addEventListener("htmx:afterSwap", highlight);
let verdictDraft = null;
document.body.addEventListener("htmx:oobBeforeSwap", (event) => {
  const form = event.detail.target;
  if (form.id === "vf_verdict") {
    verdictDraft = {
      target: form.elements.namedItem("target").value,
      revision: form.elements.namedItem("revision").value,
      summary: form.elements.namedItem("summary").value,
    };
  }
});
document.body.addEventListener("htmx:oobAfterSwap", (event) => {
  if (event.detail.target.id !== "vf_verdict") return;
  const form = document.getElementById("vf_verdict");
  if (verdictDraft && form?.elements.namedItem("target").value === verdictDraft.target
      && form.elements.namedItem("revision").value === verdictDraft.revision) {
    form.elements.namedItem("summary").value = verdictDraft.summary;
  }
  verdictDraft = null;
});

function revealComment() {
  const target = document.getElementById(window.location.hash.slice(1));
  if (window.location.hash.startsWith("#vf_trace_")) {
    let disclosure = target?.closest("details");
    while (disclosure) {
      disclosure.open = true;
      disclosure = disclosure.parentElement?.closest("details");
    }
    target?.scrollIntoView();
    return;
  }
  const thread = target?.closest("details.thread");
  if (thread) {
    thread.open = true;
    target.scrollIntoView();
  }
}
addEventListener("hashchange", revealComment);
// The rail is swapped whole, so focus goes to its error, or back to the picker, or to the Remove
// button that took the removed one's place, and the outcome is read out from a live region
// outside the rail.
function settleReviewers(sent) {
  const rail = document.getElementById("vf_sidebar");
  const removes = (root) => Array.from(root.querySelectorAll(".rev .btn"));
  const at = sent ? removes(sent.closest("aside") || rail).indexOf(sent) : -1;
  const error = rail.querySelector("p.error[tabindex]");
  const next = error || (at >= 0 && removes(rail)[at]) || rail.querySelector(".addrev select") || removes(rail).at(-1);
  next?.focus();
  // a focused role=alert error is already read out; repeating it in the live region would say it twice
  const status = error ? null : rail.querySelector("[data-rev-status]");
  const live = document.getElementById("vf_rev_live");
  if (live) live.textContent = status ? status.textContent : "";
}

document.body.addEventListener("htmx:afterSettle", (event) => {
  const path = event.detail.pathInfo?.requestPath || "";
  if (path.endsWith("/verdict") || path.endsWith("/verdict/withdraw")) {
    document.querySelector("#vf_verdict .vmsg")?.focus();
  } else if (/\/reviewers(\/[^/]+\/remove)?$/.test(path)) {
    settleReviewers(event.detail.requestConfig?.elt);
  }
});
document.body.addEventListener("htmx:responseError", (event) => showRequestError(event, errorCopy(event.detail.xhr.status, event.detail.xhr.responseText)));
document.body.addEventListener("htmx:sendError", (event) => {
  const typed = errorAnchor(event)?.querySelector?.("textarea");
  showRequestError(event, typed ? "Connection lost. Your text is still here, try again." : "Connection lost. Try again.");
});
// cleared before sending, because a stale reply replaces the form and leaves its error orphaned
document.body.addEventListener("htmx:beforeRequest", (event) => {
  const anchor = errorAnchor(event);
  if (anchor) clearRequestError(anchor);
});
document.body.addEventListener("htmx:afterRequest", (event) => {
  // hx-disabled-elt drops focus to the body while the Request button is disabled
  const form = event.detail.requestConfig?.elt;
  if (!event.detail.successful && form?.matches?.(".addrev") && form.isConnected && document.activeElement === document.body) {
    (form.querySelector("button:enabled") || form.querySelector("select"))?.focus();
  }
});

(() => {
  const nav = document.querySelector("[data-status-nav]");
  const active = nav?.querySelector('[aria-current="page"]');
  if (active && nav.scrollWidth > nav.clientWidth) {
    nav.scrollLeft = Math.max(0, active.offsetLeft - 16);
  }
  document.addEventListener("click", async event => {
    const button = event.target.closest("[data-copy-identity], [data-copy-target]");
    if (!button) return;
    const field = button.dataset.copyTarget && document.getElementById(button.dataset.copyTarget);
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
  // Browsers skip autofocus when the URL has a fragment, and scrolling to the fragment moves
  // focus, so focus the field once that is done
  addEventListener("load", () => {
    revealComment();
    document.querySelector("[autofocus]")?.focus();
  });
  document.addEventListener("submit", event => {
    const message = event.submitter?.dataset.confirm;
    if (message && !confirm(message)) event.preventDefault();
  });
})();
