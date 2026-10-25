import importlib
import json
from pathlib import Path

import pytest

from verifair.trace_importers.common import ImportOptions, TraceImportError
from verifair.traces import canonical_json

FIXTURE = Path(__file__).parent / "fixtures/traces/pi/session-v3.jsonl"


def records():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines()]


def convert(tmp_path, entries=None, **options):
    source = tmp_path / "private-input-name.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in (entries or records())))
    return importlib.import_module("verifair.trace_importers.pi").convert(
        (source,), ImportOptions(source_version="1.4.0", **options)
    )


def test_selects_only_explicit_ancestry_and_joins_distinct_retries(tmp_path):
    doc = convert(tmp_path, leaf="a3")
    tools = [event for event in doc.events if event.kind == "tool"]
    assert [(event.source_ref, event.title, event.status) for event in tools] == [
        ("toolCall:call_1", "Tool: bash", "failed"),
        ("toolCall:call_2", "Tool: bash", "completed"),
        ("toolCall:call_3", "Tool: subagent", "running"),
    ]
    owners = {event.id: event for event in doc.events}
    assert [owners[event.parent_id].source_ref for event in tools] == [
        "entry:a1",
        "entry:a2",
        "entry:a3",
    ]
    assert all(event.parent_id is None for event in doc.events if event.kind != "tool")
    assert len(doc.runs) == 1
    assert doc.runs[0].parent_run_id is None
    assert doc.runs[0].coverage == "partial"
    assert all(event.source_ref != "entry:alternate" for event in doc.events)
    assert [event.model for event in doc.events if event.kind == "assistant"] == [
        "model-one",
        "model-one",
        "model-two",
    ]
    assert doc.events[-2].status == "cancelled"
    assert all(event.started_at is None and event.ended_at is None for event in doc.events)
    assert all(event.exit_code is None for event in tools)
    warnings = " ".join(doc.runs[0].warnings).lower()
    for word in (
        "branch",
        "fork",
        "summary",
        "compaction",
        "context",
        "extension",
        "unknown",
        "result",
    ):
        assert word in warnings


def test_details_default_to_excluded_and_opt_in_is_allowlisted_and_redacted(tmp_path):
    plain = canonical_json(convert(tmp_path, leaf="a3"))
    assert "synthetic-public-text" not in plain
    assert "synthetic-command" not in plain
    detailed = convert(tmp_path, leaf="a3", include_details=True)
    text = canonical_json(detailed)
    assert "synthetic-public-text" in text
    assert "synthetic-command" in text
    assert "[REDACTED]" in text
    for sentinel in (
        "synthetic-password",
        "synthetic-token",
        "synthetic-hidden",
        "synthetic-image",
        "synthetic-env",
        "synthetic-metadata",
        "private-input-name",
        "synthetic-parent-path",
        "synthetic-alternate",
        "synthetic-signature",
        "synthetic-error-message",
    ):
        assert sentinel not in plain + text


def test_requires_leaf_only_for_ambiguous_tree(tmp_path):
    with pytest.raises(TraceImportError, match="leaf"):
        convert(tmp_path)
    doc = convert(tmp_path, [row for row in records() if row.get("id") != "alternate"])
    assert any(event.source_ref == "toolCall:call_3" for event in doc.events)


@pytest.mark.parametrize("leaf", ["missing", "u"])
def test_explicit_selection_must_name_a_leaf(tmp_path, leaf):
    with pytest.raises(TraceImportError, match="leaf"):
        convert(tmp_path, leaf=leaf)


@pytest.mark.parametrize(
    "damage", ["duplicate", "missing", "cycle", "unselected-cycle", "bad-parent"]
)
def test_validates_entire_graph_before_selecting_ancestry(tmp_path, damage):
    rows = records()
    if damage == "duplicate":
        rows.append(rows[-1].copy())
    elif damage == "missing":
        next(row for row in rows if row.get("id") == "a3")["parentId"] = "missing"
    elif damage == "cycle":
        next(row for row in rows if row.get("id") == "u")["parentId"] = "a3"
    elif damage == "unselected-cycle":
        rows.append({"type": "custom", "id": "bad", "parentId": "bad"})
    else:
        rows[-1]["parentId"] = {}
    with pytest.raises(TraceImportError):
        convert(tmp_path, rows, leaf="a3")


@pytest.mark.parametrize("version", [1, 2, 4, "3", True])
def test_rejects_unsupported_header_schema(tmp_path, version):
    rows = records()
    rows[0]["version"] = version
    with pytest.raises(TraceImportError, match="version"):
        convert(tmp_path, rows, leaf="a3")


def test_absent_header_version_warns_for_otherwise_recognized_v3_shape(tmp_path):
    rows = records()
    del rows[0]["version"]
    doc = convert(tmp_path, rows, leaf="a3")
    assert any("schema version" in warning.lower() for warning in doc.runs[0].warnings)


