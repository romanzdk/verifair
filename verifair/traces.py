import json
import re
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


def _safe_text(value: str) -> str:
    if "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError("Text must not contain NUL or surrogate characters.")
    return value


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("Text must not be blank.")
    return value


def _timestamp(value: object) -> datetime:
    if isinstance(value, str):
        if not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
            r"(?:\.[0-9]{1,6})?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})",
            value,
        ):
            raise ValueError("Timestamps must be RFC3339 with a timezone.")
        try:
            value = datetime.fromisoformat(value.upper())
        except ValueError:
            raise ValueError("Invalid timestamp.") from None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Timestamps must be RFC3339 with a timezone.")
    try:
        return value.astimezone(UTC)
    except OverflowError, ValueError:
        raise ValueError("Invalid timestamp.") from None


Text = Annotated[str, AfterValidator(_safe_text)]
Slug = Annotated[Text, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
Timestamp = Annotated[datetime, BeforeValidator(_timestamp)]


class _TraceModel(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        hide_input_in_errors=True,
        allow_inf_nan=False,
        revalidate_instances="always",
    )


class TraceRun(_TraceModel):
    id: Slug
    agent: Literal["claude_code", "codex", "opencode", "pi", "other"]
    origin: Literal["recorded", "reconstructed"]
    source_format: Annotated[Text, Field(max_length=80), AfterValidator(_nonblank)]
    source_version: Annotated[Text, Field(max_length=80)] | None = None
    coverage: Literal["partial", "unknown"]
    warnings: Annotated[list[Annotated[Text, Field(max_length=500)]], Field(max_length=20)]
    basis: Annotated[Text, Field(max_length=2000)] | None = None
    parent_run_id: Slug | None = None
    parent_event_id: Slug | None = None

    @model_validator(mode="after")
    def provenance(self) -> Self:
        if self.origin == "reconstructed":
            if self.source_format != "manual":
                raise ValueError("Reconstructed runs require manual source format.")
            if self.basis is None or not self.basis.strip():
                raise ValueError("Reconstructed runs require a nonblank basis.")
        if self.parent_event_id is not None and self.parent_run_id is None:
            raise ValueError("A parent event requires a parent run.")
        return self


class TraceEvent(_TraceModel):
    id: Slug
    run_id: Slug
    ordinal: Annotated[int, Field(gt=0)]
    parent_id: Slug | None = None
    kind: Literal["assistant", "tool", "checkpoint", "summary"]
    title: Annotated[Text, Field(max_length=200), AfterValidator(_nonblank)]
    status: Literal["unknown", "running", "completed", "failed", "cancelled"]
    model: Annotated[Text, Field(max_length=100)] | None = None
    started_at: Timestamp | None = None
    ended_at: Timestamp | None = None
    exit_code: int | None = None
    input: Annotated[Text, Field(max_length=16000)] | None = None
    output: Annotated[Text, Field(max_length=16000)] | None = None
    explanation: Annotated[Text, Field(max_length=16000)] | None = None
    source_ref: Annotated[Text, Field(max_length=200)] | None = None
    truncated: bool = False

    @model_validator(mode="after")
    def telemetry(self) -> Self:
        if self.exit_code is not None and self.kind != "tool":
            raise ValueError("Only tool events can carry exit codes.")
        if self.started_at is not None and self.ended_at is not None:
            if self.ended_at < self.started_at:
                raise ValueError("Event end must not precede its start.")
        return self


class TraceDocument(_TraceModel):
    schema_version: Literal[1]
    runs: Annotated[list[TraceRun], Field(min_length=1, max_length=16)]
    events: Annotated[list[TraceEvent], Field(min_length=1, max_length=1000)]

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Schema version must be integer 1.")
        return value

    @model_validator(mode="after")
    def references_and_bounds(self) -> Self:
        runs = {run.id: run for run in self.runs}
        events = {event.id: event for event in self.events}
        if (
            len(runs) != len(self.runs)
            or len(events) != len(self.events)
            or runs.keys() & events.keys()
        ):
            raise ValueError("Trace IDs must be unique.")
        ordinals = set()
        populated = set()
        parents = {}
        for run in self.runs:
            if run.parent_run_id is not None and run.parent_run_id not in runs:
                raise ValueError("Unknown parent run.")
            if run.parent_event_id is not None:
                parent = events.get(run.parent_event_id)
                if parent is None or parent.run_id != run.parent_run_id:
                    raise ValueError("Parent event must belong to the parent run.")
            parents[run.id] = run.parent_event_id or run.parent_run_id
        for event in self.events:
            run = runs.get(event.run_id)
            if run is None:
                raise ValueError("Unknown event run.")
            populated.add(run.id)
            ordinal = (run.id, event.ordinal)
            if ordinal in ordinals:
                raise ValueError("Event ordinals must be unique within each run.")
            ordinals.add(ordinal)
            if event.parent_id is not None:
                parent = events.get(event.parent_id)
                if parent is None or parent.run_id != event.run_id:
                    raise ValueError("Event parent must belong to the same run.")
            parents[event.id] = event.parent_id or event.run_id
            if run.origin == "reconstructed":
                if any(
                    value is not None
                    for value in (event.model, event.started_at, event.ended_at, event.exit_code)
                ):
                    raise ValueError("Reconstructed events cannot carry observed telemetry.")
                if event.kind == "tool" and event.status != "unknown":
                    raise ValueError("Reconstructed tool status must remain unknown.")
            elif event.kind == "summary":
                raise ValueError("Summary events require a reconstructed run.")
        if populated != runs.keys():
            raise ValueError("Each run must contain events.")
        for node in parents:
            path = set()
            while node is not None:
                if node in path:
                    raise ValueError("Trace parent links must not contain cycles.")
                path.add(node)
                if len(path) > 16:
                    raise ValueError("Combined trace depth must not exceed 16.")
                node = parents[node]
        if len(canonical_json(self).encode("utf-8")) > 524288:
            raise ValueError("Trace document exceeds the canonical UTF-8 byte limit.")
        return self


class TraceBinding(_TraceModel):
    version: Annotated[int, Field(ge=0)]
    plan_revision: Annotated[int, Field(ge=0)] | None = None
    commit: Text | None = None


class StoredTrace(_TraceModel):
    binding: TraceBinding
    document: TraceDocument


def canonical_json(value: TraceDocument | StoredTrace) -> str:
    return json.dumps(
        value.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def validate_document(value: object) -> TraceDocument:
    try:
        return TraceDocument.model_validate(value)
    except ValidationError:
        raise ValueError("Invalid execution trace document.") from None


def execution_tree(trace: TraceDocument) -> list[dict]:
    nodes = {run.id: {"run": run, "event": None, "children": []} for run in trace.runs}
    for event in trace.events:
        duration = None
        if event.started_at is not None and event.ended_at is not None:
            duration = f"{(event.ended_at - event.started_at).total_seconds():g} s"
        nodes[event.id] = {"run": None, "event": event, "duration": duration, "children": []}
    for event in sorted(trace.events, key=lambda event: event.ordinal):
        nodes[event.parent_id or event.run_id]["children"].append(nodes[event.id])
    roots = []
    for run in trace.runs:
        parent = run.parent_event_id or run.parent_run_id
        if parent is None:
            roots.append(nodes[run.id])
        else:
            nodes[parent]["children"].append(nodes[run.id])
    return roots
