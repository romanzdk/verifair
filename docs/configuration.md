# Configuration

verifair reads its settings from environment variables.

| Variable | Default | Meaning |
| --- | --- | --- |
| `VERIFAIR_API_TOKEN` | required in local mode | Bearer token agents use for `/api` in local mode. Not accepted in shared mode (startup logs a warning if it is set) |
| `VERIFAIR_DATABASE_URL` | required | Postgres connection URL (libpq format), for example `postgresql://verifair:verifair@localhost:5432/verifair` |
| `VERIFAIR_DB_POOL_SIZE` | `10` | Most database connections one verifair process opens. A request waits up to 10 seconds in total for a free one, then gets 503 with `Retry-After` |
| `VERIFAIR_BASE_URL` | `http://localhost:8000` | Used for links in API responses and notifications. In shared mode it must be the HTTPS URL people open: sign-in sends them back to `VERIFAIR_BASE_URL/auth/callback` |
| `VERIFAIR_REQUIRED_SECTIONS` | `Verification,Rollback,Security impact` | Comma-separated H2 headings every pushed body must have; empty turns the check off |
| `VERIFAIR_OIDC_ISSUER` | unset | Issuer URL of your OpenID Connect sign-in provider, for example `https://accounts.google.com`. Setting it turns on shared mode (see [access.md](access.md#set-up-shared-mode)). Must be HTTPS, except `localhost` and `127.0.0.1` |
| `VERIFAIR_OIDC_CLIENT_ID` | unset | Shared mode: client ID of the OAuth client at your provider. Required when the issuer is set |
| `VERIFAIR_OIDC_CLIENT_SECRET` | unset | Shared mode: client secret of that OAuth client. Required when the issuer is set |
| `VERIFAIR_SESSION_SECRET` | unset | Shared mode: secret that signs the session cookie, at least 32 characters, the same on every replica. Create one with `openssl rand -base64 48`. Changing it signs everyone out. Required when the issuer is set |
| `VERIFAIR_OIDC_ALLOWED_DOMAINS` | unset | Shared mode: comma-separated email domains that may sign in, for example `example.com`. With Google, the account must also belong to one of these Workspace domains. Unset means any verified email can sign in (it still needs to be a member) |
| `VERIFAIR_BOOTSTRAP_ADMIN_ID` | unset | Shared mode: email of the first admin, created when the database has no members. Startup fails if it is set without `VERIFAIR_OIDC_ISSUER` |
| `VERIFAIR_IDENTITY_HEADER` | removed | The trusted-header mode was removed. Startup fails while it is set. See [access.md](access.md#moving-from-the-identity-header) |
| `VERIFAIR_SLACK_WEBHOOK_URL` | unset | Optional Slack webhook for draft reports, ready-for-review notifications, comments and verdicts |
| `VERIFAIR_SMTP_URL` | unset | Optional mail server for review request emails (shared mode only, see [reviewing.md](reviewing.md#emails); in local mode startup logs a warning and no email is sent). `smtp://user:password@host:587` uses STARTTLS when the server offers it; `smtps://user:password@host:465` uses TLS from the start. Encode special characters in the user and password (`@` is `%40`). Without a port, `smtp://` uses 25 and `smtps://` 465. Startup fails when the URL is not `smtp://` or `smtps://` with a host |
| `VERIFAIR_EMAIL_FROM` | unset | Sender address of those emails: one plain address, for example `verifair@example.com`. Required when `VERIFAIR_SMTP_URL` is set; startup fails when it is not one plain address |

For a remote mail server use `smtps://`, or `smtp://` to a server that offers STARTTLS. verifair never sends the user and password without TLS: when a `smtp://` URL has a user and the server does not offer STARTTLS, it logs one warning (`email not sent report=<id> recipients=<n>: the server offers no TLS, ...`) and sends nothing. `smtp://` without a user still sends without TLS, for a local relay. verifair checks the server's certificate in both modes. The URL, password and addresses are never logged; a failed email logs `email failed report=<id> recipients=<n> error=<error type> code=<SMTP code or ->`, one line per recipient the server refuses, or one for the rest of the batch when the connection or login fails, and the request that sent it still succeeds. At most two emails are sent at the same time.

The Docker image also uses these uvicorn settings:

| Variable | Default in the image | Meaning |
| --- | --- | --- |
| `UVICORN_HOST` | `0.0.0.0` | Address to listen on. Set `127.0.0.1` when a TLS proxy runs next to verifair in the same pod. See [deployment.md](deployment.md#docker) |
| `FORWARDED_ALLOW_IPS` | `*` | Which addresses uvicorn trusts for `X-Forwarded-*` headers. Set it to your proxy's IP. See [deployment.md](deployment.md#deploying) |
