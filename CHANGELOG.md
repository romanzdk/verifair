# Changelog

All notable changes to verifair are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html): a minor version adds features, a patch version only fixes bugs.

Every pull request adds a line under **Unreleased**. A release moves those lines into a new version section.

## [Unreleased]

### Added

- Each version tag creates a GitHub release, with this changelog's section as its notes.

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

[Unreleased]: https://github.com/romanzdk/verifair/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/romanzdk/verifair/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/romanzdk/verifair/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/romanzdk/verifair/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/romanzdk/verifair/compare/v0.1.3...v0.2.0
[0.1.3]: https://github.com/romanzdk/verifair/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/romanzdk/verifair/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/romanzdk/verifair/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/romanzdk/verifair/releases/tag/v0.1.0
