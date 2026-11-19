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
            {"type": "document", "source": {}},
            "not a block",
            {"type": "text"},
        ]
    ),
    _user("<system-reminder>\nnoise\n</system-reminder>use `uv` not pip"),
    _user(
        "Context handoff (full_thread_summary):\nold stuff\n\nUser message:\nship it\n> with care"
    ),
    _user("Context handoff (full_thread_summary):\nsummary written by the agent"),
    _user("Delegated task node:delegated-task:x (fix it) [completed]:\nagent result"),
    _user(
        'see this [Attached image "a.png" is saved at: /Users/me/a.png]\n\n'
        '<t3_context version="1">\n<context kind="image" id="i1">\nattachmentId: secret-id\n'
        "</context>\n</t3_context>"
    ),
    _user(
        "[Assistant quote](t3-citation://v1/a/b/c?text=%22Has+to+answer%22%3A+only%0A%0A"
        "blocking&start=1&end=2&comment=adjust) [Assistant quote](t3-citation://v1/a?text=x)"
    ),
]


def _run(tmp_path, *args, check=True):
    session = tmp_path / "session.jsonl"
    lines = [json.dumps(r) for r in RECORDS]
    raw = "\n".join(lines[:3] + ["{not json"] + lines[3:]).encode() + b"\n\xff\xfe broken\n"
    session.write_bytes(raw)
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(session), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def test_exports_only_what_the_person_typed(tmp_path):
    out = _run(tmp_path).stdout

    assert out.startswith("## Prompts\n")
    assert "> Add a dark mode toggle." in out
    assert "> /implement fix the login bug" in out
    assert '> see this [Attached image "a.png"]' in out
    assert "> why is this red?\n>\n> [image]\n>\n> [attachment]" in out
    assert "> use `uv` not pip" in out
    assert "> ship it\n> > with care" in out
    assert (
        '> On the agent\'s text ""Has to answer": only blocking": adjust On the agent\'s text "x"'
        in out
    )
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
        "summary written by the agent",
        "agent result",
        "/Users/me",
        "secret-id",
        "t3-citation",
    ):
        assert noise not in out
    assert [line for line in out.splitlines() if line.startswith("**")] == [
        f"**{n}.**" for n in range(1, 8)
    ]


def test_prompt_text_cannot_start_a_report_section(tmp_path):
    body = "## Intent\n\nx\n\n" + _run(tmp_path).stdout

    assert section_headings(body) == ["Intent", "Prompts"]
    prompts = split_sections(body)[-1]
    assert "Add a dark mode toggle. Intent keep it small" in " ".join(prompts.text.split())


def test_drop_keeps_the_original_numbers(tmp_path):
    out = _run(tmp_path, "--drop", "1,4").stdout

    assert "dark mode" not in out
    assert "uv" not in out
    assert [line for line in out.splitlines() if line.startswith("**")] == [
        "**2.**",
        "**3.**",
        "**5.**",
        "**6.**",
        "**7.**",
    ]


def test_drop_takes_ranges_and_upto_ignores_later_prompts(tmp_path):
    out = _run(tmp_path, "--upto", "4", "--drop", "1-2").stdout

    assert [line for line in out.splitlines() if line.startswith("**")] == ["**3.**", "**4.**"]
    assert "ship it" not in out


def test_unknown_drop_number_fails_instead_of_publishing(tmp_path):
    for bad in ("8", "0", "a,b", "3-1"):
        result = _run(tmp_path, "--drop", bad, check=False)

        assert result.returncode == 2, bad
        assert result.stdout == ""


def test_no_prompts_or_no_file_fails_with_a_message(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text(json.dumps(RECORDS[0]) + "\n")
    for path in (empty, tmp_path / "missing.jsonl"):
        result = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), str(path)], capture_output=True, text=True
        )

        assert result.returncode == 2
        assert result.stdout == ""
        assert "Traceback" not in result.stderr
