import pytest

from verifair.db import add_comment, connect, init_schema, push_report
from verifair.render import (
    Section,
    build_threads,
    normalize,
    slugify,
    split_sections,
    threads_by_section,
)


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_schema(c)
    push_report(c, project="p", title="T", risk="low", body="x", id="r")
    yield c
    c.close()


def _section(heading: str, text: str) -> Section:
    return Section(heading=heading, slug=heading.lower(), html=f"<p>{text}</p>", text=text)


def test_split_basic_headings_and_tables():
    secs = split_sections("## Intent\nhi\n\n## Edge cases\n|a|b|\n|-|-|\n|1|2|\n")
    assert [s.heading for s in secs] == ["Intent", "Edge cases"]
    assert [s.slug for s in secs] == ["intent", "edge-cases"]
    assert "<table>" in secs[1].html and secs[1].text == "a b 1 2"


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
        "UPDATE comments SET created_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (second["id"],)
    )
    conn.execute(
        "UPDATE comments SET created_at = '1999-01-01T00:00:00+00:00' WHERE id = ?", (reply["id"],)
    )
    threads = build_threads(_comments(conn), [], 1)
    assert [t.root["id"] for t in threads] == [first["id"], second["id"]]


def _comments(conn):
    return conn.execute("SELECT * FROM comments ORDER BY created_at, id").fetchall()


def test_report_template_has_seven_sections_and_no_html_comments():
    from pathlib import Path

    body = (Path(__file__).parents[1] / "docs" / "report-template.md").read_text()
    sections = split_sections(body)
    assert [s.heading for s in sections] == [
        "Intent",
        "Design",
        "Edge cases",
        "Verification",
        "Agent reviews",
        "Judge this",
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
