#!/usr/bin/env python3
"""Validate event metadata completeness and success metrics for.

Programmatically validates the four success metrics from the unified emitter migration:
- Metadata completeness: 100% of events have ind, step_type, conversation_id, turn_id, streaming
- Frontend fallback usage: 0% of events use ind=-1
- Observation blending: 0% mixing of observation (ind=3) and message (ind=2) in same (turn_id, ind) group
- Event ordering violations: 0 out-of-order events within a turn

Runs the same graph flows as backend/tests/emission/test_no_legacy_helpers.py metadata tests,
collects events, and computes metrics. Exit code 0 if all metrics pass, 1 otherwise."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

# Set mock DATABASE_URL before backend/agent imports
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

# Suppress Docker SDK unavailable warning (validation does not require Docker)
logging.getLogger("backend.services.unified_docker_service").setLevel(logging.ERROR)

# Add project root to path
_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


def _make_mock_llm_client(chunks: List[str]):
    """Return a mock LLMClient that yields chunks; usage-aware path is awaitable and has async content_iterator."""
    async def _stream():
        for c in chunks:
            yield c

    class _StreamWithUsage:
        def __init__(self):
            self.content_iterator = _stream()

        def get_final_usage(self):
            return None

    class _Client:
        def stream_chat_messages(self, *args: Any, **kwargs: Any):
            return _stream()

        async def stream_chat_messages_with_usage(self, *args: Any, **kwargs: Any):
            s = _StreamWithUsage()
            s.content_iterator = _stream()
            return s

    return _Client()


def _make_capturing_writer(events: List[Dict[str, Any]]):
    """Return a writer function that appends events to the list."""
    def writer(event: Dict[str, Any]) -> None:
        events.append(dict(event))
    return writer


REQUIRED_METADATA_KEYS = {"ind", "step_type", "conversation_id", "turn_id", "streaming"}


def calculate_metadata_completeness(events: List[Dict[str, Any]]) -> float:
    """Calculate percentage of events with complete metadata."""
    if not events:
        return 100.0
    complete = sum(1 for e in events if REQUIRED_METADATA_KEYS.issubset(e.keys()))
    return (complete / len(events)) * 100.0


def calculate_fallback_usage(events: List[Dict[str, Any]]) -> float:
    """Calculate percentage of events with ind=-1 or missing ind."""
    if not events:
        return 0.0
    fallback = sum(1 for e in events if e.get("ind") == -1 or "ind" not in e)
    return (fallback / len(events)) * 100.0


def _group_events_by_turn_and_ind(events: List[Dict[str, Any]]) -> Dict[tuple, List[Dict[str, Any]]]:
    """Group events by (turn_id, ind). Phase indices: answer=2, observation=3."""
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for e in events:
        turn_id = e.get("turn_id", "")
        ind = e.get("ind", 0)
        key = (turn_id, ind)
        if key not in groups:
            groups[key] = []
        groups[key].append(e)
    return groups


def calculate_blending_rate(events: List[Dict[str, Any]]) -> float:
    """Percentage of (turn_id, ind) groups that contain both observation-type and message-type events.

    Per ticket: blending = groups where both observation (ind=3) and message (ind=2) step types
    occur. Observation types: observation_start, observation_delta, observation_section_end.
    Message types: message_start, message_delta, section_end.
    """
    if not events:
        return 0.0
    observation_types = {"observation_start", "observation_delta", "observation_section_end"}
    message_types = {"message_start", "message_delta", "section_end"}
    groups = _group_events_by_turn_and_ind(events)
    if not groups:
        return 0.0
    blended_count = 0
    for group_events in groups.values():
        step_types = {e.get("step_type") or e.get("type", "") for e in group_events}
        has_observation = bool(step_types & observation_types)
        has_message = bool(step_types & message_types)
        if has_observation and has_message:
            blended_count += 1
    return (blended_count / len(groups)) * 100.0


def calculate_ordering_violations(events: List[Dict[str, Any]]) -> int:
    """Count ordering violations: message_delta before message_start, section_end before message_start."""
    types_list = [e.get("type") or e.get("step_type") for e in events]
    violations = 0
    if "message_delta" in types_list and "message_start" in types_list:
        if types_list.index("message_delta") < types_list.index("message_start"):
            violations += 1
    if "section_end" in types_list and "message_start" in types_list:
        if types_list.index("section_end") < types_list.index("message_start"):
            violations += 1
    return violations


def _collect_events_from_flows() -> List[Dict[str, Any]]:
    """Run simple_chat, finalize_tool_results, finalize_deep_reasoning with capturing writers; return all events."""
    from agent.graph.state import FactsState, InteractiveState, InteractiveInput, TraceState

    events1: List[Dict[str, Any]] = []
    events2: List[Dict[str, Any]] = []
    events3: List[Dict[str, Any]] = []

    def _make_simple_chat_state(task_id: int, conversation_id: str, message: str):
        from agent.graph.context.builder import (
            METADATA_CONTEXT_BUNDLE_KEY,
            build_conversation_context_bundle,
        )

        bundle = build_conversation_context_bundle(
            conversation_id=conversation_id,
            turn_id=f"{conversation_id}-turn-0",
            turn_sequence=0,
            messages=[],
        )
        payload = InteractiveInput(
            task_id=task_id,
            message=message,
            conversation_id=conversation_id,
            metadata={
                "simple_chat_runtime": {"model": "stub"},
                METADATA_CONTEXT_BUNDLE_KEY: bundle,
            },
        )
        return payload.to_state().as_graph_state()

    def _make_simple_tool_state(task_id: int, conversation_id: str, synthesized_output: Dict[str, Any]):
        facts = FactsState(
            task_id=task_id,
            message="Run nmap",
            conversation_id=conversation_id,
            metadata={"synthesized_output": synthesized_output, "last_tool_result": {}},
        )
        return InteractiveState(facts=facts, trace=TraceState()).as_graph_state()

    def _make_dr_state(task_id: int, conversation_id: str, dr_iteration: Dict[str, Any] | None = None):
        metadata = dr_iteration or {}
        facts = FactsState(
            task_id=task_id,
            message="Deep reasoning task",
            conversation_id=conversation_id,
            capability="deep_reasoning",
            metadata=metadata,
        )
        return InteractiveState(facts=facts, trace=TraceState()).as_graph_state()

    simple_chat = __import__("agent.graph.nodes.simple_chat", fromlist=["run_simple_chat"])
    finalize = __import__("agent.graph.nodes.finalize", fromlist=["finalize_results"])

    with (
        patch.object(simple_chat, "get_stream_writer", lambda: _make_capturing_writer(events1)),
        patch.object(simple_chat, "resolve_llm_client", lambda *a, **k: _make_mock_llm_client(["a"])),
    ):
        asyncio.run(
            simple_chat.run_simple_chat(
                _make_simple_chat_state(1, "c1", "m"),
                context=None,
                config={"configurable": {"thread_id": "t1"}},
            )
        )
    with (
        patch.object(finalize, "get_stream_writer", lambda: _make_capturing_writer(events2)),
        patch.object(finalize, "resolve_llm_client", lambda *a, **k: _make_mock_llm_client(["b"])),
    ):
        asyncio.run(
            finalize.finalize_results(
                _make_simple_tool_state(2, "c2", {"tool": "nmap", "summary": "s"}),
                context=None,
                config={"configurable": {"thread_id": "t2"}},
            )
        )
    with (
        patch.object(finalize, "get_stream_writer", lambda: _make_capturing_writer(events3)),
        patch.object(finalize, "resolve_llm_client", lambda *a, **k: _make_mock_llm_client(["c"])),
    ):
        asyncio.run(
            finalize.finalize_results(
                _make_dr_state(3, "c3", {}),
                context=None,
                config={"configurable": {"thread_id": "t3"}},
            )
        )

    return events1 + events2 + events3


def main() -> int:
    print("=== Event Metadata Validation Results ===")

    # Collect events from the same flows as test_no_legacy_helpers
    try:
        all_events = _collect_events_from_flows()
    except Exception as e:
        print(f"ERROR: Failed to collect events: {e}")
        return 1

    if not all_events:
        print("ERROR: No events collected from graph flows.")
        return 1

    completeness = calculate_metadata_completeness(all_events)
    fallback = calculate_fallback_usage(all_events)
    blending = calculate_blending_rate(all_events)
    ordering_violations = calculate_ordering_violations(all_events)

    print(f"Metadata completeness: {completeness:.1f}%")
    print(f"Frontend fallback usage (ind=-1): {fallback:.1f}%")
    print(f"Observation blending rate: {blending:.1f}%")
    print(f"Event ordering violations: {ordering_violations}")
    print()

    passed = (
        completeness == 100.0
        and fallback == 0.0
        and blending == 0.0
        and ordering_violations == 0
    )

    if passed:
        print("All success metrics passed!")
        return 0
    else:
        print("One or more success metrics failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
