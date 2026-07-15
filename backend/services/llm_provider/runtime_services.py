"""Non-checkpointed runtime dependency bag helpers for LangGraph execution.

Runtime service objects such as live client resolvers are safe only at
invocation time. This module attaches them to local config copies and strips
them before checkpoint/state inspection or diagnostic serialization.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, MutableMapping

from .runtime_client_resolver import LLMRuntimeClientResolver

RUNTIME_SERVICES_CONFIG_KEY = "runtime_services"
RUNTIME_SELECTION_CONFIG_KEY = "llm_runtime_selection"


@dataclass(frozen=True, slots=True)
class LLMRuntimeServices:
    """Live, non-serializable dependencies for one graph invocation."""

    client_resolver: LLMRuntimeClientResolver
    memory_runtime_service: Any | None = None


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


__all__ = [
    "LLMRuntimeServices",
    "RUNTIME_SELECTION_CONFIG_KEY",
    "RUNTIME_SERVICES_CONFIG_KEY",
    "attach_runtime_services",
    "get_runtime_services",
    "strip_runtime_services",
]
