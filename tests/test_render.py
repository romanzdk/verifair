from pathlib import Path

import pytest

from verifair import render
from verifair.db import add_comment, push_report
from verifair.render import (
    Section,
    _score,
    build_threads,
    find_anchor,
    normalize,
    section_plain_text,
    slugify,
    split_sections,
    threads_by_section,
)


@pytest.fixture
def conn(conn):
    push_report(conn, project="p", title="T", risk="low", body="x", id="r")
    return conn


def _section(heading: str, text: str) -> Section:
    return Section(heading=heading, slug=heading.lower(), html=f"<p>{text}</p>", text=text)


def test_split_basic_headings_and_tables():
    secs = split_sections("## Intent\nhi\n\n## Edge cases\n|a|b|\n|-|-|\n|1|2|\n")
    assert [s.heading for s in secs] == ["Intent", "Edge cases"]
    assert [s.slug for s in secs] == ["intent", "edge-cases"]
    assert "<table>" in secs[1].html and secs[1].text == "a b 1 2"


def test_render_markdown_preserves_document_and_escapes_html():
    html = render.render_markdown(
        "# Original plán\n\n## Steps\n\n- First\n- Second\n\n"
        "| Item | Value |\n| --- | --- |\n| Café | ✓ |\n\n"
        '```python\nprint("<safe>")\n```\n\n'
        '<script>alert(1)</script>\n\n<img src=x onerror="alert(2)">\n\n'
        "<!-- secret -->\n\n{{ 7 * 7 }}"
    )
    for part in (
        "<h1>Original plán</h1>",
        "<h2>Steps</h2>",
        "<ul>",
        "<li>First</li>",
        "<table>",
        "<td>Café</td>",
        "<td>✓</td>",
        '<pre><code class="language-python">',
        "&lt;safe&gt;",
        "&lt;script&gt;",
        "&lt;img",
        "&lt;!-- secret --&gt;",
        "{{ 7 * 7 }}",
    ):
        assert part in html
    assert "<script" not in html and "<img" not in html and "<!--" not in html
    assert "data-section" not in html and 'id="' not in html


def test_render_markdown_rejects_unsafe_links():
    html = render.render_markdown(
        "[bad](javascript:alert(1))\n\n[bad](data:text/html;base64,PHNjcmlwdD4=)\n\n"
        "[safe](https://example.com/plan)\n\n![bad](data:image/svg+xml;base64,PHN2Zz4=)"
    )
    assert 'href="javascript:' not in html and 'href="data:' not in html
    assert 'src="data:' not in html
    assert '<a href="https://example.com/plan">safe</a>' in html


def test_no_headings_is_single_report_section():
    assert [s.heading for s in split_sections("just text")] == ["Report"]


def test_content_before_first_heading_is_intro():
    secs = split_sections("preamble\n\n## A\nx")
    assert [s.heading for s in secs] == ["Intro", "A"]
    assert secs[0].text == "preamble"


def test_duplicate_headings_get_unique_slugs(conn):
    sections = split_sections("## A\nx\n## A\ny\n## A\nz")
    assert [s.slug for s in sections] == ["a", "a-2", "a-3"]
    add_comment(
        conn, report_id="r", version=1, author_id="u", author_name="U", body="b", section="A"
    )
    assert build_threads(_comments(conn), sections, 1)[0].section_slug == "a"


def test_heading_inside_fenced_code_is_not_a_section():
    assert len(split_sections("## A\n```\n## not a heading\n```\n")) == 1


def test_trailing_hashes_are_stripped_from_heading():
    assert split_sections("## A ##\nx")[0].heading == "A"


@pytest.mark.parametrize(
    "body",
    [
        "## Real\n  ```\n## Still code\n  ```\ntext",
        "## Real\n~~~~\n## Still code\n~~~\n## More code\n~~~~\ntext",
        "## Real\n```\n## Still code\n~~~\n## More code\n```\ntext",
        "## Real\n    ## Indented code\n",
    ],
)
def test_section_boundaries_follow_markdown_tokens(body):
    [section] = split_sections(body)
    assert section.heading == "Real"
    assert "<pre><code>" in section.html
    assert "Still code" in section.text or "Indented code" in section.text


