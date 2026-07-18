"""Runtime-safe LLM provider service contracts.

This module owns small value objects shared by backend LLM provider services.
The contracts are intentionally non-secret and contain no database session,
provider SDK client, or encryption behavior.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from math import isfinite
from typing import Any
from uuid import UUID

from agent.providers.llm.profiles.registry import ModelProfile


@dataclass(frozen=True, slots=True)
class LLMCredentialRef:
    """Serializable lookup pointer for a user/provider credential row."""

    user_id: int
    provider: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""

        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Any) -> "LLMCredentialRef":
        """Build a credential ref from a serialized mapping."""

        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise TypeError("LLMCredentialRef requires a mapping")
        return cls(
            user_id=int(value["user_id"]),
            provider=str(value["provider"]),
        )


class LLMConnectionState(str, Enum):
    """Persisted lifecycle states for a user-owned inference connection."""

    DRAFT = "draft"
    DISABLED = "disabled"
    ENABLED = "enabled"


class LLMAuthMode(str, Enum):
    """Typed authentication modes admitted by connection resolution."""

    NONE = "none"
    API_KEY = "api_key"
    BEARER = "bearer"
    OPERATOR_MANAGED = "operator_managed"


@dataclass(frozen=True, slots=True)
class LLMConnectionCredentialRef:
    """Opaque persisted connection binding for future credential resolution."""

    connection_id: str
    expected_revision: int


@dataclass(frozen=True, slots=True)
class LLMConnectionAccessContext:
    """Trusted authenticated/runtime identity for one live authorization."""

    authenticated_user_id: int
    task_id: int | None = None
    tenant_id: int | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.authenticated_user_id, bool)
            or not isinstance(self.authenticated_user_id, int)
            or self.authenticated_user_id <= 0
        ):
            raise ValueError("authenticated_user_id must be a positive integer")
        if (self.task_id is None) != (self.tenant_id is None):
            raise ValueError("task_id and tenant_id must be supplied together")
        for field_name, value in (
            ("task_id", self.task_id),
            ("tenant_id", self.tenant_id),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"{field_name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class LLMRuntimeSelection:
    """Non-secret runtime provider/model selection for one conversation turn."""

    provider: str
    model: str
    credential_ref: LLMCredentialRef
    reasoning_effort: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""

        return {
            "provider": self.provider,
            "model": self.model,
            "credential_ref": self.credential_ref.to_dict(),
            "reasoning_effort": self.reasoning_effort,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> "LLMRuntimeSelection":
        """Build runtime selection from a serialized mapping or existing value."""

        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise TypeError("LLMRuntimeSelection requires a mapping")
        return cls(
            provider=str(value["provider"]),
            model=str(value["model"]),
            credential_ref=LLMCredentialRef.from_mapping(value["credential_ref"]),
            reasoning_effort=(
                str(value["reasoning_effort"])
                if value.get("reasoning_effort") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class DeploymentRef:
    """Checkpoint-safe deployment identity with optimistic revision."""

    deployment_id: str
    expected_revision: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "deployment_id",
            _canonical_uuid(self.deployment_id, "deployment_id"),
        )
        _positive_revision(self.expected_revision)

    def to_dict(self) -> dict[str, Any]:
        """Return the complete safe serialized deployment reference."""

        return {
            "deployment_id": self.deployment_id,
            "expected_revision": self.expected_revision,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> "DeploymentRef":
        """Parse a deployment reference without accepting extra facts."""

        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise TypeError("DeploymentRef requires a mapping")
        extra = set(value) - {"deployment_id", "expected_revision"}
        if extra:
            raise ValueError("DeploymentRef contains unsupported fields")
        return cls(
            deployment_id=str(value["deployment_id"]),
            expected_revision=value["expected_revision"],
        )


@dataclass(frozen=True, slots=True)
class LLMRuntimeSelectionV2:
    """Checkpoint-safe deployment selection without live infrastructure facts."""

    deployment_ref: DeploymentRef
    preferred_route_id: str | None = None
    reasoning_effort: str | None = None
    legacy_provider: str | None = None
    legacy_model: str | None = None
    schema_version: int = 2

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ValueError("LLM runtime selection schema_version must be 2")
        if not isinstance(self.deployment_ref, DeploymentRef):
            raise TypeError("deployment_ref must be DeploymentRef")
        if self.preferred_route_id is not None:
            object.__setattr__(
                self,
                "preferred_route_id",
                _canonical_uuid(self.preferred_route_id, "preferred_route_id"),
            )
        for field_name in (
            "reasoning_effort",
            "legacy_provider",
            "legacy_model",
        ):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field_name} must be non-empty when supplied")

    def to_dict(self) -> dict[str, Any]:
        """Serialize only checkpoint-safe identity and diagnostic fields."""

        return {
            "schema_version": self.schema_version,
            "deployment_ref": self.deployment_ref.to_dict(),
            "preferred_route_id": self.preferred_route_id,
            "reasoning_effort": self.reasoning_effort,
            "legacy_provider": self.legacy_provider,
            "legacy_model": self.legacy_model,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> "LLMRuntimeSelectionV2":
        """Parse V2 identity while rejecting resolved or unknown fields."""

        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise TypeError("LLMRuntimeSelectionV2 requires a mapping")
        allowed = {
            "schema_version",
            "deployment_ref",
            "preferred_route_id",
            "reasoning_effort",
            "legacy_provider",
            "legacy_model",
        }
        if set(value) - allowed:
            raise ValueError("LLMRuntimeSelectionV2 contains unsupported fields")
        return cls(
            schema_version=value.get("schema_version"),
            deployment_ref=DeploymentRef.from_mapping(value["deployment_ref"]),
            preferred_route_id=value.get("preferred_route_id"),
            reasoning_effort=value.get("reasoning_effort"),
            legacy_provider=value.get("legacy_provider"),
            legacy_model=value.get("legacy_model"),
        )


@dataclass(frozen=True, slots=True)
class LLMRuntimeAccessContext:
    """Trusted live user and optional task/tenant identity for resolution."""

    runtime_user_id: int
    task_id: int | None = None
    tenant_id: int | None = None

    def __post_init__(self) -> None:
        _positive_id(self.runtime_user_id, "runtime_user_id")
        if (self.task_id is None) != (self.tenant_id is None):
            raise ValueError("task_id and tenant_id must be supplied together")
        if self.task_id is not None:
            _positive_id(self.task_id, "task_id")
            _positive_id(self.tenant_id, "tenant_id")


@dataclass(frozen=True, slots=True)
class LLMCallTarget:
    """Provider/model target for a specific LLM call."""

    provider: str
    model: str
    reasoning_effort: str | None = None
    role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class CredentialStatus:
    """Non-secret credential status returned by credential services."""

    user_id: int
    provider: str
    enabled: bool
    has_api_key: bool
    masked_api_key: str | None = None
    connection_id: str | None = None
    auth_mode: LLMAuthMode | None = None


@dataclass(frozen=True, slots=True)
class LLMSelectionStatus:
    """Descriptive status for a saved provider/model selection."""

    status: str
    selectable: bool
    runnable: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProviderSecret:
    """Short-lived decrypted provider secret at an approved boundary."""

    provider: str
    value: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ResolvedAuth:
    """Request-scoped typed auth that is never safe for serialization."""

    mode: LLMAuthMode
    provider: str | None = None
    secret: ProviderSecret | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        try:
            mode = (
                self.mode
                if isinstance(self.mode, LLMAuthMode)
                else LLMAuthMode(str(self.mode))
            )
        except ValueError as exc:
            raise ValueError("Unsupported LLM auth mode") from exc
        object.__setattr__(self, "mode", mode)
        if mode in {LLMAuthMode.NONE, LLMAuthMode.OPERATOR_MANAGED}:
            if self.provider is not None or self.secret is not None:
                raise ValueError(f"{mode.value} auth cannot carry a local secret")
            return
        if not self.provider or not isinstance(self.secret, ProviderSecret):
            raise ValueError(f"{mode.value} auth requires a provider secret")
        if self.provider != self.secret.provider:
            raise ValueError("Resolved auth provider does not match its secret")

    @classmethod
    def none(cls) -> "ResolvedAuth":
        """Return explicit unauthenticated connection auth."""

        return cls(mode=LLMAuthMode.NONE)

    @classmethod
    def operator_managed(cls) -> "ResolvedAuth":
        """Return a future operator-managed auth marker without local material."""

        return cls(mode=LLMAuthMode.OPERATOR_MANAGED)

    @classmethod
    def with_secret(
        cls,
        *,
        mode: LLMAuthMode,
        provider: str,
        secret: ProviderSecret,
    ) -> "ResolvedAuth":
        """Return API-key or bearer auth with short-lived secret material."""

        if mode not in {LLMAuthMode.API_KEY, LLMAuthMode.BEARER}:
            raise ValueError("Secret auth mode must be api_key or bearer")
        return cls(mode=mode, provider=provider, secret=secret)


@dataclass(frozen=True, slots=True)
class ResolvedConnectionTarget:
    """Live authorized connection facts that must never be serialized."""

    connection_id: str
    connection_revision: int
    connection_preset_id: str
    runtime_family_id: str
    serving_operator_id: str | None
    transport_origin: str
    endpoint_policy_id: str
    endpoint: str = field(repr=False)
    resolved_auth: ResolvedAuth = field(repr=False)


@dataclass(frozen=True, slots=True)
class ResolvedLLMTarget:
    """Live deployment target used by factory and budget authorities."""

    connection: ResolvedConnectionTarget
    deployment_id: str
    deployment_revision: int
    route_id: str | None
    adapter_id: str
    adapter_version: str
    api_surface: str
    dialect_policy_id: str
    canonical_model_id: str | None
    exact_wire_model_id: str
    effective_profile: ModelProfile | None = field(repr=False)


def _canonical_uuid(value: Any, field_name: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc


def _positive_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("expected_revision must be a positive integer")
    return value


def _positive_id(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class ProviderHealthCheckResult:
    """Provider-neutral health-check result."""

    provider: str
    status: str
    message: str
    model_count: int | None = None


class LLMConnectionOperation(str, Enum):
    """Code-owned outbound operation IDs admitted by guarded egress."""

    HEALTH = "health"
    INVENTORY = "inventory"
    CAPABILITY_PROBE = "capability_probe"
    LIFECYCLE_CREATE = "lifecycle_create"
    LIFECYCLE_DELETE = "lifecycle_delete"
    INFERENCE = "inference"


@dataclass(frozen=True, slots=True)
class RegisteredLLMOperationTarget:
    """Code-owned provider endpoint selected by the operation registry."""

    operation: LLMConnectionOperation
    provider: str
    method: str
    url: str
    expected_host: str
    allowed_ports: frozenset[int]
    allowed_path_prefixes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AuthorizedLLMConnectionOperation:
    """Live non-secret authorization result for one registered operation."""

    connection_id: str
    connection_revision: int
    operation_target: RegisteredLLMOperationTarget


@dataclass(frozen=True, slots=True)
class ValidatedEgressTarget:
    """Endpoint facts validated immediately before one guarded request."""

    url: str
    scheme: str
    host: str
    port: int
    path: str
    resolved_addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GuardedEgressTimeouts:
    """Bounded connect, read/idle, and total guarded request durations."""

    connect_seconds: float = 5.0
    read_seconds: float = 15.0
    total_seconds: float = 30.0

    def __post_init__(self) -> None:
        values = (
            self.connect_seconds,
            self.read_seconds,
            self.total_seconds,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or value <= 0
            for value in values
        ):
            raise ValueError("guarded egress timeouts must be positive")
        if self.connect_seconds > self.total_seconds:
            raise ValueError("connect timeout cannot exceed total timeout")
        if self.read_seconds > self.total_seconds:
            raise ValueError("read timeout cannot exceed total timeout")


@dataclass(frozen=True, slots=True)
class GuardedEgressBounds:
    """Request, response, header, inventory, and decompression limits."""

    max_request_bytes: int = 2 * 1024 * 1024
    max_response_bytes: int = 4 * 1024 * 1024
    max_header_bytes: int = 32 * 1024
    max_inventory_items: int = 1_000
    read_chunk_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        values = (
            self.max_request_bytes,
            self.max_response_bytes,
            self.max_header_bytes,
            self.max_inventory_items,
            self.read_chunk_bytes,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in values
        ):
            raise ValueError("guarded egress bounds must be positive integers")


@dataclass(frozen=True, slots=True)
class GuardedHTTPResponse:
    """Bounded response returned without upstream headers or endpoint details."""

    status_code: int
    body: bytes
    audit_id: str


class LLMProviderServiceError(Exception):
    """Base class for backend LLM provider service errors."""


class ProviderConfigurationError(LLMProviderServiceError):
    """Raised when provider/model configuration is invalid or incomplete."""


class CredentialNotFoundError(ProviderConfigurationError):
    """Raised when a usable provider credential is missing."""


class CredentialAuthorizationError(LLMProviderServiceError):
    """Raised when a runtime context cannot use a credential reference."""


class CredentialEncryptionError(LLMProviderServiceError):
    """Raised when credential encryption or decryption fails."""


class LLMConnectionValidationError(LLMProviderServiceError):
    """Raised when connection configuration is invalid."""


class LLMConnectionNotFoundError(LLMProviderServiceError):
    """Raised when a connection is absent or not owned by the caller."""


class LLMConnectionRevisionConflictError(LLMProviderServiceError):
    """Raised when a connection mutation uses a stale expected revision."""


class LLMConnectionStateTransitionError(LLMProviderServiceError):
    """Raised when a requested connection lifecycle transition is invalid."""


class LLMDeploymentNotFoundError(LLMProviderServiceError):
    """Raised when a deployment or route is absent from the caller's scope."""


