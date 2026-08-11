"""Continuation boundary for one already-running tool-execution session.

Caller graphs retain approval and initial dispatch. They enter this subgraph
only when dispatch exposes an active execution. The subgraph then owns bounded
runtime continuations until that execution reaches a terminal state; it does
not participate in ordinary completed tool processing. One process-local
session record owns transcript, evidence, and pending-input state together.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from typing import Any
from uuid import uuid4

from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph

from agent.providers.llm.core.base import StructuredOutputSpec
from agent.tool_runtime.batch.plan_view import primary_tool_call_from_metadata
from agent.tool_runtime.output_persistence_policy import resolve_output_persistence
from core.llm import ROLE_REASONING_MAIN
from runtime_shared.shell_capabilities import SHELL_WRITE_STDIN_TOOL_ID
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
)
from runtime_shared.shell_session_port import get_shell_session_service
from core.prompts.builders.post_tool.evidence import (
    EvidenceView,
    aggregate_compact_evidence_rows,
    read_compact_evidence,
    register_runtime_compact_evidence,
)

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
from ..utils.llm_resolver import resolve_llm_client
_EXECUTION_SESSION_CACHE_LIMIT = 128
_TRANSCRIPT_MAX_ENTRIES = 24
_TRANSCRIPT_MAX_CHARS = 12_000
_SHELL_INTERACTION_WAIT_BATCH_KEY = "shell_interaction_wait_batch"
_RUNTIME_EXECUTION_SESSIONS: OrderedDict[str, dict[str, Any]] = OrderedDict()
_INTERACTION_DECISION_STRUCTURED_OUTPUT = StructuredOutputSpec(
    name="shell_interaction_decision",
    schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    ShellInteractionAction.SEND_INPUT.value,
                    ShellInteractionAction.WAIT_FOR_OUTPUT.value,
                    ShellInteractionAction.INTERRUPT.value,
                ],
            },
            "chars": {"type": ["string", "null"]},
            "reasoning": {"type": "string"},
        },
        "required": ["action", "chars", "reasoning"],
        "additionalProperties": False,
    },
    strict=True,
)

logger = logging.getLogger(__name__)


def initialize_tool_execution_session(
    state: Mapping[str, Any] | InteractiveState,
    context: Any = None,
) -> dict[str, Any]:
    """Open runtime-only aggregation after dispatch exposes active execution."""

    _ = context
    interactive = InteractiveState.from_mapping(state)
    metadata = interactive.facts.ensure_metadata()
    if read_active_execution_control(metadata) is None:
        return interactive.as_graph_update()
    turn_sequence = metadata.get("turn_sequence")
    primary_call = primary_tool_call_from_metadata(metadata)
    if not isinstance(turn_sequence, int) or primary_call is None:
        return interactive.as_graph_update()

    batch = metadata.get("planner_plan")
    batch_view = batch.get("tool_batch") if isinstance(batch, Mapping) else None
    sequence_id = (
        str(batch_view.get("tool_batch_id") or "").strip()
        if isinstance(batch_view, Mapping)
        else ""
    )
    if not sequence_id:
        sequence_id = str(metadata.get("tool_batch_id") or "").strip()
    if not sequence_id:
        raise ValueError("Prepared tool batch is missing its execution-session id")

    begin_execution_session_state(
        sequence_id=sequence_id,
        originating_tool_id=primary_call.tool_id,
        originating_parameters=primary_call.parameters,
    )
    set_execution_session_control(
        metadata,
        turn_sequence=turn_sequence,
        sequence_id=sequence_id,
        originating_tool_id=primary_call.tool_id,
        originating_tool_call_id=primary_call.tool_call_id,
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


def begin_execution_session_state(
    *,
    sequence_id: str,
    originating_tool_id: str,
    originating_parameters: Mapping[str, Any],
) -> None:
    """Start one bounded process-local execution session atomically."""

    normalized = str(sequence_id or "").strip()
    if not normalized:
        raise ValueError("Shell interaction transcript requires a sequence id")
    command = str(originating_parameters.get("command") or "").strip()
    _RUNTIME_EXECUTION_SESSIONS[normalized] = {
        "sequence_id": normalized,
        "originating_tool_id": str(originating_tool_id or "").strip(),
        "originating_command": command,
        "evidence_rows": [],
        "entries": [],
        "pending_inputs": {},
        "compacted": False,
        "omitted_entries": 0,
    }
    _RUNTIME_EXECUTION_SESSIONS.move_to_end(normalized)
    while len(_RUNTIME_EXECUTION_SESSIONS) > _EXECUTION_SESSION_CACHE_LIMIT:
        _RUNTIME_EXECUTION_SESSIONS.popitem(last=False)


def append_execution_session_evidence(
    sequence_id: str,
    evidence: EvidenceView,
) -> None:
    """Append deduplicated evidence rows to an active execution session."""

    normalized = str(sequence_id or "").strip()
    session = _RUNTIME_EXECUTION_SESSIONS.get(normalized)
    if session is None:
        raise KeyError(f"Unknown runtime execution session: {normalized}")
    rows = session["evidence_rows"]
    known_call_ids = {
        str(row.get("tool_call_id") or "").strip()
        for row in rows
        if str(row.get("tool_call_id") or "").strip()
    }
    for row in evidence.rows:
        call_id = str(row.get("tool_call_id") or "").strip()
        if call_id and call_id in known_call_ids:
            continue
        rows.append(dict(row))
        if call_id:
            known_call_ids.add(call_id)
    _RUNTIME_EXECUTION_SESSIONS.move_to_end(normalized)


def finish_execution_session_state(
    sequence_id: str,
    *,
    tool_batch_id: str,
) -> Mapping[str, Any]:
    """Remove one complete session and return its aggregate evidence."""

    normalized = str(sequence_id or "").strip()
    final_batch_id = str(tool_batch_id or "").strip()
    if not final_batch_id:
        raise ValueError("Final tool batch id is required")
    session = _RUNTIME_EXECUTION_SESSIONS.pop(normalized, None)
    if session is None:
        raise KeyError(f"Unknown runtime execution session: {normalized}")
    rows = session["evidence_rows"]
    aggregate = aggregate_compact_evidence_rows(
        tool_batch_id=final_batch_id,
        rows=rows,
    )
    return aggregate


def abort_execution_session_state(sequence_id: str) -> None:
    """Discard all process-local state for an interrupted execution session."""

    _RUNTIME_EXECUTION_SESSIONS.pop(str(sequence_id or "").strip(), None)


def _abort_execution_session_from_state(state: Mapping[str, Any]) -> None:
    """Discard the process-local session referenced by graph state."""

    interactive = InteractiveState.from_mapping(state)
    session = read_execution_session_control(interactive.facts.safe_metadata)
    if session is not None:
        abort_execution_session_state(str(session.get("sequence_id") or ""))


def append_shell_interaction_transcript(
    *,
    sequence_id: str,
    evidence: EvidenceView,
    metadata: Mapping[str, Any],
) -> None:
    """Append compact evidence rows to the current process-local transcript."""

    transcript = _RUNTIME_EXECUTION_SESSIONS.get(str(sequence_id or "").strip())
    if transcript is None:
        return
    pending_inputs = transcript.get("pending_inputs")
    if not isinstance(pending_inputs, MutableMapping):
        pending_inputs = {}
        transcript["pending_inputs"] = pending_inputs
    params_by_call_id = _tool_parameters_by_call_id(metadata)
    for row in evidence.rows:
        compact = row.get("compact_tool_result")
        compact_map = compact if isinstance(compact, Mapping) else {}
        call_id = str(row.get("tool_call_id") or "").strip()
        parameters = params_by_call_id.get(call_id, {})
        chars = ""
        if str(row.get("tool_id") or "") == SHELL_WRITE_STDIN_TOOL_ID:
            chars = str(
                pending_inputs.pop(call_id, "")
                or parameters.get("chars")
                or ""
            )
        entry = {
            "tool_id": str(row.get("tool_id") or ""),
            "tool_call_id": call_id,
            "input": chars or None,
            "stdout": str(compact_map.get("stdout") or ""),
            "stderr": str(compact_map.get("stderr") or ""),
            "boundary": _infer_boundary(compact_map),
            "process_status": compact_map.get("process_status"),
            "session_id": compact_map.get("session_id"),
            "exit_code": compact_map.get("exit_code"),
            "truncated": bool(compact_map.get("truncated")),
        }
        transcript["entries"].append(entry)
    _compact_shell_interaction_transcript(transcript)
    _RUNTIME_EXECUTION_SESSIONS.move_to_end(str(sequence_id or "").strip())


def read_shell_interaction_transcript(sequence_id: str) -> Mapping[str, Any] | None:
    """Return a copy of the process-local transcript for tests/decision prompts."""

    transcript = _RUNTIME_EXECUTION_SESSIONS.get(str(sequence_id or "").strip())
    if transcript is None:
        return None
    copied = dict(transcript)
    copied["entries"] = [dict(entry) for entry in transcript.get("entries", [])]
    copied.pop("evidence_rows", None)
    copied.pop("pending_inputs", None)
    return copied


def _materialize_terminal_session_output(
    aggregate: Mapping[str, Any],
    *,
    transcript: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Copy bounded session streams into the aggregate's terminal compact row."""

    rows = [
        dict(row)
        for row in aggregate.get("results", [])
        if isinstance(row, Mapping)
    ]
    if not rows:
        return dict(aggregate)

    entries = [
        entry
        for entry in transcript.get("entries", [])
        if isinstance(entry, Mapping)
    ]
    terminal_row = dict(rows[-1])
    terminal_compact = terminal_row.get("compact_tool_result")
    compact = dict(terminal_compact) if isinstance(terminal_compact, Mapping) else {}
    compact["stdout"] = "".join(str(entry.get("stdout") or "") for entry in entries)
    compact["stderr"] = "".join(str(entry.get("stderr") or "") for entry in entries)
    compact["truncated"] = bool(
        transcript.get("compacted")
        or any(bool(entry.get("truncated")) for entry in entries)
    )
    terminal_row["compact_tool_result"] = compact
    rows[-1] = terminal_row

    materialized = dict(aggregate)
    materialized["results"] = rows
    return materialized


