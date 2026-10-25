import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from verifair.trace_importers.common import (
    ImportOptions,
    ReadBudget,
    TraceImportError,
    finalize,
    read_json,
    sanitize_text,
)
from verifair.traces import TraceDocument, TraceEvent, TraceRun


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}", value):
        raise TraceImportError("Invalid or oversized OpenCode source identifier.")
    return value


def _object(value: object) -> dict:
    if not isinstance(value, dict):
        raise TraceImportError("Malformed OpenCode v2 export; supply a typed session export.")
    return value


def _millis(value: object) -> datetime | None:
    if value is None:
        return None
    if type(value) is not int:
        raise TraceImportError("OpenCode timestamps must be integer epoch milliseconds.")
    try:
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)
    except OverflowError:
        raise TraceImportError("OpenCode timestamp is outside the supported range.") from None


def _model(value: object) -> str | None:
    if value is None:
        return None
    value = _object(value)
    parts = [value.get("providerID"), value.get("id")]
    if any(not isinstance(part, str) or not part.strip() for part in parts):
        raise TraceImportError("OpenCode model fields must be nonblank strings.")
    model = "/".join(parts)
    if len(model) > 100:
        raise TraceImportError("OpenCode model identifier exceeds the supported limit.")
    sanitized, _ = sanitize_text(model, ImportOptions(include_details=True), limit=100)
    if not model.isprintable() or sanitized != model:
        raise TraceImportError("OpenCode model fields contain unsafe text or credentials.")
    return model


def _reference(*parts: str) -> str:
    value = "/".join(parts)
    if len(value) > 200:
        raise TraceImportError("OpenCode source reference exceeds the supported limit.")
    return value


def _warning(run: TraceRun, text: str) -> None:
    if text not in run.warnings:
        run.warnings.append(text)


def _text(values: list, options: ImportOptions) -> tuple[str | None, bool]:
    parts = []
    truncated = False
    for value in values:
        text, shortened = sanitize_text(value, options)
        truncated |= shortened
        if text is not None:
            parts.append(text)
    text, shortened = sanitize_text("\n".join(parts), options)
    return (text if parts else None), truncated or shortened


