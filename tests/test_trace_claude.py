import json
import sys
import tracemalloc
from pathlib import Path

import pytest

from verifair.trace_import import main
from verifair.trace_importers import claude
from verifair.trace_importers.common import ImportOptions, TraceImportError
from verifair.traces import canonical_json

FIXTURE = Path(__file__).parent / "fixtures/traces/claude/native.jsonl"


def convert(paths, **options):
    return claude.convert(tuple(paths), ImportOptions(**{"source_version": "2.1.309", **options}))


def capture(tmp_path, records, name="capture.jsonl"):
    path = tmp_path / name
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def text_stream(chunks):
    events = [
        {"type": "message_start", "message": {"id": "message", "model": "claude-sonnet-4-6"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        *(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            }
            for text in chunks
        ),
        {"type": "content_block_stop", "index": 0},
    ]
    return [
        {"type": "stream_event", "uuid": f"stream-{i}", "session_id": "session", "event": event}
        for i, event in enumerate(events)
    ]


def test_default_stream_does_not_accumulate_excluded_prose(tmp_path):
    path = capture(tmp_path, text_stream(["x" * 4096] * 256))
    tracemalloc.start()
    try:
        doc = convert([path])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(doc.events) == 1 and doc.events[0].output is None
    assert peak < 512 * 1024


@pytest.mark.parametrize("kind", ["text", "thinking", "tool", "tool_result", "user", "result"])
def test_default_records_do_not_retain_excluded_prose(tmp_path, kind):
    prose = "x" * 65536
    records = []
    for i in range(32):
        if kind in ("text", "thinking"):
            record = assistant(str(i), [{"type": kind, kind: prose}])
        elif kind == "tool":
            record = assistant(str(i), [tool(str(i))])
            record["message"]["content"][0]["input"]["command"] = prose
        elif kind in ("tool_result", "user"):
            record = result(str(i))
            if kind == "tool_result":
                record["message"]["content"][0]["content"] = prose
            else:
                record["message"]["content"] = prose
        else:
            record = {
                "type": "result",
                "uuid": str(i),
                "session_id": "session",
                "subtype": "success",
                "result": prose,
            }
        records.append(record)
    path = capture(tmp_path, records)
    tracemalloc.start()
    try:
        doc = convert([path])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(doc.events) == 32
    assert all(e.input is None and e.output is None and e.explanation is None for e in doc.events)
    assert peak < 1024 * 1024


def test_stream_details_redact_across_chunks_before_truncation_and_deduplicate(tmp_path):
    records = text_stream(["Author", "ization: Basic ", "PRIVATE" * 4000, "\n", "x" * 18000])
    records.insert(4, records[3])
    doc = convert([capture(tmp_path, records)], include_details=True)
    [event] = doc.events
    assert event.status == "completed" and event.truncated
    assert event.output.startswith("Authorization: [REDACTED]\nxxx")
    assert event.output.endswith("[truncated]") and len(event.output) == 16000
    assert "PRIVATE" not in canonical_json(doc)
    small = text_stream(["one ", "two"])
    small.insert(3, small[2])
    assert convert([capture(tmp_path, small)], include_details=True).events[0].output == "one two"


def test_independent_context_selection_work_grows_linearly(tmp_path):
    counts = []
    for size in (64, 128):
        path = capture(
            tmp_path,
            ({"type": "system", "subtype": "init", "session_id": f"s-{i}"} for i in range(size)),
        )
        count = 0

        def count_lines(frame, event, arg):
            nonlocal count
            if frame.f_code.co_filename == claude.__file__:
                count += event == "line"
                return count_lines

        previous = sys.gettrace()
        sys.settrace(count_lines)
        try:
            doc = convert([path], last_events=1)
        finally:
            sys.settrace(previous)
        assert len(doc.events) == len(doc.runs) == 1
        assert doc.events[0].id == f"claude-event-{size}"
        assert doc.runs[0].parent_run_id is None
        assert any("selection" in warning.lower() for warning in doc.runs[0].warnings)
        counts.append(count)
    assert counts[1] < counts[0] * 3


@pytest.mark.parametrize("final_assistant", [False, True], ids=["message-stop", "final-assistant"])
@pytest.mark.parametrize("include_details", [False, True])
def test_distinct_message_block_lookup_work_grows_linearly(
    tmp_path, final_assistant, include_details
):
    counts = []
    for size in (512, 1024):
        records = []
        for i in range(size):
            message = text_stream(["done"])
            message[0]["event"]["message"]["id"] = f"m-{i}"
            if final_assistant:
                final = assistant(
                    f"final-{i}", [{"type": "text", "text": "done"}], session_id="session"
                )
                final["message"]["id"] = f"m-{i}"
                message.insert(-1, final)
            else:
                message.append(
                    {
                        "type": "stream_event",
                        "session_id": "session",
                        "event": {"type": "message_stop"},
                    }
                )
            for j, record in enumerate(message):
                record["uuid"] = f"{i}-{j}"
            records.extend(message)
        path = capture(tmp_path, records)
        count = 0

        def count_lines(frame, event, arg):
            nonlocal count
            if frame.f_code.co_filename == claude.__file__:
                count += event == "line"
                return count_lines

        previous = sys.gettrace()
        sys.settrace(count_lines)
        try:
            doc = convert([path], last_events=1, include_details=include_details)
        finally:
            sys.settrace(previous)
        [event] = doc.events
        assert len(doc.runs) == 1
        assert (event.id, event.ordinal, event.status) == (
            f"claude-event-{size}",
            size,
            "completed",
        )
        assert event.output == ("done" if include_details else None)
        assert not event.truncated
        assert not any("completion is missing" in warning for warning in doc.runs[0].warnings)
        counts.append(count)
    assert counts[1] < counts[0] * 3


def assistant(uuid, blocks, **fields):
    return {
        "type": "assistant",
        "uuid": uuid,
        "session_id": "session-main",
        "parent_tool_use_id": None,
        "message": {
            "id": "shared-message",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": blocks,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        **fields,
    }


def tool(id="call-1"):
    return {"type": "tool_use", "id": id, "name": "Bash", "input": {"command": "pytest -q"}}


def result(id="call-1", **fields):
    return {
        "type": "user",
        "uuid": "result-" + id,
        "session_id": "session-main",
        "parent_tool_use_id": None,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": id,
                    "is_error": False,
                    "content": "tests passed exit code 12",
                }
            ],
        },
        **fields,
    }


