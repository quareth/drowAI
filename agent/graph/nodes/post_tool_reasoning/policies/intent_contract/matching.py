"""Evaluate explicit request constraints against the current-turn execution chain.

The direct-executor graph is progressive: a later helper step may inspect or
extract evidence from a prior target-facing execution that already satisfied the
user's explicit tool/target/port constraints. This module therefore evaluates
the current selected step first and then falls back to prior same-turn executed
steps recorded in ``metadata["action_history"]`` before declaring an intent
contract mismatch.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, TypedDict

from agent.tool_runtime.batch.plan_view import serialized_tool_calls_from_metadata

from .....state import InteractiveState
from .extraction import (
    _extract_executed_ports,
    _extract_executed_targets,
    _extract_expected_ports,
    _extract_expected_targets,
    _extract_expected_tools,
    _normalize_target_token,
    _normalize_tool_alias,
    _parse_port_range,
)


class _ExecutionCandidate(TypedDict):
    """Structured execution candidate considered for intent-contract matching."""

    tool_id: str
    params: Mapping[str, Any]
    matched_via: str


def _ports_match(expected_ports: List[str], executed_ports: List[str]) -> Optional[bool]:
    if not expected_ports:
        return None
    if not executed_ports:
        return False

    executed_ranges = [
        parsed
        for parsed in (_parse_port_range(token) for token in executed_ports)
        if parsed is not None
    ]
    if not executed_ranges:
        return False

    for requested in expected_ports:
        required_range = _parse_port_range(requested)
        if required_range is None:
            continue

        request_start, request_end = required_range
        if not any(
            exec_start <= request_start and exec_end >= request_end
            for exec_start, exec_end in executed_ranges
        ):
            return False
    return True


def _iter_execution_candidates(
    interactive: InteractiveState,
) -> Iterable[_ExecutionCandidate]:
    """Yield current-step and prior same-turn execution candidates.

    Current ToolBatch calls are yielded first so exact current-step matches win
    before the matcher consults prior action history. Prior candidates are read
    from the existing ``metadata["action_history"]`` ledger and filtered to the
    active ``turn_sequence`` when available.
    """
    facts = interactive.facts
    metadata = facts.safe_metadata

    for call in serialized_tool_calls_from_metadata(metadata):
        yield {
            "tool_id": call.tool_id,
            "params": call.parameters,
            "matched_via": "current_step",
        }

    action_history = metadata.get("action_history")
    if not isinstance(action_history, list):
        return

    active_turn_sequence = metadata.get("turn_sequence")
    active_turn_is_int = isinstance(active_turn_sequence, int)

    for item in reversed(action_history):
        if not isinstance(item, Mapping):
            continue
        if active_turn_is_int and item.get("turn_sequence") != active_turn_sequence:
            continue

        tool_id = str(item.get("tool_id") or "").strip()
        params = item.get("params")
        if not tool_id or not isinstance(params, Mapping):
            continue
        yield {
            "tool_id": tool_id,
            "params": params,
            "matched_via": "prior_step",
        }


def _evaluate_candidate(
    *,
    candidate: _ExecutionCandidate,
    expected_tools: List[str],
    expected_targets: List[str],
    expected_ports: List[str],
) -> Dict[str, Any]:
    """Evaluate one structured execution candidate against explicit constraints."""
    executed_tool = _normalize_tool_alias(candidate["tool_id"])
    executed_targets_raw = _extract_executed_targets(candidate["params"])
    executed_targets = [_normalize_target_token(value) for value in executed_targets_raw]
    executed_ports = _extract_executed_ports(candidate["params"], executed_targets_raw)

    tool_match: Optional[bool] = None
    if expected_tools:
        tool_match = executed_tool in expected_tools

    target_match: Optional[bool] = None
    if expected_targets:
        target_match = any(target in executed_targets for target in expected_targets)

    ports_match = _ports_match(expected_ports, executed_ports)
    checks = [value for value in (tool_match, target_match, ports_match) if value is not None]
    satisfied = all(checks) if checks else True

    mismatches: List[str] = []
    if tool_match is False:
        mismatches.append("tool mismatch")
    if target_match is False:
        mismatches.append("target mismatch")
    if ports_match is False:
        mismatches.append("ports mismatch")

    return {
        "executed_tool": executed_tool,
        "executed_targets": executed_targets,
        "executed_ports": executed_ports,
        "tool_match": tool_match,
        "target_match": target_match,
        "ports_match": ports_match,
        "mismatches": mismatches,
        "satisfied": satisfied,
        "matched_via": candidate["matched_via"] if satisfied else None,
    }


def _evaluate_simple_tool_intent_contract(
    interactive: InteractiveState,
) -> Dict[str, Any]:
    """Evaluate whether execution satisfies explicit user request constraints."""
    facts = interactive.facts
    metadata = facts.safe_metadata
    current_calls = serialized_tool_calls_from_metadata(metadata)
    current_tool_id = current_calls[0].tool_id if current_calls else ""

    expected_tools = _extract_expected_tools(facts.message or "")
    expected_targets = _extract_expected_targets(interactive)
    expected_ports = _extract_expected_ports(facts.message or "")
    applicable = bool(expected_tools or expected_targets or expected_ports)
    if not applicable:
        return {
            "applicable": False,
            "satisfied": True,
            "expected_tools": expected_tools,
            "expected_targets": expected_targets,
            "expected_ports": expected_ports,
            "executed_tool": _normalize_tool_alias(current_tool_id),
            "executed_targets": [],
            "executed_ports": [],
            "tool_match": None,
            "target_match": None,
            "ports_match": None,
            "mismatches": [],
            "matched_via": None,
        }

    last_evaluation: Dict[str, Any] = {
        "executed_tool": _normalize_tool_alias(current_tool_id),
        "executed_targets": [],
        "executed_ports": [],
        "tool_match": None,
        "target_match": None,
        "ports_match": None,
        "mismatches": [],
        "satisfied": True,
        "matched_via": None,
    }

    for candidate in _iter_execution_candidates(interactive):
        last_evaluation = _evaluate_candidate(
            candidate=candidate,
            expected_tools=expected_tools,
            expected_targets=expected_targets,
            expected_ports=expected_ports,
        )
        if last_evaluation["satisfied"]:
            break

    return {
        "applicable": applicable,
        "satisfied": bool(last_evaluation["satisfied"]),
        "expected_tools": expected_tools,
        "expected_targets": expected_targets,
        "expected_ports": expected_ports,
        "executed_tool": last_evaluation["executed_tool"],
        "executed_targets": last_evaluation["executed_targets"],
        "executed_ports": last_evaluation["executed_ports"],
        "tool_match": last_evaluation["tool_match"],
        "target_match": last_evaluation["target_match"],
        "ports_match": last_evaluation["ports_match"],
        "mismatches": last_evaluation["mismatches"],
        "matched_via": last_evaluation["matched_via"],
    }
