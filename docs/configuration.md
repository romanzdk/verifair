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
| `VERIFAIR_SMTP_URL` | unset | Optional mail server for review request emails (shared mode only, see [reviewing.md](reviewing.md#emails); in local mode startup logs a warning and no email is sent). `smtp://user:password@host:587` uses STARTTLS when the server offers it; `smtps://user:password@host:465` uses TLS from the start. Encode special characters in the user and password (`@` is `%40`). Without a port, `smtp://` uses 25 and `smtps://` 465. Startup fails when the URL is not `smtp://` or `smtps://` with a host |
| `VERIFAIR_EMAIL_FROM` | unset | Sender address of those emails: one plain address, for example `verifair@example.com`. Required when `VERIFAIR_SMTP_URL` is set; startup fails when it is not one plain address |

For a remote mail server use `smtps://`, or `smtp://` to a server that offers STARTTLS. verifair never sends the user and password without TLS: when a `smtp://` URL has a user and the server does not offer STARTTLS, it logs one warning (`email not sent report=<id> recipients=<n>: the server offers no TLS, ...`) and sends nothing. `smtp://` without a user still sends without TLS, for a local relay. verifair checks the server's certificate in both modes. The URL, password and addresses are never logged; a failed email logs `email failed report=<id> recipients=<n> error=<error type> code=<SMTP code or ->`, one line per refused recipient or one for the whole batch when the connection or login fails, and the request that sent it still succeeds. At most two emails are sent at the same time.

The Docker image also uses these uvicorn settings:

| Variable | Default in the image | Meaning |
| --- | --- | --- |
| `UVICORN_HOST` | `0.0.0.0` | Address to listen on. Set `127.0.0.1` when an auth proxy runs next to verifair in the same pod. See [deployment.md](deployment.md#docker) |
| `FORWARDED_ALLOW_IPS` | `*` | Which addresses uvicorn trusts for `X-Forwarded-*` headers. Set it to your proxy's IP. See [deployment.md](deployment.md#deploying) |