def test_cli_dispatch_reads_native_claude(tmp_path):
    output = tmp_path / "trace.json"
    assert (
        main(
            [
                "--format",
                "claude-stream-json",
                "--input",
                str(FIXTURE),
                "--output",
                str(output),
                "--source-version",
                "2.1.309",
            ]
        )
        == 0
    )
    assert json.loads(output.read_text())["runs"][0]["agent"] == "claude_code"


def test_stream_blocks_merge_without_losing_shared_message_blocks():
    doc = convert([FIXTURE])
    tools = [e for e in doc.events if e.kind == "tool"]
    assert len(tools) == 1
    assert tools[0].status == "failed" and tools[0].exit_code is None
    assert "call-1" in tools[0].source_ref
    assert len([e for e in doc.events if e.title == "Assistant statement"]) == 1
    assert any(e.title == "Compaction boundary" for e in doc.events)
    assert any(e.title == "Execution result" for e in doc.events)
    assert all(e.input is None and e.output is None and e.explanation is None for e in doc.events)
    statement = next(e for e in doc.events if e.title == "Assistant statement")
    assert statement.ended_at.isoformat() == "2026-10-25T09:00:00.123456+00:00"
    assert tools[0].started_at is None and tools[0].ended_at is None


def test_opt_in_allowlist_drops_opaque_and_nested_objects():
    doc = convert([FIXTURE], include_details=True)
    raw = canonical_json(doc)
    assert "pytest -q" in raw and "[REDACTED]" in raw
    for secret in [
        "fixture-secret",
        "fixture-password",
        "fixture-result",
        "ENV-NEVER",
        "IMAGE-NEVER",
        "/synthetic/private",
    ]:
        assert secret not in raw
    assert all(e.kind != "summary" for e in doc.events)


def test_distinct_sdk_blocks_same_upstream_id_and_duplicate_delivery(tmp_path):
    first = assistant("uuid-1", [{"type": "text", "text": "one"}])
    second = assistant("uuid-2", [{"type": "text", "text": "two"}, tool()])
    path = capture(tmp_path, [first, first, second, result()])
    doc = convert([path], include_details=True)
    assert [e.output for e in doc.events if e.title == "Assistant statement"] == ["one", "two"]
    assert len([e for e in doc.events if e.kind == "tool"]) == 1
    assert len({e.source_ref for e in doc.events}) == len(doc.events)


def test_content_block_stop_is_not_tool_completion(tmp_path):
    records = [json.loads(line) for line in FIXTURE.read_text().splitlines()][:11]
    doc = convert([capture(tmp_path, records)])
    call = next(e for e in doc.events if e.kind == "tool")
    assert call.status == "running" and call.exit_code is None
    assert any("completion" in w.lower() for w in doc.runs[0].warnings)


def test_call_id_collisions_across_files_retries_and_orphan_results(tmp_path):
    a = capture(tmp_path, [assistant("a", [tool()]), result()], "a.jsonl")
    b = capture(
        tmp_path,
        [assistant("b", [tool(), tool("retry")]), result("retry"), result("orphan")],
        "b.jsonl",
    )
    doc = convert([a, b])
    calls = [e for e in doc.events if e.kind == "tool"]
    assert len(calls) == 4
    assert [e.status for e in calls] == ["completed", "running", "completed", "completed"]
    assert len({e.id for e in calls}) == 4
    assert any("orphan" in w.lower() for w in doc.runs[1].warnings)
    selected = convert([a, b], last_events=1)
    assert len(selected.events) == len(selected.runs) == 1


