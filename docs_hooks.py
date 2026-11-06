"""MkDocs hooks: README.md is the home page, links to files outside docs/ go to GitHub, and
the site gets llms.txt with a Markdown copy of every page."""

import re
from pathlib import Path

REPO = "https://github.com/romanzdk/verifair/blob/main/"
SITE = "https://romanzdk.github.io/verifair/"
PICTURE = re.compile(
    r'<picture>\s*<source[^>]*srcset="([^"]+)">\s*<img src="([^"]+)"([^>]*)>\s*</picture>'
)
OUTSIDE_DOCS = re.compile(r"\]\((?!docs/|https?:|#|mailto:)([^)\s]+)\)")
IMAGE = re.compile(r"(?<!\[)!\[([^\]]*)\]\((images/[^)\s]+)\)")


def link_images(text: str) -> str:
    # Screenshots shrink to text width, tables and phones; GitHub links them too
    return IMAGE.sub(r"[![\1](\2)](\2)", text)


def readme_links(text: str) -> str:
    text = OUTSIDE_DOCS.sub(rf"]({REPO}\1)", text)
    return re.sub(r'(\[|\]\(|src=")docs/', r"\1", text)


def readme_page(text: str) -> str:
    # GitHub picks the wordmark from the OS theme; the site has its own light/dark toggle
    text = PICTURE.sub(r'<img src="\2#only-light"\3> <img src="\1#only-dark"\3>', text)
    text = text.replace('<div align="center">', '<div align="center" markdown>')
    text = text.replace(f"[Docs]({SITE}) · ", "")
    return link_images(readme_links(text))


def readme_markdown(text: str) -> str:
    """The README for agents: links fixed, no site-only markup."""
    return readme_links(PICTURE.sub("verifair", text))


def doc_links(text: str) -> str:
    text = text.replace("](../README.md", "](index.md")
    text = re.sub(r"\]\(\.\./([^)\s]+)\)", rf"]({REPO}\1)", text)
    return re.sub(r"\]\((examples/[^)\s]+)\)", rf"]({REPO}docs/\1)", text)


def doc_page(text: str) -> str:
    # MkDocs leaves raw HTML alone, and each docs page is served one folder deep
    return link_images(doc_links(text).replace('src="images/', 'src="../images/'))


def page_summaries(readme: str) -> dict[str, str]:
    """The one-line descriptions from the README's Documentation table."""
    return dict(re.findall(r"^\| \[docs/([\w-]+\.md)\]\([^)]+\) \| (.+?) \|$", readme, re.M))


def llms_txt(site_url: str, description: str, pages: list[tuple[str, str, str]]) -> str:
    lines = [f"# verifair\n\n> {description}\n"]
    lines.append(f"Every page in one file: [llms-full.txt]({site_url}llms-full.txt)\n\n## Docs\n")
    for title, path, summary in pages:
        lines.append(f"- [{title}]({site_url}{path}): {summary}")
    lines.append("\n## Optional\n")
    lines.append(
        f"- [Agent skill]({REPO}skills/verifair-review/SKILL.md): "
        "how an agent submits a report, waits for feedback and answers it"
    )
    return "\n".join(lines) + "\n"


def on_files(files, config):
    from mkdocs.structure.files import File

    root = Path(config.config_file_path).parent
    readme = (root / "README.md").read_text()
    files.append(File.generated(config, "index.md", content=readme_page(readme)))
    changelog = (root / "CHANGELOG.md").read_text()
    files.append(File.generated(config, "changelog.md", content=changelog))
    return files


def on_page_markdown(markdown, page, config, files):
    if page.file.generated_by:
        source = "CHANGELOG.md" if page.file.src_uri == "changelog.md" else "README.md"
        page.edit_url = REPO.replace("/blob/", "/edit/") + source
        return markdown
    return doc_page(markdown)


def on_post_build(config):
    # Reads the sources itself: dirty rebuilds skip the page hooks for unchanged pages
    site = Path(config.site_dir)
    root = Path(config.config_file_path).parent
    readme = (root / "README.md").read_text()
    summaries = page_summaries(readme) | {
        "index.md": config.site_description,
        "changelog.md": "What changed in each version",
    }
    pages, full = [], []
    for item in config.nav:
        ((title, path),) = item.items()
        if path == "index.md":
            text = readme_markdown(readme)
        elif path == "changelog.md":
            text = (root / "CHANGELOG.md").read_text()
        else:
            text = doc_links((Path(config.docs_dir) / path).read_text())
        (site / path).write_text(text)
        heading = "" if text.startswith("# ") else f"# {title}\n\n"
        full.append(f"Source: {config.site_url}{path}\n\n{heading}{text}")
        pages.append((title, path, summaries[path]))
    (site / "llms-full.txt").write_text("\n\n".join(full))
    (site / "llms.txt").write_text(llms_txt(config.site_url, config.site_description, pages))
