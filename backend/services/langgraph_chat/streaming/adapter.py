"""Public compatibility facade for LangGraph chat stream translation.

Responsibilities:
- preserve the historical ``LangGraphStreamingAdapter`` API used by handlers,
  executors, and tests
- delegate live event processing to the internal ``event_processors`` package
- expose the compatibility helpers that still belong on the adapter surface,
  such as final-event building, tool-event synthesis, and event publication

This module is intentionally small. It is the stable entrypoint for the
LangGraph-chat layer, while the detailed event-family logic lives behind the
internal ``backend.services.langgraph_chat.streaming.event_processors`` package.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Iterable, List, Optional
from uuid import uuid4

from agent.graph.state import InteractiveState
from agent.graph.streaming import build_delta_event, build_tool_event_sequence
from backend.services.metrics.utils import safe_gauge, safe_inc
from backend.services.streaming import normalize_stream_packet

from backend.services.langgraph_chat.streaming.event_processors import (
    StreamEventProcessor,
    ToolCallSnapshotService,
    TurnOutcomeEventBuilder,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from backend.services.langgraph_chat.runtime.state_container import ChatStateContainer


class LangGraphStreamingAdapter:
    """Preserve the public adapter API while delegating to focused collaborators."""

    def __init__(self) -> None:
        self._tool_call_snapshot_service = ToolCallSnapshotService()
        # The static ``_safe_inc`` indirection is intentionally retained: it
        # is the injected ``metric_inc`` callback for the stream-event and
        # turn-outcome collaborators, and it preserves the
        # ``streaming_adapter.safe_inc`` patch point that existing
        # ``backend/tests/langgraph_chat`` tests rely on for assertions.
        # Without it, patching ``streaming_adapter.safe_inc`` no longer
        # intercepts calls because the collaborators capture ``safe_inc``
        # at construction time.
        self._stream_event_processor = StreamEventProcessor(
            self._tool_call_snapshot_service,
            metric_inc=self._safe_inc,
        )
        self._turn_outcome_event_builder = TurnOutcomeEventBuilder(
            metric_inc=self._safe_inc,
        )

    @staticmethod
    def _safe_inc(name: str, value: int = 1) -> None:
        """Best-effort metric callback for stream-event collaborators.

        Single-step increments call ``safe_inc(name)`` to keep the
        existing test contract (assertions match on the one-arg form);
        multi-step increments forward the explicit ``value``.
        """

        if value == 1:
            safe_inc(name)
            return
        safe_inc(name, value)

    def process_streaming_event(
        self,
        event: dict[str, Any],
        state_container: Optional["ChatStateContainer"] = None,
    ) -> Optional[dict[str, Any]]:
        """Process streaming event from LangGraph nodes."""
        return self._stream_event_processor.process_streaming_event(
            event,
            state_container=state_container,
        )

    def build_final_event(
        self,
        state: InteractiveState,
        *,
        turn_id: Optional[str] = None,
    ) -> dict[str, object]:
        """Return a final assistant event mirroring existing SSE schema."""
        return self._turn_outcome_event_builder.build_final_event(
            state,
            turn_id=turn_id,
        )

    def build_simple_chat_events(
        self,
        state: InteractiveState,
        *,
        turn_id: Optional[str] = None,
    ) -> List[dict[str, object]]:
        """Compatibility helper returning the simple-chat final event sequence."""
        return [self.build_final_event(state, turn_id=turn_id)]

    def build_agent_pause_request_event(
        self,
        state: InteractiveState,
        *,
        turn_id: Optional[str] = None,
    ) -> Optional[dict[str, object]]:
        """Create a dedicated agent pause request event for UI rendering."""
        return self._turn_outcome_event_builder.build_agent_pause_request_event(
            state,
            turn_id=turn_id,
        )

    def build_intent_summary_event(
        self,
        state: InteractiveState,
        *,
        turn_id: Optional[str] = None,
    ) -> Optional[dict[str, object]]:
        """Create a reasoning delta summarising routing and safety context."""
        capability = state.facts.capability or "respond_only"
        hints = state.facts.intent_hints or {}
        risk_flags = state.facts.risk_flags or []
        router_data = state.facts.metadata.get("intent_router", {}) if state.facts.metadata else {}

        summary_bits = [f"Intent routing selected `{capability}`."]
        classifier = hints.get("classifier_label")
        if classifier:
            summary_bits.append(f"Classifier label: {classifier}.")
        tool_hints = hints.get("tool_hints") or []
        if tool_hints:
            summary_bits.append(f"Tool hints: {', '.join(tool_hints)}.")
        targets = hints.get("targets") or []
        if targets:
            summary_bits.append(f"Targets: {', '.join(targets)}.")
        if risk_flags:
            summary_bits.append(f"Risk flags: {', '.join(risk_flags)}.")
        if router_data.get("decisions"):
            considered = router_data["decisions"].get("considered", [])
            if considered:
                summary_bits.append(f"Considered routes: {', '.join(considered)}.")

        if not summary_bits:
            return None

        resolved_turn_id = turn_id or f"lg-{state.facts.task_id}-{uuid4()}"
        event = build_delta_event(
            " ".join(summary_bits),
            state.facts.conversation_id,
            turn_id=resolved_turn_id,
        )
        event.setdefault("metadata", {})
        event["metadata"]["subtype"] = "intent_summary"
        event["metadata"]["internal_only"] = True
        event["metadata"]["intent_summary"] = {
            "capability": capability,
            "classifier_label": classifier,
            "classifier_confidence": hints.get("classifier_confidence"),
            "tool_hints": tool_hints,
            "targets": targets,
            "risk_flags": risk_flags,
            "router": router_data,
        }
        return event

    def build_tool_events(
        self,
        state: InteractiveState,
        *,
        turn_id: Optional[str] = None,
    ) -> List[dict[str, object]]:
        """Return tool streaming events using the recorded execution summary."""
        metadata = state.facts.safe_metadata
        summary = metadata.get("last_tool_result")
        if not summary:
            return self.build_placeholder_tool_events(state, turn_id=turn_id)

        resolved_turn_id = turn_id or f"lg-{state.facts.task_id}-{uuid4()}"
        tool_id = str(
            summary.get("tool")
            or state.facts.selected_tool
            or state.facts.capability
            or "unknown"
        )

        synthesized_output = metadata.get("synthesized_output")

        sequence = build_tool_event_sequence(
            tool_id,
            summary,
            state.facts.conversation_id,
            turn_id=resolved_turn_id,
            synthesized_output=synthesized_output,
        )
        for event in sequence:
            event.setdefault("metadata", {})
            event["metadata"]["streaming"] = False
        safe_inc("langgraph_tool_runs")
        if summary.get("status") not in {"success", "ok"}:
            safe_inc("langgraph_tool_failures")
        duration = summary.get("duration")
        if duration is not None:
            try:
                safe_gauge("langgraph_tool_latency_ms", float(duration) * 1000.0)
            except Exception:  # pragma: no cover - defensive
                pass
        latest_history = None
        history = metadata.get("tool_history") or []
        if history:
            latest_history = history[-1]

        if latest_history:
            planner_reasoning = latest_history.get("reasoning") or []
            catalog_snapshot = latest_history.get("catalog") or []
            for event in sequence:
                event.setdefault("metadata", {})
                event["metadata"].setdefault("planner_reasoning", planner_reasoning)
                event["metadata"].setdefault("tool_catalog", catalog_snapshot)

        final_event = self.build_final_event(state, turn_id=resolved_turn_id)
        if latest_history:
            final_event.setdefault("metadata", {})
            final_event["metadata"].setdefault(
                "planner_reasoning",
                latest_history.get("reasoning") or [],
            )
            final_event["metadata"].setdefault(
                "tool_catalog",
                latest_history.get("catalog") or [],
            )

        return [*sequence, final_event]

    async def publish_events(
        self,
        *,
        task_id: int,
        events: Iterable[dict[str, object]],
        publisher,
    ) -> None:
        """Publish the provided events using the supplied publisher coroutine."""
        for event in events:
            normalized = normalize_stream_packet(event, task_id=task_id)
            if normalized is None:
                logger.warning("Skipping invalid stream event for task %s", task_id)
                continue
            await publisher(task_id, normalized)
            safe_inc("intent_stream_events_published")


__all__ = ["LangGraphStreamingAdapter"]
