# API

Agents use the HTTP API to push change reports, read feedback and answer it. Every call needs `Authorization: Bearer <token>`: the shared `VERIFAIR_API_TOKEN` in local mode, or the member's own agent token (`vf_...`) in shared mode. See [access.md](access.md).

The running app also serves the generated OpenAPI docs at `/docs` (in shared mode only for signed-in members).

- [Push a report](#push-a-report)
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
    "links": [{"label": "Merge request", "url": "https://example.com/code/merge_requests/231"}],
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
- Required: `project`, `title` (1 to 200 characters), `risk` (`low`, `medium` or `high`) and `body` (markdown).
- `project` and `id` must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`: letters, digits, `.`, `_` and `-`, starting with a letter or digit, up to 100 characters.
- `ticket_url`, `session_url` and `links[].url` must start with `http://` or `https://`.
- Optional `author` (up to 60 characters): the human author's display name. Defaults to `agent`. Use `author_id` to link ownership to a stable identity. In shared mode `author` is ignored and the report shows the token owner's name.
- Optional `author_id`: the owner's stable identity, separate from the display name in `author`. Optional `reviewers`: up to 50 stable identities to request reviews from. Each ID must be 1–256 characters and cannot be `agent`. In local mode it must match the user's identity exactly; in shared mode verifair turns `author_id` and `reviewers` into lower case first. Duplicate reviewers are stored once, and the report owner is left out of `reviewers` without an error.
- On later versions, omitted `author` and `author_id` retain their previous values. In local mode a supplied `author_id` transfers ownership. `null` for `author_id` also leaves the existing value unchanged.
- `reviewers` only adds review requests: the ids are added to the ones the change already has. `[]`, `null` or leaving it out changes nothing. To remove a request, use **Remove** on the review page in shared mode (see [reviewing.md](reviewing.md#request-a-review)). Requests belong to the change and survive signing, verdicts and new versions.
- Optional provenance: `agent` and `model` (up to 60 characters each), `session_url`, and `commit` (7 to 40 lowercase hex characters, the commit the version describes). The report keeps the latest `agent`, `model` and `session_url`; `commit` is stored per version.
- The body must contain every section in `VERIFAIR_REQUIRED_SECTIONS` as a top-level H2 heading, spelled exactly (case matters). Otherwise the push is refused with 422 and nothing is saved: `{"detail": "missing sections", "sections": ["Rollback"]}`. The default is `Verification,Rollback,Security impact`; set the variable to an empty string to turn the check off.
- Unknown JSON keys are rejected with 422. So is text that holds a NUL character (`\u0000`) or a lone surrogate (such as `\ud800`), because Postgres cannot store them.
- In shared mode the token owner owns the report. `author_id` can be left out; if sent, it must be the token owner, or the push gets 403 `"author_id must be the token owner."`. A push to an `id` that belongs to another member's report gets 403 `"This report belongs to another member."`, also when the token owner is an admin. A push to an `id` whose report has no owner gets 403 `"This report has no owner. Push it again with an agent token under a new id."`. Every requested reviewer must be an active member, or the push gets 422 with every id that is not: `{"detail": "inactive reviewers", "reviewers": ["ghost@example.com"]}`.

A push sends a "draft" message to Slack (if configured). The "ready for review" message is sent when the version is signed.

Report markdown: raw HTML is shown as text, not rendered, and `javascript:` links are dropped. Use plain markdown only.

`scripts/push-sample.sh` pushes the report template as a draft example with the fixed id `sample-prorate-mid-cycle`, so running it again adds a new version. It sends to `http://localhost:8000` unless you set `VERIFAIR_URL`. For any URL that is not localhost it also needs `VERIFAIR_ALLOW_REMOTE=1`:

```bash
VERIFAIR_API_TOKEN=change-me bash scripts/push-sample.sh
VERIFAIR_API_TOKEN=change-me VERIFAIR_URL=https://verifair.example.com VERIFAIR_ALLOW_REMOTE=1 bash scripts/push-sample.sh
```

## Read a report

`GET /api/reports/$ID` returns the report metadata, status, versions and verdicts. It does not return the markdown body.

```json
{
  "id": "my-change",
  "project": "billing-service",
  "title": "Prorate mid-cycle plan changes",
  "risk": "high",
  "ticket_url": "https://example.com/tickets/BILL-412",
  "links": [{"label": "Merge request", "url": "https://example.com/code/merge_requests/231"}],
  "author": "Alice",
  "author_id": "alice@example.com",
  "reviewers": ["bob@example.com"],
  "agent": "claude",
  "model": "opus",
  "session_url": "https://example.com/sessions/42",
  "signed_by": "Alice",
  "signed_at": "2026-10-21T09:30:00+00:00",
  "status": "changes_requested",
  "current_version": 2,
  "versions": [
    {"version": 1, "created_at": "2026-10-21T09:00:12+00:00", "commit": "a1b2c3d"},
    {"version": 2, "created_at": "2026-10-21T11:30:40+00:00", "commit": null}
  ],
  "verdicts": [
    {"reviewer": "Alice", "reviewer_id": "4f0c1a", "verdict": "approve", "version": 1, "current": false, "summary": "", "created_at": "2026-10-21T10:02:00+00:00"},
    {"reviewer": "Bob", "reviewer_id": "9be2d7", "verdict": "request_changes", "version": 2, "current": true, "summary": "Add the leap year case.", "created_at": "2026-10-21T12:15:09+00:00"}
  ]
}
```

- `status` is `unsigned`, `awaiting_review`, `changes_requested` or `approved`. It is `unsigned` until someone signs `current_version`; after that it only counts verdicts on `current_version` from people other than its signer. Earlier verdicts by the signer remain in the history.
- `signed_by` and `signed_at` describe the signature on `current_version`, or are `null`. `agent`, `model` and `session_url` are `null` when the push did not send them.
- `verdict` is `approve` or `request_changes`. `current` is true only for each reviewer's latest verdict on `current_version`. When a reviewer changes their verdict on the same version, the older one has `current: false`, like verdicts on earlier versions.

## Read and answer comments

```bash
curl -sS --fail-with-body -H "Authorization: Bearer $VERIFAIR_API_TOKEN" \
  "http://localhost:8000/api/reports/$ID/comments?open=true"
```

Response (`open=true` leaves out resolved threads):

```json
{
  "threads": [
    {
      "id": 1,
      "version": 1,
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
          "id": 2, "version": 1, "parent_id": 1, "section": "Edge cases", "quote": "first day of the cycle",
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

- `stale` is true when the comment was made on an older version.
- `found` is true when the quote can be located in the current version, or when the thread has no quote. Matching first tries sections with the original heading, then all sections, and tolerates whitespace changes.
- `moved` is true when the quote was found in another section. `match_start` is its zero-based offset in that section’s normalized plain text, or `null` for an unlocated quote or a thread without a quote.
- `prefix` and `suffix` hold optional context immediately before and after the quote, used to choose between repeated matches.
- Replies carry the same anchor fields, `stale`, `found`, `moved` and `match_start` as their thread, and the thread's `resolved` and `resolved_by`.
- `author_type` is `agent` or `human`.
- `resolved_by` is, in shared mode, the member id of the person who resolved the thread, or `agent` when an agent token did. It is `null` for open threads, in local mode, and for threads resolved before the switch to shared mode.

Post a comment with `POST /api/reports/$ID/comments` and `{body, author?, parent_id?, section?, quote?, prefix?, suffix?, blocking?}`. The response is 201 with one comment (same fields as above, without `stale`, `found`, `moved`, `match_start` and `replies`). For a reply, `resolved` and `resolved_by` describe the thread after the reply was saved, so a reply that leaves the thread resolved returns `"resolved": true`.

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

To close a thread: `POST /api/reports/$ID/comments/{comment_id}/resolve`. `comment_id` can be the first comment or any reply; the whole thread is resolved. The response is `{"resolved": true}`. Calling it again does nothing. Reviewers can reopen a resolved thread in the web UI.

In shared mode an agent token can only resolve threads the agent started, on its owner's reports. On another member's report it gets 403 `"This report belongs to another member."`. On a thread a person started it gets 403 `"Agent tokens can only resolve threads the agent started on its owner's reports."`.

## Wait for feedback

`GET /api/reports/$ID/wait` waits for a verdict or a new root comment on the latest version. It checks the database every 0.5 seconds and returns one event. Replies, signatures, resolutions and version pushes are not events. Each check uses the latest version, so feedback on an older version is ignored even if it arrives during the wait.

```bash
curl -sS --fail-with-body --max-time 60 -G \
  -H "Authorization: Bearer $VERIFAIR_API_TOKEN" \
  --data-urlencode "for=verdict" \
  --data-urlencode "since=2026-10-21T12:15:09+00:00" \
  --data-urlencode "timeout=55" \
  "http://localhost:8000/api/reports/$ID/wait"
```

- `for` is `verdict`, `comment` or `any` (the default).
- `timeout` is seconds, greater than zero and at most 55; the default is 55. Fractions are accepted.
- `since` is an ISO datetime. Only events strictly newer than it qualify; offsets are compared as instants and a datetime without an offset means UTC. Omitting it snapshots existing event IDs on the first database check and waits for later inserts, including inserts in the same second. Rows inserted after that snapshot remain eligible on subsequent checks.
- An event returns `event`, `since`, `next_since` and either `verdict` (the same fields as a report verdict) or `comment` (the same fields as the POST comment response). The earliest qualifying timestamp wins; verdicts precede comments when timestamps tie.
- In shared mode every check also checks the token again. If the token is revoked or its owner is deactivated during the wait, the call ends with 401 `"invalid token"`.
- A timeout returns `{"event": "timeout", "since": "...", "next_since": "..."}` with the same timestamp in both fields. `since` is the supplied instant normalized to UTC, or the UTC request start time when omitted. On an event, `next_since` is the event’s `created_at`.

This endpoint signals feedback; it does not provide a lossless event stream. Stored event timestamps have second precision, so reusing `next_since` excludes other events from that same second. Read the report and comment endpoints after a signal to collect all feedback.

## Errors

| Status | `detail` |
| --- | --- |
| 401 | `"invalid token"`. In shared mode a bearer value that does not start with `vf_` gets `"The shared token is not accepted in shared mode. Use a member agent token."` |
| 403 | shared mode only: `"This report belongs to another member."`, `"This report has no owner. Push it again with an agent token under a new id."`, `"author_id must be the token owner."` or `"Agent tokens can only resolve threads the agent started on its owner's reports."` |
| 404 | `"report not found"`, `"parent not found"` or `"comment not found"` |
| 409 | `"id belongs to another project"` |
| 422 | a list of field errors (pydantic style), `"replies cannot set section, quote, prefix or suffix"`, `"quote requires section"`, `"quote not found"`, `"ambiguous quote"`, `"unknown section"`, `"missing sections"` or, in shared mode, `"inactive reviewers"` |
| 413 | `"request body too large"`: the request body is over 1 MB (1,048,576 bytes). Checked before the body is parsed, for API and web routes |
| 503 | not JSON: the plain text `The database is not available. Try again in a moment.` with a `Retry-After: 5` header. The database cannot be reached, or no connection was free within 10 seconds. Web pages answer the same way. If the connection drops while a write is being saved, the change may already be stored: read it back before sending it again |

An unknown section also lists the valid headings: `{"detail": "unknown section", "sections": ["Intent", "Edge cases"]}`. Missing sections lists the required headings the body lacks, in the configured order. Inactive reviewers lists every requested id that is not an active member: `{"detail": "inactive reviewers", "reviewers": ["ghost@example.com"]}`.

## Report rules

[report-template.md](report-template.md) is the layout agents should follow. The [README](../README.md#the-report) lists its sections.

### Required sections

A push is refused with 422 if the body lacks a required top-level H2 heading. Headings follow Markdown syntax, including closing hashes (`## Verification ###`) and Setext H2 headings. Headings inside code blocks and the synthetic `Intro`/`Report` sections shown by the UI do not count. The list comes from `VERIFAIR_REQUIRED_SECTIONS`. The default is `Verification,Rollback,Security impact`. Set it to an empty string to turn the check off.

### Provenance

`agent`, `model`, `session_url` and `commit` tell the reader who wrote the report and what it describes. The page shows who wrote it and the commit next to each version. Agents should also add a trailer to their own commits:

```
Assisted-by: AGENT:MODEL
```

For example `Assisted-by: claude:opus`. Agents do not add `Signed-off-by`. Only a human does that.
