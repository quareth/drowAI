"""Tests for the import-time LLM connection preset catalog."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
from types import MappingProxyType

import pytest

from backend.services.llm_provider import _connection_preset_catalog as catalog
from backend.services.llm_provider._connection_operation_contracts import (
    OperationRegistryError,
)


def test_valid_manifest_loads_immutable_native_endpoints_and_presets(
    tmp_path: Path,
) -> None:
    """A valid temporary manifest produces the reviewed immutable catalog DTOs."""

    manifest = _valid_manifest()
    loaded = catalog._load_connection_presets_manifest(
        _write_manifest(tmp_path, manifest)
    )

    assert tuple(endpoint.provider_id for endpoint in loaded.native_provider_endpoints) == (
        "openai",
        "anthropic",
    )
    assert tuple(preset.id for preset in loaded.presets) == _REQUIRED_PRESET_IDS
    assert loaded.native_provider_endpoints[0].default_base_url == (
        "https://api.openai.com"
    )
    assert loaded.presets[0].auth_schema == {
        "mode": "bearer_api_key",
        "secret_fields": ("api_key",),
    }
    assert loaded.presets[0].capability_ceiling == frozenset(
        {
            catalog.LLMCapability.CHAT,
            catalog.LLMCapability.STREAMING,
            catalog.LLMCapability.TOOLS,
        }
    )

    with pytest.raises(FrozenInstanceError):
        loaded.presets[0].display_name = "changed"


def test_default_catalog_constructs_immutable_import_time_mappings() -> None:
    """Importing the catalog validates the checked-in manifest and freezes maps."""

    assert type(catalog._NATIVE_PROVIDER_ENDPOINTS) is MappingProxyType
    assert type(catalog._CONNECTION_PRESETS) is MappingProxyType
    assert type(catalog._PROVING_PRESETS) is MappingProxyType
    assert set(catalog._NATIVE_PROVIDER_ENDPOINTS) == {"openai", "anthropic"}
    assert set(catalog._PROVING_PRESETS) == {catalog.GPT_OSS_20B_PROVING_PRESET_ID}

    with pytest.raises(TypeError):
        catalog._CONNECTION_PRESETS["new"] = catalog._CONNECTION_PRESETS[
            catalog.GPT_OSS_20B_PROVING_PRESET_ID
        ]


@pytest.mark.parametrize(
    ("path_builder", "expected_message", "expected_cause"),
    (
        (
            lambda tmp_path: tmp_path,
            "Unable to read connection preset manifest",
            IsADirectoryError,
        ),
        (
            lambda tmp_path: _write_text(tmp_path / "manifest.json", "{not-json"),
            "is not valid JSON",
            json.JSONDecodeError,
        ),
    ),
)
def test_manifest_file_errors_keep_exact_messages_and_causes(
    tmp_path: Path,
    path_builder: object,
    expected_message: str,
    expected_cause: type[BaseException],
) -> None:
    """File and JSON failures keep the catalog's public error contract."""

    path = path_builder(tmp_path)

    with pytest.raises(OperationRegistryError) as exc_info:
        catalog._load_connection_presets_manifest(path)

    assert expected_message in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, expected_cause)


