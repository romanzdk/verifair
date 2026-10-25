import copy
import importlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from verifair.trace_importers.common import ImportOptions, TraceImportError
from verifair.traces import canonical_json

FIXTURES = Path(__file__).parent / "fixtures" / "traces" / "opencode"


def convert(paths, **options):
    assert importlib.util.find_spec("verifair.trace_importers.opencode"), "OpenCode adapter missing"
    adapter = importlib.import_module("verifair.trace_importers.opencode")
    return adapter.convert(tuple(paths), ImportOptions(**options))


def payload():
    return json.loads((FIXTURES / "settled.json").read_text())


def write(tmp_path, data, name="capture.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return path


def tool_event(document):
    return next(event for event in document.events if event.kind == "tool")


def warnings(document):
    return " ".join(warning.lower() for run in document.runs for warning in run.warnings)


def test_typed_v2_export_preserves_tools_and_partial_coverage():
    doc = convert([FIXTURES / "settled.json"], source_version="2.1.3")
    assert len(doc.runs) == 1
    assert doc.runs[0].agent == "opencode"
    assert doc.runs[0].source_format == "opencode-v2-export"
    assert doc.runs[0].coverage == "partial"
    assert "settled" in warnings(doc) and "child" in warnings(doc)
    tool = tool_event(doc)
    assert (tool.title, tool.status) == ("Tool: shell", "completed")
    assert tool.parent_id == next(e.id for e in doc.events if e.title == "Assistant message")
    assert "ses_parent" in tool.source_ref and "call_shell" in tool.source_ref
    assert tool.model == "synthetic/model-test"
    assert tool.exit_code == 0
    assert all(e.input is None and e.output is None and e.explanation is None for e in doc.events)
    assert "PRIVATE_TITLE" not in canonical_json(doc)


@pytest.mark.parametrize("include_details", [False, True])
@pytest.mark.parametrize("kind", ["assistant", "compaction", "model-switched"])
def test_cloudflare_model_reference_is_preserved(tmp_path, include_details, kind):
    data = payload()
    message = data["messages"][1 if kind == "assistant" else 2]
    message["type"] = kind
    message["model"] = {
        "providerID": "cloudflare-workers-ai",
        "id": "@cf/meta/llama-3.1-8b-instruct",
    }
    data["messages"] = [message]
    doc = convert([write(tmp_path, data)], include_details=include_details)
    assert all(
        event.model == "cloudflare-workers-ai/@cf/meta/llama-3.1-8b-instruct"
        for event in doc.events
    )
    if not include_details:
        assert all(
            e.input is None and e.output is None and e.explanation is None for e in doc.events
        )


@pytest.mark.parametrize("include_details", [False, True])
@pytest.mark.parametrize("field", ["providerID", "id"])
@pytest.mark.parametrize(
    "bad",
    [None, True, {}, [], "", " ", "bad\x00name", "bad\ud800name", "bad\nname", "x" * 100],
)
def test_malformed_or_oversized_model_fields_fail_safely(tmp_path, include_details, field, bad):
    data = payload()
    data["messages"][1]["model"][field] = bad
    with pytest.raises(TraceImportError) as exc:
        convert([write(tmp_path, data)], include_details=include_details)
    assert str(tmp_path) not in str(exc.value)


@pytest.mark.parametrize("include_details", [False, True])
@pytest.mark.parametrize("field", ["providerID", "id"])
@pytest.mark.parametrize("bad", ["token=DO_NOT_EXPOSE", "Bearer DO_NOT_EXPOSE"])
def test_model_fields_cannot_bypass_credential_sanitation(tmp_path, include_details, field, bad):
    data = payload()
    data["messages"][1]["model"][field] = bad
    with pytest.raises(TraceImportError) as exc:
        convert([write(tmp_path, data)], include_details=include_details)
    assert "DO_NOT_EXPOSE" not in str(exc.value)


def test_model_reference_accepts_exact_normalized_length_bound(tmp_path):
    data = payload()
    data["messages"][1]["model"] = {"providerID": "p", "id": "@" + "m" * 97}
    doc = convert([write(tmp_path, data)])
    assert tool_event(doc).model == "p/@" + "m" * 97
    assert len(tool_event(doc).model) == 100


def test_allowlisted_details_are_redacted_before_bounded_events():
    doc = convert([FIXTURES / "settled.json"], include_details=True, source_version="2.1.3")
    text = canonical_json(doc)
    assert "Visible answer" in text and "Exposed summary" in text and "Compact summary" in text
    assert "echo token=[REDACTED]" in tool_event(doc).input
    assert "/synthetic/work" in tool_event(doc).input
    assert "output password=[REDACTED]" in tool_event(doc).output
    assert "SECRET_" not in text and "NEVER_" not in text
    assert any(e.title == "Exposed reasoning summary" for e in doc.events)
    assert any(e.title == "Assistant retry" and e.kind == "checkpoint" for e in doc.events)
    assert any(e.title == "Compaction" and e.kind == "checkpoint" for e in doc.events)


def test_tool_times_use_ran_not_message_or_creation_time():
    tool = tool_event(convert([FIXTURES / "settled.json"]))
    assert tool.started_at == datetime(2026, 10, 25, 0, 0, 0, 22000, tzinfo=UTC)
    assert tool.ended_at == datetime(2026, 10, 25, 0, 0, 0, 72000, tzinfo=UTC)


def test_large_integer_milliseconds_keep_exact_precision(tmp_path):
    data = payload()
    data["messages"][1]["content"][2]["time"] = {
        "created": 253402300799001,
        "ran": 253402300799997,
        "completed": 253402300799999,
    }
    tool = tool_event(convert([write(tmp_path, data)]))
    assert tool.started_at == datetime(9999, 12, 31, 23, 59, 59, 997000, tzinfo=UTC)
    assert tool.ended_at == datetime(9999, 12, 31, 23, 59, 59, 999000, tzinfo=UTC)


@pytest.mark.parametrize(
    "native,want",
    [
        ("streaming", "running"),
        ("running", "running"),
        ("completed", "completed"),
        ("error", "failed"),
    ],
)
def test_defensive_tool_states_do_not_infer_success(tmp_path, native, want):
    data = payload()
    tool = data["messages"][1]["content"][2]
    tool["state"]["status"] = native
    if native == "streaming":
        tool["state"] = {"status": native, "input": "NEVER_RAW_STREAMING_JSON"}
    if native == "error":
        tool["state"]["error"] = {"type": "unknown", "message": "NEVER_ERROR"}
    if native in ("running", "streaming"):
        tool["time"] = {"created": 1792886400001}
    doc = convert([write(tmp_path, data)], include_details=True)
    event = tool_event(doc)
    assert event.status == want
    if want == "running":
        assert event.started_at is None and event.ended_at is None
        assert "incomplete" in warnings(doc)
    assert "NEVER_" not in canonical_json(doc)


def test_missing_tool_ran_is_not_borrowed_from_message(tmp_path):
    data = payload()
    del data["messages"][1]["content"][2]["time"]["ran"]
    event = tool_event(convert([write(tmp_path, data)]))
    assert event.started_at is None and event.ended_at is not None


def test_long_details_are_truncated_without_validation_failure(tmp_path):
    data = payload()
    data["messages"][1]["content"][0]["text"] = "x" * 20000
    doc = convert([write(tmp_path, data)], include_details=True)
    event = next(e for e in doc.events if e.title == "Assistant message")
    assert len(event.output) == 16000 and event.output.endswith("[truncated]")
    assert event.truncated


@pytest.mark.parametrize("version", [None, "2.1.4"])
def test_missing_or_untested_producer_version_warns(version):
    doc = convert([FIXTURES / "settled.json"], source_version=version)
    assert "version" in warnings(doc) or "compatibility" in warnings(doc)


@pytest.mark.parametrize("version", ["1.19.4", "3.0.0", "unrecognized"])
def test_incompatible_producer_versions_fail(version):
    with pytest.raises(TraceImportError, match="version|v2"):
        convert([FIXTURES / "settled.json"], source_version=version)


def test_v1_wrappers_are_rejected_even_with_v2_claim():
    with pytest.raises(TraceImportError, match="v1|format family"):
        convert([FIXTURES / "v1.json"], source_version="2.1.3")


def test_unknown_kinds_warn_and_additive_fields_stay_private(tmp_path):
    data = payload()
    data["private"] = {"dump": "NEVER_ROOT"}
    data["messages"][1]["parts"] = {"additive": "NEVER_ADDITIVE"}
    data["messages"].append({"id": "msg_future", "type": "future", "data": "NEVER_RECORD"})
    data["messages"][1]["content"].append({"type": "encrypted", "text": "NEVER_HIDDEN"})
    doc = convert([write(tmp_path, data)], include_details=True)
    assert "unknown" in warnings(doc)
    assert "NEVER_" not in canonical_json(doc)


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"id": "msg_user", "type": "user", "text": "PRIVATE"}],
        [{"id": "msg_x", "type": "future"}],
    ],
)
def test_export_without_recognized_execution_fails(tmp_path, messages):
    data = payload()
    data["messages"] = messages
    with pytest.raises(TraceImportError, match="recognized|empty"):
        convert([write(tmp_path, data)])


