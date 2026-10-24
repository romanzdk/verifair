import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

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
    root: dict[str, Any]
    replies: list[dict[str, Any]]
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


def _headings(tokens: list[Token]) -> Iterator[tuple[int, str]]:
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and token.tag == "h2" and token.level == 0:
            inline = tokens[index + 1]
            html = _md.renderer.renderInline(inline.children or [], _md.options, {})
            yield index, _html_to_text(html)


def section_headings(body: str) -> list[str]:
    return [heading for _, heading in _headings(_md.parse(body))]


def section_plain_text(body: str, heading: str) -> str:
    tokens = _md.parse(body)
    starts = list(_headings(tokens))
    for n, (index, name) in enumerate(starts):
        if name == heading:
            end = starts[n + 1][0] if n + 1 < len(starts) else len(tokens)
            return _plain_lines(tokens[index + 3 : end])
    return ""


def _plain_lines(tokens: list[Token]) -> str:
    lines, bullet = [], ""
    for token in tokens:
        if token.type == "list_item_open":
            bullet = "- "
        elif token.type == "inline":
            parts = (" " if c.type.endswith("break") else c.content for c in token.children or ())
            if text := normalize("".join(parts)):
                lines.append(bullet + text)
            bullet = ""
    return "\n".join(lines)


def split_sections(body: str) -> list[Section]:
    tokens = _md.parse(body)
    headings = list(_headings(tokens))
    parts = [("Report", tokens)]
    if headings:
        parts = [("Intro", tokens[: headings[0][0]])] if headings[0][0] else []
        for number, (index, heading) in enumerate(headings):
            end = headings[number + 1][0] if number + 1 < len(headings) else len(tokens)
            parts.append((heading, tokens[index + 3 : end]))
    sections: list[Section] = []
    used: set[str] = set()
    for heading, content in parts:
        base = slug = slugify(heading)
        n = 1
        while slug in used:
            n += 1
            slug = f"{base}-{n}"
        used.add(slug)
        html = _md.renderer.render(content, _md.options, {})
        sections.append(Section(heading, slug, html, _html_to_text(html)))
    return sections


def _common_len(a: str, b: str) -> int:
    return next(
        (i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), min(len(a), len(b))
    )


def _score(text: str, start: int, quote: str, prefix: str, suffix: str) -> int:
    before = _common_len(text[max(0, start - len(prefix)) : start][::-1], prefix[::-1])
    end = start + len(quote)
    return before + _common_len(text[end : end + len(suffix)], suffix)


def iter_occurrences(text: str, quote: str) -> Iterator[int]:
    if not quote:
        yield from range(len(text) + 1)
        return
    failure = [0] * len(quote)
    matched = 0
    for index in range(1, len(quote)):
        while matched and quote[index] != quote[matched]:
            matched = failure[matched - 1]
        if quote[index] == quote[matched]:
            matched += 1
        failure[index] = matched
    matched = 0
    for index, char in enumerate(text):
        while matched and char != quote[matched]:
            matched = failure[matched - 1]
        if char == quote[matched]:
            matched += 1
        if matched == len(quote):
            yield index - len(quote) + 1
            matched = failure[matched - 1]


def occurrences(text: str, quote: str) -> list[int]:
    return list(iter_occurrences(text, quote))


def find_anchor(
    section_text: str, quote: str, prefix: str | None, suffix: str | None
) -> int | None:
    prefix, suffix = prefix or "", suffix or ""
    maximum = len(prefix) + len(suffix)
    best, best_score = None, -1
    for start in iter_occurrences(section_text, quote):
        score = _score(section_text, start, quote, prefix, suffix)
        if score > best_score:
            best, best_score = start, score
        if score == maximum:
            break
    return best


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


def quote_found(quote: str, sections: list[Section]) -> bool:
    return _locate(quote, "", "", sections, []) is not None


def build_threads(
    comments: list[dict[str, Any]], sections: list[Section], current_version: int
) -> list[Thread]:
    by_heading: dict[str, list[Section]] = {}
    for s in sections:
        by_heading.setdefault(s.heading, []).append(s)
    replies: dict[int, list[dict[str, Any]]] = {}
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
