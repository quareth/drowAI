"""Checkpoint-safe Scout graph state helpers.

This module keeps Scout-specific assignment and runtime identity data inside
the existing ``InteractiveState.facts.metadata`` carrier so shared graph nodes
can continue to parse and re-emit normal interactive state without dropping
child-run identity.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.subagents.contracts import (
    AgentAssignment,
    AgentKind,
    AgentRuntimeIdentity,
    ReconCapability,
    agent_display_name,
)
from agent.subagents.scout.profile import ScoutToolProfile, ScoutToolSpec


SCOUT_METADATA_KEY = "scout"
SCOUT_GRAPH_CAPABILITY = "simple_tool_execution"


class ScoutToolState(BaseModel):
    """Serializable projection of one Scout-visible tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: str
    display_name: str
    scout_capabilities: tuple[ReconCapability, ...] = Field(default_factory=tuple)

    @classmethod
    def from_spec(cls, spec: ScoutToolSpec) -> "ScoutToolState":
        """Create a checkpoint-safe projection from a resolved tool spec."""

        return cls(
            tool_id=spec.tool_id,
            display_name=spec.display_name,
            scout_capabilities=tuple(spec.scout_capabilities),
        )

    @field_validator("tool_id", "display_name", mode="before")
    @classmethod
    def _strip_required_string(cls, value: Any, info: Any) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{info.field_name} must be a string")
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{info.field_name} must not be empty")
        return normalized


class ScoutToolProfileState(BaseModel):
    """Serializable projection of the resolved Scout tool profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tools: tuple[ScoutToolState, ...] = Field(default_factory=tuple)

    @classmethod
    def from_profile(cls, profile: ScoutToolProfile) -> "ScoutToolProfileState":
        """Create a checkpoint-safe projection from the Scout profile."""

        return cls(tools=tuple(ScoutToolState.from_spec(spec) for spec in profile.tools))

    @property
    def tool_ids(self) -> tuple[str, ...]:
        """Return visible Scout tool ids in profile order."""

        return tuple(tool.tool_id for tool in self.tools)


class ScoutRuntimeState(BaseModel):
    """Scout assignment and child runtime identity stored in graph metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assignment: AgentAssignment
    runtime_identity: AgentRuntimeIdentity
    graph_thread_id: str
    agent_run_id: str
    agent_kind: AgentKind
    parent_turn_id: str
    parent_graph_thread_id: str
    tool_profile: ScoutToolProfileState = Field(default_factory=ScoutToolProfileState)

    @classmethod
    def from_assignment(
        cls,
        *,
        assignment: AgentAssignment,
        graph_thread_id: str,
        tool_profile: ScoutToolProfile | ScoutToolProfileState | None = None,
    ) -> "ScoutRuntimeState":
        """Build metadata for a child Scout graph from a validated assignment."""

        if isinstance(tool_profile, ScoutToolProfileState):
            profile_state = tool_profile
        elif isinstance(tool_profile, ScoutToolProfile):
            profile_state = ScoutToolProfileState.from_profile(tool_profile)
        else:
            profile_state = ScoutToolProfileState()

        return cls(
            assignment=assignment,
            runtime_identity=assignment.runtime_identity,
            graph_thread_id=graph_thread_id,
            agent_run_id=assignment.agent_run_id,
            agent_kind=assignment.agent_kind,
            parent_turn_id=assignment.parent_turn_id,
            parent_graph_thread_id=assignment.parent_graph_thread_id,
            tool_profile=profile_state,
        )

    @field_validator("graph_thread_id", mode="before")
    @classmethod
    def _strip_graph_thread_id(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("graph_thread_id must be a string")
        normalized = value.strip()
        if not normalized:
            raise ValueError("graph_thread_id must not be empty")
        return normalized

    @model_validator(mode="after")
    def _duplicates_match_assignment(self) -> "ScoutRuntimeState":
        if self.runtime_identity != self.assignment.runtime_identity:
            raise ValueError("runtime_identity must match assignment.runtime_identity")
        if self.agent_run_id != self.assignment.agent_run_id:
            raise ValueError("agent_run_id must match assignment.agent_run_id")
        if self.agent_kind != self.assignment.agent_kind:
            raise ValueError("agent_kind must match assignment.agent_kind")
        if self.parent_turn_id != self.assignment.parent_turn_id:
            raise ValueError("parent_turn_id must match assignment.parent_turn_id")
        if self.parent_graph_thread_id != self.assignment.parent_graph_thread_id:
            raise ValueError(
                "parent_graph_thread_id must match assignment.parent_graph_thread_id"
            )
        return self

    def as_metadata(self) -> dict[str, Any]:
        """Return JSON-serializable metadata for graph checkpoints."""

        return self.model_dump(mode="json")


def build_scout_initial_state(
    *,
    assignment: AgentAssignment,
    graph_thread_id: str,
    tool_profile: ScoutToolProfile | ScoutToolProfileState | None = None,
) -> dict[str, Any]:
    """Return an initial ``InteractiveState`` mapping for a Scout child run."""

    scout = ScoutRuntimeState.from_assignment(
        assignment=assignment,
        graph_thread_id=graph_thread_id,
        tool_profile=tool_profile,
    )
    metadata = _metadata_from_scout_state(scout)
    facts = FactsState(
        task_id=assignment.task_id,
        conversation_id=assignment.conversation_id,
        message=assignment.objective,
        capability=SCOUT_GRAPH_CAPABILITY,
        tool_ids=list(scout.tool_profile.tool_ids),
        tool_candidates=list(scout.tool_profile.tool_ids),
        metadata=metadata,
        intent_hints={
            "agent_kind": assignment.agent_kind,
            "suggested_capabilities": list(assignment.suggested_capabilities),
            "targets": list(assignment.targets),
        },
    )
    return _as_json_graph_state(InteractiveState(facts=facts, trace=TraceState()))


def scout_state_from_graph_state(
    state: Mapping[str, Any] | InteractiveState,
) -> ScoutRuntimeState:
    """Parse Scout metadata from an interactive graph state."""

    interactive = InteractiveState.from_mapping(state)
    scout_payload = interactive.facts.safe_metadata.get(SCOUT_METADATA_KEY)
    if not isinstance(scout_payload, Mapping):
        raise ValueError("Scout graph state is missing scout metadata")
    return ScoutRuntimeState.model_validate(dict(scout_payload))


def apply_scout_state_to_interactive(
    interactive: InteractiveState,
    scout: ScoutRuntimeState,
) -> InteractiveState:
    """Attach Scout metadata and tool profile fields to an interactive state."""

    metadata = interactive.facts.metadata_copy()
    metadata.update(_metadata_from_scout_state(scout))
    interactive.facts.metadata = metadata
    interactive.facts.tool_ids = list(scout.tool_profile.tool_ids)
    interactive.facts.tool_candidates = list(scout.tool_profile.tool_ids)
    if not interactive.facts.capability:
        interactive.facts.capability = SCOUT_GRAPH_CAPABILITY
    return interactive


def _metadata_from_scout_state(scout: ScoutRuntimeState) -> dict[str, Any]:
    turn_sequence = scout.assignment.relevant_context.get("turn_sequence")
    normalized_turn_sequence = (
        turn_sequence
        if isinstance(turn_sequence, int) and not isinstance(turn_sequence, bool)
        else 0
    )
    metadata: dict[str, Any] = {
        "producer_type": "subagent",
        "agent_run_id": scout.agent_run_id,
        "agent_kind": scout.agent_kind,
        "agent_display_name": agent_display_name(scout.agent_kind),
        "parent_turn_id": scout.parent_turn_id,
        "parent_graph_thread_id": scout.parent_graph_thread_id,
        "graph_thread_id": scout.graph_thread_id,
        "internal_only": False,
        "lifecycle_version": 1,
        "graph_runtime_context": _graph_runtime_context_from_scout_state(scout),
        METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
            conversation_id=scout.assignment.conversation_id,
            turn_id=scout.parent_turn_id,
            turn_sequence=normalized_turn_sequence,
            messages=[],
            current_message=scout.assignment.objective,
        ),
        SCOUT_METADATA_KEY: scout.as_metadata(),
    }
    parent_run_id = scout.assignment.relevant_context.get("parent_run_id")
    if isinstance(parent_run_id, str) and parent_run_id.strip():
        metadata["parent_run_id"] = parent_run_id.strip()
    if isinstance(turn_sequence, int) and not isinstance(turn_sequence, bool):
        metadata["turn_sequence"] = turn_sequence
    return metadata


