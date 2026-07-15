"""Event-dict constructors for the LangGraph stream contract.

This module is **complementary to** ``agent.graph.emission.unified_emitter``,
not a duplicate. ``UnifiedEventEmitter`` writes events directly to the
graph's stream writer the moment they are produced — appropriate for
graph nodes. ``streaming.py`` returns event dicts that the caller then
mutates / annotates / fans out before publishing — appropriate for the
adapter and outcome layers (``streaming_adapter.py``,
``stream_events/outcome_builder.py``, etc.) that need to decorate the
event before it reaches the wire.

Public surface (kept after Phase 3.1 cleanup):

- :func:`build_delta_event`        — used by ``streaming_adapter``,
  ``message_reasoning_processor``, and ``outcome_builder`` for delta /
  intent-summary / pause-request events.
- :func:`build_final_event`        — used by ``outcome_builder`` for the
  terminal ``assistant_final`` event.
- :func:`build_agent_turn_metadata` — used by ``simple_tool_handler`` and
  ``outcome_builder`` to extract turn-level metadata from state.
- :func:`build_tool_event_sequence` — used by ``streaming_adapter`` (and
  one extended adapter test) to produce a synchronous start → delta → end
  triple for a tool that has already completed.
- :func:`_get_tool_parameters_for_display` and
  :func:`_build_command_for_display` — display-only helpers consumed by
  ``tool_execution`` via dependency injection (`deps`).

Removed in Phase 3.1 (re-audit cleanup):

- ``build_tool_event``, ``build_tool_start_event``, ``build_tool_end_event``
  were inlined into ``build_tool_event_sequence`` (their only caller)
  to remove indirection that nothing else used.
- ``relay_graph_stream``, ``publish_final_state`` and the ``GraphEvent``
  / ``Publisher`` type aliases had zero callers in the repo.

When in doubt: graph nodes should call ``UnifiedEventEmitter`` directly;
adapter / outcome / handler layers that need to mutate before emit should
call the helpers above.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from uuid import uuid4

from .state import InteractiveState
from agent.graph.contracts.streaming_constants import (
    STEP_TOOL_DELTA,
    STEP_TOOL_END,
    STEP_TOOL_START,
    TOOL_PHASE_INDEX,
)


def _default_turn_id(turn_id: Optional[str]) -> str:
    return turn_id or f"lg-{uuid4()}"


def build_delta_event(
    text: str,
    conversation_id: Optional[str],
    *,
    turn_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct an assistant delta event matching the streaming contract."""

    turn_identifier = _default_turn_id(turn_id)
    return {
        "type": "assistant_delta",
        "content": text,
        "metadata": {
            "conversation_id": conversation_id,
            "conversationId": conversation_id,
            "streaming": True,
            "id": turn_identifier,
        },
    }


def build_final_event(
    state: InteractiveState,
    *,
    turn_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create the final assistant event emitted after graph completion."""

    turn_identifier = _default_turn_id(turn_id)
    content = state.trace.final_text or ""
    return {
        "type": "assistant_final",
        "content": content,
        "metadata": {
            "conversation_id": state.facts.conversation_id,
            "conversationId": state.facts.conversation_id,
            "streaming": False,
            "id": turn_identifier,
        },
    }


def build_agent_turn_metadata(state: InteractiveState) -> Dict[str, Any]:
    """Derive normalized metadata describing the current assistant turn."""

    facts_metadata = state.facts.safe_metadata
    turn_metadata: Dict[str, Any] = {}

    for key in ("planner_reasoning", "tool_catalog"):
        value = facts_metadata.get(key)
        if value:
            turn_metadata[key] = value

    return turn_metadata


def _get_tool_parameters_for_display(state: InteractiveState, tool_id: str) -> Dict[str, Any]:
    """Return best-effort parameter dict for the given tool id."""

    # Prefer tool_parameters recorded on facts
    params = state.facts.tool_parameters.get(tool_id)
    if isinstance(params, dict) and params:
        return dict(params)

    # Fallback to last entry in tool_execution_history
    metadata = state.facts.safe_metadata
    history = metadata.get("tool_execution_history") or []
    if history:
        last = history[-1]
        try:
            if isinstance(last, dict):
                candidate = last.get("parameters") or {}
            else:
                candidate = getattr(last, "parameters", {}) or {}
            if isinstance(candidate, dict) and candidate:
                return dict(candidate)
        except Exception:
            return {}

    return {}


def _build_command_for_display(tool_id: str, params: Dict[str, Any]) -> Optional[str]:
    """Construct a display string representing the tool invocation.

    For shell-style tools this will usually be the actual command; for others
    it falls back to `ToolName key=value ...`.
    """

    if not params:
        return None

    # First, look for an explicit command-style parameter
    for key in ("command", "cmd", "binary"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    # Otherwise, build a generic invocation string
    short_tool = tool_id.split(".")[-1] if tool_id else "tool"
    parts = [short_tool]
    for key, value in sorted(params.items(), key=lambda item: str(item[0])):
        if value is None:
            continue
        parts.append(f"{key}={value}")

    return " ".join(parts) if len(parts) > 1 else None


def build_tool_event_sequence(
    tool_id: str,
    summary: Dict[str, Any],
    conversation_id: Optional[str],
    *,
    turn_id: Optional[str] = None,
    synthesized_output: Optional[Dict[str, Any]] = None,
) -> list[Dict[str, Any]]:
    """Create a start → delta → end event sequence for a tool run.

    Used by the streaming adapter to synthesize a complete tool lifecycle
    for a tool whose execution has already finished (i.e. when the adapter
    needs to emit the triple synchronously rather than streaming each
    event as it happens). The three sub-builders that previously lived
    next to this function were inlined in Phase 3.1 because nothing else
    called them.

    Args:
        tool_id: Tool identifier.
        summary: Tool execution summary (last_tool_result).
        conversation_id: Conversation identifier.
        turn_id: Turn identifier (used for stream ``id`` field).
        synthesized_output: Optional synthesized/formatted output from
            ``tool_synthesizer``. If provided, uses ``summary`` from the
            synthesized output instead of the raw observation text.

    Returns:
        List of three tool events (start, delta, end) in order.
    """

    status = summary.get("status", "success")

    # Prefer synthesized output so users see formatted, LLM-processed results.
    if synthesized_output and synthesized_output.get("summary"):
        observation = synthesized_output.get("summary", "")
    else:
        # Fallback to compact/normalized observation text.
        observation = summary.get("observation") or summary.get("summary") or ""

    turn_identifier = _default_turn_id(turn_id)
    base_metadata: Dict[str, Any] = {
        "conversation_id": conversation_id,
        "conversationId": conversation_id,
        "tool": tool_id,
        "id": turn_identifier,
        "ind": TOOL_PHASE_INDEX,
    }

    start_event = {
        "type": "tool_start",
        "content": "",
        "metadata": {
            **base_metadata,
            "streaming": True,
            "status": "in_progress",
            "step_type": STEP_TOOL_START,
        },
    }
    delta_event = {
        "type": "tool_delta",
        "content": observation or f"{tool_id} completed.",
        "metadata": {
            **base_metadata,
            "streaming": True,
            "status": status,
            "step_type": STEP_TOOL_DELTA,
        },
    }
    end_event = {
        "type": "tool_end",
        "content": "",
        "metadata": {
            **base_metadata,
            "streaming": False,
            "status": status,
            "step_type": STEP_TOOL_END,
        },
    }
    return [start_event, delta_event, end_event]