def test_readable_thinking_summary_opt_in_and_hidden_payload_dropped(tmp_path):
    path = capture(
        tmp_path,
        [
            assistant(
                "a",
                [
                    {
                        "type": "thinking",
                        "thinking": "Readable summary token=fixture-secret",
                        "signature": "SIGNATURE-NEVER",
                    },
                    {"type": "redacted_thinking", "data": "OPAQUE-NEVER"},
                    {"type": "text", "text": "done", "raw_content": "RAW-NEVER"},
                ],
            )
        ],
    )
    doc = convert([path], include_details=True)
    assert any(e.title == "Provider reasoning summary" and e.explanation for e in doc.events)
    for secret in ["fixture-secret", "SIGNATURE-NEVER", "OPAQUE-NEVER", "RAW-NEVER"]:
        assert secret not in canonical_json(doc)
    assert "Readable summary" not in canonical_json(convert([path]))


def test_forwarded_child_uses_agent_id_and_explicit_invocation_link(tmp_path):
    parent = assistant("p", [{"type": "tool_use", "id": "spawn", "name": "Agent", "input": {}}])
    task = {
        "type": "system",
        "subtype": "task_started",
        "uuid": "task-1",
        "session_id": "session-main",
        "task_id": "agent-1",
        "run_id": "run-1",
        "tool_use_id": "spawn",
        "task_type": "local_agent",
        "description": "DROP-ME",
    }
    child = assistant(
        "c", [{"type": "text", "text": "child"}], agent_id="agent-1", parent_tool_use_id="spawn"
    )
    doc = convert([capture(tmp_path, [parent, task, child, result("spawn")])])
    assert len(doc.runs) == 2
    assert doc.runs[1].parent_run_id == doc.runs[0].id
    assert doc.runs[1].parent_event_id == next(e.id for e in doc.events if e.kind == "tool")
    assert "DROP-ME" not in canonical_json(doc)
    missing = convert([capture(tmp_path, [parent, task], "missing.jsonl")])
    assert any("child" in w.lower() for w in missing.runs[0].warnings)


def test_resumed_child_does_not_borrow_first_invocation_edge(tmp_path):
    tasks = [
        {
            "type": "system",
            "subtype": "task_started",
            "uuid": f"task-{n}",
            "session_id": "session-main",
            "task_id": "agent-1",
            "run_id": f"run-{n}",
            "tool_use_id": f"spawn-{n}",
            "task_type": "local_agent",
            "description": "",
        }
        for n in (1, 2)
    ]
    calls = [{"type": "tool_use", "id": f"spawn-{n}", "name": "Agent", "input": {}} for n in (1, 2)]
    child = assistant(
        "child",
        [{"type": "text", "text": "child"}],
        agent_id="agent-1",
        parent_tool_use_id="spawn-1",
    )
    doc = convert([capture(tmp_path, [assistant("p", calls), *tasks, child])])
    assert doc.runs[1].parent_run_id == doc.runs[0].id
    assert doc.runs[1].parent_event_id is None
    assert any("ambiguous" in w.lower() for w in doc.runs[1].warnings)


def test_detached_placeholder_does_not_complete_tool(tmp_path):
    path = capture(
        tmp_path, [assistant("a", [tool()]), result(tool_use_result={"detachedToolCall": True})]
    )
    doc = convert([path])
    assert next(e for e in doc.events if e.kind == "tool").status == "running"


@pytest.mark.parametrize(
    "records",
    [
        [
            {
                "type": "assistant",
                "uuid": "a",
                "message": {"role": "assistant", "content": "history"},
            }
        ],
        [{"type": "session_meta", "payload": {}}],
        [{"type": "message", "role": "assistant", "content": []}],
    ],
)
def test_rejects_private_history_and_other_formats(tmp_path, records):
    with pytest.raises(TraceImportError):
        convert([capture(tmp_path, records)])


def test_unknown_records_warn_without_payload_leak(tmp_path):
    path = capture(
        tmp_path,
        [
            assistant("a", [{"type": "text", "text": "ok"}]),
            {"type": "system", "subtype": "future-DROP-ME", "session_id": "session-main"},
            {"type": "future-DROP-ME", "payload": "DROP-ME", "session_id": "session-main"},
        ],
    )
    doc = convert([path])
    assert any("unknown" in w.lower() for w in doc.runs[0].warnings)
    assert "DROP-ME" not in canonical_json(doc)


def test_versions_missing_untested_and_unsupported_major():
    assert any(
        "version" in w.lower() for w in convert([FIXTURE], source_version=None).runs[0].warnings
    )
    assert any(
        "untested" in w.lower()
        for w in convert([FIXTURE], source_version="2.1.310").runs[0].warnings
    )
    with pytest.raises(TraceImportError):
        convert([FIXTURE], source_version="3.0.0")