def _tool_parameters_by_call_id(
    metadata: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    plan = metadata.get("planner_plan")
    batch = plan.get("tool_batch") if isinstance(plan, Mapping) else None
    calls = batch.get("tool_calls") if isinstance(batch, Mapping) else None
    result: dict[str, Mapping[str, Any]] = {}
    if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes, bytearray)):
        return result
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        call_id = str(call.get("tool_call_id") or "").strip()
        params = call.get("parameters")
        if call_id and isinstance(params, Mapping):
            result[call_id] = params
    return result


def _infer_boundary(compact: Mapping[str, Any]) -> str:
    boundary = str(compact.get("interaction_boundary") or "").strip().lower()
    if boundary:
        return boundary
    process_status = str(compact.get("process_status") or "").strip().lower()
    if process_status and process_status != ShellProcessStatus.RUNNING.value:
        return "terminal"
    if compact.get("stdout") or compact.get("stderr"):
        return "output_available"
    return "quiet_boundary"


def _compact_shell_interaction_transcript(
    transcript: MutableMapping[str, Any],
) -> None:
    entries = list(transcript.get("entries") or [])
    while len(entries) > _TRANSCRIPT_MAX_ENTRIES:
        entries.pop(0)
        transcript["omitted_entries"] = int(transcript.get("omitted_entries") or 0) + 1
        transcript["compacted"] = True
    while (
        _transcript_char_count(transcript, entries) > _TRANSCRIPT_MAX_CHARS
        and len(entries) > 2
    ):
        entries.pop(0)
        transcript["omitted_entries"] = int(transcript.get("omitted_entries") or 0) + 1
        transcript["compacted"] = True
    transcript["entries"] = entries


