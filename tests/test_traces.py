import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from verifair.traces import (
    StoredTrace,
    TraceBinding,
    TraceDocument,
    TraceEvent,
    TraceRun,
    canonical_json,
    validate_document,
)


@pytest.fixture
def document():
    return {
        "schema_version": 1,
        "runs": [
            {
                "id": "main",
                "agent": "codex",
                "origin": "recorded",
                "source_format": "codex-jsonl",
                "coverage": "partial",
                "warnings": [],
            }
        ],
        "events": [
            {
                "id": "call-1",
                "run_id": "main",
                "ordinal": 1,
                "kind": "tool",
                "title": "Run tests — ověření",
                "status": "unknown",
            }
        ],
    }


def wire(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def test_canonical_round_trip_and_unknown_telemetry_stays_absent(document):
    trace = validate_document(document)
    assert isinstance(trace, TraceDocument)
    assert isinstance(trace.runs[0], TraceRun)
    assert isinstance(trace.events[0], TraceEvent)
    expected = deepcopy(document)
    expected["events"][0]["truncated"] = False
    encoded = canonical_json(trace)
    assert encoded == wire(expected)
    assert "ověření" in encoded
    assert validate_document(json.loads(encoded)) == trace
    for field in ("started_at", "ended_at", "model", "exit_code"):
        assert field not in json.loads(encoded)["events"][0]


def test_canonical_key_order_does_not_depend_on_input_order(document):
    reordered = dict(reversed(list(document.items())))
    reordered["runs"] = [dict(reversed(list(document["runs"][0].items())))]
    reordered["events"] = [dict(reversed(list(document["events"][0].items())))]
    assert canonical_json(validate_document(document)) == canonical_json(
        validate_document(reordered)
    )


def test_stored_trace_envelope_round_trip(document):
    stored = StoredTrace(
        binding=TraceBinding(version=2, plan_revision=1, commit="abc123"),
        document=validate_document(document),
    )
    encoded = canonical_json(stored)
    assert json.loads(encoded)["binding"] == {
        "version": 2,
        "plan_revision": 1,
        "commit": "abc123",
    }
    assert StoredTrace.model_validate_json(encoded) == stored
    assert json.loads(
        canonical_json(StoredTrace(binding=TraceBinding(version=1), document=stored.document))
    )["binding"] == {"version": 1}


@pytest.mark.parametrize("level", ["document", "run", "event", "binding", "stored"])
def test_unknown_fields_are_forbidden(document, level):
    value = document
    model = TraceDocument
    if level == "run":
        value, model = document["runs"][0], TraceRun
    elif level == "event":
        value, model = document["events"][0], TraceEvent
    elif level == "binding":
        value, model = {"version": 1}, TraceBinding
    elif level == "stored":
        value, model = {"binding": {"version": 1}, "document": document}, StoredTrace
    with pytest.raises(ValidationError):
        model.model_validate({**value, "extra": "not metadata"})


@pytest.mark.parametrize("value", [True, False, "1", 1.0, 0, 2, None])
def test_schema_version_is_exactly_integer_one(document, value):
    document["schema_version"] = value
    with pytest.raises(ValueError):
        validate_document(document)


@pytest.mark.parametrize("value", [True, False, "1", 1.0, 0, -1, None, float("nan"), float("inf")])
def test_ordinals_are_strict_positive_integers(document, value):
    document["events"][0]["ordinal"] = value
    with pytest.raises(ValueError):
        validate_document(document)


@pytest.mark.parametrize("field", ["version", "plan_revision"])
@pytest.mark.parametrize("value", [True, "1", 1.0, -1, float("nan"), float("inf"), -float("inf")])
def test_binding_counters_are_strict_nonnegative_integers(field, value):
    with pytest.raises(ValidationError):
        TraceBinding.model_validate({"version": 1, field: value})


@pytest.mark.parametrize("value", [True, "0", 0.0, float("nan"), float("inf"), -float("inf")])
def test_exit_codes_are_strict_signed_integers(document, value):
    document["events"][0]["exit_code"] = value
    with pytest.raises(ValueError):
        validate_document(document)


@pytest.mark.parametrize("value", [-9, 0, 1])
def test_recorded_tool_exit_codes_are_preserved(document, value):
    document["events"][0]["exit_code"] = value
    assert validate_document(document).events[0].exit_code == value


@pytest.mark.parametrize("kind", ["assistant", "checkpoint", "summary"])
def test_only_tools_have_exit_codes(document, kind):
    document["events"][0].update(kind=kind, exit_code=0)
    with pytest.raises(ValidationError):
        TraceEvent.model_validate(document["events"][0])


@pytest.mark.parametrize("value", ["true", 1, 0, None])
def test_truncated_flag_is_a_strict_boolean(document, value):
    document["events"][0]["truncated"] = value
    with pytest.raises(ValueError):
        validate_document(document)


@pytest.mark.parametrize(
    ("level", "field", "value"),
    [
        ("run", "agent", "chatgpt"),
        ("run", "origin", "inferred"),
        ("run", "coverage", "complete"),
        ("run", "source_format", " \n"),
        ("event", "kind", "metadata"),
        ("event", "title", " \t"),
        ("event", "status", "success"),
        ("event", "input", {"metadata": "unsupported"}),
        ("event", "output", 123),
    ],
)
def test_invalid_field_contracts(document, level, field, value):
    document[f"{level}s"][0][field] = value
    with pytest.raises(ValueError):
        validate_document(document)


@pytest.mark.parametrize("level", ["run", "event"])
@pytest.mark.parametrize("value", ["", "-start", "a/b", "white space", "č", "a\n", "x" * 101])
def test_ids_are_bounded_slugs(document, level, value):
    document[f"{level}s"][0]["id"] = value
    with pytest.raises(ValueError):
        validate_document(document)


def test_maximum_length_ids_and_references_are_accepted(document):
    run_id, event_id = "r" * 100, "e" * 100
    document["runs"][0]["id"] = run_id
    document["events"][0].update(id=event_id, run_id=run_id)
    document["events"].append(
        {**document["events"][0], "id": "A.z_9-", "ordinal": 2, "parent_id": event_id}
    )
    trace = validate_document(document)
    assert trace.runs[0].id == run_id
    assert trace.events[1].parent_id == event_id


TEXT_FIELDS = [
    ("run", "id", 100),
    ("run", "agent", None),
    ("run", "origin", None),
    ("run", "source_format", 80),
    ("run", "source_version", 80),
    ("run", "coverage", None),
    ("run", "warnings", 500),
    ("run", "basis", 2000),
    ("run", "parent_run_id", 100),
    ("run", "parent_event_id", 100),
    ("event", "id", 100),
    ("event", "run_id", 100),
    ("event", "parent_id", 100),
    ("event", "kind", None),
    ("event", "title", 200),
    ("event", "status", None),
    ("event", "model", 100),
    ("event", "started_at", None),
    ("event", "ended_at", None),
    ("event", "input", 16000),
    ("event", "output", 16000),
    ("event", "explanation", 16000),
    ("event", "source_ref", 200),
]


@pytest.mark.parametrize(("level", "field", "limit"), TEXT_FIELDS)
@pytest.mark.parametrize("bad_text", ["private\x00text", "private\ud800text", "private\udffftext"])
def test_all_text_fields_reject_nul_and_surrogates(document, level, field, limit, bad_text):
    document[f"{level}s"][0][field] = [bad_text] if field == "warnings" else bad_text
    with pytest.raises(ValueError):
        validate_document(document)


@pytest.mark.parametrize("bad_text", ["private\x00text", "private\ud800text", "private\udffftext"])
def test_binding_commit_rejects_unsafe_text(bad_text):
    with pytest.raises(ValidationError):
        TraceBinding(version=1, commit=bad_text)


@pytest.mark.parametrize(
    ("level", "field", "limit"),
    [
        row
        for row in TEXT_FIELDS
        if row[2] is not None
        and row[1] not in {"id", "run_id", "parent_id", "parent_run_id", "parent_event_id"}
    ],
)
def test_multibyte_text_character_bounds(document, level, field, limit):
    value = "ž" * limit
    document[f"{level}s"][0][field] = [value] if field == "warnings" else value
    validate_document(document)
    document[f"{level}s"][0][field] = [value + "ž"] if field == "warnings" else value + "ž"
    with pytest.raises(ValueError):
        validate_document(document)


def test_warning_count_bound(document):
    document["runs"][0]["warnings"] = ["partial"] * 20
    validate_document(document)
    document["runs"][0]["warnings"].append("extra")
    with pytest.raises(ValueError):
        validate_document(document)


def test_opaque_prose_and_source_references_are_preserved_as_text(document):
    event = document["events"][0]
    event.update(
        explanation="Provider summary: checked the public interface.",
        input="<script>alert('example')</script> **literal text**",
        output="[literal](file:///tmp/example)",
        source_ref="/tmp/capture.jsonl#call-1",
        truncated=True,
    )
    parsed = json.loads(canonical_json(validate_document(document)))["events"][0]
    for field in ("explanation", "input", "output", "source_ref", "truncated"):
        assert parsed[field] == event[field]


def two_runs(document):
    document["runs"].append({**document["runs"][0], "id": "child"})
    document["events"].append({**document["events"][0], "id": "call-2", "run_id": "child"})
    return document


@pytest.mark.parametrize(
    "case",
    [
        "duplicate-run",
        "duplicate-event",
        "duplicate-ordinal",
        "shared-id",
        "orphan-event-run",
        "orphan-event-parent",
        "cross-run-parent",
        "orphan-run-parent",
        "orphan-run-event",
        "event-without-run-parent",
        "wrong-parent-run",
        "run-cycle",
        "event-cycle",
        "mixed-cycle",
        "empty-run",
        "no-runs",
        "no-events",
    ],
)
def test_references_uniqueness_and_nonempty_runs(document, case):
    two_runs(document)
    main, child = document["runs"]
    first, second = document["events"]
    if case == "duplicate-run":
        child["id"] = main["id"]
    elif case == "duplicate-event":
        second["id"] = first["id"]
    elif case == "duplicate-ordinal":
        second["run_id"] = main["id"]
        document["runs"].pop()
    elif case == "shared-id":
        first["id"] = main["id"]
    elif case == "orphan-event-run":
        second["run_id"] = "missing"
    elif case == "orphan-event-parent":
        first["parent_id"] = "missing"
    elif case == "cross-run-parent":
        second["parent_id"] = first["id"]
    elif case == "orphan-run-parent":
        child["parent_run_id"] = "missing"
    elif case == "orphan-run-event":
        child.update(parent_run_id="main", parent_event_id="missing")
    elif case == "event-without-run-parent":
        child["parent_event_id"] = first["id"]
    elif case == "wrong-parent-run":
        child.update(parent_run_id="main", parent_event_id=second["id"])
    elif case == "run-cycle":
        main["parent_run_id"], child["parent_run_id"] = "child", "main"
    elif case == "event-cycle":
        first["parent_id"] = first["id"]
    elif case == "mixed-cycle":
        main.update(parent_run_id="child", parent_event_id=second["id"])
        child.update(parent_run_id="main", parent_event_id=first["id"])
    elif case == "empty-run":
        document["events"].pop()
    elif case == "no-runs":
        document["runs"] = []
    elif case == "no-events":
        document["events"] = []
    with pytest.raises(ValueError):
        validate_document(document)


def test_ordinals_are_per_run_and_do_not_reorder_events(document):
    two_runs(document)
    document["events"][0]["ordinal"] = 7
    document["events"].append({**document["events"][0], "id": "earlier", "ordinal": 1})
    trace = validate_document(document)
    assert [(e.run_id, e.ordinal) for e in trace.events] == [("main", 7), ("child", 1), ("main", 1)]


def test_run_and_event_count_bounds(document):
    run, event = document["runs"][0], document["events"][0]
    document["runs"] = [{**run, "id": f"run-{i}"} for i in range(16)]
    document["events"] = [{**event, "id": f"event-{i}", "run_id": f"run-{i}"} for i in range(16)]
    validate_document(document)
    document["runs"].append({**run, "id": "run-16"})
    document["events"].append({**event, "id": "event-16", "run_id": "run-16"})
    with pytest.raises(ValueError):
        validate_document(document)


def test_event_count_bounds(document):
    event = document["events"][0]
    document["events"] = [{**event, "id": f"e-{i}", "ordinal": i + 1} for i in range(1000)]
    validate_document(document)
    document["events"].append({**event, "id": "e-1000", "ordinal": 1001})
    with pytest.raises(ValueError):
        validate_document(document)


@pytest.mark.parametrize("nesting", ["events", "runs", "mixed"])
def test_combined_depth_sixteen_is_accepted_seventeen_rejected(document, nesting):
    run, event = document["runs"][0], document["events"][0]
    if nesting == "events":
        document["events"] = [
            {**event, "id": f"e-{i}", "ordinal": i + 1, "parent_id": f"e-{i - 1}" if i else None}
            for i in range(15)
        ]
        validate_document(document)
        document["events"].append({**event, "id": "e-15", "ordinal": 16, "parent_id": "e-14"})
    elif nesting == "runs":
        document["runs"] = [
            {**run, "id": f"r-{i}", "parent_run_id": f"r-{i - 1}" if i else None} for i in range(15)
        ]
        document["events"] = [{**event, "id": f"e-{i}", "run_id": f"r-{i}"} for i in range(15)]
        validate_document(document)
        document["runs"].append({**run, "id": "r-15", "parent_run_id": "r-14"})
        document["events"].append({**event, "id": "e-15", "run_id": "r-15"})
    else:
        document["runs"] = [
            {
                **run,
                "id": f"r-{i}",
                "parent_run_id": f"r-{i - 1}" if i else None,
                "parent_event_id": f"e-{i - 1}" if i else None,
            }
            for i in range(8)
        ]
        document["events"] = [{**event, "id": f"e-{i}", "run_id": f"r-{i}"} for i in range(8)]
        validate_document(document)
        document["events"].append(
            {**event, "id": "extra", "run_id": "r-7", "ordinal": 2, "parent_id": "e-7"}
        )
    with pytest.raises(ValueError):
        validate_document(document)


def test_parent_timestamps_need_not_nest(document):
    two_runs(document)
    document["runs"][1].update(parent_run_id="main", parent_event_id="call-1")
    document["events"][0].update(started_at="2026-10-25T12:00:00Z", ended_at="2026-10-25T12:01:00Z")
    document["events"][1].update(started_at="2026-10-24T12:00:00Z", ended_at="2026-10-26T12:01:00Z")
    validate_document(document)


@pytest.mark.parametrize(
    "value",
    [
        "2026-10-25T12:00:00",
        "2026-10-25",
        "2026-10-25 12:00:00+00:00",
        "2026-10-25T12:00:00+0000",
        "2026-10-25T12:00Z",
        "2026-13-25T12:00:00Z",
        "2026-10-25T12:00:00+00:60",
        "2026-10-25T12:00:00-00:99",
        "0001-01-01T00:00:00+01:00",
        "9999-12-31T23:59:59-01:00",
        datetime(2026, 10, 25),
        1792929600,
        True,
        float("nan"),
        float("inf"),
    ],
)
@pytest.mark.parametrize("field", ["started_at", "ended_at"])
def test_timestamps_require_rfc3339_with_timezone(document, field, value):
    document["events"][0][field] = value
    with pytest.raises(ValueError):
        validate_document(document)


def test_timestamps_normalize_to_utc_and_round_trip(document):
    document["events"][0].update(
        started_at="2026-10-25T12:00:00.123456+02:00",
        ended_at=datetime(2026, 10, 25, 12, 1, tzinfo=timezone(timedelta(hours=2))),
    )
    trace = validate_document(document)
    assert trace.events[0].started_at == datetime(2026, 10, 25, 10, 0, 0, 123456, tzinfo=UTC)
    event = json.loads(canonical_json(trace))["events"][0]
    assert event["started_at"] == "2026-10-25T10:00:00.123456Z"
    assert event["ended_at"] == "2026-10-25T10:01:00Z"
    assert validate_document(json.loads(canonical_json(trace))) == trace


def test_submicrosecond_reversed_endpoints_are_rejected(document):
    event = document["events"][0]
    event.update(
        started_at="2026-10-25T12:00:00.1234569Z",
        ended_at="2026-10-25T12:00:00.1234561Z",
    )
    with pytest.raises(ValidationError) as caught:
        TraceEvent.model_validate(event)
    assert event["started_at"] not in str(caught.value)
    assert event["ended_at"] not in str(caught.value)


@pytest.mark.parametrize("field", ["started_at", "ended_at"])
@pytest.mark.parametrize("fraction", ["1234567", "1234560", "123456789"])
def test_timestamp_precision_above_six_digits_is_rejected(document, field, fraction):
    document["events"][0][field] = f"2026-10-25T12:00:00.{fraction}Z"
    with pytest.raises(ValueError) as caught:
        validate_document(document)
    assert str(caught.value) == "Invalid execution trace document."


@pytest.mark.parametrize(
    ("fraction", "canonical_fraction"),
    [
        ("1", "100000"),
        ("12", "120000"),
        ("123", "123000"),
        ("1234", "123400"),
        ("12345", "123450"),
        ("123456", "123456"),
    ],
)
def test_supported_fractional_precision_survives_utc_normalization(
    document, fraction, canonical_fraction
):
    document["events"][0]["started_at"] = f"2026-10-25T12:00:00.{fraction}+02:00"
    trace = validate_document(document)
    encoded = canonical_json(trace)
    event = json.loads(encoded)["events"][0]
    assert event["started_at"] == f"2026-10-25T10:00:00.{canonical_fraction}Z"
    assert "ended_at" not in event
    assert validate_document(json.loads(encoded)) == trace


def test_end_cannot_precede_start(document):
    document["events"][0].update(started_at="2026-10-25T12:00:00Z", ended_at="2026-10-25T11:59:59Z")
    with pytest.raises(ValueError):
        validate_document(document)


@pytest.mark.parametrize("field", ["started_at", "ended_at"])
def test_single_timestamp_is_retained_without_inventing_other_endpoint(document, field):
    document["events"][0][field] = "2026-10-25T12:00:00Z"
    event = json.loads(canonical_json(validate_document(document)))["events"][0]
    assert event[field] == "2026-10-25T12:00:00Z"
    assert ("ended_at" if field == "started_at" else "started_at") not in event


@pytest.mark.parametrize("basis", [None, "", " \t\n"])
def test_reconstruction_requires_nonblank_basis(document, basis):
    document["runs"][0].update(origin="reconstructed", source_format="manual", basis=basis)
    with pytest.raises(ValueError):
        validate_document(document)


def test_reconstruction_requires_manual_source(document):
    document["runs"][0].update(origin="reconstructed", basis="Author's recollection")
    with pytest.raises(ValueError):
        validate_document(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", "codex"),
        ("started_at", "2026-10-25T12:00:00Z"),
        ("ended_at", "2026-10-25T12:00:00Z"),
        ("exit_code", 0),
    ],
)
def test_reconstruction_cannot_claim_observed_telemetry(document, field, value):
    document["runs"][0].update(origin="reconstructed", source_format="manual", basis="Author notes")
    document["events"][0][field] = value
    with pytest.raises(ValueError):
        validate_document(document)