def test_shared_read_budget_enforces_combined_actual_bytes(tmp_path, monkeypatch):
    from verifair.trace_importers import common

    a = capture(tmp_path, [assistant("a", [{"type": "text", "text": "a"}])], "a.jsonl")
    b = capture(tmp_path, [assistant("b", [{"type": "text", "text": "b"}])], "b.jsonl")
    monkeypatch.setattr(common, "MAX_INPUT_BYTES", a.stat().st_size + 1)
    with pytest.raises(TraceImportError, match="combined byte limit"):
        convert([a, b])


def test_bounded_details_and_invalid_metadata_are_not_payload_channels(tmp_path):
    path = capture(
        tmp_path,
        [
            assistant(
                "a",
                [
                    {"type": "text", "text": "x" * 18000},
                    {"type": "tool_use", "id": "t", "name": "token=DROP-ME", "input": {}},
                ],
                message={
                    "id": "message",
                    "model": "token=DROP-ME",
                    "content": [
                        {"type": "text", "text": "x" * 18000},
                        {"type": "tool_use", "id": "t", "name": "token=DROP-ME", "input": {}},
                    ],
                },
            )
        ],
    )
    doc = convert([path], include_details=True)
    assert any(e.truncated and len(e.output) == 16000 for e in doc.events if e.output)
    assert "DROP-ME" not in canonical_json(doc)


@pytest.mark.parametrize("status", ["async_launched", "remote_launched"])
def test_agent_launch_result_remains_running_until_notification(tmp_path, status):
    invocation = assistant("a", [{"type": "tool_use", "id": "spawn", "name": "Agent", "input": {}}])
    output = {
        "status": status,
        "description": "DROP-ME",
        "prompt": "DROP-ME",
        "outputFile": "/private/DROP-ME",
    }
    if status == "async_launched":
        output["agentId"] = "agent-1"
    else:
        output.update(taskId="remote-1", sessionUrl="https://invalid.example")
    launched = result("spawn", tool_use_result=output)
    doc = convert([capture(tmp_path, [invocation, launched])])
    assert next(e for e in doc.events if e.kind == "tool").status == "running"


def test_agent_structured_result_links_explicit_child_and_selects_report_text(tmp_path):
    invocation = assistant("a", [{"type": "tool_use", "id": "spawn", "name": "Agent", "input": {}}])
    completed = result(
        "spawn",
        tool_use_result={
            "status": "completed",
            "agentId": "agent-1",
            "content": [{"type": "text", "text": "actual report token=fixture-secret"}],
            "totalToolUseCount": 0,
            "totalDurationMs": 1,
            "totalTokens": 0,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": None,
                "cache_read_input_tokens": None,
                "server_tool_use": None,
                "service_tier": None,
                "cache_creation": None,
            },
            "prompt": "DROP-ME",
        },
    )
    child = assistant("c", [{"type": "text", "text": "child"}], agent_id="agent-1")
    doc = convert([capture(tmp_path, [invocation, completed, child])], include_details=True)
    assert doc.runs[1].parent_event_id == next(e.id for e in doc.events if e.kind == "tool")
    assert (
        next(e for e in doc.events if e.kind == "tool").output == "actual report token=[REDACTED]"
    )
    assert "DROP-ME" not in canonical_json(doc)


def test_structured_bash_interruption_is_cancelled_without_parsing_exit_text(tmp_path):
    doc = convert(
        [
            capture(
                tmp_path,
                [
                    assistant("a", [tool()]),
                    result(tool_use_result={"stdout": "", "stderr": "", "interrupted": True}),
                ],
            )
        ]
    )
    assert next(e for e in doc.events if e.kind == "tool").status == "cancelled"
    assert next(e for e in doc.events if e.kind == "tool").exit_code is None


def test_background_task_lifecycle_uses_run_id_without_borrowing_other_run(tmp_path):
    def task(subtype, uuid, run_id, **fields):
        return {
            "type": "system",
            "subtype": subtype,
            "uuid": uuid,
            "task_id": "task",
            "session_id": "session-main",
            "run_id": run_id,
            "tool_use_id": "call-1",
            "task_type": "local_bash",
            "description": "",
            **fields,
        }

    path = capture(
        tmp_path,
        [
            assistant("a", [tool()]),
            task("task_started", "s1", "run-1"),
            task(
                "task_notification",
                "n1",
                "run-1",
                status="completed",
                output_file="/private/DROP-ME",
                summary="DROP-ME",
            ),
            task("task_started", "s2", "run-2"),
        ],
    )
    doc = convert([path])
    tasks = [e for e in doc.events if e.title == "Background task"]
    assert [e.status for e in tasks] == ["completed", "running"]
    assert "DROP-ME" not in canonical_json(doc)


def test_interrupted_text_is_not_completed_by_content_block_stop(tmp_path):
    records = [json.loads(line) for line in FIXTURE.read_text().splitlines()][:6]
    records[4]["aborted"] = True
    doc = convert([capture(tmp_path, records)])
    assert next(e for e in doc.events if e.kind == "assistant").status == "unknown"


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("aborted", [False, True])
@pytest.mark.parametrize("include_details", [False, True])
def test_structured_assistant_failure_survives_final_and_stop(
    tmp_path, streamed, aborted, include_details
):
    records = [json.loads(line) for line in FIXTURE.read_text().splitlines()][1:6]
    records[3].update(error="rate_limit", aborted=aborted)
    if not streamed:
        records = [records[3]]
    doc = convert([capture(tmp_path, records)], include_details=include_details)
    [event] = doc.events
    assert event.status == "failed"
    assert any("failed" in warning.lower() for warning in doc.runs[0].warnings)
    assert event.output == ("Assistant token=[REDACTED]" if include_details else None)
    assert "fixture-secret" not in canonical_json(doc)


