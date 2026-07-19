"""Code-owned registry for guarded LLM connection operations.

The registry is the only authority that maps provider plus operation IDs to
fixed Phase 1 endpoints. It has no mutation or user-endpoint registration API.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
import re
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

from agent.providers.llm.adapters.openai.compatible_chat import (
    CONSERVATIVE_OPENAI_COMPATIBLE_DIALECT,
    OPENAI_COMPATIBLE_CHAT_ADAPTER_ID,
    OPENAI_COMPATIBLE_CHAT_ADAPTER_VERSION,
)
from .types import LLMConnectionOperation, RegisteredLLMOperationTarget

GPT_OSS_20B_PROVING_PRESET_ID = "gpt_oss_20b_openai_compatible_proving"
GPT_OSS_20B_PROVING_E2E_ENV = "DROWAI_GPT_OSS_20B_PROVING_E2E"
GPT_OSS_20B_PROVING_BASE_URL_ENV = "DROWAI_GPT_OSS_20B_PROVING_BASE_URL"
GPT_OSS_20B_PROVING_API_KEY_ENV = "DROWAI_GPT_OSS_20B_PROVING_API_KEY"


class OperationRegistryError(ValueError):
    """Raised when an operation/provider pair is not code-owned and supported."""


@dataclass(frozen=True, slots=True)
class _OperationDefinition:
    """Internal fixed operation method and path template."""

    method: str
    path_template: str
    requires_resource_id: bool = False


@dataclass(frozen=True, slots=True)
class ProvingConnectionPreset:
    """Code-owned proving preset metadata for fixed Phase 4 LLM endpoints."""

    id: str
    display_name: str
    canonical_model_id: str
    exact_wire_model_id: str
    runtime_family_id: str
    serving_operator_id: str
    adapter_id: str
    adapter_version: str
    api_surface: str
    dialect_policy_id: str
    auth_mode: str
    secret_fields: tuple[str, ...]
    user_config_fields: tuple[str, ...]
    base_url_env: str
    e2e_enabled_env: str
    e2e_api_key_env: str


_FIXED_PROVIDER_ORIGINS: Mapping[str, str] = MappingProxyType(
    {
        "openai": "https://api.openai.com",
        "anthropic": "https://api.anthropic.com",
    }
)
_GPT_OSS_20B_PROVING_PRESET = ProvingConnectionPreset(
    id=GPT_OSS_20B_PROVING_PRESET_ID,
    display_name="GPT-OSS 20B OpenAI-compatible proving",
    canonical_model_id="openai/gpt-oss-20b",
    exact_wire_model_id="openai/gpt-oss-20b",
    runtime_family_id="openai_compatible_chat",
    serving_operator_id="openai_compatible_proving",
    adapter_id=OPENAI_COMPATIBLE_CHAT_ADAPTER_ID,
    adapter_version=OPENAI_COMPATIBLE_CHAT_ADAPTER_VERSION,
    api_surface=CONSERVATIVE_OPENAI_COMPATIBLE_DIALECT.api_surface,
    dialect_policy_id=CONSERVATIVE_OPENAI_COMPATIBLE_DIALECT.policy_id,
    auth_mode="bearer_api_key",
    secret_fields=("api_key",),
    user_config_fields=("display_label", "api_key"),
    base_url_env=GPT_OSS_20B_PROVING_BASE_URL_ENV,
    e2e_enabled_env=GPT_OSS_20B_PROVING_E2E_ENV,
    e2e_api_key_env=GPT_OSS_20B_PROVING_API_KEY_ENV,
)
_PROVING_PRESETS: Mapping[str, ProvingConnectionPreset] = MappingProxyType(
    {_GPT_OSS_20B_PROVING_PRESET.id: _GPT_OSS_20B_PROVING_PRESET}
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
        (LLMConnectionOperation.HEALTH, GPT_OSS_20B_PROVING_PRESET_ID): _OperationDefinition(
            "GET", "/v1/models"
        ),
        (
            LLMConnectionOperation.INVENTORY,
            GPT_OSS_20B_PROVING_PRESET_ID,
        ): _OperationDefinition("GET", "/v1/models"),
        (
            LLMConnectionOperation.CAPABILITY_PROBE,
            GPT_OSS_20B_PROVING_PRESET_ID,
        ): _OperationDefinition("POST", "/v1/chat/completions"),
        (
            LLMConnectionOperation.INFERENCE,
            GPT_OSS_20B_PROVING_PRESET_ID,
        ): _OperationDefinition("POST", "/v1/chat/completions"),
    }
)
_RESOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
EnvGetter = Callable[[str], str | None]


class ConnectionOperationRegistry:
    """Resolve only the immutable Phase 1 provider-operation matrix."""

    def __init__(self, *, env_getter: EnvGetter | None = None) -> None:
        self._env_getter = env_getter or os.environ.get

    def list_operation_ids(self) -> tuple[str, ...]:
        """Return the complete sorted code-owned operation vocabulary."""

        return tuple(sorted(operation.value for operation in LLMConnectionOperation))

    def list_proving_preset_ids(self) -> tuple[str, ...]:
        """Return the complete sorted Phase 4 proving preset vocabulary."""

        return tuple(sorted(_PROVING_PRESETS))

    def get_proving_preset(self, preset_id: str) -> ProvingConnectionPreset:
        """Return one code-owned proving preset or reject unknown IDs."""

        normalized = _normalize_provider(preset_id)
        try:
            return _PROVING_PRESETS[normalized]
        except KeyError as exc:
            raise OperationRegistryError("Unknown proving preset") from exc

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

        origin = self._origin_for(normalized_provider)
        url = _join_origin_path(origin, path)
        parsed = urlsplit(url)
        return RegisteredLLMOperationTarget(
            operation=normalized_operation,
            provider=normalized_provider,
            method=definition.method,
            url=url,
            expected_host=str(parsed.hostname or ""),
            allowed_ports=frozenset({443}),
            allowed_path_prefixes=(parsed.path,),
        )

    def _origin_for(self, provider: str) -> str:
        fixed_origin = _FIXED_PROVIDER_ORIGINS.get(provider)
        if fixed_origin is not None:
            return fixed_origin
        preset = _PROVING_PRESETS.get(provider)
        if preset is None:
            raise OperationRegistryError("Provider has no fixed target")
        return _validated_proving_base_url(self._env_getter(preset.base_url_env))


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
    if normalized not in _FIXED_PROVIDER_ORIGINS and normalized not in _PROVING_PRESETS:
        raise OperationRegistryError("Provider has no fixed Phase 1 target")
    return normalized


def _validated_proving_base_url(value: str | None) -> str:
    """Validate a code-owned proving base URL from environment configuration."""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise OperationRegistryError("Proving endpoint base URL is not configured")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise OperationRegistryError("Proving endpoint base URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise OperationRegistryError("Proving endpoint base URL violates policy")
    path = parsed.path.rstrip("/")
    if path in {"", "/"}:
        path = ""
    elif not _valid_base_path(path):
        raise OperationRegistryError("Proving endpoint base path violates policy")
    return urlunsplit(("https", parsed.netloc, path, "", ""))


def _valid_base_path(path: str) -> bool:
    if not path.startswith("/") or "\\" in path or "//" in path:
        return False
    if re.search(r"%(?:2e|2f|5c)", path, flags=re.IGNORECASE):
        return False
    return not any(segment in {".", ".."} for segment in path.split("/"))


def _join_origin_path(origin: str, path: str) -> str:
    return f"{origin.rstrip('/')}{path}"


__all__ = [
    "ConnectionOperationRegistry",
    "GPT_OSS_20B_PROVING_API_KEY_ENV",
    "GPT_OSS_20B_PROVING_BASE_URL_ENV",
    "GPT_OSS_20B_PROVING_E2E_ENV",
    "GPT_OSS_20B_PROVING_PRESET_ID",
    "OperationRegistryError",
    "ProvingConnectionPreset",
]
