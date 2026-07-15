"""Project cached InteractiveState executions back into a ChatStateContainer.

Used by checkpoint-retry and HITL resume flows. When LangGraph resumes from a
stable checkpoint placed past one or more nodes, those nodes do not re-run and
their tool/reasoning/observation events never fire through the streaming
adapter. Their cached executions still live on ``interactive_state.trace``,
so this module projects them back into the container so canonical event
projection isn't blank after the resync-driven re-bootstrap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional

if TYPE_CHECKING:
    from agent.graph import InteractiveState
    from backend.services.langgraph_chat.runtime.state_container import ChatStateContainer


def hydrate_container_from_checkpoint_state(
    state_container: "ChatStateContainer",
    interactive_state: "InteractiveState",
    *,
    task_id: int,
) -> None:
    """Backfill container from checkpoint state when the live adapter is sparse.

    Args:
        state_container: Turn-scoped state accumulator to hydrate.
        interactive_state: Parsed LangGraph interactive state.
        task_id: Task id used for synthetic hydrated ids.
    """
    if state_container.get_tool_calls():
        return

    trace = interactive_state.trace
    facts_metadata = getattr(interactive_state.facts, "metadata", None)
    compact_metadata = facts_metadata if isinstance(facts_metadata, Mapping) else {}
    executed_tools = list(getattr(trace, "executed_tools", None) or [])

    def _compact_tool_result_at(index: int) -> Optional[Dict[str, Any]]:
        batch = compact_metadata.get("last_tool_result_compact_batch")
        if isinstance(batch, Mapping):
            results = batch.get("results")
            if isinstance(results, list) and 0 <= index < len(results):
                row = results[index]
                if isinstance(row, Mapping):
                    compact = row.get("compact_tool_result")
                    if isinstance(compact, Mapping):
                        return dict(compact)
        legacy_compact = compact_metadata.get("last_tool_result_compact")
        if len(executed_tools) == 1 and isinstance(legacy_compact, Mapping):
            return dict(legacy_compact)
        return None

    if executed_tools:
        # Per-tool hydration. ``trace.reasoning`` is intentionally not used
        # here because the runtime appends per-tool reasoning into that flat
        # list as well; reading both would double-count.
        for idx, record in enumerate(executed_tools):
            tool_name = str(getattr(record, "tool_id", "") or "").strip() or "unknown"
            args = getattr(record, "args", None)
            tool_arguments = dict(args) if isinstance(args, dict) else {}
            observation_text = getattr(record, "observation", None)
            observation_text = (
                observation_text.strip()
                if isinstance(observation_text, str) and observation_text.strip()
                else None
            )
            reasoning_text = getattr(record, "reasoning", None)
            reasoning_text = (
                reasoning_text.strip()
                if isinstance(reasoning_text, str) and reasoning_text.strip()
                else None
            )
            status = str(getattr(record, "status", "") or "success")

            # Synthetic ids are safe because checkpoint retry persistence
            # replaces this turn's canonical detail rows before inserting
            # hydrated rows.
            synthetic_call_id = f"hydrated-{task_id}-{idx}"
            fallback_tool_result: Dict[str, Any] = {
                "status": status,
                "summary": observation_text or "",
            }
            tool_result = _compact_tool_result_at(idx) or fallback_tool_result
            tool_result.setdefault("status", status)
            tool_result.setdefault("summary", observation_text or "")

            if reasoning_text:
                state_container.start_reasoning(sub_turn_index=idx)
                state_container.append_reasoning(reasoning_text)
                state_container.end_reasoning(sub_turn_index=idx)

            state_container.add_tool_call(
                {
                    "tool_call_id": synthetic_call_id,
                    "tool_id": None,
                    "tool_name": tool_name,
                    "tool_arguments": tool_arguments,
                    "tool_result": tool_result,
                    "turn_index": idx,
                }
            )

            if observation_text:
                state_container.start_observation(sub_turn_index=idx)
                state_container.append_observation(
                    observation_text,
                    sub_turn_index=idx,
                )
                state_container.end_observation(sub_turn_index=idx)
        return

    # No completed tool executions cached (e.g. HITL interrupt that paused
    # at the first tool's approval before any tool_end fired). Project the
    # cached flat reasoning trace onto synthesized sections so the canonical
    # event projection isn't blank after the resync-driven re-bootstrap.
    # Only fires when no live reasoning was captured either, so we never
    # duplicate sections.
    if state_container.get_reasoning_sections():
        return
    trace_reasoning = list(getattr(trace, "reasoning", None) or [])
    for idx, text in enumerate(trace_reasoning):
        cleaned = text.strip() if isinstance(text, str) else ""
        if not cleaned:
            continue
        state_container.start_reasoning(sub_turn_index=idx)
        state_container.append_reasoning(cleaned)
        state_container.end_reasoning(sub_turn_index=idx)