def test_incomplete_stream_text_has_visible_completion_warning(tmp_path):
    records = [json.loads(line) for line in FIXTURE.read_text().splitlines()][:4]
    doc = convert([capture(tmp_path, records)], include_details=True)
    assert next(e for e in doc.events if e.kind == "assistant").status == "running"
    assert any("completion" in w.lower() for w in doc.runs[0].warnings)


@pytest.mark.parametrize("timestamp", ["2026-10-25T09:00:00.1234567Z", "2026-10-25T09:00:00"])
def test_invalid_source_timestamp_fails_with_static_error(tmp_path, timestamp):
    path = capture(
        tmp_path, [assistant("a", [{"type": "text", "text": "ok"}], timestamp=timestamp)]
    )
    with pytest.raises(TraceImportError) as error:
        convert([path])
    assert timestamp not in str(error.value) and str(path) not in str(error.value)


def test_nested_task_parentage_uses_parent_task_id(tmp_path):
    def spawn(id):
        return {"type": "tool_use", "id": id, "name": "Agent", "input": {}}

    records = [
        assistant("main", [spawn("spawn-1")]),
        {
            "type": "system",
            "subtype": "task_started",
            "uuid": "t1",
            "session_id": "session-main",
            "task_id": "agent-1",
            "tool_use_id": "spawn-1",
            "task_type": "local_agent",
            "description": "",
        },
        assistant("child1", [spawn("spawn-2")], agent_id="agent-1", parent_tool_use_id="spawn-1"),
        {
            "type": "system",
            "subtype": "task_started",
            "uuid": "t2",
            "session_id": "session-main",
            "task_id": "agent-2",
            "parent_task_id": "agent-1",
            "tool_use_id": "spawn-2",
            "task_type": "local_agent",
            "description": "",
        },
        assistant("child2", [{"type": "text", "text": "nested"}], agent_id="agent-2"),
    ]
    doc = convert([capture(tmp_path, records)])
    assert doc.runs[2].parent_run_id == doc.runs[1].id
    assert doc.runs[2].parent_event_id == next(
        e.id for e in doc.events if e.source_ref == "spawn-2"
    )


def test_resumed_child_without_agent_id_keeps_only_session_parent(tmp_path):
    calls = [{"type": "tool_use", "id": f"spawn-{n}", "name": "Agent", "input": {}} for n in (1, 2)]
    tasks = [
        {
            "type": "system",
            "subtype": "task_started",
            "uuid": f"task-{n}",
            "session_id": "session-main",
            "task_id": "agent-1",
            "run_id": f"run-{n}",
            "tool_use_id": f"spawn-{n}",
            "task_type": "local_agent",
            "description": "",
        }
        for n in (1, 2)
    ]
    child = assistant("child", [{"type": "text", "text": "child"}], parent_tool_use_id="spawn-1")
    doc = convert([capture(tmp_path, [assistant("p", calls), *tasks, child])])
    assert doc.runs[1].parent_run_id == doc.runs[0].id
    assert doc.runs[1].parent_event_id is None
    assert any("ambiguous" in w.lower() for w in doc.runs[1].warnings)


def test_explicit_child_file_is_linked_without_reading_adjacent_files(tmp_path):
    parent = capture(
        tmp_path,
        [assistant("p", [{"type": "tool_use", "id": "spawn", "name": "Agent", "input": {}}])],
        "parent.jsonl",
    )
    child = capture(
        tmp_path,
        [
            assistant(
                "c",
                [{"type": "text", "text": "child"}],
                session_id="child-session",
                agent_id="agent-1",
                parent_tool_use_id="spawn",
            )
        ],
        "child.jsonl",
    )
    ignored = tmp_path / "unselected.jsonl"
    ignored.write_text("invalid json")
    doc = convert([parent, child])
    assert doc.runs[1].parent_event_id == doc.events[0].id
    assert len(convert([parent]).runs) == 1


def test_task_progress_without_start_is_running_and_malformed_status_is_safe(tmp_path):
    task = {
        "type": "system",
        "subtype": "task_progress",
        "uuid": "task",
        "session_id": "session-main",
        "task_id": "task-1",
        "description": "",
        "usage": {"total_tokens": 0, "tool_uses": 0, "duration_ms": 1},
    }
    doc = convert([capture(tmp_path, [task])])
    assert doc.events[0].status == "running"
    task.update(subtype="task_notification", status={})
    with pytest.raises(TraceImportError):
        convert([capture(tmp_path, [task], "bad.jsonl")])


