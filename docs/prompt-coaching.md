# Prompt coaching

When an agent writes the code, the prompts are the part a person wrote. They decide what gets built, how many rounds it takes, and what the agent never heard about. Code review gives feedback on the code. Prompt coaching gives feedback on the prompts, so people learn from each other how to work with agents. Some tools already let you read the prompts behind a change; here teammates comment on single prompts, on the same page where they review the change.

An author can add the prompts they typed in the agent session to their review. Teammates then comment on them like on any other part of the report: what was unclear, what context was missing, how they would have asked.

It is optional and off by default. Nobody's prompts are shared unless they ask for it, and the comments never block the change.

## Share your prompts

Ask your agent, while it prepares the report: "add my prompts to the review". The [agent skill](api.md#get-the-agent-skill) then:

1. Finds the session you typed in, and shows you its file name and first prompt to confirm it is yours.
2. Runs `export-prompts.py`, which prints only what you typed: no agent replies, tool output, system text or attachment paths. Nothing is uploaded yet.
3. Shows you every prompt, numbered, and asks which to leave out, for example `1-12` for prompts about other work, or one that has a secret or a client name in it.
4. Adds the rest, word for word, as a `## Prompts` section at the end of the report.

You can also run the script yourself, from the installed skill's folder:

```bash
python3 -I export-prompts.py ~/.claude/projects/<project>/<session>.jsonl
python3 -I export-prompts.py SESSION.jsonl --upto 14 --drop 1-3,9
```

`--drop` leaves prompts out and keeps the original numbers. `--upto` uses only the first N prompts, so whatever you type after the preview stays out.

!!! warning "Read before you share"
    Every reader of the report sees the prompts, and a pushed version stays in history. Removing a prompt later does not remove it from older versions. Nothing is removed automatically: you choose what to drop. If a secret slips through, rotate it.

## Give good feedback

Select the text of a prompt and comment with the **suggestion** label, so the thread does not block approval. The change itself is judged on the rest of the report.

Feedback that helps:

- **Say what was missing.** "The agent had to guess the currency rules. One line with JPY 0 and BHD 3 decimals would have saved two rounds."
- **Show a rewrite.** "Invoice totals round to 2 decimals for every currency. Round to each currency's minor units (JPY 0, BHD 3). Done when the totals match the finance export."
- **Point at the pattern, not the person.** Short follow-ups like "no, not like that" usually mean the first prompt lacked a goal or a way to check the result.
- **Praise what worked.** The `praise` label marks a prompt worth copying.

The author answers or resolves the thread like any other.

## Limits

- Claude Code sessions only for now. Other agents can still paste a prompts section by hand.
- The section is plain report text. There is no separate Prompts tab yet.
- A prompt that contains Markdown, such as headings or lists, is shown formatted inside its quote. It cannot start a new report section.
