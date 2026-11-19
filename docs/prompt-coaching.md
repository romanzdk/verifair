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
python3 -I export-prompts.py ~/.codex/sessions/YYYY/MM/DD/rollout-SESSION.jsonl
python3 -I export-prompts.py SESSION.jsonl --upto 14 --drop 1-3,9
```

The format is detected automatically. For Codex, select the rollout for your session ID and check its `session_meta` working directory; do not pick a file just because it is newest. A custom `CODEX_HOME` changes the directory above, and archived rollouts can be under `archived_sessions/`. The exporter reads user-message events and user input messages, collapsing consecutive matching records from the two representations so mirrored prompts are not shown twice. Repeated prompts you actually typed stay separate. Known injected instructions, environment context and T3 handoffs/history are skipped; images become `[image]` placeholders without their paths.

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

## Evals, DSPy and prompt coaching

Evals and tools like [DSPy](https://dspy.ai) improve prompts by measuring them: you run the same prompt on many test cases, score the results and tune it. That works for prompts that run again and again, such as a system prompt, a skill, a `CLAUDE.md` or a product feature.

Most prompts typed into a coding agent run once. There is no test set to score them on, so feedback from a person is the practical option. The two work together:

- **No evals yet?** Start here. A teammate's comment needs no setup, no test cases and no tooling.
- **Already using evals?** A teammate who knows the project still sees what a score does not: context about the codebase the agent was missing, an unclear goal, a habit that costs extra rounds.
- **Same advice again and again?** Move it into something reusable, like a `CLAUDE.md` rule, a skill or a prompt template. That reusable text is what evals and DSPy can then measure and tune.

## Limits

- Claude Code sessions and local Codex rollout JSONL are supported (Codex 0.167.0 response-item layout checked locally; user-message event layout covered by synthetic tests). Codex rollout files are an internal format and can change. `codex exec --json` output contains execution events, not input prompts; use the rollout instead. Other agents and Codex sessions without a local rollout can still paste a prompts section by hand.
- The section is plain report text. There is no separate Prompts tab yet.
- A prompt that contains Markdown, such as headings or lists, is shown formatted inside its quote. It cannot start a new report section.
