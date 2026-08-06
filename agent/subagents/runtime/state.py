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

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.context.runtime_state import refresh_bundle_from_working_memory
from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.memory.memory_manager import MemoryManager
from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.subagents.contracts import AgentAssignment, AgentKind, AgentRuntimeIdentity
from agent.subagents.definition import SubagentDefinition
from agent.subagents.runtime.profile import (
    SubagentToolProfile,
    effective_subagent_tool_profile,
    resolve_subagent_tool_profile,
)


SUBAGENT_METADATA_KEY = "subagent"
SUBAGENT_GRAPH_CAPABILITY = "simple_tool_execution"
SUPPORTED_AGENT_MODES = frozenset({"agent", "full_access", "plan", "chat"})


class SubagentToolState(BaseModel):
    """Serializable projection of one definition-visible tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: str
    display_name: str
    capabilities: tuple[str, ...] = Field(default_factory=tuple)

    @classmethod
    def from_spec(cls, spec: Any) -> "SubagentToolState":
        """Create a checkpoint-safe projection from a resolved tool spec."""

        return cls(
            tool_id=spec.tool_id,
            display_name=spec.display_name,
            capabilities=tuple(spec.capabilities),
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
    graph_thread_id: str
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
        if definition is None:
            if isinstance(tool_profile, SubagentToolProfileState):
                profile_state = tool_profile
            elif tool_profile is not None:
                profile_state = SubagentToolProfileState.from_profile(tool_profile)
            else:
                profile_state = SubagentToolProfileState()
        else:
            effective_profile = effective_subagent_tool_profile(
                definition,
                tool_profile,
            )
            profile_state = SubagentToolProfileState.from_profile(effective_profile)

        return cls(
            assignment=assignment,
            graph_thread_id=graph_thread_id,
            tool_profile=profile_state,
        )

    @property
    def runtime_identity(self) -> AgentRuntimeIdentity:
        """Return runtime identity from the canonical assignment payload."""

        return self.assignment.runtime_identity

    @property
    def agent_run_id(self) -> str:
        """Return child run identity from the canonical assignment payload."""

        return self.assignment.agent_run_id

    @property
    def agent_id(self) -> str:
        """Return subagent definition identity from the assignment payload."""

        return self.assignment.agent_id

    @property
    def agent_kind(self) -> AgentKind:
        """Return subagent kind from the assignment payload."""

        return self.assignment.agent_kind

    @property
    def parent_turn_id(self) -> str:
        """Return parent turn identity from the assignment payload."""

        return self.assignment.parent_turn_id

    @property
    def parent_graph_thread_id(self) -> str:
        """Return parent graph thread identity from the assignment payload."""

        return self.assignment.parent_graph_thread_id

    @field_validator("graph_thread_id", mode="before")
    @classmethod
    def _strip_graph_thread_id(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("graph_thread_id must be a string")
        normalized = value.strip()
        if not normalized:
            raise ValueError("graph_thread_id must not be empty")
        return normalized

    def as_metadata(self) -> dict[str, Any]:
        """Return JSON-serializable metadata for graph checkpoints."""

        return self.model_dump(mode="json", exclude={"graph_thread_id", "tool_profile"})


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
    metadata["working_memory"] = _initial_working_memory(definition, subagent)
    refresh_bundle_from_working_memory(metadata)
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


def _initial_working_memory(
    definition: SubagentDefinition,
    subagent: SubagentRuntimeState,
) -> dict[str, Any]:
    """Seed canonical child memory through the turn-start reducer."""

    assignment = subagent.assignment
    turn_sequence = assignment.relevant_context.get("turn_sequence")
    normalized_turn_sequence = (
        turn_sequence
        if isinstance(turn_sequence, int) and not isinstance(turn_sequence, bool)
        else 0
    )
    return MemoryManager.reduce_turn_start(
        previous=None,
        user_message=assignment.objective,
        conversation_history_tail=[],
        runtime_ids={
            "tenant_id": assignment.tenant_id,
            "task_id": assignment.task_id,
            "conversation_id": assignment.conversation_id,
            "turn_id": assignment.parent_turn_id,
            "turn_sequence": normalized_turn_sequence,
            "parent_turn_id": assignment.parent_turn_id,
            "parent_graph_thread_id": assignment.parent_graph_thread_id,
            "graph_thread_id": subagent.graph_thread_id,
            "agent_run_id": subagent.agent_run_id,
            "agent_id": subagent.agent_id,
            "agent_kind": subagent.agent_kind,
        },
        route=SUBAGENT_GRAPH_CAPABILITY,
        constraints=_initial_memory_constraints(definition, subagent),
        intent_hints={
            "target": assignment.targets[0] if assignment.targets else None,
            "targets": list(assignment.targets),
            "agent_id": assignment.agent_id,
            "agent_kind": assignment.agent_kind,
            "suggested_capabilities": list(assignment.suggested_capabilities),
        },
        intent_target_continuity={"status": "disallow"},
        intent_turn_interpretation={
            "next_operational_goal": assignment.objective,
            "execution_readiness": "ready",
            "blocking_reason": "",
        },
    )


def _initial_memory_constraints(
    definition: SubagentDefinition,
    subagent: SubagentRuntimeState,
) -> dict[str, Any]:
    """Return bounded assignment constraints for child working memory."""

    _ = definition
    assignment = subagent.assignment
    return {"scope": [assignment.scope_summary] if assignment.scope_summary else []}


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
    normalized_payload = dict(payload)
    if "graph_thread_id" not in normalized_payload:
        normalized_payload["graph_thread_id"] = interactive.facts.safe_metadata.get(
            "graph_thread_id"
        )
    subagent = SubagentRuntimeState.model_validate(normalized_payload)
    if subagent.agent_kind != definition.kind:
        raise ValueError("Subagent graph state kind does not match definition")
    if subagent.tool_profile.tools:
        return subagent
    return SubagentRuntimeState.from_assignment(
        definition=definition,
        assignment=subagent.assignment,
        graph_thread_id=subagent.graph_thread_id,
        tool_profile=resolve_subagent_tool_profile(
            definition,
            interactive.facts.tool_candidates
            or interactive.facts.tool_ids
            or definition.tool_ids,
        ),
    )


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
    approval_metadata = _approval_metadata_from_assignment(subagent.assignment)
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
        **approval_metadata,
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


def _approval_metadata_from_assignment(
    assignment: AgentAssignment,
) -> dict[str, Any]:
    """Restore the parent HITL policy required by the shared approval gate."""

    context = assignment.relevant_context
    agent_mode = context.get("agent_mode")
    if agent_mode not in SUPPORTED_AGENT_MODES:
        raise ValueError("Subagent assignment is missing a valid agent_mode")

    metadata: dict[str, Any] = {"agent_mode": agent_mode}
    reserved_message_id = context.get("reserved_message_id")
    if isinstance(reserved_message_id, int) and not isinstance(
        reserved_message_id,
        bool,
    ):
        metadata["reserved_message_id"] = reserved_message_id
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
        execution_owner_id=f"subagent:{subagent.agent_run_id}",
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
