import json
from pathlib import Path

import pytest

from verifair.trace_import import main
from verifair.trace_importers.common import ImportOptions, TraceImportError
from verifair.traces import canonical_json

FIXTURE = Path(__file__).parent / "fixtures/traces/codex/native.jsonl"


def convert(paths, **options):
    from verifair.trace_importers.codex import convert as run

    return run(tuple(paths), ImportOptions(**{"source_version": "0.163.0", **options}))


def capture(tmp_path, records, name="capture.jsonl"):
    path = tmp_path / name
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def item(kind, id="item_0", **fields):
    return {"type": "item.completed", "item": {"type": kind, "id": id, **fields}}


def test_cli_dispatch_reads_native_codex(tmp_path):
    output = tmp_path / "trace.json"
    assert (
        main(
            [
                "--format",
                "codex-exec-jsonl",
                "--input",
                str(FIXTURE),
                "--output",
                str(output),
                "--source-version",
                "0.163.0",
            ]
        )
        == 0
    )
    assert json.loads(output.read_text())["runs"][0]["agent"] == "codex"


def test_native_revisions_keep_one_call_and_only_observed_telemetry():
    doc = convert([FIXTURE])
    commands = [e for e in doc.events if e.title == "Command execution"]
    assert len(commands) == 1
    assert (commands[0].status, commands[0].exit_code) == ("failed", 2)
    assert "item_0" in commands[0].source_ref
    assert len([e for e in doc.events if e.kind == "assistant"]) == 2
    assert all(e.model is None and e.started_at is None and e.ended_at is None for e in doc.events)
    assert all(e.input is None and e.output is None and e.explanation is None for e in doc.events)
    assert len([e for e in doc.events if "Turn" in e.title]) == 1


def test_opt_in_details_redact_before_model_construction():
    doc = convert([FIXTURE], include_details=True)
    raw = canonical_json(doc)
    for secret in [
        "fixture-secret",
        "fixture-key",
        "fixture-password",
        "IMAGE-NEVER",
        "STRUCTURED-NEVER",
        "ENCRYPTED-NEVER",
        "RAW-NEVER",
        "never-copy",
    ]:
        assert secret not in raw
    assert "pytest -q" in raw and "[REDACTED]" in raw
    assert any(e.title == "Provider reasoning summary" and e.explanation for e in doc.events)
    assert any(
        e.title == "MCP tool: synthetic.lookup" and e.status == "completed" for e in doc.events
    )


def test_scoped_ids_retries_duplicate_delivery_and_global_selection(tmp_path):
    records = [
        {"type": "thread.started", "thread_id": "thread-same"},
        item(
            "command_execution",
            command="echo one",
            aggregated_output="one",
            exit_code=0,
            status="completed",
        ),
    ]
    a = capture(tmp_path, records + [records[-1]], "a.jsonl")
    b = capture(
        tmp_path,
        records
        + [
            item(
                "command_execution",
                id="item_retry",
                command="echo two",
                aggregated_output="two",
                exit_code=1,
                status="failed",
            )
        ],
        "b.jsonl",
    )
    doc = convert([a, b])
    assert len(doc.runs) == 2
    assert len([e for e in doc.events if e.kind == "tool"]) == 3
    assert len({e.id for e in doc.events}) == len(doc.events)
    selected = convert([a, b], last_events=1)
    assert len(selected.events) == len(selected.runs) == 1
    assert selected.events[0].status == "failed"
    assert any("selection" in w.lower() for w in selected.runs[0].warnings)