def _graph_runtime_context_from_scout_state(scout: ScoutRuntimeState) -> dict[str, Any]:
    identity = scout.runtime_identity
    payload = GraphRuntimeContext(
        task_id=identity.task_id,
        user_id=identity.user_id,
        graph_thread_id=scout.graph_thread_id,
        tenant_id=identity.tenant_id,
        runtime_placement_mode=identity.runtime_placement_mode,
        workspace_id=identity.workspace_id,
        actor_type=identity.actor_type,
        actor_id=identity.actor_id,
        runner_id=identity.runner_id,
        execution_site_id=identity.execution_site_id,
        workspace_path=identity.workspace_path,
        provider=identity.provider,
        model=identity.model,
        reasoning_effort=identity.reasoning_effort,
        feature_flags=identity.feature_flags,
        turn_id=scout.parent_turn_id,
        turn_sequence=(
            scout.assignment.relevant_context.get("turn_sequence")
            if isinstance(
                scout.assignment.relevant_context.get("turn_sequence"),
                int,
            )
            and not isinstance(
                scout.assignment.relevant_context.get("turn_sequence"),
                bool,
            )
            else None
        ),
    ).model_dump()
    payload.pop("credential_ref", None)
    return payload


def _as_json_graph_state(interactive: InteractiveState) -> dict[str, Any]:
    return interactive.model_dump(mode="json")


__all__ = [
    "SCOUT_GRAPH_CAPABILITY",
    "SCOUT_METADATA_KEY",
    "ScoutRuntimeState",
    "ScoutToolProfileState",
    "ScoutToolState",
    "apply_scout_state_to_interactive",
    "build_scout_initial_state",
    "scout_state_from_graph_state",
]