@pytest.mark.parametrize("field", ["agent_id", "parent_tool_use_id"])
def test_invalid_explicit_child_identity_is_not_silently_main_thread(tmp_path, field):
    message = assistant("a", [{"type": "text", "text": "child"}], **{field: "token=DROP-ME"})
    with pytest.raises(TraceImportError) as error:
        convert([capture(tmp_path, [message])])
    assert "DROP-ME" not in str(error.value)


def test_credential_assignments_are_not_valid_model_or_tool_identifiers(tmp_path):
    message = assistant(
        "a",
        [
            {"type": "text", "text": "ok"},
            {"type": "tool_use", "id": "call", "name": "token:fixture-secret", "input": {}},
        ],
    )
    message["message"]["model"] = "authorization:fixture-secret"
    assert "fixture-secret" not in canonical_json(convert([capture(tmp_path, [message])]))


def test_unknown_result_subtype_warns_without_copying_subtype_payload(tmp_path):
    record = {
        "type": "result",
        "subtype": "future-DROP-ME",
        "uuid": "result",
        "session_id": "session-main",
        "is_error": False,
        "result": "DROP-ME",
    }
    doc = convert([capture(tmp_path, [record])])
    assert doc.events[0].status == "unknown"
    assert any("unknown" in w.lower() for w in doc.runs[0].warnings)
    assert "DROP-ME" not in canonical_json(doc)


def task_record(subtype, uuid, task_id, run_id="run-1", **fields):
    record = {
        "type": "system",
        "subtype": subtype,
        "uuid": uuid,
        "session_id": "session-main",
        "task_id": task_id,
    }
    if run_id is not None:
        record["run_id"] = run_id
    if subtype == "task_started":
        record.update(description="", task_type="local_agent")
    elif subtype == "task_notification":
        record.update(
            status="completed", output_file="/synthetic/output", summary="Synthetic result"
        )
    return {**record, **fields}


def agent_call(id):
    return {"type": "tool_use", "id": id, "name": "Agent", "input": {}}


def test_local_explicit_parent_wins_over_foreign_task_id_collision(tmp_path):
    foreign = capture(
        tmp_path,
        [
            assistant("parent-a", [agent_call("spawn-a")], session_id="session-a"),
            task_record(
                "task_started",
                "task-a",
                "shared-agent",
                tool_use_id="spawn-a",
                session_id="session-a",
            ),
        ],
        "a.jsonl",
    )
    local = capture(
        tmp_path,
        [
            assistant("parent-b", [agent_call("spawn-b")], session_id="session-b"),
            assistant(
                "child-b",
                [{"type": "text", "text": "child"}],
                session_id="session-b",
                agent_id="shared-agent",
                parent_tool_use_id="spawn-b",
            ),
        ],
        "b.jsonl",
    )
    doc = convert([foreign, local])
    owner = next(e for e in doc.events if e.source_ref == "spawn-b")
    child = doc.runs[2]
    assert (child.parent_run_id, child.parent_event_id) == (owner.run_id, owner.id)
    assert any("child capture is missing" in w.lower() for w in doc.runs[0].warnings)


@pytest.mark.parametrize("session", ["session-main", "session-other"])
def test_task_id_alone_cannot_link_different_explicit_captures(tmp_path, session):
    parent = capture(
        tmp_path,
        [
            assistant("parent", [agent_call("spawn")]),
            task_record("task_started", "start", "shared-agent", tool_use_id="spawn"),
        ],
        "parent.jsonl",
    )
    child = capture(
        tmp_path,
        [
            assistant(
                "child",
                [{"type": "text", "text": "child"}],
                session_id=session,
                agent_id="shared-agent",
            )
        ],
        "child.jsonl",
    )
    doc = convert([parent, child])
    assert doc.runs[1].parent_run_id is None and doc.runs[1].parent_event_id is None
    assert any("unavailable" in w.lower() for w in doc.runs[1].warnings)


@pytest.mark.parametrize(
    ("native_status", "expected"),
    [("completed", "completed"), ("failed", "failed"), ("stopped", "cancelled")],
)
def test_task_notification_uses_retained_call_when_optional_tool_id_is_absent(
    tmp_path, native_status, expected
):
    records = [
        assistant("parent", [agent_call("spawn")]),
        result(
            "spawn",
            tool_use_result={
                "status": "async_launched",
                "agentId": "agent-1",
                "description": "",
                "prompt": "",
                "outputFile": "/synthetic/output",
            },
        ),
        task_record("task_started", "start", "agent-1", tool_use_id="spawn"),
        task_record("task_notification", "end", "agent-1", status=native_status),
    ]
    doc = convert([capture(tmp_path, records)])
    assert next(e for e in doc.events if e.kind == "tool").status == expected
    assert next(e for e in doc.events if e.title == "Background task").status == expected


