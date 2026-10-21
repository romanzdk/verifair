const popover = document.getElementById("vf_popover");
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
  preview.textContent = quote ? `"${quote}"` : "";
  preview.hidden = !quote;
  form.hidden = false;
  form.body.focus();
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
  const rect = selection.getRangeAt(0).getBoundingClientRect();
  pending = { section, text };
  popover.style.top = `${rect.top + window.scrollY - 36}px`;
  popover.style.left = `${rect.left + window.scrollX}px`;
  popover.hidden = false;
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

function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function highlight() {
  document.querySelectorAll("mark[id^=vf_m]").forEach((mark) => {
    const parent = mark.parentNode;
    mark.replaceWith(...mark.childNodes);
    parent.normalize();
  });
  document.querySelectorAll('.thread[data-quote][data-found="true"]').forEach((thread) => {
    const content = document.querySelector(`#${CSS.escape(thread.dataset.sectionSlug)} .content`);
    if (!content) return;
    const pattern = new RegExp(escapeRegExp(thread.dataset.quote.trim()).replace(/\s+/g, "\\s+"));
    const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      const match = pattern.exec(node.data);
      if (!match) continue;
      const range = document.createRange();
      range.setStart(node, match.index);
      range.setEnd(node, match.index + match[0].length);
      const mark = document.createElement("mark");
      mark.id = `vf_m${thread.id.slice(1)}`;
      range.surroundContents(mark);
      return;
    }
  });
}

highlight();
document.body.addEventListener("htmx:afterSwap", highlight);
