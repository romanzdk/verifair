# Reviewing in the web UI

How people find work, sign versions and handle threads. For who may do what in shared mode, see [access.md](access.md).

## Personal views

The sidebar has four views under **Your work**:

- **Inbox** (`/`, the home page) shows what needs you, grouped by whose turn it is:
  - **Needs your review**: changes where your review was requested and the status is awaiting review: the author has signed this version and no reviewer has given a verdict on it yet. As soon as any reviewer gives a verdict, the change leaves this group for every requested reviewer. When a reviewer requests changes, the turn passes to the author (it shows in their **Returned to you**), and it comes back to you after the author pushes and signs a new version. High risk comes first, then the ones waiting longest. Your own changes, and changes you signed, never show here, even if you are on their reviewer list.
  - **Needs your signature**: your unsigned changes. In local mode it also shows unsigned changes with no owner, because anyone can sign those.
  - **Returned to you**: your changes where a reviewer requested changes.
  - **Waiting on others**: your signed changes that wait for a review. This group starts closed.

  When a new version is pushed, the change goes back to the author to sign, and then back to the reviewers. When nothing needs you, the Inbox says so. With no projects yet, it shows how to connect your first agent.
- **My reviews** (`/changes?scope=reviewer`) lists every change where your review was requested, including finished reviews.
- **My changes** (`/changes?scope=author`) lists every change you own, including unsigned drafts.
- **All changes** (`/changes`) lists everyone's changes. Everyone sees all projects and changes.

The lists and project pages (`/p/<project>`, under **Projects** in the sidebar) have status tabs with counts. A report is at `/r/<id>`.

Below 1024px wide the sidebar becomes a menu that opens from the top bar. On the review page it is always a menu, so the report gets the full width.

Your id is at the bottom of the sidebar, with a button to copy it. Give it to your agent. Set `author_id` to the owner's id and `reviewers` to the requested reviewers' ids when publishing. In shared mode the agent token sets the owner, so `author_id` can be left out. Display names do not decide ownership. Review requests help people find work. The review page lists requested reviewers under **Reviewers** with "review requested" until they comment or give a verdict. Anyone with access can still read and review a report, and the normal signing and approval rules apply.

## Request a review

In shared mode, any member can ask someone to review a change. Under **Reviewers** on the review page, pick a member and press **Request**. The list shows active members who do not own the report and were not asked yet, sorted by name; two members with the same name show their ids. A requested reviewer who has not given a verdict on the current version has a **Remove** button. Someone who gave a verdict on the current version cannot be removed, so a finished review stays in their **My reviews**. Removing a request does not remove their comments or verdicts. The person sees the change in their Inbox (**Needs your review**) once the author has signed it, and in **My reviews**. Agents add requests with `reviewers` when they push; a push never removes one. Requests are changed on the latest version only.

In local mode there is no picker. Agents set `reviewers` when they push, and **Remove** works as in shared mode.

### Emails

With `VERIFAIR_SMTP_URL` and `VERIFAIR_EMAIL_FROM` set (see [configuration.md](configuration.md)), shared mode also sends a plain-text email:

- when the author signs a version: to every requested reviewer except the signer,
- when someone adds a reviewer while the current version is signed: to that person. Adding yourself sends nothing.

A push makes a new unsigned version, so it sends no email; the reviewers get one when the author signs it. Emails go only to active members whose id is one plain address, such as `bob@example.com`. Each person gets at most one email per report version, so removing and adding them again sends nothing new. Local mode sends no email.

The subject is `Review requested: "<title>"`. The text says who signed which version and who asked for the review, then the project, the risk, the report's **Judge this** section as plain text (code blocks left out, links as their text, up to about 1,500 characters, then "Read the rest on the page.") and the link:

```text
Alice signed v3 of "Prorate plan changes by day". You were asked to review it by Bob.

Project: billing · Risk: high

Judge this:
Is a day the right unit for proration?

Open it to comment, approve or request changes:
https://verifair.example.com/r/prorate

— Sent by verifair. Replies to this mail are not read.
```

Someone added after the version was signed gets `Bob asked you to review "<title>" (v3, signed by Alice).` as the first line.

Local identities belong to a browser. With the auth proxy described in [deployment.md](deployment.md#deploying), the trusted identity header supplies the same ID across devices. In local mode, signing a report without an owner records the signer as the owner, and **My changes** also lists unowned reports whose `author` equals your identity. In shared mode only `author_id` counts, and a report without an owner is frozen (see [Report ownership](access.md#report-ownership)).

## Signing

- A version must be signed before anyone can approve or request changes on it. Until then the status is `unsigned`.
- The first signature on a version is kept. Signing again does nothing.
- Without `VERIFAIR_IDENTITY_HEADER`, anyone who has set a name can sign. The name is only a cookie, so this is a convenience, not a control.
- With `VERIFAIR_IDENTITY_HEADER`, only the owner can sign: `author_id` must equal the header value (for example the email). A report with no `author_id` cannot be signed: `This report has no owner. Push it again with an agent token under a new id.`
- The person who signed a version cannot approve it or request changes on it. Someone else has to.
- Only with the identity header behind an auth proxy is signing a real control. See [deployment.md](deployment.md#deploying).

## Blocking threads

- A new thread is blocking by default. A reviewer can tick "Non-blocking (suggestion)" when writing it, and switch a thread between blocking and non-blocking later. In shared mode only some people can do that; see [Moderation](access.md#moderation).
- Approve is refused while any blocking thread is open. Resolve the thread or mark it non-blocking first. In shared mode the message also says who can do that: the person who started the thread, the report owner for agent threads, or an admin.
- Unresolved threads carry across report versions and continue to block approval. `stale` records the version where a discussion began; it does not resolve the discussion, even when its section or quote is gone. Close obsolete concerns explicitly or mark them non-blocking.
- Request changes needs a summary, or an open blocking thread of your own.
- The sidebar counts open blocking threads and suggestions.
