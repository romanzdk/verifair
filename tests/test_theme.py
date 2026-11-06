import re
from pathlib import Path

import pytest

CSS = (Path(__file__).parents[1] / "verifair" / "static" / "app.css").read_text()


def html_tag(page):
    return re.search(r"<html[^>]*>", page)[0]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_choosing_a_theme_sets_a_cookie_and_returns_to_the_page(client, theme):
    r = client.post(
        "/theme", data={"theme": theme, "next": "/changes?tab=all"}, follow_redirects=False
    )
    assert (r.status_code, r.headers["location"]) == (303, "/changes?tab=all")
    assert f"vf_theme={theme}" in r.headers["set-cookie"]
    assert f'data-theme="{theme}"' in html_tag(client.get("/").text)


def test_system_clears_the_choice_and_unsafe_next_goes_home(client):
    client.post("/theme", data={"theme": "dark", "next": "/"})
    r = client.post("/theme", data={"theme": "system", "next": "//evil"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    assert "data-theme" not in html_tag(client.get("/").text)


def test_unknown_values_are_refused_or_ignored(client):
    assert client.post("/theme", data={"theme": "neon"}).status_code == 422
    client.cookies.set("vf_theme", '"><script>')
    assert "data-theme" not in html_tag(client.get("/").text)


def test_switch_shows_the_current_choice(client):
    page = client.get("/").text
    assert 'action="/theme"' in page
    assert re.search(r'value="system"[^>]*aria-pressed="true"', page)
    client.cookies.set("vf_theme", "dark")
    assert re.search(r'value="dark"[^>]*aria-pressed="true"', client.get("/").text)


def test_dark_colours_are_the_same_for_the_choice_and_the_system():
    chosen = re.search(r':root\[data-theme="dark"\] \{(.*?)\n\}', CSS, re.S)[1]
    system = re.search(
        r"@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme\]\) \{(.*?)\n  \}",
        CSS,
        re.S,
    )[1]
    squash = lambda block: " ".join(block.split())  # noqa: E731
    assert squash(chosen) == squash(system)