def _transcript_char_count(
    transcript: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
) -> int:
    total = len(str(transcript.get("originating_command") or ""))
    for entry in entries:
        total += len(str(entry.get("input") or ""))
        total += len(str(entry.get("stdout") or ""))
        total += len(str(entry.get("stderr") or ""))
    return total


async def _call_interaction_decision_boundary(
    *,
    interactive: InteractiveState,
    metadata: Mapping[str, Any],
    session: Mapping[str, Any],
    active: Mapping[str, Any],
    context: Any,
    config: Mapping[str, Any] | None,
    decide_fn: Callable[..., Any] | None,
) -> Mapping[str, Any]:
    if decide_fn is not None:
        result = decide_fn(
            interactive=interactive,
            metadata=metadata,
            session=session,
            active=active,
            transcript=read_shell_interaction_transcript(str(session["sequence_id"])),
            context=context,
        )
        if hasattr(result, "__await__"):
            result = await result
        return result if isinstance(result, Mapping) else {}
    transcript = read_shell_interaction_transcript(str(session["sequence_id"]))
    if transcript is None:
        return {"action": ShellInteractionAction.WAIT_FOR_OUTPUT.value}
    try:
        llm_client = resolve_llm_client(
            dict(metadata),
            context,
            config=config,
            role=ROLE_REASONING_MAIN,
        )
        response = await llm_client.chat_with_usage(
            _shell_interaction_decision_system_prompt(),
            _shell_interaction_decision_user_prompt(
                interactive=interactive,
                session=session,
                active=active,
                transcript=transcript,
            ),
            structured_output=_INTERACTION_DECISION_STRUCTURED_OUTPUT,
            temperature=0.1,
            max_tokens=500,
        )
    except Exception as exc:
        logger.warning(
            "Shell interaction decision model unavailable; interrupting live session: %s",
            exc,
        )
        return {
            "action": ShellInteractionAction.INTERRUPT.value,
            "coordination_failed": True,
        }
    payload = getattr(response, "structured_output", None)
    return payload if isinstance(payload, Mapping) else {}