class LLMDeploymentValidationError(LLMProviderServiceError):
    """Raised when deployment configuration is invalid."""


class LLMConnectionAuthorizationError(LLMProviderServiceError):
    """Sanitized fail-closed result from live connection authorization."""

    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


__all__ = [
    "AuthorizedLLMConnectionOperation",
    "CredentialAuthorizationError",
    "CredentialEncryptionError",
    "CredentialNotFoundError",
    "CredentialStatus",
    "DeploymentRef",
    "GuardedEgressBounds",
    "GuardedEgressTimeouts",
    "GuardedHTTPResponse",
    "LLMCallTarget",
    "LLMAuthMode",
    "LLMConnectionAccessContext",
    "LLMConnectionAuthorizationError",
    "LLMConnectionCredentialRef",
    "LLMConnectionNotFoundError",
    "LLMConnectionOperation",
    "LLMConnectionRevisionConflictError",
    "LLMConnectionState",
    "LLMConnectionStateTransitionError",
    "LLMConnectionValidationError",
    "LLMCredentialRef",
    "LLMDeploymentNotFoundError",
    "LLMDeploymentValidationError",
    "LLMProviderServiceError",
    "LLMRuntimeSelection",
    "LLMRuntimeAccessContext",
    "LLMRuntimeSelectionV2",
    "LLMSelectionStatus",
    "ProviderConfigurationError",
    "ProviderHealthCheckResult",
    "ProviderSecret",
    "ResolvedAuth",
    "ResolvedConnectionTarget",
    "ResolvedLLMTarget",
    "RegisteredLLMOperationTarget",
    "ValidatedEgressTarget",
]
