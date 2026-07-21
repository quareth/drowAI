"""Import-time catalog for reviewed LLM connection presets.

This module owns checked-in manifest constants, DTOs, loading, validation, and
immutable catalog mappings only; it does not read runtime environment values,
own operation matrices, import the facade, or compose registered targets.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit

from agent.providers.llm.adapters.openai.compatible_dialects import (
    OPENAI_COMPATIBLE_CHAT_ADAPTER_ID,
    OPENAI_COMPATIBLE_CHAT_ADAPTER_VERSION,
    resolve_openai_compatible_dialect,
)
from agent.providers.llm.core.capabilities import LLMCapability, freeze_capabilities
from agent.providers.llm.core.exceptions import LLMConfigurationError

from backend.services.llm_provider._connection_operation_contracts import (
    OperationRegistryError,
    _valid_base_path,
)

GPT_OSS_20B_PROVING_PRESET_ID = "gpt_oss_20b_openai_compatible_proving"
HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID = "huggingface_openai_compatible_chat"
NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID = "nvidia_nim_openai_compatible_chat"
OLLAMA_OPENAI_COMPATIBLE_PRESET_ID = "ollama_openai_compatible_chat"
VLLM_OPENAI_COMPATIBLE_PRESET_ID = "vllm_openai_compatible_chat"
CUSTOM_OPENAI_COMPATIBLE_PRESET_ID = "custom_openai_compatible_chat"
PUBLIC_GPT_OSS_20B_PRESET_IDS = (
    NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
    VLLM_OPENAI_COMPATIBLE_PRESET_ID,
)
GPT_OSS_20B_PROVING_E2E_ENV = "DROWAI_GPT_OSS_20B_PROVING_E2E"
GPT_OSS_20B_PROVING_BASE_URL_ENV = "DROWAI_GPT_OSS_20B_PROVING_BASE_URL"
GPT_OSS_20B_PROVING_API_KEY_ENV = "DROWAI_GPT_OSS_20B_PROVING_API_KEY"
OPENAI_BASE_URL_ENV = "OPENAI_BASE_URL"
ANTHROPIC_BASE_URL_ENV = "ANTHROPIC_BASE_URL"
HUGGINGFACE_BASE_URL_ENV = "DROWAI_HUGGINGFACE_BASE_URL"
NVIDIA_NIM_BASE_URL_ENV = "DROWAI_NVIDIA_NIM_BASE_URL"
FIXED_PROVIDER_ENDPOINT_POLICY_ID = "fixed_provider_v1"
USER_HTTPS_BASE_URL_ENDPOINT_POLICY_ID = "user_https_base_url_v1"
DEFAULT_CONNECTION_PRESET_MANIFEST_PATH = Path(__file__).with_name(
    "connection_presets_manifest.json"
)
_SUPPORTED_PRESET_SCHEMA_VERSION = 2
_ALLOWED_PRESET_AUTH_MODES = frozenset({"bearer_api_key"})
_ALLOWED_PRESET_DISCOVERY_STRATEGIES = frozenset({"openai_models_endpoint"})
_ALLOWED_PRESET_ENDPOINT_POLICIES = frozenset(
    {FIXED_PROVIDER_ENDPOINT_POLICY_ID, USER_HTTPS_BASE_URL_ENDPOINT_POLICY_ID}
)


@dataclass(frozen=True, slots=True)
class _NativeProviderEndpoint:
    """Declarative default and operator override source for a native provider."""

    provider_id: str
    default_base_url: str
    base_url_env: str
    client_base_path: str


@dataclass(frozen=True, slots=True)
class _ConnectionPresetManifest:
    """Validated immutable endpoint and preset data loaded from one manifest."""

    native_provider_endpoints: tuple[_NativeProviderEndpoint, ...]
    presets: tuple["ProvingConnectionPreset", ...]


@dataclass(frozen=True, slots=True)
class ProvingConnectionPreset:
    """Code-owned preset metadata for reviewed LLM endpoint families."""

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
    capability_ceiling: frozenset[LLMCapability]
    endpoint_policy_id: str
    discovery_strategy: str
    auth_mode: str
    secret_fields: tuple[str, ...]
    user_config_fields: tuple[str, ...]
    fixed_base_url: str | None
    endpoint_config_field: str | None
    client_base_path: str
    billing_provider_id: str | None
    base_url_env: str | None = None
    e2e_enabled_env: str | None = None
    e2e_api_key_env: str | None = None
    is_proving: bool = False

    @property
    def auth_schema(self) -> Mapping[str, object]:
        """Return the reviewed non-secret auth schema descriptor."""

        return MappingProxyType(
            {
                "mode": self.auth_mode,
                "secret_fields": self.secret_fields,
            }
        )


def _load_connection_presets_manifest(path: Path) -> _ConnectionPresetManifest:
    """Load and validate reviewed connection presets from checked-in data."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise OperationRegistryError(
            f"Unable to read connection preset manifest '{path}'"
        ) from exc
    except json.JSONDecodeError as exc:
        raise OperationRegistryError(
            f"Connection preset manifest '{path}' is not valid JSON"
        ) from exc
    if not isinstance(payload, Mapping):
        raise OperationRegistryError("Connection preset manifest must be a JSON object")
    if payload.get("schema_version") != _SUPPORTED_PRESET_SCHEMA_VERSION:
        raise OperationRegistryError("Unsupported connection preset manifest schema")
    native_endpoints_payload = payload.get("native_provider_endpoints")
    if not isinstance(native_endpoints_payload, list) or not native_endpoints_payload:
        raise OperationRegistryError(
            "Connection preset manifest requires native provider endpoints"
        )
    presets_payload = payload.get("presets")
    if not isinstance(presets_payload, list) or not presets_payload:
        raise OperationRegistryError("Connection preset manifest requires presets")

    native_endpoints = tuple(
        _native_provider_endpoint_from_payload(item)
        for item in native_endpoints_payload
    )
    native_provider_ids = [endpoint.provider_id for endpoint in native_endpoints]
    if len(set(native_provider_ids)) != len(native_provider_ids):
        raise OperationRegistryError(
            "Connection preset manifest contains duplicate native providers"
        )
    if set(native_provider_ids) != {"openai", "anthropic"}:
        raise OperationRegistryError(
            "Connection preset manifest must define reviewed native providers"
        )

    presets = tuple(_connection_preset_from_payload(item) for item in presets_payload)
    preset_ids = [preset.id for preset in presets]
    if len(set(preset_ids)) != len(preset_ids):
        raise OperationRegistryError("Connection preset manifest contains duplicate IDs")
    proving_ids = {preset.id for preset in presets if preset.is_proving}
    if proving_ids != {GPT_OSS_20B_PROVING_PRESET_ID}:
        raise OperationRegistryError(
            "Connection preset manifest must define one proving preset"
        )
    required_ids = {
        GPT_OSS_20B_PROVING_PRESET_ID,
        HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
        NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
        VLLM_OPENAI_COMPATIBLE_PRESET_ID,
        CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
    }
    if not required_ids.issubset(set(preset_ids)):
        raise OperationRegistryError("Connection preset manifest is missing reviewed presets")
    return _ConnectionPresetManifest(
        native_provider_endpoints=native_endpoints,
        presets=presets,
    )


