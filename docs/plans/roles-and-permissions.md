# Roles and permissions implementation plan

Status: Planned.

Add Admin and Member permissions to the existing FastAPI and SQLite app. Membership is admin-approved: a trusted SSO identity identifies the person, and an active membership grants access. Active members continue to see every project and report by default. Author and reviewer remain relationships to a change.

Preserve the accepted review detail layout and styles. Change the availability of existing actions where permissions require it; put account administration on separate pages. Keep the All changes, My changes and My reviews scopes.

## First release policy

Implement Admin and Member now. Defer Viewer, private projects, custom roles, built-in password or OAuth login, and integration accounts that can publish for multiple human owners. Agents initially use tokens owned by one active member. Admins can issue and revoke those tokens for users; members can manage their own tokens within the same limits.

| Action | Member | Admin | Agent token |
| --- | --- | --- | --- |
| Read projects, reports and discussions | All workspace reports | All workspace reports | Explicit project grants and read scope |
| Publish or revise a report | Own reports through an agent token | Same ownership rule | Token owner's reports and granted projects |
| Set requested reviewers | Own reports | Own reports | Owner's reports with write scope |
| Sign a version | Own reports | Own reports | Denied |
| Approve or request changes | Other people's signed reports | Same review rules | Denied |
| Start a discussion or reply | Allowed | Allowed | Replies on owner's reports with reply scope |
| Reopen a discussion | Allowed | Allowed | A permitted reply may reopen it |
| Resolve a discussion or change its blocking flag | Discussion creator | Creator, or an override with a reason | Denied |
| Manage personal agent tokens | Own tokens | Own tokens and administrative issuance or revocation | Denied |
| Add members, change roles, deactivate accounts | Denied | Allowed | Denied |
| Transfer report ownership or link a legacy identity | Denied | Allowed with a reason and audit record | Denied |
| Read the administrative audit log | Denied | Allowed | Denied |

Review assignments do not exclude other members from volunteering a review. Both the current report owner and the signer of the current version are barred from approving or requesting changes on that version, including when they are admins. Unsigned versions cannot receive verdicts, and open blocking threads still prevent approval.

Any permitted reply retains the current behavior of reopening its discussion. This is an explicit escalation rule. Closing or downgrading feedback requires the creator's authority or a recorded admin override. When a route accepts a reply ID, authorization uses the discussion root and its creator.

## Authentication and membership

Add `VERIFAIR_AUTH_MODE=managed|demo`, defaulting to `managed`. Local development explicitly selects `demo`. Managed startup requires the configured trusted identity header. Only demo mode requires the existing `VERIFAIR_API_TOKEN`; that shared credential and browser identity cookies work only in demo mode. Management pages and managed tokens are unavailable there.

Managed browser requests require a verified identity and an active membership. An absent identity returns 401; an unknown or inactive member returns 403. SSO alone never provisions membership, and cookies or a submitted role value cannot override the database role. `/healthz` and static assets can remain public; other browser routes, including API documentation and schemas, require membership. Reject `/me` cookie changes in managed mode. Check origin and CSRF protections for every unsafe browser HTTP method.

Bootstrap the first admin from `VERIFAIR_BOOTSTRAP_ADMIN_ID` only while initializing an empty membership store; fail startup if that initial identity is missing. Persist bootstrap completion so a restart cannot recreate or reactivate a removed admin. Later membership changes go through authenticated administration. Deactivation preserves history and revokes the account's tokens. Reactivation does not revive revoked tokens. Prevent demoting or deactivating the last active admin, including concurrent attempts.

The deployment must keep the backend reachable only through the trusted auth proxy, which authenticates users and overwrites the identity header. Document the explicit managed setting and restrict forwarded-header trust to that proxy. Use the existing provider-neutral proxy integration; configuring a particular SSO provider remains deployment work.

## Data and boundaries

Add these SQLite records with idempotent schema upgrades:

- `users`: exact verified user ID, display name, Admin or Member role, active flag, creation and update timestamps.
- `identity_links`: an immutable, admin-confirmed mapping from a legacy actor ID to an existing verified user, including who linked it, when and why. These links resolve historical relationships; they never authenticate a new request.
- `api_tokens`: public token ID, secret digest, label, owner user ID, issuer user ID, creation, expiry and revocation timestamps.
- `api_token_projects` and `api_token_scopes`: explicit grants for each token. Project grants may name a valid future project slug; an empty list grants no access.
- `audit_events`: actor type, human or token ID, action, target, timestamp, relevant before and after values, and an administrative reason where required.
- A persisted bootstrap marker. Do not delete users or tokens referenced by history.

