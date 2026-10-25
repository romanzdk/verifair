import re
from collections import defaultdict
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
    if value is not None and not re.fullmatch(r"2\.[0-9]+\.[0-9]+", value):
        raise TraceImportError(
            "Unsupported Claude producer version; use documented 2.x SDK stream."
        )


def _text_blocks(value: object) -> str | None:
    if isinstance(value, str):
        return value
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


def _tool_input(name: str, value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    field = (
        "command" if name == "Bash" else "file_path" if name in ("Read", "Write", "Edit") else None
    )
    return value.get(field) if field and isinstance(value.get(field), str) else None


def convert(paths: tuple[Path, ...], options: ImportOptions) -> TraceDocument:
    _version(options.source_version)
    budget = ReadBudget()
    contexts = {}
    events = []
    resolved_tasks = []
    record_order = 0

    def context(file_index, session, agent):
        key = file_index, session, agent
        if key not in contexts:
            run = TraceRun(
                id=f"claude-run-{len(contexts) + 1}",
                agent="claude_code",
                origin="recorded",
                source_format="claude-stream-json",
                source_version=options.source_version,
                coverage="partial",
                warnings=[
                    "SDK output is partial execution evidence; uncaptured history, model "
                    "HTTP calls and child internals are unavailable."
                ],
            )
            if options.source_version not in (None, "2.1.309"):
                _warn(run, "Untested Claude producer version; only 2.1.309 is fixture-tested.")
            contexts[key] = dict(
                run=run,
                file=file_index,
                session=session,
                agent=agent,
                calls={},
                seen=set(),
                blocks={},
                message=None,
                model=None,
                parents=set(),
                task_events={},
                count=0,
            )
        return contexts[key]

    def emit(ctx, title, kind="checkpoint", status="unknown", source_ref=None):
        ctx["count"] += 1
        event = dict(
            id=f"claude-event-{len(events) + 1}",
            run_id=ctx["run"].id,
            ordinal=ctx["count"],
            kind=kind,
            title=title,
            status=status,
            source_ref=source_ref,
        )
        events.append(event)
        return event

    def call(ctx, native_id):
        identity = _identifier(native_id)
        if identity is None:
            raise TraceImportError("Claude tool record requires a bounded tool-use ID.")
        if identity not in ctx["calls"]:
            ctx["calls"][identity] = emit(ctx, "Tool call", "tool", source_ref=identity)
        return ctx["calls"][identity]

    def block(
        ctx,
        value,
        source_ref,
        *,
        model=None,
        existing=None,
        timestamp=None,
        aborted=False,
        failed=False,
    ):
        if not isinstance(value, dict):
            raise TraceImportError("Claude content blocks must be objects.")
        kind = value.get("type")
        if kind == "tool_use":
            event = call(ctx, value.get("id"))
            name = _identifier(value.get("name"), 100)
            if name:
                event["title"] = f"Tool: {name}"
            else:
                _warn(ctx["run"], "Invalid tool identifier was omitted.")
            event["_has_call"] = True
            if not event.get("_has_result"):
                event["status"] = "running"
            if options.include_details:
                event["input"] = _tool_input(name, value.get("input"))
            return event
        if kind not in ("text", "thinking"):
            if kind not in ("redacted_thinking", "image", "document"):
                _warn(ctx["run"], "Unknown Claude content block was omitted; coverage is partial.")
            return None
        event = existing or emit(
            ctx,
            "Assistant statement" if kind == "text" else "Provider reasoning summary",
            "assistant",
            source_ref=source_ref,
        )
        event["status"] = "failed" if failed else "unknown" if aborted else "completed"
        event["_aborted"] = aborted
        event["model"] = model
        event.pop("_chunks", None)
        if options.include_details:
            event["output" if kind == "text" else "explanation"] = value.get(
                "text" if kind == "text" else "thinking"
            )
        if timestamp is not None:
            event["ended_at"] = timestamp
        return event

    for file_index, path in enumerate(paths):
        current_session = None
        for record in read_jsonl(path, budget=budget):
            record_order += 1
            kind = record.get("type")
            session = _identifier(record.get("session_id"))
            if session is None and kind == "user":
                session = current_session
            if session is None:
                raise TraceImportError("Use documented Claude SDK stream records with session IDs.")
            current_session = session
            agent = _identifier(record.get("agent_id"))
            parent = _identifier(record.get("parent_tool_use_id"))
            if (record.get("agent_id") is not None and agent is None) or (
                record.get("parent_tool_use_id") is not None and parent is None
            ):
                raise TraceImportError("Claude child identifiers must be bounded source IDs.")
            if agent is None and parent is not None:
                agent = f"parent:{parent}"
            ctx = context(file_index, session, agent)
            run = ctx["run"]
            if parent:
                ctx["parents"].add(parent)
            uuid = _identifier(record.get("uuid"))
            if uuid is not None:
                if uuid in ctx["seen"]:
                    continue
                ctx["seen"].add(uuid)
            elif kind in ("assistant", "stream_event", "result"):
                raise TraceImportError(
                    "Documented Claude stream messages require bounded SDK UUIDs."
                )
            if kind == "assistant":
                message = record.get("message")
                if not isinstance(message, dict) or not isinstance(message.get("content"), list):
                    raise TraceImportError(
                        "Use Claude SDK content-block messages, not private history."
                    )
                native_message = _identifier(message.get("id"))
                if native_message is None:
                    raise TraceImportError(
                        "Claude assistant message requires an upstream message ID."
                    )
                model = _identifier(message.get("model"), 100)
                if message.get("model") is not None and model is None:
                    _warn(run, "Invalid model identifier was omitted.")
                for index, value in enumerate(message["content"]):
                    existing = None
                    if isinstance(value, dict):
                        available = [
                            entry
                            for entry in ctx["blocks"].get(native_message, {}).values()
                            if not entry["complete"]
                            and entry["type"] == value.get("type")
                            and not entry["closed"]
                        ]
                        if len(available) == 1:
                            available[0]["complete"] = True
                            existing = available[0]["event"]
                    block(
                        ctx,
                        value,
                        f"{native_message}:{uuid}:{index}",
                        model=model,
                        existing=existing,
                        timestamp=record.get("timestamp"),
                        aborted=record.get("aborted") is True,
                        failed=record.get("error") is not None,
                    )
                if record.get("error") is not None:
                    _warn(run, "Assistant generation failed; capture is partial.")
                if record.get("aborted") is True:
                    _warn(run, "Assistant generation was interrupted; content is partial.")
            elif kind == "stream_event":
                event = record.get("event")
                if not isinstance(event, dict):
                    raise TraceImportError("Claude stream envelope requires a native event object.")
                subtype = event.get("type")
                if subtype == "message_start":
                    message = event.get("message")
                    if not isinstance(message, dict) or _identifier(message.get("id")) is None:
                        raise TraceImportError(
                            "Claude message start requires an upstream message ID."
                        )
                    ctx["message"] = message["id"]
                    ctx["model"] = _identifier(message.get("model"), 100)
                elif subtype in (
                    "content_block_start",
                    "content_block_delta",
                    "content_block_stop",
                ):
                    index = event.get("index")
                    if ctx["message"] is None or type(index) is not int or index < 0:
                        _warn(run, "Stream block ancestry is missing; partial content was omitted.")
                        continue
                    blocks = ctx["blocks"].setdefault(ctx["message"], {})
                    if subtype == "content_block_start":
                        value = event.get("content_block")
                        if not isinstance(value, dict):
                            raise TraceImportError("Claude stream content block must be an object.")
                        if index in blocks:
                            continue
                        event_kind = value.get("type")
                        partial = None
                        if event_kind in ("text", "tool_use"):
                            partial = block(
                                ctx, value, f"{ctx['message']}:{index}", model=ctx["model"]
                            )
                            if partial and partial["kind"] == "assistant":
                                partial["status"] = "running"
                                if options.include_details:
                                    initial = partial.pop("output", None)
                                    partial["_chunks"] = (
                                        [initial] if isinstance(initial, str) else []
                                    )
                        blocks[index] = dict(
                            type=event_kind, event=partial, complete=False, closed=False
                        )
                    elif index in blocks:
                        entry = blocks[index]
                        partial = entry["event"]
                        if subtype == "content_block_stop":
                            entry["closed"] = True
                            if partial and partial["kind"] == "assistant":
                                if not partial.get("_aborted") and partial["status"] != "failed":
                                    partial["status"] = "completed"
                        else:
                            delta = event.get("delta")
                            if (
                                isinstance(delta, dict)
                                and delta.get("type") == "text_delta"
                                and partial
                                and entry["type"] == "text"
                                and not entry["complete"]
                                and options.include_details
                            ):
                                text = delta.get("text")
                                if isinstance(text, str):
                                    partial["_chunks"].append(text)
                    else:
                        _warn(run, "Stream block start is missing; partial content was omitted.")
                elif subtype == "message_stop":
                    if any(not b["closed"] for b in ctx["blocks"].get(ctx["message"], {}).values()):
                        _warn(
                            run, "Stream ended with incomplete content blocks; capture is partial."
                        )
                elif subtype not in ("message_delta", "ping"):
                    _warn(run, "Unknown Claude stream event was omitted; coverage is partial.")
            elif kind == "user":
                message = record.get("message")
                if not isinstance(message, dict):
                    raise TraceImportError("Claude user record requires a message object.")
                content = message.get("content")
                values = (
                    content if isinstance(content, list) else [{"type": "text", "text": content}]
                )
                for index, value in enumerate(values):
                    if not isinstance(value, dict):
                        raise TraceImportError("Claude user content blocks must be objects.")
                    if value.get("type") == "tool_result":
                        native_id = value.get("tool_use_id")
                        data = call(ctx, native_id)
                        data["_has_result"] = True
                        data["_result_order"] = record_order
                        output = record.get("tool_use_result")
                        detached = isinstance(output, dict) and (
                            output.get("detachedToolCall") is True
                            or isinstance(output.get("backgroundTaskId"), str)
                        )
                        agent_tool = data["title"] in ("Tool: Agent", "Tool: Task")
                        if isinstance(output, dict) and agent_tool:
                            detached |= output.get("status") in (
                                "async_launched",
                                "remote_launched",
                            )
                            agent_id = _identifier(output.get("agentId"))
                            if agent_id:
                                resolved_tasks.append((ctx, agent_id, native_id))
                        data["status"] = (
                            "running"
                            if detached
                            else "failed"
                            if value.get("is_error") is True
                            else "completed"
                        )
                        if options.include_details:
                            data["output"] = _text_blocks(value.get("content"))
                        if isinstance(output, dict):
                            if data["title"] == "Tool: Bash" and output.get("interrupted") is True:
                                data["status"] = "cancelled"
                            if (
                                agent_tool
                                and output.get("status") == "completed"
                                and options.include_details
                            ):
                                data["output"] = _text_blocks(output.get("content"))
                    elif value.get("type") == "text":
                        data = emit(
                            ctx,
                            "User input",
                            status="completed",
                            source_ref=f"{uuid}:{index}" if uuid else None,
                        )
                        if options.include_details:
                            data["input"] = value.get("text")
                    elif value.get("type") not in ("image", "document"):
                        _warn(run, "Unknown Claude user content was omitted; coverage is partial.")
            elif kind == "result":
                if record.get("subtype") not in (
                    "success",
                    "error_max_turns",
                    "error_during_execution",
                    "error_max_budget_usd",
                    "error_max_structured_output_retries",
                ):
                    _warn(run, "Unknown Claude result kind was retained with limited coverage.")
                data = emit(
                    ctx,
                    "Execution result",
                    status="failed"
                    if record.get("is_error") is True
                    else "completed"
                    if record.get("subtype") == "success"
                    else "unknown",
                    source_ref=uuid,
                )
                if options.include_details:
                    data["output"] = record.get("result")
                if record.get("terminal_reason") not in (None, "completed"):
                    _warn(run, "Source reports a nonstandard execution ending; capture is partial.")
            elif kind == "system":
                subtype = record.get("subtype")
                if subtype == "init":
                    version = record.get("claude_code_version")
                    if isinstance(version, str):
                        _version(version)
                        if version != "2.1.309":
                            _warn(
                                run,
                                "Untested Claude producer version; only 2.1.309 is fixture-tested.",
                            )
                    emit(ctx, "Session initialized", status="completed", source_ref=uuid)
                elif subtype == "compact_boundary":
                    emit(ctx, "Compaction boundary", status="completed", source_ref=uuid)
                    _warn(
                        run, "Compaction boundary is recorded; discarded history was not recovered."
                    )
                elif subtype in ("task_started", "task_progress", "task_notification"):
                    fields = ("tool_use_id", "run_id")
                    if subtype == "task_started":
                        fields += ("parent_task_id",)
                    if any(
                        record.get(field) is not None and _identifier(record[field]) is None
                        for field in fields
                    ):
                        raise TraceImportError(
                            "Claude task binding IDs must be bounded source IDs."
                        )
                    task_id = _identifier(record.get("task_id"))
                    tool_id = _identifier(record.get("tool_use_id"))
                    if task_id is None:
                        raise TraceImportError("Claude task record requires a bounded task ID.")
                    run_id = _identifier(record.get("run_id"))
                    task_key = task_id, run_id
                    ambiguous_run = False
                    if run_id is None:
                        matches = [key for key in ctx["task_events"] if key[0] == task_id]
                        if len(matches) == 1:
                            task_key = matches[0]
                        elif len(matches) > 1:
                            _warn(run, "Task run is ambiguous; no earlier lifecycle was selected.")
                            ambiguous_run = True
                    lifecycle = ctx["task_events"].get(task_key)
                    if lifecycle is None:
                        lifecycle = dict(
                            event=emit(ctx, "Background task", source_ref=uuid),
                            parents=set(),
                            tools=set(),
                            agent_task=False,
                            ambiguous_run=False,
                            terminal=None,
                        )
                        ctx["task_events"][task_key] = lifecycle
                    lifecycle["ambiguous_run"] |= ambiguous_run
                    if tool_id is not None:
                        lifecycle["tools"].add(tool_id)
                    if subtype == "task_started":
                        lifecycle["parents"].add(_identifier(record.get("parent_task_id")))
                        lifecycle["agent_task"] |= record.get("task_type") in (
                            "local_agent",
                            "remote_agent",
                        )
                    task_event = lifecycle["event"]
                    if subtype == "task_notification":
                        if not isinstance(record.get("status"), str):
                            raise TraceImportError(
                                "Claude task notification requires a string status."
                            )
                        status = {
                            "completed": "completed",
                            "failed": "failed",
                            "stopped": "cancelled",
                        }.get(record.get("status"), "unknown")
                        lifecycle["terminal"] = record_order, status
                        task_event["status"] = status
                    else:
                        if task_event["status"] == "unknown":
                            task_event["status"] = "running"
                else:
                    _warn(run, "Unknown Claude system record was omitted; coverage is partial.")
            elif kind == "tool_progress":
                tool_id = _identifier(record.get("tool_use_id"))
                if tool_id in ctx["calls"] and not ctx["calls"][tool_id].get("_has_result"):
                    ctx["calls"][tool_id]["status"] = "running"
            else:
                _warn(run, "Unknown Claude record kind was omitted; coverage is partial.")
    result_bindings = tuple(resolved_tasks)
    scopes = defaultdict(list)
    call_owners = defaultdict(list)
    for ctx in contexts.values():
        scopes[ctx["file"], ctx["session"]].append(ctx)
        for tool_id in ctx["calls"]:
            call_owners[tool_id].append(ctx)
    for ctx in contexts.values():
        if not ctx["task_events"]:
            continue
        scope = scopes[ctx["file"], ctx["session"]]
        for (task_id, _), lifecycle in ctx["task_events"].items():
            owners = {}
            missing_owner = False
            for parent_task in lifecycle["parents"] or {None}:
                if parent_task is not None:
                    candidates = [
                        candidate for candidate in scope if candidate["agent"] == parent_task
                    ]
                    missing_owner |= len(candidates) != 1
                else:
                    candidates = [ctx] if ctx["agent"] is not None else scope
                for owner in candidates:
                    owners[owner["run"].id] = owner
            tool_ids = set(lifecycle["tools"])
            task_runs = [key for key in ctx["task_events"] if key[0] == task_id]
            if not tool_ids and len(task_runs) == 1 and not missing_owner:
                known = {
                    owner["calls"][tool_id]["id"]: (owner, tool_id)
                    for owner, agent_id, tool_id in result_bindings
                    if agent_id == task_id and owner["run"].id in owners
                }
                if len(known) == 1:
                    owner, tool_id = next(iter(known.values()))
                    owners = {owner["run"].id: owner}
                    tool_ids.add(tool_id)
            bindings = [
                (owner, tool_id)
                for owner in owners.values()
                for tool_id in tool_ids
                if tool_id in owner["calls"] and owner["calls"][tool_id].get("_has_call")
            ]
            unique_binding = (
                len(bindings) == 1
                and len(tool_ids) == 1
                and not missing_owner
                and not lifecycle["ambiguous_run"]
            )
            if lifecycle["terminal"] is not None:
                if unique_binding:
                    owner, tool_id = bindings[0]
                    data = owner["calls"][tool_id]
                    order, status = lifecycle["terminal"]
                    if order >= data.get("_result_order", -1):
                        data.update(status=status, _has_result=True, _result_order=order)
                else:
                    _warn(
                        ctx["run"],
                        "Task tool binding is missing or ambiguous; "
                        "no call completion was inferred.",
                    )
            if missing_owner:
                _warn(
                    ctx["run"],
                    "Task owner capture is missing or ambiguous; nesting is unavailable.",
                )
            if lifecycle["agent_task"] and not missing_owner and not lifecycle["ambiguous_run"]:
                if bindings:
                    resolved_tasks.extend((owner, task_id, tool_id) for owner, tool_id in bindings)
                elif len(owners) == 1:
                    resolved_tasks.append((next(iter(owners.values())), task_id, None))

    def relationships(child, scope, scoped_tasks, *, agent_only):
        if agent_only:
            authoritative = [
                (parent, tool_id)
                for parent, task_id, tool_id in scoped_tasks
                if task_id == child["agent"]
            ]
            if authoritative:
                return authoritative
        explicit = [
            (parent, native_id)
            for parent in scope
            for native_id in child["parents"]
            if native_id in parent["calls"]
        ]
        if explicit:
            owner_ids = {parent["run"].id for parent, _ in explicit}
            matched = [
                (parent, tool_id)
                for parent, task_id, tool_id in scoped_tasks
                if task_id == child["agent"] and parent["run"].id in owner_ids
            ]
            if not matched:
                related = {
                    (parent["run"].id, task_id)
                    for parent, task_id, tool_id in scoped_tasks
                    if tool_id in child["parents"] and parent["run"].id in owner_ids
                }
                matched = [
                    (parent, tool_id)
                    for parent, task_id, tool_id in scoped_tasks
                    if (parent["run"].id, task_id) in related
                ]
            return matched or explicit
        return []

    tasks_by_scope = defaultdict(list)
    tasks_by_owner = defaultdict(list)
    children_by_parent = defaultdict(list)
    for entry in resolved_tasks:
        owner = entry[0]
        tasks_by_scope[owner["file"], owner["session"]].append(entry)
        tasks_by_owner[owner["run"].id].append(entry)
    for child in contexts.values():
        if child["agent"] is None and not child["parents"]:
            continue
        scope_key = child["file"], child["session"]
        local = [parent for parent in scopes[scope_key] if parent is not child]
        local_tasks = [entry for entry in tasks_by_scope[scope_key] if entry[0] is not child]
        possible = relationships(child, local, local_tasks, agent_only=True)
        if not possible and child["parents"]:
            external = {
                parent["run"].id: parent
                for tool_id in child["parents"]
                for parent in call_owners[tool_id]
                if (parent["file"], parent["session"]) != scope_key
            }
            external_tasks = [entry for owner_id in external for entry in tasks_by_owner[owner_id]]
            possible = relationships(child, external.values(), external_tasks, agent_only=False)
        parent_ids = {parent["run"].id for parent, _ in possible}
        if len(parent_ids) == 1:
            child["run"].parent_run_id = next(iter(parent_ids))
            children_by_parent[child["run"].parent_run_id].append(child)
            invocation_ids = {
                parent["calls"][tool_id]["id"]
                for parent, tool_id in possible
                if tool_id in parent["calls"]
            }
            if len(invocation_ids) == 1 and all(
                tool_id in parent["calls"] for parent, tool_id in possible
            ):
                child["run"].parent_event_id = next(iter(invocation_ids))
            else:
                _warn(
                    child["run"],
                    "Child invocation is ambiguous; only the session parent is retained.",
                )
        elif possible:
            _warn(child["run"], "Child parent is ambiguous; no execution edge was inferred.")
        elif child["agent"] is not None or child["parents"]:
            _warn(child["run"], "Child parent is unavailable; no execution edge was inferred.")
    for parent, task_id, tool_id in resolved_tasks:
        if not any(
            child["run"].parent_run_id == parent["run"].id
            and (child["agent"] == task_id or tool_id in child["parents"])
            for child in children_by_parent[parent["run"].id]
            if child is not parent
        ):
            _warn(parent["run"], "Child capture is missing; child internals are unavailable.")
    for ctx in contexts.values():
        if any(
            not entry["closed"] for blocks in ctx["blocks"].values() for entry in blocks.values()
        ):
            _warn(ctx["run"], "Stream block completion is missing; capture is partial.")
        for data in ctx["calls"].values():
            if not data.get("_has_call"):
                _warn(
                    ctx["run"],
                    "Orphan tool result has no captured invocation; details are partial.",
                )
            if not data.get("_has_result") or data["status"] in ("unknown", "running"):
                _warn(ctx["run"], "Tool completion is missing or pending; capture is partial.")
            if (
                data["title"] in ("Tool: Agent", "Tool: Task")
                and not children_by_parent[ctx["run"].id]
            ):
                _warn(ctx["run"], "Child capture is missing; child internals are unavailable.")
    normalized = []
    for data in events:
        if "_chunks" in data:
            data["output"] = "".join(data.pop("_chunks"))
        data.pop("_has_call", None)
        data.pop("_has_result", None)
        data.pop("_aborted", None)
        data.pop("_result_order", None)
        truncated = False
        for field in ("input", "output", "explanation"):
            data[field], shortened = sanitize_text(data.get(field), options)
            truncated |= shortened
        try:
            normalized.append(TraceEvent(**data, truncated=truncated))
        except ValueError:
            raise TraceImportError("Claude event has invalid documented field values.") from None
    return finalize([ctx["run"] for ctx in contexts.values()], normalized, options)
