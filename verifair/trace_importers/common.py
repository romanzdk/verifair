import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from verifair.traces import TraceDocument, TraceEvent, TraceRun, validate_document

MAX_FILES = 16
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_RECORD_BYTES = 1024 * 1024
MAX_NESTING = 64


class TraceImportError(ValueError):
    pass


@dataclass(frozen=True)
class ImportOptions:
    include_details: bool = False
    source_version: str | None = None
    last_events: int | None = None
    leaf: str | None = None

    def __post_init__(self):
        if self.last_events is not None and (
            type(self.last_events) is not int or not 1 <= self.last_events <= 1000
        ):
            raise TraceImportError("Event selection must be between 1 and 1000.")
        if self.source_version is not None and (
            not isinstance(self.source_version, str)
            or len(self.source_version) > 80
            or any(c == "\x00" or 0xD800 <= ord(c) <= 0xDFFF for c in self.source_version)
        ):
            raise TraceImportError("Invalid claimed source version.")


@dataclass
class ReadBudget:
    used: int = 0

    def consume(self, count: int) -> None:
        self.used += count
        if self.used > MAX_INPUT_BYTES:
            raise TraceImportError("Explicit inputs exceed the combined byte limit.")


@contextmanager
def _open_regular(path: Path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise TraceImportError("Inputs must be regular local files.")
            yield stream
    except OSError:
        raise TraceImportError("Cannot read a regular local input file.") from None


def check_inputs(paths: tuple[Path, ...]) -> None:
    if not 1 <= len(paths) <= MAX_FILES:
        raise TraceImportError("Supply between 1 and 16 explicit input files.")
    try:
        total = 0
        for path in paths:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise TraceImportError("Inputs must be regular local files.")
            total += info.st_size
            if total > MAX_INPUT_BYTES:
                raise TraceImportError("Explicit inputs exceed the combined byte limit.")
    except OSError:
        raise TraceImportError("Cannot inspect an explicit input file.") from None


def _parse(data: bytes) -> object:
    try:
        text = data.decode("utf-8")
        depth = 0
        quoted = escaped = False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                if depth > MAX_NESTING:
                    raise TraceImportError("Input JSON nesting exceeds the supported limit.")
            elif char in "]}":
                depth -= 1
        return json.loads(text, parse_constant=_reject_constant)
    except UnicodeError, ValueError, RecursionError:
        raise TraceImportError("Input must contain bounded valid UTF-8 JSON.") from None


def _reject_constant(value: str):
    raise TraceImportError("Non-finite JSON numbers are unsupported.")


def read_json(path: Path, *, budget: ReadBudget | None = None) -> object:
    budget = budget if budget is not None else ReadBudget()
    chunks = []
    with _open_regular(path) as stream:
        while chunk := stream.read(min(65536, MAX_INPUT_BYTES - budget.used + 1)):
            budget.consume(len(chunk))
            chunks.append(chunk)
    return _parse(b"".join(chunks))


def read_jsonl(path: Path, *, budget: ReadBudget | None = None) -> Iterator[dict]:
    budget = budget if budget is not None else ReadBudget()
    with _open_regular(path) as stream:
        while record := stream.readline(min(MAX_RECORD_BYTES, MAX_INPUT_BYTES - budget.used) + 1):
            budget.consume(len(record))
            if len(record) > MAX_RECORD_BYTES:
                raise TraceImportError("A JSONL record exceeds the byte limit.")
            if not record.strip():
                continue
            value = _parse(record)
            if not isinstance(value, dict):
                raise TraceImportError("JSONL records must be objects.")
            yield value


_LINE_BOUNDARIES = r"\r\n\v\f\x1c-\x1e\x85\u2028\u2029"
_AUTHORIZATION = re.compile(
    rf"""(?i)(["']?authorization["']?\s*[:=][^\S{_LINE_BOUNDARIES}]*)"""
    rf"""(?:\[REDACTED\]|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\S[^{_LINE_BOUNDARIES}]*)"""
)
_CREDENTIAL = re.compile(
    r"""(?i)(["']?(?:authorization(?=["']?\s*[:=]\s*["'])|"""
    r"""password|token|api[_-]?key|secret)["']?\s*[:=]\s*)"""
    r"""(?:\[REDACTED\]|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}\]]+)"""
)
_BEARER = re.compile(r"(?i)\bbearer\s+(?:\[REDACTED\]|[^\s\"',;}\]]+)")


def sanitize_text(
    value: object, options: ImportOptions, *, limit: int = 16000
) -> tuple[str | None, bool]:
    if not options.include_details or not isinstance(value, str):
        return None, False
    text = _AUTHORIZATION.sub(r"\1[REDACTED]", value)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _CREDENTIAL.sub(r"\1[REDACTED]", text)
    text = "".join(c for c in text if c != "\x00" and not 0xD800 <= ord(c) <= 0xDFFF)
    if len(text) > limit:
        marker = "[truncated]"
        return text[: max(0, limit - len(marker))] + marker[:limit], True
    return text, False


def finalize(
    runs: list[TraceRun], events: list[TraceEvent], options: ImportOptions
) -> TraceDocument:
    selected = events[-options.last_events :] if options.last_events is not None else events
    event_ids = {event.id for event in selected}
    populated = {event.run_id for event in selected}
    retained = [run for run in runs if run.id in populated]
    run_ids = {run.id for run in retained}
    dropped = set()
    cleaned_events = []
    for event in selected:
        updates = {}
        if event.parent_id is not None and event.parent_id not in event_ids:
            updates["parent_id"] = None
            dropped.add(event.run_id)
        truncated = event.truncated
        for field in ("input", "output", "explanation"):
            updates[field], shortened = sanitize_text(getattr(event, field), options)
            truncated |= shortened
        updates["truncated"] = truncated
        cleaned_events.append(event.model_copy(update=updates))
    cleaned_runs = []
    for run in retained:
        updates = {}
        if run.parent_run_id is not None and run.parent_run_id not in run_ids:
            updates.update(parent_run_id=None, parent_event_id=None)
            dropped.add(run.id)
        elif run.parent_event_id is not None and run.parent_event_id not in event_ids:
            updates["parent_event_id"] = None
            dropped.add(run.id)
        warnings = []
        if options.last_events is not None:
            warnings.append("Explicit event selection is partial; omitted history is unavailable.")
            updates["coverage"] = "partial"
        if run.id in dropped:
            warnings.append("Unavailable parent references were removed; coverage is partial.")
            updates["coverage"] = "partial"
        if run.source_version is None:
            warnings.append("Producer version was not supplied; compatibility is unverified.")
        updates["warnings"] = run.warnings[: 20 - len(warnings)] + warnings
        cleaned_runs.append(run.model_copy(update=updates))
    try:
        return validate_document(
            {"schema_version": 1, "runs": cleaned_runs, "events": cleaned_events}
        )
    except ValueError:
        raise TraceImportError(
            "Imported trace is empty, invalid or exceeds document limits."
        ) from None