Tokens use a random secret with at least 256 bits of entropy. Store its digest and reveal the secret only once; use a public token ID for lookup and constant-time digest comparison. Require a finite expiry and use 30 days as the initial default. Creation responses use `Cache-Control: no-store`. Never put secrets in URLs, application logs or audit records. Rotation creates a new token and revokes the old one.

Add `verifair/auth.py` for browser and token authentication and `verifair/permissions.py` for a typed principal and pure permission decisions. Add `verifair/admin.py` for management routes. Keep storage and transactional writes in `db.py`; keep HTTP concerns out of the policy and storage layers. Use the same policy functions for server enforcement and template capabilities.

For writes, reload active membership, token grants and resource ownership inside the SQLite write transaction. Perform authorization, mutation and its audit insert atomically. A failed check or audit insert rolls back the operation. Keep one write transaction per operation: automatic reopening belongs to the reply transaction and must not introduce a nested transaction or commit separately. Do not cache membership across requests.

This combines workspace roles with ownership checks and applies permission checks to every request, following the [OWASP authorization guidance](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html#validate-the-permissions-on-every-request).

## Agent API contract

Managed tokens grant only `reports:read`, `reports:write` and `comments:reply`; no grant implies another. They never acquire the owner's Admin role. An expired or revoked token, or a token whose owner is inactive, cannot authenticate.

| Existing endpoint | Managed requirement |
| --- | --- |
| `POST /api/reports` | Write scope, granted project, active token owner, and ownership of an existing report |
| `GET /api/reports/{id}` | Read scope for the report's stored project |
| `GET /api/reports/{id}/comments` | Same read requirement |
| `GET /api/reports/{id}/wait` | Same read requirement, checked again during polling and before returning an event |
| `POST /api/reports/{id}/comments` | Reply scope, owner's report, and a parent belonging to that report |
| `POST /api/reports/{id}/comments/{cid}/resolve` | Denied to agent tokens in managed mode |

Derive new report ownership and the human author display name from the token owner. Reject a contradictory `author_id`; a token cannot take over an unowned or differently owned report. Check the stored report and its project before an update, rather than trusting submitted metadata. Requested reviewers must be active members. Omitted reviewers retain their existing values, and an empty list clears them.

Add `PUT /api/reports/{id}/reviewers` with write scope, project and ownership checks, so changing review requests alone does not create a report version or invalidate a signature. Keep report body revisions on the existing versioned push endpoint.

In managed mode, agent comment writes are replies. Reject new root discussions and resolution requests through this API. Preserve the robot presentation and agent attribution of replies, while recording the exact token ID in the audit event. The API continues to return metadata and comments without exposing browser administration routes to bearer credentials.

## Administration and review controls

Add a Users and access page for adding verified IDs, approving membership, changing roles, deactivating accounts, issuing or revoking tokens, and reading audit history. Provide members a separate My agent tokens page. Tokens show their owner, projects, scopes, expiry and revocation state; existing secrets are never retrievable.

Provide separate admin forms for legacy identity linking, ownership transfer and discussion overrides. Overrides require a nonblank reason. Ownership transfer creates a new unsigned version from the current body and commit, assigns the active new owner, and preserves earlier signatures and verdicts. Admins cannot fabricate signatures, verdicts or another person's comments.

On the existing review detail page, conditionally render the current sign, verdict, resolve and blocking controls from policy results. Preserve layout, typography, evidence content, selection and commenting interactions. An admin who is not the discussion creator uses the separate override form. Keep permission failures understandable for direct requests and HTMX requests using the existing error handling.

Add management navigation on landing pages. Preserve role scopes through normal project and status navigation. Show the current verified identity in the existing identity control.

## Existing data and deployment

Keep existing reports, versions, signatures, verdicts, comments and reviewer requests. Never grant membership from display names, report authorship or a browser cookie. Existing IDs that already match a verified member work directly. An admin must explicitly link historical browser IDs when migrating those people to SSO; reject conflicting links, links to another verified user's ID, and links for `agent`. Adding a new verified member must also reject an ID already linked to someone else.

Use linked identities consistently for ownership, discussion creators, signer checks, latest verdict grouping and the My changes and My reviews scopes. Retain the original IDs and names in historical records. A newly linked identity must not let someone review their own signature under a different ID; status calculations must apply the same owner and signer exclusions as new verdict writes. In managed mode, ownership requires a resolved `author_id`; the free-form `author` display name cannot grant ownership. Admins adopt unowned reports through the ownership transfer operation. Unmapped historical discussions remain readable, and admins can resolve access problems through recorded linking or moderation.

Deployment order:

1. Back up SQLite with its backup API and verify restoration on a disposable database.
2. Configure the trusted proxy, managed mode and first admin identity before restarting.
3. Run the additive upgrade and bootstrap once. Add or approve members and reconcile legacy identities.
4. Issue limited tokens and switch publishers from the shared token. Verify human and agent flows against a private test instance before reopening shared access.
5. Confirm the shared token and cookie fallback are rejected in managed mode. Test a deactivated user and revoked token, including an active wait request.

Do not roll a shared instance back to code that accepts the old unrestricted credentials. Recover through the matching database backup and application version behind restricted access. The local demo can continue separately with explicit demo mode.

## Implementation sequence

Deliver these as ordered local commits. Enable managed access only after the complete route and API policy is enforced.

1. **Membership, schema and bootstrap.** Work in `db.py`, `config.py`, new `permissions.py` and storage/configuration tests. Add access tables, the principal type, mode validation, one-time admin bootstrap and legacy identity linking storage. Test upgrade idempotence, no automatic membership, link conflicts, token digest storage and last-admin invariants.
2. **Verified browser access.** Work in new `auth.py`, `app.py`, `web.py` and authentication tests. Replace the managed cookie fallback with identity plus membership checks. Protect all browser reads and writes, while keeping explicit demo fixtures for the existing workflow. Test missing identity, unknown or inactive members, forged cookies and role headers, static and health exceptions, `/me`, and CSRF for unsafe methods.
3. **Review permissions.** Work in `permissions.py`, `rules.py`, `db.py`, `web.py` and existing detail partials. Add owner and signer verdict checks, discussion-root authorization, transactional rechecks and atomic audit events. Update only the capabilities of existing detail controls. Test creator, other member, owner and admin cases, resolution by reply ID, automatic reopening, stale versions and linked identities.
4. **Scoped agent credentials.** Work in `auth.py`, `api.py`, `db.py` and API/credential tests. Implement issuance, expiry, revocation, project and action grants, publishing for the token owner, reviewer updates and restricted replies. Reauthorize wait polling. Test cross-project and cross-owner access, attempted owner spoofing, reply parent substitution, human thread resolution, old shared credentials, deactivation and revocation while waiting.
5. **Management pages.** Work in new `admin.py`, new management templates/assets and management tests. Build Users and access, My agent tokens and the separate admin forms. Check direct requests as well as navigation visibility. Test single-use secret display, non-admin denial, administrative reasons, audit pagination, last-admin races, and membership changes taking effect on the next request.
6. **Migration and release verification.** Work in tests, README, publishing examples and deployment configuration. Run the full existing and new suites, lint and JavaScript checks, and browser checks for Admin, Member, unknown and inactive identities. Exercise desktop, mobile and dark mode, identity copying, personal scopes and HTMX permission errors. Rehearse upgrade and recovery on a copied database, then document rollout.

## Completion checks

- An active Member can read all reports, sign their own report, and review another member's signed report. Admins follow the same review rules.
- Missing identity, unknown membership, inactive users and forged role or cookie values never gain managed access.
- A member or agent cannot close someone else's feedback through a root ID, a reply ID, the browser route or the API. A reasoned admin intervention is recorded with its real actor.
- Agents can perform their granted publication, read and reply operations and cannot sign, give verdicts, manage access or change ownership.
- Invalid, expired and revoked credentials are denied; deactivation revokes existing tokens, and outstanding waits stop returning protected data.
- No membership, token, ownership or moderation mutation can commit without its audit event, and concurrent operations cannot remove the last active admin.
- Historical attribution and content survive migration. Explicit identity links preserve self-review restrictions and personal scopes across the move to SSO.
- The accepted review detail design remains intact for permitted actions, and the existing demo workflow passes under explicit demo mode.
