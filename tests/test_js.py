import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "case",
    [
        "unicode_before_duplicate",
        "unicode_inside_quote",
        "unicode_across_nodes",
        "unicode_squashed_match",
        "unicode_context",
        "unicode_context_boundary",
    ],
)
def test_actual_javascript_anchor_ranges(case):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise the browser anchor helpers")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("anchor_probe.cjs")), case],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
