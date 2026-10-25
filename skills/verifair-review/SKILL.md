---
name: verifair-review
description: Use when a plan or finished change needs a human review in verifair, when asked to publish, push or update a verifair report, or to wait for and answer verifair review comments or verdicts. Also when VERIFAIR_URL is set and an implementation is being handed back.
---

# verifair review

Submit either an implementation report (`artifact`, the default) or a plan first (`plan_first`). You write Markdown, a human signs that exact document revision, and a different human reviews it. Plan-first review later attaches the implementation to the same ID and URL. You never sign, approve or request changes.

## Setup

`VERIFAIR_URL` (default `http://localhost:8000`) and `VERIFAIR_API_TOKEN` (your owner's agent token in shared mode, `vf_…`). Missing token → use the host’s private secret-entry tool if available, otherwise ask the user to configure it outside chat; never guess or request a token in chat. Everything you need is below; `$VERIFAIR_URL/docs` only opens for signed-in members in shared mode.

## The report

Implementation body = Markdown with these H2 headings, in this order, spelled exactly (the server only enforces some of them; reviewers expect all):

| Section | What goes in it |
|---|---|
| `## Intent` | The problem in plain words, the ticket, what is out of scope. |
| `## Judge this` | 2–4 questions only a human can answer (product intent, trade-offs, risk to accept). Not "please review". |
| `## Design` | How it works in a few lines, and one rejected alternative with the reason. |
| `## Edge cases` | Table: case · how it is handled · which check proves it. |
| `## Verification` | Table: proves · command · result (✅ / ⚠️ / ❌, reason for anything not ✅) · link · fails on revert (yes/no). |
| `## Not verified / assumptions` | What you did not check or took on trust, one bullet each. At least one bullet. |
| `## Rollback` | How to undo it and what that leaves behind (data written meanwhile, migrations). |
| `## Security impact` | What it touches that matters, or `None` with the reason. |
| `## Agent reviews` | Table: finding · severity · fixed / dismissed · reason. |
| `## Links` | Ticket, deploy, notebook, CI run; the code (MR/commit) last. Nothing hosted → "none, local only" and the commit sha as text. |

Evidence rules:
- Only results you saw. Re-run what you can; anything copied from earlier notes or not run is ⚠️ with that said.
- Links a reviewer can open: CI jobs, MRs, dashboards, deployed URLs. No local paths (`/Users/…`, `.superpowers/…`), no secrets.
- Plain simple language. Markdown only; raw HTML is shown as text.

## Push

Keep the Markdown in files you own. Build JSON with Python and pipe it to curl. The default artifact workflow requires the whole body on every push:

```bash
python3 - "$REPORT_MD" <<'EOF' | curl -sS --fail-with-body -X POST "$VERIFAIR_URL/api/reports" \
  -H "Authorization: Bearer $VERIFAIR_API_TOKEN" -H "Content-Type: application/json" -d @-
import json, sys
from pathlib import Path
print(json.dumps({
  "id": "short-stable-slug",
  "project": "repo-name", "title": "What changes, under 200 chars",
  "risk": "low|medium|high", "body": Path(sys.argv[1]).read_bytes().decode("utf-8"),
  "links": [{"label": "Merge request", "url": "https://…"}],
  "reviewers": ["bob@example.com"],
  "agent": "claude", "model": "<your model>", "commit": "<sha the report describes>",
}))
EOF
```

### Artifact with optional read-only plan

For a finished implementation, omit `workflow` or choose `"artifact"`. The implementation body and required headings are required on every push. Optionally attach one arbitrary nonblank Markdown `plan`; no template, headings or particular planning skill is required. Read its UTF-8 file as bytes then decode so CRLF is preserved. Send contents, not a path or URL. See [API examples](../../docs/api.md#optional-plan).

Reuse `id` on updates. Omitted `plan` inherits; `"plan": None` emits JSON null and removes it only from the new snapshot. This workflow's Plan tab stays read-only; plan feedback operations return 422. Each push creates a new unsigned implementation snapshot, including identical-body or plan-only-context changes. Earlier signatures/verdicts remain history.

### Plan first, then implementation

For review before implementation, create `workflow: "plan_first"` with `plan` and required metadata, omitting body and expected_version. Explicit body null is also accepted on creation. Choose any nonblank plan Markdown; no implementation headings apply. The same Python/curl pattern works:

```python
import json, sys
from pathlib import Path

print(
    json.dumps(
        {
            "id": "short-stable-slug",
            "project": "repo-name",
            "title": "Proposed change",
            "risk": "low",
            "workflow": "plan_first",
            "plan": Path(sys.argv[1]).read_bytes().decode("utf-8"),
        }
    )
)
```

Save the returned `{id, version, url}`. If creation omitted `id`, use the **returned ID** on every later push; omitting it again creates another review. Workflow is fixed after creation. Tell the owner to sign the plan, then wait for another human's approval. No agent token can sign or give a verdict on either target.

Before updating, read `GET /api/reports/$ID`. Repeat `project`, `title`, `risk` and the same `id`; include `expected_version` equal to its `current_version`. Supply at least one content field. Omitted `plan`/`body` inherits current contents; explicit null for either is rejected on updates. No document can be removed. Blank/NUL/surrogate documents are rejected without saving metadata or content.

When `stage` is `awaiting_implementation`, attach the report body at that ID with the guard, omitting plan or repeating its exact approved contents. Supply a non-null `commit` only together with a supplied body. Required report headings apply to this body. The URL remains unchanged. A plan-only update inherits an existing body's commit; a snapshot without implementation has null commit. A changed plan plus body in one request is refused; revise and get approval of the plan before submitting body.

Every push creates a global snapshot, even with identical text. Plan revision is the snapshot that began the continuous run of identical plan text; it can skip numbers and stays unchanged across body pushes. Changing even whitespace needs a new signature and approval. Reverting text after an edit creates another revision. An unchanged approved plan retains approval across implementation pushes. Every new snapshot containing implementation needs a new implementation signature/verdict, even when the body was inherited by a plan-only edit.

An open blocking plan thread, including an old unresolved concern, pauses attachment and implementation signing/verdicts. Reopening or promoting a suggestion to blocking also pauses it. Resolving or making it non-blocking restores approval for an otherwise approved unchanged plan. Changed plans always need fresh approval. Preserve all prior history.

### Read exact content and identity

`GET /api/reports/$ID/versions/$N` returns `{version, created_at, commit, body, plan, plan_revision}` with exact immutable Markdown. Metadata GET omits Markdown and adds `workflow`, `stage`, `implementation_status`, and `plan_review` (`revision/status/signed_by/signed_at/open_blocking/verdicts`). Versions add `has_implementation` and `plan_revision`. Legacy signature/verdict fields describe implementation; before attachment they are null/empty. Plan history is separate under `plan_review`; verdict `current` flags use that target's revision.

Use top-level `status` for the overall gate. It can be `unsigned`, `awaiting_review`, `changes_requested`, `awaiting_implementation` or `approved`; implementation can still say approved while a reopened plan pauses the overall review. History keeps exact documents and revision-associated live feedback; later replies and current resolution/blocking state on older included threads are live, not a frozen replay. Report metadata remains latest-value.

Both workflows store exact Markdown without trimming/normalization. The 1,048,576-byte limit covers the whole JSON request. Raw HTML is text and unsafe links are refused. Local mode trusts cookie identities/shared token; it does not authenticate a person against a directory. Shared mode enforces membership/ownership. Human signing is a separate browser action on each target, never an agent action. Upgrades need all writers restarted together; mixed old writers with nullable-body plan-first snapshots are unsupported.

### Push results

- `reviewers` only adds review requests; a push cannot remove one. If the user wants a request removed, they use **Remove** under Reviewers on the review page (shared mode).
- Add `"ticket_url"` if there is a ticket. Local mode only: add `"author"` and `"author_id"` if the user told you their verifair id. Shared mode: leave them out; the token owner is the author.
- Responses:

  | Answer | Do |
  |---|---|
  | 422 `missing sections`, field errors | Fix the body or field, push again. |
  | 422 `inactive reviewers` | Drop or correct the listed ids; ask the user if unsure. |
  | 403 `belongs to another member` / `has no owner` | Your `id` is taken: pick a new slug and push as a new report. |
  | 403 `author_id must be the token owner` | Leave `author_id` out. |
  | 401 | Token wrong, revoked or owner deactivated: stop and tell the user. |
  | 409 `review changed` / `document changed` | Reread metadata, exact snapshot and selected-target feedback; reconsider the edit, then use the new guard. Never blindly retry. |
  | 409 `current plan is not approved` | Read plan status/blockers and ask the human to handle approval; no bypass. |
  | 409 `workflow cannot be changed` | Keep the stored workflow or create a separate review. |
  | 422 content/guard error | Follow the workflow rules above; do not replace omission with null. |
  | 503 | Wait `Retry-After` seconds, then `GET` the report: compare exact snapshot contents and commit to the intended push; a higher version count alone can be another writer. Push only after resolving whether your write was stored. |
- Proofread first: every push creates a snapshot. Implementation comments become stale across snapshots; plan comments become stale only when plan revision changes.
- Give the user the URL and exact document needing their signature. A new plan revision needs plan signing; each implementation snapshot needs implementation signing. You never sign, approve or request changes.

## Wait and answer

Choose `TARGET=plan` while reviewing/revising a plan and `TARGET=implementation` for implementation feedback. Defaults always select implementation, which has empty feedback and null revision before attachment. Explicit plan operations on artifact reviews return 422.

```bash
curl -sS --fail-with-body --max-time 60 -G -H "Authorization: Bearer $VERIFAIR_API_TOKEN" \
  --data-urlencode "target=$TARGET" --data-urlencode "timeout=55" \
  ${SINCE:+--data-urlencode "since=$SINCE"} "$VERIFAIR_URL/api/reports/$ID/wait"
```

1. Loop `/wait` within the time the user allowed (default 30 min). First call omits `since`; subsequent calls can use `next_since`. A 401 stops the loop. The endpoint signals current-target root comments/verdicts, not a lossless stream or durable cursor. Timestamp cursors can skip other events in the same second.
2. After **every event or timeout**, reread metadata and `GET /api/reports/$ID/comments?target=$TARGET&open=true`. Replies, signatures, pushes and moderation (including reopening) are non-events. Wait follows the current target revision; an unchanged plan still receives feedback after a body push. Effective approval needs the normal state read.
3. Feedback envelopes carry `current_version`, `target`, `revision` (null without a document), from the same consistent read as the threads/events. Compare them with metadata and exact snapshot readback; separate requests can see different states. Reread on mismatch before writing. Comment/verdict `version` is the record's target revision, not necessarily the global version.
4. Reply to each open human thread once with `{body, parent_id}`. Omitted target inherits the root's target; explicit conflicts get 422. Optional `revision` guards the **current** target revision even when replying to an older root; the saved reply retains the root's old revision/anchor. Answer first, then say what changed. Leave human threads open for the reviewer. Shared-mode agent replies do not reopen human-started threads. Resolve only roots you started on your owner's report.
5. A new plan root requires `target:"plan"` and current `revision`. A new implementation root defaults to implementation and may omit revision for compatibility. Supply the guard when available. Quotes require a section and must occur in locked current Markdown; repeated quotes need prefix/suffix context. General comments need no section. Send `blocking:false` for informational notes.
6. For requested edits, update the code/Markdown, rerun affected checks, then push at the same ID with the correct snapshot guard. Plan edits supply plan; body edits supply body and optional commit. Never attach against an older approved plan. `request_changes` summaries can be addressed in a nonblocking root on that target, using its current revision.
7. Report approval using the top-level overall status. A plan approval alone means wait for implementation or attach the reviewed implementation; it does not approve implementation. Report remaining blockers when your allotted wait ends.

## Common mistakes

| Mistake | Instead |
|---|---|
| Own headings ("What changed", "Open items") | The exact sections above; questions go in `Judge this` |
| Verification as "tests pass" | One row per claim, with command, result and link |
| Pushing v2 seconds after v1 for typos | Proofread first; versions are for real changes |
| Resolving the reviewer's thread after replying | Reply, leave it open |
| Signing or approving "to unblock" | Only humans sign and give verdicts |
