"""Tests for the code-owned LLM connection operation registry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields
import inspect
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from urllib.parse import urlsplit

import pytest

from backend.services.llm_provider._connection_operation_contracts import (
    OperationRegistryError as SharedOperationRegistryError,
)
from backend.services.llm_provider._connection_preset_catalog import (
    ProvingConnectionPreset as CatalogProvingConnectionPreset,
)
from backend.services.llm_provider.guarded_transport import GuardedTransport
from backend.services.llm_provider import operation_registry
from backend.services.llm_provider.operation_registry import (
    ANTHROPIC_BASE_URL_ENV,
    CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
    ConnectionOperationRegistry,
    FIXED_PROVIDER_ENDPOINT_POLICY_ID,
    GPT_OSS_20B_PROVING_API_KEY_ENV,
    GPT_OSS_20B_PROVING_BASE_URL_ENV,
    GPT_OSS_20B_PROVING_E2E_ENV,
    GPT_OSS_20B_PROVING_PRESET_ID,
    HUGGINGFACE_BASE_URL_ENV,
    HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    NVIDIA_NIM_BASE_URL_ENV,
    NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
    OPENAI_BASE_URL_ENV,
    OperationRegistryError,
    ProvingConnectionPreset,
    PUBLIC_GPT_OSS_20B_PRESET_IDS,
    USER_HTTPS_BASE_URL_ENDPOINT_POLICY_ID,
    VLLM_OPENAI_COMPATIBLE_PRESET_ID,
)
from backend.services.llm_provider.types import (
    LLMEgressNetworkScope,
    LLMConnectionOperation,
)


EXPECTED_FACADE_ALL = [
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

EXPECTED_PUBLIC_CONSTANTS = {
    "ANTHROPIC_BASE_URL_ENV": "ANTHROPIC_BASE_URL",
    "CUSTOM_OPENAI_COMPATIBLE_PRESET_ID": "custom_openai_compatible_chat",
    "FIXED_PROVIDER_ENDPOINT_POLICY_ID": "fixed_provider_v1",
    "GPT_OSS_20B_PROVING_API_KEY_ENV": "DROWAI_GPT_OSS_20B_PROVING_API_KEY",
    "GPT_OSS_20B_PROVING_BASE_URL_ENV": "DROWAI_GPT_OSS_20B_PROVING_BASE_URL",
    "GPT_OSS_20B_PROVING_E2E_ENV": "DROWAI_GPT_OSS_20B_PROVING_E2E",
    "GPT_OSS_20B_PROVING_PRESET_ID": "gpt_oss_20b_openai_compatible_proving",
    "HUGGINGFACE_BASE_URL_ENV": "DROWAI_HUGGINGFACE_BASE_URL",
    "HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID": "huggingface_openai_compatible_chat",
    "NVIDIA_NIM_BASE_URL_ENV": "DROWAI_NVIDIA_NIM_BASE_URL",
    "NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID": "nvidia_nim_openai_compatible_chat",
    "OLLAMA_OPENAI_COMPATIBLE_PRESET_ID": "ollama_openai_compatible_chat",
    "OPENAI_BASE_URL_ENV": "OPENAI_BASE_URL",
    "PUBLIC_GPT_OSS_20B_PRESET_IDS": (
        "nvidia_nim_openai_compatible_chat",
        "huggingface_openai_compatible_chat",
        "ollama_openai_compatible_chat",
        "vllm_openai_compatible_chat",
    ),
    "USER_HTTPS_BASE_URL_ENDPOINT_POLICY_ID": "user_https_base_url_v1",
    "VLLM_OPENAI_COMPATIBLE_PRESET_ID": "vllm_openai_compatible_chat",
}

EXPECTED_PRESET_FIELDS = (
    "id",
    "display_name",
    "canonical_model_id",
    "exact_wire_model_id",
    "runtime_family_id",
    "serving_operator_id",
    "adapter_id",
    "adapter_version",
    "api_surface",
    "dialect_policy_id",
    "capability_ceiling",
    "endpoint_policy_id",
    "discovery_strategy",
    "auth_mode",
    "secret_fields",
    "user_config_fields",
    "fixed_base_url",
    "endpoint_config_field",
    "client_base_path",
    "billing_provider_id",
    "base_url_env",
    "e2e_enabled_env",
    "e2e_api_key_env",
    "is_proving",
)

CONFIGURABLE_PRESETS = {
    CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
    OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
    VLLM_OPENAI_COMPATIBLE_PRESET_ID,
}

OPENAI_COMPATIBLE_PRESET_IDS = (
    CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
    GPT_OSS_20B_PROVING_PRESET_ID,
    HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
    VLLM_OPENAI_COMPATIBLE_PRESET_ID,
)


def test_facade_exports_exact_public_names_and_imports() -> None:
    """The canonical facade keeps its current export and direct import surface."""

    assert operation_registry.__all__ == EXPECTED_FACADE_ALL

    imported = {
        name: getattr(
            __import__(
                "backend.services.llm_provider.operation_registry",
                fromlist=[name],
            ),
            name,
        )
        for name in EXPECTED_FACADE_ALL
    }

    for name in EXPECTED_FACADE_ALL:
        assert imported[name] is getattr(operation_registry, name)
    assert operation_registry.ConnectionOperationRegistry is ConnectionOperationRegistry
    assert operation_registry.OperationRegistryError is OperationRegistryError
    assert operation_registry.ProvingConnectionPreset is ProvingConnectionPreset
    assert OperationRegistryError is SharedOperationRegistryError
    assert ProvingConnectionPreset is CatalogProvingConnectionPreset
    assert issubclass(OperationRegistryError, ValueError)
    assert tuple(field.name for field in fields(ProvingConnectionPreset)) == (
        EXPECTED_PRESET_FIELDS
    )


def test_facade_public_constants_keep_current_values() -> None:
    """Public operation-registry constants remain exact string contracts."""

    assert ANTHROPIC_BASE_URL_ENV == EXPECTED_PUBLIC_CONSTANTS["ANTHROPIC_BASE_URL_ENV"]
    assert (
        CUSTOM_OPENAI_COMPATIBLE_PRESET_ID
        == EXPECTED_PUBLIC_CONSTANTS["CUSTOM_OPENAI_COMPATIBLE_PRESET_ID"]
    )
    assert (
        FIXED_PROVIDER_ENDPOINT_POLICY_ID
        == EXPECTED_PUBLIC_CONSTANTS["FIXED_PROVIDER_ENDPOINT_POLICY_ID"]
    )
    assert (
        GPT_OSS_20B_PROVING_API_KEY_ENV
        == EXPECTED_PUBLIC_CONSTANTS["GPT_OSS_20B_PROVING_API_KEY_ENV"]
    )
    assert (
        GPT_OSS_20B_PROVING_BASE_URL_ENV
        == EXPECTED_PUBLIC_CONSTANTS["GPT_OSS_20B_PROVING_BASE_URL_ENV"]
    )
    assert (
        GPT_OSS_20B_PROVING_E2E_ENV
        == EXPECTED_PUBLIC_CONSTANTS["GPT_OSS_20B_PROVING_E2E_ENV"]
    )
    assert (
        GPT_OSS_20B_PROVING_PRESET_ID
        == EXPECTED_PUBLIC_CONSTANTS["GPT_OSS_20B_PROVING_PRESET_ID"]
    )
    assert HUGGINGFACE_BASE_URL_ENV == EXPECTED_PUBLIC_CONSTANTS["HUGGINGFACE_BASE_URL_ENV"]
    assert (
        HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID
        == EXPECTED_PUBLIC_CONSTANTS["HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID"]
    )
    assert NVIDIA_NIM_BASE_URL_ENV == EXPECTED_PUBLIC_CONSTANTS["NVIDIA_NIM_BASE_URL_ENV"]
    assert (
        NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID
        == EXPECTED_PUBLIC_CONSTANTS["NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID"]
    )
    assert (
        OLLAMA_OPENAI_COMPATIBLE_PRESET_ID
        == EXPECTED_PUBLIC_CONSTANTS["OLLAMA_OPENAI_COMPATIBLE_PRESET_ID"]
    )
    assert OPENAI_BASE_URL_ENV == EXPECTED_PUBLIC_CONSTANTS["OPENAI_BASE_URL_ENV"]
    assert (
        PUBLIC_GPT_OSS_20B_PRESET_IDS
        == EXPECTED_PUBLIC_CONSTANTS["PUBLIC_GPT_OSS_20B_PRESET_IDS"]
    )
    assert (
        USER_HTTPS_BASE_URL_ENDPOINT_POLICY_ID
        == EXPECTED_PUBLIC_CONSTANTS["USER_HTTPS_BASE_URL_ENDPOINT_POLICY_ID"]
    )
    assert (
        VLLM_OPENAI_COMPATIBLE_PRESET_ID
        == EXPECTED_PUBLIC_CONSTANTS["VLLM_OPENAI_COMPATIBLE_PRESET_ID"]
    )


def test_facade_public_method_signatures_are_stable() -> None:
    """The registry facade keeps its constructor, query, and validation seams."""

    registry_type = ConnectionOperationRegistry

    assert str(inspect.signature(registry_type.__init__)) == (
        "(self, *, env_getter: 'EnvGetter | None' = None) -> 'None'"
    )
    assert str(inspect.signature(registry_type.list_operation_ids)) == (
        "(self) -> 'tuple[str, ...]'"
    )
    assert str(inspect.signature(registry_type.list_proving_preset_ids)) == (
        "(self) -> 'tuple[str, ...]'"
    )
    assert str(inspect.signature(registry_type.list_connection_preset_ids)) == (
        "(self) -> 'tuple[str, ...]'"
    )
    assert str(inspect.signature(registry_type.list_public_gpt_oss_20b_preset_ids)) == (
        "(self) -> 'tuple[str, ...]'"
    )
    assert str(inspect.signature(registry_type.get_connection_preset)) == (
        "(self, preset_id: 'str') -> 'ProvingConnectionPreset'"
    )
    assert str(inspect.signature(registry_type.get_proving_preset)) == (
        "(self, preset_id: 'str') -> 'ProvingConnectionPreset'"
    )
    assert str(inspect.signature(registry_type.validate_preset_base_url)) == (
        "(self, preset_id: 'str', base_url: 'str | None') -> 'str'"
    )
    assert str(inspect.signature(registry_type.resolve)) == (
        "(self, operation: 'LLMConnectionOperation | str', *, provider: 'str', "
        "base_url: 'str | None' = None, resource_id: 'str | None' = None) -> "
        "'RegisteredLLMOperationTarget'"
    )


def test_import_time_manifest_validation_fails_closed(
    tmp_path: Path,
) -> None:
    """Invalid checked-in manifest data prevents the registry facade from loading."""

    manifest = _checked_in_manifest()
    manifest["schema_version"] = -1

    with pytest.raises(ValueError) as exc_info:
        _load_operation_registry_copy(tmp_path, manifest)

    assert exc_info.type.__name__ == "OperationRegistryError"
    assert str(exc_info.value) == "Unsupported connection preset manifest schema"
    assert exc_info.value.__cause__ is None


def test_import_time_manifest_validation_preserves_cause_chaining(
    tmp_path: Path,
) -> None:
    """Manifest parsing errors keep their current public message and cause."""

    manifest = _checked_in_manifest()
    manifest["presets"][1]["fixed_base_url"] = "https://router.huggingface.co:bad"

    with pytest.raises(ValueError) as exc_info:
        _load_operation_registry_copy(tmp_path, manifest)

    assert exc_info.type.__name__ == "OperationRegistryError"
    assert str(exc_info.value) == "Connection preset fixed endpoint is invalid"
    assert isinstance(exc_info.value.__cause__, ValueError)


def test_operation_provider_and_preset_matrix_snapshot() -> None:
    """Every current operation/provider/preset pair resolves or fails identically."""

    registry = ConnectionOperationRegistry(env_getter=lambda _name: None)

    assert registry.list_connection_preset_ids() == OPENAI_COMPATIBLE_PRESET_IDS
    assert registry.list_public_gpt_oss_20b_preset_ids() == PUBLIC_GPT_OSS_20B_PRESET_IDS

    providers = (
        "openai",
        "anthropic",
        *registry.list_connection_preset_ids(),
    )
    matrix = {
        (provider, operation): _resolve_snapshot(registry, provider, operation)
        for provider in providers
        for operation in registry.list_operation_ids()
    }

    assert matrix == {
        **_native_provider_operation_snapshot(),
        **_openai_compatible_preset_operation_snapshot(),
        **_proving_preset_operation_snapshot(),
    }

    proving_registry = ConnectionOperationRegistry(
        env_getter={
            GPT_OSS_20B_PROVING_BASE_URL_ENV: "https://proving.example.test/base",
        }.get
    )
    proving_matrix = {
        (GPT_OSS_20B_PROVING_PRESET_ID, operation): _resolve_snapshot(
            proving_registry,
            GPT_OSS_20B_PROVING_PRESET_ID,
            operation,
        )
        for operation in registry.list_operation_ids()
    }
    assert proving_matrix == _configured_proving_preset_operation_snapshot()


def test_lifecycle_resource_identifier_contract_messages() -> None:
    """Lifecycle resource IDs keep their exact acceptance and rejection contract."""

    registry = ConnectionOperationRegistry(env_getter=lambda _name: None)

    accepted = registry.resolve(
        LLMConnectionOperation.LIFECYCLE_DELETE,
        provider="openai",
        resource_id="conv_ABC-123",
    )
    assert accepted.url == "https://api.openai.com/v1/conversations/conv_ABC-123"

    for resource_id in ("../admin", "a/b", "", "conv?id=1", "conv%2fother"):
        with pytest.raises(OperationRegistryError) as exc_info:
            registry.resolve(
                LLMConnectionOperation.LIFECYCLE_DELETE,
                provider="openai",
                resource_id=resource_id,
            )
        assert str(exc_info.value) == "Invalid lifecycle resource identifier"
        assert exc_info.value.__cause__ is None

    with pytest.raises(OperationRegistryError) as missing_info:
        registry.resolve(LLMConnectionOperation.LIFECYCLE_DELETE, provider="openai")
    assert str(missing_info.value) == "Invalid lifecycle resource identifier"

    with pytest.raises(OperationRegistryError) as extra_info:
        registry.resolve(
            LLMConnectionOperation.HEALTH,
            provider="openai",
            resource_id="conv_ABC-123",
    )
    assert str(extra_info.value) == "Operation does not accept a resource identifier"


@pytest.mark.parametrize(
    (
        "provider",
        "env_overrides",
        "base_url",
        "expected_url",
        "expected_client_base_url",
        "expected_host",
        "expected_ports",
        "expected_path_prefixes",
    ),
    (
        (
            "openai",
            {OPENAI_BASE_URL_ENV: "https://gateway.example.test"},
            None,
            "https://gateway.example.test/v1/chat/completions",
            "https://gateway.example.test/v1",
            "gateway.example.test",
            frozenset({443}),
            ("/v1/chat/completions",),
        ),
        (
            "openai",
            {OPENAI_BASE_URL_ENV: "https://gateway.example.test:443/team"},
            None,
            "https://gateway.example.test:443/team/v1/chat/completions",
            "https://gateway.example.test:443/team/v1",
            "gateway.example.test",
            frozenset({443}),
            ("/team/v1/chat/completions",),
        ),
        (
            CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
            {},
            "https://tenant.example.test:443/api",
            "https://tenant.example.test:443/api/v1/chat/completions",
            "https://tenant.example.test:443/api/v1",
            "tenant.example.test",
            frozenset({443}),
            ("/api/v1/chat/completions",),
        ),
    ),
)
def test_public_https_endpoint_origin_fields_are_stable(
    provider: str,
    env_overrides: dict[str, str],
    base_url: str | None,
    expected_url: str,
    expected_client_base_url: str,
    expected_host: str,
    expected_ports: frozenset[int],
    expected_path_prefixes: tuple[str, ...],
) -> None:
    """Public HTTPS origins preserve port, host, path, and scope fields."""

    registry = ConnectionOperationRegistry(env_getter=env_overrides.get)

    target = registry.resolve(
        LLMConnectionOperation.INFERENCE,
        provider=provider,
        base_url=base_url,
    )

    assert target.url == expected_url
    assert target.client_base_url == expected_client_base_url
    assert target.expected_host == expected_host
    assert target.allowed_ports == expected_ports
    assert target.allowed_path_prefixes == expected_path_prefixes
    assert target.network_scope is LLMEgressNetworkScope.PUBLIC


@pytest.mark.parametrize(
    (
        "override",
        "expected_url",
        "expected_client_base_url",
        "expected_host",
        "expected_ports",
        "expected_path_prefixes",
    ),
    (
        (
            "http://localhost:4100/v1",
            "http://localhost:4100/v1/chat/completions",
            "http://localhost:4100/v1",
            "localhost",
            frozenset({4100}),
            ("/v1/chat/completions",),
        ),
        (
            "http://127.0.0.1:4101/base",
            "http://127.0.0.1:4101/base/v1/chat/completions",
            "http://127.0.0.1:4101/base/v1",
            "127.0.0.1",
            frozenset({4101}),
            ("/base/v1/chat/completions",),
        ),
        (
            "http://[::1]:4102/v1",
            "http://[::1]:4102/v1/chat/completions",
            "http://[::1]:4102/v1",
            "::1",
            frozenset({4102}),
            ("/v1/chat/completions",),
        ),
    ),
)
def test_loopback_http_operator_origins_keep_current_fields(
    override: str,
    expected_url: str,
    expected_client_base_url: str,
    expected_host: str,
    expected_ports: frozenset[int],
    expected_path_prefixes: tuple[str, ...],
) -> None:
    """Operator HTTP overrides are accepted only for current loopback forms."""

    registry = ConnectionOperationRegistry(
        env_getter={OPENAI_BASE_URL_ENV: override}.get
    )

    target = registry.resolve(LLMConnectionOperation.INFERENCE, provider="openai")

    assert target.url == expected_url
    assert target.client_base_url == expected_client_base_url
    assert target.expected_host == expected_host
    assert target.allowed_ports == expected_ports
    assert target.allowed_path_prefixes == expected_path_prefixes
    assert target.network_scope is LLMEgressNetworkScope.LOOPBACK


@pytest.mark.parametrize(
    ("override", "expected_message", "expected_cause"),
    (
        (
            "http://provider.example.test:4000",
            "Provider operator base URL violates policy",
            None,
        ),
        ("http://10.0.0.1:4000", "Provider operator base URL violates policy", None),
        (
            "http://169.254.169.254:4000",
            "Provider operator base URL violates policy",
            None,
        ),
        ("ftp://127.0.0.1:4000", "Provider operator base URL violates policy", None),
        (
            "http://user:password@127.0.0.1:4000",
            "Provider operator base URL violates policy",
            None,
        ),
        (
            "http://127.0.0.1:4000?token=secret",
            "Provider operator base URL violates policy",
            None,
        ),
        (
            "http://127.0.0.1:4000#fragment",
            "Provider operator base URL violates policy",
            None,
        ),
        (" http://127.0.0.1:4000", "Provider operator base URL is invalid", None),
        ("http://127.0.0.1:bad", "Provider operator base URL is invalid", ValueError),
        (
            "http://127.0.0.1:4000/a%2fb",
            "Provider operator base URL path violates policy",
            None,
        ),
        (
            "http://127.0.0.1:4000/a\\b",
            "Provider operator base URL path violates policy",
            None,
        ),
        (
            "http://127.0.0.1:4000/a//b",
            "Provider operator base URL path violates policy",
            None,
        ),
    ),
)
def test_operator_override_rejections_keep_exact_messages(
    override: str,
    expected_message: str,
    expected_cause: type[BaseException] | None,
) -> None:
    """Unsafe operator overrides fail with current exact messages and causes."""

    registry = ConnectionOperationRegistry(
        env_getter={NVIDIA_NIM_BASE_URL_ENV: override}.get
    )

    _assert_registry_error(
        lambda: registry.resolve(
            LLMConnectionOperation.INFERENCE,
            provider=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        ),
        expected_message,
        expected_cause,
    )


@pytest.mark.parametrize(
    ("base_url", "expected_message", "expected_cause"),
    (
        (None, "Preset endpoint base URL is not configured", None),
        ("", "Preset endpoint base URL is not configured", None),
        (" ", "Preset endpoint base URL is not configured", None),
        ("http://tenant.example.test", "Preset endpoint base URL violates policy", None),
        (
            "https://tenant.example.test:8443",
            "Preset endpoint base URL violates policy",
            None,
        ),
        (
            "https://user:pw@tenant.example.test",
            "Preset endpoint base URL violates policy",
            None,
        ),
        (
            "https://tenant.example.test?token=secret",
            "Preset endpoint base URL violates policy",
            None,
        ),
        (
            "https://tenant.example.test#fragment",
            "Preset endpoint base URL violates policy",
            None,
        ),
        ("https://tenant.example.test:bad", "Preset endpoint base URL is invalid", ValueError),
        (
            "https://tenant.example.test/a%2fb",
            "Preset endpoint base URL path violates policy",
            None,
        ),
        (
            "https://tenant.example.test/a\\b",
            "Preset endpoint base URL path violates policy",
            None,
        ),
        (
            "https://tenant.example.test/a//b",
            "Preset endpoint base URL path violates policy",
            None,
        ),
    ),
)
def test_configurable_preset_base_url_rejections_keep_exact_messages(
    base_url: str | None,
    expected_message: str,
    expected_cause: type[BaseException] | None,
) -> None:
    """Unsafe user-configured endpoints fail with current exact messages."""

    registry = ConnectionOperationRegistry(env_getter=lambda _name: None)

    _assert_registry_error(
        lambda: registry.resolve(
            LLMConnectionOperation.INFERENCE,
            provider=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
            base_url=base_url,
        ),
        expected_message,
        expected_cause,
    )


def test_endpoint_kind_cases_keep_exact_messages_and_composition() -> None:
    """Missing, fixed, and user endpoints keep current resolve-time behavior."""

    registry = ConnectionOperationRegistry(env_getter=lambda _name: None)

    _assert_registry_error(
        lambda: registry.resolve(
            LLMConnectionOperation.HEALTH,
            provider="openai",
            base_url="https://tenant.example.test",
        ),
        "Fixed provider target does not accept a base URL",
    )
    _assert_registry_error(
        lambda: registry.resolve(
            LLMConnectionOperation.HEALTH,
            provider=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
            base_url="https://tenant.example.test",
        ),
        "Fixed preset target does not accept a base URL",
    )
    _assert_registry_error(
        lambda: registry.resolve(
            LLMConnectionOperation.HEALTH,
            provider=GPT_OSS_20B_PROVING_PRESET_ID,
        ),
        "Proving endpoint base URL is not configured",
    )

    target = registry.resolve(
        LLMConnectionOperation.INFERENCE,
        provider=OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
        base_url="https://tenant.example.test/team/v1",
    )

    assert target.url == "https://tenant.example.test/team/v1/chat/completions"
    assert target.client_base_url == "https://tenant.example.test/team/v1"
    assert target.allowed_path_prefixes == ("/team/v1/chat/completions",)
    assert target.allowed_ports == frozenset({443})
    assert target.expected_host == "tenant.example.test"
    assert target.network_scope is LLMEgressNetworkScope.PUBLIC


def _assert_registry_error(
    action: Callable[[], object],
    expected_message: str,
    expected_cause: type[BaseException] | None = None,
) -> None:
    with pytest.raises(OperationRegistryError) as exc_info:
        action()
    assert str(exc_info.value) == expected_message
    if expected_cause is None:
        assert exc_info.value.__cause__ is None
    else:
        assert isinstance(exc_info.value.__cause__, expected_cause)


def _checked_in_manifest() -> dict[str, object]:
    manifest_path = Path(operation_registry.__file__).with_name(
        "connection_presets_manifest.json"
    )
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _load_operation_registry_copy(
    tmp_path: Path,
    manifest: dict[str, object],
) -> ModuleType:
    package_path = tmp_path / "backend" / "services" / "llm_provider"
    package_path.mkdir(parents=True)
    module_path = package_path / "operation_registry.py"
    catalog_path = package_path / "_connection_preset_catalog.py"
    manifest_path = package_path / "connection_presets_manifest.json"
    module_path.write_text(
        Path(operation_registry.__file__).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    catalog_module_name = (
        "backend.services.llm_provider._connection_preset_catalog"
    )
    loaded_catalog = sys.modules[catalog_module_name]
    catalog_path.write_text(
        Path(loaded_catalog.__file__).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    import backend.services.llm_provider as llm_provider_package

    module_name = (
        "backend.services.llm_provider._operation_registry_import_characterization"
    )
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    had_catalog_attribute = hasattr(
        llm_provider_package,
        "_connection_preset_catalog",
    )
    prior_catalog_attribute = getattr(
        llm_provider_package,
        "_connection_preset_catalog",
        None,
    )
    sys.modules.pop(catalog_module_name)
    if had_catalog_attribute:
        delattr(llm_provider_package, "_connection_preset_catalog")
    llm_provider_package.__path__.insert(0, str(package_path))
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
        sys.modules.pop(catalog_module_name, None)
        llm_provider_package.__path__.remove(str(package_path))
        sys.modules[catalog_module_name] = loaded_catalog
        if had_catalog_attribute:
            setattr(
                llm_provider_package,
                "_connection_preset_catalog",
                prior_catalog_attribute,
            )
        elif hasattr(llm_provider_package, "_connection_preset_catalog"):
            delattr(llm_provider_package, "_connection_preset_catalog")
    return module


def _resolve_snapshot(
    registry: ConnectionOperationRegistry,
    provider: str,
    operation: str,
) -> tuple[object, ...]:
    kwargs: dict[str, str] = {}
    if operation == LLMConnectionOperation.LIFECYCLE_DELETE.value:
        kwargs["resource_id"] = "conv_ABC-123"
    if provider in CONFIGURABLE_PRESETS:
        kwargs["base_url"] = "https://tenant.example.test/api"
    try:
        target = registry.resolve(operation, provider=provider, **kwargs)
    except OperationRegistryError as exc:
        return ("error", str(exc), type(exc.__cause__).__name__ if exc.__cause__ else None)
    return (
        "ok",
        target.operation.value,
        target.provider,
        target.method,
        target.url,
        target.client_base_url,
        target.expected_host,
        tuple(sorted(target.allowed_ports)),
        target.allowed_path_prefixes,
        target.network_scope.value,
    )


def _target(
    operation: str,
    provider: str,
    method: str,
    url: str,
    client_base_url: str,
    host: str,
    path: str,
) -> tuple[object, ...]:
    return (
        "ok",
        operation,
        provider,
        method,
        url,
        client_base_url,
        host,
        (443,),
        (path,),
        LLMEgressNetworkScope.PUBLIC.value,
    )


def _unsupported() -> tuple[object, ...]:
    return ("error", "Operation is not registered for provider", None)


def _missing_proving_endpoint() -> tuple[object, ...]:
    return ("error", "Proving endpoint base URL is not configured", None)


def _native_provider_operation_snapshot() -> dict[tuple[str, str], tuple[object, ...]]:
    return {
        ("openai", "capability_probe"): _target(
            "capability_probe",
            "openai",
            "POST",
            "https://api.openai.com/v1/chat/completions",
            "https://api.openai.com/v1",
            "api.openai.com",
            "/v1/chat/completions",
        ),
        ("openai", "health"): _target(
            "health",
            "openai",
            "GET",
            "https://api.openai.com/v1/models",
            "https://api.openai.com/v1",
            "api.openai.com",
            "/v1/models",
        ),
        ("openai", "inference"): _target(
            "inference",
            "openai",
            "POST",
            "https://api.openai.com/v1/chat/completions",
            "https://api.openai.com/v1",
            "api.openai.com",
            "/v1/chat/completions",
        ),
        ("openai", "inventory"): _target(
            "inventory",
            "openai",
            "GET",
            "https://api.openai.com/v1/models",
            "https://api.openai.com/v1",
            "api.openai.com",
            "/v1/models",
        ),
        ("openai", "lifecycle_create"): _target(
            "lifecycle_create",
            "openai",
            "POST",
            "https://api.openai.com/v1/conversations",
            "https://api.openai.com/v1",
            "api.openai.com",
            "/v1/conversations",
        ),
        ("openai", "lifecycle_delete"): _target(
            "lifecycle_delete",
            "openai",
            "DELETE",
            "https://api.openai.com/v1/conversations/conv_ABC-123",
            "https://api.openai.com/v1",
            "api.openai.com",
            "/v1/conversations/conv_ABC-123",
        ),
        ("anthropic", "capability_probe"): _target(
            "capability_probe",
            "anthropic",
            "POST",
            "https://api.anthropic.com/v1/messages",
            "https://api.anthropic.com",
            "api.anthropic.com",
            "/v1/messages",
        ),
        ("anthropic", "health"): _target(
            "health",
            "anthropic",
            "GET",
            "https://api.anthropic.com/v1/models",
            "https://api.anthropic.com",
            "api.anthropic.com",
            "/v1/models",
        ),
        ("anthropic", "inference"): _target(
            "inference",
            "anthropic",
            "POST",
            "https://api.anthropic.com/v1/messages",
            "https://api.anthropic.com",
            "api.anthropic.com",
            "/v1/messages",
        ),
        ("anthropic", "inventory"): _target(
            "inventory",
            "anthropic",
            "GET",
            "https://api.anthropic.com/v1/models",
            "https://api.anthropic.com",
            "api.anthropic.com",
            "/v1/models",
        ),
        ("anthropic", "lifecycle_create"): _unsupported(),
        ("anthropic", "lifecycle_delete"): _unsupported(),
    }


def _openai_compatible_preset_operation_snapshot() -> dict[
    tuple[str, str],
    tuple[object, ...],
]:
    fixed_preset_specs = {
        HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID: (
            "https://router.huggingface.co/v1",
            "router.huggingface.co",
        ),
        NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID: (
            "https://integrate.api.nvidia.com/v1",
            "integrate.api.nvidia.com",
        ),
    }
    configurable_preset_specs = {
        preset_id: ("https://tenant.example.test/api/v1", "tenant.example.test")
        for preset_id in CONFIGURABLE_PRESETS
    }
    result: dict[tuple[str, str], tuple[object, ...]] = {}
    for preset_id, (client_base_url, host) in {
        **fixed_preset_specs,
        **configurable_preset_specs,
    }.items():
        result.update(
            {
                (preset_id, "capability_probe"): _target(
                    "capability_probe",
                    preset_id,
                    "POST",
                    f"{client_base_url}/chat/completions",
                    client_base_url,
                    host,
                    f"{_url_path(client_base_url)}/chat/completions",
                ),
                (preset_id, "health"): _target(
                    "health",
                    preset_id,
                    "GET",
                    f"{client_base_url}/models",
                    client_base_url,
                    host,
                    f"{_url_path(client_base_url)}/models",
                ),
                (preset_id, "inference"): _target(
                    "inference",
                    preset_id,
                    "POST",
                    f"{client_base_url}/chat/completions",
                    client_base_url,
                    host,
                    f"{_url_path(client_base_url)}/chat/completions",
                ),
                (preset_id, "inventory"): _target(
                    "inventory",
                    preset_id,
                    "GET",
                    f"{client_base_url}/models",
                    client_base_url,
                    host,
                    f"{_url_path(client_base_url)}/models",
                ),
                (preset_id, "lifecycle_create"): _unsupported(),
                (preset_id, "lifecycle_delete"): _unsupported(),
            }
        )
    return result


def _url_path(url: str) -> str:
    return urlsplit(url).path


def _proving_preset_operation_snapshot() -> dict[tuple[str, str], tuple[object, ...]]:
    return {
        (GPT_OSS_20B_PROVING_PRESET_ID, "capability_probe"): _missing_proving_endpoint(),
        (GPT_OSS_20B_PROVING_PRESET_ID, "health"): _missing_proving_endpoint(),
        (GPT_OSS_20B_PROVING_PRESET_ID, "inference"): _missing_proving_endpoint(),
        (GPT_OSS_20B_PROVING_PRESET_ID, "inventory"): _missing_proving_endpoint(),
        (GPT_OSS_20B_PROVING_PRESET_ID, "lifecycle_create"): _unsupported(),
        (GPT_OSS_20B_PROVING_PRESET_ID, "lifecycle_delete"): _unsupported(),
    }


def _configured_proving_preset_operation_snapshot() -> dict[
    tuple[str, str],
    tuple[object, ...],
]:
    client_base_url = "https://proving.example.test/base/v1"
    return {
        (GPT_OSS_20B_PROVING_PRESET_ID, "capability_probe"): _target(
            "capability_probe",
            GPT_OSS_20B_PROVING_PRESET_ID,
            "POST",
            f"{client_base_url}/chat/completions",
            client_base_url,
            "proving.example.test",
            "/base/v1/chat/completions",
        ),
        (GPT_OSS_20B_PROVING_PRESET_ID, "health"): _target(
            "health",
            GPT_OSS_20B_PROVING_PRESET_ID,
            "GET",
            f"{client_base_url}/models",
            client_base_url,
            "proving.example.test",
            "/base/v1/models",
        ),
        (GPT_OSS_20B_PROVING_PRESET_ID, "inference"): _target(
            "inference",
            GPT_OSS_20B_PROVING_PRESET_ID,
            "POST",
            f"{client_base_url}/chat/completions",
            client_base_url,
            "proving.example.test",
            "/base/v1/chat/completions",
        ),
        (GPT_OSS_20B_PROVING_PRESET_ID, "inventory"): _target(
            "inventory",
            GPT_OSS_20B_PROVING_PRESET_ID,
            "GET",
            f"{client_base_url}/models",
            client_base_url,
            "proving.example.test",
            "/base/v1/models",
        ),
        (GPT_OSS_20B_PROVING_PRESET_ID, "lifecycle_create"): _unsupported(),
        (GPT_OSS_20B_PROVING_PRESET_ID, "lifecycle_delete"): _unsupported(),
    }


def test_registry_exposes_only_code_owned_operation_ids() -> None:
    """The Phase 1 operation vocabulary is fixed and complete."""

    registry = ConnectionOperationRegistry()

    assert registry.list_operation_ids() == (
        "capability_probe",
        "health",
        "inference",
        "inventory",
        "lifecycle_create",
        "lifecycle_delete",
    )
    assert set(LLMConnectionOperation) == {
        LLMConnectionOperation.HEALTH,
        LLMConnectionOperation.INVENTORY,
        LLMConnectionOperation.CAPABILITY_PROBE,
        LLMConnectionOperation.LIFECYCLE_CREATE,
        LLMConnectionOperation.LIFECYCLE_DELETE,
        LLMConnectionOperation.INFERENCE,
    }


@pytest.mark.parametrize(
    ("provider", "operation", "expected_url", "expected_method"),
    [
        ("openai", "health", "https://api.openai.com/v1/models", "GET"),
        ("anthropic", "inventory", "https://api.anthropic.com/v1/models", "GET"),
        (
            "openai",
            "capability_probe",
            "https://api.openai.com/v1/chat/completions",
            "POST",
        ),
        (
            "anthropic",
            "inference",
            "https://api.anthropic.com/v1/messages",
            "POST",
        ),
        (
            "openai",
            "lifecycle_create",
            "https://api.openai.com/v1/conversations",
            "POST",
        ),
    ],
)
def test_registry_resolves_fixed_provider_targets(
    provider: str,
    operation: str,
    expected_url: str,
    expected_method: str,
) -> None:
    """Provider and operation resolve to code-owned method and endpoint data."""

    target = ConnectionOperationRegistry().resolve(operation, provider=provider)

    assert target.url == expected_url
    assert target.method == expected_method
    assert target.provider == provider


def test_registry_validates_lifecycle_resource_id_as_one_path_segment() -> None:
    """Lifecycle deletion accepts an opaque segment but no path injection."""

    registry = ConnectionOperationRegistry()
    target = registry.resolve(
        "lifecycle_delete",
        provider="openai",
        resource_id="conv_ABC-123",
    )
    assert target.url == "https://api.openai.com/v1/conversations/conv_ABC-123"

    for resource_id in ("../admin", "a/b", "", "conv?id=1", "conv%2fother"):
        with pytest.raises(OperationRegistryError):
            registry.resolve(
                "lifecycle_delete",
                provider="openai",
                resource_id=resource_id,
            )


def test_registry_rejects_unknown_operations_and_unsupported_provider_matrix() -> None:
    """Unknown IDs and unsupported provider operations cannot become side paths."""

    registry = ConnectionOperationRegistry()
    with pytest.raises(OperationRegistryError):
        registry.resolve("arbitrary_fetch", provider="openai")
    with pytest.raises(OperationRegistryError):
        registry.resolve("health", provider="custom")
    with pytest.raises(OperationRegistryError):
        registry.resolve("lifecycle_create", provider="anthropic")


def test_registry_and_transport_have_no_raw_url_or_header_inputs() -> None:
    """Services cannot feed arbitrary destinations or headers through the seam."""

    resolve_parameters = inspect.signature(
        ConnectionOperationRegistry.resolve
    ).parameters
    execute_parameters = inspect.signature(GuardedTransport.execute).parameters

    for forbidden in ("url", "endpoint", "headers", "follow_redirects", "proxies"):
        assert forbidden not in resolve_parameters
        assert forbidden not in execute_parameters

    registry = ConnectionOperationRegistry()
    with pytest.raises(TypeError):
        registry.resolve(
            "health",
            provider="openai",
            endpoint="https://attacker.invalid",  # type: ignore[call-arg]
        )
