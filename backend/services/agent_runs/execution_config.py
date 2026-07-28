"""Build safe LangGraph execution config for process-local Scout child runs.

This module owns the boundary between a parent chat turn and the Scout child
checkpoint thread. It verifies tenant/task/user ownership through the live
process-local registry and returns only serializable config values.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.graph.graph_names import GRAPH_NAME_SCOUT_RECON
from agent.graph.infrastructure.state_models import checkpoint_safe_llm_runtime_selection
from backend.services.langgraph_chat.checkpoint.thread_identity import (
    format_graph_thread_id,
    require_graph_thread_id,
)
from backend.services.langgraph_chat.contracts import LangGraphRuntimeConfig

from .contracts import AgentAssignment, AgentRuntimeIdentity
from .contracts import agent_display_name
from .registry import ProcessLocalAgentRunRegistry


class ChildExecutionConfigError(RuntimeError):
    """Raised when Scout child execution config cannot be built safely."""


async def build_child_execution_config(
    *,
    assignment: AgentAssignment,
    runtime_config: LangGraphRuntimeConfig,
    registry: ProcessLocalAgentRunRegistry,
    graph_thread_id: str,
) -> dict[str, Any]:
    """Return checkpoint-safe LangGraph config for one registered Scout child run."""
    _authorize_parent_runtime(assignment=assignment, runtime_config=runtime_config)
    child_graph_thread_id = require_graph_thread_id(
        graph_thread_id,
        task_id=assignment.task_id,
    )
    entry = await registry.get(
        tenant_id=assignment.tenant_id,
        task_id=assignment.task_id,
        agent_run_id=assignment.agent_run_id,
    )
    if entry is None:
        raise ChildExecutionConfigError("Scout child run is not registered")
    if entry.graph_thread_id != child_graph_thread_id:
        raise ChildExecutionConfigError("Scout child thread does not match registry")

    parent_run_id = _parent_run_id(runtime_config.metadata) or _parent_run_id(
        assignment.relevant_context
    )
    runtime_projection = _runtime_projection(
        assignment.runtime_identity,
        child_graph_thread_id=child_graph_thread_id,
        assignment=assignment,
        parent_run_id=parent_run_id,
    )
    configurable: dict[str, Any] = {
        "thread_id": format_graph_thread_id(
            child_graph_thread_id,
            task_id=assignment.task_id,
        ),
        "graph_name": GRAPH_NAME_SCOUT_RECON,
        "graph_thread_id": child_graph_thread_id,
        "producer_type": "subagent",
        "agent_run_id": assignment.agent_run_id,
        "agent_id": assignment.agent_id,
        "agent_kind": assignment.agent_kind,
        "agent_display_name": agent_display_name(assignment.agent_id),
        "parent_turn_id": assignment.parent_turn_id,
        "parent_run_id": parent_run_id,
        "parent_graph_thread_id": assignment.parent_graph_thread_id,
        "internal_only": False,
        "lifecycle_version": entry.lifecycle_version,
        "runtime_projection": runtime_projection,
    }

    selection_payload = checkpoint_safe_llm_runtime_selection(
        runtime_config.llm_runtime_selection
        or runtime_config.chat_inputs.llm_runtime_selection
    )
    if selection_payload:
        configurable["llm_runtime_selection"] = selection_payload

    return {"configurable": configurable}


def _authorize_parent_runtime(
    *,
    assignment: AgentAssignment,
    runtime_config: LangGraphRuntimeConfig,
) -> None:
    metadata = runtime_config.metadata
    chat_inputs = runtime_config.chat_inputs
    runtime_identity = assignment.runtime_identity
    _require_equal(
        "tenant_id",
        _coerce_int(metadata.get("tenant_id")),
        assignment.tenant_id,
    )
    _require_equal("task_id", int(chat_inputs.task_id), assignment.task_id)
    _require_equal("user_id", int(chat_inputs.user_id), runtime_identity.user_id)
    _require_equal(
        "parent_graph_thread_id",
        _non_empty(metadata.get("graph_thread_id")),
        assignment.parent_graph_thread_id,
    )
    _require_equal(
        "workspace_id",
        _non_empty(metadata.get("workspace_id")),
        runtime_identity.workspace_id,
    )
    _require_equal(
        "runtime_placement_mode",
        _non_empty(metadata.get("runtime_placement_mode")),
        runtime_identity.runtime_placement_mode,
    )
    _require_equal(
        "actor_type",
        _non_empty(metadata.get("actor_type")),
        runtime_identity.actor_type,
    )
    _require_equal(
        "actor_id",
        _non_empty(metadata.get("actor_id")),
        runtime_identity.actor_id,
    )
    _require_equal(
        "runner_id",
        _optional_string(metadata.get("runner_id")),
        runtime_identity.runner_id,
    )
    _require_equal(
        "execution_site_id",
        _optional_string(metadata.get("execution_site_id")),
        runtime_identity.execution_site_id,
    )


def _runtime_projection(
    runtime_identity: AgentRuntimeIdentity,
    *,
    child_graph_thread_id: str,
    assignment: AgentAssignment,
    parent_run_id: str | None,
) -> dict[str, Any]:
    projection = runtime_identity.model_dump(mode="json", exclude_none=True)
    projection["graph_thread_id"] = child_graph_thread_id
    projection["agent_run_id"] = assignment.agent_run_id
    projection["agent_id"] = assignment.agent_id
    projection["agent_kind"] = assignment.agent_kind
    projection["parent_turn_id"] = assignment.parent_turn_id
    projection["parent_run_id"] = parent_run_id
    projection["parent_graph_thread_id"] = assignment.parent_graph_thread_id
    return projection


def _parent_run_id(metadata: Mapping[str, Any]) -> str | None:
    for key in ("parent_run_id", "run_id", "turn_id"):
        value = _optional_string(metadata.get(key))
        if value:
            return value
    return None


def _require_equal(field_name: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        raise ChildExecutionConfigError(f"Scout child config {field_name} mismatch")


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ChildExecutionConfigError("Scout child config tenant_id mismatch") from exc


def _non_empty(value: Any) -> str:
    normalized = _optional_string(value)
    if normalized is None:
        raise ChildExecutionConfigError("Scout child config is missing parent identity")
    return normalized


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        raise ChildExecutionConfigError("Scout child config contains invalid identity")
    normalized = str(value).strip()
    return normalized or None


__all__ = [
    "ChildExecutionConfigError",
    "build_child_execution_config",
]
