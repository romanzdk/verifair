const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function rootOf(parts) {
  const root = { nodeType: 1, nodes: [] };
  root.nodes = parts.map((data) => ({ data, nodeType: 3, root }));
  return root;
}

function position(node, offset) {
  if (node.nodes) return [node, 0];
  const nodes = node.root.nodes;
  const prior = nodes.slice(0, nodes.indexOf(node)).reduce((n, current) => n + current.data.length, 0);
  return [node.root, prior + offset];
}

const bodyEvents = new Map();
const documentEvents = new Map();
const windowEvents = new Map();
const document = {
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: (name, fn) => documentEvents.set(name, [...(documentEvents.get(name) || []), fn]),
  body: { addEventListener: (name, fn) => bodyEvents.set(name, fn) },
  createTreeWalker(root) {
    let i = -1;
    return {
      nextNode() { return ++i < root.nodes.length; },
      get currentNode() { return root.nodes[i]; },
    };
  },
  createRange() {
    return {
      setStart(node, offset) { this.startContainer = node; this.startOffset = offset; },
      setEnd(node, offset) { this.endContainer = node; this.endOffset = offset; },
      toString() {
        const [root, start] = position(this.startContainer, this.startOffset);
        const [endRoot, end] = position(this.endContainer, this.endOffset);
        assert.equal(root, endRoot);
        return root.nodes.map((node) => node.data).join("").slice(start, end);
      },
    };
  },
};
const scope = vm.createContext({ document, Node: { ELEMENT_NODE: 1 }, NodeFilter: { SHOW_TEXT: 4 }, window: {}, getComputedStyle: (node) => node.style, addEventListener: (name, fn) => windowEvents.set(name, fn) });
vm.runInContext(fs.readFileSync(path.join(__dirname, "../verifair/static/app.js"), "utf8"), scope);

function find(parts, quote, start) {
  const root = rootOf(parts);
  const range = scope.findRange(root, quote, start);
  assert.ok(range);
  assert.equal(range.toString(), quote);
  return range;
}

