# verifair

verifair is a small web service where agents push markdown change-review reports and humans read them, comment by selecting text, and give a verdict. Agents already handle code review. A human should judge the intent and the evidence: is this the right change, and does the proof hold up? verifair gives them one page per change to do that.

![verifair report page](docs/screenshot.png)

## Run it

```bash
uv sync
export VERIFAIR_API_TOKEN=change-me
uv run uvicorn verifair.asgi:app
```

Open http://localhost:8000. Reviewers set a display name (kept in a cookie), select text to comment, and approve or request changes.

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
    "body": "## Intent\nCharge upgrades by remaining days.",
    "ticket_url": "https://example.com/tickets/BILL-412",
    "links": [{"label": "Merge request", "url": "https://example.com/code/merge_requests/231"}],
    "author": "agent"
  }'
```

Response:

```json
{"id": "my-change", "version": 1, "url": "http://localhost:8000/r/my-change"}
```

The commands below use the report `id` as `$ID`.

- `id` is optional on the first push. Without it, verifair makes one from the title and returns it.
- To add a new version, push again with the same `id`. Without `id`, every push makes a new report. Comments stay on the version they were made on; the API marks them `stale` when a newer version exists and `found` when the quoted text still appears in the same-named section of the current version.
- Required: `project`, `title` (1 to 200 characters), `risk` (`low`, `medium` or `high`) and `body` (markdown).
- `project` and `id` must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`: letters, digits, `.`, `_` and `-`, starting with a letter or digit, up to 100 characters.
- `ticket_url` and `links[].url` must start with `http://` or `https://`.
- Unknown JSON keys are rejected with 422.

Report markdown: raw HTML is shown as text, not rendered, and `javascript:` links are dropped. Use plain markdown only.

`scripts/push-sample.sh` pushes the report template as a ready example with the fixed id `sample-prorate-mid-cycle`, so running it again adds a new version. It sends to `http://localhost:8000` unless you set `VERIFAIR_URL`. For any URL that is not localhost it also needs `VERIFAIR_ALLOW_REMOTE=1`:

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
  "author": "agent",
  "status": "changes_requested",
  "current_version": 2,
  "versions": [
    {"version": 1, "created_at": "2026-10-21T09:00:12+00:00"},
    {"version": 2, "created_at": "2026-10-21T11:30:40+00:00"}
  ],
  "verdicts": [
    {"reviewer": "Alice", "reviewer_id": "4f0c1a", "verdict": "approve", "version": 1, "current": false, "summary": "", "created_at": "2026-10-21T10:02:00+00:00"},
    {"reviewer": "Bob", "reviewer_id": "9be2d7", "verdict": "request_changes", "version": 2, "current": true, "summary": "Add the leap year case.", "created_at": "2026-10-21T12:15:09+00:00"}
  ]
}
```

- `status` is `awaiting_review`, `changes_requested` or `approved`. It only counts verdicts on `current_version`.
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
      "author": "Alice",
      "author_id": "4f0c1a",
      "author_type": "human",
      "body": "Which first day?",
      "created_at": "2026-10-21T09:14:03+00:00",
      "resolved": false,
      "stale": true,
      "found": true,
      "replies": [
        {
          "id": 2, "version": 1, "parent_id": 1, "section": "Edge cases", "quote": "first day of the cycle",
          "author": "agent", "author_id": "agent", "author_type": "agent",
          "body": "Cycle start day, inclusive.", "created_at": "2026-10-21T09:20:41+00:00",
          "resolved": false, "stale": true, "found": true
        }
      ]
    }
  ]
}
```

- `stale` is true when the comment was made on an older version.
- `found` is true when the quote is still in the same-named section of the current version, or when the thread has no quote.
- Replies carry the same `section`, `quote`, `stale` and `found` as their thread.
- `author_type` is `agent` or `human`.

Post a comment with `POST /api/reports/$ID/comments` and `{body, author?, parent_id?, section?, quote?}`. The response is 201 with one comment (same fields as above, without `stale`, `found` and `replies`).

```bash
curl -sS --fail-with-body -X POST "http://localhost:8000/api/reports/$ID/comments" \
  -H "Authorization: Bearer $VERIFAIR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": "Cycle start day, inclusive.", "parent_id": 1}'
```

- With `parent_id` it is a reply. A reply to a resolved thread opens it again. A reply takes its section and quote from the thread, so sending `section` or `quote` with `parent_id` is rejected with 422 `"replies cannot set section or quote"`.
- Without `parent_id` it starts a new thread. `section` must be an H2 heading of the current version, exactly as written, for example `"section": "Edge cases"`. Text before the first `##` is the section `Intro`; a report with no `##` at all is one section called `Report`. `quote` points at a piece of text in that section, so `quote` without `section` is rejected with 422 `"quote requires section"`.
- Without `section` the thread is a general comment on the whole report.

To close a thread: `POST /api/reports/$ID/comments/{comment_id}/resolve`. `comment_id` can be the first comment or any reply; the whole thread is resolved. The response is `{"resolved": true}`. Calling it again does nothing. Reviewers can reopen a resolved thread in the web UI.

## Errors

| Status | `detail` |
| --- | --- |
| 401 | `"invalid token"` |
| 404 | `"report not found"`, `"parent not found"` or `"comment not found"` |
| 409 | `"id belongs to another project"` |
| 422 | a list of field errors (pydantic style), `"replies cannot set section or quote"`, `"quote requires section"`, or `"unknown section"` |

An unknown section also lists the valid headings: `{"detail": "unknown section", "sections": ["Intent", "Edge cases"]}`.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `VERIFAIR_API_TOKEN` | required | Bearer token agents use for `/api` |
| `VERIFAIR_DB_PATH` | `./verifair.db` | SQLite file |
| `VERIFAIR_BASE_URL` | `http://localhost:8000` | Used for links in API responses and notifications |
| `VERIFAIR_SLACK_WEBHOOK_URL` | unset | Optional Slack webhook for new reports, comments and verdicts |

## Report template

[docs/report-template.md](docs/report-template.md) is the layout agents should follow. It has seven sections, each with a one-line hint shown as a quote: `Intent`, `Design`, `Edge cases`, `Verification`, `Agent reviews`, `Judge this`, `Links`.

## Docker

```bash
docker build -t verifair .
docker run --rm -e VERIFAIR_API_TOKEN=change-me -p 127.0.0.1:8000:8000 -v verifair-data:/data verifair
```

The database lives in `/data/verifair.db`, so keep `/data` on a volume. For bind mounts or Kubernetes PVCs, `/data` must be writable by uid 10001 (`chown 10001 <dir>` or `fsGroup: 10001`).

## Deploying

- The web UI has no login. Anyone who can open it can comment and give verdicts. Run it on a private network or behind an auth proxy (SSO, basic auth). The Docker example above only listens on `127.0.0.1`.
- Set `VERIFAIR_BASE_URL` to the public URL. It is used in notification links and in the push response.
- Serve it at the root of a domain. Sub-paths (like `/verifair/`) are not supported.
- `/data` must be a volume on local disk. SQLite runs in WAL mode and does not work well on network file systems.
- Back up with `sqlite3 /data/verifair.db ".backup /backup/verifair.db"`, not `cp`, which can copy a half-written file.
- Run one replica only.
- The image trusts `X-Forwarded-*` headers from any address. Set `FORWARDED_ALLOW_IPS` to your proxy's IP.

## License

MIT. See [LICENSE](LICENSE).