def test_setext_h2_is_a_real_section_boundary():
    sections = split_sections("Intro text\n\nVerification\n------------\nevidence")
    assert [s.heading for s in sections] == ["Intro", "Verification"]
    assert sections[1].text == "evidence"


def test_overlapping_anchor_stops_at_first_maximum_context_score(monkeypatch):
    from verifair import render

    calls = []
    original = render._score

    def count_score(*args):
        calls.append(args[1])
        return original(*args)

    monkeypatch.setattr(render, "_score", count_score)
    assert find_anchor("a" * 262_144, "a" * 2000, "a" * 64, "a" * 64) == 64
    assert len(calls) <= 65


def test_anchor_does_not_truncate_late_best_match_or_replace_first_tie():
    text = "a" * 20_000 + "b" + "a" * 2000 + "c" + "b" + "a" * 2000 + "c"
    assert find_anchor(text, "a" * 2000, "b", "c") == 20_001
    assert find_anchor("aaaa", "aa", "x", "y") == 0


@pytest.mark.parametrize("last", ["a", "b"])
def test_overlapping_anchor_quote_comparisons_have_linear_bound(last):
    class CountedQuote(str):
        reads = 0

        def __getitem__(self, index):
            if isinstance(index, int):
                self.reads += 1
            return super().__getitem__(index)

    text = "a" * 262_144
    quote = CountedQuote("a" * 1999 + last)
    expected = 63 if last == "a" else None
    assert find_anchor(text, quote, "b" + "a" * 63, "a" * 63 + "b") == expected


def test_anchor_matches_exhaustive_best_score_and_first_tie_oracle():
    import random

    randomizer = random.Random(41)
    for _ in range(1000):
        text = "".join(randomizer.choices("aaab 😀", k=40))
        quote = text[randomizer.randrange(30) :][: randomizer.randrange(1, 9)]
        prefix = "".join(randomizer.choices("ab 😀", k=randomizer.randrange(7)))
        suffix = "".join(randomizer.choices("ab 😀", k=randomizer.randrange(7)))
        hits = [i for i in range(len(text)) if text.startswith(quote, i)]
        expected = max(hits, key=lambda i: _score(text, i, quote, prefix, suffix), default=None)
        assert find_anchor(text, quote, prefix, suffix) == expected


def test_normalize_collapses_whitespace():
    assert normalize("  a \n\t b  ") == "a b"


def test_thread_found_normalises_whitespace_and_marks_stale(conn):
    sections = split_sections("## Price\nthe  full\nnew price\n")
    add_comment(
        conn,
        report_id="r",
        version=1,
        author_id="u",
        author_name="U",
        body="why?",
        section="Price",
        quote="full new price",
    )
    (thread,) = build_threads(_comments(conn), sections, current_version=2)
    assert thread.section_slug == "price"
    assert thread.found is True
    assert thread.stale is True


def test_quote_spanning_inline_markup_is_found(conn):
    sections = split_sections("## A\nprice is **5**. See `x`, ok.")
    add_comment(
        conn,
        report_id="r",
        version=1,
        author_id="u",
        author_name="U",
        body="b",
        section="A",
        quote="price is 5. See x, ok.",
    )
    assert build_threads(_comments(conn), sections, 1)[0].found is True


def test_thread_quote_missing_from_section_is_not_found(conn):
    push_report(conn, project="p", title="T", risk="low", body="new price", id="r")
    add_comment(
        conn,
        report_id="r",
        version=2,
        author_id="u",
        author_name="U",
        body="b",
        section="Price",
        quote="old price",
    )
    (thread,) = build_threads(_comments(conn), [_section("Price", "new price")], 2)
    assert thread.found is False
    assert thread.stale is False


def test_thread_section_gone_is_orphaned(conn):
    add_comment(
        conn,
        report_id="r",
        version=1,
        author_id="u",
        author_name="U",
        body="b",
        section="Removed",
        quote="q",
    )
    threads = build_threads(_comments(conn), [_section("Price", "x")], 1)
    assert threads[0].section_slug is None
    assert threads[0].found is False
    assert threads_by_section(threads)[None] == threads