const cases = {
  execution_fragment_reveals_all_ancestors() {
    const run = { open: false, parentElement: null, closest() { return this; } };
    const parent = { open: false, parentElement: run, closest() { return this; } };
    const event = { open: false, parentElement: parent, closest() { return this; }, scrollIntoView() { this.scrolled = true; } };
    scope.window.location = { hash: "#vf_trace_tool-1" };
    document.getElementById = (id) => id === "vf_trace_tool-1" ? event : null;
    windowEvents.get("load")();
    assert.equal(event.open, true);
    assert.equal(parent.open, true);
    assert.equal(run.open, true);
    assert.equal(event.scrolled, true);
    event.open = parent.open = run.open = false;
    windowEvents.get("hashchange")();
    assert.equal(event.open && parent.open && run.open, true);
    scope.window.location.hash = "#missing";
    windowEvents.get("hashchange")();
  },
  plan_general_cancel() {
    let resets = 0;
    const form = { hidden: false, reset: () => resets++, closest: () => ({}) };
    const cancel = { closest: () => form };
    const event = { target: { closest: (selector) => selector === "[data-cancel]" ? cancel : null } };
    documentEvents.get("click").forEach(fn => fn(event));
    assert.equal(form.hidden, false);
    assert.equal(resets, 1);
    form.closest = () => null;
    documentEvents.get("click").forEach(fn => fn(event));
    assert.equal(form.hidden, true);
  },
  verdict_draft_survives_gate_refresh() {
    const form = (target, revision, summary) => {
      const fields = { target: { value: target }, revision: { value: revision }, summary: { value: summary } };
      return {
        id: "vf_verdict", target: "_self",
        elements: { namedItem: (name) => fields[name] },
        summary: fields.summary,
      };
    };
    const original = form("plan", "1", "Keep my draft");
    bodyEvents.get("htmx:oobBeforeSwap")({ detail: { target: original } });
    let refreshed = form("plan", "1", "");
    document.getElementById = () => refreshed;
    bodyEvents.get("htmx:oobAfterSwap")({ detail: { target: refreshed } });
    assert.equal(refreshed.summary.value, "Keep my draft");
    bodyEvents.get("htmx:oobBeforeSwap")({ detail: { target: original } });
    refreshed = form("implementation", "1", "");
    bodyEvents.get("htmx:oobAfterSwap")({ detail: { target: refreshed } });
    assert.equal(refreshed.summary.value, "");
    bodyEvents.get("htmx:oobBeforeSwap")({ detail: { target: original } });
    refreshed = form("plan", "2", "");
    bodyEvents.get("htmx:oobAfterSwap")({ detail: { target: refreshed } });
    assert.equal(refreshed.summary.value, "");
  },
  resolved_comment_deep_link() {
    const thread = { open: false };
    let scrolled = 0;
    scope.window.location = { hash: "#vf_c13" };
    document.getElementById = (id) => id === "vf_c13" ? { closest: () => thread, scrollIntoView: () => scrolled++ } : null;
    windowEvents.get("hashchange")();
    assert.equal(thread.open, true);
    assert.equal(scrolled, 1);
  },
  load_reveals_fragment_and_focuses_autofocus() {
    const effects = [];
    const thread = { open: false };
    scope.window.location = { hash: "#vf_c13" };
    document.getElementById = (id) => id === "vf_c13" ? {
      closest: () => thread,
      scrollIntoView: () => effects.push("reveal"),
    } : null;
    document.querySelector = (selector) => {
      assert.equal(selector, "[autofocus]");
      return { focus: () => effects.push("focus") };
    };
    windowEvents.get("load")();
    assert.equal(thread.open, true);
    assert.deepEqual(effects, ["reveal", "focus"]);
  },
  unicode_before_duplicate() {
    const range = find(["😀😀 First repeated. Second repeated."], "repeated", 26);
    assert.equal(range.startOffset, 28);
    assert.equal(range.endOffset, 36);
  },
  unicode_inside_quote() {
    const range = find(["😀 first target😀. Second target😀"], "target😀", 24);
    assert.equal(range.startOffset, 26);
    assert.equal(range.endOffset, 34);
  },
  unicode_across_nodes() {
    const range = find(["🚀 before foo", "😀", "bar😀 after"], "foo😀bar😀", 9);
    assert.equal(range.startOffset, 10);
    assert.equal(range.endOffset, 5);
  },
  unicode_squashed_match() {
    const range = scope.findRange(rootOf(["😀 first foobar. Second foo", " 😀 bar😀"]), "foo😀bar😀", 23);
    assert.ok(range);
    assert.equal(range.toString(), "foo 😀 bar😀");
    assert.equal(range.startOffset, 24);
    assert.equal(range.endOffset, 9);
  },
  unicode_context() {
    const root = rootOf(["😀😀 before target😀 after🚀"]);
    const range = document.createRange();
    range.setStart(root.nodes[0], 12);
    range.setEnd(root.nodes[0], 20);
    assert.equal(range.toString(), "target😀");
    const [prefix, suffix] = scope.context({ querySelector: () => root }, range, "target😀");
    assert.equal(prefix, "😀😀 before ");
    assert.equal(suffix, " after🚀");
  },
  unicode_context_boundary() {
    const root = rootOf(["🚀" + "x".repeat(30) + " target " + "x".repeat(30) + "🚀"]);
    const range = document.createRange();
    range.setStart(root.nodes[0], 33);
    range.setEnd(root.nodes[0], 39);
    assert.equal(range.toString(), "target");
    const [prefix, suffix] = scope.context({ querySelector: () => root }, range, "target");
    assert.equal(prefix, "🚀" + "x".repeat(30) + " ");
    assert.equal(suffix, " " + "x".repeat(30) + "🚀");
  },
  error_copy() {
    assert.equal(scope.errorCopy(401), "Your access changed. Reload the page.");
    assert.equal(scope.errorCopy(403), "Your access changed. Reload the page.");
    assert.equal(scope.errorCopy(404), "This item no longer exists. Reload the page.");
    assert.equal(scope.errorCopy(422), "Check the text and try again.");
    assert.equal(
      scope.errorCopy(422, '{"detail":"quote not found"}'),
      "The selected text was not found. Cancel and select it again.",
    );
    assert.equal(scope.errorCopy(500), "Something went wrong (500). Reload and try again.");
  },
  triple_click_clamp() {
    const content = { childNodes: [{}, {}], contains: (node) => node === inside };
    const section = { querySelector: () => content };
    const inside = {
      nodeType: 3,
      parentElement: { closest: (selector) => (selector === "section .content" ? content : section) },
    };
    const selecting = (selected, endContainer) => ({
      toString: () => selected,
      getRangeAt: () => ({
        startContainer: inside,
        endContainer,
        cloneRange() {
          return {
            setEnd(node, offset) { this.endContainer = node; this.endOffset = offset; },
            toString: () => "Last paragraph",
          };
        },
      }),
    });
    const nextBlock = {};
    const triple = scope.sectionRange(selecting("Last paragraph\n", nextBlock));
    assert.equal(triple.section, section);
    assert.equal(triple.range.endContainer, content);
    assert.equal(triple.range.endOffset, 2);
    assert.equal(scope.sectionRange(selecting("Last paragraph\n\nNext heading", nextBlock)), null);
  },
  name_error_opens_closed_drawer() {
    const nav = (open, display) => {
      const shown = [];
      return {
        style: { display },
        shown,
        showPopover: () => shown.push(true),
        querySelector: (selector) => (open && selector === "details.me[open]" ? {} : null),
      };
    };
    const run = (...args) => {
      const target = nav(...args);
      document.getElementById = () => target;
      scope.showNavForNameError();
      return target.shown.length;
    };
    assert.equal(run(true, "none"), 1);
    assert.equal(run(true, "flex"), 0);
    assert.equal(run(false, "none"), 0);
  },
};
cases[process.argv[2]]();
console.log(`${process.argv[2]}: correct DOM range`);
