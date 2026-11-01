import re
from pathlib import Path
from types import SimpleNamespace

import docs_hooks

REPO = "https://github.com/romanzdk/verifair/blob/main/"


def test_readme_links_point_into_the_site_or_github():
    text = docs_hooks.readme_page(
        "See [docs/api.md](docs/api.md#push), ![shot](docs/images/review.png), "
        '<img src="docs/images/mobile.png" width="260">, [LICENSE](LICENSE), '
        "[skill](skills/verifair-review/SKILL.md), [top](#quickstart) and [x](https://x.dev)."
    )
    assert "[api.md](api.md#push)" in text
    assert "[![shot](images/review.png)](images/review.png)" in text
    assert '<img src="images/mobile.png" width="260">' in text
    assert f"[LICENSE]({REPO}LICENSE)" in text
    assert f"[skill]({REPO}skills/verifair-review/SKILL.md)" in text
    assert "[top](#quickstart)" in text
    assert "[x](https://x.dev)" in text


def test_readme_wordmark_follows_the_site_theme():
    text = docs_hooks.readme_page(
        '<div align="center">\n\n<h1>\n  <picture>\n'
        '    <source media="(prefers-color-scheme: dark)" srcset="docs/images/wordmark-dark.svg">\n'
        '    <img src="docs/images/wordmark-light.svg" height="64" alt="verifair">\n'
        "  </picture>\n</h1>\n\n**Tagline**\n\n</div>"
    )
    assert '<div align="center" markdown>' in text
    assert '<img src="images/wordmark-light.svg#only-light" height="64" alt="verifair">' in text
    assert '<img src="images/wordmark-dark.svg#only-dark" height="64" alt="verifair">' in text
    assert "<picture>" not in text


def test_doc_links_outside_docs_go_to_github():
    text = docs_hooks.doc_page(
        "[the report](../README.md#the-report), [fixtures](../tests/fixtures/traces/README.md), "
        "[script](examples/trace-report.py), [API](api.md), ![tree](images/execution.png) and "
        '<img src="images/mobile-execution.png" width="260">.'
    )
    assert "[the report](index.md#the-report)" in text
    assert f"[fixtures]({REPO}tests/fixtures/traces/README.md)" in text
    assert f"[script]({REPO}docs/examples/trace-report.py)" in text
    assert "[API](api.md)" in text
    assert '<img src="../images/mobile-execution.png" width="260">' in text
    assert "[![tree](images/execution.png)](images/execution.png)" in text


def test_real_readme_has_no_links_left_outside_the_site():
    text = docs_hooks.readme_page(Path("README.md").read_text())
    assert "docs/" not in text.replace(REPO + "docs/", "")


def test_readme_drops_the_link_to_the_site_itself():
    text = docs_hooks.readme_page(
        "[Docs](https://romanzdk.github.io/verifair/) · [Quickstart](#quickstart)"
    )
    assert text == "[Quickstart](#quickstart)"


def test_every_link_sent_to_github_points_at_a_real_file():
    pages = [docs_hooks.readme_page(Path("README.md").read_text())]
    pages += [docs_hooks.doc_page(p.read_text()) for p in Path("docs").glob("*.md")]
    targets = {m for page in pages for m in re.findall(re.escape(REPO) + r"([^)#\s]+)", page)}
    assert targets
    for target in targets:
        assert Path(target).exists(), target


def test_llms_txt_lists_every_page_as_markdown():
    pages = [("Home", "index.md", "Review the evidence."), ("API", "api.md", "Push a report")]
    text = docs_hooks.llms_txt("https://x.dev/v/", "Review the evidence.", pages)
    assert text.startswith("# verifair\n\n> Review the evidence.\n")
    assert "- [Home](https://x.dev/v/index.md): Review the evidence." in text
    assert "- [API](https://x.dev/v/api.md): Push a report" in text
    assert "(https://x.dev/v/llms-full.txt)" in text
    assert f"({REPO}skills/verifair-review/SKILL.md)" in text


def test_every_docs_page_has_a_summary_in_the_readme():
    summaries = docs_hooks.page_summaries(Path("README.md").read_text())
    assert set(summaries) == {p.name for p in Path("docs").glob("*.md")}
    assert summaries["api.md"].startswith("Push a report")


def test_markdown_copies_keep_root_relative_image_paths():
    text = docs_hooks.doc_links('<img src="images/a.png"> and [x](../README.md#y)')
    assert text == '<img src="images/a.png"> and [x](index.md#y)'


def test_post_build_writes_markdown_copies_and_llms_files(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "site").mkdir()
    (tmp_path / "README.md").write_text(
        '<h1>\n  <picture>\n    <source srcset="docs/images/d.svg">\n'
        '    <img src="docs/images/l.svg" alt="verifair">\n  </picture>\n</h1>\n\n'
        "See [API](docs/api.md) and ![shot](docs/images/s.png).\n\n"
        "| [docs/api.md](docs/api.md) | Push a report |\n"
        "| [docs/report-template.md](docs/report-template.md) | The layout |\n"
    )
    (tmp_path / "docs/api.md").write_text('# API\n\n<img src="images/a.png"> [r](../README.md#y)\n')
    (tmp_path / "docs/report-template.md").write_text("## Intent\n")
    config = SimpleNamespace(
        config_file_path=str(tmp_path / "mkdocs.yml"),
        docs_dir=str(tmp_path / "docs"),
        site_dir=str(tmp_path / "site"),
        site_url="https://x.dev/v/",
        site_description="Review the evidence.",
        nav=[{"Home": "index.md"}, {"Template": "report-template.md"}, {"API": "api.md"}],
    )
    docs_hooks.on_post_build(config)
    site = tmp_path / "site"
    assert (site / "index.md").read_text().startswith("<h1>\n  verifair\n</h1>")
    assert "See [API](api.md) and ![shot](images/s.png)." in (site / "index.md").read_text()
    assert (site / "api.md").read_text() == '# API\n\n<img src="images/a.png"> [r](index.md#y)\n'
    llms = (site / "llms.txt").read_text()
    assert llms.index("[Template]") < llms.index("[API]")
    assert "- [API](https://x.dev/v/api.md): Push a report" in llms
    full = (site / "llms-full.txt").read_text()
    assert "Source: https://x.dev/v/report-template.md\n\n# Template\n\n## Intent" in full
    assert "Source: https://x.dev/v/api.md\n\n# API\n" in full