def test_replies_attach_to_root_and_groups_keep_order(conn):
    kwargs = {"report_id": "r", "version": 1, "author_id": "u", "author_name": "U"}
    first = add_comment(conn, body="1", section="A", **kwargs)
    second = add_comment(conn, body="2", section="B", **kwargs)
    reply = add_comment(conn, body="re", parent_id=first["id"], **kwargs)
    sections = [_section("A", "x"), _section("B", "y")]
    threads = build_threads(_comments(conn), sections, 1)
    assert [t.root["id"] for t in threads] == [first["id"], second["id"]]
    assert [r["id"] for r in threads[0].replies] == [reply["id"]]
    assert threads[0].found is True
    assert list(threads_by_section(threads)) == ["a", "b"]


def test_raw_html_in_report_is_shown_as_text():
    (s,) = split_sections("## A\nReturns List<Order> <script>alert(1)</script> <!-- x -->")
    assert "<script>" not in s.html and "<Order>" not in s.html and "<!--" not in s.html
    assert "List&lt;Order&gt;" in s.html and "&lt;!-- x --&gt;" in s.html
    assert "List<Order>" in s.text


def test_javascript_link_has_no_href():
    (s,) = split_sections("## A\n[x](javascript:alert(1)) [ok](https://example.com)")
    assert 'href="javascript' not in s.html and 'href="https://example.com"' in s.html
    assert s.html.count("href=") == 1


def test_fenced_code_and_bold_render():
    (s,) = split_sections("## A\n**b**\n\n```\nx < y\n```")
    assert "<strong>b</strong>" in s.html and "<pre><code>x &lt; y" in s.html


def test_duplicate_heading_thread_goes_to_section_containing_quote(conn):
    sections = split_sections("## Notes\nfirst part\n## Notes\nsecond part")
    add_comment(
        conn,
        report_id="r",
        version=1,
        author_id="u",
        author_name="U",
        body="b",
        section="Notes",
        quote="second part",
    )
    (thread,) = build_threads(_comments(conn), sections, 1)
    assert (thread.section_slug, thread.found) == ("notes-2", True)


def test_slugify_folds_to_ascii():
    assert slugify("Žluťoučký kůň") == "zlutoucky-kun"
    assert slugify("!!!") == "report"


def test_threads_sort_by_id_not_created_at(conn):
    kwargs = {"report_id": "r", "version": 1, "author_id": "u", "author_name": "U"}
    first = add_comment(conn, body="1", **kwargs)
    second = add_comment(conn, body="2", **kwargs)
    reply = add_comment(conn, body="re", parent_id=first["id"], **kwargs)
    conn.execute(
        "UPDATE comments SET created_at = '2000-01-01T00:00:00+00:00' WHERE id = %s",
        (second["id"],),
    )
    conn.execute(
        "UPDATE comments SET created_at = '1999-01-01T00:00:00+00:00' WHERE id = %s", (reply["id"],)
    )
    threads = build_threads(_comments(conn), [], 1)
    assert [t.root["id"] for t in threads] == [first["id"], second["id"]]


def _comments(conn):
    return conn.execute("SELECT * FROM comments ORDER BY created_at, id").fetchall()


def test_report_template_has_ten_sections_and_no_html_comments():
    from pathlib import Path

    body = (Path(__file__).parents[1] / "docs" / "report-template.md").read_text()
    sections = split_sections(body)
    assert [s.heading for s in sections] == [
        "Intent",
        "Judge this",
        "Design",
        "Edge cases",
        "Verification",
        "Not verified / assumptions",
        "Rollback",
        "Security impact",
        "Agent reviews",
        "Links",
    ]
    assert all("&lt;!--" not in s.html and s.html.startswith("<blockquote>") for s in sections)


def test_section_less_threads_are_general_not_orphaned(conn):
    kwargs = {"report_id": "r", "version": 1, "author_id": "u", "author_name": "U"}
    general = add_comment(conn, body="g", **kwargs)
    orphan = add_comment(conn, body="o", section="Removed", **kwargs)
    threads = build_threads(_comments(conn), [_section("Price", "x")], 1)
    grouped = threads_by_section(threads)
    assert [t.root["id"] for t in grouped["vf_general"]] == [general["id"]]
    assert [t.root["id"] for t in grouped[None]] == [orphan["id"]]
    assert grouped["vf_general"][0].found is True


