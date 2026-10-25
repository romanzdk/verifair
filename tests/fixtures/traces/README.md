# Trace converter fixture policy

Only small, synthetic captures belong here. Never copy a real session, prompt,
private repository content, credential, or environment dump into a fixture.

Each adapter's fixture entry must record:

- The exact producer CLI version and/or pinned source commit.
- The selected format and exact producer command/export path (including flags).
- How the fixture was synthesized against that version's public schema.
- The source fields and lifecycle/relationship cases it exercises.
- Unsupported versions, record kinds, branches, or missing capture evidence.

Do not claim universal compatibility. Missing producer versions and untested
versions need visible warnings. Unknown optional fields must be ignored through
explicit extraction; unknown record kinds need coverage warnings. No discovery
of related sessions or producer executables is allowed.

The foundation tests synthesize generic JSON/JSONL records inline in
`tests/test_trace_import.py`. They are deliberately not provider captures and
make no provider-version compatibility claim. The test-only converter covers
bounded local reading, opt-in text details, provenance forwarding, selection,
validation, and private canonical output. The four provider adapters use these
pinned synthetic fixtures; each linked README records its source and limits:

| Format | Producer baseline | Fixture and provenance |
| --- | --- | --- |
| `claude-stream-json` | Claude Code 2.1.309 / documented SDK stream | [claude/native.jsonl](claude/native.jsonl), [provenance](claude/README.md) |
| `codex-exec-jsonl` | Codex 0.163.0 / `rust-v0.163.0` | [codex/native.jsonl](codex/native.jsonl), [provenance](codex/README.md) |
| `opencode-v2-export` | OpenCode 2.1.3 / `bb25a2f4a3f3f04d7c344bec0c0574ea3510b3b1` | [opencode/settled.json](opencode/settled.json), [provenance](opencode/README.md) |
| `pi-session-jsonl` | Pi 1.4.0 / schema 3 / `4a080252f06caf63a525d7873c5d94d3c23887e0` | [pi/session-v3.jsonl](pi/session-v3.jsonl), [provenance](pi/README.md) |

`tests/test_trace_workflow.py` runs these through real converter and request-builder
subprocesses, authenticated application writes, disposable PostgreSQL storage,
exact snapshot reads and the Execution view in both workflows. The Pi fixture
requires `--leaf a3` for the main selected branch. This proves integration of the
synthetic shapes, not that a live producer emitted these captures.

Details are excluded by default. Adapters must extract only documented plain
text fields before calling the shared sanitation helper. Never flatten arbitrary
objects or copy image/binary/environment/hidden/encrypted reasoning blocks.
Credential redaction is best effort and incomplete. Even names and timestamps
may be sensitive: inspect the local JSON before including it in a report.
There is no automatic upload command. Submitted normalized evidence has the
report's readership; replacing it does not remove historical evidence. For an
accidental disclosure, use the instance operator's existing removal process.
