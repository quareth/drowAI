"""Retention executor contracts and safe result serialization helpers.

This module defines side-effect-free DTOs shared by retention policy
resolution, orchestration, and module-owned executors.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, is_dataclass
from enum import Enum
import re
from typing import Any, Mapping, Protocol, Sequence, TypeAlias


RetentionClass: TypeAlias = str
RetentionRunMode: TypeAlias = str
RetentionReasonCode: TypeAlias = str
TenantId: TypeAlias = int


RETENTION_CLASS_OPERATIONAL_EPHEMERAL: RetentionClass = "operational_ephemeral"
RETENTION_CLASS_RUNTIME_RESUME_STATE: RetentionClass = "runtime_resume_state"
RETENTION_CLASS_TASK_RECORD: RetentionClass = "task_record"
RETENTION_CLASS_TASK_TRANSCRIPT: RetentionClass = "task_transcript"
RETENTION_CLASS_ARTIFACT_PAYLOAD: RetentionClass = "artifact_payload"
RETENTION_CLASS_EXECUTION_PROVENANCE: RetentionClass = "execution_provenance"
RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE: RetentionClass = "engagement_knowledge"
RETENTION_CLASS_SEMANTIC_MEMORY: RetentionClass = "semantic_memory"
RETENTION_CLASS_REPORTING: RetentionClass = "reporting"
RETENTION_CLASS_USAGE_ACCOUNTING: RetentionClass = "usage_accounting"

RETENTION_CLASSES: tuple[RetentionClass, ...] = (
    RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
    RETENTION_CLASS_RUNTIME_RESUME_STATE,
    RETENTION_CLASS_TASK_RECORD,
    RETENTION_CLASS_TASK_TRANSCRIPT,
    RETENTION_CLASS_ARTIFACT_PAYLOAD,
    RETENTION_CLASS_EXECUTION_PROVENANCE,
    RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,
    RETENTION_CLASS_SEMANTIC_MEMORY,
    RETENTION_CLASS_REPORTING,
    RETENTION_CLASS_USAGE_ACCOUNTING,
)

RETENTION_RUN_MODE_DRY_RUN: RetentionRunMode = "dry_run"
RETENTION_RUN_MODE_APPLY: RetentionRunMode = "apply"
RETENTION_RUN_MODES: tuple[RetentionRunMode, ...] = (
    RETENTION_RUN_MODE_DRY_RUN,
    RETENTION_RUN_MODE_APPLY,
)

RETENTION_SCOPE_TENANT: str = "tenant"
RETENTION_SCOPE_ALL_TENANTS: str = "all_tenants"
RETENTION_SCOPES: tuple[str, ...] = (
    RETENTION_SCOPE_TENANT,
    RETENTION_SCOPE_ALL_TENANTS,
)

RETENTION_DECISION_CANDIDATE: str = "candidate"
RETENTION_DECISION_PROTECTED: str = "protected"
RETENTION_DECISION_APPLIED: str = "applied"
RETENTION_DECISION_SKIPPED: str = "skipped"
RETENTION_DECISION_FAILED: str = "failed"
RETENTION_DECISION_OUTCOMES: tuple[str, ...] = (
    RETENTION_DECISION_CANDIDATE,
    RETENTION_DECISION_PROTECTED,
    RETENTION_DECISION_APPLIED,
    RETENTION_DECISION_SKIPPED,
    RETENTION_DECISION_FAILED,
)

_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.:@#/-]{1,256}$")
_UNSAFE_FIELD_NAME_PARTS: frozenset[str] = frozenset(
    {
        "api_key",
        "bearer",
        "content",
        "cookie",
        "credential",
        "jwt",
        "object_key",
        "objectkey",
        "payload",
        "prompt",
        "secret",
        "token",
        "transcript",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RetentionRunRequest:
    """Input envelope for one orchestrated retention run."""

    mode: RetentionRunMode
    scope: str = RETENTION_SCOPE_TENANT
    tenant_id: TenantId | None = None
    retention_classes: tuple[RetentionClass, ...] = RETENTION_CLASSES
    limit_per_tenant: int | None = None

    def __post_init__(self) -> None:
        validate_run_mode(self.mode)
        validate_scope(self.scope)
        for retention_class in self.retention_classes:
            validate_retention_class(retention_class)
        if self.scope == RETENTION_SCOPE_TENANT and self.tenant_id is None:
            raise ValueError("tenant_id is required for tenant-scoped retention")
        if self.scope == RETENTION_SCOPE_ALL_TENANTS and self.tenant_id is not None:
            raise ValueError("tenant_id must be omitted for all-tenant retention")
        if self.limit_per_tenant is not None and self.limit_per_tenant < 1:
            raise ValueError("limit_per_tenant must be positive when provided")


@dataclass(frozen=True, slots=True, kw_only=True)
class RetentionDecision:
    """Safe per-candidate decision summary emitted by module executors."""

    retention_class: RetentionClass
    outcome: str
    reason_code: RetentionReasonCode
    resource_id: str | None = None
    count: int = 1

    def __post_init__(self) -> None:
        validate_retention_class(self.retention_class)
        validate_decision_outcome(self.outcome)
        object.__setattr__(
            self,
            "reason_code",
            normalize_reason_code(self.reason_code),
        )
        if self.resource_id is not None:
            validate_safe_identifier(self.resource_id)
        if self.count < 0:
            raise ValueError("count must be non-negative")


@dataclass(frozen=True, slots=True, kw_only=True)
class RetentionBatchCounts:
    """Bounded batch counters shared by dry-run and apply results."""

    scanned_count: int = 0
    candidate_count: int = 0
    protected_count: int = 0
    applied_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    preserved_count: int = 0
    already_deleted_count: int = 0
    batch_count: int = 0
    batch_limit: int | None = None

    def __post_init__(self) -> None:
        for field_info in fields(self):
            value = getattr(self, field_info.name)
            if value is not None and value < 0:
                raise ValueError(f"{field_info.name} must be non-negative")


@dataclass(frozen=True, slots=True, kw_only=True)
class RetentionExecutorResult:
    """Safe result returned by one module-owned retention executor."""

    executor_name: str
    retention_class: RetentionClass
    mode: RetentionRunMode
    tenant_id: TenantId
    counts: RetentionBatchCounts
    reason_counts: Mapping[RetentionReasonCode, int]
    decisions: tuple[RetentionDecision, ...] = ()
    succeeded: bool = True
    error_code: str | None = None

    def __post_init__(self) -> None:
        validate_safe_identifier(self.executor_name)
        validate_retention_class(self.retention_class)
        validate_run_mode(self.mode)
        if self.tenant_id < 1:
            raise ValueError("tenant_id must be positive")
        normalized_reason_counts = {}
        for reason_code, count in self.reason_counts.items():
            normalized_reason_counts[normalize_reason_code(reason_code)] = count
            if count < 0:
                raise ValueError("reason_counts values must be non-negative")
        object.__setattr__(self, "reason_counts", normalized_reason_counts)
        for decision in self.decisions:
            if decision.retention_class != self.retention_class:
                raise ValueError("decision retention_class must match result")
        if self.error_code is not None:
            object.__setattr__(
                self,
                "error_code",
                normalize_reason_code(self.error_code),
            )

    def to_safe_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible dictionary with unsafe field names rejected."""

        return to_safe_dict(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class RetentionRunResult:
    """Safe aggregate result for a tenant or all-tenant retention request."""

    mode: RetentionRunMode
    scope: str
    tenant_id: TenantId | None
    results: tuple[RetentionExecutorResult, ...]
    succeeded: bool = True

    def __post_init__(self) -> None:
        validate_run_mode(self.mode)
        validate_scope(self.scope)
        if self.scope == RETENTION_SCOPE_TENANT and self.tenant_id is None:
            raise ValueError("tenant_id is required for tenant-scoped retention")
        if self.scope == RETENTION_SCOPE_ALL_TENANTS and self.tenant_id is not None:
            raise ValueError("tenant_id must be omitted for all-tenant retention")
        for result in self.results:
            if result.mode != self.mode:
                raise ValueError("executor result mode must match run result mode")

    def to_safe_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible dictionary with unsafe field names rejected."""

        return to_safe_dict(self)


class RetentionExecutor(Protocol):
    """Structural contract for module-owned retention executors."""

    name: str
    retention_class: RetentionClass

    def run(
        self,
        *,
        policy: object,
        tenant_id: TenantId,
        mode: RetentionRunMode,
        limit: int,
    ) -> RetentionExecutorResult:
        """Evaluate candidates and optionally apply bounded retention actions."""


def validate_retention_class(value: str) -> RetentionClass:
    """Return a canonical retention class or raise ValueError."""

    if value not in RETENTION_CLASSES:
        raise ValueError(f"unknown retention_class: {value}")
    return value


def validate_run_mode(value: str) -> RetentionRunMode:
    """Return a canonical retention run mode or raise ValueError."""

    if value not in RETENTION_RUN_MODES:
        raise ValueError(f"unknown retention run mode: {value}")
    return value


def validate_scope(value: str) -> str:
    """Return a canonical run scope or raise ValueError."""

    if value not in RETENTION_SCOPES:
        raise ValueError(f"unknown retention scope: {value}")
    return value


def validate_decision_outcome(value: str) -> str:
    """Return a canonical retention decision outcome or raise ValueError."""

    if value not in RETENTION_DECISION_OUTCOMES:
        raise ValueError(f"unknown retention decision outcome: {value}")
    return value


def normalize_reason_code(value: str) -> RetentionReasonCode:
    """Return a normalized safe reason code or raise ValueError."""

    normalized = value.strip().lower()
    if not _REASON_CODE_PATTERN.fullmatch(normalized):
        raise ValueError(f"invalid retention reason code: {value}")
    return normalized


def validate_safe_identifier(value: str) -> str:
    """Return a safe identifier for summaries, logs, metrics, and audit records."""

    if not _SAFE_ID_PATTERN.fullmatch(value):
        raise ValueError(f"invalid retention safe identifier: {value}")
    return value


def to_safe_dict(value: Any) -> dict[str, Any]:
    """Serialize dataclasses or mappings after recursively rejecting unsafe keys."""

    serialized = _serialize_safe_value(value)
    if not isinstance(serialized, dict):
        raise TypeError("to_safe_dict requires a dataclass or mapping root")
    validate_safe_field_names(serialized)
    return serialized


def validate_safe_field_names(value: Mapping[str, Any]) -> None:
    """Reject fields that could carry raw contents, object keys, or secrets."""

    _validate_safe_field_names(value)


def _serialize_safe_value(value: Any) -> Any:
    if is_dataclass(value):
        return _serialize_safe_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _serialize_safe_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_serialize_safe_value(item) for item in value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_serialize_safe_value(item) for item in value]
    return value


def _validate_safe_field_names(value: Any, *, parent_key: str | None = None) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if parent_key == "reason_counts":
                normalize_reason_code(key_text)
            else:
                _validate_safe_field_name(key_text)
            _validate_safe_field_names(item, parent_key=key_text)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _validate_safe_field_names(item, parent_key=parent_key)


def _validate_safe_field_name(field_name: str) -> None:
    normalized = field_name.strip().lower().replace("-", "_")
    for unsafe_part in _UNSAFE_FIELD_NAME_PARTS:
        if unsafe_part in normalized:
            raise ValueError(f"unsafe retention summary field: {field_name}")
