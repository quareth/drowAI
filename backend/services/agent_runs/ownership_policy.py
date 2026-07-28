"""Deterministic subagent ownership policy for classifier handoff routing.

The policy resolves explicit classifier handoffs through the canonical
subagent registry. It does not authorize tools, open workspaces, access
databases, or mutate the process-local run registry.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .contracts import ReconCapability
from .subagent_registry import SubagentRegistry, get_subagent_registry


MAX_ASSIGNMENT_TARGETS = 16
MAX_TARGET_LENGTH = 512

_CAPABILITY_ALIASES: dict[str, ReconCapability] = {
    "discover_hosts": "host_discovery",
    "discovery": "host_discovery",
    "gather_info": "host_discovery",
    "host_discovery": "host_discovery",
    "host_discover": "host_discovery",
    "host_enumeration": "host_discovery",
    "host_recon": "host_discovery",
    "information_gathering": "host_discovery",
    "network_discovery": "host_discovery",
    "network_scan": "port_scan",
    "network_scanning": "port_scan",
    "recon": "host_discovery",
    "reconnaissance": "host_discovery",
    "enumerate_services": "service_enum",
    "service_detection": "service_enum",
    "service_discovery": "service_enum",
    "service_enum": "service_enum",
    "service_enumeration": "service_enum",
    "port_discovery": "port_scan",
    "port_enumeration": "port_scan",
    "port_scan": "port_scan",
    "port_scanning": "port_scan",
    "scan_ports": "port_scan",
}
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
class SubagentRoutingDecision:
    """Result of deterministic registry-backed handoff evaluation."""

    should_delegate: bool
    reason: str
    subagent_name: str | None = None
    agent_kind: str | None = None
    dispatch_branch: str | None = None
    capabilities: tuple[ReconCapability, ...] = ()
    targets: tuple[str, ...] = ()
    objective: str | None = None


def resolve_subagent_handoff(
    metadata: Mapping[str, Any],
    *,
    registry: SubagentRegistry | None = None,
    active_runs_by_subagent: Mapping[str, int] | None = None,
) -> SubagentRoutingDecision:
    """Resolve one explicit classifier handoff through the live registry."""
    if _classifier_label(metadata) != "direct_executor":
        return SubagentRoutingDecision(False, "classifier_not_direct_executor")

    handoffs = _required_agent_handoffs(metadata)
    if not handoffs:
        return SubagentRoutingDecision(False, "missing_agent_handoff")
    if len(handoffs) != 1:
        return SubagentRoutingDecision(False, "unsupported_handoff_cardinality")

    subagent_name, objective = handoffs[0]
    resolved_registry = registry or get_subagent_registry()
    spec = resolved_registry.get(subagent_name)
    if spec is None:
        return SubagentRoutingDecision(False, "unsupported_agent_handoff")

    active_count = int((active_runs_by_subagent or {}).get(subagent_name, 0))
    if not resolved_registry.is_available(
        subagent_name,
        active_runs_for_task=max(0, active_count),
    ):
        return SubagentRoutingDecision(
            False,
            "subagent_unavailable",
            subagent_name=spec.name,
            agent_kind=spec.agent_kind,
            dispatch_branch=spec.dispatch_branch,
            objective=objective,
        )

    targets = _assignment_targets(metadata)
    if spec.requires_resolved_target and not targets:
        return SubagentRoutingDecision(
            False,
            "invalid_assignment_scope",
            subagent_name=spec.name,
            agent_kind=spec.agent_kind,
            dispatch_branch=spec.dispatch_branch,
            objective=objective,
        )

    capabilities, _unknown = _requested_capabilities(metadata)
    return SubagentRoutingDecision(
        True,
        f"{spec.name}_owned",
        subagent_name=spec.name,
        agent_kind=spec.agent_kind,
        dispatch_branch=spec.dispatch_branch,
        capabilities=tuple(capabilities),
        targets=targets,
        objective=objective,
    )


def should_route_to_scout(
    metadata: Mapping[str, Any],
    *,
    active_recon_run_exists: bool = False,
) -> SubagentRoutingDecision:
    """Compatibility helper for callers that still ask about Scout directly."""
    decision = resolve_subagent_handoff(
        metadata,
        active_runs_by_subagent={"scout": int(active_recon_run_exists)},
    )
    if decision.reason == "subagent_unavailable" and decision.subagent_name == "scout":
        return SubagentRoutingDecision(
            False,
            "active_recon_run_exists",
            subagent_name=decision.subagent_name,
            agent_kind=decision.agent_kind,
            dispatch_branch=decision.dispatch_branch,
            objective=decision.objective,
        )
    return decision


def _required_agent_handoffs(
    metadata: Mapping[str, Any],
) -> tuple[tuple[str, str], ...]:
    """Return ordered, normalized required handoffs from classifier metadata."""
    raw_handoffs = metadata.get("intent_agent_handoffs")
    if not isinstance(raw_handoffs, Sequence) or isinstance(raw_handoffs, str):
        raw_response = metadata.get("intent_classifier_raw_response")
        raw_handoffs = (
            raw_response.get("agent_handoffs")
            if isinstance(raw_response, Mapping)
            else None
        )
    if not isinstance(raw_handoffs, Sequence) or isinstance(raw_handoffs, str):
        return ()

    handoffs: list[tuple[str, str]] = []
    for raw_handoff in raw_handoffs:
        if not isinstance(raw_handoff, Mapping):
            continue
        handoff = _normalize_token(raw_handoff.get("agent_handoff"))
        subagent = _normalize_token(raw_handoff.get("subagent"))
        objective = raw_handoff.get("objective")
        if (
            handoff != "required"
            or not subagent
            or not isinstance(objective, str)
            or not objective.strip()
        ):
            continue
        handoffs.append((subagent, objective.strip()))
    return tuple(handoffs)


def _classifier_label(metadata: Mapping[str, Any]) -> str:
    hints = metadata.get("intent_hints")
    if isinstance(hints, Mapping):
        hinted = _normalize_token(hints.get("classifier_label"))
        if hinted:
            return hinted
    return _normalize_token(metadata.get("intent_classifier_label"))


def _requested_capabilities(
    metadata: Mapping[str, Any]
) -> tuple[tuple[ReconCapability, ...], tuple[str, ...]]:
    raw_response = metadata.get("intent_classifier_raw_response")
    if isinstance(raw_response, Mapping):
        raw_capabilities = _sequence(raw_response.get("suggested_capabilities"))
        if raw_capabilities:
            return _normalize_capability_values(raw_capabilities)

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

    return _normalize_capability_values(values)


def _normalize_capability_values(
    values: Sequence[Any],
) -> tuple[tuple[ReconCapability, ...], tuple[str, ...]]:
    capabilities: list[ReconCapability] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = _normalize_token(value)
        if not token or token in seen:
            continue
        seen.add(token)
        if token in _NON_CAPABILITY_ROUTE_HINTS:
            continue
        capability = _CAPABILITY_ALIASES.get(token)
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
    "MAX_TARGET_LENGTH",
    "SubagentRoutingDecision",
    "resolve_subagent_handoff",
    "should_route_to_scout",
]