def _shell_interaction_decision_system_prompt() -> str:
    """Return the narrow semantic-action prompt for live shell coordination."""

    return (
        "You choose one semantic action for an already-running shell session.\n"
        "Valid actions are send_input, wait_for_output, and interrupt.\n"
        "Use send_input only when explicit non-empty characters should be sent "
        "to the existing session. Use wait_for_output when the program may "
        "continue producing autonomous output. Use interrupt only when controlled "
        "termination is the right next action. Never use empty input for polling."
    )


def _shell_interaction_decision_user_prompt(
    *,
    interactive: InteractiveState,
    session: Mapping[str, Any],
    active: Mapping[str, Any],
    transcript: Mapping[str, Any],
) -> str:
    """Project bounded transcript context for the semantic-action model call."""

    payload = {
        "user_goal": interactive.facts.current_goal or interactive.facts.message,
        "message": interactive.facts.message,
        "originating_tool_id": session.get("originating_tool_id"),
        "session_id": active.get("session_id"),
        "process_status": active.get("process_status"),
        "stdin_available": bool(active.get("stdin_available")),
        "valid_actions": [
            ShellInteractionAction.SEND_INPUT.value,
            ShellInteractionAction.WAIT_FOR_OUTPUT.value,
            ShellInteractionAction.INTERRUPT.value,
        ],
        "transcript": transcript,
        "output_contract": {
            "send_input": "Set action=send_input and chars to exact non-empty input.",
            "wait_for_output": "Set action=wait_for_output and chars=null.",
            "interrupt": "Set action=interrupt and chars=null.",
        },
    }
    return json.dumps(payload, sort_keys=True)


def _normalize_interaction_decision(raw: Mapping[str, Any]) -> dict[str, Any]:
    action = str(raw.get("action") or "").strip().lower()
    if action not in {item.value for item in ShellInteractionAction}:
        action = ShellInteractionAction.WAIT_FOR_OUTPUT.value
    chars = str(raw.get("chars") or raw.get("input") or "")
    if action == ShellInteractionAction.SEND_INPUT.value and not chars:
        return {"action": ShellInteractionAction.WAIT_FOR_OUTPUT.value}
    return {
        "action": action,
        "chars": chars,
        "coordination_failed": bool(raw.get("coordination_failed")),
    }


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


def _remember_shell_input(
    *,
    sequence_id: str,
    call_id: str,
    chars: str,
) -> None:
    """Keep exact stdin only in the process-local transcript cache."""

    transcript = _RUNTIME_EXECUTION_SESSIONS.get(str(sequence_id or "").strip())
    if transcript is None or not call_id or not chars:
        return
    pending_inputs = transcript.get("pending_inputs")
    if not isinstance(pending_inputs, MutableMapping):
        pending_inputs = {}
        transcript["pending_inputs"] = pending_inputs
    pending_inputs[call_id] = chars


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
    suffix = "input" if input_chars is not None else "wait"
    batch_id = f"{sequence_id}:{suffix}:{uuid4().hex[:12]}"
    call_id = f"{batch_id}:runtime-{suffix}"
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
        "stderr": update.stderr,
        "exit_code": update.exit_code,
        "stdin_available": update.stdin_available,
        "truncated": update.truncated,
        "error_code": error_code,
        "metadata": {
            "runtime_session": {
                "originating_tool_id": originating_tool_id,
                "originating_capability": (
                    "assessment" if "assessment" in originating_tool_id else "utility"
                ),
            }
        },
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
