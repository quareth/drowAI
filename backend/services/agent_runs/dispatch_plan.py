"""Build validated, immutable subagent dispatch plans.

This module converts an already-positive ownership decision into ordered
assignments and stable child graph identities. It does not inspect live run
capacity, mutate the run registry, launch workers, or execute parent graphs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from agent.subagents.registry import SubagentRegistry, get_subagent_registry
from backend.services.langgraph_chat.checkpoint.thread_identity import (
    generate_graph_thread_id,
)
from backend.services.langgraph_chat.contracts import LangGraphRuntimeConfig

from .assignment_builder import build_agent_assignment, string_list
from .contracts import AgentAssignment
from .ownership_policy import MAX_AGENT_HANDOFFS, SubagentRoutingDecision


GraphThreadIdFactory = Callable[[], str]


@dataclass(frozen=True, slots=True)
class PlannedAgentInvocation:
    """One immutable subagent invocation prepared before launch."""

    index: int
    assignment: AgentAssignment
    display_name: str
    graph_thread_id: str


def build_assignment(
    runtime_config: LangGraphRuntimeConfig,
    *,
    parent_turn_id: str,
    subagent_registry: SubagentRegistry | None = None,
) -> AgentAssignment:
    """Build the first assignment from a validated single-handoff plan."""
    return build_dispatch_plan(
        runtime_config,
        parent_turn_id=parent_turn_id,
        subagent_registry=subagent_registry,
    )[0].assignment


def build_dispatch_plan(
    runtime_config: LangGraphRuntimeConfig,
    *,
    parent_turn_id: str,
    subagent_registry: SubagentRegistry | None = None,
    graph_thread_id_factory: GraphThreadIdFactory = generate_graph_thread_id,
) -> tuple[PlannedAgentInvocation, ...]:
    """Validate routing metadata and build its ordered invocation plan."""
    metadata = runtime_config.metadata
    ownership = metadata.get("subagent_routing")
    if not isinstance(ownership, Mapping) or not ownership.get("should_delegate"):
        raise RuntimeError("Subagent branch requires a positive ownership decision")

    raw_handoffs = ownership.get("handoffs")
    if isinstance(raw_handoffs, list | tuple) and raw_handoffs:
        requested_handoffs = tuple(raw_handoffs)
    else:
        requested_handoffs = (ownership,)

    if len(requested_handoffs) > MAX_AGENT_HANDOFFS:
        raise RuntimeError("Subagent dispatch plan has too many handoffs")

    registry = subagent_registry or get_subagent_registry()
    validated: list[tuple[Mapping[str, Any], Any]] = []
    for raw_handoff in requested_handoffs:
        if not isinstance(raw_handoff, Mapping):
            raise RuntimeError("Subagent dispatch plan contains an invalid handoff")
        if "reason" not in raw_handoff and "reason" in ownership:
            raw_handoff = {**raw_handoff, "reason": ownership.get("reason")}
        agent_id = _required_string(raw_handoff.get("agent_id"), "agent_id")
        try:
            spec = registry.require(agent_id)
        except KeyError as exc:
            raise RuntimeError(
                f"subagent is not registered or enabled: {agent_id}"
            ) from exc
        if raw_handoff.get("agent_kind") != spec.kind:
            raise RuntimeError("Subagent branch agent kind does not match registry")
        if spec.requires_resolved_target and not string_list(
            raw_handoff.get("targets")
        ):
            raise RuntimeError("Subagent dispatch plan requires resolved targets")
        validated.append((raw_handoff, spec))

    return tuple(
        PlannedAgentInvocation(
            index=index,
            assignment=build_agent_assignment(
                runtime_config,
                parent_turn_id=parent_turn_id,
                ownership=raw_handoff,
                spec=spec,
            ),
            display_name=spec.display_name,
            graph_thread_id=graph_thread_id_factory(),
        )
        for index, (raw_handoff, spec) in enumerate(validated)
    )


def runtime_config_with_subagent_routing(
    runtime_config: LangGraphRuntimeConfig,
    routing: dict[str, Any],
) -> LangGraphRuntimeConfig:
    """Return a shallow runtime config copy with PAR routing metadata."""
    metadata = dict(runtime_config.metadata)
    metadata["subagent_routing"] = routing
    return LangGraphRuntimeConfig(
        chat_inputs=runtime_config.chat_inputs,
        tooling=runtime_config.tooling,
        persistence=runtime_config.persistence,
        execution_mode=runtime_config.execution_mode,
        metadata=metadata,
        llm_runtime_selection=runtime_config.llm_runtime_selection,
        runtime_services=runtime_config.runtime_services,
    )


def routing_metadata_from_decision(
    decision: SubagentRoutingDecision,
    *,
    delegation_source: str | None = None,
    delegation_decision_id: str | None = None,
) -> dict[str, Any]:
    """Project an ownership decision into dispatch-plan metadata."""
    stable_handoffs = [
        _handoff_metadata(
            handoff,
            reason=decision.reason,
            delegation_source=delegation_source,
            delegation_decision_id=delegation_decision_id,
        )
        for handoff in decision.handoffs
    ]
    first = stable_handoffs[0] if stable_handoffs else {}
    return {
        "should_delegate": decision.should_delegate,
        "reason": decision.reason,
        "agent_id": decision.agent_id,
        "agent_kind": decision.agent_kind,
        "dispatch_branch": decision.dispatch_branch,
        "capabilities": list(decision.capabilities),
        "targets": list(decision.targets),
        "objective": decision.objective,
        "delegation_source": delegation_source,
        "delegation_decision_id": delegation_decision_id,
        "assignment_id": first.get("assignment_id"),
        "agent_run_id": first.get("agent_run_id"),
        "handoffs": stable_handoffs,
    }


def stable_par_assignment_identity(
    *,
    delegation_decision_id: str,
    agent_id: str,
    objective: str,
) -> tuple[str, str]:
    """Return replay-stable assignment and run IDs for one PAR decision."""
    identity = {
        "delegation_decision_id": delegation_decision_id,
        "agent_id": agent_id,
        "objective": objective,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]
    return f"assignment-par-{digest}", f"agent-run-par-{digest}"


def _handoff_metadata(
    handoff: Any,
    *,
    reason: str,
    delegation_source: str | None,
    delegation_decision_id: str | None,
) -> dict[str, Any]:
    payload = {
        "agent_id": handoff.agent_id,
        "agent_kind": handoff.agent_kind,
        "dispatch_branch": handoff.dispatch_branch,
        "reason": reason,
        "capabilities": list(handoff.capabilities),
        "targets": list(handoff.targets),
        "objective": handoff.objective,
        "delegation_source": delegation_source,
        "delegation_decision_id": delegation_decision_id,
    }
    if delegation_source == "par" and delegation_decision_id:
        assignment_id, agent_run_id = stable_par_assignment_identity(
            delegation_decision_id=delegation_decision_id,
            agent_id=handoff.agent_id,
            objective=handoff.objective,
        )
        payload["assignment_id"] = assignment_id
        payload["agent_run_id"] = agent_run_id
    return payload


def _required_string(value: Any, field_name: str) -> str:
    normalized = _optional_string(value)
    if not normalized:
        raise RuntimeError(f"Subagent assignment requires {field_name}")
    return normalized


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


__all__ = [
    "GraphThreadIdFactory",
    "PlannedAgentInvocation",
    "build_assignment",
    "build_dispatch_plan",
    "routing_metadata_from_decision",
    "runtime_config_with_subagent_routing",
    "stable_par_assignment_identity",
]