@pytest.mark.parametrize(
    ("mutator", "expected_message"),
    (
        (lambda manifest: [], "Connection preset manifest must be a JSON object"),
        (
            lambda manifest: _set(manifest, "schema_version", 999),
            "Unsupported connection preset manifest schema",
        ),
        (
            lambda manifest: _set(manifest, "native_provider_endpoints", []),
            "Connection preset manifest requires native provider endpoints",
        ),
        (
            lambda manifest: _set(manifest, "presets", []),
            "Connection preset manifest requires presets",
        ),
        (
            lambda manifest: _set(
                manifest,
                "native_provider_endpoints",
                [
                    manifest["native_provider_endpoints"][0],
                    manifest["native_provider_endpoints"][0],
                ],
            ),
            "Connection preset manifest contains duplicate native providers",
        ),
        (
            lambda manifest: _set(
                manifest["native_provider_endpoints"][0],
                "provider_id",
                "openai-compatible",
            ),
            "Connection preset manifest must define reviewed native providers",
        ),
        (
            lambda manifest: _set(
                manifest,
                "presets",
                [manifest["presets"][0], manifest["presets"][0]],
            ),
            "Connection preset manifest contains duplicate IDs",
        ),
        (
            lambda manifest: _set(manifest["presets"][0], "proving", False),
            "Connection preset manifest must define one proving preset",
        ),
        (
            lambda manifest: _set(manifest["presets"][1], "id", "unreviewed"),
            "Connection preset manifest is missing reviewed presets",
        ),
    ),
)
def test_manifest_shape_and_required_id_rejections(
    tmp_path: Path,
    mutator: object,
    expected_message: str,
) -> None:
    """Manifest-level shape and required-ID failures are fail-closed."""

    manifest = _mutated_manifest(mutator)

    _assert_manifest_error(tmp_path, manifest, expected_message)


@pytest.mark.parametrize(
    ("field", "value", "expected_message", "expected_cause"),
    (
        ("adapter_id", "unsupported", "Connection preset adapter is not supported", None),
        (
            "adapter_version",
            "999",
            "Connection preset adapter version is not supported",
            None,
        ),
        (
            "dialect_policy_id",
            "missing_policy",
            "Connection preset dialect policy is not supported",
            catalog.LLMConfigurationError,
        ),
        (
            "api_surface",
            "responses",
            "Connection preset API surface is not supported",
            None,
        ),
        (
            "capability_ceiling",
            ["reasoning_effort"],
            "Connection preset capability ceiling is not supported",
            None,
        ),
        (
            "endpoint_policy_id",
            "unknown_policy",
            "Connection preset endpoint policy is not supported",
            None,
        ),
        (
            "discovery_strategy",
            "manual",
            "Connection preset discovery strategy is not supported",
            None,
        ),
        ("auth_mode", "none", "Connection preset auth mode is not supported", None),
    ),
)
def test_unsupported_preset_contract_values_are_rejected(
    tmp_path: Path,
    field: str,
    value: object,
    expected_message: str,
    expected_cause: type[BaseException] | None,
) -> None:
    """Unsupported adapter, dialect, capability, endpoint, and auth data fails."""

    manifest = _valid_manifest()
    manifest["presets"][1][field] = value

    _assert_manifest_error(tmp_path, manifest, expected_message, expected_cause)