def test_incomplete_declined_orphan_and_unknown_records(tmp_path):
    path = capture(
        tmp_path,
        [
            {"type": "thread.started", "thread_id": "thread-main"},
            {
                "type": "item.started",
                "item": {
                    "type": "command_execution",
                    "id": "unfinished",
                    "command": "echo test passed",
                    "aggregated_output": "",
                    "exit_code": None,
                    "status": "in_progress",
                },
            },
            item(
                "command_execution",
                id="declined",
                command="true",
                aggregated_output="",
                exit_code=None,
                status="declined",
            ),
            item(
                "mcp_tool_call",
                id="orphan",
                server="s",
                tool="t",
                arguments={},
                result=None,
                error={"message": "secret=never-emit"},
                status="failed",
            ),
            item("future_kind", id="unknown", text="PAYLOAD-NEVER"),
            {"type": "future_record", "text": "PAYLOAD-NEVER"},
        ],
    )
    doc = convert([path])
    tools = [e for e in doc.events if e.kind == "tool"]
    assert [e.status for e in tools] == ["running", "cancelled", "failed"]
    assert all(e.exit_code is None for e in tools)
    warnings = " ".join(doc.runs[0].warnings).lower()
    assert "completion" in warnings and "unknown" in warnings
    assert "PAYLOAD-NEVER" not in canonical_json(doc)


def test_explicit_collaboration_child_link_and_missing_child_warning(tmp_path):
    parent = capture(
        tmp_path,
        [
            {"type": "thread.started", "thread_id": "parent"},
            item(
                "collab_tool_call",
                tool="spawn_agent",
                sender_thread_id="parent",
                receiver_thread_ids=["child"],
                prompt="SECRET-PROMPT",
                agents_states={},
                status="completed",
            ),
        ],
        "parent.jsonl",
    )
    child = capture(
        tmp_path,
        [
            {"type": "thread.started", "thread_id": "child"},
            item("agent_message", text="child statement"),
        ],
        "child.jsonl",
    )
    doc = convert([parent, child])
    assert doc.runs[1].parent_run_id == doc.runs[0].id
    assert doc.runs[1].parent_event_id in {e.id for e in doc.events if e.kind == "tool"}
    assert any("child" in w.lower() for w in convert([parent]).runs[0].warnings)
    assert "SECRET-PROMPT" not in canonical_json(doc)


def test_ambiguous_child_link_does_not_choose_an_invocation(tmp_path):
    def spawn(id):
        return item(
            "collab_tool_call",
            id=id,
            tool="spawn_agent",
            sender_thread_id="parent",
            receiver_thread_ids=["child"],
            prompt=None,
            agents_states={},
            status="completed",
        )

    parent = capture(
        tmp_path,
        [{"type": "thread.started", "thread_id": "parent"}, spawn("first"), spawn("second")],
        "parent.jsonl",
    )
    child = capture(
        tmp_path,
        [{"type": "thread.started", "thread_id": "child"}, item("agent_message", text="child")],
        "child.jsonl",
    )
    doc = convert([parent, child])
    assert doc.runs[1].parent_run_id == doc.runs[0].id
    assert doc.runs[1].parent_event_id is None
    assert any("ambiguous" in w.lower() for w in doc.runs[1].warnings)


def test_errors_are_events_without_claiming_success(tmp_path):
    path = capture(
        tmp_path,
        [
            {"type": "thread.started", "thread_id": "t"},
            {"type": "turn.started"},
            {"type": "error", "message": "password=secret"},
            item("error", message="token=secret"),
            {"type": "turn.failed", "error": {"message": "failed"}},
        ],
    )
    doc = convert([path], include_details=True)
    assert len([e for e in doc.events if e.status == "failed"]) == 3
    assert "secret" not in canonical_json(doc)


@pytest.mark.parametrize(
    "records",
    [
        [{"type": "session_meta", "payload": {}}],
        [{"method": "thread/read", "result": {}}],
        [{"type": "item.completed", "item": {"type": "agentMessage", "id": "i", "text": "x"}}],
    ],
)
def test_rejects_other_format_families(tmp_path, records):
    with pytest.raises(TraceImportError):
        convert([capture(tmp_path, records)])


def test_versions_missing_untested_and_unsupported_major():
    assert any(
        "version" in w.lower() for w in convert([FIXTURE], source_version=None).runs[0].warnings
    )
    assert any(
        "untested" in w.lower()
        for w in convert([FIXTURE], source_version="0.164.0").runs[0].warnings
    )
    with pytest.raises(TraceImportError):
        convert([FIXTURE], source_version="1.0.0")


