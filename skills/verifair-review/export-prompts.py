"""Print the prompts you typed in one Claude Code session as a `## Prompts` report section.

Usage: python3 -I export-prompts.py SESSION.jsonl [--drop 2,5]

Read the output before you share it: it is your words, as typed, secrets included.
Nothing is uploaded; the section goes to stdout.
"""

import argparse
import json
import re
from urllib.parse import parse_qs, urlsplit

INTRO = (
    "Optional, for learning: the prompts the author typed in the agent session, "
    "word for word. Ideas for clearer prompts are welcome. Please use the "
    "**suggestion** label, so your comment does not block approval."
)
HARNESS_OUTPUT = ("<task-notification>", "<local-command-", "[Request interrupted")
SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>\s*", re.DOTALL)
SLASH_COMMAND = re.compile(r"<command-name>(.*?)</command-name>")
COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
T3_HANDOFF = "\nUser message:\n"
T3_QUOTE = re.compile(r"\[[^\]\n]*\]\((t3-citation://[^)\s]+)\)")


def readable_quote(link: re.Match) -> str:
    query = parse_qs(urlsplit(link[1]).query)
    quoted = " ".join(query.get("text", [""])[0].split())
    quoted = quoted if len(quoted) <= 120 else quoted[:119] + "…"
    comment = query.get("comment", [""])[0].strip()
    return f'On "{quoted}": {comment}' if comment else f'On "{quoted}"'


def prompt_text(record: dict) -> str | None:
    if record.get("type") != "user":
        return None
    if record.get("isMeta") or record.get("isCompactSummary") or record.get("isSidechain"):
        return None
    content = record.get("message", {}).get("content")
    if isinstance(content, list):
        if any(block.get("type") not in ("text", "image") for block in content):
            return None
        content = "\n\n".join(
            block["text"] if block["type"] == "text" else "[image]" for block in content
        )
    if not isinstance(content, str):
        return None
    text = SYSTEM_REMINDER.sub("", content).strip()
    if text.startswith(HARNESS_OUTPUT):
        return None
    if command := SLASH_COMMAND.search(text):
        args = COMMAND_ARGS.search(text)
        args = args[1].strip() if args else ""
        return f"{command[1]} {args}" if args else None
    if text.startswith("Context handoff (") and T3_HANDOFF in text:
        text = text.split(T3_HANDOFF, 1)[1].strip()
    text = T3_QUOTE.sub(readable_quote, text)
    return text or None


def read_prompts(path: str) -> list[str]:
    prompts = []
    with open(path, encoding="utf-8") as lines:
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and (text := prompt_text(record)):
                prompts.append(text)
    return prompts


def section(prompts: list[str], drop: set[int]) -> str:
    parts = ["## Prompts", INTRO]
    for number, text in enumerate(prompts, 1):
        if number in drop:
            continue
        quoted = "\n".join(f"> {line}" if line else ">" for line in text.splitlines())
        parts.append(f"**{number}.**\n\n{quoted}")
    return "\n\n".join(parts) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("session", help="Claude Code session file (~/.claude/projects/*/*.jsonl)")
    parser.add_argument(
        "--drop",
        default="",
        type=lambda value: {int(n) for n in value.split(",") if n.strip()},
        help="comma-separated prompt numbers to leave out",
    )
    args = parser.parse_args()
    print(section(read_prompts(args.session), args.drop or set()), end="")


if __name__ == "__main__":
    main()
