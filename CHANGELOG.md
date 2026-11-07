# Changelog

All notable changes to verifair are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html): a minor version adds features, a patch version only fixes bugs.

Every pull request adds a line under **Unreleased**. A release moves those lines into a new version section.

## [Unreleased]

### Fixed

- Accessibility fixes from an audit. Input borders and a few grey texts are darker so they are easier to see. The section numbers on a review page are easier to read. Links inside a paragraph are underlined. The sidebar and token page headings follow the page order. The review page has no second page banner, and the review summary and your review bar are named regions. The theme buttons are a named group. Code blocks that scroll can be reached with the keyboard.
- Keyboard and screen reader users no longer lose their place after posting a comment or reply, resolving or reopening a thread, cancelling a comment form, signing or unsigning, or withdrawing a verdict. Focus moves to the new message or the new status.

### Changed

- On the delete page, "Delete permanently" stays disabled until the typed id matches. The server still checks the id.
- On phones the members table is compact: name and email on top, then role, status and actions.
- The API explorer pages (`/docs` and `/redoc`) follow your theme: dark when you chose dark, or when your system is dark and you chose system.
- Docs, README feature list, screenshots and the demo GIF match the current app.

## [0.8.0] - 2026-11-07

### Security

- Images on other sites in a report are shown as a link ("Image from <host>: <alt text>") instead of being loaded, so opening a report no longer tells another site who opened it, and an agent cannot make reviewers' browsers call arbitrary addresses. The content security policy now allows images only from verifair itself and `data:`.

### Changed

- Text written by an agent is labelled in notifications. The review email's excerpt is headed "Judge this (written by the agent, not checked by verifair)", and Slack comment messages from the agent say "(agent)" after the name.

## [0.7.0] - 2026-11-07

### Security

- Comment anchoring no longer slows down sharply on huge, repetitive sections, so an agent-written report cannot stall the pages that show it. When the exact quoted context is gone after an edit, only the first 200 matches of the quote are compared, so in very repetitive text a moved comment can land on an earlier match.
- Local mode refuses web pages on dotted hostnames other than the one in `VERIFAIR_BASE_URL` (400 `Unknown host`), which blocks DNS-rebinding attacks from websites you visit. The API and health checks are not affected.
- The comment label matcher no longer backtracks on long runs of spaces.
- The API explorer pages (`/docs`, `/redoc`) load pinned Swagger UI 5.34.1 and ReDoc 2.5.4 with integrity hashes, and no longer load Google Fonts.
- The content security policy adds `base-uri 'none'` and `form-action 'self'`.

### Changed

- The API explorer and `openapi.json` show the real version and only the API and health endpoints.
- The **Docs** and **GitHub** links open in a new tab.
- Each sent email is logged (`email sent report=… recipients=N`, without addresses).

### Fixed

- The "Connect your coding agent" banner returns when every token of a member is revoked.

## [0.6.1] - 2026-11-06

### Fixed

- A heading inside a plan or report overlapped itself when it wrapped on a phone.
- On phones the verdict bar took about a quarter of the screen. The summary box is now one line that grows when you tap it, and is hidden when you cannot give a verdict.
- On phones the new agent token was cut off in its box; it now wraps so you can see all of it.
- Small buttons, comment label chips and "comment" links are at least 32px tall on phones.
- The project name in the phone breadcrumb is no longer squeezed to a few letters.
- The disabled **Approve** button is easier to read.
- After a refused sign-in, the button says **Sign in with another account** (or **Try again** after an error) instead of a plain "Sign in" next to "Trying again will not help".

## [0.6.0] - 2026-11-06

### Added

- Each version tag creates a GitHub release, with this changelog's section as its notes.

### Changed

- The theme switch is now a small icon control (monitor, sun, moon) next to **Sign out**, with a tooltip and a screen-reader label on each option.

## [0.5.0] - 2026-11-06

### Added

