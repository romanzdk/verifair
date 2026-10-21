import re
import sqlite3
from dataclasses import dataclass
from html.parser import HTMLParser

import markdown

from verifair.db import slugify


@dataclass
class Section:
    heading: str
    slug: str
    html: str
    text: str


@dataclass
class Thread:
    root: sqlite3.Row
    replies: list[sqlite3.Row]
    section_slug: str | None
    found: bool
    stale: bool


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _html_to_text(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    return normalize(" ".join(extractor.parts))


def _split_raw(body: str) -> list[tuple[str, list[str]]]:
    parts: list[tuple[str, list[str]]] = [("Intro", [])]
    in_fence = False
    for line in body.splitlines():
        if line.startswith(("```", "~~~")):
            in_fence = not in_fence
        if not in_fence and line.startswith("## "):
            heading = re.sub(r"\s+##\s*$", "", line[3:].strip()).strip()
            parts.append((heading, []))
        else:
            parts[-1][1].append(line)
    if len(parts) == 1:
        return [("Report", parts[0][1])]
    if not "".join(parts[0][1]).strip():
        parts.pop(0)
    return parts


def split_sections(body: str) -> list[Section]:
    sections: list[Section] = []
    used: set[str] = set()
    for heading, lines in _split_raw(body):
        base = slug = slugify(heading)
        n = 1
        while slug in used:
            n += 1
            slug = f"{base}-{n}"
        used.add(slug)
        html = markdown.markdown("\n".join(lines), extensions=["tables", "fenced_code"])
        sections.append(Section(heading, slug, html, _html_to_text(html)))
    return sections


def build_threads(
    comments: list[sqlite3.Row], sections: list[Section], current_version: int
) -> list[Thread]:
    by_heading = {s.heading: s for s in sections}
    replies: dict[int, list[sqlite3.Row]] = {}
    for c in comments:
        if c["parent_id"] is not None:
            replies.setdefault(c["parent_id"], []).append(c)
    threads = []
    for root in sorted(comments, key=lambda c: (c["created_at"], c["id"])):
        if root["parent_id"] is not None:
            continue
        section = by_heading.get(root["section"])
        quote = root["quote"]
        found = section is not None and (not quote or normalize(quote) in normalize(section.text))
        threads.append(
            Thread(
                root=root,
                replies=sorted(
                    replies.get(root["id"], []), key=lambda r: (r["created_at"], r["id"])
                ),
                section_slug=section.slug if section else None,
                found=found,
                stale=root["version"] != current_version,
            )
        )
    return threads


def threads_by_section(threads: list[Thread]) -> dict[str | None, list[Thread]]:
    grouped: dict[str | None, list[Thread]] = {}
    for t in threads:
        grouped.setdefault(t.section_slug, []).append(t)
    return grouped