def delegation(data, cid="call_child"):
    tool = copy.deepcopy(data["messages"][1]["content"][2])
    tool.update(id=cid, name="subagent")
    tool["state"]["metadata"] = {"sessionID": "ses_child", "status": "completed"}
    return tool


def test_explicit_child_links_once_to_native_invocation(tmp_path):
    data = payload()
    data["messages"][1]["content"].append(delegation(data))
    doc = convert([write(tmp_path, data), FIXTURES / "child.json"])
    parent, child = doc.runs
    invocation = next(e for e in doc.events if e.title == "Tool: subagent")
    assert child.parent_run_id == parent.id and child.parent_event_id == invocation.id
    assert len([e for e in doc.events if e.run_id == child.id]) == 1


def test_reused_child_keeps_parent_run_without_ambiguous_invocation(tmp_path):
    data = payload()
    data["messages"][1]["content"] += [delegation(data), delegation(data, "call_resume")]
    doc = convert([write(tmp_path, data), FIXTURES / "child.json", FIXTURES / "child.json"])
    assert len(doc.runs) == 2
    assert doc.runs[1].parent_run_id == doc.runs[0].id
    assert doc.runs[1].parent_event_id is None
    assert len([e for e in doc.events if e.run_id == doc.runs[1].id]) == 1
    assert "ambiguous" in warnings(doc) or "reused" in warnings(doc)


