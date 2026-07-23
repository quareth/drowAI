"""Code-owned registry for guarded LLM connection operations.

The registry is the only authority that maps provider or preset operation IDs
to fixed endpoints or policy-validated preset endpoints. It has no mutation,
custom header, fallback, or dynamic adapter registration API.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
import re
from types import MappingProxyType
from typing import Iterable, Mapping

from agent.providers.llm.adapters.openai.compatible_dialects import (
    OPENAI_COMPATIBLE_CHAT_ADAPTER_ID,
)
from backend.services.llm_provider import (
    _connection_target_resolution as _target_resolution,
)
from backend.services.llm_provider._connection_operation_contracts import (
    OperationRegistryError,
)
from backend.services.llm_provider._connection_preset_catalog import (
    ANTHROPIC_BASE_URL_ENV,
    CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
    FIXED_PROVIDER_ENDPOINT_POLICY_ID,
    GPT_OSS_20B_PROVING_API_KEY_ENV,
    GPT_OSS_20B_PROVING_BASE_URL_ENV,
    GPT_OSS_20B_PROVING_E2E_ENV,
    GPT_OSS_20B_PROVING_PRESET_ID,
    HUGGINGFACE_BASE_URL_ENV,
    HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    MISTRAL_BASE_URL_ENV,
    MISTRAL_OPENAI_COMPATIBLE_PRESET_ID,
    NVIDIA_NIM_BASE_URL_ENV,
    NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
    OPENAI_BASE_URL_ENV,
    PUBLIC_GPT_OSS_20B_PRESET_IDS,
    PUBLIC_REVIEWED_MODEL_PRESET_IDS,
    ProvingConnectionPreset,
    USER_HTTPS_BASE_URL_ENDPOINT_POLICY_ID,
    VLLM_OPENAI_COMPATIBLE_PRESET_ID,
    _CONNECTION_PRESETS,
    _NATIVE_PROVIDER_ENDPOINTS,
    _PROVING_PRESETS,
)
from .types import (
    LLMConnectionOperation,
    RegisteredLLMOperationTarget,
)


@dataclass(frozen=True, slots=True)
class _OperationDefinition:
    """Internal fixed operation method and path template."""

    method: str
    path_template: str
    requires_resource_id: bool = False


_FIXED_OPERATION_DEFINITIONS: Mapping[
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
_OPENAI_COMPATIBLE_PRESET_OPERATIONS: Mapping[
    LLMConnectionOperation,
    _OperationDefinition,
] = MappingProxyType(
    {
        LLMConnectionOperation.HEALTH: _OperationDefinition("GET", "/v1/models"),
        LLMConnectionOperation.INVENTORY: _OperationDefinition("GET", "/v1/models"),
        LLMConnectionOperation.CAPABILITY_PROBE: _OperationDefinition(
            "POST", "/v1/chat/completions"
        ),
        LLMConnectionOperation.INFERENCE: _OperationDefinition(
            "POST", "/v1/chat/completions"
        ),
    }
)


def _operation_definitions_from_presets(
    presets: Iterable[ProvingConnectionPreset],
) -> dict[tuple[LLMConnectionOperation, str], _OperationDefinition]:
    """Build guarded operation rows for validated compatible preset data."""

    definitions: dict[tuple[LLMConnectionOperation, str], _OperationDefinition] = {}
    for preset in presets:
        if preset.adapter_id != OPENAI_COMPATIBLE_CHAT_ADAPTER_ID:
            raise OperationRegistryError("Connection preset adapter has no operation matrix")
        for operation, definition in _OPENAI_COMPATIBLE_PRESET_OPERATIONS.items():
            definitions[(operation, preset.id)] = definition
    return definitions


_OPERATION_DEFINITIONS: Mapping[
    tuple[LLMConnectionOperation, str],
    _OperationDefinition,
] = MappingProxyType(
    {
        **_FIXED_OPERATION_DEFINITIONS,
        **_operation_definitions_from_presets(_CONNECTION_PRESETS.values()),
    }
)
_RESOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
EnvGetter = Callable[[str], str | None]


class ConnectionOperationRegistry:
    """Resolve only the immutable provider/preset operation matrix."""

    def __init__(self, *, env_getter: EnvGetter | None = None) -> None:
        self._env_getter = env_getter or os.environ.get

    def list_operation_ids(self) -> tuple[str, ...]:
        """Return the complete sorted code-owned operation vocabulary."""

        return tuple(sorted(operation.value for operation in LLMConnectionOperation))

    def list_proving_preset_ids(self) -> tuple[str, ...]:
        """Return the complete sorted Phase 4 proving preset vocabulary."""

        return tuple(sorted(_PROVING_PRESETS))

    def list_connection_preset_ids(self) -> tuple[str, ...]:
        """Return the complete sorted reviewed connection preset vocabulary."""

        return tuple(sorted(_CONNECTION_PRESETS))

    def list_public_gpt_oss_20b_preset_ids(self) -> tuple[str, ...]:
        """Return intentionally product-supported GPT-OSS serving presets."""

        return tuple(
            preset_id
            for preset_id in PUBLIC_GPT_OSS_20B_PRESET_IDS
            if preset_id in _CONNECTION_PRESETS
        )

    def list_public_reviewed_model_preset_ids(self) -> tuple[str, ...]:
        """Return intentionally product-supported reviewed model presets."""

        return tuple(
            preset_id
            for preset_id in PUBLIC_REVIEWED_MODEL_PRESET_IDS
            if preset_id in _CONNECTION_PRESETS
        )

    def get_connection_preset(self, preset_id: str) -> ProvingConnectionPreset:
        """Return one reviewed connection preset or reject unknown IDs."""

        normalized = _normalize_connection_preset(preset_id)
        try:
            return _CONNECTION_PRESETS[normalized]
        except KeyError as exc:
            raise OperationRegistryError("Unknown connection preset") from exc

    def get_proving_preset(self, preset_id: str) -> ProvingConnectionPreset:
        """Return one code-owned proving preset or reject unknown IDs."""

        normalized = _normalize_connection_preset(preset_id)
        try:
            return _PROVING_PRESETS[normalized]
        except KeyError as exc:
            raise OperationRegistryError("Unknown proving preset") from exc

    def validate_preset_base_url(self, preset_id: str, base_url: str | None) -> str:
        """Validate and normalize a preset's configurable endpoint base URL."""

        preset = self.get_connection_preset(preset_id)
        if preset.endpoint_config_field is None:
            raise OperationRegistryError("Preset does not accept a user endpoint")
        return _target_resolution._validated_https_base_url(
            base_url,
            "Preset endpoint base URL",
        )

    def resolve(
        self,
        operation: LLMConnectionOperation | str,
        *,
        provider: str,
        base_url: str | None = None,
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

        native_endpoint = _NATIVE_PROVIDER_ENDPOINTS.get(normalized_provider)
        if native_endpoint is not None:
            origin_inputs = _target_resolution._NativeEndpointOriginInputs(
                default_base_url=native_endpoint.default_base_url,
                base_url_env=native_endpoint.base_url_env,
                client_base_path=native_endpoint.client_base_path,
            )
        else:
            preset = _CONNECTION_PRESETS.get(normalized_provider)
            if preset is None:
                raise OperationRegistryError("Provider has no fixed target")
            origin_inputs = _target_resolution._PresetOriginInputs(
                fixed_base_url=preset.fixed_base_url,
                base_url_env=preset.base_url_env,
                endpoint_config_field=preset.endpoint_config_field,
                client_base_path=preset.client_base_path,
            )

        return _target_resolution._resolve_connection_operation_target(
            normalized_operation,
            normalized_provider,
            method=definition.method,
            operation_path=path,
            origin_inputs=origin_inputs,
            env_getter=self._env_getter,
            base_url=base_url,
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
    """Normalize one registered provider or preset ID."""

    if not isinstance(provider, str):
        raise OperationRegistryError("Provider must be a string")
    normalized = provider.strip().lower()
    if (
        normalized not in _NATIVE_PROVIDER_ENDPOINTS
        and normalized not in _CONNECTION_PRESETS
    ):
        raise OperationRegistryError("Provider or preset has no registered operation target")
    return normalized


def _normalize_connection_preset(preset_id: str) -> str:
    """Normalize one connection preset ID."""

    if not isinstance(preset_id, str):
        raise OperationRegistryError("Connection preset must be a string")
    normalized = preset_id.strip().lower()
    if normalized not in _CONNECTION_PRESETS:
        raise OperationRegistryError("Unknown connection preset")
    return normalized


__all__ = [
    "ANTHROPIC_BASE_URL_ENV",
    "CUSTOM_OPENAI_COMPATIBLE_PRESET_ID",
    "ConnectionOperationRegistry",
    "FIXED_PROVIDER_ENDPOINT_POLICY_ID",
    "GPT_OSS_20B_PROVING_API_KEY_ENV",
    "GPT_OSS_20B_PROVING_BASE_URL_ENV",
    "GPT_OSS_20B_PROVING_E2E_ENV",
    "GPT_OSS_20B_PROVING_PRESET_ID",
    "HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID",
    "HUGGINGFACE_BASE_URL_ENV",
    "MISTRAL_BASE_URL_ENV",
    "MISTRAL_OPENAI_COMPATIBLE_PRESET_ID",
    "NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID",
    "NVIDIA_NIM_BASE_URL_ENV",
    "OPENAI_BASE_URL_ENV",
    "OLLAMA_OPENAI_COMPATIBLE_PRESET_ID",
    "OperationRegistryError",
    "ProvingConnectionPreset",
    "PUBLIC_REVIEWED_MODEL_PRESET_IDS",
    "USER_HTTPS_BASE_URL_ENDPOINT_POLICY_ID",
    "VLLM_OPENAI_COMPATIBLE_PRESET_ID",
]
