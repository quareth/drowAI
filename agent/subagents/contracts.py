"""Shared serializable contracts for asynchronous subagent runs.

This module defines the subagent data shapes exchanged between the backend
control plane and agent runtime. The contracts intentionally exclude raw
tool output, chain-of-thought, live runtime handles, database sessions, and
provider clients so values can be safely persisted in graph checkpoints or
projected to task stream events.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, TypeAlias, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from core.skills.contracts import MAX_REQUESTED_SKILLS
from core.skills.identifiers import normalize_skill_ids


AgentKind: TypeAlias = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
]
AgentId: TypeAlias = str
AgentCapability: TypeAlias = str
AgentRunStatus: TypeAlias = Literal[
    "queued",
    "running",
    "waiting_for_approval",
    "completed",
    "interrupted",
    "cancelled",
]
AgentRunOutcome: TypeAlias = Literal[
    "completed",
    "partial",
    "blocked",
    "cancelled",
]

SENSITIVE_CONTEXT_KEYS = frozenset(
    {
        "chain_of_thought",
        "cot",
        "reasoning_trace",
        "raw_reasoning",
        "raw_tool_output",
        "raw_output",
        "tool_stdout",
        "tool_stderr",
    }
)

SENSITIVE_CREDENTIAL_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "bearer_token",
        "client_secret",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
)

SENSITIVE_CREDENTIAL_VALUE_MARKERS = (
    "api_key=",
    "bearer ",
    "password=",
    "secret=",
    "sk-",
    "token=",
)


class _FrozenDict(dict[str, Any]):
    """JSON-serializable mapping that rejects post-validation mutation."""

    _frozen: bool

    def __init__(self, value: dict[str, Any]) -> None:
        self._frozen = False
        super().__init__(value)
        self._frozen = True

    def _reject_mutation(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("contract mapping is immutable")

    def __setitem__(self, key: str, value: Any) -> None:
        if self._frozen:
            self._reject_mutation()
        super().__setitem__(key, value)

    def __delitem__(self, key: str) -> None:
        self._reject_mutation()

    def clear(self) -> None:
        self._reject_mutation()

    def pop(self, *_args: Any) -> Any:
        self._reject_mutation()

    def popitem(self) -> tuple[str, Any]:
        self._reject_mutation()

    def setdefault(self, *_args: Any) -> Any:
        self._reject_mutation()

    def update(self, *_args: Any, **_kwargs: Any) -> None:
        self._reject_mutation()

    def __ior__(self, other: Any) -> "_FrozenDict":
        self._reject_mutation()


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = (
    JsonScalar | list[Any] | dict[str, Any] | tuple[Any, ...] | _FrozenDict
)


def _validate_non_empty(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _validate_json_value(value: Any, path: str) -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [_validate_json_value(item, f"{path}[]") for item in value]
    if isinstance(value, dict):
        normalized: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string key")
            normalized_key = key.strip()
            if not normalized_key:
                raise ValueError(f"{path} contains an empty key")
            if normalized_key.lower() in SENSITIVE_CONTEXT_KEYS:
                raise ValueError(f"{path}.{normalized_key} is not safe to project")
            normalized[normalized_key] = _validate_json_value(
                item, f"{path}.{normalized_key}"
            )
        return normalized
    raise ValueError(f"{path} must contain only JSON-serializable values")


def _freeze_json_value(value: JsonValue) -> JsonValue:
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    if isinstance(value, dict):
        return _FrozenDict(
            {key: _freeze_json_value(item) for key, item in value.items()}
        )
    return value


def _freeze_json_object(value: dict[str, JsonValue]) -> _FrozenDict:
    return cast(_FrozenDict, _freeze_json_value(value))


class _StrictContract(BaseModel):
    """Base model for immutable, extra-forbidden pilot contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentCredentialReference(_StrictContract):
    """Identifier-only credential reference safe for runtime identity payloads."""

    provider: str
    credential_id: str

    @model_validator(mode="before")
    @classmethod
    def _reject_secret_payload(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        for key, raw in value.items():
            normalized_key = str(key).strip().lower()
            if normalized_key in SENSITIVE_CREDENTIAL_KEYS:
                raise ValueError(f"credential_ref.{key} is not safe to project")
            if isinstance(raw, str) and _looks_like_secret_credential_value(raw):
                raise ValueError(f"credential_ref.{key} appears to contain a secret")
        return value

    @field_validator("provider", "credential_id", mode="before")
    @classmethod
    def _strip_identifier(cls, value: Any, info: Any) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{info.field_name} must be a string identifier")
        normalized = _validate_non_empty(value, info.field_name)
        if _looks_like_secret_credential_value(normalized):
            raise ValueError(f"{info.field_name} appears to contain a secret")
        return normalized


def _looks_like_secret_credential_value(value: str) -> bool:
    normalized = value.strip().lower()
    return any(marker in normalized for marker in SENSITIVE_CREDENTIAL_VALUE_MARKERS)


class AgentRuntimeIdentity(_StrictContract):
    """Serializable runtime identity inherited by a child subagent run."""

    tenant_id: int
    task_id: int
    user_id: int | None = None
    workspace_id: str
    workspace_path: str | None = None
    runtime_placement_mode: str
    actor_type: str
    actor_id: str
    runner_id: str | None = None
    execution_site_id: str | None = None
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    feature_flags: dict[str, bool] = Field(default_factory=dict)
    credential_ref: AgentCredentialReference | None = None

    @field_validator(
        "workspace_id",
        "runtime_placement_mode",
        "actor_type",
        "actor_id",
        "workspace_path",
        "runner_id",
        "execution_site_id",
        "provider",
        "model",
        "reasoning_effort",
        mode="before",
    )
    @classmethod
    def _strip_optional_strings(cls, value: Any, info: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if info.field_name in {
            "workspace_id",
            "runtime_placement_mode",
            "actor_type",
            "actor_id",
        }:
            return _validate_non_empty(normalized, info.field_name)
        return normalized or None

    @field_validator("feature_flags")
    @classmethod
    def _feature_flags_are_immutable(cls, value: dict[str, bool]) -> _FrozenDict:
        return _FrozenDict(dict(value))


class AgentAssignment(_StrictContract):
    """Immutable assignment used to launch one subagent run."""

    assignment_id: str
    agent_run_id: str
    agent_id: AgentId
    agent_kind: AgentKind
    task_id: int
    tenant_id: int
    conversation_id: str
    parent_turn_id: str
    parent_graph_thread_id: str
    objective: str
    targets: tuple[str, ...] = Field(default_factory=tuple)
    suggested_capabilities: tuple[AgentCapability, ...] = Field(default_factory=tuple)
    requested_skill_ids: tuple[str, ...] = Field(
        default_factory=tuple,
        max_length=MAX_REQUESTED_SKILLS,
    )
    scope_summary: str | None = None
    relevant_context: dict[str, Any] = Field(default_factory=dict)
    runtime_identity: AgentRuntimeIdentity

    @field_validator(
        "assignment_id",
        "agent_run_id",
        "agent_id",
        "conversation_id",
        "parent_turn_id",
        "parent_graph_thread_id",
        "objective",
        "scope_summary",
        mode="before",
    )
    @classmethod
    def _strip_assignment_strings(cls, value: Any, info: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if info.field_name == "scope_summary":
            return normalized or None
        return _validate_non_empty(normalized, info.field_name)

    @field_validator("targets", mode="before")
    @classmethod
    def _normalize_targets(cls, value: Any) -> Any:
        if value is None:
            return ()
        if not isinstance(value, list | tuple):
            return value
        return tuple(_validate_non_empty(str(item), "targets") for item in value)

    @field_validator("requested_skill_ids", mode="before")
    @classmethod
    def _normalize_requested_skill_ids(cls, value: Any) -> tuple[str, ...]:
        """Store only bounded canonical identifiers in stable request order."""

        if value is None:
            return ()
        if not isinstance(value, list | tuple):
            raise ValueError("requested_skill_ids must be a sequence")
        if len(value) > MAX_REQUESTED_SKILLS:
            raise ValueError(
                "requested_skill_ids contains more than "
                f"{MAX_REQUESTED_SKILLS} items"
            )
        try:
            return normalize_skill_ids(value)
        except ValueError as exc:
            raise ValueError(
                "requested_skill_ids contains an invalid identifier"
            ) from exc

    @field_validator("relevant_context")
    @classmethod
    def _relevant_context_is_json_safe(
        cls, value: dict[str, Any]
    ) -> _FrozenDict:
        validated = _validate_json_value(value, "relevant_context")
        if not isinstance(validated, dict):
            raise ValueError("relevant_context must be a JSON object")
        return _freeze_json_object(validated)

    @model_validator(mode="after")
    def _runtime_identity_matches_assignment(self) -> "AgentAssignment":
        if self.runtime_identity.tenant_id != self.tenant_id:
            raise ValueError("runtime_identity.tenant_id must match assignment tenant_id")
        if self.runtime_identity.task_id != self.task_id:
            raise ValueError("runtime_identity.task_id must match assignment task_id")
        return self


class AgentAssignmentProjection(_StrictContract):
    """UI-safe assignment fields for client-facing run projections."""

    assignment_id: str
    agent_run_id: str
    agent_id: AgentId
    agent_kind: AgentKind
    task_id: int
    conversation_id: str
    parent_turn_id: str
    objective: str
    targets: tuple[str, ...] = Field(default_factory=tuple)
    suggested_capabilities: tuple[AgentCapability, ...] = Field(default_factory=tuple)
    scope_summary: str | None = None

    @classmethod
    def from_assignment(
        cls,
        assignment: AgentAssignment,
    ) -> "AgentAssignmentProjection":
        """Project presentation and correlation fields without runtime identity."""
        return cls(
            assignment_id=assignment.assignment_id,
            agent_run_id=assignment.agent_run_id,
            agent_id=assignment.agent_id,
            agent_kind=assignment.agent_kind,
            task_id=assignment.task_id,
            conversation_id=assignment.conversation_id,
            parent_turn_id=assignment.parent_turn_id,
            objective=assignment.objective,
            targets=assignment.targets,
            suggested_capabilities=assignment.suggested_capabilities,
            scope_summary=assignment.scope_summary,
        )

    @field_validator(
        "assignment_id",
        "agent_run_id",
        "agent_id",
        "conversation_id",
        "parent_turn_id",
        "objective",
        "scope_summary",
        mode="before",
    )
    @classmethod
    def _strip_projection_strings(cls, value: Any, info: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if info.field_name == "scope_summary":
            return normalized or None
        return _validate_non_empty(normalized, info.field_name)

    @field_validator("targets", "suggested_capabilities", mode="before")
    @classmethod
    def _normalize_projection_lists(cls, value: Any, info: Any) -> Any:
        if value is None:
            return ()
        if not isinstance(value, list | tuple):
            return value
        return tuple(
            _validate_non_empty(str(item), info.field_name) for item in value
        )


class AgentResult(_StrictContract):
    """Safe terminal result produced by a subagent run."""

    agent_run_id: str
    agent_id: AgentId
    agent_kind: AgentKind
    outcome: AgentRunOutcome
    summary: str
    key_findings: tuple[str, ...] = Field(default_factory=tuple)
    evidence_refs: tuple[dict[str, str], ...] = Field(default_factory=tuple)
    tools_used: tuple[str, ...] = Field(default_factory=tuple)
    limitations: tuple[str, ...] = Field(default_factory=tuple)
    recommended_next_steps: tuple[str, ...] = Field(default_factory=tuple)
    final_checkpoint_id: str | None = None

    @field_validator(
        "agent_run_id",
        "agent_id",
        "summary",
        "final_checkpoint_id",
        mode="before",
    )
    @classmethod
    def _strip_result_strings(cls, value: Any, info: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if info.field_name == "final_checkpoint_id":
            return normalized or None
        return _validate_non_empty(normalized, info.field_name)

    @field_validator(
        "key_findings",
        "tools_used",
        "limitations",
        "recommended_next_steps",
        mode="before",
    )
    @classmethod
    def _normalize_string_lists(cls, value: Any, info: Any) -> Any:
        if value is None:
            return ()
        if not isinstance(value, list | tuple):
            return value
        return tuple(_validate_non_empty(str(item), info.field_name) for item in value)

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _normalize_evidence_refs(cls, value: Any) -> Any:
        if value is None:
            return ()
        if not isinstance(value, list | tuple):
            return value
        normalized: list[dict[str, str]] = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise ValueError(f"evidence_refs[{index}] must be an object")
            normalized_item: dict[str, str] = {}
            for key, raw in item.items():
                normalized_key = _validate_non_empty(str(key), "evidence_refs key")
                if normalized_key.lower() in SENSITIVE_CONTEXT_KEYS:
                    raise ValueError(
                        f"evidence_refs[{index}].{normalized_key} is not safe to project"
                    )
                normalized_item[normalized_key] = _validate_non_empty(
                    str(raw), f"evidence_refs[{index}].{normalized_key}"
                )
            normalized.append(normalized_item)
        return tuple(normalized)

    @field_validator("evidence_refs")
    @classmethod
    def _freeze_evidence_refs(
        cls, value: tuple[dict[str, str], ...]
    ) -> tuple[_FrozenDict, ...]:
        return tuple(_FrozenDict(dict(item)) for item in value)


class AgentRunLifecycleProjection(_StrictContract):
    """Safe stream projection for subagent lifecycle card updates."""

    agent_run_id: str
    agent_id: AgentId
    agent_kind: AgentKind
    agent_display_name: str
    agent_icon_key: str
    status: AgentRunStatus
    lifecycle_version: int
    task_id: int
    conversation_id: str
    parent_turn_id: str
    parent_run_id: str | None = None
    assignment: AgentAssignmentProjection | None = None
    result: "AgentResultProjection | None" = None
    safe_error: str | None = None

    @field_validator(
        "agent_run_id",
        "agent_id",
        "agent_display_name",
        "agent_icon_key",
        "conversation_id",
        "parent_turn_id",
        "parent_run_id",
        "safe_error",
        mode="before",
    )
    @classmethod
    def _strip_projection_strings(cls, value: Any, info: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if info.field_name in {"parent_run_id", "safe_error"}:
            return normalized or None
        return _validate_non_empty(normalized, info.field_name)

    @model_validator(mode="after")
    def _nested_identity_matches_projection(self) -> "AgentRunLifecycleProjection":
        if self.assignment is not None:
            assignment_fields = (
                ("agent_run_id", self.agent_run_id),
                ("agent_id", self.agent_id),
                ("agent_kind", self.agent_kind),
                ("task_id", self.task_id),
                ("conversation_id", self.conversation_id),
                ("parent_turn_id", self.parent_turn_id),
            )
            for field_name, expected in assignment_fields:
                if getattr(self.assignment, field_name) != expected:
                    raise ValueError(
                        f"assignment.{field_name} must match lifecycle projection"
                    )
        if self.result is not None and self.result.agent_run_id != self.agent_run_id:
            raise ValueError("result.agent_run_id must match lifecycle projection")
        if self.result is not None and self.result.agent_id != self.agent_id:
            raise ValueError("result.agent_id must match lifecycle projection")
        return self


class AgentResultProjection(_StrictContract):
    """Safe result subset for frontend and main-agent context handoff."""

    agent_run_id: str
    agent_id: AgentId
    agent_kind: AgentKind
    agent_display_name: str
    outcome: AgentRunOutcome
    summary: str
    key_findings: tuple[str, ...] = Field(default_factory=tuple)
    evidence_refs: tuple[dict[str, str], ...] = Field(default_factory=tuple)
    tools_used: tuple[str, ...] = Field(default_factory=tuple)
    limitations: tuple[str, ...] = Field(default_factory=tuple)
    recommended_next_steps: tuple[str, ...] = Field(default_factory=tuple)
    final_checkpoint_id: str | None = None

    @classmethod
    def from_result(
        cls,
        result: AgentResult,
        *,
        agent_display_name: str,
    ) -> "AgentResultProjection":
        """Build the public projection for a validated terminal result."""
        return cls(
            **result.model_dump(),
            agent_display_name=agent_display_name,
        )

    @field_validator("agent_display_name")
    @classmethod
    def _display_name_is_known(cls, value: str) -> str:
        return _validate_non_empty(value, "agent_display_name")

    @field_validator(
        "key_findings",
        "tools_used",
        "limitations",
        "recommended_next_steps",
        mode="before",
    )
    @classmethod
    def _normalize_projection_string_lists(cls, value: Any, info: Any) -> Any:
        return AgentResult._normalize_string_lists(value, info)

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _normalize_projection_evidence_refs(cls, value: Any) -> Any:
        return AgentResult._normalize_evidence_refs(value)

    @field_validator("evidence_refs")
    @classmethod
    def _freeze_projection_evidence_refs(
        cls, value: tuple[dict[str, str], ...]
    ) -> tuple[_FrozenDict, ...]:
        return AgentResult._freeze_evidence_refs(value)

__all__ = [
    "AgentAssignment",
    "AgentAssignmentProjection",
    "AgentCapability",
    "AgentCredentialReference",
    "AgentId",
    "AgentKind",
    "AgentResult",
    "AgentResultProjection",
    "AgentRunLifecycleProjection",
    "AgentRunOutcome",
    "AgentRunStatus",
    "AgentRuntimeIdentity",
    "JsonValue",
]