def convert(paths: tuple[Path, ...], options: ImportOptions) -> TraceDocument:
    if not 1 <= len(paths) <= 16:
        raise TraceImportError("Supply between 1 and 16 explicit OpenCode exports.")
    if options.leaf is not None:
        raise TraceImportError("OpenCode exports already select a session; omit the leaf option.")
    if options.source_version is not None and not re.fullmatch(
        r"v?2\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", options.source_version
    ):
        raise TraceImportError("Unsupported OpenCode producer version; supply a v2 session export.")

    budget = ReadBudget()
    sessions = {}
    order = []
    messages = {}
    for path in paths:
        data = _object(read_json(path, budget=budget))
        info = _object(data.get("info"))
        sid = _identifier(info.get("id"))
        if not sid.startswith("ses_") or not isinstance(data.get("messages"), list):
            raise TraceImportError(
                "Unsupported OpenCode format family; supply a v2 session export."
            )
        if sid in sessions and sessions[sid].get("parentID") != info.get("parentID"):
            raise TraceImportError("Conflicting OpenCode session parent identifiers.")
        sessions[sid] = info
        for message in data["messages"]:
            message = _object(message)
            if "type" not in message and ("parts" in message or "info" in message):
                raise TraceImportError(
                    "OpenCode v1 message wrappers are unsupported; export with v2."
                )
            mid = _identifier(message.get("id"))
            if not mid.startswith("msg_"):
                raise TraceImportError("Malformed OpenCode v2 message identifier.")
            key = (sid, mid)
            if key not in messages:
                order.append(key)
            messages[key] = message

    runs = {}
    for sid, info in sessions.items():
        run = TraceRun(
            id=f"oc-r{len(runs) + 1}",
            agent="opencode",
            origin="recorded",
            source_format="opencode-v2-export",
            source_version=options.source_version,
            coverage="partial",
            warnings=[
                "Export contains settled projected messages only; unsettled history is omitted.",
                "Export excludes recursive child history; supply related exports explicitly.",
            ],
        )
        if options.source_version not in (None, "2.1.3", "v2.1.3"):
            _warning(
                run, "Producer version is untested; compatibility is pinned to OpenCode 2.1.3."
            )
        if info.get("fork") is not None:
            _warning(run, "Session has a fork relation; it is not a delegation edge.")
        if info.get("revert") is not None:
            _warning(
                run, "Export has revert metadata; projected history is not an active-branch log."
            )
        runs[sid] = run

    events = []
    ordinals = dict.fromkeys(sessions, 0)
    child_calls = {}

    def add(sid, mid, *, input=None, output=None, explanation=None, **fields):
        truncated = fields.pop("truncated", False)
        details = {}
        for name, values in (("input", input), ("output", output), ("explanation", explanation)):
            details[name], shortened = _text(values or [], options)
            truncated |= shortened
        ordinals[sid] += 1
        try:
            event = TraceEvent(
                id=f"oc-e{len(events) + 1}",
                run_id=runs[sid].id,
                ordinal=ordinals[sid],
                source_ref=_reference(sid, mid),
                truncated=truncated,
                **details,
                **fields,
            )
        except ValidationError:
            raise TraceImportError("Malformed OpenCode event fields or timing.") from None
        events.append(event)
        return event

    for sid, mid in order:
        message = messages[sid, mid]
        run = runs[sid]
        kind = message.get("type")
        if kind in (
            "user",
            "synthetic",
            "system",
            "skill",
            "agent-switched",
            "location-switched",
            "idle",
        ):
            continue
        if kind not in ("assistant", "compaction", "shell", "model-switched"):
            _warning(run, "Unknown message kind was omitted; coverage is partial.")
            continue
        time = _object(message.get("time"))
        created = _millis(time.get("created"))
        completed = _millis(time.get("completed"))
        if kind == "model-switched":
            add(
                sid,
                mid,
                kind="checkpoint",
                title="Model switched",
                status="unknown",
                model=_model(message.get("model")),
                started_at=created,
            )
            continue
        if kind == "compaction":
            status = message.get("status")
            if not isinstance(status, str):
                raise TraceImportError("OpenCode compaction status must be a string.")
            if status not in ("running", "completed", "failed"):
                _warning(run, "Unknown compaction state was retained as unknown.")
                status = "unknown"
            if status in ("running", "unknown"):
                _warning(run, "Incomplete compaction was retained; completion is unavailable.")
            _warning(
                run,
                "Compaction does not reconstruct discarded context or native replacement messages.",
            )
            add(
                sid,
                mid,
                kind="checkpoint",
                title="Compaction",
                status=status,
                model=_model(message.get("model")),
                started_at=created,
                explanation=[message.get("summary")],
            )
            continue
        if kind == "shell":
            if not isinstance(message.get("status"), str):
                raise TraceImportError("OpenCode shell status must be a string.")
            status = {
                "running": "running",
                "exited": "completed",
                "timeout": "failed",
                "killed": "cancelled",
            }.get(message.get("status"), "unknown")
            exit_code = message.get("exit")
            if exit_code is not None and type(exit_code) is not int:
                raise TraceImportError("OpenCode shell exit code must be an integer.")
            if status == "completed" and exit_code not in (None, 0):
                status = "failed"
            output = _object(message.get("output", {}))
            if status in ("running", "unknown"):
                _warning(run, "Incomplete shell execution was retained; completion is unavailable.")
            add(
                sid,
                mid,
                kind="tool",
                title="Shell command",
                status=status,
                ended_at=completed,
                exit_code=exit_code,
                input=[message.get("command")],
                output=[output.get("output")],
                truncated=output.get("truncated") is True,
            )
            continue

        content = message.get("content")
        if not isinstance(content, list):
            raise TraceImportError("OpenCode v2 assistant content must be an array.")
        model = _model(message.get("model"))
        status = (
            "failed"
            if message.get("error") is not None
            else ("completed" if completed is not None else "running")
        )
        if status == "running":
            _warning(run, "Incomplete assistant message was retained; completion is unavailable.")
        assistant = add(
            sid,
            mid,
            kind="assistant",
            title="Assistant message",
            status=status,
            model=model,
            started_at=created,
            ended_at=completed,
            output=[
                part.get("text")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ],
        )
        if message.get("retry") is not None:
            retry = _object(message["retry"])
            add(
                sid,
                _reference(mid, "retry"),
                kind="checkpoint",
                title="Assistant retry",
                status="unknown",
                model=model,
                started_at=_millis(retry.get("at")),
                parent_id=assistant.id,
            )
        calls = {
            _identifier(part.get("id")): part
            for part in content
            if isinstance(part, dict) and part.get("type") == "tool"
        }
        for index, part in enumerate(content):
            part = _object(part)
            if part.get("type") == "text":
                continue
            if part.get("type") == "reasoning":
                add(
                    sid,
                    _reference(mid, f"reasoning-{index + 1}"),
                    kind="assistant",
                    title="Exposed reasoning summary",
                    status="unknown",
                    model=model,
                    explanation=[part.get("text")],
                    parent_id=assistant.id,
                )
                continue
            if part.get("type") != "tool":
                _warning(run, "Unknown assistant content was omitted; coverage is partial.")
                continue
            cid = _identifier(part.get("id"))
            if calls[cid] is not part:
                continue
            name = _identifier(part.get("name"))
            state = _object(part.get("state"))
            if not isinstance(state.get("status"), str):
                raise TraceImportError("OpenCode tool status must be a string.")
            status = {
                "streaming": "running",
                "running": "running",
                "completed": "completed",
                "error": "failed",
            }.get(state.get("status"), "unknown")
            if status in ("running", "unknown"):
                _warning(
                    run, "Incomplete or unknown tool state was retained; completion is unavailable."
                )
            time = _object(part.get("time"))
            args = state.get("input")
            inputs = []
            if name == "shell" and isinstance(args, dict):
                inputs = [args.get("command"), args.get("workdir")]
            elif name == "read" and isinstance(args, dict):
                inputs = [args.get("path")]
            results = state.get("content", [])
            if not isinstance(results, list):
                raise TraceImportError("OpenCode tool content must be an array.")
            texts = []
            for result in results:
                result = _object(result)
                if result.get("type") == "text":
                    texts.append(result.get("text"))
                elif result.get("type") != "file":
                    _warning(run, "Unknown tool content was omitted; coverage is partial.")
            metadata = _object(state.get("metadata", {}))
            exit_code = metadata.get("exit") if name == "shell" else None
            if exit_code is not None and type(exit_code) is not int:
                raise TraceImportError("OpenCode shell exit code must be an integer.")
            event = add(
                sid,
                _reference(mid, cid),
                kind="tool",
                title=f"Tool: {name}",
                status=status,
                parent_id=assistant.id,
                model=model,
                started_at=_millis(time.get("ran")),
                ended_at=_millis(time.get("completed")),
                input=inputs,
                output=texts,
                exit_code=exit_code,
                truncated=name == "shell" and metadata.get("truncated") is True,
            )
            if name == "subagent" and metadata.get("sessionID") is not None:
                child = _identifier(metadata["sessionID"])
                child_calls.setdefault(child, []).append((sid, event.id))

    for sid, run in runs.items():
        parent = sessions[sid].get("parentID")
        if parent is not None:
            parent = _identifier(parent)
            if parent in runs:
                run.parent_run_id = runs[parent].id
                calls = child_calls.get(sid, [])
                if len(calls) == 1 and calls[0][0] == parent:
                    run.parent_event_id = calls[0][1]
                else:
                    _warning(
                        run,
                        "Child invocation is ambiguous; only the session parent is known.",
                    )
            else:
                _warning(
                    run, "Parent session export was not supplied; parent execution is unavailable."
                )
        elif sid in child_calls:
            _warning(
                run,
                "Child metadata lacks a matching session parent; no parent was inferred.",
            )
    for child, calls in child_calls.items():
        for sid, _ in calls:
            if child not in runs:
                _warning(
                    runs[sid],
                    "Referenced child export was not supplied; child execution is unavailable.",
                )
            elif sessions[child].get("parentID") != sid:
                _warning(
                    runs[sid],
                    "Child metadata conflicts with session parent; no invocation was inferred.",
                )
    return finalize(list(runs.values()), events, options)
