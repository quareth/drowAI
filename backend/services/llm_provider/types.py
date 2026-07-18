"""Runtime-safe LLM provider service contracts.

This module owns small value objects shared by backend LLM provider services.
The contracts are intentionally non-secret and contain no database session,
provider SDK client, or encryption behavior.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from math import isfinite
from typing import Any


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
    value: str


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


__all__ = [
    "CredentialAuthorizationError",
    "CredentialEncryptionError",
    "CredentialNotFoundError",
    "CredentialStatus",
    "GuardedEgressBounds",
    "GuardedEgressTimeouts",
    "GuardedHTTPResponse",
    "LLMCallTarget",
    "LLMConnectionOperation",
    "LLMCredentialRef",
    "LLMProviderServiceError",
    "LLMRuntimeSelection",
    "LLMSelectionStatus",
    "ProviderConfigurationError",
    "ProviderHealthCheckResult",
    "ProviderSecret",
    "RegisteredLLMOperationTarget",
    "ValidatedEgressTarget",
]