def _quote_comment(conn, section, quote, prefix=None, suffix=None):
    add_comment(
        conn,
        report_id="r",
        version=1,
        author_id="u",
        author_name="U",
        body="b",
        section=section,
        quote=quote,
        prefix=prefix,
        suffix=suffix,
    )


def test_find_anchor_scores_prefix_and_suffix():
    text = "the price is 5. later the price is 6."
    assert find_anchor(text, "the price is", None, None) == 0
    assert find_anchor(text, "the price is", "5. later ", " 6.") == text.rindex("the price")
    assert find_anchor(text, "the price is", "", " 5.") == 0
    assert find_anchor(text, "missing", "", "") is None


def test_duplicate_quote_picks_occurrence_by_prefix_and_suffix(conn):
    sections = split_sections("## A\nthe price is 5. later the price is 6.")
    _quote_comment(conn, "A", "the price is", prefix="is 5. later ", suffix=" 6.")
    (thread,) = build_threads(_comments(conn), sections, 1)
    assert (thread.found, thread.moved) == (True, False)
    assert thread.match_start == sections[0].text.rindex("the price")


def test_duplicate_heading_with_quote_in_both_prefers_better_context(conn):
    sections = split_sections("## Notes\nalpha same thing\n## Notes\nbeta same thing")
    _quote_comment(conn, "Notes", "same thing", prefix="beta ")
    (thread,) = build_threads(_comments(conn), sections, 1)
    assert (thread.section_slug, thread.match_start) == ("notes-2", 5)


def test_renamed_heading_keeps_thread_found_and_moved(conn):
    sections = split_sections("## Intro\nhello\n## New name\nkeep this line")
    _quote_comment(conn, "Old name", "this line")
    (thread,) = build_threads(_comments(conn), sections, 1)
    assert (thread.section_slug, thread.found, thread.moved) == ("new-name", True, True)
    assert thread.match_start == 5


def test_quote_moved_out_of_its_section_is_found_elsewhere(conn):
    sections = split_sections("## A\nnothing here\n## B\nthe moved text")
    _quote_comment(conn, "A", "moved text")
    (thread,) = build_threads(_comments(conn), sections, 1)
    assert (thread.section_slug, thread.found, thread.moved) == ("b", True, True)


def test_quote_across_table_cells_matches_without_whitespace(conn):
    sections = split_sections("## A\n|Price|Qty|\n|-|-|\n|10|20|\n")
    assert sections[0].text == "Price Qty 10 20"
    _quote_comment(conn, "A", "Qty10")
    (thread,) = build_threads(_comments(conn), sections, 1)
    assert (thread.found, thread.moved, thread.match_start) == (True, False, 6)


def test_section_level_and_orphan_threads_have_no_match_start(conn):
    _quote_comment(conn, "A", None)
    _quote_comment(conn, "A", "gone")
    plain, orphan = build_threads(_comments(conn), [_section("A", "x")], 1)
    assert (plain.found, plain.match_start, plain.moved) == (True, None, False)
    assert (orphan.found, orphan.match_start, orphan.moved) == (False, None, False)


def test_score_reads_only_as_much_text_as_the_context_is_long():
    text = "x" * 100_000 + "the price is 5." + "y" * 100_000
    start = text.index("the price")
    assert _score(text, start, "the price is", "price: ", " 5.y") == 0 + 0 + 4
    assert _score(text, start, "the price is", "xxx", " 5.yz") == 3 + 4


def test_score_bounds_text_slices_to_context_lengths():
    class TrackedText(str):
        def __getitem__(self, key):
            result = super().__getitem__(key)
            if isinstance(key, slice):
                assert len(result) <= 5
            return result

    text = TrackedText("x" * 100_000 + "the price is 5." + "y" * 100_000)
    assert _score(text, 100_000, "the price is", "xxx", " 5.yz") == 7


