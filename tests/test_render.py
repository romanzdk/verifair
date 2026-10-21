import pytest

from verifair.db import add_comment, connect, init_schema, push_report
from verifair.render import Section, build_threads, normalize, split_sections, threads_by_section


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


def _comments(conn):
    return conn.execute("SELECT * FROM comments ORDER BY created_at, id").fetchall()