@pytest.mark.parametrize("include_tool_id", [True, False])
def test_nested_notification_updates_child_call_without_borrowing_colliding_root_id(
    tmp_path, include_tool_id
):
    records = [
        assistant("parent", [agent_call("launch-child"), agent_call("shared-call")]),
        task_record("task_started", "child-start", "child-1", tool_use_id="launch-child"),
        task_record(
            "task_started",
            "nested-start",
            "child-2",
            tool_use_id="shared-call",
            parent_task_id="child-1",
        ),
        assistant(
            "child",
            [agent_call("shared-call")],
            agent_id="child-1",
            parent_tool_use_id="launch-child",
        ),
        task_record(
            "task_notification",
            "nested-end",
            "child-2",
            **({"tool_use_id": "shared-call"} if include_tool_id else {}),
        ),
    ]
    doc = convert([capture(tmp_path, records)])
    calls = [e for e in doc.events if e.source_ref == "shared-call"]
    assert [e.status for e in calls] == ["running", "completed"]
    assert calls[0].run_id != calls[1].run_id
    assert doc.runs[1].parent_run_id == calls[0].run_id


def test_unavailable_nested_owner_does_not_complete_root_with_same_call_id(tmp_path):
    doc = convert(
        [
            capture(
                tmp_path,
                [
                    assistant("root", [agent_call("shared")]),
                    task_record(
                        "task_started",
                        "start",
                        "nested",
                        tool_use_id="shared",
                        parent_task_id="uncaptured-parent",
                    ),
                    task_record("task_notification", "end", "nested", tool_use_id="shared"),
                ],
            )
        ]
    )
    assert next(e for e in doc.events if e.kind == "tool").status == "running"
    assert any("binding" in w.lower() or "owner" in w.lower() for w in doc.runs[0].warnings)


def test_repeated_run_id_across_tasks_does_not_borrow_other_call_binding(tmp_path):
    doc = convert(
        [
            capture(
                tmp_path,
                [
                    assistant("root", [agent_call("one"), agent_call("two")]),
                    task_record(
                        "task_started", "s1", "task-one", run_id="shared-run", tool_use_id="one"
                    ),
                    task_record(
                        "task_started", "s2", "task-two", run_id="shared-run", tool_use_id="two"
                    ),
                    task_record("task_notification", "end", "task-one", run_id="shared-run"),
                ],
            )
        ]
    )
    assert [(e.source_ref, e.status) for e in doc.events if e.kind == "tool"] == [
        ("one", "completed"),
        ("two", "running"),
    ]
    assert [e.status for e in doc.events if e.title == "Background task"] == [
        "completed",
        "running",
    ]


def test_late_terminal_for_earlier_run_only_updates_that_run_call(tmp_path):
    doc = convert(
        [
            capture(
                tmp_path,
                [
                    assistant("root", [agent_call("one"), agent_call("two")]),
                    task_record(
                        "task_started", "s1", "same-task", run_id="run-one", tool_use_id="one"
                    ),
                    task_record(
                        "task_started", "s2", "same-task", run_id="run-two", tool_use_id="two"
                    ),
                    task_record("task_notification", "end", "same-task", run_id="run-one"),
                ],
            )
        ]
    )
    assert [(e.source_ref, e.status) for e in doc.events if e.kind == "tool"] == [
        ("one", "completed"),
        ("two", "running"),
    ]


def test_ambiguous_notification_without_run_id_does_not_choose_a_known_run(tmp_path):
    doc = convert(
        [
            capture(
                tmp_path,
                [
                    assistant("root", [agent_call("one"), agent_call("two")]),
                    task_record(
                        "task_started", "s1", "same-task", run_id="run-one", tool_use_id="one"
                    ),
                    task_record(
                        "task_started", "s2", "same-task", run_id="run-two", tool_use_id="two"
                    ),
                    task_record("task_notification", "end", "same-task", run_id=None),
                ],
            )
        ]
    )
    assert [e.status for e in doc.events if e.kind == "tool"] == ["running", "running"]
    assert any("ambiguous" in w.lower() for w in doc.runs[0].warnings)


def test_conflicting_lifecycle_tool_ids_do_not_complete_either_call(tmp_path):
    doc = convert(
        [
            capture(
                tmp_path,
                [
                    assistant("root", [agent_call("one"), agent_call("two")]),
                    task_record("task_started", "start", "same-task", tool_use_id="one"),
                    task_record("task_notification", "end", "same-task", tool_use_id="two"),
                ],
            )
        ]
    )
    assert [e.status for e in doc.events if e.kind == "tool"] == ["running", "running"]
    assert any("ambiguous" in w.lower() for w in doc.runs[0].warnings)


def test_repeated_task_run_with_conflicting_owners_does_not_select_either_call(tmp_path):
    doc = convert(
        [
            capture(
                tmp_path,
                [
                    assistant("root", [agent_call("launch"), agent_call("shared")]),
                    assistant(
                        "child",
                        [agent_call("shared")],
                        agent_id="child-1",
                        parent_tool_use_id="launch",
                    ),
                    task_record("task_started", "s1", "same-task", tool_use_id="shared"),
                    task_record(
                        "task_started",
                        "s2",
                        "same-task",
                        tool_use_id="shared",
                        parent_task_id="child-1",
                    ),
                    task_record("task_notification", "end", "same-task", tool_use_id="shared"),
                ],
            )
        ]
    )
    assert [e.status for e in doc.events if e.source_ref == "shared"] == ["running", "running"]
    assert any("ambiguous" in w.lower() for run in doc.runs for w in run.warnings)


