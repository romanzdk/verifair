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


def _codex_user(*content):
    return {
        "type": "response_item",
        "payload": {"type": "message", "role": "user", "content": list(content)},
    }


def _codex_event(text, **extra):
    return {"type": "event_msg", "payload": {"type": "user_message", "message": text, **extra}}


def _export_records(tmp_path, records, *args, check=True):
    session = tmp_path / "rollout.jsonl"
    session.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(session), *args],
        capture_output=True,
        text=True,
        check=check,
    )


CODEX_RECORDS = [
    {"type": "session_meta", "payload": {"cli_version": "0.167.0", "cwd": "/private/project"}},
    _codex_user(
        {
            "type": "input_text",
            "text": "# AGENTS.md instructions for /private/project\nprivate rules",
        }
    ),
    _codex_user(
        {"type": "input_text", "text": "<environment_context>private cwd</environment_context>"}
    ),
    _codex_user(
        {"type": "input_text", "text": "Context handoff (full_thread_summary):\nprivate history"}
    ),
    _codex_user(
        {
            "type": "input_text",
            "text": "[Historical user; user_message; thread=x]\nprivate old task",
        }
    ),
    _codex_user(
        {
            "type": "input_text",
            "text": "[Historical assistant; assistant_message; thread=x]\nprivate old reply",
        }
    ),
    {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": "private policy"}],
        },
    },
    {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "private reply"}],
        },
    },
    {
        "type": "response_item",
        "payload": {"type": "function_call_output", "output": "private tool output"},
    },
    _codex_user({"type": "input_text", "text": "Fix the login.\n\n## Intent\nKeep it simple."}),
    _codex_user(
        {"type": "input_text", "text": "<environment_context>private cwd</environment_context>"},
        {"type": "input_text", "text": "Why is this red?"},
        {"type": "input_image", "image_url": "file:///private/screenshot.png"},
    ),
    _codex_user({"type": "input_text", "text": "Try again"}),
    _codex_user({"type": "input_text", "text": "Try again"}),
    {"type": "compacted", "payload": {"message": "private summary"}},
    {"type": "response_item", "payload": None},
]


def test_codex_rollout_exports_prompts_without_injected_context(tmp_path):
    out = _export_records(tmp_path, CODEX_RECORDS).stdout

    assert "> Fix the login.\n>\n> ## Intent\n> Keep it simple." in out
    assert "> Why is this red?\n>\n> [image]" in out
    assert out.count("> Try again") == 2
    assert "private" not in out
    assert section_headings("## Intent\n\nx\n\n" + out) == ["Intent", "Prompts"]
    assert [line for line in out.splitlines() if line.startswith("**")] == [
        "**1.**",
        "**2.**",
        "**3.**",
        "**4.**",
    ]


def test_codex_preview_selection_keeps_original_numbers(tmp_path):
    out = _export_records(tmp_path, CODEX_RECORDS, "--upto", "3", "--drop", "1-2").stdout

    assert out.endswith("**3.**\n\n> Try again\n")
    assert "Fix the login" not in out
    assert "Why is this red" not in out


def test_codex_mirrored_events_do_not_duplicate_prompts(tmp_path):
    records = [
        _codex_user({"type": "input_text", "text": "Try again"}),
        _codex_event("Try again"),
        _codex_user({"type": "input_text", "text": "Try again"}),
        _codex_event("Try again"),
        _codex_event(
            "Look at this", images=["/private/image.png"], local_images=["/private/local.png"]
        ),
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "private reply"}},
    ]
    out = _export_records(tmp_path, records).stdout

    assert out.count("> Try again") == 2
    assert "> Look at this" in out
    assert out.count("> [image]") == 2
    assert "private" not in out


def test_codex_exec_output_without_prompts_fails_closed(tmp_path):
    result = _export_records(
        tmp_path,
        [
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "private reply"}},
        ],
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "no prompts found" in result.stderr
    assert "rollout-*.jsonl" in result.stderr


def test_codex_events_skip_injected_context_and_malformed_attachments(tmp_path):
    out = _export_records(
        tmp_path,
        [
            _codex_event("# AGENTS.md instructions\nprivate rules"),
            _codex_event("<environment_context>private cwd</environment_context>"),
            _codex_event("[Historical user; user_message; thread=x]\nprivate old task"),
            _codex_event("Keep this", images=123, local_images="/private/image.png"),
            _codex_event(None),
        ],
    ).stdout

    assert "private" not in out
    assert "[image]" not in out
    assert out.endswith("**1.**\n\n> Keep this\n")


def test_codex_mixed_rollout_preserves_unmirrored_prompts(tmp_path):
    records = [
        _codex_user({"type": "input_text", "text": "Earlier real prompt"}),
        _codex_user({"type": "input_text", "text": "Current real prompt"}),
        _codex_event("Current real prompt"),
        _codex_user({"type": "input_text", "text": "Last real prompt"}),
        _codex_event("Try again"),
        _codex_user({"type": "input_text", "text": "Try again"}),
        _codex_event("Try again"),
        _codex_user({"type": "input_text", "text": "Try again"}),
    ]
    out = _export_records(tmp_path, records).stdout

    assert [line for line in out.splitlines() if line.startswith("> ")] == [
        "> Earlier real prompt",
        "> Current real prompt",
        "> Last real prompt",
        "> Try again",
        "> Try again",
    ]


