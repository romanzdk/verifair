"""Print your Claude Code or Codex session prompts as a `## Prompts` report section.

Usage: python3 -I export-prompts.py SESSION.jsonl [--upto N] [--drop 1-3,5]

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
HARNESS_OUTPUT = (
    "<task-notification>",
    "<local-command-",
    "[Request interrupted",
    "Delegated task node:",
)
HARNESS_BLOCK = re.compile(r"<(system-reminder|t3_context)\b[^>]*>.*?</\1>\s*", re.DOTALL)
SAVED_AT = re.compile(r'(\[Attached \w+ "[^"\n]*") is saved at: [^\]\n]*\]')
SLASH_COMMAND = re.compile(r"<command-name>(.*?)</command-name>")
COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
T3_HANDOFF = ("Context handoff (", "\nUser message:\n")
T3_QUOTE = re.compile(r"\[[^\]\n]*\]\((t3-citation://[^)\s]+)\)")
CODEX_CONTEXT = (
    "# AGENTS.md instructions",
    "<environment_context>",
    "[Historical user;",
    "[Historical assistant;",
    "<skill>",
)


def readable_quote(link: re.Match) -> str:
    query = parse_qs(urlsplit(link[1]).query)
    quoted = " ".join(query.get("text", [""])[0].split())
    quoted = quoted if len(quoted) <= 120 else quoted[:119] + "…"
    comment = query.get("comment", [""])[0].strip()
    on = f'On the agent\'s text "{quoted}"'
    return f"{on}: {comment}" if comment else on


def block_text(block: dict) -> str:
    kind = block.get("type")
    if kind == "text":
        return str(block.get("text", ""))
    return "[image]" if kind == "image" else "[attachment]"


def prompt_text(record: dict) -> str | None:
    if record.get("type") != "user":
        return None
    if record.get("isMeta") or record.get("isCompactSummary") or record.get("isSidechain"):
        return None
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        blocks = [block for block in content if isinstance(block, dict)]
        if any(block.get("type") == "tool_result" for block in blocks):
            return None
        content = "\n\n".join(filter(None, map(block_text, blocks)))
    if not isinstance(content, str):
        return None
    text = SAVED_AT.sub(r"\1]", HARNESS_BLOCK.sub("", content)).strip()
    if text.startswith(HARNESS_OUTPUT):
        return None
    if command := SLASH_COMMAND.search(text):
        args = COMMAND_ARGS.search(text)
        args = args[1].strip() if args else ""
        return f"{command[1]} {args}" if args else None
    if text.startswith(T3_HANDOFF[0]):
        if T3_HANDOFF[1] not in text:
            return None
        text = text.split(T3_HANDOFF[1], 1)[1].strip()
    text = T3_QUOTE.sub(readable_quote, text)
    return text or None


def codex_prompt(record: dict) -> str | None:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    if record.get("type") == "event_msg" and payload.get("type") == "user_message":
        text = payload.get("message")
        if not isinstance(text, str):
            return None
        images = sum(
            len(value)
            for key in ("images", "local_images")
            if isinstance(value := payload.get(key), list)
        )
        content = [{"type": "text", "text": text}] + [{"type": "image"}] * images
    elif (
        record.get("type") == "response_item"
        and payload.get("type") == "message"
        and payload.get("role") == "user"
        and isinstance(payload.get("content"), list)
    ):
        content = []
        for block in payload["content"]:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "input_text" and isinstance(block.get("text"), str):
                text = block["text"]
                content.append({"type": "text", "text": text})
            elif block.get("type") == "input_image":
                content.append({"type": "image"})
    else:
        return None
    content = [
        block for block in content if not block.get("text", "").lstrip().startswith(CODEX_CONTEXT)
    ]
    return prompt_text({"type": "user", "message": {"content": content}})


def is_subagent(meta: dict) -> bool:
    payload = meta.get("payload")
    source = payload.get("source") if isinstance(payload, dict) else None
    return isinstance(source, dict) and "subagent" in source


def read_prompts(path: str) -> list[str]:
    prompts = []
    previous_source = None
    with open(path, encoding="utf-8", errors="replace") as lines:
        for line in lines:
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, RecursionError):
                previous_source = None
                continue
            if not isinstance(record, dict):
                previous_source = None
                continue
            if record.get("type") == "session_meta" and is_subagent(record):
                raise ValueError(
                    f"{path} is a subagent session: another agent wrote its prompts, not the author"
                )
            if not (text := prompt_text(record) or codex_prompt(record)):
                previous_source = None
                continue
            source = record.get("type")
            if (
                source in ("event_msg", "response_item")
                and previous_source in ("event_msg", "response_item")
                and source != previous_source
                and prompts[-1] == text
            ):
                previous_source = None
                continue
            prompts.append(text)
            previous_source = source
    return prompts


def numbers(value: str) -> set[int]:
    chosen = set()
    for part in filter(None, (p.strip() for p in value.split(","))):
        start, _, end = part.partition("-")
        try:
            first, last = int(start), int(end or start)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a number or range: {part}") from None
        if not 0 < first <= last:
            raise argparse.ArgumentTypeError(f"not a valid range: {part}")
        chosen.update(range(first, last + 1))
    return chosen


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
    parser.add_argument("session", help="Claude Code session or Codex rollout .jsonl file")
    parser.add_argument(
        "--upto",
        type=int,
        help="use only the first N prompts, the ones the author already read",
    )
    parser.add_argument(
        "--drop",
        default=set(),
        type=numbers,
        help="prompt numbers or ranges to leave out, e.g. 1-3,5",
    )
    args = parser.parse_args()
    try:
        prompts = read_prompts(args.session)[: args.upto]
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if not prompts:
        parser.error(
            f"no prompts found in {args.session}: expected a Claude Code session or a Codex"
            " rollout-*.jsonl file (`codex exec --json` output has no prompts)"
        )
    if unknown := sorted(args.drop - set(range(1, len(prompts) + 1))):
        parser.error(f"--drop: no prompt numbered {unknown} (there are {len(prompts)})")
    print(section(prompts, args.drop), end="")


if __name__ == "__main__":
    main()