def test_notification_without_owner_binding_does_not_choose_root_collision(tmp_path):
    doc = convert(
        [
            capture(
                tmp_path,
                [
                    assistant("root", [agent_call("launch"), agent_call("shared")]),
                    assistant(
                        "child",
                        [agent_call("shared")],
                        agent_id="child-1",
                        parent_tool_use_id="launch",
                    ),
                    task_record("task_notification", "end", "unstarted-task", tool_use_id="shared"),
                ],
            )
        ]
    )
    assert [e.status for e in doc.events if e.source_ref == "shared"] == ["running", "running"]
    assert any("ambiguous" in w.lower() for run in doc.runs for w in run.warnings)


def test_later_explicit_tool_result_is_not_overwritten_by_earlier_task_notification(tmp_path):
    failed = result("spawn")
    failed["message"]["content"][0]["is_error"] = True
    doc = convert(
        [
            capture(
                tmp_path,
                [
                    assistant("root", [agent_call("spawn")]),
                    task_record("task_started", "start", "task", tool_use_id="spawn"),
                    task_record("task_notification", "end", "task"),
                    failed,
                ],
            )
        ]
    )
    assert next(e for e in doc.events if e.kind == "tool").status == "failed"


@pytest.mark.parametrize("field", ["parent_task_id", "tool_use_id", "run_id"])
def test_invalid_explicit_task_binding_identity_is_not_treated_as_absent(tmp_path, field):
    start = task_record("task_started", "start", "task", tool_use_id="spawn")
    start[field] = "token=DROP-ME"
    path = capture(
        tmp_path,
        [
            assistant("root", [agent_call("spawn")]),
            start,
            task_record("task_notification", "end", "task", tool_use_id="spawn"),
        ],
    )
    with pytest.raises(TraceImportError) as error:
        convert([path])
    assert "DROP-ME" not in str(error.value) and str(path) not in str(error.value)


@pytest.mark.parametrize("include_start", [True, False])
def test_notification_can_use_unique_explicit_agent_result_binding(tmp_path, include_start):
    records = [
        assistant("root", [agent_call("spawn")]),
        result(
            "spawn",
            tool_use_result={
                "status": "async_launched",
                "agentId": "agent-1",
                "description": "",
                "prompt": "",
                "outputFile": "/synthetic/output",
            },
        ),
    ]
    if include_start:
        records.append(task_record("task_started", "start", "agent-1"))
    records.append(task_record("task_notification", "end", "agent-1"))
    doc = convert([capture(tmp_path, records)])
    assert next(e for e in doc.events if e.kind == "tool").status == "completed"


def test_notification_does_not_choose_between_resumed_agent_result_bindings(tmp_path):
    records = [assistant("root", [agent_call("one"), agent_call("two")])]
    for call_id in ("one", "two"):
        records.append(
            result(
                call_id,
                tool_use_result={
                    "status": "async_launched",
                    "agentId": "agent-1",
                    "description": "",
                    "prompt": "",
                    "outputFile": "/synthetic/output",
                },
            )
        )
    records.append(task_record("task_notification", "end", "agent-1"))
    doc = convert([capture(tmp_path, records)])
    assert [e.status for e in doc.events if e.kind == "tool"] == ["running", "running"]
    assert any("ambiguous" in w.lower() for w in doc.runs[0].warnings)


def test_cross_owner_resume_retains_all_scoped_task_bindings_before_parent_selection(tmp_path):
    records = [
        assistant("root", [agent_call("launch-owner"), agent_call("first-call")]),
        task_record(
            "task_started", "owner-start", "owner", run_id="owner-run", tool_use_id="launch-owner"
        ),
        task_record(
            "task_started",
            "child-start",
            "resumed-child",
            run_id="child-run-1",
            tool_use_id="first-call",
        ),
        assistant(
            "forwarded-owner",
            [agent_call("resume-call")],
            agent_id="owner",
            parent_tool_use_id="launch-owner",
        ),
        task_record(
            "task_started",
            "child-resume",
            "resumed-child",
            run_id="child-run-2",
            tool_use_id="resume-call",
            parent_task_id="owner",
        ),
        assistant(
            "forwarded-child",
            [{"type": "text", "text": "resumed child statement"}],
            agent_id="resumed-child",
            parent_tool_use_id="first-call",
        ),
    ]
    doc = convert([capture(tmp_path, records)])
    assert len(doc.runs) == 3
    child = doc.runs[2]
    assert child.parent_run_id is None and child.parent_event_id is None
    assert any("ambiguous" in warning.lower() for warning in child.warnings)
    owner_launch = next(event for event in doc.events if event.source_ref == "launch-owner")
    assert doc.runs[1].parent_event_id == owner_launch.id
