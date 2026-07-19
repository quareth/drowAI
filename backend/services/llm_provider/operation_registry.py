"""Code-owned registry for guarded LLM connection operations.

The registry is the only authority that maps provider or preset operation IDs
to fixed endpoints or policy-validated preset endpoints. It has no mutation,
custom header, fallback, or dynamic adapter registration API.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import ip_address
import json
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from agent.providers.llm.adapters.openai.compatible_dialects import (
    OPENAI_COMPATIBLE_CHAT_ADAPTER_ID,
    OPENAI_COMPATIBLE_CHAT_ADAPTER_VERSION,
    resolve_openai_compatible_dialect,
)
from agent.providers.llm.core.capabilities import LLMCapability, freeze_capabilities
from agent.providers.llm.core.exceptions import LLMConfigurationError
from .types import (
    LLMEgressNetworkScope,
    LLMConnectionOperation,
    RegisteredLLMOperationTarget,
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
_HTTP_SCHEME = "http"
_HTTPS_SCHEME = "https"
_HTTP_DEFAULT_PORT = 80
_HTTPS_DEFAULT_PORT = 443
_LOOPBACK_HOSTNAME = "localhost"
DEFAULT_CONNECTION_PRESET_MANIFEST_PATH = Path(__file__).with_name(
    "connection_presets_manifest.json"
)
_SUPPORTED_PRESET_SCHEMA_VERSION = 2
_ALLOWED_PRESET_AUTH_MODES = frozenset({"bearer_api_key"})
_ALLOWED_PRESET_DISCOVERY_STRATEGIES = frozenset({"openai_models_endpoint"})
_ALLOWED_PRESET_ENDPOINT_POLICIES = frozenset(
    {FIXED_PROVIDER_ENDPOINT_POLICY_ID, USER_HTTPS_BASE_URL_ENDPOINT_POLICY_ID}
)


class OperationRegistryError(ValueError):
    """Raised when an operation/provider pair is not code-owned and supported."""


@dataclass(frozen=True, slots=True)
class _OperationDefinition:
    """Internal fixed operation method and path template."""

    method: str
    path_template: str
    requires_resource_id: bool = False


@dataclass(frozen=True, slots=True)
class _ResolvedOperationOrigin:
    """Validated operation origin plus its exact network confinement."""

    base_url: str
    client_base_path: str
    port: int
    network_scope: LLMEgressNetworkScope


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


def _valid_base_path(path: str) -> bool:
    """Return whether a declared URL base path is safe to compose."""

    if not path.startswith("/") or "\\" in path or "//" in path:
        return False
    if re.search(r"%(?:2e|2f|5c)", path, flags=re.IGNORECASE):
        return False
    return not any(segment in {".", ".."} for segment in path.split("/"))


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
        raise OperationRegistryError("Connection preset manifest must define one proving preset")
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
    """Validate one native provider endpoint declaration."""

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
    """Validate one preset manifest row and return immutable runtime metadata."""

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
        return _validated_https_base_url(base_url, "Preset endpoint base URL")

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

        origin = self._origin_for(normalized_provider, base_url=base_url)
        client_base_url = _join_declared_base_path(
            origin.base_url,
            origin.client_base_path,
        )
        operation_path = _operation_path_relative_to_client_base(
            path,
            origin.client_base_path,
        )
        url = _join_origin_path(client_base_url, operation_path)
        parsed = urlsplit(url)
        return RegisteredLLMOperationTarget(
            operation=normalized_operation,
            provider=normalized_provider,
            method=definition.method,
            url=url,
            client_base_url=client_base_url,
            expected_host=str(parsed.hostname or ""),
            allowed_ports=frozenset({origin.port}),
            allowed_path_prefixes=(parsed.path,),
            network_scope=origin.network_scope,
        )

    def _origin_for(
        self,
        provider: str,
        *,
        base_url: str | None,
    ) -> _ResolvedOperationOrigin:
        native_endpoint = _NATIVE_PROVIDER_ENDPOINTS.get(provider)
        if native_endpoint is not None:
            if base_url is not None:
                raise OperationRegistryError("Fixed provider target does not accept a base URL")
            operator_override = self._env_getter(native_endpoint.base_url_env)
            origin = (
                _validated_operator_base_url(operator_override)
                if operator_override
                else _public_https_origin(
                    native_endpoint.default_base_url,
                    "Native provider target",
                )
            )
            return _with_client_base_path(origin, native_endpoint.client_base_path)
        preset = _CONNECTION_PRESETS.get(provider)
        if preset is None:
            raise OperationRegistryError("Provider has no fixed target")

        if preset.endpoint_config_field is not None:
            return _with_client_base_path(
                _public_https_origin(
                    self.validate_preset_base_url(preset.id, base_url),
                    "Preset endpoint base URL",
                ),
                preset.client_base_path,
            )
        if base_url is not None:
            raise OperationRegistryError("Fixed preset target does not accept a base URL")

        operator_override = (
            self._env_getter(preset.base_url_env)
            if preset.base_url_env is not None
            else None
        )
        if operator_override:
            return _with_client_base_path(
                _validated_operator_base_url(operator_override),
                preset.client_base_path,
            )
        if preset.fixed_base_url is not None:
            return _with_client_base_path(
                _public_https_origin(
                    preset.fixed_base_url,
                    "Preset fixed base URL",
                ),
                preset.client_base_path,
            )
        if preset.base_url_env is None:
            raise OperationRegistryError("Preset has no endpoint target")
        return _with_client_base_path(
            _public_https_origin(
                operator_override,
                "Proving endpoint base URL",
            ),
            preset.client_base_path,
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
    if normalized not in _NATIVE_PROVIDER_ENDPOINTS and normalized not in _CONNECTION_PRESETS:
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


def _validated_https_base_url(value: str | None, label: str) -> str:
    """Validate an HTTPS base URL before route path composition."""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise OperationRegistryError(f"{label} is not configured")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise OperationRegistryError(f"{label} is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise OperationRegistryError(f"{label} violates policy")
    path = parsed.path.rstrip("/")
    if path in {"", "/"}:
        path = ""
    elif not _valid_base_path(path):
        raise OperationRegistryError(f"{label} path violates policy")
    return urlunsplit(("https", parsed.netloc, path, "", ""))


def _public_https_origin(value: str | None, label: str) -> _ResolvedOperationOrigin:
    """Return a validated public HTTPS origin with its registered port."""

    base_url = _validated_https_base_url(value, label)
    parsed = urlsplit(base_url)
    return _ResolvedOperationOrigin(
        base_url=base_url,
        client_base_path="",
        port=parsed.port or _HTTPS_DEFAULT_PORT,
        network_scope=LLMEgressNetworkScope.PUBLIC,
    )


def _validated_operator_base_url(value: str) -> _ResolvedOperationOrigin:
    """Validate an explicit operator override without admitting arbitrary HTTP."""

    label = "Provider operator base URL"
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise OperationRegistryError(f"{label} is invalid")
    try:
        parsed = urlsplit(value)
        explicit_port = parsed.port
    except ValueError as exc:
        raise OperationRegistryError(f"{label} is invalid") from exc

    host = str(parsed.hostname or "").lower()
    is_loopback = _is_loopback_host(host)
    if (
        parsed.scheme not in {_HTTP_SCHEME, _HTTPS_SCHEME}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == _HTTP_SCHEME and not is_loopback)
    ):
        raise OperationRegistryError(f"{label} violates policy")

    path = parsed.path.rstrip("/")
    if path in {"", "/"}:
        path = ""
    elif not _valid_base_path(path):
        raise OperationRegistryError(f"{label} path violates policy")

    default_port = (
        _HTTPS_DEFAULT_PORT if parsed.scheme == _HTTPS_SCHEME else _HTTP_DEFAULT_PORT
    )
    return _ResolvedOperationOrigin(
        base_url=urlunsplit((parsed.scheme, parsed.netloc, path, "", "")),
        client_base_path="",
        port=explicit_port or default_port,
        network_scope=(
            LLMEgressNetworkScope.LOOPBACK
            if is_loopback
            else LLMEgressNetworkScope.PUBLIC
        ),
    )


def _with_client_base_path(
    origin: _ResolvedOperationOrigin,
    client_base_path: str,
) -> _ResolvedOperationOrigin:
    """Attach a reviewed SDK client base path to a validated origin."""

    return _ResolvedOperationOrigin(
        base_url=origin.base_url,
        client_base_path=client_base_path,
        port=origin.port,
        network_scope=origin.network_scope,
    )


def _is_loopback_host(host: str) -> bool:
    """Return whether a URL hostname is explicitly local to this machine."""

    if host == _LOOPBACK_HOSTNAME:
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _join_declared_base_path(base_url: str, client_base_path: str) -> str:
    """Compose a reviewed client path exactly once onto an endpoint base URL."""

    normalized_base = base_url.rstrip("/")
    if not client_base_path:
        return normalized_base
    parsed_path = urlsplit(normalized_base).path.rstrip("/")
    if parsed_path == client_base_path or parsed_path.endswith(client_base_path):
        return normalized_base
    return f"{normalized_base}{client_base_path}"


def _operation_path_relative_to_client_base(
    operation_path: str,
    client_base_path: str,
) -> str:
    """Return the operation suffix expected below the SDK client base URL."""

    if client_base_path and operation_path.startswith(f"{client_base_path}/"):
        return operation_path[len(client_base_path) :]
    return operation_path


def _join_origin_path(origin: str, path: str) -> str:
    """Join a validated absolute client base URL and operation path."""

    return f"{origin.rstrip('/')}{path}"


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
    "NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID",
    "NVIDIA_NIM_BASE_URL_ENV",
    "OPENAI_BASE_URL_ENV",
    "OLLAMA_OPENAI_COMPATIBLE_PRESET_ID",
    "OperationRegistryError",
    "ProvingConnectionPreset",
    "USER_HTTPS_BASE_URL_ENDPOINT_POLICY_ID",
    "VLLM_OPENAI_COMPATIBLE_PRESET_ID",
]
