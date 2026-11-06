import importlib.metadata
import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("changelog", ROOT / "scripts/changelog_section.py")
changelog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(changelog)
TEXT = (ROOT / "CHANGELOG.md").read_text()


def test_the_current_version_has_a_changelog_section_and_a_compare_link():
    version = importlib.metadata.version("verifair")
    assert changelog.section(version), f"CHANGELOG.md has no section for {version}"
    assert f"\n[{version}]: https://github.com/romanzdk/verifair/" in TEXT
    assert "## [Unreleased]" in TEXT


def test_a_section_stops_at_the_next_version_and_at_the_links():
    text = "## [1.1.0] - d\n\n### Added\n\n- b\n\n## [1.0.0] - d\n\n- a\n\n[1.0.0]: x\n"
    assert changelog.section("1.1.0", text) == "### Added\n\n- b"
    assert changelog.section("1.0.0", text) == "- a"
    assert changelog.section("2.0.0", text) == ""
