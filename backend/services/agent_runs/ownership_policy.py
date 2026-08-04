"""Deterministic subagent ownership policy for classifier handoff routing.

The policy resolves explicit classifier handoffs through the canonical
subagent registry. It does not authorize tools, open workspaces, access
databases, or mutate the process-local run registry.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agent.subagents.definition import (
    SubagentDefinition,
    resolve_definition_capability,
)
from agent.subagents.handoff import normalize_agent_handoff_entries
from agent.subagents.registry import SubagentRegistry, get_subagent_registry

from .contracts import AgentCapability


MAX_ASSIGNMENT_TARGETS = 16
MAX_TARGET_LENGTH = 512
MAX_AGENT_HANDOFFS = 3
SUBAGENT_DISPATCH_BRANCH = "subagent"

_NON_CAPABILITY_ROUTE_HINTS = frozenset(
    {
        "direct_executor",
        "simple_tool",
        "simple_tool_execution",
        "tool",
        "tool_call",
        "tool_execution",
    }
)


@dataclass(frozen=True, slots=True)
class SubagentPlanHandoff:
    """One validated entry in the ordered subagent dispatch plan."""

    agent_id: str
    agent_kind: str
    dispatch_branch: str
    capabilities: tuple[AgentCapability, ...]
    targets: tuple[str, ...]
    objective: str


@dataclass(frozen=True, slots=True)
class SubagentRoutingDecision:
    """Result of deterministic registry-backed handoff evaluation."""

    should_delegate: bool
    reason: str
    agent_id: str | None = None
    agent_kind: str | None = None
    dispatch_branch: str | None = None
    capabilities: tuple[AgentCapability, ...] = ()
    targets: tuple[str, ...] = ()
    objective: str | None = None
    handoffs: tuple[SubagentPlanHandoff, ...] = ()


def resolve_subagent_handoff(
    metadata: Mapping[str, Any],
    *,
    registry: SubagentRegistry | None = None,
    active_runs_by_agent_id: Mapping[str, int] | None = None,
    handoff_entries: Any = None,
    require_direct_executor: bool = True,
) -> SubagentRoutingDecision:
    """Resolve explicit handoffs through the live registry."""
    if require_direct_executor and _classifier_label(metadata) != "direct_executor":
        return SubagentRoutingDecision(False, "classifier_not_direct_executor")

    requested_handoffs, invalid_reason = _required_agent_handoffs(
        metadata,
        handoff_entries=handoff_entries,
    )
    if invalid_reason is not None:
        return SubagentRoutingDecision(False, invalid_reason)
    if not requested_handoffs:
        return SubagentRoutingDecision(False, "missing_agent_handoff")
    if len(requested_handoffs) > MAX_AGENT_HANDOFFS:
        return SubagentRoutingDecision(False, "too_many_agent_handoffs")

    resolved_registry = registry or get_subagent_registry()
    targets = _assignment_targets(metadata)
    plan: list[SubagentPlanHandoff] = []
    for agent_id, objective in requested_handoffs:
        spec = resolved_registry.get(agent_id)
        if spec is None:
            return SubagentRoutingDecision(False, "unsupported_agent_handoff")

        active_count = int((active_runs_by_agent_id or {}).get(agent_id, 0))
        if not resolved_registry.is_available(
            agent_id,
            active_runs_for_task=max(0, active_count),
        ):
            return SubagentRoutingDecision(
                False,
                "subagent_unavailable",
                agent_id=spec.id,
                agent_kind=spec.kind,
                dispatch_branch=SUBAGENT_DISPATCH_BRANCH,
                objective=objective,
            )

        if spec.requires_resolved_target and not targets:
            return SubagentRoutingDecision(
                False,
                "invalid_assignment_scope",
                agent_id=spec.id,
                agent_kind=spec.kind,
                dispatch_branch=SUBAGENT_DISPATCH_BRANCH,
                objective=objective,
            )

        capabilities, _unknown = _requested_capabilities(
            metadata,
            definition=spec,
        )
        plan.append(
            SubagentPlanHandoff(
                agent_id=spec.id,
                agent_kind=spec.kind,
                dispatch_branch=SUBAGENT_DISPATCH_BRANCH,
                capabilities=tuple(capabilities),
                targets=targets,
                objective=objective,
            )
        )

    first = plan[0]
    return SubagentRoutingDecision(
        True,
        f"{first.agent_id}_owned" if len(plan) == 1 else "ordered_handoff_plan",
        agent_id=first.agent_id,
        agent_kind=first.agent_kind,
        dispatch_branch=first.dispatch_branch,
        capabilities=first.capabilities,
        targets=first.targets,
        objective=first.objective,
        handoffs=tuple(plan),
    )


def _required_agent_handoffs(
    metadata: Mapping[str, Any],
    *,
    handoff_entries: Any = None,
) -> tuple[tuple[tuple[str, str], ...], str | None]:
    """Return ordered, normalized required handoffs from shared input."""
    raw_handoffs = handoff_entries
    if raw_handoffs is None:
        raw_handoffs = metadata.get("intent_agent_handoffs")
    if raw_handoffs is None:
        raw_response = metadata.get("intent_classifier_raw_response")
        raw_handoffs = (
            raw_response.get("agent_handoffs")
            if isinstance(raw_response, Mapping)
            else None
        )
    try:
        normalized = normalize_agent_handoff_entries(
            raw_handoffs,
            reject_invalid=True,
        )
    except ValueError:
        return (), "invalid_handoff_plan"
    return (
        tuple((entry["subagent"], entry["objective"]) for entry in normalized),
        None,
    )


def _classifier_label(metadata: Mapping[str, Any]) -> str:
    hints = metadata.get("intent_hints")
    if isinstance(hints, Mapping):
        hinted = _normalize_token(hints.get("classifier_label"))
        if hinted:
            return hinted
    return _normalize_token(metadata.get("intent_classifier_label"))


def _requested_capabilities(
    metadata: Mapping[str, Any],
    *,
    definition: SubagentDefinition,
) -> tuple[tuple[AgentCapability, ...], tuple[str, ...]]:
    raw_response = metadata.get("intent_classifier_raw_response")
    if isinstance(raw_response, Mapping):
        raw_capabilities = _sequence(raw_response.get("suggested_capabilities"))
        if raw_capabilities:
            return _normalize_capability_values(
                raw_capabilities,
                definition=definition,
            )

    values: list[Any] = []
    for key in (
        "intent_capability_candidates",
        "suggested_capabilities",
        "intent_capability",
    ):
        values.extend(_sequence(metadata.get(key)))

    hints = metadata.get("intent_hints")
    if isinstance(hints, Mapping):
        values.extend(_sequence(hints.get("suggested_capabilities")))
        values.extend(_sequence(hints.get("capabilities")))

    for key in ("intent_signals", "intent_signal_cache"):
        signals = metadata.get(key)
        if isinstance(signals, Mapping):
            values.extend(_sequence(signals.get("suggested_capabilities")))

    return _normalize_capability_values(
        values,
        definition=definition,
    )


def _normalize_capability_values(
    values: Sequence[Any],
    *,
    definition: SubagentDefinition,
) -> tuple[tuple[AgentCapability, ...], tuple[str, ...]]:
    capabilities: list[AgentCapability] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = _normalize_token(value)
        if not token or token in seen:
            continue
        seen.add(token)
        if token in _NON_CAPABILITY_ROUTE_HINTS:
            continue
        capability = resolve_definition_capability(definition, token)
        if capability is None:
            unknown.append(token)
            continue
        if capability not in capabilities:
            capabilities.append(capability)
    return tuple(capabilities), tuple(unknown)


def _assignment_targets(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    hints = metadata.get("intent_hints")
    raw_targets = _sequence(hints.get("targets") if isinstance(hints, Mapping) else None)
    targets: list[str] = []
    seen: set[str] = set()
    for raw in raw_targets:
        if not isinstance(raw, str):
            continue
        target = raw.strip()
        if not target or len(target) > MAX_TARGET_LENGTH or target in seen:
            continue
        targets.append(target)
        seen.add(target)
        if len(targets) >= MAX_ASSIGNMENT_TARGETS:
            break
    return tuple(targets)


def _sequence(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence):
        return list(value)
    return []


def _normalize_token(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower().replace("-", "_").replace(" ", "_")


__all__ = [
    "MAX_ASSIGNMENT_TARGETS",
    "MAX_AGENT_HANDOFFS",
    "MAX_TARGET_LENGTH",
    "SUBAGENT_DISPATCH_BRANCH",
    "SubagentPlanHandoff",
    "SubagentRoutingDecision",
    "normalize_agent_handoff_entries",
    "resolve_subagent_handoff",
]
