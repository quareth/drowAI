"""Continuation boundary for one already-running tool-execution session.

Caller graphs retain approval and initial dispatch. They enter this subgraph
only when dispatch exposes an active execution. The subgraph then owns bounded
runtime continuations until that execution reaches a terminal state; it does
not participate in ordinary completed tool processing. Process-local state and
LLM decision prompting are delegated to focused sibling modules.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, MutableMapping
from typing import Any

from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph

from agent.tool_runtime.batch.ids import mint_tool_batch_id, mint_tool_call_id
from agent.tool_runtime.batch.plan_view import (
    primary_tool_call_from_metadata,
    serialized_tool_calls_from_metadata,
)
from agent.tool_runtime.output_persistence_policy import resolve_output_persistence
from core.prompts.builders.post_tool.evidence import (
    aggregate_compact_evidence_rows,
    read_compact_evidence,
    register_runtime_compact_evidence,
)
from runtime_shared.shell_capabilities import (
    SHELL_WRITE_STDIN_TOOL_ID,
    resolve_shell_session_start_capability,
)
from runtime_shared.durable_secret_masking import mask_durable_secrets
from runtime_shared.shell_session_contracts import (
    ShellInteractionAction,
    ShellProcessStatus,
    ShellSessionErrorCode,
    ShellSessionIdentity,
    ShellSessionLifecycleStatus,
    ShellSessionUpdate,
    ShellWaitRequest,
    ShellWriteRequest,
    project_shell_session_artifacts,
)
from runtime_shared.shell_session_port import get_shell_session_service

from ..builders.common_edges import (
    WrapperLogCallback,
    with_interactive_state,
    wrap_with_context,
    wrap_with_context_async,
)
from ..runtime_controls import (
    read_active_execution_control,
    read_execution_session_control,
    set_execution_session_control,
    set_active_execution_control,
)
from ..state import InteractiveState
from .shell_interaction_decision import (
    call_shell_interaction_decision as _call_interaction_decision_boundary,
    normalize_shell_interaction_decision as _normalize_interaction_decision,
)
from .tool_execution_session_state import (
    abort_execution_session_state,
    append_execution_session_evidence,
    append_shell_interaction_transcript,
    begin_execution_session_state,
    finish_execution_session_state,
    infer_shell_interaction_boundary as _infer_boundary,
    materialize_terminal_session_output as _materialize_terminal_session_output,
    read_shell_interaction_transcript,
    remember_shell_input as _remember_shell_input,
)

_SHELL_INTERACTION_WAIT_BATCH_KEY = "shell_interaction_wait_batch"


def initialize_tool_execution_session(
    state: Mapping[str, Any] | InteractiveState,
    context: Any = None,
) -> dict[str, Any]:
    """Open runtime-only aggregation after dispatch exposes active execution."""

    _ = context
    interactive = InteractiveState.from_mapping(state)
    metadata = interactive.facts.ensure_metadata()
    active = read_active_execution_control(metadata)
    if active is None:
        return interactive.as_graph_update()
    turn_sequence = metadata.get("turn_sequence")
    if not isinstance(turn_sequence, int):
        return interactive.as_graph_update()

    active_call_id = str(active.get("originating_tool_call_id") or "").strip()
    active_tool_id = str(active.get("originating_tool_id") or "").strip()
    calls = serialized_tool_calls_from_metadata(metadata)
    active_call = next(
        (call for call in calls if call.tool_call_id == active_call_id),
        None,
    )
    if active_call is None and not active_call_id and active_tool_id:
        matching_calls = [call for call in calls if call.tool_id == active_tool_id]
        active_call = matching_calls[0] if len(matching_calls) == 1 else None
    if active_call is None or (
        active_tool_id and active_call.tool_id != active_tool_id
    ):
        raise ValueError("Active shell execution is missing from the prepared batch")

    batch = metadata.get("planner_plan")
    batch_view = batch.get("tool_batch") if isinstance(batch, Mapping) else None
    planned_batch_id = (
        str(batch_view.get("tool_batch_id") or "").strip()
        if isinstance(batch_view, Mapping)
        else ""
    )
    sequence_id = str(active.get("originating_tool_batch_id") or "").strip()
    if not sequence_id:
        sequence_id = planned_batch_id
    if not sequence_id:
        sequence_id = str(metadata.get("tool_batch_id") or "").strip()
    if not sequence_id:
        raise ValueError("Prepared tool batch is missing its execution-session id")

    begin_execution_session_state(
        sequence_id=sequence_id,
        originating_tool_id=active_call.tool_id,
        originating_parameters=active_call.parameters,
    )
    set_execution_session_control(
        metadata,
        turn_sequence=turn_sequence,
        sequence_id=sequence_id,
        originating_tool_id=active_call.tool_id,
        originating_tool_call_id=active_call.tool_call_id,
        originating_tool_batch_id=sequence_id,
    )
    interactive.facts.metadata = metadata
    return interactive.as_graph_update()


def collect_tool_execution_session_result(
    state: Mapping[str, Any] | InteractiveState,
    context: Any = None,
) -> dict[str, Any]:
    """Append the latest batch and publish one aggregate at terminal state."""

    _ = context
    interactive = InteractiveState.from_mapping(state)
    metadata = interactive.facts.ensure_metadata()
    session = read_execution_session_control(metadata)
    if session is None:
        return interactive.as_graph_update()

    evidence = read_compact_evidence(metadata, prefer_runtime=True)
    if evidence is None:
        raise RuntimeError("Tool execution session produced no compact evidence")
    sequence_id = str(session["sequence_id"])
    if read_shell_interaction_transcript(sequence_id) is None:
        _publish_unavailable_execution_session(metadata, session=session)
        interactive.facts.metadata = metadata
        return interactive.as_graph_update()
    try:
        append_execution_session_evidence(sequence_id, evidence)
    except KeyError:
        _publish_unavailable_execution_session(metadata, session=session)
        interactive.facts.metadata = metadata
        return interactive.as_graph_update()
    append_shell_interaction_transcript(
        sequence_id=sequence_id,
        evidence=evidence,
        metadata=metadata,
    )
    metadata.pop(_SHELL_INTERACTION_WAIT_BATCH_KEY, None)
    if read_active_execution_control(metadata) is not None:
        return interactive.as_graph_update()

    transcript = read_shell_interaction_transcript(sequence_id)
    if transcript is None:
        _publish_unavailable_execution_session(metadata, session=session)
        interactive.facts.metadata = metadata
        return interactive.as_graph_update()
    final_batch_id = str(metadata.get("tool_batch_id") or "").strip()
    try:
        aggregate = finish_execution_session_state(
            sequence_id,
            tool_batch_id=final_batch_id,
        )
    except KeyError:
        _publish_unavailable_execution_session(metadata, session=session)
        interactive.facts.metadata = metadata
        return interactive.as_graph_update()
    aggregate = _materialize_terminal_session_output(
        aggregate,
        transcript=transcript,
    )
    terminal_rows = aggregate.get("results")
    terminal_compact = (
        terminal_rows[-1].get("compact_tool_result")
        if isinstance(terminal_rows, list)
        and terminal_rows
        and isinstance(terminal_rows[-1], Mapping)
        else None
    )
    register_runtime_compact_evidence(
        aggregate,
        single_compact=(
            terminal_compact if isinstance(terminal_compact, Mapping) else None
        ),
    )
    durable_rows = [
        dict(row)
        for row in aggregate.get("results", [])
        if isinstance(row, Mapping) and _row_is_durable(row)
    ]
    if durable_rows:
        durable_aggregate = aggregate_compact_evidence_rows(
            tool_batch_id=final_batch_id,
            rows=durable_rows,
        )
        metadata["last_tool_result_compact_batch"] = dict(durable_aggregate)
        last_compact = durable_rows[-1].get("compact_tool_result")
        if isinstance(last_compact, Mapping):
            metadata["last_tool_result_compact"] = dict(last_compact)
    else:
        metadata.pop("last_tool_result_compact_batch", None)
        metadata.pop("last_tool_result_compact", None)

    turn_sequence = metadata.get("turn_sequence")
    if isinstance(turn_sequence, int):
        set_execution_session_control(
            metadata,
            turn_sequence=turn_sequence,
            sequence_id=None,
        )
    interactive.facts.metadata = metadata
    return interactive.as_graph_update()


async def coordinate_shell_interaction(
    state: Mapping[str, Any] | InteractiveState,
    context: Any = None,
    config: Mapping[str, Any] | None = None,
    *,
    decide_fn: Callable[..., Any] | None = None,
    wait_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Choose and apply one semantic action for a live shell session."""

    interactive = InteractiveState.from_mapping(state)
    metadata = interactive.facts.ensure_metadata()
    session = read_execution_session_control(metadata)
    active = read_active_execution_control(metadata)
    turn_sequence = metadata.get("turn_sequence")
    if session is None or active is None or not isinstance(turn_sequence, int):
        return interactive.as_graph_update()

    sequence_id = str(session["sequence_id"])
    if read_shell_interaction_transcript(sequence_id) is None:
        _publish_unavailable_execution_session(metadata, session=session)
        interactive.facts.metadata = metadata
        return interactive.as_graph_update()
    decision = _normalize_interaction_decision(
        await _call_interaction_decision_boundary(
            interactive=interactive,
            metadata=metadata,
            session=session,
            active=active,
            context=context,
            config=config,
            decide_fn=decide_fn,
        )
    )
    action = decision["action"]
    input_chars = (
        "\u0003"
        if action == ShellInteractionAction.INTERRUPT.value
        else str(decision["chars"])
    )
    if action in {
        ShellInteractionAction.SEND_INPUT.value,
        ShellInteractionAction.INTERRUPT.value,
    }:
        update = await _write_shell_input(
            interactive=interactive,
            metadata=metadata,
            active=active,
            context=context,
            chars=input_chars,
        )
        if decision.get("coordination_failed"):
            update = update.model_copy(
                update={
                    "summary": (
                        "Interactive session coordination failed; an interrupt was "
                        "sent to avoid leaving the command stuck."
                    )
                }
            )
        _publish_direct_shell_update(
            metadata,
            sequence_id=sequence_id,
            session=session,
            active=active,
            update=update,
            row_tool_id=SHELL_WRITE_STDIN_TOOL_ID,
            intent=(
                "Interrupt the existing shell session."
                if action == ShellInteractionAction.INTERRUPT.value
                else "Send input to the existing live shell session."
            ),
            input_chars=input_chars,
        )
    else:
        update = await _wait_for_shell_output(
            interactive=interactive,
            metadata=metadata,
            active=active,
            context=context,
            wait_fn=wait_fn,
        )
        _publish_direct_shell_update(
            metadata,
            sequence_id=sequence_id,
            session=session,
            active=active,
            update=update,
            row_tool_id=str(
                session.get("originating_tool_id")
                or active.get("originating_tool_id")
                or ""
            ).strip(),
            intent="Runtime-owned wait for the existing shell session.",
        )
    interactive.facts.metadata = metadata
    return interactive.as_graph_update()


