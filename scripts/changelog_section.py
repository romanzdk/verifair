"""Print one version's section of CHANGELOG.md, without its heading: changelog_section.py 0.3.0"""

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).parents[1] / "CHANGELOG.md"


def section(version: str, text: str | None = None) -> str:
    text = CHANGELOG.read_text() if text is None else text
    found = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|^\[[^\]]+\]: |\Z)", text, re.M | re.S
    )
    return found[1].strip() if found else ""


if __name__ == "__main__":
    print(section(sys.argv[1]))
