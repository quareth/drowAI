"""Task execution runtime provider contracts.

Responsibilities:
- Define runtime operation request/response envelopes shared by all providers.
- Carry tenant/task/actor/runtime-placement identity on every provider operation.
- Expose typed operation metadata without coupling to Docker, routers, or ORM layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, MutableMapping, Optional, Union

TenantId = Union[int, str]
ActorId = Union[int, str]
PlacementId = Optional[str]


class RuntimePlacementMode(str, Enum):
    """Runtime placement mode for task execution."""

    LOCAL = "local"
    RUNNER = "runner"


class RuntimeActorType(str, Enum):
    """Actor type initiating or owning a runtime operation."""

    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class RuntimeCallScope(str, Enum):
    """Known runtime operation policy scopes."""

    PRODUCT = "product"
    PRODUCT_TASK = "product_task"
    DIAGNOSTIC = "diagnostic"
    TEST = "test"
    DEV_OVERRIDE = "dev_override"


class RuntimeOperationStatus(str, Enum):
    """Normalized operation status returned by runtime providers."""

    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


_PENDING_OPERATION_STATUSES = frozenset(
    {
        RuntimeOperationStatus.ACCEPTED.value,
        RuntimeOperationStatus.RUNNING.value,
    }
)


@dataclass(slots=True, kw_only=True)
class RuntimeOperationRequest:
    """Input envelope for task runtime provider operations."""

    tenant_id: TenantId
    task_id: int
    actor_type: RuntimeActorType
    actor_id: ActorId
    runtime_placement_mode: RuntimePlacementMode
    workspace_id: str
    operation: str
    runtime_call_scope: RuntimeCallScope = RuntimeCallScope.PRODUCT_TASK
    user_id: Optional[int] = None
    runner_id: PlacementId = None
    execution_site_id: PlacementId = None
    timeout_seconds: Optional[float] = None
    metadata: MutableMapping[str, Any] = field(default_factory=dict)
    payload: MutableMapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalize enum-like request fields and reject unsupported call scopes."""
        self.actor_type = RuntimeActorType(self.actor_type)
        self.runtime_placement_mode = RuntimePlacementMode(self.runtime_placement_mode)
        self.runtime_call_scope = normalize_runtime_call_scope(self.runtime_call_scope)
        self.metadata = dict(self.metadata)
        self.metadata["runtime_call_scope"] = self.runtime_call_scope.value
        self.payload = dict(self.payload)

    def with_payload(self, **kwargs: Any) -> "RuntimeOperationRequest":
        """Return a copy-like request with payload updates for callsites."""
        updated_payload = dict(self.payload)
        updated_payload.update(kwargs)
        return RuntimeOperationRequest(
            tenant_id=self.tenant_id,
            task_id=self.task_id,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            runtime_placement_mode=self.runtime_placement_mode,
            workspace_id=self.workspace_id,
            operation=self.operation,
            runtime_call_scope=self.runtime_call_scope,
            user_id=self.user_id,
            runner_id=self.runner_id,
            execution_site_id=self.execution_site_id,
            timeout_seconds=self.timeout_seconds,
            metadata=dict(self.metadata),
            payload=updated_payload,
        )