@pytest.mark.parametrize("version,allowed", [(None, True), ("1.4.1", True), ("2.0.0", False)])
def test_producer_version_is_pinned_not_universal(tmp_path, version, allowed):
    module = importlib.import_module("verifair.trace_importers.pi")
    options = ImportOptions(source_version=version, leaf="a3")
    if allowed:
        doc = module.convert((FIXTURE,), options)
        assert any("version" in warning.lower() for warning in doc.runs[0].warnings)
    else:
        with pytest.raises(TraceImportError, match="version"):
            module.convert((FIXTURE,), options)


@pytest.mark.parametrize(
    "rows",
    [
        [{"type": "agent_start"}, {"type": "message_end", "message": {"role": "assistant"}}],
        [{"type": "session", "version": 3, "id": "session"}, {"type": "message_end"}],
        [{"type": "session", "version": 3, "id": "session"}],
    ],
)
def test_rejects_json_mode_stream_and_empty_session(tmp_path, rows):
    with pytest.raises(TraceImportError):
        convert(tmp_path, rows)


def test_compaction_and_context_edits_do_not_reconstruct_missing_history(tmp_path):
    rows = [
        records()[0],
        {
            "type": "compaction",
            "id": "comp",
            "parentId": None,
            "summary": "synthetic-public-text",
            "firstKeptEntryId": "missing",
            "tokensBefore": 100,
        },
        {
            "type": "context_edit",
            "id": "edit",
            "parentId": "comp",
            "targetId": "missing",
            "replacement": None,
        },
    ]
    doc = convert(tmp_path, rows, include_details=True)
    assert [event.kind for event in doc.events] == ["checkpoint", "checkpoint"]
    assert doc.events[0].explanation == "synthetic-public-text"
    assert doc.runs[0].coverage == "partial"


def test_orphan_result_is_visible_without_inventing_a_call_or_parent(tmp_path):
    rows = [
        records()[0],
        {
            "type": "message",
            "id": "r",
            "parentId": None,
            "message": {
                "role": "toolResult",
                "toolCallId": "orphan",
                "toolName": "read",
                "content": [{"type": "text", "text": "known output"}],
                "isError": False,
            },
        },
    ]
    doc = convert(tmp_path, rows, include_details=True)
    event = doc.events[0]
    assert (event.kind, event.status, event.parent_id, event.input, event.output) == (
        "tool",
        "completed",
        None,
        None,
        "known output",
    )
    assert any("call" in warning.lower() for warning in doc.runs[0].warnings)


@pytest.mark.parametrize("duplicate", ["call", "result"])
def test_duplicate_tool_identity_cannot_borrow_or_overwrite_results(tmp_path, duplicate):
    rows = records()
    if duplicate == "call":
        next(row for row in rows if row.get("id") == "a2")["message"]["content"][0]["id"] = "call_1"
    else:
        row = next(row for row in rows if row.get("id") == "r1").copy()
        row.update(id="duplicate-result", parentId="a3")
        rows.append(row)
    with pytest.raises(TraceImportError):
        convert(tmp_path, rows, leaf="duplicate-result" if duplicate == "result" else "a3")


@pytest.mark.parametrize(
    "cancelled,exit_code,status",
    [
        (False, 0, "completed"),
        (False, 7, "failed"),
        (True, None, "cancelled"),
        (False, None, "unknown"),
    ],
)
def test_direct_bash_execution_preserves_only_structured_exit_status(
    tmp_path, cancelled, exit_code, status
):
    rows = [
        records()[0],
        {
            "type": "message",
            "id": "shell",
            "parentId": None,
            "message": {
                "role": "bashExecution",
                "command": "echo token=synthetic-token",
                "output": "text",
                "cancelled": cancelled,
                "exitCode": exit_code,
                "truncated": False,
                "timestamp": 1792900000123,
            },
        },
    ]
    event = convert(tmp_path, rows, include_details=True).events[0]
    assert (event.kind, event.status, event.exit_code) == ("tool", status, exit_code)
    assert event.started_at is None and event.ended_at is None
    assert "synthetic-token" not in event.input


def test_sanitizes_and_truncates_each_field_before_event_validation(tmp_path):
    rows = records()
    next(row for row in rows if row.get("id") == "a1")["message"]["content"][1]["text"] = (
        "token=synthetic-token " + "x" * 18000
    )
    doc = convert(tmp_path, rows, leaf="a3", include_details=True)
    event = next(event for event in doc.events if event.source_ref == "entry:a1")
    assert len(event.output) == 16000 and event.truncated
    assert event.output.endswith("[truncated]")
    assert "synthetic-token" not in event.output


