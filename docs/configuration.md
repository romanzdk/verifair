# Configuration

verifair reads its settings from environment variables.

| Variable | Default | Meaning |
| --- | --- | --- |
| `VERIFAIR_API_TOKEN` | required in local mode | Bearer token agents use for `/api` in local mode. Not accepted in shared mode (startup logs a warning if it is set) |
| `VERIFAIR_DATABASE_URL` | required | Postgres connection URL (libpq format), for example `postgresql://verifair:verifair@localhost:5432/verifair` |
| `VERIFAIR_DB_POOL_SIZE` | `10` | Most database connections one verifair process opens. A request waits up to 10 seconds in total for a free one, then gets 503 with `Retry-After` |
| `VERIFAIR_BASE_URL` | `http://localhost:8000` | Used for links in API responses and notifications |
| `VERIFAIR_REQUIRED_SECTIONS` | `Verification,Rollback,Security impact` | Comma-separated H2 headings every pushed body must have; empty turns the check off |
| `VERIFAIR_IDENTITY_HEADER` | unset | Name of a request header that carries the user identity. Setting it turns on shared mode (see [access.md](access.md)) |
| `VERIFAIR_BOOTSTRAP_ADMIN_ID` | unset | Shared mode: id of the first admin, created when the database has no members. Startup fails if it is set without `VERIFAIR_IDENTITY_HEADER` |
| `VERIFAIR_SLACK_WEBHOOK_URL` | unset | Optional Slack webhook for draft reports, ready-for-review notifications, comments and verdicts |

The Docker image also uses these uvicorn settings:

| Variable | Default in the image | Meaning |
| --- | --- | --- |
| `UVICORN_HOST` | `0.0.0.0` | Address to listen on. Set `127.0.0.1` when an auth proxy runs next to verifair in the same pod. See [deployment.md](deployment.md#docker) |
| `FORWARDED_ALLOW_IPS` | `*` | Which addresses uvicorn trusts for `X-Forwarded-*` headers. Set it to your proxy's IP. See [deployment.md](deployment.md#deploying) |