@dataclass(slots=True, kw_only=True)
class RuntimeOperationResult:
    """Output envelope returned by task runtime provider operations."""

    tenant_id: TenantId
    task_id: int
    actor_type: RuntimeActorType
    actor_id: ActorId
    runtime_placement_mode: RuntimePlacementMode
    workspace_id: str
    accepted: bool
    provider: str
    operation: str
    status: RuntimeOperationStatus
    user_id: Optional[int] = None
    runner_id: PlacementId = None
    execution_site_id: PlacementId = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Return true when an operation is accepted and not failed."""
        if not self.accepted:
            return False
        return self.status not in (
            RuntimeOperationStatus.FAILED,
            RuntimeOperationStatus.REJECTED,
        )

    @property
    def is_pending_runner_operation(self) -> bool:
        """Return true for runner-placement operations still in accepted/running state."""
        return is_pending_runner_operation_result(self)

    @property
    def is_runner_assignment_probe(self) -> bool:
        """Return true when metadata marks a compatibility assignment-probe response."""
        return is_runner_assignment_probe_result(self)


def is_runner_placement_mode(mode: RuntimePlacementMode | str | None) -> bool:
    """Return true when runtime placement resolves to managed-runner mode."""
    if isinstance(mode, RuntimePlacementMode):
        mode_value = mode.value
    else:
        mode_value = str(mode or "").strip().lower()
    return mode_value == RuntimePlacementMode.RUNNER.value


def normalize_runtime_call_scope(scope: RuntimeCallScope | str | None) -> RuntimeCallScope:
    """Normalize runtime call scope and fail closed for unsupported values."""
    if isinstance(scope, RuntimeCallScope):
        return scope
    normalized = str(scope or "").strip().lower()
    try:
        return RuntimeCallScope(normalized)
    except ValueError as exc:
        raise ValueError(f"Unsupported runtime call scope: `{normalized}`.") from exc


def is_pending_runner_operation_result(
    result: Any,
    *,
    runtime_placement_mode: RuntimePlacementMode | str | None = None,
) -> bool:
    """Return true when a runner-placement result remains accepted/running."""
    placement_mode = (
        runtime_placement_mode
        if runtime_placement_mode is not None
        else getattr(result, "runtime_placement_mode", None)
    )
    if not is_runner_placement_mode(placement_mode):
        return False

    result_ok = getattr(result, "ok", None)
    if result_ok is None:
        if not bool(getattr(result, "accepted", False)):
            return False
    elif not bool(result_ok):
        return False

    status = getattr(result, "status", None)
    if isinstance(status, RuntimeOperationStatus):
        status_value = status.value
    else:
        status_value = str(status or "").strip().lower()
    return status_value in _PENDING_OPERATION_STATUSES


def is_runner_assignment_probe_result(
    result: Any,
    *,
    runtime_placement_mode: RuntimePlacementMode | str | None = None,
) -> bool:
    """Detect Runner Control assignment-probe compatibility responses for runner placement."""
    if not is_pending_runner_operation_result(
        result,
        runtime_placement_mode=runtime_placement_mode,
    ):
        return False

    status = getattr(result, "status", None)
    status_value = status.value if isinstance(status, RuntimeOperationStatus) else str(status or "").strip().lower()
    if status_value != RuntimeOperationStatus.ACCEPTED.value:
        return False

    raw_metadata = getattr(result, "metadata", None)
    metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
    assignment_probe = metadata.get("assignment_probe")
    if assignment_probe is not None:
        if isinstance(assignment_probe, bool):
            return assignment_probe
        return str(assignment_probe).strip().lower() in {"1", "true", "yes"}
    return str(metadata.get("protocol_domain") or "").strip() == "runner_control"


def build_runtime_result(
    request: RuntimeOperationRequest,
    *,
    accepted: bool,
    provider: str,
    status: RuntimeOperationStatus,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> RuntimeOperationResult:
    """Build a normalized provider result from a request envelope."""
    return RuntimeOperationResult(
        tenant_id=request.tenant_id,
        task_id=request.task_id,
        user_id=request.user_id,
        actor_type=request.actor_type,
        actor_id=request.actor_id,
        runtime_placement_mode=request.runtime_placement_mode,
        workspace_id=request.workspace_id,
        runner_id=request.runner_id,
        execution_site_id=request.execution_site_id,
        accepted=accepted,
        provider=provider,
        operation=request.operation,
        status=status,
        error_code=error_code,
        error_message=error_message,
        metadata=metadata or {},
    )


__all__ = [
    "ActorId",
    "PlacementId",
    "RuntimeActorType",
    "RuntimeCallScope",
    "RuntimeOperationRequest",
    "RuntimeOperationResult",
    "RuntimeOperationStatus",
    "RuntimePlacementMode",
    "TenantId",
    "build_runtime_result",
    "is_pending_runner_operation_result",
    "is_runner_placement_mode",
    "is_runner_assignment_probe_result",
    "normalize_runtime_call_scope",
]