@pytest.mark.parametrize(
    ("mutator", "expected_message", "expected_cause"),
    (
        (
            lambda manifest: _set(manifest["native_provider_endpoints"][0], "provider_id", ""),
            "Connection preset field 'provider_id' is invalid",
            None,
        ),
        (
            lambda manifest: _set(manifest["presets"][1], "display_name", " Hugging Face"),
            "Connection preset field 'display_name' is invalid",
            None,
        ),
        (
            lambda manifest: _set(manifest["presets"][1], "fixed_base_url", 42),
            "Connection preset field 'fixed_base_url' is invalid",
            None,
        ),
        (
            lambda manifest: _set(manifest["presets"][1], "base_url_env", ""),
            "Connection preset field 'base_url_env' is invalid",
            None,
        ),
        (
            lambda manifest: _set(manifest["presets"][1], "capability_ceiling", []),
            "Connection preset field 'capability_ceiling' is invalid",
            None,
        ),
        (
            lambda manifest: _set(
                manifest["presets"][1],
                "capability_ceiling",
                ["chat", "chat"],
            ),
            "Connection preset field 'capability_ceiling' has duplicates",
            None,
        ),
        (
            lambda manifest: _set(manifest["presets"][1], "secret_fields", ["api_key", "api_key"]),
            "Connection preset field 'secret_fields' has duplicates",
            None,
        ),
        (
            lambda manifest: _set(manifest["presets"][1], "secret_fields", []),
            "Connection preset field 'secret_fields' is invalid",
            None,
        ),
        (
            lambda manifest: _set(manifest["presets"][1], "client_base_path", "/v1/"),
            "Connection preset field 'client_base_path' is invalid",
            None,
        ),
        (
            lambda manifest: _set(manifest["presets"][1], "client_base_path", "/a//b"),
            "Connection preset field 'client_base_path' is invalid",
            None,
        ),
        (
            lambda manifest: _set(manifest["presets"][1], "fixed_base_url", "https://host:bad"),
            "Connection preset fixed endpoint is invalid",
            ValueError,
        ),
        (
            lambda manifest: _set(manifest["presets"][1], "fixed_base_url", "http://host"),
            "Connection preset fixed endpoint violates policy",
            None,
        ),
        (
            lambda manifest: _set(manifest["presets"][1], "endpoint_config_field", "base_url"),
            "Fixed connection preset cannot accept endpoints",
            None,
        ),
        (
            lambda manifest: _set(manifest["presets"][3], "fixed_base_url", "https://host"),
            "User endpoint preset must declare base_url only",
            None,
        ),
        (
            lambda manifest: _set(manifest["presets"][3], "base_url_env", "OLLAMA_BASE_URL"),
            "User endpoint preset must declare base_url only",
            None,
        ),
        (
            lambda manifest: _set(manifest["presets"][3], "endpoint_config_field", "url"),
            "User endpoint preset must declare base_url only",
            None,
        ),
    ),
)
def test_preset_field_validation_and_policy_mismatches(
    tmp_path: Path,
    mutator: object,
    expected_message: str,
    expected_cause: type[BaseException] | None,
) -> None:
    """Field validation, endpoint policy, and URL checks preserve exact errors."""

    manifest = _mutated_manifest(mutator)

    _assert_manifest_error(tmp_path, manifest, expected_message, expected_cause)


def test_catalog_does_not_import_target_resolution_or_read_environment() -> None:
    """The catalog remains independent of runtime target resolution."""

    source = Path(catalog.__file__).read_text(encoding="utf-8")
    assert "_connection_target_resolution" not in source
    assert "operation_registry" not in source
    assert "os.environ" not in source
    assert "getenv" not in source


_REQUIRED_PRESET_IDS = (
    catalog.GPT_OSS_20B_PROVING_PRESET_ID,
    catalog.HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    catalog.NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    catalog.OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
    catalog.VLLM_OPENAI_COMPATIBLE_PRESET_ID,
    catalog.CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
)


def _valid_manifest() -> dict[str, object]:
    return {
        "schema_version": 2,
        "native_provider_endpoints": [
            _native_endpoint("openai", "https://api.openai.com", "OPENAI_BASE_URL", "/v1"),
            _native_endpoint(
                "anthropic",
                "https://api.anthropic.com",
                "ANTHROPIC_BASE_URL",
                "",
            ),
        ],
        "presets": [
            _preset(
                catalog.GPT_OSS_20B_PROVING_PRESET_ID,
                "GPT-OSS 20B OpenAI-compatible proving",
                fixed_base_url=None,
                endpoint_config_field=None,
                user_config_fields=["display_label", "api_key"],
                base_url_env=catalog.GPT_OSS_20B_PROVING_BASE_URL_ENV,
                proving=True,
            ),
            _preset(
                catalog.HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
                "Hugging Face",
                fixed_base_url="https://router.huggingface.co",
                endpoint_config_field=None,
                user_config_fields=["display_label", "api_key"],
                base_url_env=catalog.HUGGINGFACE_BASE_URL_ENV,
            ),
            _preset(
                catalog.NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
                "NVIDIA",
                fixed_base_url="https://integrate.api.nvidia.com",
                endpoint_config_field=None,
                user_config_fields=["display_label", "api_key"],
                base_url_env=catalog.NVIDIA_NIM_BASE_URL_ENV,
            ),
            _preset(
                catalog.OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
                "Ollama",
                fixed_base_url=None,
                endpoint_config_field="base_url",
                user_config_fields=["display_label", "base_url", "api_key"],
            ),
            _preset(
                catalog.VLLM_OPENAI_COMPATIBLE_PRESET_ID,
                "vLLM",
                fixed_base_url=None,
                endpoint_config_field="base_url",
                user_config_fields=["display_label", "base_url", "api_key"],
            ),
            _preset(
                catalog.CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
                "Custom OpenAI-compatible HTTPS endpoint",
                fixed_base_url=None,
                endpoint_config_field="base_url",
                user_config_fields=["display_label", "base_url", "api_key"],
                dialect_policy_id="openai_compatible_chat.conservative_v1",
                capability_ceiling=["chat", "streaming"],
            ),
        ],
    }