def test_shared_read_budget_enforces_combined_actual_bytes(tmp_path, monkeypatch):
    from verifair.trace_importers import common

    records = [{"type": "thread.started", "thread_id": "t"}, item("agent_message", text="x")]
    a = capture(tmp_path, records, "a.jsonl")
    b = capture(tmp_path, records, "b.jsonl")
    monkeypatch.setattr(common, "MAX_INPUT_BYTES", a.stat().st_size + 1)
    with pytest.raises(TraceImportError, match="combined byte limit"):
        convert([a, b])


def test_details_are_bounded_and_metadata_cannot_carry_payload(tmp_path):
    path = capture(
        tmp_path,
        [
            {"type": "thread.started", "thread_id": "t"},
            item("agent_message", text="x" * 18000, metadata={"secret": "DROP-ME"}),
            item(
                "mcp_tool_call",
                id="m",
                server="secret=DROP-ME",
                tool="/private/DROP-ME",
                arguments={"env": "DROP-ME"},
                result=None,
                error=None,
                status="completed",
            ),
        ],
    )
    doc = convert([path], include_details=True)
    assert any(e.truncated and len(e.output) == 16000 for e in doc.events if e.output)
    assert "DROP-ME" not in canonical_json(doc)


def test_other_emitter_variants_preserve_status_without_claiming_todo_checks(tmp_path):
    path = capture(
        tmp_path,
        [
            {"type": "thread.started", "thread_id": "t"},
            item(
                "file_change",
                id="files",
                changes=[{"path": "/synthetic/source.py", "kind": "update"}],
                status="completed",
            ),
            item(
                "web_search",
                id="web",
                query="synthetic query",
                action={"type": "search", "query": "synthetic query"},
                results=[{"env": "DROP-ME"}],
            ),
            item(
                "todo_list", id="todo", items=[{"text": "tests passed DROP-ME", "completed": True}]
            ),
        ],
    )
    doc = convert([path], include_details=True)
    assert [(e.title, e.kind, e.status) for e in doc.events][1:] == [
        ("File changes", "tool", "completed"),
        ("Web search", "tool", "completed"),
        ("Agent todo list", "checkpoint", "completed"),
    ]
    assert "/synthetic/source.py" in canonical_json(doc)
    assert "synthetic query" in canonical_json(doc)
    assert "DROP-ME" not in canonical_json(doc)


def test_selection_applies_before_document_count_validation(tmp_path):
    path = capture(
        tmp_path,
        [
            {"type": "thread.started", "thread_id": "t"},
            *[item("agent_message", id=f"item_{i}", text="ok") for i in range(1001)],
        ],
    )
    doc = convert([path], last_events=1)
    assert len(doc.events) == 1 and doc.events[0].ordinal == 1002


@pytest.mark.parametrize(
    "record",
    [
        {"type": []},
        {"type": "item.completed", "item": {"id": "x", "type": []}},
        {"type": "item.completed", "item": {"id": "x", "type": "command_execution", "status": {}}},
    ],
)
def test_malformed_native_discriminators_fail_safely(tmp_path, record):
    path = capture(tmp_path, [{"type": "thread.started", "thread_id": "t"}, record])
    with pytest.raises(TraceImportError) as error:
        convert([path])
    assert str(path) not in str(error.value)


def test_declined_label_is_retained_without_details(tmp_path):
    path = capture(
        tmp_path,
        [
            item(
                "command_execution",
                command="secret-command",
                aggregated_output="",
                exit_code=None,
                status="declined",
            )
        ],
    )
    event = convert([path]).events[0]
    assert event.status == "cancelled" and "declined by source" in event.title.lower()
    assert "secret-command" not in canonical_json(convert([path]))


def test_credential_assignments_are_not_valid_metadata_identifiers(tmp_path):
    path = capture(
        tmp_path,
        [
            item(
                "mcp_tool_call",
                server="token:fixture-secret",
                tool="authorization:fixture-secret",
                arguments={},
                result=None,
                error=None,
                status="completed",
            )
        ],
    )
    assert "fixture-secret" not in canonical_json(convert([path]))
