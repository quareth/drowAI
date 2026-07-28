"""Checkpoint-safe generic subagent graph state helpers.

Purpose
-------
Build, validate, and re-attach definition-configured subagent assignment and
runtime identity metadata inside the shared ``InteractiveState`` carrier.

Responsibility boundary
-----------------------
This module owns serializable graph-state projection only. It does not import
backend services, graph builders, LLM clients, tool implementations, or live
runtime handles.
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
from agent.subagents.contracts import AgentAssignment, AgentKind, AgentRuntimeIdentity
from agent.subagents.definition import SubagentDefinition
from agent.subagents.runtime.profile import SubagentToolProfile


SUBAGENT_METADATA_KEY = "scout"
SUBAGENT_GRAPH_CAPABILITY = "simple_tool_execution"


class SubagentToolState(BaseModel):
    """Serializable projection of one definition-visible tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: str
    display_name: str
    scout_capabilities: tuple[str, ...] = Field(default_factory=tuple)

    @classmethod
    def from_spec(cls, spec: Any) -> "SubagentToolState":
        """Create a checkpoint-safe projection from a resolved tool spec."""

        capabilities = getattr(spec, "capabilities", None)
        if capabilities is None:
            capabilities = getattr(spec, "scout_capabilities", ())
        return cls(
            tool_id=spec.tool_id,
            display_name=spec.display_name,
            scout_capabilities=tuple(capabilities),
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


class SubagentToolProfileState(BaseModel):
    """Serializable projection of the resolved subagent tool profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tools: tuple[SubagentToolState, ...] = Field(default_factory=tuple)

    @classmethod
    def from_profile(cls, profile: Any) -> "SubagentToolProfileState":
        """Create a checkpoint-safe projection from a resolved profile."""

        return cls(tools=tuple(SubagentToolState.from_spec(spec) for spec in profile.tools))

    @property
    def tool_ids(self) -> tuple[str, ...]:
        """Return visible tool ids in profile order."""

        return tuple(tool.tool_id for tool in self.tools)


class SubagentRuntimeState(BaseModel):
    """Subagent assignment and child runtime identity stored in graph metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assignment: AgentAssignment
    runtime_identity: AgentRuntimeIdentity
    graph_thread_id: str
    agent_run_id: str
    agent_id: str
    agent_kind: AgentKind
    parent_turn_id: str
    parent_graph_thread_id: str
    tool_profile: SubagentToolProfileState = Field(
        default_factory=SubagentToolProfileState
    )

    @classmethod
    def from_assignment(
        cls,
        *,
        definition: SubagentDefinition | None = None,
        assignment: AgentAssignment,
        graph_thread_id: str,
        tool_profile: SubagentToolProfile | SubagentToolProfileState | Any | None = None,
    ) -> "SubagentRuntimeState":
        """Build metadata for a child subagent graph from a validated assignment."""

        if definition is not None and assignment.agent_kind != definition.kind:
            raise ValueError("assignment.agent_kind must match definition.kind")
        if definition is not None and assignment.agent_id != definition.id:
            raise ValueError("assignment.agent_id must match definition.id")
        if isinstance(tool_profile, SubagentToolProfileState):
            profile_state = tool_profile
        elif tool_profile is not None:
            profile_state = SubagentToolProfileState.from_profile(tool_profile)
        else:
            profile_state = SubagentToolProfileState()

        return cls(
            assignment=assignment,
            runtime_identity=assignment.runtime_identity,
            graph_thread_id=graph_thread_id,
            agent_run_id=assignment.agent_run_id,
            agent_id=assignment.agent_id,
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
    def _duplicates_match_assignment(self) -> "SubagentRuntimeState":
        if self.runtime_identity != self.assignment.runtime_identity:
            raise ValueError("runtime_identity must match assignment.runtime_identity")
        if self.agent_run_id != self.assignment.agent_run_id:
            raise ValueError("agent_run_id must match assignment.agent_run_id")
        if self.agent_id != self.assignment.agent_id:
            raise ValueError("agent_id must match assignment.agent_id")
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


def build_subagent_initial_state(
    *,
    definition: SubagentDefinition,
    assignment: AgentAssignment,
    graph_thread_id: str,
    tool_profile: SubagentToolProfile | SubagentToolProfileState | Any | None = None,
) -> dict[str, Any]:
    """Return an initial ``InteractiveState`` mapping for a child subagent run."""

    subagent = SubagentRuntimeState.from_assignment(
        definition=definition,
        assignment=assignment,
        graph_thread_id=graph_thread_id,
        tool_profile=tool_profile,
    )
    metadata = _metadata_from_subagent_state(definition, subagent)
    facts = FactsState(
        task_id=assignment.task_id,
        conversation_id=assignment.conversation_id,
        message=assignment.objective,
        capability=SUBAGENT_GRAPH_CAPABILITY,
        tool_ids=list(subagent.tool_profile.tool_ids),
        tool_candidates=list(subagent.tool_profile.tool_ids),
        metadata=metadata,
        intent_hints={
            "agent_id": assignment.agent_id,
            "agent_kind": assignment.agent_kind,
            "suggested_capabilities": list(assignment.suggested_capabilities),
            "targets": list(assignment.targets),
        },
    )
    return _as_json_graph_state(InteractiveState(facts=facts, trace=TraceState()))


def subagent_state_from_graph_state(
    state: Mapping[str, Any] | InteractiveState,
    *,
    definition: SubagentDefinition,
) -> SubagentRuntimeState:
    """Parse subagent metadata from an interactive graph state."""

    interactive = InteractiveState.from_mapping(state)
    payload = interactive.facts.safe_metadata.get(_metadata_key(definition))
    if not isinstance(payload, Mapping):
        raise ValueError("Subagent graph state is missing subagent metadata")
    subagent = SubagentRuntimeState.model_validate(dict(payload))
    if subagent.agent_kind != definition.kind:
        raise ValueError("Subagent graph state kind does not match definition")
    return subagent


def apply_subagent_state_to_interactive(
    interactive: InteractiveState,
    subagent: SubagentRuntimeState,
    *,
    definition: SubagentDefinition,
) -> InteractiveState:
    """Attach subagent metadata and tool profile fields to an interactive state."""

    metadata = interactive.facts.metadata_copy()
    metadata.update(_metadata_from_subagent_state(definition, subagent))
    interactive.facts.metadata = metadata
    interactive.facts.tool_ids = list(subagent.tool_profile.tool_ids)
    interactive.facts.tool_candidates = list(subagent.tool_profile.tool_ids)
    if not interactive.facts.capability:
        interactive.facts.capability = SUBAGENT_GRAPH_CAPABILITY
    return interactive


def _metadata_from_subagent_state(
    definition: SubagentDefinition,
    subagent: SubagentRuntimeState,
) -> dict[str, Any]:
    turn_sequence = subagent.assignment.relevant_context.get("turn_sequence")
    normalized_turn_sequence = (
        turn_sequence
        if isinstance(turn_sequence, int) and not isinstance(turn_sequence, bool)
        else 0
    )
    metadata: dict[str, Any] = {
        "producer_type": "subagent",
        "agent_run_id": subagent.agent_run_id,
        "agent_id": subagent.agent_id,
        "agent_kind": subagent.agent_kind,
        "agent_display_name": definition.display_name,
        "parent_turn_id": subagent.parent_turn_id,
        "parent_graph_thread_id": subagent.parent_graph_thread_id,
        "graph_thread_id": subagent.graph_thread_id,
        "internal_only": False,
        "lifecycle_version": 1,
        "graph_runtime_context": _graph_runtime_context_from_subagent_state(subagent),
        METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
            conversation_id=subagent.assignment.conversation_id,
            turn_id=subagent.parent_turn_id,
            turn_sequence=normalized_turn_sequence,
            messages=[],
            current_message=subagent.assignment.objective,
        ),
        _metadata_key(definition): subagent.as_metadata(),
    }
    parent_run_id = subagent.assignment.relevant_context.get("parent_run_id")
    if isinstance(parent_run_id, str) and parent_run_id.strip():
        metadata["parent_run_id"] = parent_run_id.strip()
    if isinstance(turn_sequence, int) and not isinstance(turn_sequence, bool):
        metadata["turn_sequence"] = turn_sequence
    return metadata


def _graph_runtime_context_from_subagent_state(
    subagent: SubagentRuntimeState,
) -> dict[str, Any]:
    identity = subagent.runtime_identity
    payload = GraphRuntimeContext(
        task_id=identity.task_id,
        user_id=identity.user_id,
        graph_thread_id=subagent.graph_thread_id,
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
        turn_id=subagent.parent_turn_id,
        turn_sequence=(
            subagent.assignment.relevant_context.get("turn_sequence")
            if isinstance(subagent.assignment.relevant_context.get("turn_sequence"), int)
            and not isinstance(
                subagent.assignment.relevant_context.get("turn_sequence"),
                bool,
            )
            else None
        ),
    ).model_dump()
    payload.pop("credential_ref", None)
    return payload


def _metadata_key(_definition: SubagentDefinition) -> str:
    return SUBAGENT_METADATA_KEY


def _as_json_graph_state(interactive: InteractiveState) -> dict[str, Any]:
    return interactive.model_dump(mode="json")


__all__ = [
    "SUBAGENT_GRAPH_CAPABILITY",
    "SUBAGENT_METADATA_KEY",
    "SubagentRuntimeState",
    "SubagentToolProfileState",
    "SubagentToolState",
    "apply_subagent_state_to_interactive",
    "build_subagent_initial_state",
    "subagent_state_from_graph_state",
]
