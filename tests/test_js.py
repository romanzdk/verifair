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
        "error_copy",
        "triple_click_clamp",
        "name_error_opens_closed_drawer",
        "plan_general_cancel",
        "verdict_draft_survives_gate_refresh",
        "resolved_comment_deep_link",
        "load_reveals_fragment_and_focuses_autofocus",
        "execution_fragment_reveals_all_ancestors",
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
