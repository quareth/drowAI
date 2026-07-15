"""Factory for deterministic scenario graphs used by E2E mode.

This module provides a lightweight graph-like object with an ``astream`` API
that yields pre-scripted ``(mode, chunk)`` tuples. It allows deterministic
streaming tests to run without invoking real LLM or tool execution.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional, Set

from agent.graph.graph_names import (
    GRAPH_NAME_DEEP_REASONING,
    GRAPH_NAME_INTERRUPT_RESUME,
    GRAPH_NAME_SIMPLE_TOOL,
)

_SCENARIO_FILE_MAP: Dict[str, str] = {
    GRAPH_NAME_SIMPLE_TOOL: "simple_tool_script.json",
    GRAPH_NAME_DEEP_REASONING: "deep_reasoning_script.json",
    GRAPH_NAME_INTERRUPT_RESUME: "interrupt_resume_script.json",
}


class ScenarioGraph:
    """Minimal graph-compatible object that replays scripted stream events."""

    def __init__(self, scenario_name: str, checkpointer: Any) -> None:
        self._scenario_name = scenario_name
        self._checkpointer = checkpointer  # kept for API parity with real graphs

    async def astream(
        self,
        graph_input: Any,
        config: Optional[Dict[str, Any]] = None,
        stream_mode: Any = None,
    ) -> AsyncIterator[tuple[str, Any]]:
        """Yield scripted stream events in deterministic order.

        Args:
            graph_input: Unused input payload, kept for LangGraph API parity.
            config: Unused config payload, kept for LangGraph API parity.
            stream_mode: Requested stream mode(s). If provided, only matching
                scripted events are yielded.
        """
        _ = graph_input
        _ = config
        script = _load_scenario_script(self._scenario_name)
        events = _resolve_script_events(
            scenario_name=self._scenario_name,
            script=script,
            graph_input=graph_input,
        )
        events = _inject_runtime_metadata(
            events=events,
            graph_input=graph_input,
            config=config,
        )
        requested_modes = _normalize_stream_modes(stream_mode)

        for item in events:
            delay_ms = item.get("delay_ms") if isinstance(item, dict) else None
            if isinstance(delay_ms, (int, float)) and delay_ms > 0:
                await asyncio.sleep(float(delay_ms) / 1000.0)
            mode = item["mode"]
            chunk = item["chunk"]
            if requested_modes is not None and mode not in requested_modes:
                continue
            yield mode, chunk


def get_scenario_graph(scenario_name: str, checkpointer: Any) -> Any:
    """Return a deterministic scenario graph for the requested scenario name."""
    if scenario_name not in _SCENARIO_FILE_MAP:
        supported = ", ".join(sorted(_SCENARIO_FILE_MAP))
        raise ValueError(f"Unsupported scenario '{scenario_name}'. Supported: {supported}")
    return ScenarioGraph(scenario_name=scenario_name, checkpointer=checkpointer)


def _load_scenario_script(scenario_name: str) -> Dict[str, Any]:
    script_path = _resolve_scenario_script_path(scenario_name)
    with script_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if not isinstance(payload, dict):
        raise ValueError(f"Scenario script must be an object: {script_path}")

    _validate_script_payload(payload, script_path)

    return payload


def _resolve_scenario_script_path(scenario_name: str) -> Path:
    script_file = _SCENARIO_FILE_MAP[scenario_name]
    module_root = Path(__file__).resolve().parent
    repo_root = _resolve_repo_root(module_root)

    candidates = [module_root / "scenarios" / script_file]
    if repo_root is not None:
        candidates.append(repo_root / "e2e" / "scenarios" / script_file)

    for candidate in candidates:
        if candidate.exists():
            return candidate

    candidate_display = ", ".join(str(path) for path in candidates)
    raise FileNotFoundError(
        f"Scenario script not found for '{scenario_name}'. Checked: {candidate_display}"
    )


def _resolve_repo_root(module_root: Path) -> Optional[Path]:
    for candidate in (module_root, *module_root.parents):
        if (
            (candidate / "backend").is_dir()
            and (candidate / "e2e" / "scenarios").is_dir()
        ):
            return candidate
    for candidate in (module_root, *module_root.parents):
        if (candidate / "backend").is_dir() and (candidate / "e2e").is_dir():
            return candidate
    return None


def _normalize_stream_modes(stream_mode: Any) -> Optional[Set[str]]:
    if stream_mode is None:
        return None
    if isinstance(stream_mode, str):
        return {stream_mode}
    if isinstance(stream_mode, (list, tuple, set)):
        return {str(mode) for mode in stream_mode}
    raise TypeError("stream_mode must be None, a string, or a list/tuple/set of strings")


def _resolve_script_events(
    *,
    scenario_name: str,
    script: Dict[str, Any],
    graph_input: Any,
) -> list[Dict[str, Any]]:
    if (
        scenario_name == GRAPH_NAME_DEEP_REASONING
        and "deterministic-cancellable-chat" in _graph_input_message(graph_input)
    ):
        cancellable_events = script.get("cancellable_events")
        if isinstance(cancellable_events, list):
            return cancellable_events

    events = script.get("events")
    if isinstance(events, list):
        return events

    if scenario_name == GRAPH_NAME_INTERRUPT_RESUME:
        if _is_resume_input(graph_input):
            action = _resume_action(graph_input)
            resume_key = (
                "reject_events"
                if action == "reject"
                else "clarify_resume_events"
                if action == "answer"
                else "resume_events"
            )
            resume_events = script.get(resume_key)
            if isinstance(resume_events, list):
                return resume_events
        else:
            message = _graph_input_message(graph_input)
            initial_key = (
                "clarify_events"
                if "deterministic-interrupt-clarify" in message
                else "plan_review_events"
                if "deterministic-interrupt-plan-review" in message
                else "initial_events"
            )
            initial_events = script.get(initial_key)
            if isinstance(initial_events, list):
                return initial_events

    raise ValueError(
        f"Scenario '{scenario_name}' does not define a valid event script for this phase"
    )


def _inject_runtime_metadata(
    *,
    events: list[Dict[str, Any]],
    graph_input: Any,
    config: Optional[Dict[str, Any]],
) -> list[Dict[str, Any]]:
    """Stamp scripted events with runtime turn/conversation/task identifiers."""
    if not events:
        return events

    facts = graph_input.get("facts", {}) if isinstance(graph_input, dict) else {}
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}

    conversation_id = configurable.get("canonical_conversation_id") or facts.get("conversation_id")
    turn_id = configurable.get("canonical_turn_id")
    turn_sequence = configurable.get("canonical_turn_sequence")
    task_id = facts.get("task_id")
    facts_metadata = facts.get("metadata") if isinstance(facts, dict) else None
    reserved_message_id = (
        facts_metadata.get("reserved_message_id")
        if isinstance(facts_metadata, dict)
        else None
    )
    runtime_projection = configurable.get("runtime_projection")
    if not isinstance(task_id, int) and isinstance(runtime_projection, dict):
        task_id = runtime_projection.get("task_id")

    patched_events: list[Dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            patched_events.append(event)
            continue
        mode = event.get("mode")
        chunk = event.get("chunk")
        if mode == "values" and isinstance(chunk, dict):
            patched_event = copy.deepcopy(event)
            patched_chunk = patched_event.get("chunk", {})
            patched_facts = patched_chunk.get("facts")
            if not isinstance(patched_facts, dict):
                patched_facts = {}
                patched_chunk["facts"] = patched_facts

            if isinstance(task_id, int):
                patched_facts["task_id"] = task_id
            if conversation_id:
                patched_facts["conversation_id"] = conversation_id
            if not str(patched_facts.get("message") or "").strip():
                trace = patched_chunk.get("trace")
                final_text = trace.get("final_text") if isinstance(trace, dict) else None
                patched_facts["message"] = str(
                    final_text or "Deterministic scenario completed."
                )

            interrupts = patched_chunk.get("__interrupt__")
            if isinstance(interrupts, list):
                for interrupt in interrupts:
                    if not isinstance(interrupt, dict):
                        continue
                    if turn_id:
                        interrupt.setdefault("turn_id", turn_id)
                    if isinstance(turn_sequence, int):
                        interrupt.setdefault("turn_sequence", turn_sequence)
                    if isinstance(reserved_message_id, int):
                        interrupt.setdefault("reserved_message_id", reserved_message_id)
                    if conversation_id:
                        interrupt.setdefault("conversation_id", conversation_id)

            patched_events.append(patched_event)
            continue
        if mode != "custom" or not isinstance(chunk, dict):
            patched_events.append(event)
            continue

        patched_event = copy.deepcopy(event)
        patched_chunk = patched_event.get("chunk", {})
        patched_metadata = patched_chunk.get("metadata")
        if not isinstance(patched_metadata, dict):
            patched_metadata = {}
            patched_chunk["metadata"] = patched_metadata

        if conversation_id:
            patched_chunk["conversation_id"] = conversation_id
            patched_metadata["conversation_id"] = conversation_id
            patched_metadata["conversationId"] = conversation_id
        if turn_id:
            patched_chunk["turn_id"] = turn_id
            patched_metadata["id"] = turn_id
        if isinstance(turn_sequence, int):
            patched_chunk["turn_sequence"] = turn_sequence
            patched_metadata["turn_sequence"] = turn_sequence
        if isinstance(task_id, int):
            patched_metadata["task_id"] = task_id

        patched_events.append(patched_event)

    return patched_events


def _is_resume_input(graph_input: Any) -> bool:
    # LangGraph resume path passes Command(resume=...), while initial turns pass mappings.
    return hasattr(graph_input, "resume")


def _graph_input_message(graph_input: Any) -> str:
    """Read the normalized deterministic scenario marker from graph facts."""
    if not isinstance(graph_input, dict):
        return ""
    facts = graph_input.get("facts")
    if not isinstance(facts, dict):
        return ""
    return str(facts.get("message") or "").strip().lower()


def _resume_action(graph_input: Any) -> str:
    """Read only the whitelisted HITL action from a LangGraph resume command."""
    resume = getattr(graph_input, "resume", None)
    if not isinstance(resume, dict):
        return ""
    response = resume.get("response") if isinstance(resume.get("response"), dict) else resume
    return str(response.get("action") or "").strip().lower()


def _validate_script_payload(payload: Dict[str, Any], script_path: Path) -> None:
    if isinstance(payload.get("events"), list):
        _validate_event_list(payload["events"], script_path, "events")
        if isinstance(payload.get("cancellable_events"), list):
            _validate_event_list(
                payload["cancellable_events"],
                script_path,
                "cancellable_events",
            )
        return

    if isinstance(payload.get("initial_events"), list) and isinstance(
        payload.get("resume_events"), list
    ):
        _validate_event_list(payload["initial_events"], script_path, "initial_events")
        _validate_event_list(payload["resume_events"], script_path, "resume_events")
        return

    raise ValueError(
        "Scenario script must include either 'events' or both "
        f"'initial_events' and 'resume_events': {script_path}"
    )


def _validate_event_list(
    events: list[Any],
    script_path: Path,
    key: str,
) -> None:
    for index, item in enumerate(events):
        if not isinstance(item, dict) or "mode" not in item or "chunk" not in item:
            raise ValueError(
                f"Invalid scenario event at index {index} in {script_path} ({key}); "
                "expected object with mode and chunk"
            )


__all__ = ["get_scenario_graph", "ScenarioGraph"]