def test_missing_child_and_parent_exports_warn(tmp_path):
    data = payload()
    data["messages"][1]["content"].append(delegation(data))
    assert "not supplied" in warnings(convert([write(tmp_path, data)]))
    doc = convert([FIXTURES / "child.json"])
    assert doc.runs[0].parent_run_id is None
    assert "parent" in warnings(doc)


def test_fork_metadata_never_creates_delegation(tmp_path):
    child = json.loads((FIXTURES / "child.json").read_text())
    del child["info"]["parentID"]
    child["info"]["fork"] = {
        "sessionID": "ses_parent",
        "boundary": {"type": "through", "messageID": "msg_first"},
    }
    doc = convert([FIXTURES / "settled.json", write(tmp_path, child)])
    assert doc.runs[1].parent_run_id is None and doc.runs[1].parent_event_id is None
    assert "fork" in warnings(doc)


def test_selection_is_once_after_multiple_inputs(tmp_path):
    doc = convert([FIXTURES / "settled.json", FIXTURES / "child.json"], last_events=1)
    assert len(doc.runs) == 1 and len(doc.events) == 1
    assert "msg_child" in doc.events[0].source_ref
    assert "selection" in warnings(doc)


def test_aggregate_budget_applies_across_files(tmp_path, monkeypatch):
    from verifair.trace_importers import common

    first = FIXTURES / "settled.json"
    second = FIXTURES / "child.json"
    monkeypatch.setattr(common, "MAX_INPUT_BYTES", first.stat().st_size + second.stat().st_size - 1)
    with pytest.raises(TraceImportError, match="combined byte"):
        convert([first, second])


