# verifair

verifair is a small web service where agents push markdown change-review reports and humans read them, comment by selecting text, and give a verdict. Agents already handle code review. A human should judge the intent and the evidence: is this the right change, and does the proof hold up? verifair gives them one page per change to do that.

![verifair report page](docs/screenshot.png)

## How it works

1. The agent pushes a report. It is a draft and its status is `unsigned`. The team is not told to review it yet.
2. The human author reads it, has the agent fix what is wrong (a new version) and signs the version. Signing says: I read this and I stand behind it. The team now gets a "ready for review" message.
3. Reviewers select text and comment. A comment is a blocking thread by default, or a non-blocking suggestion. The agent can read and answer threads through the API.
4. A reviewer approves or requests changes. The person who signed cannot approve.
5. A new version has no signature, so it goes back to `unsigned`. Signatures and verdicts are kept per version.

## Run it

```bash
uv sync
export VERIFAIR_API_TOKEN=change-me
uv run uvicorn verifair.asgi:app
```

Open http://localhost:8000. Users set a display name (kept in a cookie). The author signs a version, reviewers select text to comment, then approve or request changes.

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
    "agent": "claude",
    "model": "opus",
    "commit": "a1b2c3d"
  }'
```

Response:

```json
{"id": "my-change", "version": 1, "url": "http://localhost:8000/r/my-change"}
```

The commands below use the report `id` as `$ID`.

- `id` is optional on the first push. Without it, verifair makes one from the title and returns it.
- To add a new version, push again with the same `id`. Without `id`, every push makes a new report. Comments stay on the version they were made on; the API marks them `stale` when a newer version exists and `found` when the quoted text can be located in the current version, including after a section rename or move.
- Required: `project`, `title` (1 to 200 characters), `risk` (`low`, `medium` or `high`) and `body` (markdown).
- `project` and `id` must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`: letters, digits, `.`, `_` and `-`, starting with a letter or digit, up to 100 characters.
- `ticket_url`, `session_url` and `links[].url` must start with `http://` or `https://`.
- Optional `author` (up to 60 characters): the human who owns the report and should sign it. The default is `agent`, which means anyone with a name may sign.
- Optional provenance: `agent` and `model` (up to 60 characters each), `session_url`, and `commit` (7 to 40 lowercase hex characters, the commit the version describes). The report keeps the latest `agent`, `model` and `session_url`; `commit` is stored per version.
- The body must contain every section in `VERIFAIR_REQUIRED_SECTIONS` as an H2 heading, spelled exactly (case matters). Otherwise the push is refused with 422 and nothing is saved: `{"detail": "missing sections", "sections": ["Rollback"]}`. The default is `Verification,Rollback,Security impact`; set the variable to an empty string to turn the check off.
- Unknown JSON keys are rejected with 422.

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