def _row_is_durable(row: Mapping[str, Any]) -> bool:
    """Apply the canonical per-tool persistence policy to one aggregate row."""

    compact = row.get("compact_tool_result")
    return resolve_output_persistence(
        row.get("tool_id"),
        compact if isinstance(compact, Mapping) else None,
    ).retain_durable_output


def _route_after_collection(interactive: InteractiveState) -> str:
    """Continue only while the runtime exposes an active execution."""

    if read_active_execution_control(interactive.facts.safe_metadata) is not None:
        return "shell_interaction"
    return "terminal"


def _route_after_shell_interaction(interactive: InteractiveState) -> str:
    """Collect direct continuation results or keep coordinating a live session."""

    metadata = interactive.facts.safe_metadata
    if metadata.get(_SHELL_INTERACTION_WAIT_BATCH_KEY):
        return "collect_result"
    if read_active_execution_control(metadata) is not None:
        return "shell_interaction"
    return "collect_result"


def route_after_tool_dispatch(interactive: InteractiveState) -> str:
    """Route only a genuinely running execution into session continuation."""

    if read_active_execution_control(interactive.facts.safe_metadata) is not None:
        return "execution_session"
    return "terminal"


def build_tool_execution_session_subgraph(
    *,
    build_only: bool = False,
    initialize_fn: Callable[..., Any] = initialize_tool_execution_session,
    collect_fn: Callable[..., Any] = collect_tool_execution_session_result,
    interaction_fn: Callable[..., Any] = coordinate_shell_interaction,
    decide_interaction_fn: Callable[..., Any] | None = None,
    wait_interaction_fn: Callable[..., Any] | None = None,
    on_wrap_log: WrapperLogCallback | None = None,
) -> Any:
    """Build continuation for an execution proven active by initial dispatch."""

    def _collect_node(
        state: Mapping[str, Any],
        context: Any = None,
    ) -> dict[str, Any]:
        try:
            return collect_fn(state, context)
        except Exception:
            _abort_execution_session_from_state(state)
            raise

    async def _interaction_node(
        state: Mapping[str, Any],
        context: Any = None,
        config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            return await interaction_fn(
                state,
                context,
                config,
                decide_fn=decide_interaction_fn,
                wait_fn=wait_interaction_fn,
            )
        except (asyncio.CancelledError, Exception):
            _abort_execution_session_from_state(state)
            raise

    graph = StateGraph(dict)
    graph.add_node(
        "initialize",
        wrap_with_context(
            initialize_fn,
            node_name="tool_execution_session.initialize",
            on_wrap_log=on_wrap_log,
        ),
    )
    graph.add_node(
        "collect_result",
        wrap_with_context(
            _collect_node,
            node_name="tool_execution_session.collect_result",
            on_wrap_log=on_wrap_log,
        ),
    )
    graph.add_node(
        "shell_interaction",
        wrap_with_context_async(
            _interaction_node,
            node_name="tool_execution_session.shell_interaction",
            on_wrap_log=on_wrap_log,
        ),
    )
    graph.set_entry_point("initialize")
    graph.add_edge("initialize", "collect_result")
    graph.add_conditional_edges(
        "collect_result",
        with_interactive_state(_route_after_collection),
        {
            "shell_interaction": "shell_interaction",
            "terminal": END,
        },
    )
    graph.add_conditional_edges(
        "shell_interaction",
        with_interactive_state(_route_after_shell_interaction),
        {
            "collect_result": "collect_result",
            "shell_interaction": "shell_interaction",
        },
    )
    return graph if build_only else graph.compile()


def _abort_execution_session_from_state(state: Mapping[str, Any]) -> None:
    """Discard the process-local session referenced by graph state."""

    interactive = InteractiveState.from_mapping(state)
    session = read_execution_session_control(interactive.facts.safe_metadata)
    if session is not None:
        abort_execution_session_state(str(session.get("sequence_id") or ""))


async def _write_shell_input(
    *,
    interactive: InteractiveState,
    metadata: Mapping[str, Any],
    active: Mapping[str, Any],
    context: Any,
    chars: str,
) -> ShellSessionUpdate:
    session_id = str(active.get("session_id") or "").strip()
    if not session_id or not chars:
        return _unavailable_update(
            session_id=session_id or None,
            summary="Shell session input could not be sent.",
        )
    identity = _shell_session_identity_from_context(
        interactive=interactive,
        metadata=metadata,
        context=context,
    )
    if identity is None:
        return _unavailable_update(
            session_id=session_id,
            summary="Shell session is unavailable; runtime identity is missing.",
        )
    service = get_shell_session_service()
    return await service.write_stdin(
        identity=identity,
        request=ShellWriteRequest(session_id=session_id, chars=chars),
    )


async def _wait_for_shell_output(
    *,
    interactive: InteractiveState,
    metadata: Mapping[str, Any],
    active: Mapping[str, Any],
    context: Any,
    wait_fn: Callable[..., Any] | None,
) -> ShellSessionUpdate:
    if wait_fn is not None:
        result = wait_fn(
            interactive=interactive,
            metadata=metadata,
            active=active,
            context=context,
        )
        if hasattr(result, "__await__"):
            result = await result
        if isinstance(result, ShellSessionUpdate):
            return result
    identity = _shell_session_identity_from_context(
        interactive=interactive,
        metadata=metadata,
        context=context,
    )
    if identity is None:
        return _unavailable_update(
            session_id=str(active.get("session_id") or "").strip() or None,
            summary="Shell session is unavailable; runtime identity is missing.",
        )
    service = get_shell_session_service()
    return await service.wait_for_output(
        identity=identity,
        request=ShellWaitRequest(
            session_id=str(active.get("session_id") or "").strip(),
        ),
    )


def _shell_session_identity_from_context(
    *,
    interactive: InteractiveState,
    metadata: Mapping[str, Any],
    context: Any,
) -> ShellSessionIdentity | None:
    """Build authority identity from immutable graph context when available."""

    def _read(name: str) -> Any:
        if context is not None and hasattr(context, name):
            return getattr(context, name)
        metadata_value = metadata.get(name)
        return metadata_value if isinstance(metadata_value, (str, int)) else None

    def _read_optional_text(name: str) -> str | None:
        value = (
            getattr(context, name)
            if context is not None and hasattr(context, name)
            else metadata.get(name)
        )
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    tenant_id = _read("tenant_id")
    task_id = _read("task_id") or interactive.facts.task_id
    execution_owner_id = _read("execution_owner_id")
    runtime_placement_mode = _read("runtime_placement_mode")
    workspace_id = _read("workspace_id")
    missing = [
        name
        for name, value in (
            ("tenant_id", tenant_id),
            ("task_id", task_id),
            ("execution_owner_id", execution_owner_id),
            ("runtime_placement_mode", runtime_placement_mode),
            ("workspace_id", workspace_id),
        )
        if value in (None, "")
    ]
    if missing:
        return None
    return ShellSessionIdentity(
        tenant_id=int(tenant_id),
        task_id=int(task_id),
        execution_owner_id=str(execution_owner_id).strip(),
        runtime_placement_mode=str(runtime_placement_mode).strip().lower(),  # type: ignore[arg-type]
        workspace_id=str(workspace_id).strip(),
        workspace_path=_read_optional_text("workspace_path"),
        runner_id=_read_optional_text("runner_id"),
        execution_site_id=_read_optional_text("execution_site_id"),
    )


def _publish_direct_shell_update(
    metadata: MutableMapping[str, Any],
    *,
    sequence_id: str,
    session: Mapping[str, Any],
    active: Mapping[str, Any],
    update: ShellSessionUpdate,
    row_tool_id: str,
    intent: str,
    input_chars: str | None = None,
) -> None:
    turn_sequence = metadata.get("turn_sequence")
    if not isinstance(turn_sequence, int):
        return
    batch_id = mint_tool_batch_id()
    call_id = mint_tool_call_id()
    originating_tool = str(
        session.get("originating_tool_id") or active.get("originating_tool_id") or ""
    ).strip()
    normalized_row_tool_id = str(row_tool_id or originating_tool).strip()
    compact = _compact_from_shell_update(
        tool_id=normalized_row_tool_id,
        update=update,
        originating_tool_id=originating_tool,
    )
    originating_call_id = _originating_tool_call_id(metadata, session=session)
    originating_batch_id = _originating_tool_batch_id(session, fallback=sequence_id)
    _emit_shell_lifecycle_progress(
        metadata,
        tool_id=originating_tool or normalized_row_tool_id,
        tool_call_id=originating_call_id,
        tool_batch_id=originating_batch_id,
        compact=compact,
        success=bool(update.success),
    )
    row = {
        "tool_call_id": call_id,
        "tool_id": normalized_row_tool_id,
        "intent": intent,
        "status": "success" if update.success else "failed",
        "success": bool(update.success),
        "compact_tool_result": compact,
    }
    batch = _aggregate_wait_row(batch_id=batch_id, row=row)
    metadata["tool_batch_id"] = batch_id
    metadata[_SHELL_INTERACTION_WAIT_BATCH_KEY] = True
    metadata["last_tool_result_compact_batch"] = batch
    metadata["last_tool_result_compact"] = compact
    if input_chars:
        _remember_shell_input(
            sequence_id=sequence_id,
            call_id=call_id,
            chars=input_chars,
        )
    if update.process_status is ShellProcessStatus.RUNNING and update.session_id:
        set_active_execution_control(
            metadata,
            turn_sequence=turn_sequence,
            active_execution={
                "originating_tool_id": originating_tool,
                "originating_tool_call_id": originating_call_id,
                "originating_tool_batch_id": originating_batch_id,
                "continuation_tool_id": SHELL_WRITE_STDIN_TOOL_ID,
                "process_status": "running",
                "session_id": update.session_id,
                "stdin_available": update.stdin_available,
            },
        )
    else:
        set_active_execution_control(
            metadata,
            turn_sequence=turn_sequence,
            active_execution=None,
        )


def _originating_tool_call_id(
    metadata: Mapping[str, Any],
    *,
    session: Mapping[str, Any] | None = None,
) -> str | None:
    """Return the original shell card id for correlated session updates."""

    if isinstance(session, Mapping):
        normalized = str(session.get("originating_tool_call_id") or "").strip()
        if normalized:
            return normalized
    primary_call = primary_tool_call_from_metadata(metadata)
    if primary_call is None:
        return None
    normalized = str(primary_call.tool_call_id or "").strip()
    return normalized or None


def _originating_tool_batch_id(
    session: Mapping[str, Any],
    *,
    fallback: str,
) -> str | None:
    """Return the original shell batch id for correlated session updates."""

    for value in (
        session.get("originating_tool_batch_id"),
        session.get("sequence_id"),
        fallback,
    ):
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return None


def _emit_shell_lifecycle_progress(
    metadata: Mapping[str, Any],
    *,
    tool_id: str,
    tool_call_id: str | None,
    tool_batch_id: str | None,
    compact: Mapping[str, Any],
    success: bool,
) -> None:
    """Publish one bounded UI lifecycle update for an interactive shell session."""

    if not tool_call_id:
        return
    process_status = str(compact.get("process_status") or "").strip().lower()
    session_status = str(compact.get("session_status") or "").strip().lower()
    boundary = str(compact.get("interaction_boundary") or "").strip().lower()
    if not process_status and not session_status and not boundary:
        return
    event_type = "tool_delta" if process_status == ShellProcessStatus.RUNNING.value else "tool_end"
    try:
        writer = get_stream_writer()
    except RuntimeError:
        writer = None
    if writer is None:
        return
    output = "\n".join(
        part
        for part in (
            str(compact.get("stdout") or "").strip(),
            str(compact.get("stderr") or "").strip(),
        )
        if part
    )
    if not output:
        output = str(compact.get("summary") or "").strip()
    display_compact = {
        "schema_version": "2.0",
        "tool": tool_id,
        "status": "success" if success else "failed",
        "success": success,
        "exit_code": compact.get("exit_code"),
        "summary": output[:4000],
        "key_findings": [],
        "errors": [],
        "report_recommendations": [],
        "structured_signals": [],
        "decision_evidence": [],
        "lossiness_risk": "high",
        "artifact_refs": [],
        "compression": None,
        "process_status": process_status or None,
        "session_status": session_status or None,
        "interaction_boundary": boundary or None,
        "session_id": compact.get("session_id"),
    }
    masked_display_compact = mask_durable_secrets(
        display_compact,
        source="last_tool_result_compact",
    )
    masked_output = str(masked_display_compact.get("summary") or "")
    writer(
        {
            "type": event_type,
            "tool": tool_id,
            "tool_call_id": tool_call_id,
            "tool_batch_id": tool_batch_id,
            "conversation_id": metadata.get("conversation_id"),
            "turn_id": metadata.get("turn_id"),
            "turn_sequence": metadata.get("turn_sequence"),
            "status": "success" if success else "error",
            "process_status": process_status or None,
            "session_status": session_status or None,
            "interaction_boundary": boundary or None,
            "session_id": compact.get("session_id"),
            "exit_code": compact.get("exit_code"),
            "content": masked_output,
            "summary": {"summary": masked_output},
            "compact_tool_result": masked_display_compact,
            "output_persistence": "transient",
            "shell_lifecycle_event": True,
        }
    )


def _aggregate_wait_row(*, batch_id: str, row: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "tool_batch_id": batch_id,
        "status": "completed" if row.get("success") else "failed",
        "success": bool(row.get("success")),
        "results": [dict(row)],
        "deferred_followups": [],
    }


def _compact_from_shell_update(
    *,
    tool_id: str,
    update: ShellSessionUpdate,
    originating_tool_id: str,
) -> dict[str, Any]:
    process_status = (
        update.process_status.value
        if isinstance(update.process_status, ShellProcessStatus)
        else update.process_status
    )
    session_status = (
        update.session_status.value
        if isinstance(update.session_status, ShellSessionLifecycleStatus)
        else update.session_status
    )
    boundary = (
        update.interaction_boundary.value
        if update.interaction_boundary is not None
        else _infer_boundary({"process_status": process_status, "stdout": update.stdout, "stderr": update.stderr})
    )
    error_code = (
        update.error_code.value
        if isinstance(update.error_code, ShellSessionErrorCode)
        else update.error_code
    )
    runtime_session = {
        "originating_tool_id": originating_tool_id,
    }
    originating_capability = resolve_shell_session_start_capability(
        originating_tool_id
    )
    if originating_capability is not None:
        runtime_session["originating_capability"] = originating_capability.value
    artifact_projection = project_shell_session_artifacts(
        update,
        originating_capability,
    )
    artifact_capture = artifact_projection.get("artifact_capture")
    if isinstance(artifact_capture, dict):
        runtime_session["artifact_capture"] = artifact_capture
    result_metadata = {"runtime_session": runtime_session}
    artifact_scope = artifact_projection.get("artifact_scope")
    if isinstance(artifact_scope, str):
        result_metadata["artifact_scope"] = artifact_scope
    return {
        "schema_version": "2.0",
        "tool": tool_id,
        "status": "success" if update.success else "failed",
        "success": bool(update.success),
        "summary": update.summary,
        "process_status": process_status,
        "session_status": session_status,
        "interaction_boundary": boundary,
        "session_id": update.session_id,
        "stdout": update.stdout,
        "stdout_ends_with_newline": update.stdout_ends_with_newline,
        "stderr": update.stderr,
        "artifacts": list(artifact_projection["artifacts"]),
        "exit_code": update.exit_code,
        "stdin_available": update.stdin_available,
        "truncated": update.truncated,
        "error_code": error_code,
        "metadata": result_metadata,
    }


def _publish_unavailable_execution_session(
    metadata: MutableMapping[str, Any],
    *,
    session: Mapping[str, Any],
) -> None:
    sequence_id = str(session.get("sequence_id") or "").strip()
    originating_tool = str(session.get("originating_tool_id") or "").strip()
    update = _unavailable_update(summary="Shell session state is unavailable.")
    compact = _compact_from_shell_update(
        tool_id=originating_tool,
        update=update,
        originating_tool_id=originating_tool,
    )
    batch_id = str(metadata.get("tool_batch_id") or sequence_id or "shell-session-unavailable")
    row = {
        "tool_call_id": f"{batch_id}:unavailable",
        "tool_id": originating_tool,
        "intent": "Fail closed because process-local shell session state was lost.",
        "status": "failed",
        "success": False,
        "compact_tool_result": compact,
        "failure_category": "tool_unavailable",
        "error_message": update.summary,
    }
    metadata["last_tool_result_compact_batch"] = _aggregate_wait_row(
        batch_id=batch_id,
        row=row,
    )
    metadata["last_tool_result_compact"] = compact
    turn_sequence = metadata.get("turn_sequence")
    if isinstance(turn_sequence, int):
        set_active_execution_control(
            metadata,
            turn_sequence=turn_sequence,
            active_execution=None,
        )
        set_execution_session_control(
            metadata,
            turn_sequence=turn_sequence,
            sequence_id=None,
        )
    abort_execution_session_state(sequence_id)


def _unavailable_update(
    *,
    session_id: str | None = None,
    summary: str,
) -> ShellSessionUpdate:
    return ShellSessionUpdate(
        success=False,
        status="error",
        process_status=ShellProcessStatus.FAILED,
        session_status=ShellSessionLifecycleStatus.UNAVAILABLE,
        interaction_boundary=None,
        session_id=session_id,
        stdout="",
        stderr="",
        exit_code=None,
        stdin_available=False,
        truncated=False,
        duration_ms=0,
        summary=summary,
        error_code=ShellSessionErrorCode.SESSION_UNAVAILABLE,
    )


__all__ = [
    "abort_execution_session_state",
    "append_execution_session_evidence",
    "append_shell_interaction_transcript",
    "begin_execution_session_state",
    "build_tool_execution_session_subgraph",
    "collect_tool_execution_session_result",
    "coordinate_shell_interaction",
    "finish_execution_session_state",
    "initialize_tool_execution_session",
    "read_shell_interaction_transcript",
    "route_after_tool_dispatch",
]
