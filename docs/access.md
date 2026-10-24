# Roles and access

verifair has two modes. `VERIFAIR_IDENTITY_HEADER` switches between them.

- **Local mode** (the header is not set): no sign-in. People set a name, kept in a cookie. Agents share one `VERIFAIR_API_TOKEN`. Anyone can resolve any thread. Use it on your own machine or in a small trusted group. The settings pages (`/access/tokens` and `/access/members`) only show a note.
- **Shared mode** (the header is set): an auth proxy signs people in and verifair checks that they are members. Each agent uses a token owned by one member. This is the mode for a team.

## Set up shared mode

1. Put verifair behind an auth proxy that signs users in, for example with SSO.
2. The proxy must set the identity header on every request. It must strip any value the client sent and write its own. Otherwise anyone can pose as anyone.
3. Make verifair reachable only through the proxy. Anyone who can reach it directly can send any header. See [Deploying](deployment.md#deploying) for how.
4. Set `VERIFAIR_IDENTITY_HEADER` to the header name, for example `X-Forwarded-Email`.
5. Set `VERIFAIR_BOOTSTRAP_ADMIN_ID` to your own id as the proxy sends it, for example `jane@example.com`. Case does not matter. On a database with no members, startup makes this id the first admin. Once members exist, it adds no one.
6. `VERIFAIR_API_TOKEN` is not needed. If it is set, shared mode refuses it and startup logs a warning.

```bash
export VERIFAIR_IDENTITY_HEADER=X-Forwarded-Email
export VERIFAIR_BOOTSTRAP_ADMIN_ID=jane@example.com
uv run uvicorn verifair.asgi:app
```

With compose, put the same settings in a `docker-compose.override.yml` next to `docker-compose.yml`. Compose reads it on its own. `!reset null` (Compose 2.24.4 or newer) removes the API token the main file sets:

```yaml
services:
  app:
    environment:
      VERIFAIR_IDENTITY_HEADER: X-Forwarded-Email
      VERIFAIR_BOOTSTRAP_ADMIN_ID: jane@example.com
      VERIFAIR_API_TOKEN: !reset null
```

The proxy rules above still apply. Compose only starts verifair on `127.0.0.1:8000`, so you still need to put an auth proxy in front of it.

Startup fails when the database has no members and `VERIFAIR_BOOTSTRAP_ADMIN_ID` is not set. It also fails when `VERIFAIR_BOOTSTRAP_ADMIN_ID` is set without `VERIFAIR_IDENTITY_HEADER`, so a missing header does not quietly start local mode. It also fails when the id is not a plain id (no spaces, `/` or `\`, and not `agent`). Startup logs `bootstrap admin created id=...` or `bootstrap skipped: members exist`.

Once a database has members, it stays in shared mode. Starting it without `VERIFAIR_IDENTITY_HEADER` fails with `This database has members. Set VERIFAIR_IDENTITY_HEADER to run in shared mode.` This stops a config mistake from opening the workspace to everyone.

A request without the header, or with the header sent more than once, gets 401. A signed-in person who was never added gets 403 with a note to ask an admin to add their id. A deactivated member gets 403 `Your access to this workspace was turned off. Ask an admin to reactivate {id}.` Each refusal is logged at INFO as `membership denied reason=... path=...`, where the reason is `missing-header`, `duplicate-header`, `not-member` or `inactive`. `/healthz` and `/static/` stay public. `/api/` uses tokens. Every other page, including `/docs` and `/openapi.json`, needs membership.

## What members and admins can do

| Action | Member | Admin | Agent token |
| --- | --- | --- | --- |
| Read all projects, reports and threads | Yes | Yes | Yes |
| Push a report or a new version | Through their own agent token | Same as a member | The owner's reports only |
| Comment, reply, reopen a thread | Yes | Yes | Comment and reply on the owner's reports |
| Sign a version | Own reports | Own reports | No |
| Approve or request changes | Reports someone else signed | Same as a member | No |
| Resolve a thread or change its blocking flag | Threads they started, and agent threads on their own reports | Any thread | Resolve agent threads on the owner's reports |
| Create and revoke agent tokens | Own tokens | Own tokens, and revoke anyone's | No |
| Add members, change roles, deactivate | No | Yes | No |

## Members and roles

Admins manage members on the **Members** page (`/access/members`, under **Settings** in the sidebar). Other members cannot open it. To add a member, enter their id as the proxy sends it, a display name, and a role: `admin` or `member`. Ids are matched without regard to case: verifair stores them in lower case and turns the header value into lower case too, so `Jane@Example.com` and `jane@example.com` are the same member. The members table changes roles and deactivates or reactivates people. Members are never deleted, so their history stays.

A deactivated member is refused on their next request, and all their tokens are revoked. Reactivating them does not bring old tokens back.

There must always be at least one active admin. verifair refuses to demote or deactivate the last one: `There must be at least one active admin.`

Admin and token actions are written to the app log as `access action=... actor=... target=...`. A member change also logs `tokens_revoked=N`, the number of tokens a deactivation revoked. The token secret is never logged.

## Agent tokens

Every member creates their own agent tokens on the **Agent tokens** page (`/access/tokens`, under **Settings** in the sidebar). `/access` takes you there. A token looks like `vf_<id>.<secret>`. It is shown once, right after you create it. Copy it then; verifair keeps only a hash. Send it as `Authorization: Bearer vf_...`.

- The **Agent tokens** page lists your tokens with when each was created and last used (**Last used** shows `Never` until an agent uses it, and is updated at most once a minute). Revoke a token there when you no longer need it. Admins also see every member's tokens and can revoke any of them.
- Deactivating a member revokes their tokens.
- A token acts for its owner, but never with admin power, even when the owner is an admin. It cannot push to or comment on another member's report, and it cannot resolve threads that a person started.
- The shared `VERIFAIR_API_TOKEN` is refused in shared mode with 401 `The shared token is not accepted in shared mode. Use a member agent token.`
- Each refused token is logged at INFO as `api token refused reason=... path=...`. The reason is `missing` (no `Authorization` header), `malformed` (not a `Bearer` value, or a `vf_` token of the wrong shape), `shared-token` (a bearer value that does not start with `vf_`), `unknown` (no such token, or a wrong secret), `revoked` or `inactive` (the owner is deactivated). The token itself is never logged.

## Report ownership

The token owner owns every report the token pushes. Report ids are chosen by the client. If an id is already used by another member's report, the push gets 403 `This report belongs to another member.` Pick a different id, or leave `id` out and verifair makes a new one. Ownership cannot be transferred.

A report with no owner (a report pushed in local mode and never signed) is frozen in shared mode. Nobody can sign it, push a new version to it, or comment on it with an agent token. People can still read it and comment in the browser. To go on, push it again with an agent token under a new id.

## Moderation

In shared mode, anyone can reply to a thread or reopen it with the **Reopen** button. Only these people can resolve a thread or change its blocking flag:

- the person who started the thread,
- an admin,
- the report owner, when the agent started the thread.

A reply to a resolved thread opens it again only when the person replying could resolve it. The same rule holds for agent tokens: the owner's agent reopens a thread by replying only when the agent started it. So an agent that answers "fixed" on a reviewer's resolved thread does not open it again, but the reviewer's own reply does. In local mode every reply opens the thread again.

The buttons are hidden for everyone else. If someone else tries anyway, a request without htmx gets 403 `Only the person who started this thread, the report owner for agent threads, or an admin can do that.`, and a request from the page shows the same text in the thread instead. A resolved thread shows who resolved it.

In local mode, anyone can resolve any thread.

## Switching from local to shared mode

A deployment that starts with `VERIFAIR_IDENTITY_HEADER` set runs in shared mode.

Before you switch:

1. Back up the database with `pg_dump -Fc "$VERIFAIR_DATABASE_URL" > verifair.dump`.
2. Set `VERIFAIR_BOOTSTRAP_ADMIN_ID`, or startup fails.
3. Let `/api/` through the proxy without sign-in. Agents use tokens, not the browser login. Keep the proxy stripping the identity header on those routes too. With oauth2-proxy: `--skip-auth-route=^/api/`. Leave `--pass-authorization-header` and `--basic-auth-password` off: with either one set, oauth2-proxy removes the client's `Authorization` header even on skipped routes, so agent tokens never reach verifair.
4. Point liveness and readiness probes at `/healthz`. verifair needs no sign-in there, but oauth2-proxy asks for one on every route it does not skip. Add `--skip-auth-route=GET=^/healthz$`, use an exec probe, or probe the proxy's own `/ping` instead.
5. Plan who to add first. After the switch, everyone except the bootstrap admin gets 403 until an admin adds them. The 403 page shows the exact id to add, so ask people to open verifair and send you that id. Ids are matched without regard to case.

After the switch:

- The bootstrap admin is already a member. Add the other members on `/access/members` and give each agent a member token. The old shared token stops working.
- The first start in shared mode turns the stored ids into lower case once: report owners (`author_id`), requested reviewers, signers, verdict and comment authors, and `resolved_by`. Local mode stores them as they were sent, and shared mode matches them to the identity header in lower case. Startup logs `lowercased ids rows=N`. Later starts change nothing. An old id that would turn into `agent` stays as it is.
- Existing reports keep their `author_id`. A member can push new versions only to reports whose `author_id` is their id.
- Reports with no owner are frozen. Push them again with an agent token under a new id.
- If no admin can sign in any more, make someone admin again in the database. Use the id in lower case:

  ```bash
  psql "$VERIFAIR_DATABASE_URL" -c "UPDATE users SET role='admin', active=1 WHERE id='jane@example.com'"
  ```

## Not built yet

- An audit log in the app.
- Token scopes and token expiry.
- Ownership transfer.
- Admin overrides, such as resolving with a recorded reason.
- Moving local-mode identities to member ids.
- Moving data from an old SQLite deployment.
