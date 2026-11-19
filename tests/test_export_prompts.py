import json
import subprocess
import sys
from pathlib import Path

from verifair.render import section_headings, split_sections

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "verifair-review" / "export-prompts.py"


def _user(content, **extra):
    return {"type": "user", "message": {"role": "user", "content": content}, **extra}


RECORDS = [
    {"type": "system", "content": "start"},
    _user("Add a dark mode toggle.\n\n## Intent\nkeep it small"),
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}},
    _user([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}], toolUseResult={}),
    _user("Base directory for this skill: /x", isMeta=True),
    _user("summary of earlier work", isCompactSummary=True),
    _user("sidechain prompt", isSidechain=True),
    _user("<task-notification>\n<task-id>a1</task-id>\n</task-notification>"),
    _user("<local-command-stdout>Compacted</local-command-stdout>"),
    _user("<command-name>/compact</command-name>\n<command-args></command-args>"),
    _user(
        "<command-message>implement</command-message>\n<command-name>/implement</command-name>\n"
        "<command-args>fix the login bug</command-args>"
    ),
    _user([{"type": "text", "text": "[Request interrupted by user]"}]),
    _user(
        [
            {"type": "text", "text": "why is this red?"},
            {"type": "image", "source": {}},
        ]
    ),
    _user("<system-reminder>\nnoise\n</system-reminder>use `uv` not pip"),
    _user(
        "Context handoff (full_thread_summary):\nold stuff\n\nUser message:\nship it\n> with care"
    ),
    _user(
        "[Assistant quote](t3-citation://v1/a/b/c?text=%22Has+to+answer%22%3A+only%0A%0A"
        "blocking&start=1&end=2&comment=adjust) [Assistant quote](t3-citation://v1/a?text=x)"
    ),
]


def _run(tmp_path, *args):
    session = tmp_path / "session.jsonl"
    lines = [json.dumps(r) for r in RECORDS]
    session.write_text("\n".join(lines[:3] + ["{not json"] + lines[3:]) + "\n")
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(session), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_exports_only_what_the_person_typed(tmp_path):
    out = _run(tmp_path)

    assert out.startswith("## Prompts\n")
    assert "> Add a dark mode toggle." in out
    assert "> /implement fix the login bug" in out
    assert "> why is this red?\n>\n> [image]" in out
    assert "> use `uv` not pip" in out
    assert "> ship it\n> > with care" in out
    assert '> On ""Has to answer": only blocking": adjust On "x"' in out
    for noise in (
        "Done.",
        "tool_result",
        "Base directory",
        "summary of earlier work",
        "sidechain",
        "task-notification",
        "Compacted",
        "/compact",
        "interrupted",
        "noise",
        "old stuff",
        "t3-citation",
    ):
        assert noise not in out
    assert [line for line in out.splitlines() if line.startswith("**")] == [
        f"**{n}.**" for n in range(1, 7)
    ]


def test_prompt_text_cannot_start_a_report_section(tmp_path):
    body = "## Intent\n\nx\n\n" + _run(tmp_path)

    assert section_headings(body) == ["Intent", "Prompts"]
    prompts = split_sections(body)[-1]
    assert "Add a dark mode toggle. Intent keep it small" in " ".join(prompts.text.split())


def test_drop_keeps_the_original_numbers(tmp_path):
    out = _run(tmp_path, "--drop", "1,4")

    assert "dark mode" not in out
    assert "uv" not in out
    assert [line for line in out.splitlines() if line.startswith("**")] == [
        "**2.**",
        "**3.**",
        "**5.**",
        "**6.**",
    ]