def test_same_session_updates_merge_native_ids_without_duplicate_tools(tmp_path):
    data = payload()
    earlier = copy.deepcopy(data)
    earlier["messages"][1]["content"][2]["state"]["status"] = "running"
    paths = [write(tmp_path, earlier, "earlier.json"), write(tmp_path, data, "later.json")]
    doc = convert(paths)
    assert len(doc.runs) == 1 and len([e for e in doc.events if e.kind == "tool"]) == 1
    assert tool_event(doc).status == "completed"


@pytest.mark.parametrize("value", [1.5, True, "2026-10-25T00:00:00Z", 10**30])
def test_invalid_milliseconds_fail_with_static_errors(tmp_path, value):
    data = payload()
    data["messages"][1]["content"][2]["time"]["ran"] = value
    with pytest.raises(TraceImportError) as exc:
        convert([write(tmp_path, data)])
    assert "capture.json" not in str(exc.value) and str(value) not in str(exc.value)


def test_malformed_identifiers_and_payloads_never_escape_errors(tmp_path):
    data = payload()
    data["info"]["id"] = "password=DO_NOT_EXPOSE"
    with pytest.raises(TraceImportError) as exc:
        convert([write(tmp_path, data)])
    assert "DO_NOT_EXPOSE" not in str(exc.value) and str(tmp_path) not in str(exc.value)


def test_cli_uses_existing_lazy_dispatch(tmp_path):
    output = tmp_path / "trace.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "verifair.trace_import",
            "--format",
            "opencode-v2-export",
            "--source-version",
            "2.1.3",
            "--input",
            str(FIXTURES / "settled.json"),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text())["runs"][0]["agent"] == "opencode"


@pytest.mark.parametrize("native", [[], {"private": "NEVER_STATE"}])
@pytest.mark.parametrize("kind", ["tool", "shell", "compaction"])
def test_malformed_state_discriminators_fail_safely(tmp_path, native, kind):
    data = payload()
    if kind == "tool":
        data["messages"][1]["content"][2]["state"]["status"] = native
    else:
        data["messages"][2].update(type=kind, status=native)
    with pytest.raises(TraceImportError) as exc:
        convert([write(tmp_path, data)])
    assert "NEVER_STATE" not in str(exc.value)


def test_only_known_read_path_is_extracted_from_arguments(tmp_path):
    data = payload()
    tool = data["messages"][1]["content"][2]
    tool["name"] = "read"
    tool["state"]["input"] = {"path": "/synthetic/read.txt", "private": "NEVER_ARGS"}
    event = tool_event(convert([write(tmp_path, data)], include_details=True))
    assert event.input == "/synthetic/read.txt" and event.exit_code is None
    tool["name"] = "custom"
    event = tool_event(convert([write(tmp_path, data)], include_details=True))
    assert event.input is None and event.exit_code is None


@pytest.mark.parametrize("field", ["command", "workdir"])
def test_object_arguments_are_never_stringified(tmp_path, field):
    data = payload()
    data["messages"][1]["content"][2]["state"]["input"][field] = {"text": "NEVER_OBJECT"}
    assert "NEVER_" not in canonical_json(convert([write(tmp_path, data)], include_details=True))