def test_last_events_applies_after_result_merge_and_preserves_partial_warning(tmp_path):
    doc = convert(tmp_path, leaf="a3", last_events=1)
    assert [event.source_ref for event in doc.events] == ["toolCall:call_3"]
    assert doc.events[0].parent_id is None
    assert any("selection" in warning.lower() for warning in doc.runs[0].warnings)


def test_requires_exactly_one_explicit_input():
    module = importlib.import_module("verifair.trace_importers.pi")
    for paths in ((), (FIXTURE, FIXTURE)):
        with pytest.raises(TraceImportError, match="one"):
            module.convert(paths, ImportOptions())


def test_dropped_detail_objects_and_unsafe_identifiers_never_escape(tmp_path):
    rows = records()
    assistant = next(row for row in rows if row.get("id") == "a1")["message"]
    assistant["model"] = "Authorization: Bearer synthetic-token"
    assistant["content"][0]["name"] = "/synthetic-parent-path\ntoken=synthetic-token"
    assistant["content"][0]["arguments"]["command"] = {"text": "synthetic-metadata"}
    assistant["content"][1]["text"] = {"text": "synthetic-metadata"}
    text = canonical_json(convert(tmp_path, rows, leaf="a3", include_details=True))
    assert "synthetic-token" not in text
    assert "synthetic-metadata" not in text
    assert "synthetic-parent-path" not in text


def test_reads_all_records_with_one_budget_before_final_selection(tmp_path, monkeypatch):
    module = importlib.import_module("verifair.trace_importers.pi")
    from verifair.trace_importers import common

    monkeypatch.setattr(common, "MAX_INPUT_BYTES", FIXTURE.stat().st_size - 1)
    with pytest.raises(TraceImportError, match="byte limit"):
        module.convert((FIXTURE,), ImportOptions(leaf="a3", last_events=1))


def test_existing_cli_dispatch_imports_pi_without_shared_edits(tmp_path):
    from verifair.trace_import import main

    output = tmp_path / "trace.json"
    assert (
        main(
            [
                "--format",
                "pi-session-jsonl",
                "--source-version",
                "1.4.0",
                "--input",
                str(FIXTURE),
                "--leaf",
                "a3",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert json.loads(output.read_text())["runs"][0]["agent"] == "pi"


def test_preserves_native_session_identity(tmp_path):
    assert convert(tmp_path, leaf="a3").runs[0].id == "pi-synthetic-session"


@pytest.mark.parametrize("session_id", ["event-1", "event-2", "event-11"])
def test_custom_session_ids_cannot_collide_with_event_ids(tmp_path, session_id):
    rows = records()
    rows[0]["id"] = session_id
    doc = convert(tmp_path, rows, leaf="a3")
    run = doc.runs[0]
    assert run.id == "pi-" + session_id
    assert len(doc.events) == 11
    ids = [run.id, *(event.id for event in doc.events)]
    assert len(set(ids)) == len(ids)
    assert all(event.run_id == run.id for event in doc.events)
    events_by_id = {event.id: event for event in doc.events}
    assert {
        event.source_ref: events_by_id[event.parent_id].source_ref
        for event in doc.events
        if event.kind == "tool"
    } == {
        "toolCall:call_1": "entry:a1",
        "toolCall:call_2": "entry:a2",
        "toolCall:call_3": "entry:a3",
    }


@pytest.mark.parametrize("damage", ["kind-object", "stop-object", "empty-session-id"])
def test_malformed_source_fields_fail_with_static_import_error(tmp_path, damage):
    rows = records()
    if damage == "kind-object":
        rows[-1]["type"] = {"payload": "synthetic-private-payload"}
    elif damage == "stop-object":
        next(row for row in rows if row.get("id") == "a1")["message"]["stopReason"] = {
            "payload": "synthetic-private-payload"
        }
    else:
        rows[0]["id"] = ""
    with pytest.raises(TraceImportError) as error:
        convert(tmp_path, rows, leaf="a3")
    assert "synthetic-private-payload" not in str(error.value)
    assert "private-input-name" not in str(error.value)


def test_path_shaped_source_ids_are_not_copied_to_metadata(tmp_path):
    rows = [
        records()[0],
        {
            "type": "model_change",
            "id": "/synthetic-private-source-path",
            "parentId": None,
            "modelId": "model-one",
        },
    ]
    doc = convert(tmp_path, rows)
    assert "synthetic-private-source-path" not in canonical_json(doc)
    assert doc.events[0].source_ref is None


def test_fork_reference_alone_marks_missing_history_partial(tmp_path):
    rows = [
        records()[0],
        {"type": "model_change", "id": "model", "parentId": None, "modelId": "model-one"},
    ]
    assert convert(tmp_path, rows).runs[0].coverage == "partial"