@pytest.mark.parametrize("status", ["running", "completed", "failed", "cancelled"])
def test_reconstructed_tools_remain_unknown(document, status):
    document["runs"][0].update(origin="reconstructed", source_format="manual", basis="Author notes")
    document["events"][0]["status"] = status
    with pytest.raises(ValueError):
        validate_document(document)


def test_reconstructed_summary_preserves_labelled_prose(document):
    document["runs"][0].update(origin="reconstructed", source_format="manual", basis="Author notes")
    document["events"][0].update(kind="summary", explanation="Reconstruction: I reviewed the API.")
    trace = validate_document(document)
    assert trace.runs[0].origin == "reconstructed"
    assert trace.events[0].explanation == "Reconstruction: I reviewed the API."


def test_reconstructed_tool_with_unknown_status_is_accepted(document):
    document["runs"][0].update(origin="reconstructed", source_format="manual", basis="Author notes")
    trace = validate_document(document)
    assert trace.events[0].status == "unknown"
    assert trace.events[0].exit_code is None


def test_recorded_runs_cannot_have_summary_events(document):
    document["events"][0]["kind"] = "summary"
    with pytest.raises(ValueError):
        validate_document(document)


def document_with_byte_size(document, size):
    event = document["events"][0]
    document["events"] = [
        {**event, "id": f"e-{i}", "ordinal": i + 1, "output": "", "truncated": False}
        for i in range(20)
    ]
    remaining = size - len(wire(document).encode("utf-8"))
    for event in document["events"]:
        count = min(remaining // 2, 16000)
        event["output"] = "é" * count
        remaining -= count * 2
        if remaining == 1 and count < 16000:
            event["output"] += "x"
            remaining = 0
    assert remaining == 0
    assert len(wire(document).encode("utf-8")) == size
    return document


def test_exact_canonical_utf8_budget_is_accepted(document):
    value = document_with_byte_size(document, 524288)
    assert len(canonical_json(validate_document(value)).encode("utf-8")) == 524288


def test_one_byte_above_canonical_utf8_budget_is_rejected(document):
    value = document_with_byte_size(document, 524289)
    with pytest.raises(ValueError):
        validate_document(value)


@pytest.mark.parametrize(
    "value", [None, [], "{}", {"schema_version": True}, {"private-value": "secret"}]
)
def test_document_errors_are_static_and_do_not_echo_rejected_values(value):
    with pytest.raises(ValueError) as caught:
        validate_document(value)
    assert str(caught.value) == "Invalid execution trace document."


def test_model_errors_hide_rejected_field_values(document):
    document["events"][0]["ordinal"] = "private-value"
    with pytest.raises(ValidationError) as caught:
        TraceDocument.model_validate(document)
    assert "private-value" not in str(caught.value)


def test_preexisting_model_instances_are_revalidated(document):
    trace = validate_document(document)
    trace.events[0].title = ""
    with pytest.raises(ValueError):
        validate_document(trace)
