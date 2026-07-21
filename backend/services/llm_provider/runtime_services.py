"""Non-checkpointed runtime dependency bag helpers for LangGraph execution.

Runtime service objects such as live client resolvers are safe only at
invocation time. This module attaches them to local config copies and strips
them before checkpoint/state inspection or diagnostic serialization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping, MutableMapping

if TYPE_CHECKING:
    from .runtime_client_resolver import LLMRuntimeClientResolver

RUNTIME_SERVICES_CONFIG_KEY = "runtime_services"
RUNTIME_SELECTION_CONFIG_KEY = "llm_runtime_selection"
LLM_INVENTORY_REFRESH_SERVICE_ACTOR = "llm_inventory_refresh"
TRUSTED_LLM_SERVICE_ACTORS = frozenset({LLM_INVENTORY_REFRESH_SERVICE_ACTOR})

_JOB_AUTHORIZATION_FIELD_NAMES = frozenset(
    {
        "authenticated_user_id",
        "connection_user_id",
        "owner_id",
        "runtime_user_id",
        "tenant_id",
        "user_id",
    }
)


@dataclass(frozen=True, slots=True)
class LLMRuntimeServices:
    """Live, non-serializable dependencies for one graph invocation."""

    client_resolver: LLMRuntimeClientResolver
    memory_runtime_service: Any | None = None


@dataclass(frozen=True, slots=True)
class LLMServiceOperationContext:
    """Trusted scheduler/service identity for bounded background LLM operations."""

    service_actor: str
    job_id: str
    correlation_id: str | None = None
    correlation_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        actor = _required_service_text(self.service_actor, "service_actor")
        if actor not in TRUSTED_LLM_SERVICE_ACTORS:
            raise ValueError("service_actor is not trusted for LLM service operations")
        job_id = _required_service_text(self.job_id, "job_id")
        correlation_id = (
            _required_service_text(self.correlation_id, "correlation_id")
            if self.correlation_id is not None
            else None
        )
        if not isinstance(self.correlation_metadata, Mapping):
            raise ValueError("correlation_metadata must be a mapping")
        metadata = dict(self.correlation_metadata)
        unsupported = _JOB_AUTHORIZATION_FIELD_NAMES.intersection(metadata)
        if unsupported:
            raise ValueError("job payload contains unsupported authorization fields")
        for key in metadata:
            if not isinstance(key, str) or not key.strip():
                raise ValueError("correlation_metadata keys must be non-empty strings")
        object.__setattr__(self, "service_actor", actor)
        object.__setattr__(self, "job_id", job_id)
        object.__setattr__(self, "correlation_id", correlation_id)
        object.__setattr__(
            self,
            "correlation_metadata",
            MappingProxyType(metadata),
        )

    @classmethod
    def from_job_payload(
        cls,
        *,
        service_actor: str,
        job_id: str,
        correlation_id: str | None = None,
        correlation_metadata: Mapping[str, Any] | None = None,
    ) -> "LLMServiceOperationContext":
        """Build service context from scheduler metadata without auth facts."""

        return cls(
            service_actor=service_actor,
            job_id=job_id,
            correlation_id=correlation_id,
            correlation_metadata=correlation_metadata or {},
        )


def attach_runtime_services(
    config: Mapping[str, Any] | None,
    runtime_services: LLMRuntimeServices,
) -> dict[str, Any]:
    """Return a config copy with runtime services attached."""

    copied = _copy_config(config)
    configurable = dict(copied.get("configurable") or {})
    configurable[RUNTIME_SERVICES_CONFIG_KEY] = runtime_services
    copied["configurable"] = configurable
    return copied


def strip_runtime_services(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a config copy with non-checkpointed runtime service objects removed."""

    copied = _copy_config(config)
    configurable = dict(copied.get("configurable") or {})
    configurable.pop(RUNTIME_SERVICES_CONFIG_KEY, None)
    copied["configurable"] = configurable
    return copied


def get_runtime_services(config: Mapping[str, Any] | None) -> LLMRuntimeServices | None:
    """Return attached runtime services if present."""

    if not config:
        return None
    configurable = config.get("configurable") if isinstance(config, Mapping) else None
    if not isinstance(configurable, Mapping):
        return None
    services = configurable.get(RUNTIME_SERVICES_CONFIG_KEY)
    return services if isinstance(services, LLMRuntimeServices) else None


def _copy_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    copied: dict[str, Any] = dict(config or {})
    if isinstance(copied.get("metadata"), MutableMapping):
        copied["metadata"] = dict(copied["metadata"])
    if isinstance(copied.get("configurable"), MutableMapping):
        copied["configurable"] = dict(copied["configurable"])
    return copied


def _required_service_text(value: str | None, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


__all__ = [
    "LLM_INVENTORY_REFRESH_SERVICE_ACTOR",
    "LLMRuntimeServices",
    "LLMServiceOperationContext",
    "RUNTIME_SELECTION_CONFIG_KEY",
    "RUNTIME_SERVICES_CONFIG_KEY",
    "TRUSTED_LLM_SERVICE_ACTORS",
    "attach_runtime_services",
    "get_runtime_services",
    "strip_runtime_services",
]
