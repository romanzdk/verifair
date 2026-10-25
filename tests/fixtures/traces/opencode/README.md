# OpenCode v2 synthetic exports

Producer baseline: product **2.1.3**, tag `v2.1.3`, commit
`bb25a2f4a3f3f04d7c344bec0c0574ea3510b3b1` in `anomalyco/opencode`.
Format: `opencode-v2-export`, the JSON printed by `opencode session export SESSION_ID`.
These fixtures are hand-written synthetic source-shaped records, never real transcripts.
No OpenCode executable, server, credentials, live capture or inference was used.

Primary sources at that revision:

- [Transfer document schema](https://github.com/anomalyco/opencode/blob/bb25a2f4a3f3f04d7c344bec0c0574ea3510b3b1/packages/schema/src/session-transfer.ts)
- [Typed messages and tool states](https://github.com/anomalyco/opencode/blob/bb25a2f4a3f3f04d7c344bec0c0574ea3510b3b1/packages/schema/src/session-message.ts)
- [Session parent and fork relationships](https://github.com/anomalyco/opencode/blob/bb25a2f4a3f3f04d7c344bec0c0574ea3510b3b1/packages/schema/src/session.ts)
- [Settled-only exporter and sanitizer](https://github.com/anomalyco/opencode/blob/bb25a2f4a3f3f04d7c344bec0c0574ea3510b3b1/packages/core/src/session/transfer.ts)
- [Subagent session reuse and metadata](https://github.com/anomalyco/opencode/blob/bb25a2f4a3f3f04d7c344bec0c0574ea3510b3b1/packages/core/src/tool/plugin/subagent.ts)
- [Plain text and file result schema](https://github.com/anomalyco/opencode/blob/bb25a2f4a3f3f04d7c344bec0c0574ea3510b3b1/packages/schema/src/tool.ts)
- [Model references](https://github.com/anomalyco/opencode/blob/bb25a2f4a3f3f04d7c344bec0c0574ea3510b3b1/packages/schema/src/model.ts)
- [Structured errors](https://github.com/anomalyco/opencode/blob/bb25a2f4a3f3f04d7c344bec0c0574ea3510b3b1/packages/schema/src/session-error.ts)
- [Shell input fields and result metadata](https://github.com/anomalyco/opencode/blob/bb25a2f4a3f3f04d7c344bec0c0574ea3510b3b1/packages/core/src/tool/plugin/shell.ts)
- [Shell exit and truncation metadata](https://github.com/anomalyco/opencode/blob/bb25a2f4a3f3f04d7c344bec0c0574ea3510b3b1/packages/core/src/shell/result.ts)
- [Read input path](https://github.com/anomalyco/opencode/blob/bb25a2f4a3f3f04d7c344bec0c0574ea3510b3b1/packages/core/src/tool/plugin/read.ts)
- [Shell message status and output](https://github.com/anomalyco/opencode/blob/bb25a2f4a3f3f04d7c344bec0c0574ea3510b3b1/packages/schema/src/shell.ts)
- [Fork boundary](https://github.com/anomalyco/opencode/blob/bb25a2f4a3f3f04d7c344bec0c0574ea3510b3b1/packages/schema/src/session-fork.ts)

| Fixture | Coverage |
| --- | --- |
| `settled.json` | Direct typed user/assistant/compaction records, text, exposed reasoning text, tool final state, retry, model, integer millisecond timing, ignored native data and file results, credential redaction. |
| `child.json` | Separately supplied session with native `parentID`; tests add exact `subagent` metadata and cover unique, reused, missing and conflicting invocation links. |
| `v1.json` | Minimal deliberate rejection fixture with legacy `{info, parts}` message wrappers. The adapter never interprets its v1 payload. |

Tests vary these fixtures to cover running/streaming/error states, unknown additive fields
and record kinds, malformed states/timestamps, no recognized events, native shell records,
assistant failure, compaction states, read paths, oversized details, repeated exports,
forks, aggregate read limits and aggregate event selection. Synthetic incomplete states
test defensive behavior; the pinned exporter itself filters unsettled messages.

Mapping and limits:

- One run per native session ID. Local run/event IDs are importer-owned slugs;
  `source_ref` retains scoped session/message/tool IDs. Exact repeated message IDs
  merge in explicit input order with the last supplied projection replacing an earlier
  projection. Repeated child exports never multiply that child's history.
- Every assistant message has an assistant event; its text becomes optional output.
  Exposed `reasoning.text` is a separate labelled assistant event with optional explanation.
  `native`, hidden/encrypted blocks and arbitrary unknown objects are never copied.
  A retry is a checkpoint, never a fabricated HTTP/model call. Compaction is a checkpoint,
  never reconstructed discarded execution.
- Tool `streaming/running/completed/error` maps to `running/running/completed/failed`.
  Unknown tool states remain unknown with a warning. Tool timestamps use only
  `time.ran/completed`, with exact integer/timedelta conversion, never `time.created`
  or surrounding message times. Completed tools do not imply successful tests.
- Native shell records preserve explicit exit codes; exited nonzero maps to failed,
  timeout to failed, killed to cancelled. Their emission/start relationship is not
  established by this adapter, so no start is invented. Shell tool completion preserves
  its tool lifecycle status and separately records the structured shell exit value.
- Opt-in details allow assistant text, exposed reasoning text, compaction summary,
  text tool results, native shell command/output, shell tool `command/workdir`, and
  read tool `path`. Each plain field is sanitized before event construction. Streaming
  JSON arguments, arbitrary tool inputs, error bodies, prompts, binary/file/image
  content, environment objects and metadata dumps are excluded. Names/timestamps may
  still be sensitive, and redaction is incomplete: inspect the local JSON before sharing.
- Child invocation links require matching native `info.parentID` and exactly one
  matching `subagent` tool's `state.metadata.sessionID`. Reuse keeps only the known
  parent run and warns. Missing/conflicting links warn. A fork is not a delegation.
- All exports stay partial: settled projected history and nonrecursive child coverage
  are always visible. Missing producer version warns; untested v2 versions warn;
  unsupported majors, malformed recognized fields and v1 wrappers fail safely.
  No universal compatibility is claimed. Source/tool identifiers are bounded ASCII
  tokens up to 100 characters each and source references up to 200. Model references
  allow safe printable strings (including `@`), with a combined 100-character bound;
  malformed fields, control characters and recognized credential values are rejected.
- Only explicitly supplied files are read, with one shared actual-byte budget.
  Aggregate selection/final validation runs once after merging every input. Display
  order follows native message order across supplied files, not inferred chronology.