- A **System / Light / Dark** theme switch at the bottom of the sidebar. The choice is saved per browser; System follows the operating system.

## [0.4.0] - 2026-11-06

### Added

- [Conventional Comments](https://conventionalcomments.org) labels: buttons above the comment box add `praise:`, `nitpick:`, `suggestion:`, `issue:`, `question:`, `thought:`, `todo:`, `chore:` or `note:` to the comment. `issue` and `todo` make it blocking, the others non-blocking. Threads show the label as a badge; it is stored as plain text, so agents read it as is.

## [0.3.0] - 2026-11-06

### Added

- **Unapprove** and **Withdraw request**: after you approve or request changes, that button turns into its undo and removes your verdict on the version.
- **Unsign**: the signer or an admin can unsign the current version while nobody has given a verdict on it.
- Withdrawing a verdict posts to Slack, like giving one.
- **Docs** and **GitHub** links at the bottom of the sidebar.

### Changed

- Clearer dividers, spacing and numbered headings between the sections of a review.
- In a plan-first review, the plan's approval and signature can no longer be undone once the implementation is attached.

### Fixed

- After approving, the **Approve** button stayed active and could record the same verdict again.
- The API explorer at `/docs` and `/redoc` was blank, because the content security policy blocked its scripts.

## [0.2.0] - 2026-11-06

### Changed

- The skill is installed with [`npx skills`](https://github.com/vercel-labs/skills), pinned to the server's version, so it works for Claude Code, Codex, Cursor and other agents. `GET /api/skill` stays as a fallback without Node.js.
- Clearer navigation: **Assigned to me** (you are the reviewer), **Created by me** (you are the author) and **All reviews**. The app now says "review" for the document everywhere.

## [0.1.3] - 2026-11-06

### Added

- **Archive** and **Restore** a review: it leaves the Inbox, lists and sidebar but stays readable at its link. The owner, requested reviewers and admins can do it, also through the API (`POST /api/reports/{id}/archive` and `/restore`). An **Archived** tab finds archived reviews again.
- **Archive project** (admins): archives every review in a project.
- **Delete permanently** (admins): removes a review and everything on it, after typing its id.

## [0.1.2] - 2026-11-06

### Added

- Onboarding: the first-run page and the Agent tokens page show how to install the skill (`GET /api/skill`), and members without a token see a "Connect your coding agent" banner.
- A welcome email when an admin adds a member.
- The new token is filled into the `export` lines right after you create it.
- Optional open sign-up (`VERIFAIR_OPEN_SIGNUP=true`): anyone from the allowed domains becomes a member on first sign-in. Off by default.

## [0.1.1] - 2026-11-06

### Fixed

- Resolve, Reopen and Mark blocking now work without JavaScript.
- A blank comment shows "The comment is empty" instead of a vague error.
- Pages are no longer cached by browsers or proxies (`Cache-Control: no-store`), and HTTPS deployments send `Strict-Transport-Security`.

## [0.1.0] - 2026-11-05

### Added

- First release. Coding agents publish change reports through a JSON API; people sign them, comment on any text, and approve or request changes, with versions, blocking threads and an Inbox grouped by whose turn it is.
- Plan-first reviews, optional execution traces, local and shared mode with Google or OpenID Connect sign-in, members, admins and per-member agent tokens, optional Slack messages and review emails.
- Docker image at `ghcr.io/romanzdk/verifair` and the `verifair-review` agent skill.

[Unreleased]: https://github.com/romanzdk/verifair/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/romanzdk/verifair/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/romanzdk/verifair/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/romanzdk/verifair/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/romanzdk/verifair/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/romanzdk/verifair/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/romanzdk/verifair/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/romanzdk/verifair/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/romanzdk/verifair/compare/v0.1.3...v0.2.0
[0.1.3]: https://github.com/romanzdk/verifair/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/romanzdk/verifair/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/romanzdk/verifair/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/romanzdk/verifair/releases/tag/v0.1.0
