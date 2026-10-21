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

const document = {
  getElementById: () => null,
  querySelectorAll: () => [],
  addEventListener: () => {},
  body: { addEventListener: () => {} },
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
const scope = vm.createContext({ document, Node: { ELEMENT_NODE: 1 }, NodeFilter: { SHOW_TEXT: 4 }, window: {} });
vm.runInContext(fs.readFileSync(path.join(__dirname, "../verifair/static/app.js"), "utf8"), scope);

function find(parts, quote, start) {
  const root = rootOf(parts);
  const range = scope.findRange(root, quote, start);
  assert.ok(range);
  assert.equal(range.toString(), quote);
  return range;
}

const cases = {
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
};
cases[process.argv[2]]();
console.log(`${process.argv[2]}: correct DOM range`);