def test_section_plain_text_keeps_lines_and_link_text():
    body = (
        "## Intent\nx\n\n## Judge this\nIs a **day**\nthe right [unit](https://e.com/u)?\n\n"
        "- first `item`\n- second\n\n```py\nsecret()\n```\n\n    indented code\n\n"
        "![chart](https://e.com/c.png)\n\n## Rollback\ny"
    )
    assert section_plain_text(body, "Judge this") == (
        "Is a day the right unit?\n\n- first item\n- second\n\nchart"
    )
    assert section_plain_text(body, "Missing") == ""


def test_section_plain_text_drops_a_leading_template_hint():
    body = (
        "## Judge this\n> The questions only a human can answer: product intent, trade-offs,"
        " risk you accept.\n\nIs a day right?\n\n> A quote the agent meant.\n"
    )
    assert section_plain_text(body, "Judge this") == "Is a day right?\n\nA quote the agent meant."
    other = body.replace("Judge this", "Design")
    assert section_plain_text(other, "Design").startswith("The questions only")
    own = "## Judge this\n> Finance wrote: round once.\n\nIs that a decision?\n"
    assert (
        section_plain_text(own, "Judge this") == "Finance wrote: round once.\n\nIs that a decision?"
    )


def test_the_dropped_hint_is_the_report_templates():
    template = (Path(__file__).parents[1] / "docs" / "report-template.md").read_text()
    assert section_plain_text(template, "Judge this").startswith("- Is ")


def test_section_plain_text_keeps_numbers_and_table_rows():
    body = (
        "## Judge this\nFirst para.\n\nSecond para.\n\n1. one\n2. two\n\n"
        "| Case | Result |\n|---|---|\n| empty | **0** |\n| one | 1 |\n"
    )
    assert section_plain_text(body, "Judge this") == (
        "First para.\n\nSecond para.\n\n1. one\n2. two\n\nCase | Result\nempty | 0\none | 1"
    )


def test_anchoring_stays_fast_on_a_huge_repetitive_section():
    import time

    from verifair.render import Section, build_threads, find_anchor

    text = "a" * 900_000
    started = time.perf_counter()
    for _ in range(20):
        assert find_anchor(text, "aa", "b", "b") == 0
    sections = [Section(heading="H", slug="h", html="", text=text)]
    comments = [
        {
            "id": i,
            "parent_id": None,
            "version": 1,
            "section": "H",
            "quote": "aa",
            "prefix": "b",
            "suffix": "b",
            "resolved_at": None,
            "blocking": 1,
        }
        for i in range(1, 21)
    ]
    build_threads(comments, sections, 1)
    assert time.perf_counter() - started < 2


def test_occurrences_still_overlap():
    from verifair.render import occurrences

    assert occurrences("aaaa", "aa") == [0, 1, 2]
    assert occurrences("abc", "") == [0, 1, 2, 3]


def test_external_images_become_links_and_local_ones_stay():
    from verifair.render import render_markdown

    html = render_markdown("![chart](https://grafana.example.com/x.png?a=1&b=2)")
    assert "<img" not in html
    assert (
        '<a class="ext-img" href="https://grafana.example.com/x.png?a=1&amp;b=2" target="_blank"'
        ' rel="noopener noreferrer">Image from grafana.example.com: chart</a>'
    ) in html
    assert '<img src="/static/logo.svg" alt="logo"' in render_markdown("![logo](/static/logo.svg)")
    assert "<img" in render_markdown("![dot](data:image/png;base64,iVBORw0KGgo=)")
    assert "<img" not in render_markdown("![x](//evil.example/x.png)")
    assert "&lt;b&gt;" in render_markdown("![<b>](https://e.com/x.png)")


def test_mermaid_fence_stays_a_code_block_the_page_script_can_find():
    html = render.render_markdown("```mermaid\nflowchart LR\n  A --> B\n```")
    assert html == '<pre><code class="language-mermaid">flowchart LR\n  A --&gt; B\n</code></pre>\n'


def test_uploaded_image_path_renders_as_an_image():
    html = render.render_markdown("![Inbox](/images/" + "a" * 64 + ".png)")
    assert html == f'<p><img src="/images/{"a" * 64}.png" alt="Inbox" /></p>\n'
