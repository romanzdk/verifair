---
name: verifair-review
description: Use when a finished change needs a human review in verifair, when asked to publish, push or update a verifair report, or to wait for and answer verifair review comments or verdicts. Also when VERIFAIR_URL is set and an implementation is being handed back.
---

# verifair review

A verifair report is what a human reads and approves instead of the diff. You write it, a human signs it, other humans review it. Your job: a report a reviewer can judge without opening the code, then answer their feedback.

## Setup

`VERIFAIR_URL` (default `http://localhost:8000`) and `VERIFAIR_API_TOKEN` (your owner's agent token in shared mode, `vf_…`). Missing token → ask the user; never guess one. Everything you need is below; `$VERIFAIR_URL/docs` only opens for signed-in members in shared mode.

## The report

Body = markdown with these H2 headings, in this order, spelled exactly (the server only enforces some of them; reviewers expect all):

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

Keep the body in a temp file you own (the API never returns it; every version resends the whole body). Build the JSON with Python and pipe it to curl:

```bash
python3 - "$REPORT_MD" <<'EOF' | curl -sS --fail-with-body -X POST "$VERIFAIR_URL/api/reports" \
  -H "Authorization: Bearer $VERIFAIR_API_TOKEN" -H "Content-Type: application/json" -d @-
import json, sys
print(json.dumps({
  "id": "short-stable-slug",            # reuse it for every new version
  "project": "repo-name", "title": "What changes, under 200 chars",
  "risk": "low|medium|high", "body": open(sys.argv[1]).read(),
  "links": [{"label": "Merge request", "url": "https://…"}],   # [] if none
  "reviewers": ["bob@example.com"],      # ids the user gave you; only adds, omit if none
  "agent": "claude", "model": "<your model>", "commit": "<sha the report describes>",
}))
EOF
```

### Optional plan

Attach one design or implementation Markdown document as `plan` in the same JSON payload. No plan template or headings are required; the report `body` and its headings remain required. Pass `"$PLAN_MD"` as a second argument to Python, import `Path` from `pathlib`, and add `"plan": Path(sys.argv[2]).read_text(encoding="utf-8")` to the payload above. Keep Python JSON encoding and send file contents, not a path or URL. See the complete [Python-plus-curl example](../../docs/api.md#optional-plan). There is no multipart upload or web editor.

On updates, reuse `id` and resend `body` and the required fields. Omitting `plan` inherits the previous version's plan (or no plan on creation); `"plan": None` emits JSON `null` and removes it from the new version only. Nonblank text is stored exactly per version, without trimming or normalization; earlier snapshots survive replacement or removal. Blank text, wrong JSON types, NUL and lone surrogates are rejected with 422 and nothing saved. The existing 1 MiB (1,048,576-byte) limit applies to the entire encoded JSON request, including body and plan together. POST's 201 `{id, version, url}` response and GET's metadata shape are unchanged; GET returns neither document's contents.

The optional **Plan** tab is read-only context for the selected report version; **Report** has signing, comments and verdicts. There is no independent plan approval. Every push, including a plan change or removal with the same body, creates a new unsigned report version that needs a fresh human signature. Earlier signatures and verdicts remain historical.

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
  | 503 | Wait `Retry-After` seconds, then `GET` the report: if the last version already has this push's `commit` (or the version count went up), it is stored; push only if not. |
- Proofread first: every push is a new version, marks earlier comments stale and needs a new human signature.
- Then tell the user the URL and that **they must sign it** (again after every new version) before anyone can give a verdict. You never sign, approve or request changes.

## Wait and answer

```bash
curl -sS --fail-with-body --max-time 60 -G -H "Authorization: Bearer $VERIFAIR_API_TOKEN" \
  --data-urlencode "timeout=55" ${SINCE:+--data-urlencode "since=$SINCE"} "$VERIFAIR_URL/api/reports/$ID/wait"
```

1. Loop `/wait` until an event or the time the user allowed (default 30 min), then report back what is still open. A 401 ends the loop: tell the user. First call: leave `since` out (it then waits for anything new). After that pass `next_since` as `since`. Skip events whose `comment.author_type` is `agent` (your own comments).
2. On any event read everything: `GET /api/reports/$ID` (status, signature, verdicts) and `GET /api/reports/$ID/comments?open=true`. `/wait` does not report replies, signatures or same-second events.
3. Answer every open human thread once with a reply (`POST …/comments` with `parent_id`): the direct answer first, then what you changed and in which version. If you disagree, say why once and leave it to the human.
4. Feedback that needs a change: change the code or report, re-run the affected checks, push a new version with the same `id` and the new `commit`.
5. Leave human threads open; the reviewer resolves them (open blocking threads block approval, so say "resolve it if this answers it"). Resolve only threads you started.
6. `request_changes` cannot be replied to: address its summary like a blocking thread, then post one top-level comment with `"blocking": false` saying what changed in which version. `approved` → tell the user; you are done. A reviewer can change their verdict; the latest one per reviewer counts (`current: true`).

## Common mistakes

| Mistake | Instead |
|---|---|
| Own headings ("What changed", "Open items") | The exact sections above; questions go in `Judge this` |
| Verification as "tests pass" | One row per claim, with command, result and link |
| Pushing v2 seconds after v1 for typos | Proofread first; versions are for real changes |
| Resolving the reviewer's thread after replying | Reply, leave it open |
| Signing or approving "to unblock" | Only humans sign and give verdicts |
