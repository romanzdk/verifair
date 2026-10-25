import re
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


def _identifier(value: object, limit: int = 80) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1," + str(limit) + "}", value):
        cleaned, _ = sanitize_text(value, ImportOptions(include_details=True), limit=limit)
        if cleaned == value:
            return value
    return None


def _warn(run: TraceRun, message: str) -> None:
    if message not in run.warnings and len(run.warnings) < 16:
        run.warnings.append(message)


def _version(value: str | None) -> None:
    if value is not None and not re.fullmatch(r"0\.[0-9]+\.[0-9]+", value):
        raise TraceImportError("Unsupported Codex producer version; use native exec JSONL 0.x.")


def _text_blocks(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    return (
        "\n".join(
            block["text"]
            for block in value
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
        or None
    )


def convert(paths: tuple[Path, ...], options: ImportOptions) -> TraceDocument:
    _version(options.source_version)
    budget = ReadBudget()
    runs = []
    events = []
    threads = {}
    links = []
    ordinals = {}

    def emit(run, title, kind="checkpoint", status="unknown", source_ref=None):
        ordinals[run.id] = ordinals.get(run.id, 0) + 1
        event = dict(
            id=f"codex-event-{len(events) + 1}",
            run_id=run.id,
            ordinal=ordinals[run.id],
            kind=kind,
            title=title,
            status=status,
            source_ref=source_ref,
        )
        events.append(event)
        return event

    for path in paths:
        run = None
        thread = None
        items = {}
        turn = None

        def start_run(identity):
            current = TraceRun(
                id=f"codex-run-{len(runs) + 1}",
                agent="codex",
                origin="recorded",
                source_format="codex-exec-jsonl",
                source_version=options.source_version,
                coverage="partial",
                warnings=[
                    "Native exec capture has no universal timestamps, model or turn IDs; "
                    "uncaptured history and child internals are unavailable."
                ],
            )
            if options.source_version not in (None, "0.163.0"):
                _warn(current, "Untested Codex producer version; only 0.163.0 is fixture-tested.")
            runs.append(current)
            if identity is not None:
                threads.setdefault(identity, []).append(current)
            return current

        for record in read_jsonl(path, budget=budget):
            kind = record.get("type")
            if not isinstance(kind, str):
                raise TraceImportError("Codex records require a string event type.")
            if kind == "thread.started":
                identity = _identifier(record.get("thread_id"))
                if identity is None:
                    raise TraceImportError(
                        "Native Codex thread start requires a bounded thread ID."
                    )
                if identity != thread:
                    run = start_run(identity)
                    thread = identity
                    items = {}
                    turn = None
                    emit(run, "Thread started", status="completed", source_ref=identity)
                continue
            if run is None:
                run = start_run(None)
                _warn(run, "Thread start is missing; this explicit capture is partial.")
            if kind == "turn.started":
                if turn is not None and turn["status"] == "running":
                    _warn(run, "A turn completion is missing; capture is partial.")
                turn = emit(run, "Turn boundary", status="running")
            elif kind in ("turn.completed", "turn.failed"):
                if turn is None or turn["status"] != "running":
                    turn = emit(run, "Turn boundary")
                    _warn(run, "A turn start is missing; capture is partial.")
                turn["status"] = "failed" if kind == "turn.failed" else "completed"
                if kind == "turn.failed" and isinstance(record.get("error"), dict):
                    turn["output"] = record["error"].get("message")
            elif kind == "error":
                emit(run, "Stream error", status="failed")["output"] = record.get("message")
            elif kind in ("item.started", "item.updated", "item.completed"):
                item = record.get("item")
                if not isinstance(item, dict) or _identifier(item.get("id")) is None:
                    raise TraceImportError(
                        "Native Codex item requires an object and bounded item ID."
                    )
                item_id = item["id"]
                variant = item.get("type")
                if not isinstance(variant, str):
                    raise TraceImportError("Codex items require a string item type.")
                titles = {
                    "agent_message": "Assistant statement",
                    "reasoning": "Provider reasoning summary",
                    "command_execution": "Command execution",
                    "file_change": "File changes",
                    "mcp_tool_call": "MCP tool",
                    "collab_tool_call": "Collaboration tool",
                    "web_search": "Web search",
                    "todo_list": "Agent todo list",
                    "error": "Item error",
                }
                if variant not in titles:
                    _warn(run, "Unknown Codex item kind was omitted; coverage is partial.")
                    continue
                if item_id in items:
                    event, previous_variant = items[item_id]
                    if previous_variant != variant:
                        raise TraceImportError(
                            "Codex item identity changed kind within one execution."
                        )
                    if event.get("_terminal") and kind != "item.completed":
                        continue
                else:
                    event_kind = (
                        "assistant"
                        if variant in ("agent_message", "reasoning")
                        else "checkpoint"
                        if variant in ("todo_list", "error")
                        else "tool"
                    )
                    event = emit(
                        run,
                        titles[variant],
                        event_kind,
                        source_ref=f"{thread}:{item_id}" if thread else item_id,
                    )
                    items[item_id] = event, variant
                terminal = kind == "item.completed"
                event["_terminal"] = terminal
                status = item.get("status")
                if status is not None and not isinstance(status, str):
                    raise TraceImportError("Codex item status must be a string.")
                event["status"] = {
                    "in_progress": "running",
                    "completed": "completed",
                    "failed": "failed",
                    "declined": "cancelled",
                }.get(status, "unknown")
                if variant in ("agent_message", "reasoning", "web_search", "todo_list"):
                    event["status"] = "completed" if terminal else "running"
                if variant == "command_execution":
                    event["input"] = item.get("command")
                    event["output"] = item.get("aggregated_output")
                    code = item.get("exit_code")
                    event["exit_code"] = code if type(code) is int else None
                    if status == "declined":
                        event["title"] = "Command execution: declined by source"
                elif variant == "agent_message":
                    event["output"] = item.get("text")
                elif variant == "reasoning":
                    event["explanation"] = item.get("text")
                elif variant == "mcp_tool_call":
                    server, tool = (
                        _identifier(item.get("server"), 45),
                        _identifier(item.get("tool"), 45),
                    )
                    if server and tool:
                        event["title"] = f"MCP tool: {server}.{tool}"
                    else:
                        _warn(run, "Invalid MCP identifiers were omitted.")
                    result = item.get("result")
                    if isinstance(result, dict):
                        event["output"] = _text_blocks(result.get("content"))
                    error = item.get("error")
                    if isinstance(error, dict):
                        event["explanation"] = error.get("message")
                elif variant == "file_change":
                    changes = item.get("changes")
                    if isinstance(changes, list):
                        event["input"] = (
                            "\n".join(
                                c["path"]
                                for c in changes
                                if isinstance(c, dict) and isinstance(c.get("path"), str)
                            )
                            or None
                        )
                elif variant == "web_search":
                    event["input"] = item.get("query")
                elif variant == "error":
                    event["status"] = "failed"
                    event["output"] = item.get("message")
                elif variant == "collab_tool_call":
                    tool = item.get("tool")
                    if tool in ("spawn_agent", "send_input", "wait", "close_agent"):
                        event["title"] = f"Collaboration tool: {tool}"
                    else:
                        _warn(run, "Unknown collaboration tool was omitted from the label.")
                    event["input"] = item.get("prompt")
                    if tool == "spawn_agent":
                        receivers = item.get("receiver_thread_ids")
                        if isinstance(receivers, list) and item.get("sender_thread_id") == thread:
                            for receiver in receivers:
                                if _identifier(receiver):
                                    link = (run.id, event["id"], receiver)
                                    if link not in links:
                                        links.append(link)
                        else:
                            _warn(run, "Collaboration parent identifiers are unavailable.")
            else:
                _warn(run, "Unknown Codex record kind was omitted; coverage is partial.")
        if run is not None and turn is not None and turn["status"] == "running":
            _warn(run, "A turn completion is missing; capture is partial.")
    by_run = {run.id: run for run in runs}
    candidates = {}
    for parent_id, event_id, receiver in links:
        children = threads.get(receiver, [])
        if len(children) != 1 or children[0].id == parent_id:
            _warn(
                by_run[parent_id],
                "Child capture is missing or ambiguous; internals are unavailable.",
            )
            continue
        candidates.setdefault(children[0].id, set()).add((parent_id, event_id))
    for child_id, parents in candidates.items():
        child = by_run[child_id]
        parent_ids = {parent for parent, _ in parents}
        if len(parent_ids) == 1:
            child.parent_run_id = next(iter(parent_ids))
            if len(parents) == 1:
                child.parent_event_id = next(iter(parents))[1]
            else:
                _warn(child, "Child invocation is ambiguous; only the session parent is retained.")
        else:
            _warn(child, "Child parent is ambiguous; no execution edge was inferred.")
    normalized = []
    for data in events:
        terminal = data.pop("_terminal", True)
        if not terminal or data["status"] in ("running", "unknown"):
            _warn(
                by_run[data["run_id"]],
                "An item completion is missing or unknown; capture is partial.",
            )
        truncated = False
        for field in ("input", "output", "explanation"):
            data[field], shortened = sanitize_text(data.get(field), options)
            truncated |= shortened
        normalized.append(TraceEvent(**data, truncated=truncated))
    return finalize(runs, normalized, options)