def test_codex_prompts_can_be_uploaded_and_read_in_a_report(tmp_path, client):
    prompts = _export_records(tmp_path, CODEX_RECORDS, "--upto", "2", "--drop", "1").stdout
    body = "## Intent\n\nFix the login.\n\n" + prompts
    headers = {"Authorization": "Bearer t"}
    response = client.post(
        "/api/reports",
        headers=headers,
        json={
            "id": "codex-prompts",
            "project": "login",
            "title": "Login",
            "risk": "low",
            "body": body,
        },
    )

    assert response.status_code == 201
    snapshot = client.get("/api/reports/codex-prompts/versions/1", headers=headers)
    assert snapshot.json()["body"] == body
    page = client.get("/r/codex-prompts")
    assert page.status_code == 200
    assert "Why is this red?" in page.text
    assert "<blockquote>" in page.text
    assert "[image]" in page.text
    assert "private screenshot" not in page.text


def test_codex_repeated_prompt_after_turn_boundary_is_not_a_mirror(tmp_path):
    for boundary in [
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {"type": "turn_context", "payload": {}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant"}},
    ]:
        out = _export_records(
            tmp_path,
            [
                _codex_event("Again"),
                boundary,
                _codex_user({"type": "input_text", "text": "Again"}),
            ],
        ).stdout
        assert out.count("> Again") == 2


def test_codex_skill_bodies_are_not_prompts(tmp_path):
    skill = "<skill>\n<name>code-review</name>\n<path>/private/SKILL.md</path>\nprivate\n</skill>"
    records = [
        _codex_user({"type": "input_text", "text": skill}),
        _codex_user({"type": "input_text", "text": "Review it."}),
    ]
    out = _export_records(tmp_path, records).stdout

    assert "> Review it." in out
    assert "private" not in out


def test_codex_subagent_rollout_is_refused(tmp_path):
    # its prompts are another agent's brief, not what the author typed
    records = [
        {"type": "session_meta", "payload": {"source": {"subagent": {"thread_spawn": {}}}}},
        _codex_user({"type": "input_text", "text": "private brief"}),
    ]
    result = _export_records(tmp_path, records, check=False)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "subagent" in result.stderr


def test_deeply_nested_line_is_skipped(tmp_path):
    session = tmp_path / "rollout.jsonl"
    record = _codex_user({"type": "input_text", "text": "Still here."})
    session.write_text("[" * 100_000 + "\n" + json.dumps(record) + "\n")
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(session)], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    assert "> Still here." in result.stdout


def _opencode_export(tmp_path, messages, check=True, **info):
    export = {"info": {"id": "ses_test", "title": "t", **info}, "messages": messages}
    session = tmp_path / "session.json"
    session.write_text(json.dumps(export, indent=2))
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(session)],
        capture_output=True,
        text=True,
        check=check,
    )


def _oc(kind, n, **fields):
    return {"id": f"msg_{n}", "type": kind, "time": {"created": n}, **fields}


OPENCODE_MESSAGES = [
    _oc("agent-switched", 1, agent="build"),
    _oc(
        "user",
        2,
        text="Why is checkout slow?\n\n## Intent\nfind out",
        files=[
            {"data": "cHJpdmF0ZSBzY3JlZW4=", "mime": "image/png", "source": {"type": "inline"}},
            {
                "data": "cHJpdmF0ZSBjb2Rl",
                "mime": "text/plain",
                "name": "private.py",
                "source": {"type": "uri", "uri": "file:///private.py"},
            },
        ],
    ),
    _oc("synthetic", 3, text="private injected context"),
    _oc("system", 4, text="private system update"),
    _oc("skill", 5, skill="s", name="review", text="private skill body"),
    _oc("shell", 6, shellID="sh_1", command="cat private.env"),
    _oc("assistant", 7, content=[{"type": "text", "text": "private reply"}]),
    _oc("user", 8, text="<system-reminder>private</system-reminder>Add an index."),
    _oc("compaction", 9, summary="private summary"),
    _oc("user", 10, text="   "),
    _oc("idle", 11),
]


def test_opencode_export_keeps_only_typed_prompts(tmp_path):
    out = _opencode_export(tmp_path, OPENCODE_MESSAGES).stdout

    assert "private" not in out
    assert "cHJp" not in out
    assert [line for line in out.splitlines() if line.startswith(">")] == [
        "> Why is checkout slow?",
        ">",
        "> ## Intent",
        "> find out",
        ">",
        "> [image]",
        ">",
        "> [attachment]",
        "> Add an index.",
    ]
    assert "**1.**" in out and "**2.**" in out and "**3.**" not in out


def test_opencode_child_session_is_refused(tmp_path):
    result = _opencode_export(tmp_path, OPENCODE_MESSAGES, check=False, parentID="ses_parent")

    assert result.returncode == 2
    assert result.stdout == ""
    assert "subagent" in result.stderr


def test_opencode_v1_export_fails_with_a_hint(tmp_path):
    v1 = [{"info": {"id": "msg_1", "role": "user"}, "parts": [{"type": "text", "text": "Hi"}]}]
    result = _opencode_export(tmp_path, v1, check=False)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "opencode session export" in result.stderr
    assert "Traceback" not in result.stderr


def test_opencode_empty_parent_id_is_still_a_child_session(tmp_path):
    result = _opencode_export(tmp_path, OPENCODE_MESSAGES, check=False, parentID="")

    assert result.returncode == 2
    assert "subagent" in result.stderr


def test_compact_one_line_opencode_export_is_read(tmp_path):
    export = {"info": {"id": "ses_x"}, "messages": [_oc("user", 1, text="One line.")]}
    session = tmp_path / "session.json"
    session.write_text(json.dumps(export))
    out = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(session)], capture_output=True, text=True
    ).stdout

    assert out.endswith("**1.**\n\n> One line.\n")


def test_line_separators_inside_a_prompt_do_not_split_the_record(tmp_path):
    session = tmp_path / "session.jsonl"
    records = [_user("first second"), _user("Next prompt.")]
    session.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    out = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(session)], capture_output=True, text=True
    ).stdout

    assert "> first\n> second\n" in out and "> Next prompt." in out
