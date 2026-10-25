import re
from hashlib import sha256
from pathlib import Path

from verifair.trace_importers.common import (
    ImportOptions,
    ReadBudget,
    TraceImportError,
    finalize,
    read_jsonl,
    sanitize_text,
)
from verifair.traces import TraceDocument, TraceEvent, TraceRun


def _identifier(value: object, limit: int = 100) -> str | None:
    if (
        isinstance(value, str)
        and len(value) <= limit
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]*", value)
    ):
        return value
    return None


def _text(values: list[object], options: ImportOptions) -> tuple[str | None, bool]:
    parts = []
    truncated = False
    for value in values:
        text, shortened = sanitize_text(value, options)
        truncated |= shortened
        if text is not None:
            parts.append(text)
    text, shortened = sanitize_text("\n".join(parts) if parts else None, options)
    return text, truncated or shortened


def _content(value: object) -> list[object]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [
            block.get("text")
            for block in value
            if isinstance(block, dict) and block.get("type") == "text"
        ]
    return []


def convert(paths: tuple[Path, ...], options: ImportOptions) -> TraceDocument:
    if len(paths) != 1:
        raise TraceImportError("Pi session import requires exactly one explicit file.")
    budget = ReadBudget()
    rows = iter(read_jsonl(paths[0], budget=budget))
    header = next(rows, None)
    if (
        not header
        or header.get("type") != "session"
        or not isinstance(header.get("id"), str)
        or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", header["id"]) is None
    ):
        raise TraceImportError("Supply a persisted Pi session JSONL file with a session header.")
    if "version" in header and (type(header["version"]) is not int or header["version"] != 3):
        raise TraceImportError("Unsupported Pi session schema version; export schema 3.")
    if options.source_version is not None:
        version = re.fullmatch(r"1\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", options.source_version)
        if version is None:
            raise TraceImportError("Unsupported Pi producer version; use a 1.x session export.")
    warnings = ["Selected branch ancestry only; conversation parents are not execution nesting."]

    def warn(message: str) -> None:
        if message not in warnings and len(warnings) < 17:
            warnings.append(message)

    if "version" not in header:
        warn("Session schema version is absent; only the recognized tree shape is supported.")
    if options.source_version not in (None, "1.4.0"):
        warn("Producer version is untested; the fixture baseline is Pi 1.4.0 with schema 3.")
    if "parentSession" in header:
        warn("Fork references another session artifact; its path and history were not read.")
    session_id = header["id"]
    if len(session_id) > 97:
        session_id = sha256(session_id.encode()).hexdigest()
        warn("The session identifier exceeds normalized bounds; its stable digest is used.")
    run_id = "pi-" + session_id

    entries = {}
    stream_kinds = {
        "agent_start",
        "agent_end",
        "turn_start",
        "turn_end",
        "message_start",
        "message_update",
        "message_end",
        "tool_execution_start",
        "tool_execution_update",
        "tool_execution_end",
        "auto_compaction_start",
        "auto_compaction_end",
        "auto_retry_start",
        "auto_retry_end",
    }
    for row in rows:
        native_id = row.get("id")
        parent = row.get("parentId")
        if (
            not isinstance(native_id, str)
            or not native_id
            or native_id in entries
            or "parentId" not in row
            or (parent is not None and not isinstance(parent, str))
            or not isinstance(row.get("type"), str)
        ):
            raise TraceImportError("Pi entries require unique IDs and explicit parent IDs.")
        if row["type"] == "session" or row["type"] in stream_kinds:
            raise TraceImportError("JSON-mode event streams are not persisted Pi sessions.")
        entries[native_id] = row
    parents = {key: row["parentId"] for key, row in entries.items()}
    if any(parent is not None and parent not in entries for parent in parents.values()):
        raise TraceImportError(
            "Pi ancestry is missing a parent entry; supply the complete selected file."
        )
    visited = set()
    for native_id in entries:
        path = set()
        cursor = native_id
        while cursor is not None and cursor not in visited:
            if cursor in path:
                raise TraceImportError("Pi parent graph contains a cycle; supply a valid session.")
            path.add(cursor)
            cursor = parents[cursor]
        visited.update(path)
    leaves = set(entries) - {parent for parent in parents.values() if parent is not None}
    leaf = options.leaf
    if leaf is None:
        if len(leaves) != 1:
            raise TraceImportError(
                "Supply --leaf for a Pi session with multiple leaves or no active leaf."
            )
        leaf = next(iter(leaves))
    if leaf not in leaves:
        raise TraceImportError("Selected Pi leaf must identify an existing leaf entry.")
    branch = []
    while leaf is not None:
        branch.append(entries[leaf])
        leaf = parents[leaf]
    branch.reverse()
    selected_ids = {row["id"] for row in branch}
    events: list[TraceEvent] = []
    calls: dict[str, int] = {}
    results = set()
    partial = "parentSession" in header

    def add(
        kind,
        title,
        status,
        *,
        ref,
        input_values=None,
        output_values=None,
        explanation_values=None,
        **fields,
    ):
        input_text, input_cut = _text(input_values or [], options)
        output_text, output_cut = _text(output_values or [], options)
        explanation, explanation_cut = _text(explanation_values or [], options)
        source_id = (
            ref
            if re.fullmatch(r"(?:entry|toolCall):[A-Za-z0-9][A-Za-z0-9._-]{0,179}", ref)
            else None
        )
        if source_id is None:
            warn("A source identifier was omitted because it exceeds safe identifier bounds.")
        event = TraceEvent(
            id=f"event-pi-{len(events) + 1}",
            run_id=run_id,
            ordinal=len(events) + 1,
            kind=kind,
            title=title,
            status=status,
            source_ref=source_id,
            input=input_text,
            output=output_text,
            explanation=explanation,
            truncated=input_cut or output_cut or explanation_cut,
            **fields,
        )
        events.append(event)
        return event

    for row in branch:
        kind = row["type"]
        ref = "entry:" + row["id"]
        if kind == "model_change":
            model = _identifier(row.get("modelId"))
            add("checkpoint", "Model changed", "completed", ref=ref, model=model)
        elif kind in ("compaction", "branch_summary", "context_edit"):
            title = {
                "compaction": "Context compaction",
                "branch_summary": "Branch summary",
                "context_edit": "Context edit",
            }[kind]
            add(
                "checkpoint",
                title,
                "completed",
                ref=ref,
                explanation_values=[row.get("summary")] if kind != "context_edit" else [],
            )
            partial = True
            warn(
                "Compaction, branch summary or context edit recorded; "
                "omitted history is not reconstructed."
            )
            reference = row.get(
                {
                    "compaction": "firstKeptEntryId",
                    "branch_summary": "fromId",
                    "context_edit": "targetId",
                }[kind]
            )
            if not isinstance(reference, str) or reference not in selected_ids:
                warn(
                    "A context or summary reference is outside selected ancestry; "
                    "referenced history is unavailable."
                )
        elif kind in ("custom", "custom_message"):
            partial = True
            warn("Opaque extension records were excluded; no child execution was recovered.")
        elif kind in ("thinking_level_change", "usage", "label", "session_info"):
            continue
        elif kind == "message":
            message = row.get("message")
            if not isinstance(message, dict):
                raise TraceImportError("Pi message entries require a message object.")
            role = message.get("role")
            if role in ("system", "user"):
                continue
            if role == "assistant":
                content = message.get("content")
                if not isinstance(content, list):
                    raise TraceImportError(
                        "Pi assistant content must contain native content blocks."
                    )
                stop = message.get("stopReason")
                if stop is not None and not isinstance(stop, str):
                    raise TraceImportError("Pi assistant stop reasons must be native strings.")
                status = {
                    "stop": "completed",
                    "length": "completed",
                    "toolUse": "completed",
                    "error": "failed",
                    "aborted": "cancelled",
                    "pending": "running",
                }.get(stop, "unknown")
                if status in ("running", "unknown"):
                    partial = True
                    warn(
                        "An assistant message has no supported terminal state; coverage is partial."
                    )
                assistant = add(
                    "assistant",
                    "Assistant message",
                    status,
                    ref=ref,
                    model=_identifier(message.get("model")),
                    output_values=_content(content),
                )
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "toolCall":
                        continue
                    call_id = block.get("id")
                    if (
                        not isinstance(call_id, str)
                        or not call_id
                        or call_id in calls
                        or call_id in results
                    ):
                        raise TraceImportError("Pi tool calls require distinct native call IDs.")
                    arguments = block.get("arguments")
                    values = (
                        [arguments.get(key) for key in ("command", "path")]
                        if isinstance(arguments, dict)
                        else []
                    )
                    name = _identifier(block.get("name"))
                    calls[call_id] = len(events)
                    add(
                        "tool",
                        f"Tool: {name}" if name else "Tool call",
                        "running",
                        ref="toolCall:" + call_id,
                        parent_id=assistant.id,
                        input_values=values,
                    )
            elif role == "toolResult":
                call_id = message.get("toolCallId")
                if not isinstance(call_id, str) or not call_id or call_id in results:
                    raise TraceImportError("Pi tool results require distinct native call IDs.")
                results.add(call_id)
                is_error = message.get("isError")
                status = (
                    "failed"
                    if is_error is True
                    else "completed"
                    if is_error is False
                    else "unknown"
                )
                if status == "unknown":
                    partial = True
                    warn("A tool result has no supported terminal state; coverage is partial.")
                if call_id in calls:
                    index = calls[call_id]
                    output, shortened = _text(_content(message.get("content")), options)
                    events[index] = TraceEvent(
                        **{
                            **events[index].model_dump(),
                            "status": status,
                            "output": output,
                            "truncated": events[index].truncated or shortened,
                        }
                    )
                else:
                    partial = True
                    warn(
                        "A tool result has no captured call; "
                        "arguments and ownership are unavailable."
                    )
                    name = _identifier(message.get("toolName"))
                    add(
                        "tool",
                        f"Tool: {name}" if name else "Tool result",
                        status,
                        ref="toolCall:" + call_id,
                        output_values=_content(message.get("content")),
                    )
            elif role == "bashExecution":
                exit_code = message.get("exitCode")
                if exit_code is not None and type(exit_code) is not int:
                    raise TraceImportError("Pi shell exit codes must be structured integers.")
                status = (
                    "cancelled"
                    if message.get("cancelled") is True
                    else "unknown"
                    if exit_code is None
                    else "completed"
                    if exit_code == 0
                    else "failed"
                )
                event = add(
                    "tool",
                    "Shell execution",
                    status,
                    ref=ref,
                    exit_code=exit_code,
                    input_values=[message.get("command")],
                    output_values=[message.get("output")],
                )
                if message.get("truncated") is True:
                    events[-1] = event.model_copy(update={"truncated": True})
                if status == "unknown":
                    partial = True
                    warn("A shell execution has no terminal state; coverage is partial.")
            else:
                partial = True
                warn("Unknown or extension message role was excluded; coverage is partial.")
        else:
            partial = True
            warn("Unknown session record kind was excluded; coverage is partial.")
    if calls.keys() - results:
        partial = True
        warn("Tool calls without results remain running; completion is not inferred.")
    run = TraceRun(
        id=run_id,
        agent="pi",
        origin="recorded",
        source_format="pi-session-jsonl",
        source_version=options.source_version,
        coverage="partial" if partial else "unknown",
        warnings=warnings,
    )
    return finalize([run], events, options)