def _native_provider_endpoint_from_payload(payload: Any) -> _NativeProviderEndpoint:
    """Build one reviewed native provider endpoint from manifest data."""

    if not isinstance(payload, Mapping):
        raise OperationRegistryError("Native provider endpoint must be a JSON object")
    default_base_url = _manifest_text(payload, "default_base_url")
    _validate_manifest_https_base_url(default_base_url)
    return _NativeProviderEndpoint(
        provider_id=_manifest_text(payload, "provider_id"),
        default_base_url=default_base_url,
        base_url_env=_manifest_text(payload, "base_url_env"),
        client_base_path=_manifest_base_path(payload, "client_base_path"),
    )


def _connection_preset_from_payload(payload: Any) -> ProvingConnectionPreset:
    """Build one reviewed connection preset from manifest data."""

    if not isinstance(payload, Mapping):
        raise OperationRegistryError("Connection preset must be a JSON object")
    preset_id = _manifest_text(payload, "id")
    adapter_id = _manifest_text(payload, "adapter_id")
    if adapter_id != OPENAI_COMPATIBLE_CHAT_ADAPTER_ID:
        raise OperationRegistryError("Connection preset adapter is not supported")
    adapter_version = _manifest_text(payload, "adapter_version")
    if adapter_version != OPENAI_COMPATIBLE_CHAT_ADAPTER_VERSION:
        raise OperationRegistryError("Connection preset adapter version is not supported")
    api_surface = _manifest_text(payload, "api_surface")
    dialect_policy_id = _manifest_text(payload, "dialect_policy_id")
    try:
        dialect_policy = resolve_openai_compatible_dialect(dialect_policy_id)
    except LLMConfigurationError as exc:
        raise OperationRegistryError(
            "Connection preset dialect policy is not supported"
        ) from exc
    if api_surface != dialect_policy.api_surface:
        raise OperationRegistryError("Connection preset API surface is not supported")

    capabilities = freeze_capabilities(_manifest_text_tuple(payload, "capability_ceiling"))
    if not capabilities.issubset(dialect_policy.capabilities):
        raise OperationRegistryError("Connection preset capability ceiling is not supported")
    endpoint_policy_id = _manifest_text(payload, "endpoint_policy_id")
    if endpoint_policy_id not in _ALLOWED_PRESET_ENDPOINT_POLICIES:
        raise OperationRegistryError("Connection preset endpoint policy is not supported")
    discovery_strategy = _manifest_text(payload, "discovery_strategy")
    if discovery_strategy not in _ALLOWED_PRESET_DISCOVERY_STRATEGIES:
        raise OperationRegistryError("Connection preset discovery strategy is not supported")
    auth_mode = _manifest_text(payload, "auth_mode")
    if auth_mode not in _ALLOWED_PRESET_AUTH_MODES:
        raise OperationRegistryError("Connection preset auth mode is not supported")

    fixed_base_url = _manifest_optional_text(payload, "fixed_base_url")
    endpoint_config_field = _manifest_optional_text(payload, "endpoint_config_field")
    base_url_env = _manifest_optional_text(payload, "base_url_env")
    if endpoint_policy_id == FIXED_PROVIDER_ENDPOINT_POLICY_ID:
        if endpoint_config_field is not None:
            raise OperationRegistryError("Fixed connection preset cannot accept endpoints")
        if fixed_base_url is not None:
            _validate_manifest_https_base_url(fixed_base_url)
        elif base_url_env is None:
            raise OperationRegistryError("Fixed connection preset requires an endpoint")
    elif (
        endpoint_config_field != "base_url"
        or fixed_base_url is not None
        or base_url_env is not None
    ):
        raise OperationRegistryError("User endpoint preset must declare base_url only")

    return ProvingConnectionPreset(
        id=preset_id,
        display_name=_manifest_text(payload, "display_name"),
        canonical_model_id=_manifest_text(payload, "canonical_model_id", allow_empty=True),
        exact_wire_model_id=_manifest_text(payload, "exact_wire_model_id", allow_empty=True),
        runtime_family_id=_manifest_text(payload, "runtime_family_id"),
        serving_operator_id=_manifest_text(payload, "serving_operator_id"),
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        api_surface=api_surface,
        dialect_policy_id=dialect_policy_id,
        capability_ceiling=capabilities,
        endpoint_policy_id=endpoint_policy_id,
        discovery_strategy=discovery_strategy,
        auth_mode=auth_mode,
        secret_fields=_manifest_text_tuple(payload, "secret_fields"),
        user_config_fields=_manifest_text_tuple(payload, "user_config_fields"),
        fixed_base_url=fixed_base_url,
        endpoint_config_field=endpoint_config_field,
        client_base_path=_manifest_base_path(payload, "client_base_path"),
        billing_provider_id=_manifest_optional_text(payload, "billing_provider_id"),
        base_url_env=base_url_env,
        e2e_enabled_env=_manifest_optional_text(payload, "e2e_enabled_env"),
        e2e_api_key_env=_manifest_optional_text(payload, "e2e_api_key_env"),
        is_proving=bool(payload.get("proving", False)),
    )