@pytest.mark.parametrize(
    "native,want",
    [
        ("completed", "completed"),
        ("running", "running"),
        ("failed", "failed"),
        ("future", "unknown"),
    ],
)
def test_compaction_status_and_discarded_context_limit(tmp_path, native, want):
    data = payload()
    data["messages"][2]["status"] = native
    doc = convert([write(tmp_path, data)], include_details=True)
    event = next(e for e in doc.events if e.title == "Compaction")
    assert event.status == want and event.ended_at is None
    assert "discarded" in warnings(doc) and "NEVER_HIDDEN_CONTEXT" not in canonical_json(doc)


@pytest.mark.parametrize(
    "completed,error,want", [(False, False, "running"), (True, True, "failed")]
)
def test_assistant_lifecycle_uses_settlement_and_structured_error(tmp_path, completed, error, want):
    data = payload()
    message = data["messages"][1]
    if not completed:
        del message["time"]["completed"]
    if error:
        message["error"] = {"type": "unknown", "message": "NEVER_ERROR"}
    doc = convert([write(tmp_path, data)], include_details=True)
    event = next(e for e in doc.events if e.title == "Assistant message")
    assert event.status == want and "NEVER_" not in canonical_json(doc)


@pytest.mark.parametrize(
    "native,exit_code,want",
    [
        ("exited", 0, "completed"),
        ("exited", 3, "failed"),
        ("running", None, "running"),
        ("timeout", None, "failed"),
        ("killed", None, "cancelled"),
    ],
)
def test_native_shell_records_preserve_explicit_outcome(tmp_path, native, exit_code, want):
    data = payload()
    data["messages"] = [
        {
            "id": "msg_shell",
            "type": "shell",
            "shellID": "sh_synthetic",
            "command": "echo shell",
            "status": native,
            "time": {"created": 1792886400001},
            "output": {"output": "shell output", "cursor": 12, "size": 12, "truncated": True},
        }
    ]
    if exit_code is not None:
        data["messages"][0]["exit"] = exit_code
        data["messages"][0]["time"]["completed"] = 1792886400010
    doc = convert([write(tmp_path, data)], include_details=True)
    event = tool_event(doc)
    assert event.status == want and event.exit_code == exit_code
    assert event.started_at is None and event.truncated
    assert event.input == "echo shell" and event.output == "shell output"


def test_selection_can_reduce_oversized_aggregate(tmp_path):
    data = payload()
    template = data["messages"][1]
    data["messages"] = [
        {**template, "id": f"msg_{i}", "content": [], "retry": None} for i in range(1001)
    ]
    doc = convert([write(tmp_path, data)], last_events=1)
    assert len(doc.events) == 1 and doc.events[0].ordinal == 1001


def test_conflicting_child_metadata_does_not_override_session_parent(tmp_path):
    data = payload()
    data["messages"][1]["content"].append(delegation(data))
    child = json.loads((FIXTURES / "child.json").read_text())
    child["info"]["parentID"] = "ses_missing"
    doc = convert([write(tmp_path, data), write(tmp_path, child, "child.json")])
    assert doc.runs[1].parent_run_id is None and doc.runs[1].parent_event_id is None
    assert "conflict" in warnings(doc)


def test_last_events_respects_native_tool_and_reasoning_order(tmp_path):
    data = payload()
    message = data["messages"][1]
    message.pop("retry")
    message["content"] = [message["content"][2], message["content"][1]]
    data["messages"] = [message]
    doc = convert([write(tmp_path, data)], last_events=1)
    assert doc.events[0].title == "Exposed reasoning summary"


def test_repeated_native_tool_id_is_one_event_at_final_content_position(tmp_path):
    data = payload()
    message = data["messages"][1]
    initial = copy.deepcopy(message["content"][2])
    initial["state"]["status"] = "running"
    message["content"].insert(0, initial)
    doc = convert([write(tmp_path, data)])
    assert len([e for e in doc.events if e.kind == "tool"]) == 1
    assert tool_event(doc).status == "completed"
