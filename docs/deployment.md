# Deployment

- [Run from source](#run-from-source)
- [Docker](#docker)
- [Deploying](#deploying)
- [Upgrading an existing HTTP deployment](#upgrading-an-existing-http-deployment)

## Run from source

To run from source, start only the database and point verifair at it:

```bash
docker compose up -d db
uv sync
export VERIFAIR_API_TOKEN=change-me
export VERIFAIR_DATABASE_URL=postgresql://verifair:verifair@localhost:5432/verifair
uv run uvicorn verifair.asgi:app
```

Every start runs the schema SQL, which is safe to repeat. It runs under an advisory lock, so several copies can start at once. `CREATE INDEX IF NOT EXISTS` takes a lock that waits behind writes in progress, so a start can wait up to 5 seconds. After that it stops with `Startup waited too long for a database lock held by another transaction. Restart to try again.`

Older versions kept their data in a SQLite file, `verifair.db`. This version does not read that file or move its data. Start with an empty database.

## Docker

```bash
docker build -t verifair .
docker run --rm -e VERIFAIR_API_TOKEN=change-me \
  -e VERIFAIR_DATABASE_URL=postgresql://verifair:verifair@host.docker.internal:5432/verifair \
  -p 127.0.0.1:8000:8000 verifair
```

This needs a running Postgres. `host.docker.internal` reaches a Postgres on your own machine (Docker Desktop). On Linux, or for another server, replace it with the database host.

The image listens on all addresses (`UVICORN_HOST=0.0.0.0`). When the auth proxy runs next to verifair in the same pod, set `UVICORN_HOST=127.0.0.1` so only the proxy can reach it. The image `HEALTHCHECK` still works then, because it runs inside the container.

## Deploying

- In local mode the web UI has no login. Anyone who can open it can comment and give verdicts. Run it on a private network or behind an auth proxy (SSO, basic auth). For a team, use shared mode (see [access.md](access.md)). The Docker example above only listens on `127.0.0.1`.
- HTTPS is required for browser access outside loopback, including private-network hostnames. Local development may use HTTP on `localhost`, `127.0.0.1` or `[::1]`. Put a TLS reverse proxy in front of the loopback listener for shared deployments. Ordinary HTTP hostnames can load pages but browser writes return 403 because browsers omit the required `Sec-Fetch-Site` header on those origins.
- To identify users from an auth proxy, set `VERIFAIR_IDENTITY_HEADER` to the header the proxy adds, for example `X-Forwarded-Email` with oauth2-proxy. This turns on shared mode. The header value becomes the user id; the display name comes from the member list. The name prompt goes away, and signing and approvals are tied to that identity. The proxy must strip this header from client requests, or anyone can pose as anyone. Requests without the header get 401, but keep the app reachable only through the proxy.
- Set `VERIFAIR_BASE_URL` to the public HTTPS URL, for example `https://verifair.example.com`. It is used in notification links and in the push response, and enables secure cookies. Keep the browser on this origin and have the reverse proxy preserve `Sec-Fetch-Site`.
- Serve it at the root of a domain. Sub-paths (like `/verifair/`) are not supported.
- verifair needs a Postgres 18 database. Its database role must own the schema and the tables, because every start runs the schema SQL, and Postgres 15 and newer does not let every role create tables in `public`. Where Postgres runs in production is not decided here.
- For a database on another host, add `sslmode=verify-full` (or at least `sslmode=require`) to `VERIFAIR_DATABASE_URL`, for example `postgresql://verifair:secret@db.example.com:5432/verifair?sslmode=verify-full`.
- Behind PgBouncer, use session mode. Transaction mode needs PgBouncer 1.21 or newer with `max_prepared_statements` set, because psycopg makes prepared statements and they cannot be turned off from the URL.
- Startup creates and updates the schema under an advisory lock, so several replicas can start at once. A start can wait up to 5 seconds behind writes in progress, then stops with a clear message. Restart it to try again.
- You can run several replicas, because all state is in Postgres. Keep `(replicas + extra pods during a rolling update) × VERIFAIR_DB_POOL_SIZE`, plus the connections you need for admin work and backups, below the server's `max_connections`.
- Back up with `pg_dump -Fc "$VERIFAIR_DATABASE_URL" > verifair.dump`. Restore into an empty database as verifair's role with `pg_restore --no-owner -d <empty database> verifair.dump`, so that role owns the restored tables.
- The image trusts `X-Forwarded-*` headers from any address. Set `FORWARDED_ALLOW_IPS` to your proxy's IP.
- `FORWARDED_ALLOW_IPS` does not protect the identity header. It only decides whose `X-Forwarded-For` and `X-Forwarded-Proto` uvicorn believes. verifair reads the identity header from every request, so anyone who can reach verifair directly, not through the proxy, can claim any identity. Make sure only the proxy can reach it: run the proxy as a sidecar and set `UVICORN_HOST=127.0.0.1`, or use a network policy that only lets the proxy in.
- Use `/healthz` for the liveness probe and `/readyz` for the readiness probe. `/healthz` does not touch the database, so a database outage does not restart pods. `/readyz` returns 200 `{"ok": true}` when the database answers `SELECT 1`, and 503 `{"ok": false}` with `Retry-After: 5` when it is down or no connection is free within 2 seconds, so the pod stops getting traffic until the database is back. Both are public, also in shared mode.
- Give both probes a timeout of at least 5 seconds (`timeoutSeconds: 5`). Under heavy load the answer can take a few seconds, and a shorter timeout restarts a pod that is only busy.
- With `UVICORN_HOST=127.0.0.1`, an HTTP probe from outside the container cannot reach `/healthz`. Use an exec probe with the same command as the image `HEALTHCHECK`, or probe `/healthz` through the proxy. oauth2-proxy then needs `--skip-auth-route=GET=^/(healthz|readyz)$`, or it asks the probe to sign in.

### Upgrading an existing HTTP deployment

Before upgrading a shared deployment, configure a trusted TLS certificate and an HTTPS reverse proxy for its hostname. Redirect the old HTTP URL to HTTPS, set `VERIFAIR_BASE_URL` to that HTTPS origin, and restart verifair behind the proxy with its existing database. Update agent URLs and bookmarked report links to HTTPS. A healthy `/healthz` response or a readable page on HTTP does not confirm that browser writes work.

After restarting, open the HTTPS URL in a browser. In local mode, set your name if needed; in shared mode, sign in as a member first. Then submit a comment on a draft report to check that writes pass through the proxy. Existing non-loopback HTTP deployments need this change before the upgrade; private-network placement alone does not enable browser writes. The API continues to use bearer tokens, and `/healthz` can still be checked over the internal loopback HTTP listener.
