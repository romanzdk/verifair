# Roles and access

verifair has two modes. `VERIFAIR_OIDC_ISSUER` switches between them.

- **Local mode** (no issuer set): no sign-in. People set a name, kept in a cookie. Agents share one `VERIFAIR_API_TOKEN`. Anyone can resolve any thread. Use it on your own machine or in a small trusted group. The settings pages (`/access/tokens` and `/access/members`) only show a note.
- **Shared mode** (issuer set): people sign in with your company login (Google, Microsoft Entra, Okta, Keycloak or any other OpenID Connect provider), and verifair checks that they are members. Each agent uses a token owned by one member. This is the mode for a team.

verifair does the sign-in itself. You do not need an auth proxy in front of it.

## Set up shared mode

You need:

- verifair on an HTTPS URL, for example `https://verifair.example.com`. Set `VERIFAIR_BASE_URL` to it. Sign-in sends people back to `VERIFAIR_BASE_URL/auth/callback`, so it must be the exact URL people open. Plain HTTP only works on `localhost` and `127.0.0.1`.
- An OAuth client at your sign-in provider (see [Google](#sign-in-with-google) and [other providers](#other-providers) below).
- A session secret: a random string of at least 32 characters. Create one with `openssl rand -base64 48`.

Then set:

| Variable | Value |
| --- | --- |
| `VERIFAIR_OIDC_ISSUER` | Your provider's issuer URL, for example `https://accounts.google.com` |
| `VERIFAIR_OIDC_CLIENT_ID` | The client ID from your provider |
| `VERIFAIR_OIDC_CLIENT_SECRET` | The client secret from your provider |
| `VERIFAIR_SESSION_SECRET` | The session secret. Use the same value on every replica |
| `VERIFAIR_OIDC_ALLOWED_DOMAINS` | Optional. Comma-separated email domains that may sign in, for example `example.com` |
| `VERIFAIR_BOOTSTRAP_ADMIN_ID` | Your own email, for example `jane@example.com`. On a database with no members, startup makes it the first admin. Once members exist, it adds no one. Case does not matter |
| `VERIFAIR_BASE_URL` | The HTTPS URL of verifair |

`VERIFAIR_API_TOKEN` is not needed. If it is set, shared mode refuses it and startup logs a warning.

```bash
export VERIFAIR_BASE_URL=https://verifair.example.com
export VERIFAIR_OIDC_ISSUER=https://accounts.google.com
export VERIFAIR_OIDC_CLIENT_ID=1234-abcd.apps.googleusercontent.com
export VERIFAIR_OIDC_CLIENT_SECRET=...
export VERIFAIR_SESSION_SECRET="$(openssl rand -base64 48)"
export VERIFAIR_OIDC_ALLOWED_DOMAINS=example.com
export VERIFAIR_BOOTSTRAP_ADMIN_ID=jane@example.com
uv run uvicorn verifair.asgi:app
```

With compose, put the same settings in a `docker-compose.override.yml` next to `docker-compose.yml`. Compose reads it on its own. `!reset null` (Compose 2.24.4 or newer) removes the API token the main file sets. Keep the secrets in a `.env` file next to it, not in the YAML:

```yaml
services:
  app:
    environment:
      VERIFAIR_BASE_URL: https://verifair.example.com
      VERIFAIR_OIDC_ISSUER: https://accounts.google.com
      VERIFAIR_OIDC_CLIENT_ID: ${VERIFAIR_OIDC_CLIENT_ID}
      VERIFAIR_OIDC_CLIENT_SECRET: ${VERIFAIR_OIDC_CLIENT_SECRET}
      VERIFAIR_SESSION_SECRET: ${VERIFAIR_SESSION_SECRET}
      VERIFAIR_OIDC_ALLOWED_DOMAINS: example.com
      VERIFAIR_BOOTSTRAP_ADMIN_ID: jane@example.com
      VERIFAIR_API_TOKEN: !reset null
```

Compose only starts verifair on `127.0.0.1:8000`. Put an HTTPS reverse proxy (or your cluster's ingress) in front of it for the public URL.

### Sign in with Google

1. Open the [Google Cloud console](https://console.cloud.google.com/) and pick or create a project for verifair.
2. Go to **APIs & Services > OAuth consent screen** (in newer consoles: **Google Auth Platform**). Fill in the app name and a support email. Under **Audience**, pick **Internal** if you use Google Workspace: then only people in your organisation can sign in. Pick **External** only if you need accounts from outside it.
3. Go to **APIs & Services > Credentials > Create credentials > OAuth client ID**. Pick the application type **Web application**.
4. Under **Authorised redirect URIs**, add `https://<your host>/auth/callback`, for example `https://verifair.example.com/auth/callback`. It must match `VERIFAIR_BASE_URL` exactly. You do not need JavaScript origins.
5. Create the client. Copy the **Client ID** into `VERIFAIR_OIDC_CLIENT_ID` and the **Client secret** into `VERIFAIR_OIDC_CLIENT_SECRET`. Set `VERIFAIR_OIDC_ISSUER=https://accounts.google.com`.
6. Set `VERIFAIR_OIDC_ALLOWED_DOMAINS` to your Workspace domain, for example `example.com`. verifair then accepts only emails in that domain, and only accounts that Google marks as belonging to that Workspace (the `hd` claim). Personal Gmail accounts are refused.

### Other providers

Any provider that supports OpenID Connect with the authorization code flow works. Create a client of type "web" or "confidential", add the redirect URI `https://<your host>/auth/callback`, and copy the client ID and secret. The issuer URL is:

| Provider | `VERIFAIR_OIDC_ISSUER` |
| --- | --- |
| Google | `https://accounts.google.com` |
| Microsoft Entra ID | `https://login.microsoftonline.com/<tenant id>/v2.0` |
| Okta | `https://<your org>.okta.com` or `https://<your org>.okta.com/oauth2/default` |
| Keycloak | `https://<keycloak host>/realms/<realm>` |

verifair reads `<issuer>/.well-known/openid-configuration` on the first sign-in, so the issuer must be the exact value the provider puts in its tokens. It asks for the scopes `openid email profile`.

verifair only accepts an email that the provider marks as verified (the `email_verified` claim must be `true`). Google, Okta and Keycloak send it. Microsoft Entra ID does not send `email_verified`, so sign-in through Entra fails with `sign-in failed reason=email` today.

### How sign-in works

- Opening any page without a session sends you to `/login`, which sends you to the provider. After sign-in you come back to the page you opened.
- Your member id is your email in lower case.
- verifair keeps you signed in for 7 days with a cookie signed by `VERIFAIR_SESSION_SECRET`. After that you sign in again. Changing the secret signs everyone out. Every replica must use the same secret, or people are signed out when a request reaches another replica.
- **Sign out** is at the bottom of the sidebar. It ends the verifair session only, not your session at the provider.
- verifair checks membership on every request. A deactivated member is refused on their next request, even with a valid session cookie.
- verifair checks the ID token it gets straight from the provider over HTTPS: issuer, audience, expiry, nonce, verified email and allowed domains. The sign-in uses `state` and PKCE, so a sign-in started on one browser cannot be finished on another.

### Startup checks

Startup fails with a clear message when:

- `VERIFAIR_OIDC_ISSUER` is set without `VERIFAIR_OIDC_CLIENT_ID`, `VERIFAIR_OIDC_CLIENT_SECRET` or `VERIFAIR_SESSION_SECRET`, or the session secret is shorter than 32 characters.
- The issuer or `VERIFAIR_BASE_URL` is not HTTPS (plain HTTP is only allowed for `localhost` and `127.0.0.1`).
- `VERIFAIR_IDENTITY_HEADER` is set. The trusted-header mode was removed. See [Moving from the identity header](#moving-from-the-identity-header).
- The database has no members and `VERIFAIR_BOOTSTRAP_ADMIN_ID` is not set.
- `VERIFAIR_BOOTSTRAP_ADMIN_ID` is set without `VERIFAIR_OIDC_ISSUER`, so a missing issuer does not quietly start local mode.
- The bootstrap id is not a plain id (no spaces, `/` or `\`, and not `agent`).

Startup logs `bootstrap admin created id=...` or `bootstrap skipped: members exist`. Secret values are never printed.

Once a database has members, it stays in shared mode. Starting it without `VERIFAIR_OIDC_ISSUER` fails with `This database has members. Set VERIFAIR_OIDC_ISSUER to run in shared mode.` This stops a config mistake from opening the workspace to everyone.

### Refusals

- A page opened without a valid session goes to sign-in. An htmx request or a form post without one gets 401 `Sign in to use verifair.`
- A signed-in person who was never added gets 403 with a note to ask an admin to add their email, and a **Sign out** button. A deactivated member gets 403 `Your access to this workspace was turned off. Ask an admin to reactivate {id}.`
- Each refusal is logged at INFO as `membership denied reason=... path=...`. The reason is `no-session`, `bad-session` (a changed, expired or foreign cookie), `not-member` or `inactive`.
- A failed sign-in shows **Sign-in failed** with a link to try again, and logs `sign-in failed reason=...`. The reason is `no-flow` (the sign-in took longer than 10 minutes or the cookie is missing), `state`, `provider-error`, `no-code`, `discovery`, `token-endpoint`, `id-token`, `iss`, `aud`, `exp`, `iat`, `nonce`, `email` (missing or not verified) or `domain`.
- `/healthz`, `/readyz`, `/static/`, `/login`, `/auth/callback`, `/logout` and `/signed-out` stay public. `/api/` uses tokens. Every other page, including `/docs` and `/openapi.json`, needs membership.

## What members and admins can do

| Action | Member | Admin | Agent token |
| --- | --- | --- | --- |
| Read all projects, reports and threads | Yes | Yes | Yes |
| Push a report or a new version | Through their own agent token | Same as a member | The owner's reports only |
| Comment, reply, reopen a thread | Yes | Yes | Comment and reply on the owner's reports |
| Request a review, or remove a request | Yes, on any report, from any active member except the report owner. A reviewer with a current verdict on either review target cannot be removed | Same as a member | Add requests on the owner's reports when pushing |
| Sign a version | Own reports | Own reports | No |
| Approve or request changes | Reports someone else signed | Same as a member | No |
| Resolve a thread or change its blocking flag | Threads they started, and agent threads on their own reports | Any thread | Resolve agent threads on the owner's reports |
| Create and revoke agent tokens | Own tokens | Own tokens, and revoke anyone's | No |
| Add members, change roles, deactivate | No | Yes | No |

## Members and roles

Admins manage members on the **Members** page (`/access/members`, under **Settings** in the sidebar). Other members cannot open it. To add a member, enter their email, a display name, and a role: `admin` or `member`. Ids are matched without regard to case: verifair stores them in lower case and turns the email from sign-in into lower case too, so `Jane@Example.com` and `jane@example.com` are the same member. The members table changes roles and deactivates or reactivates people. Members are never deleted, so their history stays.

A deactivated member is refused on their next request, and all their tokens are revoked. Reactivating them does not bring old tokens back.

There must always be at least one active admin. verifair refuses to demote or deactivate the last one: `There must be at least one active admin.`

Admin and token actions, and added or removed review requests (`action=add_reviewer` and `action=remove_reviewer`, with `report=<id>`), are written to the app log as `access action=... actor=... target=...`. A member change also logs `tokens_revoked=N`, the number of tokens a deactivation revoked. The token secret is never logged.

## Agent tokens

Every member creates their own agent tokens on the **Agent tokens** page (`/access/tokens`, under **Settings** in the sidebar). `/access` takes you there. A token looks like `vf_<id>.<secret>`. It is shown once, right after you create it. Copy it then; verifair keeps only a hash. Send it as `Authorization: Bearer vf_...`.

- The **Agent tokens** page lists your tokens with when each was created and last used (**Last used** shows `Never` until an agent uses it, and is updated at most once a minute). Revoke a token there when you no longer need it. Admins also see every member's tokens and can revoke any of them.
- Deactivating a member revokes their tokens.
- A token acts for its owner, but never with admin power, even when the owner is an admin. It cannot push to or comment on another member's report, and it cannot resolve threads that a person started.
- The shared `VERIFAIR_API_TOKEN` is refused in shared mode with 401 `The shared token is not accepted in shared mode. Use a member agent token.`
- Each refused token is logged at INFO as `api token refused reason=... path=...`. The reason is `missing` (no `Authorization` header), `malformed` (not a `Bearer` value, or a `vf_` token of the wrong shape), `shared-token` (a bearer value that does not start with `vf_`), `unknown` (no such token, or a wrong secret), `revoked` or `inactive` (the owner is deactivated). The token itself is never logged.

## Report ownership

The token owner owns every report the token pushes. Report ids are chosen by the client. If an id is already used by another member's report, the push gets 403 `This report belongs to another member.` Pick a different id, or leave `id` out and verifair makes a new one. Ownership cannot be transferred.

A report with no owner (a report pushed in local mode and never signed) is frozen in shared mode. Nobody can sign it, push a new version to it, or comment on it with an agent token. People can still read it, comment and request reviews in the browser. To go on, push it again with an agent token under a new id.

## Moderation

For plan-first reviews these permissions apply separately to the plan and implementation. Shared-mode owners sign each target, and another person gives its verdict. Agent tokens can submit and discuss their owner's documents but cannot sign or approve either one. Implementation attachment, signing and verdicts wait for effective approval of the current plan. Admins have no bypass. In local mode the existing cookie identities and signing rules still apply; they do not provide strong authentication.

In shared mode, anyone can reply to a thread or reopen it with the **Reopen** button. Only these people can resolve a thread or change its blocking flag:

- the person who started the thread,
- an admin,
- the report owner, when the agent started the thread.

A reply to a resolved thread opens it again only when the person replying could resolve it. The same rule holds for agent tokens: the owner's agent reopens a thread by replying only when the agent started it. So an agent that answers "fixed" on a reviewer's resolved thread does not open it again, but the reviewer's own reply does. In local mode every reply opens the thread again.

The buttons are hidden for everyone else. If someone else tries anyway, a request without htmx gets 403 `Only the person who started this thread, the report owner for agent threads, or an admin can do that.`, and a request from the page shows the same text in the thread instead. A resolved thread shows who resolved it.

In local mode, anyone can resolve any thread.

## Switching from local to shared mode

A deployment that starts with `VERIFAIR_OIDC_ISSUER` set runs in shared mode.

Before you switch:

1. Back up the database with `pg_dump -Fc "$VERIFAIR_DATABASE_URL" > verifair.dump`.
2. Set up sign-in as in [Set up shared mode](#set-up-shared-mode), including `VERIFAIR_BOOTSTRAP_ADMIN_ID`, or startup fails.
3. Point the liveness probe at `/healthz` and the readiness probe at `/readyz`. They need no sign-in.
4. Plan who to add first. After the switch, everyone except the bootstrap admin gets 403 until an admin adds them. The 403 page shows the exact email to add, so ask people to open verifair and send it to you.

After the switch:

- The bootstrap admin is already a member. Add the other members on `/access/members` and give each agent a member token. The old shared token stops working.
- The first start in shared mode turns the stored ids into lower case once: report owners (`author_id`), requested reviewers, signers, verdict and comment authors, and `resolved_by`. Local mode stores them as they were sent, and shared mode matches them to the email from sign-in in lower case. Startup logs `lowercased ids rows=N`. Later starts change nothing. An old id that would turn into `agent` stays as it is.
- Existing reports keep their `author_id`. A member can push new versions only to reports whose `author_id` is their id.
- Reports with no owner are frozen. Push them again with an agent token under a new id.
- If no admin can sign in any more, make someone admin again in the database. Use the email in lower case:

  ```bash
  psql "$VERIFAIR_DATABASE_URL" -c "UPDATE users SET role='admin', active=1 WHERE id='jane@example.com'"
  ```

## Moving from the identity header

Older versions trusted a header from an auth proxy (`VERIFAIR_IDENTITY_HEADER`, often with oauth2-proxy). That mode was removed, and startup now fails while the variable is set.

1. Set up sign-in as in [Set up shared mode](#set-up-shared-mode). You can reuse the OAuth client the proxy used: add the redirect URI `https://<your host>/auth/callback` to it.
2. Remove `VERIFAIR_IDENTITY_HEADER` and take the auth proxy out. Keep your HTTPS reverse proxy or ingress.
3. Member ids stay the same when the proxy sent the email (for example `X-Forwarded-Email`). If it sent something else, such as a user name, add each person again under their email; the old member cannot be renamed.
4. Probes and `/api/` no longer need proxy skip rules.

## Not built yet

- An audit log in the app.
- Token scopes and token expiry.
- Ownership transfer.
- Admin overrides, such as resolving with a recorded reason.
- Moving local-mode identities to member ids.
- Moving data from an old SQLite deployment.
