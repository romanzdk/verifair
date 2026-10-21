import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from os.path import commonprefix

from markdown_it import MarkdownIt

# slugify never makes "_", so this cannot collide with a section slug
GENERAL = "vf_general"

_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")


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
    blocking: bool
    match_start: int | None
    moved: bool


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-") or "report"


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _html_to_text(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    return normalize("".join(extractor.parts))


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
        html = _md.render("\n".join(lines))
        sections.append(Section(heading, slug, html, _html_to_text(html)))
    return sections


def _score(text: str, start: int, quote: str, prefix: str, suffix: str) -> int:
    before = len(commonprefix([text[max(0, start - len(prefix)) : start][::-1], prefix[::-1]]))
    end = start + len(quote)
    return before + len(commonprefix([text[end : end + len(suffix)], suffix]))


def occurrences(text: str, quote: str) -> list[int]:
    return [m.start() for m in re.finditer(f"(?={re.escape(quote)})", text)]


def find_anchor(
    section_text: str, quote: str, prefix: str | None, suffix: str | None
) -> int | None:
    return max(
        occurrences(section_text, quote),
        key=lambda s: _score(section_text, s, quote, prefix or "", suffix or ""),
        default=None,
    )


def _squash(text: str) -> str:
    return re.sub(r"\s", "", text)


def _locate(
    quote: str, prefix: str, suffix: str, same: list[Section], every: list[Section]
) -> tuple[Section, int, bool] | None:
    for squash in (False, True):
        fix = _squash if squash else lambda t: t
        q, p, s = fix(quote), fix(prefix), fix(suffix)
        for pool, moved in ((same, False), (every, True)):
            hits = []
            for section in pool:
                text = fix(section.text)
                start = find_anchor(text, q, p, s)
                if start is not None:
                    hits.append((_score(text, start, q, p, s), section, start))
            if hits:
                _, section, start = max(hits, key=lambda h: h[0])
                if squash:
                    start = [m.start() for m in re.finditer(r"\S", section.text)][start]
                return section, start, moved
    return None


def build_threads(
    comments: list[sqlite3.Row], sections: list[Section], current_version: int
) -> list[Thread]:
    by_heading: dict[str, list[Section]] = {}
    for s in sections:
        by_heading.setdefault(s.heading, []).append(s)
    replies: dict[int, list[sqlite3.Row]] = {}
    for c in comments:
        if c["parent_id"] is not None:
            replies.setdefault(c["parent_id"], []).append(c)
    threads = []
    for root in sorted(comments, key=lambda c: c["id"]):
        if root["parent_id"] is not None:
            continue
        candidates = by_heading.get(root["section"], [])
        quote = normalize(root["quote"] or "")
        general = root["section"] is None
        section, match_start, moved = None, None, False
        if quote:
            prefix, suffix = (re.sub(r"\s+", " ", root[k] or "") for k in ("prefix", "suffix"))
            located = _locate(quote, prefix, suffix, candidates, sections)
            if located:
                section, match_start, moved = located
        found = section is not None or not quote and (general or bool(candidates))
        section = section or next(iter(candidates), None)
        threads.append(
            Thread(
                root=root,
                replies=sorted(replies.get(root["id"], []), key=lambda r: r["id"]),
                section_slug=section.slug if section else GENERAL if general else None,
                found=found,
                stale=root["version"] != current_version,
                blocking=bool(root["blocking"]),
                match_start=match_start,
                moved=moved,
            )
        )
    return threads


def threads_by_section(threads: list[Thread]) -> dict[str | None, list[Thread]]:
    grouped: dict[str | None, list[Thread]] = {}
    for t in threads:
        grouped.setdefault(t.section_slug, []).append(t)
    return grouped
