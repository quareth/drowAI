"""Code-owned registry for guarded LLM connection operations.

The registry is the only authority that maps provider plus operation IDs to
fixed Phase 1 endpoints. It has no mutation or user-endpoint registration API.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Mapping

from .types import LLMConnectionOperation, RegisteredLLMOperationTarget


class OperationRegistryError(ValueError):
    """Raised when an operation/provider pair is not code-owned and supported."""


@dataclass(frozen=True, slots=True)
class _OperationDefinition:
    """Internal fixed operation method and path template."""

    method: str
    path_template: str
    requires_resource_id: bool = False


_FIXED_PROVIDER_ORIGINS: Mapping[str, str] = MappingProxyType(
    {
        "openai": "https://api.openai.com",
        "anthropic": "https://api.anthropic.com",
    }
)
_OPERATION_DEFINITIONS: Mapping[
    tuple[LLMConnectionOperation, str],
    _OperationDefinition,
] = MappingProxyType(
    {
        (LLMConnectionOperation.HEALTH, "openai"): _OperationDefinition(
            "GET", "/v1/models"
        ),
        (LLMConnectionOperation.HEALTH, "anthropic"): _OperationDefinition(
            "GET", "/v1/models"
        ),
        (LLMConnectionOperation.INVENTORY, "openai"): _OperationDefinition(
            "GET", "/v1/models"
        ),
        (LLMConnectionOperation.INVENTORY, "anthropic"): _OperationDefinition(
            "GET", "/v1/models"
        ),
        (LLMConnectionOperation.CAPABILITY_PROBE, "openai"): _OperationDefinition(
            "POST", "/v1/chat/completions"
        ),
        (
            LLMConnectionOperation.CAPABILITY_PROBE,
            "anthropic",
        ): _OperationDefinition("POST", "/v1/messages"),
        (LLMConnectionOperation.LIFECYCLE_CREATE, "openai"): _OperationDefinition(
            "POST", "/v1/conversations"
        ),
        (LLMConnectionOperation.LIFECYCLE_DELETE, "openai"): _OperationDefinition(
            "DELETE",
            "/v1/conversations/{resource_id}",
            requires_resource_id=True,
        ),
        (LLMConnectionOperation.INFERENCE, "openai"): _OperationDefinition(
            "POST", "/v1/chat/completions"
        ),
        (LLMConnectionOperation.INFERENCE, "anthropic"): _OperationDefinition(
            "POST", "/v1/messages"
        ),
    }
)
_RESOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


class ConnectionOperationRegistry:
    """Resolve only the immutable Phase 1 provider-operation matrix."""

    def list_operation_ids(self) -> tuple[str, ...]:
        """Return the complete sorted code-owned operation vocabulary."""

        return tuple(sorted(operation.value for operation in LLMConnectionOperation))

    def resolve(
        self,
        operation: LLMConnectionOperation | str,
        *,
        provider: str,
        resource_id: str | None = None,
    ) -> RegisteredLLMOperationTarget:
        """Resolve a fixed target without accepting a URL or arbitrary headers."""

        normalized_operation = _normalize_operation(operation)
        normalized_provider = _normalize_provider(provider)
        definition = _OPERATION_DEFINITIONS.get(
            (normalized_operation, normalized_provider)
        )
        if definition is None:
            raise OperationRegistryError("Operation is not registered for provider")

        path = definition.path_template
        if definition.requires_resource_id:
            if not isinstance(resource_id, str) or not _RESOURCE_ID_PATTERN.fullmatch(
                resource_id
            ):
                raise OperationRegistryError("Invalid lifecycle resource identifier")
            path = path.format(resource_id=resource_id)
        elif resource_id is not None:
            raise OperationRegistryError(
                "Operation does not accept a resource identifier"
            )

        origin = _FIXED_PROVIDER_ORIGINS[normalized_provider]
        return RegisteredLLMOperationTarget(
            operation=normalized_operation,
            provider=normalized_provider,
            method=definition.method,
            url=f"{origin}{path}",
            expected_host=origin.removeprefix("https://"),
            allowed_ports=frozenset({443}),
            allowed_path_prefixes=(path,),
        )


def _normalize_operation(
    operation: LLMConnectionOperation | str,
) -> LLMConnectionOperation:
    """Normalize one operation ID or reject it closed."""

    if isinstance(operation, LLMConnectionOperation):
        return operation
    try:
        return LLMConnectionOperation(str(operation).strip().lower())
    except ValueError as exc:
        raise OperationRegistryError("Unknown connection operation") from exc


def _normalize_provider(provider: str) -> str:
    """Normalize one fixed provider ID without accepting custom providers."""

    if not isinstance(provider, str):
        raise OperationRegistryError("Provider must be a string")
    normalized = provider.strip().lower()
    if normalized not in _FIXED_PROVIDER_ORIGINS:
        raise OperationRegistryError("Provider has no fixed Phase 1 target")
    return normalized


__all__ = [
    "ConnectionOperationRegistry",
    "OperationRegistryError",
]
