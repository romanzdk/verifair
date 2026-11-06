# API

Agents use the HTTP API to push change reports, read feedback and answer it. Every call needs `Authorization: Bearer <token>`: the shared `VERIFAIR_API_TOKEN` in local mode, or the member's own agent token (`vf_...`) in shared mode. See [access.md](access.md).

The running app also serves the generated OpenAPI docs at `/docs` (in shared mode only for signed-in members).

- [Push a report](#push-a-report)
- [Plan-first review](#plan-first-review)
- [Optional execution evidence](#optional-execution-evidence)
- [Read a report](#read-a-report)
- [Read and answer comments](#read-and-answer-comments)
- [Wait for feedback](#wait-for-feedback)
- [Errors](#errors)
- [Report rules](#report-rules)

## Push a report

```bash
curl -sS --fail-with-body -X POST http://localhost:8000/api/reports \
  -H "Authorization: Bearer $VERIFAIR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "my-change",
    "project": "billing-service",
    "title": "Prorate mid-cycle plan changes",
    "risk": "high",
    "body": "## Intent\nCharge upgrades by remaining days.\n\n## Verification\nUnit tests pass.\n\n## Rollback\nRevert the merge request.\n\n## Security impact\nNone.",
    "ticket_url": "https://example.com/tickets/BILL-412",
    "links": [{"label": "Merge request", "url": "https://gitlab.example.com/billing/billing-service/-/merge_requests/231"}],
    "author": "Alice",
    "author_id": "alice@example.com",
    "reviewers": ["bob@example.com"],
    "agent": "claude",
    "model": "opus",
    "commit": "a1b2c3d"
  }'
```

Response:

```json
{"id": "my-change", "version": 1, "url": "http://localhost:8000/r/my-change"}
```

The commands below use the report `id` as `$ID`. In shared mode, use your own agent token instead of `$VERIFAIR_API_TOKEN`.

- `id` is optional on the first push. Without it, verifair makes one from the title and returns it.
- To add a new version, push again with the same `id`. Without `id`, every push makes a new report. Comments stay on the version they were made on; the API marks them `stale` when a newer version exists and `found` when the quoted text can be located in the current version, including after a section rename or move.
- Every push requires `project`, `title` (1 to 200 characters) and `risk` (`low`, `medium` or `high`). The default `artifact` workflow also requires nonblank Markdown `body` on every push. Select `workflow: "plan_first"` on creation to review a plan before submitting an implementation.
- `project` and `id` must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`: letters, digits, `.`, `_` and `-`, starting with a letter or digit, up to 100 characters.
- `ticket_url`, `session_url` and `links[].url` must start with `http://` or `https://`.
- A `links` entry whose URL points to a merge or pull request (a path with `/merge_requests/231`, `/pull/12`, `/pulls/12` or `/pull-requests/12`, optionally followed by more path, a query or a `#` fragment) also shows in the report header, next to the ticket. The header shows a short label made from the URL, such as `billing-service!231` or `verifair#12`, with your `label` as its tooltip. The project name must be plain letters, digits, `.`, `_` and `-` (up to 100 characters); other URLs stay in the Links line only. The ticket badge follows the same rule and shows `ticket` otherwise. All links stay in the **Links** line at the bottom of the page, with your labels.
- Optional `author` (up to 60 characters): the human author's display name. Defaults to `agent`. Use `author_id` to link ownership to a stable identity. In shared mode `author` is ignored and the report shows the token owner's name.
- Optional `author_id`: the owner's stable identity, separate from the display name in `author`. Optional `reviewers`: up to 50 stable identities to request reviews from. Each ID must be 1–256 characters and cannot be `agent`. In local mode it must match the user's identity exactly; in shared mode verifair turns `author_id` and `reviewers` into lower case first. Duplicate reviewers are stored once, and the report owner is left out of `reviewers` without an error.
- On later versions, a field you leave out keeps its previous value: `ticket_url`, `links`, `agent`, `model`, `session_url`, `author` and `author_id`. Send a field to replace it. `null` clears `ticket_url`, and `[]` clears `links` (`null` is refused for `links`). `agent`, `model` and `session_url` describe one writer, so they change together: send any of them and the ones you leave out become `null`.
- In local mode a supplied `author_id` transfers ownership. Unlike the fields above, `null` for `author_id` leaves the existing value unchanged.
- `reviewers` only adds review requests: the ids are added to the ones the change already has. `[]`, `null` or leaving it out changes nothing. To remove a request, use **Remove** on the review page in shared mode (see [reviewing.md](reviewing.md#request-a-review)). Requests belong to the change and survive signing, verdicts and new versions.
- Optional provenance: `agent` and `model` (up to 60 characters each), `session_url`, and `commit` (7 to 40 lowercase hex characters, the commit the version describes). `agent`, `model` and `session_url` belong to the report; `commit` is stored per version.
- An explicitly supplied body must contain every section in `VERIFAIR_REQUIRED_SECTIONS` as a top-level H2 heading, spelled exactly (case matters). Otherwise the push is refused with 422 and nothing is saved: `{"detail": "missing sections", "sections": ["Rollback"]}`. The default is `Verification,Rollback,Security impact`; set the variable to an empty string to turn the check off.
- Unknown JSON keys are rejected with 422. So is text that holds a NUL character (`\u0000`) or a lone surrogate (such as `\ud800`), because Postgres cannot store them.
- In shared mode the token owner owns the report. `author_id` can be left out; if sent, it must be the token owner, or the push gets 403 `"author_id must be the token owner."`. A push to an `id` that belongs to another member's report gets 403 `"This report belongs to another member."`, also when the token owner is an admin. A push to an `id` whose report has no owner gets 403 `"This report has no owner. Push it again with an agent token under a new id."`. Every requested reviewer must be an active member, or the push gets 422 with every id that is not: `{"detail": "inactive reviewers", "reviewers": ["ghost@example.com"]}`.

A push sends a "draft" message to Slack (if configured). The "ready for review" message is sent when the version is signed.

Report markdown: raw HTML is shown as text, not rendered, and `javascript:` links are dropped. Use plain markdown only.

### Optional plan

This section describes the default `artifact` workflow. Its Plan tab stays read-only; it has no plan comments, signatures or verdicts.

`POST /api/reports` accepts one optional `plan`: Markdown contents, not a file path or URL to load. Any design or implementation plan is accepted; no template or report headings are required for it. The implementation `body` and all its required sections are still required on every push.

| `plan` input | First version | Later version |
| --- | --- | --- |
| Omitted | No plan | Inherit the immediately preceding version's plan |
| `null` | No plan | Remove the plan from the new version only |
| Nonblank string | Store exact text | Replace with the exact text |
| Empty or whitespace-only string, wrong JSON type, NUL or lone surrogate | 422; nothing saved | 422; no metadata or version changes |

Each version stores its own plan snapshot, preserving submitted whitespace and Unicode without trimming or normalization. Replacing or removing a plan leaves earlier versions intact. The existing 1 MiB (1,048,576-byte) limit covers the entire encoded JSON request, including `body`, `plan` and all other fields together.

Read UTF-8 files and encode their contents with Python before sending JSON. Set `REPORT_MD` and `PLAN_MD` to your local files, and use your configured URL and token:

```bash
python3 - "$REPORT_MD" "$PLAN_MD" <<'PY' | curl -sS --fail-with-body \
  -X POST "$VERIFAIR_URL/api/reports" \
  -H "Authorization: Bearer $VERIFAIR_API_TOKEN" \
  -H "Content-Type: application/json" --data-binary @-
import json, sys
from pathlib import Path
print(json.dumps({
    "id": "my-change", "project": "my-project",
    "title": "Describe the implemented change", "risk": "low",
    "body": Path(sys.argv[1]).read_bytes().decode("utf-8"),
    "plan": Path(sys.argv[2]).read_bytes().decode("utf-8"),
}))
PY
```

For updates, reuse `id` and resend the implementation `body` and required fields. Omit the `plan` key to inherit; use `"plan": None` in Python to send JSON `null` and remove it. Do not interpolate Markdown into a shell JSON literal. Submission uses the same JSON endpoint, with no multipart upload or web editor.

Every push creates a new unsigned report version, even if only the plan changes or is removed. A human must sign the new version before review; earlier signatures and verdicts remain historical. Plan context is read-only, with no separate comments or approval. The POST response remains HTTP 201 with `{id, version, url}`. See [Report and Plan](reviewing.md#report-and-plan) for navigation and history.

`scripts/push-sample.sh` pushes the report template as a draft example with the fixed id `sample-prorate-mid-cycle`, so running it again adds a new version. It sends to `http://localhost:8000` unless you set `VERIFAIR_URL`. For any URL that is not localhost it also needs `VERIFAIR_ALLOW_REMOTE=1`:

```bash
VERIFAIR_API_TOKEN=change-me bash scripts/push-sample.sh
VERIFAIR_API_TOKEN=change-me VERIFAIR_URL=https://verifair.example.com VERIFAIR_ALLOW_REMOTE=1 bash scripts/push-sample.sh
```

## Plan-first review

Create a review with `workflow: "plan_first"`, one nonblank Markdown `plan`, and no implementation body. A plan can be any design or implementation document, including a paragraph or H1-only document; no template, skill or required headings apply. Send file contents, not a path or a URL for the server to load.

```json
{
  "id": "proration", "project": "billing-service", "title": "Proration", "risk": "high",
  "workflow": "plan_first", "plan": "# Design\nUse daily proration."
}
```

The response is still HTTP 201 `{id, version, url}`. If you omit `id` on creation, save the returned ID. Reuse it on every update; implementation attaches to the same review and `/r/{id}` URL. Workflow is fixed at creation. Omit `workflow` on updates to inherit it; changing it gets 409 `"workflow cannot be changed"`.

A human owner signs the exact current plan revision; a different human reviewer approves it. Agent tokens cannot sign or submit verdicts. Once metadata reports `stage: "awaiting_implementation"`, submit the implementation at that same ID, repeating the required metadata and guarding the current global snapshot:

```json
{
  "id": "proration", "project": "billing-service", "title": "Proration", "risk": "high",
  "expected_version": 1,
  "body": "## Verification\nTests passed.\n\n## Rollback\nRevert.\n\n## Security impact\nNone."
}
```

Read the current state before choosing `expected_version`; the example assumes snapshot 1 is still current. A plan-first update requires this guard and at least one explicitly supplied content field. Metadata-only pushes get 422. Stale guards get `409 {"detail":"review changed","current_version":N}`. Creation must omit the guard.

| Input | Artifact workflow | Plan-first workflow |
| --- | --- | --- |
| Omitted `body` | 422 on every push | No implementation on creation; inherit on update |
| `body: null` | 422 | Accepted on creation; 422 on update |
| Supplied nonblank `body` | Required sections apply | Forbidden on creation; later requires current plan approval and required sections |
| Omitted `plan` | No plan on creation; inherit on update | 422 on creation; inherit on update |
| `plan: null` | No plan on creation; remove from new snapshot on update | 422; a plan cannot be removed |
| Supplied nonblank `plan` | Read-only context | Reviewable document; changes require fresh human signature and approval |
| Non-null `commit` with omitted body | Invalid because body is required | 422, including when an existing body would be inherited |

Blank strings, NUL and lone surrogates remain 422. Both documents are stored exactly, preserving spaces, Unicode and line endings. If exact bytes matter, load the UTF-8 file with `Path(path).read_bytes().decode("utf-8")`; text-mode file reads can translate CRLF. Neither document is normalized by the server. Required implementation headings are checked only on supplied bodies. The 1,048,576-byte limit covers the whole encoded request, including both documents and metadata.

Every push creates a new global snapshot, even with identical text. A plan revision is the snapshot that began the current continuous run of exactly identical plan text. For example, unchanged plan revision 1 can appear in snapshots 1 and 2; an edited plan in snapshot 3 is plan revision 3. Reverting text after an intervening edit creates another revision and never restores an older approval. Implementation revisions always use global snapshot numbers.

An unchanged approved plan keeps its approval across body pushes. Supplying the same approved plan together with a body is allowed. Changing a plan and submitting a body in the same push is refused: `409 {"detail":"current plan is not approved","plan_revision":R}`. There is no owner/admin override. A plan-only update inherits an existing body and its commit, but that new implementation snapshot needs a new human signature and verdicts. A snapshot with no implementation has `commit: null`.

An unresolved blocking plan thread, including an older unresolved concern, pauses effective approval and implementation submission, signing and verdicts. Reopening a blocker or making a suggestion blocking also pauses it. Resolving it or making it non-blocking restores an otherwise approved unchanged plan without another verdict. Editing the plan instead always needs fresh approval. Implementation threads affect implementation approval separately. No stored signatures or verdicts are deleted.

## Optional execution evidence

Traces use normalized trace schema v1. `POST /api/reports` accepts optional `trace: TraceDocument | null`. Send normalized JSON, not a native provider export, file path or URL. Responses remain HTTP 201 `{id, version, url}`. Reports without traces keep their existing behavior. See [tracing.md](tracing.md) for the schema, version-pinned capture recipes, privacy limits and safe JSON assembly with curl.

| Operation | `trace` behavior |
| --- | --- |
| Create artifact with body | Object attaches; omission/null means no trace. |
| Create plan-first without body | Object rejected; omission/null means no trace. |
| Submit implementation body in either workflow | Object attaches to the new snapshot; omission/null clears evidence for this new implementation. |
| Plan-only update after implementation exists | Omission preserves the old evidence and its original binding, alongside inherited body/commit. Explicit trace, including null, is rejected. |
| Replace or remove attached evidence | Resubmit the implementation body as a new version with an object or null; identical body bytes are allowed. |

All updates supplying a trace object require `expected_version`, including artifact updates. Artifact clients without trace retain their existing guard behavior. Plan-first updates still always require the guard and the existing content rules. A trace-only update cannot replace a body or plan update. Implementation submission still requires the current plan's effective approval; a changed plan and body cannot be submitted together. Stale guards and plan-gate conflicts remain 409. Invalid evidence/content combinations return 422 and save no metadata, snapshot or trace.

The server binds evidence to the snapshot where its implementation body was submitted, that snapshot's exact plan revision when present, and the submission's claimed commit when present. Absent `plan_revision` and `commit` keys are omitted from the binding, rather than serialized as null. Clients cannot set the binding. It does not prove that the claimed commit ran. A plan-only update retains the original binding even if the currently attached plan changes.

Adding, replacing or removing evidence creates a fresh unsigned implementation snapshot. Earlier implementation signatures/verdicts still refer to their exact snapshots, including evidence. Plan signatures remain tied to exact plan revisions. Trace presence, recorded failures and coverage never change approval status automatically. Reopened plan blockers retain their existing hold behavior.

The canonical normalized document is at most **524288 UTF-8 bytes (512 KiB)**. The whole encoded request remains **1048576 bytes (1 MiB)**, including body, plan, trace and metadata; a valid trace can still push that request over budget (413). There is no compressed/archive upload, remote-fetch endpoint or mutable attachment endpoint.

Writes use the existing token and owner checks; members cannot add evidence to another member's report. API reads keep the existing authenticated-member scope; web reads keep local/shared-mode guards. Evidence is visible to the same readers as the report, with no separate access list or unguarded download route. Validation errors and logs must not echo trace text or native source records. Removing evidence from a new version does not erase history; accidental disclosure requires the instance operator's existing data-removal process.

## Read a report

`GET /api/reports/$ID` returns the report metadata, status, versions and verdicts. It returns neither Markdown document, including in `versions`. Use the exact snapshot route below to read them.

```json
{
  "id": "my-change",
  "project": "billing-service",
  "title": "Prorate mid-cycle plan changes",
  "risk": "high",
  "ticket_url": "https://example.com/tickets/BILL-412",
  "links": [{"label": "Merge request", "url": "https://gitlab.example.com/billing/billing-service/-/merge_requests/231"}],
  "author": "Alice",
  "author_id": "alice@example.com",
  "reviewers": ["bob@example.com"],
  "agent": "claude",
  "model": "opus",
  "session_url": "https://example.com/sessions/42",
  "signed_by": "Alice",
  "signed_at": "2026-10-21T09:30:00+00:00",
  "status": "changes_requested",
  "workflow": "artifact",
  "archived_at": null,
  "archived_by": null,
  "stage": "implementation",
  "implementation_status": "changes_requested",
  "plan_review": null,
  "current_version": 2,
  "versions": [
    {"version": 1, "created_at": "2026-10-21T09:00:12+00:00", "commit": "a1b2c3d", "has_implementation": true, "plan_revision": null, "has_trace": false},
    {"version": 2, "created_at": "2026-10-21T11:30:40+00:00", "commit": null, "has_implementation": true, "plan_revision": null, "has_trace": false}
  ],
  "verdicts": [
    {"reviewer": "Alice", "reviewer_id": "4f0c1a", "verdict": "approve", "target": "implementation", "version": 1, "current": false, "summary": "", "created_at": "2026-10-21T10:02:00+00:00"},
    {"reviewer": "Bob", "reviewer_id": "9be2d7", "verdict": "request_changes", "target": "implementation", "version": 2, "current": true, "summary": "Add the leap year case.", "created_at": "2026-10-21T12:15:09+00:00"}
  ]
}
```

- For artifact reviews, `status` is `unsigned`, `awaiting_review`, `changes_requested` or `approved`. It is `unsigned` until someone signs `current_version`; after that it only counts verdicts on `current_version` from people other than its signer. Earlier verdicts by the signer remain in the history.
- `archived_at` and `archived_by` (a display name) are set while the report is archived, otherwise `null`. See [Archive a report](#archive-a-report).
- `signed_by` and `signed_at` describe the signature on `current_version`, or are `null`. `agent`, `model` and `session_url` are `null` until a push sets them, or after a push clears them with `null`.
- `verdict` is `approve` or `request_changes`. `current` is true only for each reviewer's latest verdict on `current_version`. When a reviewer changes their verdict on the same version, the older one has `current: false`, like verdicts on earlier versions.

For plan-first reviews, top-level `status` is the overall gate; it can also be `awaiting_implementation`. `stage` is `plan` until the current plan is effectively approved, then `awaiting_implementation` without a body, or `implementation` with a body. `implementation_status` and the legacy implementation `signed_by`/`signed_at` are null until body attachment. Raw implementation status can remain `approved` while a reopened plan makes the overall status `changes_requested`.

`plan_review` is null for artifact reviews. For plan-first reviews it contains:

```json
{
  "revision": 1, "status": "approved", "signed_by": "Alice",
  "signed_at": "2026-10-21T09:30:00+00:00", "open_blocking": 0,
  "verdicts": [
    {"reviewer": "Bob", "reviewer_id": "bob@example.com", "target": "plan", "verdict": "approve", "version": 1, "current": true, "summary": "", "created_at": "2026-10-21T10:02:00+00:00"}
  ]
}
```

Plan `current` verdict flags compare against that plan revision, independent of the global snapshot. Top-level `verdicts` remain implementation-only. Each version-list item includes `has_implementation`, `plan_revision` and `has_trace`. Metadata/list reads do not include full trace payloads; use the exact snapshot route.

### Exact snapshot readback

`GET /api/reports/$ID/versions/2` returns the immutable stored documents:

```json
{
  "version": 2, "created_at": "2026-10-21T11:30:40+00:00", "commit": "abcdef0",
  "body": "## Verification\nTests passed.", "plan": "# Design\nUse daily proration.", "plan_revision": 1,
  "trace": null
}
```

`body` is null before implementation attachment; `plan` is null when absent. Missing review/version gets 404. This route needs the same bearer token as other reads. Each assembled metadata/feedback response reads one consistent database snapshot, but separate HTTP calls may observe different current versions.

`trace` is null when absent, otherwise a `StoredTrace` object with exactly `binding` and `document`. `binding` always contains `version` (the attachment snapshot); `plan_revision` (its original plan revision) and `commit` (the claimed commit) are included only when present. For example, an attachment at version 1 with no plan or claimed commit has `"binding": {"version": 1}`. Absent optional binding keys are omitted, not null; `document` is the validated schema-v1 `TraceDocument`. The bound plan is the exact original revision, not the latest plan or report-level agent/model/session metadata. After a plan-only update, `binding.version` can be older than the requested snapshot. See the [valid synthetic document](tracing.md#manual-reconstruction).

History preserves exact Markdown and revision-associated live feedback. It is not a frozen replay: replies, resolution and blocking state on included older threads stay live. Historical pages omit roots and verdicts from later document revisions. Metadata such as title and author still describes the latest report; it is not historically versioned or separately signed.

## Read and answer comments

```bash
curl -sS --fail-with-body -H "Authorization: Bearer $VERIFAIR_API_TOKEN" \
  "http://localhost:8000/api/reports/$ID/comments?open=true"
```

Response (`open=true` leaves out resolved threads):

```json
{
  "current_version": 2, "target": "implementation", "revision": 2,
  "threads": [
    {
      "id": 1,
      "version": 1,
      "target": "implementation",
      "parent_id": null,
      "section": "Edge cases",
      "quote": "first day of the cycle",
      "prefix": null,
      "suffix": null,
      "author": "Alice",
      "author_id": "4f0c1a",
      "author_type": "human",
      "body": "Which first day?",
      "created_at": "2026-10-21T09:14:03+00:00",
      "blocking": true,
      "resolved": false,
      "resolved_by": null,
      "stale": true,
      "found": true,
      "moved": false,
      "match_start": 0,
      "replies": [
        {
          "id": 2, "version": 1, "target": "implementation", "parent_id": 1, "section": "Edge cases", "quote": "first day of the cycle",
          "author": "agent", "author_id": "agent", "author_type": "agent",
          "body": "Cycle start day, inclusive.", "created_at": "2026-10-21T09:20:41+00:00",
          "blocking": true, "resolved": false, "resolved_by": null, "stale": true, "found": true,
          "prefix": null, "suffix": null, "moved": false, "match_start": 0
        }
      ]
    }
  ]
}
```

- `stale` is true when the comment belongs to an older revision of the selected target. An unchanged plan does not become stale just because implementation creates another global snapshot.
- `found` is true when the quote can be located in the current version, or when the thread has no quote. Matching first tries sections with the original heading, then all sections, and tolerates whitespace changes.
- `moved` is true when the quote was found in another section. `match_start` is its zero-based offset in that section’s normalized plain text, or `null` for an unlocated quote or a thread without a quote.
- `prefix` and `suffix` hold optional context immediately before and after the quote, used to choose between repeated matches.
- Replies carry the same anchor fields, `stale`, `found`, `moved` and `match_start` as their thread, and the thread's `resolved` and `resolved_by`.
- `author_type` is `agent` or `human`.
- `resolved_by` is, in shared mode, the member id of the person who resolved the thread, or `agent` when an agent token did. It is `null` for open threads, in local mode, and for threads resolved before the switch to shared mode.

Post a comment with `POST /api/reports/$ID/comments` and `{body, author?, parent_id?, section?, quote?, prefix?, suffix?, blocking?, target?, revision?}`. The response is 201 with one comment (same fields as above, without `stale`, `found`, `moved`, `match_start` and `replies`). For a reply, `resolved` and `resolved_by` describe the thread after the reply was saved, so a reply that leaves the thread resolved returns `"resolved": true`.

```bash
curl -sS --fail-with-body -X POST "http://localhost:8000/api/reports/$ID/comments" \
  -H "Authorization: Bearer $VERIFAIR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": "Cycle start day, inclusive.", "parent_id": 1}'
```

- With `parent_id` it is a reply. In local mode a reply to a resolved thread opens it again. In shared mode an agent reply opens it again only when the agent started the thread; a reply to a thread a person started leaves it resolved. A reply inherits its anchor from the thread, so any non-null `section`, `quote`, `prefix` or `suffix` with `parent_id` is rejected with 422 `"replies cannot set section, quote, prefix or suffix"`.
- Without `parent_id` it starts a new thread. `section` must be an H2 heading of the current version, exactly as written, for example `"section": "Edge cases"`. Text before the first `##` is the section `Intro`; a report with no `##` at all is one section called `Report`. `quote` points at a piece of text in that section, so `quote` without `section` is rejected with 422 `"quote requires section"`.
- `section` and `quote` are limited to 2,000 characters each; `prefix` and `suffix` to 64 each. A quote that does not occur in the named section is rejected with 422 `"quote not found"`; it is matched like `found` above, ignoring whitespace changes. A quote repeated within the named section without nonempty context is rejected with 422 `"ambiguous quote"`. Add `prefix` or `suffix` to disambiguate it. Nothing is saved when a comment is rejected.
- Without `section` the thread is a general comment on the whole report.
- `blocking` defaults to `true`. Send `false` for a nit that should not hold up approval. Replies always take `blocking` from their thread.
- In shared mode `author` is ignored. The comment shows the token owner's name with `'s agent`, for example `Jane's agent`.
- In shared mode an agent token can only comment on its owner's reports. Other reports return 403 `"This report belongs to another member."`, and reports with no owner return 403 `"This report has no owner. Push it again with an agent token under a new id."`.

### Select feedback target

Comments and wait default to `target=implementation`. For plan-first feedback explicitly request `GET /api/reports/$ID/comments?target=plan&open=true` or `/wait?target=plan`. Before body attachment, default implementation reads return no threads/events and `revision: null`. Artifact plans stay read-only: explicit plan feedback reads/writes get 422 `"artifact plans are read-only"`. Execution is read-only supporting evidence, not a feedback target; the only targets remain `implementation` and `plan`.

For a plan root send `{"body":"Which rounding rule?","target":"plan","revision":1}` using the current plan revision. A root without target defaults to implementation; its revision can be omitted for legacy clients and is then chosen under the write lock. Supplying revision guards the current selected document; stale guards return `409 {"detail":"document changed","target":"plan","current_revision":R}`. Writing implementation comments before attachment gets 409 `"implementation has not been attached"`.

Replies may omit target to inherit the root's target. An explicit different target gets 422. A supplied reply revision checks the **current** target revision, while the saved reply retains the older root's revision and anchor. This allows answering old concerns after a document edit; do not send the root's old revision as a guard against the new text. Root section/quote validation runs against the locked current Markdown.

Comment/verdict records add `target`; their legacy `version` field means that document's revision. Comments and wait envelopes, including timeout, add `current_version`, `target` and `revision`, assembled with their threads/events in one consistent read. Compare these identities with metadata and exact readback; if they differ, reread before a guarded write.

To close a thread: `POST /api/reports/$ID/comments/{comment_id}/resolve`. `comment_id` can be the first comment or any reply; the whole thread is resolved. The response is `{"resolved": true}`. Calling it again does nothing. Reviewers can reopen a resolved thread in the web UI.

In shared mode an agent token can only resolve threads the agent started, on its owner's reports. On another member's report it gets 403 `"This report belongs to another member."`. On a thread a person started it gets 403 `"Agent tokens can only resolve threads the agent started on its owner's reports."`.

## Wait for feedback

`GET /api/reports/$ID/wait` waits for a verdict or a new root comment on the current revision of the selected target. It checks the database every 0.5 seconds and returns one event. Replies, signatures, moderation (including resolve/reopen/blocking changes) and version pushes are not events. Each check follows the current selected revision, never a pinned snapshot. Feedback on older revisions is ignored, but feedback on an unchanged plan still qualifies after a body push advances the global snapshot.

```bash
curl -sS --fail-with-body --max-time 60 -G \
  -H "Authorization: Bearer $VERIFAIR_API_TOKEN" \
  --data-urlencode "for=verdict" \
  --data-urlencode "since=2026-10-21T12:15:09+00:00" \
  --data-urlencode "timeout=55" \
  "http://localhost:8000/api/reports/$ID/wait"
```

- `target` is `implementation` (default) or `plan`; select `plan` explicitly for plan-first feedback.
- `for` is `verdict`, `comment` or `any` (the default).
- `timeout` is seconds, greater than zero and at most 55; the default is 55. Fractions are accepted.
- `since` is an ISO datetime. Only events strictly newer than it qualify; offsets are compared as instants and a datetime without an offset means UTC. Omitting it snapshots existing event IDs on the first database check and waits for later inserts, including inserts in the same second. Rows inserted after that snapshot remain eligible on subsequent checks.
- An event returns `event`, `since`, `next_since` and either `verdict` (the same fields as a report verdict) or `comment` (the same fields as the POST comment response). The earliest qualifying timestamp wins; verdicts precede comments when timestamps tie.
- In shared mode every check also checks the token again. If the token is revoked or its owner is deactivated during the wait, the call ends with 401 `"invalid token"`.
- A timeout returns `{"event": "timeout", "since": "...", "next_since": "...", "current_version": 2, "target": "plan", "revision": 1}` with the same timestamp in both fields. `since` is the supplied instant normalized to UTC, or the UTC request start time when omitted. On an event, `next_since` is the event’s `created_at`.

This endpoint signals feedback; it does not provide a lossless event stream. Stored event timestamps have second precision, so reusing `next_since` excludes other events from that same second. After **every result or timeout**, reread metadata and selected-target comments before interpreting effective approval or writing. This also catches non-events such as reopening. There is no lossless replay or durable resume cursor.

## Archive a report

`POST /api/reports/{id}/archive` hides a finished or test report from the lists; `POST /api/reports/{id}/restore` brings it back. Both answer with the report, as `GET /api/reports/{id}` does. In shared mode a token may do this for reports its owner owns or was asked to review, otherwise 403. Archiving twice, or restoring a report that is not archived, changes nothing.

An archived report stays readable by its id and URL, and people can still comment on, sign and judge it. Pushing a new version to it is refused with 409 `This report is archived. Restore it before pushing a new version.`

Deleting a report for good is not in the API: an admin does it in the app.

## Get the agent skill

`GET /api/skill` returns the `verifair-review` skill (`SKILL.md`, `text/markdown`) that ships with this server, so an agent gets the instructions that match this version. It needs a token like the rest of the API.

```bash
d=~/.claude/skills/verifair-review && mkdir -p "$d" && curl -fsS -H "Authorization: Bearer $VERIFAIR_API_TOKEN" "${VERIFAIR_URL%/}/api/skill" -o "$d/SKILL.md.new" && grep -q "^name: verifair-review" "$d/SKILL.md.new" && mv "$d/SKILL.md.new" "$d/SKILL.md" || rm -f "$d/SKILL.md.new"
```

## Errors

| Status | `detail` |
| --- | --- |
| 401 | `"invalid token"`. In shared mode a bearer value that does not start with `vf_` gets `"The shared token is not accepted in shared mode. Use a member agent token."` |
| 403 | shared mode only: `"This report belongs to another member."`, `"This report has no owner. Push it again with an agent token under a new id."`, `"author_id must be the token owner."` or `"Agent tokens can only resolve threads the agent started on its owner's reports."` |
| 404 | `"report not found"`, `"version not found"`, `"parent not found"` or `"comment not found"` |
| 409 | `"id belongs to another project"`, `"workflow cannot be changed"`, `"review changed"` with `current_version`, `"document changed"` with `target`/`current_revision`, `"current plan is not approved"` with `plan_revision`, or `"implementation has not been attached"` |
| 422 | a list of field errors (pydantic style), `"replies cannot set section, quote, prefix or suffix"`, `"quote requires section"`, `"quote not found"`, `"ambiguous quote"`, `"unknown section"`, `"missing sections"`, workflow/content/guard errors described above, `"artifact plans are read-only"`, `"plan revision is required"`, `"reply target conflicts with root target"`, or, in shared mode, `"inactive reviewers"` |
| 413 | `"request body too large"`: the request body is over 1 MB (1,048,576 bytes). Checked before the body is parsed, for API and web routes |
| 503 | not JSON: the plain text `The database is not available. Try again in a moment.` with a `Retry-After: 5` header. The database cannot be reached, or no connection was free within 10 seconds. Web pages answer the same way. If the connection drops while a write is being saved, the change may already be stored: read it back before sending it again |

An unknown section also lists the valid headings: `{"detail": "unknown section", "sections": ["Intent", "Edge cases"]}`. Missing sections lists the required headings the body lacks, in the configured order. Inactive reviewers lists every requested id that is not an active member: `{"detail": "inactive reviewers", "reviewers": ["ghost@example.com"]}`.

## Report rules

[report-template.md](report-template.md) is the layout agents should follow. The [README](../README.md#the-report) lists its sections.

### Required sections

A supplied implementation body is refused with 422 if it lacks a required top-level H2 heading. Headings follow Markdown syntax, including closing hashes (`## Verification ###`) and Setext H2 headings. Headings inside code blocks and the synthetic `Intro`/`Report` sections shown by the UI do not count. The list comes from `VERIFAIR_REQUIRED_SECTIONS`. The default is `Verification,Rollback,Security impact`. Set it to an empty string to turn the check off.

### Provenance

`agent`, `model`, `session_url` and `commit` tell the reader who wrote the report and what it describes. The page shows the commit next to each snapshot. A non-null commit must accompany a supplied implementation body; plan-only updates carry any inherited body's commit. Only humans sign and give verdicts in the web UI, separately for plan and implementation. Agent tokens have no signature/verdict API.

Local mode trusts the local browser's cookie identity and shared API token; it does not prove a human's identity against a directory. Use shared mode, where people sign in with your OpenID Connect provider, for enforced membership and ownership. Plan approval does not change that trust boundary.

### Upgrade boundary

Restart all writers together after this schema upgrade. Existing reviews stay `artifact` and existing feedback stays implementation-targeted. Mixed old writers and plan-first data are unsupported. Preserve the database when rolling back; older code cannot safely interpret plan-only snapshots with null body. See [deployment.md](deployment.md) before a code/schema rollback.
