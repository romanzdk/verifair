# Pi session fixture

`session-v3.jsonl` is entirely synthetic. No agent was run and no private transcript
was used. Target: Pi CLI **1.4.0**, session header schema **3**, source commit
`4a080252f06caf63a525d7873c5d94d3c23887e0`.

Primary source at that commit:

- [Persisted session format](https://github.com/earendil-works/pi/blob/4a080252f06caf63a525d7873c5d94d3c23887e0/packages/coding-agent/docs/session-format.md)
- [Message and content types](https://github.com/earendil-works/pi/blob/4a080252f06caf63a525d7873c5d94d3c23887e0/packages/coding-agent/docs/message-types.md)
- [SessionManager and SDK persistence](https://github.com/earendil-works/pi/blob/4a080252f06caf63a525d7873c5d94d3c23887e0/packages/coding-agent/src/core/session-manager.ts)
- [Branch JSONL exporter](https://github.com/earendil-works/pi/blob/4a080252f06caf63a525d7873c5d94d3c23887e0/packages/coding-agent/src/core/session-export.ts)

The pinned public source copies under `/tmp/verifair-phase3/public-sources/pi-mono-release`
were used to author the shapes. SDK `SessionManager.create`/`appendMessage` uses
the same persisted header and message-entry types as CLI sessions, so there is
no separate SDK schema fixture. The synthetic custom session ID also exercises
the SDK's documented custom-ID shape.

Coverage:

- Versioned header, system/user messages, model changes, explicit ID/parent tree,
  two leaves, selected ancestry, and an out-of-file fork path that must never open.
- Assistant text, tool calls with distinct retry IDs, failed/successful results,
  an interrupted assistant with a tool lacking a result, and an opaque `subagent` tool.
- Compaction, context edit, branch summary referencing unavailable history,
  extension records and an unknown additive record type.
- Credential, image, thinking/signature, environment, metadata and alternate-branch
  sentinels for default exclusions and bounded opt-in details.
- Tests derive unique-branch, malformed/cyclic/missing-parent, orphan-result,
  structured direct-shell and unsupported-format/version cases from synthetic rows.

The full fixture requires `--leaf a3` or `--leaf alternate`; omission is an error.
Tests remove the alternate leaf to exercise unique-leaf branch exports. Header
time is deliberately later than message time, as a branch export creates its
header at export time. Neither timestamp is a tool execution duration.

Only schema 3 is version-tested. Absent schema/producer versions warn if the
tree shape is otherwise recognized. Other producer versions in major 1 warn;
other major versions and explicit schema versions other than 3 fail. Unknown
additive fields are ignored. This is not a universal Pi compatibility claim.

Default output excludes prose and arguments. `--include-details` admits only
documented text blocks, compaction/branch-summary text, tool `command`/`path`,
and direct-shell command/output fields after shared redaction and truncation.
Inspect output before sharing: redaction is incomplete, and names can be sensitive.
Raw thinking, images, environment objects, extension payloads and arbitrary result
details are always excluded. Extension tools remain tools; no spawned sessions,
model HTTP calls, nested-call metadata, discarded history or successful tests
are reconstructed. All persisted message/entry/header timestamps remain untimed.
