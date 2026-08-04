"""Build deterministic subagent runtime assignments for parent handoffs.

This module owns the mechanical conversion from a validated ownership handoff
into an ``AgentAssignment``. Semantic delegation decisions stay with the
classifier or PAR; registry ownership validation stays with the ownership
policy.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from backend.services.agent_runs.contracts import (
    AgentAssignment,
    AgentCapability,
    AgentCredentialReference,
    AgentRuntimeIdentity,
)
from backend.services.langgraph_chat.contracts import LangGraphRuntimeConfig


def build_agent_assignment(
    runtime_config: LangGraphRuntimeConfig,
    *,
    parent_turn_id: str,
    ownership: Mapping[str, Any],
    spec: Any,
) -> AgentAssignment:
    """Build one assignment from an already validated registry handoff."""
    metadata = runtime_config.metadata
    chat_inputs = runtime_config.chat_inputs
    agent_id = _required_string(ownership.get("agent_id"), "agent_id")

    tenant_id = _required_int(metadata.get("tenant_id"), "tenant_id")
    task_id = int(chat_inputs.task_id)
    runtime_identity = AgentRuntimeIdentity(
        tenant_id=tenant_id,
        task_id=task_id,
        user_id=chat_inputs.user_id,
        workspace_id=_required_string(metadata.get("workspace_id"), "workspace_id"),
        workspace_path=_optional_string(metadata.get("workspace_path")),
        runtime_placement_mode=_required_string(
            metadata.get("runtime_placement_mode"),
            "runtime_placement_mode",
        ),
        actor_type=_required_string(metadata.get("actor_type"), "actor_type"),
        actor_id=_required_string(metadata.get("actor_id"), "actor_id"),
        runner_id=_optional_string(metadata.get("runner_id")),
        execution_site_id=_optional_string(metadata.get("execution_site_id")),
        provider=_optional_string(chat_inputs.provider or metadata.get("provider")),
        model=_optional_string(chat_inputs.model or metadata.get("runtime_model")),
        reasoning_effort=_optional_string(chat_inputs.reasoning_effort),
        feature_flags=_assignment_feature_flags(metadata),
        credential_ref=_credential_ref_from_input(chat_inputs.credential_ref),
    )
    relevant_context: dict[str, Any] = {
        "classifier_label": _optional_string(metadata.get("intent_classifier_label"))
        or _optional_string(
            (metadata.get("intent_hints") or {}).get("classifier_label")
            if isinstance(metadata.get("intent_hints"), Mapping)
            else None
        ),
        "ownership_reason": _optional_string(ownership.get("reason")),
        "delegation_source": _optional_string(ownership.get("delegation_source")),
        "delegation_decision_id": _optional_string(
            ownership.get("delegation_decision_id")
        ),
        "parent_run_id": parent_run_id_from_metadata(metadata),
        "turn_sequence": metadata.get("turn_sequence"),
        "agent_mode": chat_inputs.agent_mode.value,
    }
    reserved_message_id = metadata.get("reserved_message_id")
    if isinstance(reserved_message_id, int) and not isinstance(
        reserved_message_id,
        bool,
    ):
        relevant_context["reserved_message_id"] = reserved_message_id

    return AgentAssignment(
        assignment_id=_optional_string(ownership.get("assignment_id"))
        or f"assignment-{uuid4().hex}",
        agent_run_id=_optional_string(ownership.get("agent_run_id"))
        or f"agent-run-{uuid4().hex}",
        agent_id=agent_id,
        agent_kind=spec.kind,
        task_id=task_id,
        tenant_id=tenant_id,
        conversation_id=_required_string(
            chat_inputs.conversation_id,
            "conversation_id",
        ),
        parent_turn_id=parent_turn_id,
        parent_graph_thread_id=_required_string(
            metadata.get("graph_thread_id"),
            "graph_thread_id",
        ),
        objective=_required_string(ownership.get("objective"), "objective"),
        targets=tuple(string_list(ownership.get("targets"))),
        suggested_capabilities=tuple(
            _agent_capabilities(
                ownership.get("capabilities"),
                allowed=spec.supported_task_categories,
            )
        ),
        scope_summary=_scope_summary(ownership.get("targets")),
        relevant_context=relevant_context,
        runtime_identity=runtime_identity,
    )


def string_list(value: Any) -> list[str]:
    """Return non-empty string values from scalar or list-like input."""
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list | tuple):
        values = list(value)
    else:
        values = []
    return [str(item).strip() for item in values if str(item).strip()]


def parent_run_id_from_metadata(metadata: Mapping[str, Any]) -> str | None:
    """Resolve the stable parent run identifier from turn metadata."""
    for key in ("parent_run_id", "run_id", "turn_id"):
        value = _optional_string(metadata.get(key))
        if value:
            return value
    return None


def _assignment_feature_flags(metadata: Mapping[str, Any]) -> dict[str, bool]:
    flags = metadata.get("feature_flags")
    return {
        str(key): bool(value)
        for key, value in (flags.items() if isinstance(flags, Mapping) else ())
        if isinstance(key, str)
    }


def _credential_ref_from_input(value: Any) -> AgentCredentialReference | None:
    if not isinstance(value, Mapping):
        return None
    provider = _optional_string(value.get("provider"))
    credential_id = _optional_string(value.get("credential_id"))
    if not provider or not credential_id:
        return None
    return AgentCredentialReference(provider=provider, credential_id=credential_id)


def _scope_summary(value: Any) -> str | None:
    targets = string_list(value)
    if not targets:
        return None
    return "Targets: " + ", ".join(targets)


def _agent_capabilities(
    value: Any,
    *,
    allowed: tuple[str, ...],
) -> list[AgentCapability]:
    allowed_set = set(allowed)
    return [
        capability
        for capability in string_list(value)
        if capability in allowed_set
    ]


def _required_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Subagent assignment requires {field_name}") from exc


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
    "build_agent_assignment",
    "parent_run_id_from_metadata",
    "string_list",
]