def _manifest_text(
    payload: Mapping[str, Any],
    key: str,
    *,
    allow_empty: bool = False,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or value != value.strip() or (
        not allow_empty and not value
    ):
        raise OperationRegistryError(f"Connection preset field '{key}' is invalid")
    return value


def _manifest_optional_text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise OperationRegistryError(f"Connection preset field '{key}' is invalid")
    return value


def _manifest_base_path(payload: Mapping[str, Any], key: str) -> str:
    """Return an empty or validated absolute client base path."""

    value = _manifest_text(payload, key, allow_empty=True)
    if value and (value.endswith("/") or not _valid_base_path(value)):
        raise OperationRegistryError(f"Connection preset field '{key}' is invalid")
    return value


def _manifest_text_tuple(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise OperationRegistryError(f"Connection preset field '{key}' is invalid")
    result = tuple(_manifest_list_text(item, key) for item in value)
    if len(set(result)) != len(result):
        raise OperationRegistryError(f"Connection preset field '{key}' has duplicates")
    return result


def _manifest_list_text(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise OperationRegistryError(f"Connection preset field '{key}' is invalid")
    return value


def _validate_manifest_https_base_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise OperationRegistryError("Connection preset fixed endpoint is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise OperationRegistryError("Connection preset fixed endpoint violates policy")


_MANIFEST = _load_connection_presets_manifest(
    DEFAULT_CONNECTION_PRESET_MANIFEST_PATH
)
_NATIVE_PROVIDER_ENDPOINTS: Mapping[str, _NativeProviderEndpoint] = MappingProxyType(
    {
        endpoint.provider_id: endpoint
        for endpoint in _MANIFEST.native_provider_endpoints
    }
)
_CONNECTION_PRESETS: Mapping[str, ProvingConnectionPreset] = MappingProxyType(
    {
        preset.id: preset
        for preset in _MANIFEST.presets
    }
)
_PROVING_PRESETS: Mapping[str, ProvingConnectionPreset] = MappingProxyType(
    {preset.id: preset for preset in _CONNECTION_PRESETS.values() if preset.is_proving}
)
