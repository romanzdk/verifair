# Deployment

- [Deploy with your agent](#deploy-with-your-agent)
- [Run from source](#run-from-source)
- [Docker](#docker)
- [Kubernetes](#kubernetes)
- [Deploying](#deploying)
- [Upgrading an existing HTTP deployment](#upgrading-an-existing-http-deployment)

## Deploy with your agent

Paste this into your coding agent (Claude Code, Codex or another). It asks what you need, shows its plan before it changes anything, and at the end connects itself to verifair:

```text
Set up verifair (https://github.com/romanzdk/verifair) for me.
Clone it at its latest release tag (the newest vX.Y.Z) and follow docs/deployment.md
from that checkout (docs/access.md for sign-in).

1. Ask me: run it on this machine with Docker, or deploy it to Kubernetes?
2. Ask only what that choice needs, and pick sensible defaults for the rest:
   - Docker: nothing more.
   - Kubernetes: which kube context and namespace, the bundled Postgres or my own,
     and whether I only want to try it (port-forward) or run it for a team
     (domain, TLS, sign-in provider).
3. Show me the plan and the commands, and wait for my OK before you change anything.
4. Rules: use the release tag, not main. Never put secrets in git or ask me to paste
   them in chat. Never put verifair on a domain without sign-in. Check that the
   ports you use are free.
5. When it runs: check that /readyz answers and give me the URL. Then install the
   verifair-review skill and save VERIFAIR_URL and VERIFAIR_API_TOKEN where your
   new sessions read them. In shared mode the token comes after the first sign-in:
   tell me how to create it and how to give it to you outside this chat.
```

## Run from source

To run from source, start a database and point verifair at it:

```bash
docker run -d --name verifair-dev-db -e POSTGRES_USER=verifair -e POSTGRES_PASSWORD=verifair \
  -p 127.0.0.1:5432:5432 postgres:18
uv sync
export VERIFAIR_API_TOKEN=change-me
export VERIFAIR_DATABASE_URL=postgresql://verifair:verifair@localhost:5432/verifair
uv run uvicorn verifair.asgi:app
```

Every start runs the schema SQL, which is safe to repeat. It runs under an advisory lock, so several copies can start at once. `CREATE INDEX IF NOT EXISTS` takes a lock that waits behind writes in progress, so a start can wait up to 5 seconds. After that it stops with `Startup waited too long for a database lock held by another transaction. Restart to try again.`

Plan-first reviews add a fixed workflow and target-specific review records, allow a snapshot without an implementation, and backfill the revision associated with each saved plan. Existing reports remain artifact reviews; existing comments, signatures and verdicts remain implementation records. The migration preserves saved Markdown and is safe to repeat under the same startup lock. Back up first, stop older application instances, and restart all instances on this version before accepting plan-first submissions. Do not mix older readers or writers with migrated data: they cannot inherit plans correctly and assume every snapshot has an implementation body. Downgrading is not supported after plan-first records exist; restore the backup into a separate empty database if a rollback is needed. Keep the migrated database for its review history.

Older versions kept their data in a SQLite file, `verifair.db`. This version does not read that file or move its data. Start with an empty database.

## Docker

CI publishes the image to `ghcr.io/romanzdk/verifair` for `linux/amd64` and `linux/arm64`. It is public, so pulling needs no login.

| Tag | Published on |
| --- | --- |
| `main`, `sha-<short commit>` (for example `sha-1787484`) | every push to `main` (`main` only moves for the newest commit, so a slower run for an older commit leaves it alone) |
| `1.2.3`, `latest`, `sha-<short commit>` | a `v1.2.3` git tag on a commit in `main` |

Each `v1.2.3` tag also creates a GitHub release. Its notes are that version's section of the [changelog](../CHANGELOG.md), with the image and skill install commands.

Other git tags, like `v1.2` or `v1.2.3-rc.1`, fail the publish job and publish nothing.

Deploy a version tag or `sha-<short commit>`, not `main` or `latest`, so a restart does not pull a different version. To build it yourself, run `docker build -t verifair .`.

To run it on your own machine with its own database, use Docker Compose from a release tag checkout. Put your own token in `.env` first, because Compose reads it from there:

```bash
git checkout v1.2.3
printf 'VERIFAIR_API_TOKEN=%s\n' "$(openssl rand -hex 32)" > .env
docker compose up -d
```

Compose pulls the image of the release you checked out. `docker compose up --build` builds your checkout instead. Postgres is reachable only inside Compose, so it does not clash with a Postgres already on your machine.

```bash
docker run --rm -e VERIFAIR_API_TOKEN=change-me \
  -e VERIFAIR_DATABASE_URL=postgresql://verifair:verifair@host.docker.internal:5432/verifair \
  -p 127.0.0.1:8000:8000 ghcr.io/romanzdk/verifair:1.2.3
```

The image runs as user `10001`, not root, so it works with Kubernetes `runAsNonRoot: true`. With `readOnlyRootFilesystem: true`, mount an `emptyDir` at `/tmp`, because `uv run` writes there at start.

This needs a running Postgres. `host.docker.internal` reaches a Postgres on your own machine (Docker Desktop). On Linux, or for another server, replace it with the database host.

The image listens on all addresses (`UVICORN_HOST=0.0.0.0`). When a TLS proxy runs next to verifair in the same pod, you can set `UVICORN_HOST=127.0.0.1` so only the proxy can reach it. The image `HEALTHCHECK` still works then, because it runs inside the container.

## Kubernetes

[`deploy/kustomize`](../deploy/kustomize) has [Kustomize](https://kustomize.io/) manifests. They need Kubernetes 1.30 or newer.

| Folder | What it has |
| --- | --- |
| `base` | verifair's Deployment and Service. Settings come from the ConfigMap `verifair-config` and the Secret `verifair-secrets` |
| `postgres` | optional: one Postgres 18 pod with a 5 GiB volume, a network policy that lets only verifair reach it, and `VERIFAIR_DATABASE_URL` pointing to it. The password comes from the Secret `verifair-db` |
| `example` | the overlay to copy: namespace `verifair` (Pod Security `restricted`), the settings, the secrets and the bundled Postgres |

The image tag in `base` is the version of that checkout. Use a release tag (`git checkout v1.2.3`): on `main`, a version that was just released can need a few minutes before its image is published.

### Try it

This runs verifair in local mode, with no sign-in, and opens it through a port-forward:

```bash
cd deploy/kustomize/example
umask 077
[ -f secrets.env ] || printf 'VERIFAIR_API_TOKEN=%s\n' "$(openssl rand -hex 32)" > secrets.env
[ -f db.env ] || printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 32)" > db.env
kubectl apply -k .
kubectl -n verifair rollout status deploy/verifair
kubectl -n verifair port-forward svc/verifair 8000:80
```

Open http://localhost:8000. To add the demo reviews, run this from the repository root in another terminal:

```bash
VERIFAIR_API_TOKEN=$(sed -n 's/^VERIFAIR_API_TOKEN=//p' deploy/kustomize/example/secrets.env) python3 scripts/demo.py
```

If port 8000 is taken on your machine, forward another one (`8080:80`), set `VERIFAIR_BASE_URL=http://localhost:8080` in `kustomization.yaml`, and use that URL in the browser and as `VERIFAIR_URL` (also for `scripts/demo.py`). The port-forward stops when you stop the command. To use another namespace, change `namespace` in `kustomization.yaml` and the name in `namespace.yaml`.

On the first install verifair can restart once or twice while Postgres creates its database. Keep `db.env`: Postgres keeps the password it was created with. With another password, the new verifair pod logs `password authentication failed` and restarts while the old one keeps running; put the old `db.env` back and apply again. The `.env` files are git-ignored. `kubectl delete -k .` deletes the namespace, and the database volume with it.

### Run it for a team

Copy the `deploy/kustomize` folder into your own repository, because `example` uses `../base` and `../postgres`. Then change `example`:

1. **Sign-in.** Turn on shared mode (see [access.md](access.md#set-up-shared-mode)):
   - in `configMapGenerator`: `VERIFAIR_BASE_URL=https://<your host>`, `VERIFAIR_OIDC_ISSUER`, `VERIFAIR_OIDC_CLIENT_ID`, `VERIFAIR_OIDC_ALLOWED_DOMAINS` and `VERIFAIR_BOOTSTRAP_ADMIN_ID`
   - in `secrets.env`: `VERIFAIR_OIDC_CLIENT_SECRET` and `VERIFAIR_SESSION_SECRET`, and remove `VERIFAIR_API_TOKEN`
2. **HTTPS.** Only after sign-in works: uncomment `ingress.yaml` in `resources`, and set your host, the TLS secret and `ingressClassName` in it. Without sign-in, anyone who reaches the host can comment and give verdicts. Set `FORWARDED_ALLOW_IPS` in `configMapGenerator` to the addresses of your ingress controller pods, so other pods cannot fake `X-Forwarded-*` headers.
3. **Your own Postgres.** Remove `../postgres` from `components` and the `verifair-db` secret generator. Put `VERIFAIR_DATABASE_URL` in `secrets.env`, with `sslmode=verify-full` when it is on another host. Encode special characters in the password (`@` is `%40`). The bundled Postgres has no backups and no upgrades; it is for trying verifair out.
4. **The image.** Pin it in your overlay, so a change in this repository does not upgrade you. A digest (`digest: sha256:…` instead of `newTag`) also guards against a moved tag:

   ```yaml
   images:
     - name: ghcr.io/romanzdk/verifair
       newTag: 1.2.3
   ```

Keep secrets out of git: create the `.env` files on deploy, or replace the secret generators with your own secret tool (Sealed Secrets, External Secrets) that makes Secrets with the same names. The rest of [Deploying](#deploying) applies too: replicas and backups.

## Deploying

- In local mode the web UI has no login. Anyone who can open it can comment and give verdicts. Run it on a private network or behind an auth proxy (SSO, basic auth). For a team, use shared mode, where verifair signs people in itself with your OpenID Connect provider (see [access.md](access.md#set-up-shared-mode)). The Docker example above only listens on `127.0.0.1`. Local mode also answers only on `localhost`, IP addresses, names without a dot and the host in `VERIFAIR_BASE_URL`, so a malicious website cannot reach it through DNS rebinding; set `VERIFAIR_BASE_URL` when you open it under another name.
- HTTPS is required for browser access outside loopback, including private-network hostnames. Local development may use HTTP on `localhost`, `127.0.0.1`, `[::1]` or `0.0.0.0`. In local mode, on these addresses (any `127.x.x.x` too), a browser post without `Sec-Fetch-Site` is accepted when its `Origin` matches the address, because some browsers leave the header out on `http://0.0.0.0`. Put a TLS reverse proxy in front of the loopback listener for shared deployments. Ordinary HTTP hostnames can load pages but browser writes return 403 because browsers omit the required `Sec-Fetch-Site` header on those origins.
- To sign people in, set `VERIFAIR_OIDC_ISSUER`, `VERIFAIR_OIDC_CLIENT_ID`, `VERIFAIR_OIDC_CLIENT_SECRET` and `VERIFAIR_SESSION_SECRET` (see [access.md](access.md#set-up-shared-mode)). This turns on shared mode. The email from sign-in becomes the user id; the display name comes from the member list. The name prompt goes away, and signing and approvals are tied to that identity. No auth proxy is needed.
- With several replicas, give every replica the same `VERIFAIR_SESSION_SECRET`. A replica with another secret does not accept the session cookie and sends people to sign in again.
- verifair must be able to reach the provider over HTTPS (for Google: `accounts.google.com` and `oauth2.googleapis.com`) for sign-in. Already signed-in people keep working while the provider is down.
- Set `VERIFAIR_BASE_URL` to the public HTTPS URL, for example `https://verifair.example.com`. It is used in notification links, in the push response and as the sign-in redirect URI (`VERIFAIR_BASE_URL/auth/callback`), and enables secure cookies. Shared mode refuses to start with an HTTP base URL other than `localhost` and `127.0.0.1`. Keep the browser on this origin and have the reverse proxy preserve `Sec-Fetch-Site`.
- Serve it at the root of a domain. Sub-paths (like `/verifair/`) are not supported.
- verifair needs a Postgres 16 or newer database. Its database role must own the schema and the tables, because every start runs the schema SQL, and Postgres 15 and newer does not let every role create tables in `public`. Where Postgres runs in production is not decided here.
- For a database on another host, add `sslmode=verify-full` (or at least `sslmode=require`) to `VERIFAIR_DATABASE_URL`, for example `postgresql://verifair:secret@db.example.com:5432/verifair?sslmode=verify-full`.
- Behind PgBouncer, use session mode. Transaction mode needs PgBouncer 1.21 or newer with `max_prepared_statements` set, because psycopg makes prepared statements and they cannot be turned off from the URL.
- Startup creates and updates the schema under an advisory lock, so several replicas can start at once. A start can wait up to 5 seconds behind writes in progress, then stops with a clear message. Restart it to try again.
- You can run several replicas, because all state is in Postgres. Keep `(replicas + extra pods during a rolling update) × VERIFAIR_DB_POOL_SIZE`, plus the connections you need for admin work and backups, below the server's `max_connections`.
- Back up with `pg_dump -Fc "$VERIFAIR_DATABASE_URL" > verifair.dump`. Restore into an empty database as verifair's role with `pg_restore --no-owner -d <empty database> verifair.dump`, so that role owns the restored tables.
- The image trusts `X-Forwarded-*` headers from any address. Set `FORWARDED_ALLOW_IPS` to your proxy's IP.
- Use `/healthz` for the liveness probe and `/readyz` for the readiness probe. `/healthz` does not touch the database, so a database outage does not restart pods. `/readyz` returns 200 `{"ok": true}` when the database answers `SELECT 1`, and 503 `{"ok": false}` with `Retry-After: 5` when it is down or no connection is free within 2 seconds, so the pod stops getting traffic until the database is back. Both are public, also in shared mode.
- Give both probes a timeout of at least 5 seconds (`timeoutSeconds: 5`). Under heavy load the answer can take a few seconds, and a shorter timeout restarts a pod that is only busy.
- The probes need no sign-in and no proxy rules. With `UVICORN_HOST=127.0.0.1`, an HTTP probe from outside the container cannot reach `/healthz`; use an exec probe with the same command as the image `HEALTHCHECK` then.

### Upgrading an existing HTTP deployment

Before upgrading a shared deployment, configure a trusted TLS certificate and an HTTPS reverse proxy for its hostname. Redirect the old HTTP URL to HTTPS, set `VERIFAIR_BASE_URL` to that HTTPS origin, and restart verifair behind the proxy with its existing database. Update agent URLs and bookmarked report links to HTTPS. A healthy `/healthz` response or a readable page on HTTP does not confirm that browser writes work.

After restarting, open the HTTPS URL in a browser. In local mode, set your name if needed; in shared mode, sign in as a member first. Then submit a comment on a draft report to check that writes pass through the proxy. Existing non-loopback HTTP deployments need this change before the upgrade; private-network placement alone does not enable browser writes. The API continues to use bearer tokens, and `/healthz` can still be checked over the internal loopback HTTP listener.
