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
curl -s -X POST http://localhost:8000/api/reports \
  -H "Authorization: Bearer $VERIFAIR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "project": "billing-service",
    "title": "Prorate mid-cycle plan changes",
    "risk": "high",
    "body": "## Intent\nCharge upgrades by remaining days.",
    "ticket_url": "https://example.com/tickets/BILL-412",
    "links": [{"label": "Merge request", "url": "https://example.com/code/merge_requests/231"}],
    "author": "agent"
  }'
```

Response: `{"id": "...", "version": 1, "url": "http://localhost:8000/r/..."}`.

- `project`, `title`, `risk` (`low`, `medium` or `high`) and `body` (markdown) are required.
- Pushing again with the same `id` adds a new version of that report. Comments move to the new version when their quoted text still exists.
- `GET /api/reports/{id}` returns the report, its status and the verdicts.

`scripts/push-sample.sh` pushes the report template as a ready example:

```bash
VERIFAIR_API_TOKEN=change-me bash scripts/push-sample.sh
```

## Read and answer comments

```bash
curl -s -H "Authorization: Bearer $VERIFAIR_API_TOKEN" \
  "http://localhost:8000/api/reports/$ID/comments?open=true"
```

```bash
curl -s -X POST "http://localhost:8000/api/reports/$ID/comments" \
  -H "Authorization: Bearer $VERIFAIR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": "Cycle start day, inclusive.", "parent_id": 1}'
```

To close a thread: `POST /api/reports/{id}/comments/{comment_id}/resolve`.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `VERIFAIR_API_TOKEN` | required | Bearer token agents use for `/api` |
| `VERIFAIR_DB_PATH` | `./verifair.db` | SQLite file |
| `VERIFAIR_BASE_URL` | `http://localhost:8000` | Used for links in API responses and notifications |
| `VERIFAIR_SLACK_WEBHOOK_URL` | unset | Optional Slack webhook for new reports and comments |

## Report template

[docs/report-template.md](docs/report-template.md) is the layout agents should follow. It has seven sections, each with a hidden one-line hint: `Intent`, `Design`, `Edge cases`, `Verification`, `Agent reviews`, `Judge this`, `Links`.

## Docker

```bash
docker build -t verifair .
docker run --rm -e VERIFAIR_API_TOKEN=change-me -p 8000:8000 -v verifair-data:/data verifair
```

The database lives in `/data/verifair.db`, so keep `/data` on a volume.

## License

MIT. See [LICENSE](LICENSE).