def _native_endpoint(
    provider_id: str,
    default_base_url: str,
    base_url_env: str,
    client_base_path: str,
) -> dict[str, str]:
    return {
        "provider_id": provider_id,
        "default_base_url": default_base_url,
        "base_url_env": base_url_env,
        "client_base_path": client_base_path,
    }


def _preset(
    preset_id: str,
    display_name: str,
    *,
    fixed_base_url: str | None,
    endpoint_config_field: str | None,
    user_config_fields: list[str],
    base_url_env: str | None = None,
    dialect_policy_id: str = "openai_compatible_chat.agent_v1",
    capability_ceiling: list[str] | None = None,
    proving: bool = False,
) -> dict[str, object]:
    endpoint_policy_id = (
        catalog.FIXED_PROVIDER_ENDPOINT_POLICY_ID
        if endpoint_config_field is None
        else catalog.USER_HTTPS_BASE_URL_ENDPOINT_POLICY_ID
    )
    return {
        "id": preset_id,
        "display_name": display_name,
        "canonical_model_id": "openai/gpt-oss-20b",
        "exact_wire_model_id": "openai/gpt-oss-20b",
        "runtime_family_id": "openai_compatible_chat",
        "serving_operator_id": "reviewed_operator",
        "adapter_id": catalog.OPENAI_COMPATIBLE_CHAT_ADAPTER_ID,
        "adapter_version": catalog.OPENAI_COMPATIBLE_CHAT_ADAPTER_VERSION,
        "api_surface": "chat_completions",
        "dialect_policy_id": dialect_policy_id,
        "capability_ceiling": capability_ceiling or ["chat", "streaming", "tools"],
        "endpoint_policy_id": endpoint_policy_id,
        "discovery_strategy": "openai_models_endpoint",
        "auth_mode": "bearer_api_key",
        "secret_fields": ["api_key"],
        "user_config_fields": user_config_fields,
        "fixed_base_url": fixed_base_url,
        "endpoint_config_field": endpoint_config_field,
        "client_base_path": "/v1",
        "billing_provider_id": None,
        "base_url_env": base_url_env,
        "proving": proving,
    }


def _write_manifest(tmp_path: Path, manifest: object) -> Path:
    return _write_text(tmp_path / "manifest.json", json.dumps(manifest))


def _write_text(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _set(target: object, key: str, value: object) -> object:
    target[key] = value
    return target


def _mutated_manifest(mutator: object) -> object:
    manifest = deepcopy(_valid_manifest())
    result = mutator(manifest)
    return manifest if result is not manifest and isinstance(result, dict) else result


def _assert_manifest_error(
    tmp_path: Path,
    manifest: object,
    expected_message: str,
    expected_cause: type[BaseException] | None = None,
) -> None:
    with pytest.raises(OperationRegistryError) as exc_info:
        catalog._load_connection_presets_manifest(_write_manifest(tmp_path, manifest))
    assert str(exc_info.value) == expected_message
    if expected_cause is None:
        assert exc_info.value.__cause__ is None
    else:
        assert isinstance(exc_info.value.__cause__, expected_cause)