- `status` is `unsigned`, `awaiting_review`, `changes_requested` or `approved`. It is `unsigned` until someone signs `current_version`; after that it only counts verdicts on `current_version`.
- `signed_by` and `signed_at` describe the signature on `current_version`, or are `null`. `agent`, `model` and `session_url` are `null` when the push did not send them.
- `verdict` is `approve` or `request_changes`. `current` is true when the verdict was given on `current_version`.

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
      "stale": true,
      "found": true,
      "moved": false,
      "match_start": 0,
      "replies": [
        {
          "id": 2, "version": 1, "parent_id": 1, "section": "Edge cases", "quote": "first day of the cycle",
          "author": "agent", "author_id": "agent", "author_type": "agent",
          "body": "Cycle start day, inclusive.", "created_at": "2026-10-21T09:20:41+00:00",
          "blocking": true, "resolved": false, "stale": true, "found": true,
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
- Replies carry the same anchor fields, `stale`, `found`, `moved` and `match_start` as their thread.
- `author_type` is `agent` or `human`.

Post a comment with `POST /api/reports/$ID/comments` and `{body, author?, parent_id?, section?, quote?, prefix?, suffix?, blocking?}`. The response is 201 with one comment (same fields as above, without `stale`, `found`, `moved`, `match_start` and `replies`).

```bash
curl -sS --fail-with-body -X POST "http://localhost:8000/api/reports/$ID/comments" \
  -H "Authorization: Bearer $VERIFAIR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": "Cycle start day, inclusive.", "parent_id": 1}'
```

- With `parent_id` it is a reply. A reply to a resolved thread opens it again. A reply inherits its anchor from the thread, so any non-null `section`, `quote`, `prefix` or `suffix` with `parent_id` is rejected with 422 `"replies cannot set section, quote, prefix or suffix"`.
- Without `parent_id` it starts a new thread. `section` must be an H2 heading of the current version, exactly as written, for example `"section": "Edge cases"`. Text before the first `##` is the section `Intro`; a report with no `##` at all is one section called `Report`. `quote` points at a piece of text in that section, so `quote` without `section` is rejected with 422 `"quote requires section"`.
- `section` and `quote` are limited to 2,000 characters each; `prefix` and `suffix` to 64 each. A quote repeated within the named section without nonempty context is rejected with 422 `"ambiguous quote"`. Add `prefix` or `suffix` to disambiguate it.
- Without `section` the thread is a general comment on the whole report.
- `blocking` defaults to `true`. Send `false` for a nit that should not hold up approval. Replies always take `blocking` from their thread.

To close a thread: `POST /api/reports/$ID/comments/{comment_id}/resolve`. `comment_id` can be the first comment or any reply; the whole thread is resolved. The response is `{"resolved": true}`. Calling it again does nothing. Reviewers can reopen a resolved thread in the web UI.

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
- A timeout returns `{"event": "timeout", "since": "...", "next_since": "..."}` with the same timestamp in both fields. `since` is the supplied instant normalized to UTC, or the UTC request start time when omitted. On an event, `next_since` is the event’s `created_at`.

This endpoint signals feedback; it does not provide a lossless event stream. Stored event timestamps have second precision, so reusing `next_since` excludes other events from that same second. Read the report and comment endpoints after a signal to collect all feedback.

## Errors

| Status | `detail` |
| --- | --- |
| 401 | `"invalid token"` |
| 404 | `"report not found"`, `"parent not found"` or `"comment not found"` |
| 409 | `"id belongs to another project"` |
| 422 | a list of field errors (pydantic style), `"replies cannot set section, quote, prefix or suffix"`, `"quote requires section"`, `"ambiguous quote"`, `"unknown section"` or `"missing sections"` |

An unknown section also lists the valid headings: `{"detail": "unknown section", "sections": ["Intent", "Edge cases"]}`. Missing sections lists the required headings the body lacks, in the configured order.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `VERIFAIR_API_TOKEN` | required | Bearer token agents use for `/api` |
| `VERIFAIR_DB_PATH` | `./verifair.db` | SQLite file |
| `VERIFAIR_BASE_URL` | `http://localhost:8000` | Used for links in API responses and notifications |
| `VERIFAIR_REQUIRED_SECTIONS` | `Verification,Rollback,Security impact` | Comma-separated H2 headings every pushed body must have; empty turns the check off |
| `VERIFAIR_IDENTITY_HEADER` | unset | Name of a request header that carries the user identity (see Deploying) |
| `VERIFAIR_SLACK_WEBHOOK_URL` | unset | Optional Slack webhook for draft reports, ready-for-review notifications, comments and verdicts |

## Report template

[docs/report-template.md](docs/report-template.md) is the layout agents should follow. Each section has a one-line hint shown as a quote. What the human must decide comes first, the evidence after.

| Section | Why it is there |
| --- | --- |
| `Intent` | The problem, the ticket and what is out of scope. |
| `Judge this` | The questions only a human can answer, so reviewers know where to look. |
| `Design` | How it works, and one option that was rejected. |
| `Edge cases` | One row per worry, with the check that proves it. |
| `Verification` | What each check proves, the command, the result (✅, ⚠️ or ❌, with a reason for anything not run), a link, and whether it fails if the change is reverted. |
| `Not verified / assumptions` | What was not checked or was taken on trust. |
| `Rollback` | How to undo the change. |
| `Security impact` | What it touches that matters for security, or `None` with the reason. |
| `Agent reviews` | What the agent reviewers found, how bad it was, what was done and why. |
| `Links` | Ticket, deploy, notebook, and the code last. |

### Required sections

A push is refused with 422 if the body lacks a required H2 heading. The list comes from `VERIFAIR_REQUIRED_SECTIONS`. The default is `Verification,Rollback,Security impact`. Set it to an empty string to turn the check off.

### Provenance

`agent`, `model`, `session_url` and `commit` tell the reader who wrote the report and what it describes. The page shows who wrote it and the commit next to each version. Agents should also add a trailer to their own commits:

```
Assisted-by: AGENT:MODEL
```

For example `Assisted-by: claude:opus`. Agents do not add `Signed-off-by`. Only a human does that.

## Signing

- A version must be signed before anyone can approve or request changes on it. Until then the status is `unsigned`.
- The first signature on a version is kept. Signing again does nothing.
- Without `VERIFAIR_IDENTITY_HEADER`, anyone who has set a name can sign. The name is only a cookie, so this is a convenience, not a control.
- With `VERIFAIR_IDENTITY_HEADER`, a report whose `author` is a person can only be signed by that person, so `author` must equal the header value (for example the email). A report whose `author` is `agent` can be signed by anyone.
- The person who signed a version cannot approve it or request changes on it. Someone else has to.
- Only with the identity header behind an auth proxy is signing a real control. See Deploying.

## Blocking threads

- A new thread is blocking by default. A reviewer can tick "Non-blocking (suggestion)" when writing it, and switch a thread between blocking and non-blocking later.
- Approve is refused while any blocking thread is open. Resolve the thread or mark it non-blocking first.
- Request changes needs a summary, or an open blocking thread of your own.
- The sidebar counts open blocking threads and suggestions.

## Docker

```bash
docker build -t verifair .
docker run --rm -e VERIFAIR_API_TOKEN=change-me -p 127.0.0.1:8000:8000 -v verifair-data:/data verifair
```

The database lives in `/data/verifair.db`, so keep `/data` on a volume. For bind mounts or Kubernetes PVCs, `/data` must be writable by uid 10001 (`chown 10001 <dir>` or `fsGroup: 10001`).

## Deploying

- The web UI has no login. Anyone who can open it can comment and give verdicts. Run it on a private network or behind an auth proxy (SSO, basic auth). The Docker example above only listens on `127.0.0.1`.
- To identify users from an auth proxy, set `VERIFAIR_IDENTITY_HEADER` to the header the proxy adds, for example `X-Forwarded-Email` with oauth2-proxy. The header value becomes the user id and the part before `@` the display name. The name prompt goes away, and signing and approvals are tied to that identity. The proxy must strip this header from client requests, or anyone can pose as anyone. Requests without the header fall back to the name cookie, so keep the app reachable only through the proxy.
- Set `VERIFAIR_BASE_URL` to the public URL. It is used in notification links and in the push response.
- Serve it at the root of a domain. Sub-paths (like `/verifair/`) are not supported.
- `/data` must be a volume on local disk. SQLite runs in WAL mode and does not work well on network file systems.
- Back up with `sqlite3 /data/verifair.db ".backup /backup/verifair.db"`, not `cp`, which can copy a half-written file.
- Run one replica only.
- The image trusts `X-Forwarded-*` headers from any address. Set `FORWARDED_ALLOW_IPS` to your proxy's IP.

## Related projects

verifair reviews the change report, not the diff.

- Tools that review code feed its "Agent reviews" section: CodeRabbit, Greptile, Qodo, Devin Review, Claude Code Review.
- Tools that review plans before the work starts sit earlier in the flow: Kiro, [Plannotator](https://github.com/backnotprop/plannotator).
- [rcliao/comments](https://github.com/rcliao/comments) is the closest one: markdown review with a human-only verdict for agents. verifair adds the evidence template, versions, signing, a web UI for a team, and an API for agents to read and answer comments.

## License

MIT. See [LICENSE](LICENSE).
