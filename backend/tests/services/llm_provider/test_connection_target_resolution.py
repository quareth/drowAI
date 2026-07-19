"""Tests for runtime LLM connection target resolution and URL policy."""

from __future__ import annotations

import ast
from collections.abc import Callable
import inspect
from pathlib import Path
import typing
from urllib.parse import urlsplit

import pytest

from backend.services.llm_provider import _connection_target_resolution as resolution
from backend.services.llm_provider._connection_operation_contracts import (
    OperationRegistryError,
)
from backend.services.llm_provider.types import (
    LLMEgressNetworkScope,
    LLMConnectionOperation,
    RegisteredLLMOperationTarget,
)


def test_native_default_https_returns_exact_registered_target() -> None:
    """A native default is read at resolution time and confined to HTTPS 443."""

    env_reads: list[str] = []

    target = _resolve(
        origin_inputs=_native_origin(),
        env_getter=lambda name: env_reads.append(name) or None,
        provider="openai",
        method="GET",
        operation_path="/v1/models",
        operation=LLMConnectionOperation.INVENTORY,
    )

    assert target == RegisteredLLMOperationTarget(
        operation=LLMConnectionOperation.INVENTORY,
        provider="openai",
        method="GET",
        url="https://api.example.test/v1/models",
        client_base_url="https://api.example.test/v1",
        expected_host="api.example.test",
        allowed_ports=frozenset({443}),
        allowed_path_prefixes=("/v1/models",),
        network_scope=LLMEgressNetworkScope.PUBLIC,
    )
    assert env_reads == ["TEST_NATIVE_BASE_URL"]


@pytest.mark.parametrize(
    (
        "override",
        "expected_url",
        "expected_base_url",
        "expected_host",
        "expected_port",
        "expected_scope",
    ),
    (
        (
            "https://gateway.example.test/root",
            "https://gateway.example.test/root/v1/chat/completions",
            "https://gateway.example.test/root/v1",
            "gateway.example.test",
            443,
            LLMEgressNetworkScope.PUBLIC,
        ),
        (
            "https://gateway.example.test:443/team",
            "https://gateway.example.test:443/team/v1/chat/completions",
            "https://gateway.example.test:443/team/v1",
            "gateway.example.test",
            443,
            LLMEgressNetworkScope.PUBLIC,
        ),
        (
            "https://gateway.example.test:8443/root",
            "https://gateway.example.test:8443/root/v1/chat/completions",
            "https://gateway.example.test:8443/root/v1",
            "gateway.example.test",
            8443,
            LLMEgressNetworkScope.PUBLIC,
        ),
        (
            "http://localhost",
            "http://localhost/v1/chat/completions",
            "http://localhost/v1",
            "localhost",
            80,
            LLMEgressNetworkScope.LOOPBACK,
        ),
        (
            "http://127.0.0.1:4101/base",
            "http://127.0.0.1:4101/base/v1/chat/completions",
            "http://127.0.0.1:4101/base/v1",
            "127.0.0.1",
            4101,
            LLMEgressNetworkScope.LOOPBACK,
        ),
        (
            "http://[::1]:4102/v1",
            "http://[::1]:4102/v1/chat/completions",
            "http://[::1]:4102/v1",
            "::1",
            4102,
            LLMEgressNetworkScope.LOOPBACK,
        ),
    ),
)
def test_native_operator_overrides_preserve_origin_confinement(
    override: str,
    expected_url: str,
    expected_base_url: str,
    expected_host: str,
    expected_port: int,
    expected_scope: LLMEgressNetworkScope,
) -> None:
    """Operator overrides preserve public HTTPS and loopback-only HTTP policy."""

    target = _resolve(
        origin_inputs=_native_origin(),
        env_getter={"TEST_NATIVE_BASE_URL": override}.get,
    )

    assert target.url == expected_url
    assert target.client_base_url == expected_base_url
    assert target.expected_host == expected_host
    assert target.allowed_ports == frozenset({expected_port})
    assert target.allowed_path_prefixes == (urlsplit(expected_url).path,)
    assert target.network_scope is expected_scope


def test_fixed_preset_uses_fixed_origin_after_runtime_environment_read() -> None:
    """A fixed preset checks its override before falling back to reviewed HTTPS."""

    env_reads: list[str] = []

    target = _resolve(
        origin_inputs=_fixed_preset_origin(),
        env_getter=lambda name: env_reads.append(name) or None,
        provider="fixed_preset",
    )

    assert target == RegisteredLLMOperationTarget(
        operation=LLMConnectionOperation.INFERENCE,
        provider="fixed_preset",
        method="POST",
        url="https://fixed.example.test/v1/chat/completions",
        client_base_url="https://fixed.example.test/v1",
        expected_host="fixed.example.test",
        allowed_ports=frozenset({443}),
        allowed_path_prefixes=("/v1/chat/completions",),
        network_scope=LLMEgressNetworkScope.PUBLIC,
    )
    assert env_reads == ["TEST_FIXED_PRESET_BASE_URL"]


def test_proving_preset_uses_injected_runtime_origin() -> None:
    """A proving preset reads its required endpoint only during resolution."""

    env_reads: list[str] = []

    target = _resolve(
        origin_inputs=_proving_preset_origin(),
        env_getter=lambda name: env_reads.append(name)
        or "https://proving.example.test/service",
        provider="proving_preset",
    )

    assert target.url == "https://proving.example.test/service/v1/chat/completions"
    assert target.client_base_url == "https://proving.example.test/service/v1"
    assert target.expected_host == "proving.example.test"
    assert target.allowed_ports == frozenset({443})
    assert target.allowed_path_prefixes == ("/service/v1/chat/completions",)
    assert target.network_scope is LLMEgressNetworkScope.PUBLIC
    assert env_reads == ["TEST_PROVING_BASE_URL"]


def test_configurable_preset_uses_caller_https_without_environment_read() -> None:
    """A configurable preset validates only its caller-supplied HTTPS origin."""

    env_reads: list[str] = []

    target = _resolve(
        origin_inputs=_configurable_preset_origin(),
        env_getter=lambda name: env_reads.append(name) or None,
        base_url="https://tenant.example.test/team",
        provider="configurable_preset",
    )

    assert target.url == "https://tenant.example.test/team/v1/chat/completions"
    assert target.client_base_url == "https://tenant.example.test/team/v1"
    assert target.expected_host == "tenant.example.test"
    assert target.allowed_ports == frozenset({443})
    assert target.allowed_path_prefixes == ("/team/v1/chat/completions",)
    assert target.network_scope is LLMEgressNetworkScope.PUBLIC
    assert env_reads == []


@pytest.mark.parametrize(
    ("base_url", "expected_client_base_url", "expected_url"),
    (
        (
            "https://tenant.example.test",
            "https://tenant.example.test/v1",
            "https://tenant.example.test/v1/chat/completions",
        ),
        (
            "https://tenant.example.test/v1",
            "https://tenant.example.test/v1",
            "https://tenant.example.test/v1/chat/completions",
        ),
        (
            "https://tenant.example.test/team/v1",
            "https://tenant.example.test/team/v1",
            "https://tenant.example.test/team/v1/chat/completions",
        ),
        (
            "https://tenant.example.test/team",
            "https://tenant.example.test/team/v1",
            "https://tenant.example.test/team/v1/chat/completions",
        ),
    ),
)
def test_client_base_path_is_appended_exactly_once(
    base_url: str,
    expected_client_base_url: str,
    expected_url: str,
) -> None:
    """Reviewed client paths already present in an origin are not duplicated."""

    target = _resolve(
        origin_inputs=_configurable_preset_origin(),
        base_url=base_url,
    )

    assert target.client_base_url == expected_client_base_url
    assert target.url == expected_url


def test_formatted_lifecycle_operation_path_is_composed_unchanged() -> None:
    """The resolver composes a facade-formatted path without reformatting it."""

    target = _resolve(
        origin_inputs=_configurable_preset_origin(),
        base_url="https://tenant.example.test/v1",
        operation=LLMConnectionOperation.LIFECYCLE_DELETE,
        method="DELETE",
        operation_path="/v1/conversations/conv_ABC-123",
    )

    assert target.url == "https://tenant.example.test/v1/conversations/conv_ABC-123"
    assert target.allowed_path_prefixes == ("/v1/conversations/conv_ABC-123",)


@pytest.mark.parametrize(
    ("override", "expected_message", "expected_cause"),
    (
        (
            "http://provider.example.test:4000",
            "Provider operator base URL violates policy",
            None,
        ),
        (
            "http://10.0.0.1:4000",
            "Provider operator base URL violates policy",
            None,
        ),
        (
            "http://169.254.169.254:4000",
            "Provider operator base URL violates policy",
            None,
        ),
        (
            "ftp://127.0.0.1:4000",
            "Provider operator base URL violates policy",
            None,
        ),
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
        (
            " http://127.0.0.1:4000",
            "Provider operator base URL is invalid",
            None,
        ),
        (
            "http://127.0.0.1:bad",
            "Provider operator base URL is invalid",
            ValueError,
        ),
        (
            "http://127.0.0.1:4000/../admin",
            "Provider operator base URL path violates policy",
            None,
        ),
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
def test_operator_override_rejections_keep_exact_errors(
    override: str,
    expected_message: str,
    expected_cause: type[BaseException] | None,
) -> None:
    """Unsafe operator origins retain exact messages and exception causes."""

    _assert_resolution_error(
        lambda: _resolve(
            origin_inputs=_native_origin(),
            env_getter={"TEST_NATIVE_BASE_URL": override}.get,
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
        (" https://tenant.example.test", "Preset endpoint base URL is not configured", None),
        (
            "http://tenant.example.test",
            "Preset endpoint base URL violates policy",
            None,
        ),
        (
            "https://user:password@tenant.example.test",
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
        (
            "https://tenant.example.test:8443",
            "Preset endpoint base URL violates policy",
            None,
        ),
        (
            "https://tenant.example.test:bad",
            "Preset endpoint base URL is invalid",
            ValueError,
        ),
        (
            "https://tenant.example.test/../admin",
            "Preset endpoint base URL path violates policy",
            None,
        ),
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
def test_configurable_origin_rejections_keep_exact_errors(
    base_url: str | None,
    expected_message: str,
    expected_cause: type[BaseException] | None,
) -> None:
    """Unsafe caller endpoints retain exact messages and exception causes."""

    _assert_resolution_error(
        lambda: _resolve(
            origin_inputs=_configurable_preset_origin(),
            base_url=base_url,
        ),
        expected_message,
        expected_cause,
    )


def test_fixed_origin_rejections_do_not_read_environment() -> None:
    """Caller URLs are rejected before fixed-origin environment lookup."""

    for origin_inputs, expected_message in (
        (_native_origin(), "Fixed provider target does not accept a base URL"),
        (_fixed_preset_origin(), "Fixed preset target does not accept a base URL"),
    ):
        env_reads: list[str] = []
        _assert_resolution_error(
            lambda origin_inputs=origin_inputs: _resolve(
                origin_inputs=origin_inputs,
                env_getter=lambda name: env_reads.append(name) or None,
                base_url="https://caller.example.test",
            ),
            expected_message,
        )
        assert env_reads == []


def test_missing_proving_origin_reads_environment_once_and_fails_closed() -> None:
    """A missing proving endpoint fails after one injected environment read."""

    env_reads: list[str] = []

    _assert_resolution_error(
        lambda: _resolve(
            origin_inputs=_proving_preset_origin(),
            env_getter=lambda name: env_reads.append(name) or None,
        ),
        "Proving endpoint base URL is not configured",
    )
    assert env_reads == ["TEST_PROVING_BASE_URL"]


def test_resolver_imports_and_types_enforce_the_internal_boundary() -> None:
    """The resolver has no facade import or untyped operation-definition seam."""

    source = Path(resolution.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    referenced_names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }

    assert "backend.services.llm_provider.operation_registry" not in imported_modules
    assert "backend.services.llm_provider._connection_preset_catalog" not in imported_modules
    assert imported_modules.isdisjoint({"httpx", "os", "requests", "socket", "urllib.request"})
    assert "_OperationDefinition" not in referenced_names
    assert "Any" not in referenced_names

    signature = inspect.signature(resolution._resolve_connection_operation_target)
    assert tuple(signature.parameters) == (
        "operation",
        "provider",
        "method",
        "operation_path",
        "origin_inputs",
        "env_getter",
        "base_url",
    )
    hints = typing.get_type_hints(resolution._resolve_connection_operation_target)
    assert hints["method"] is str
    assert hints["operation_path"] is str
    assert hints["origin_inputs"] == (
        resolution._NativeEndpointOriginInputs | resolution._PresetOriginInputs
    )
    assert all(not _contains_any(annotation) for annotation in hints.values())


def _resolve(
    *,
    origin_inputs: object,
    env_getter: Callable[[str], str | None] = lambda _name: None,
    base_url: str | None = None,
    operation: LLMConnectionOperation = LLMConnectionOperation.INFERENCE,
    provider: str = "test_provider",
    method: str = "POST",
    operation_path: str = "/v1/chat/completions",
) -> RegisteredLLMOperationTarget:
    return resolution._resolve_connection_operation_target(
        operation,
        provider,
        method=method,
        operation_path=operation_path,
        origin_inputs=origin_inputs,
        env_getter=env_getter,
        base_url=base_url,
    )


def _native_origin() -> object:
    return resolution._NativeEndpointOriginInputs(
        default_base_url="https://api.example.test",
        base_url_env="TEST_NATIVE_BASE_URL",
        client_base_path="/v1",
    )


def _fixed_preset_origin() -> object:
    return resolution._PresetOriginInputs(
        fixed_base_url="https://fixed.example.test",
        base_url_env="TEST_FIXED_PRESET_BASE_URL",
        endpoint_config_field=None,
        client_base_path="/v1",
    )


def _proving_preset_origin() -> object:
    return resolution._PresetOriginInputs(
        fixed_base_url=None,
        base_url_env="TEST_PROVING_BASE_URL",
        endpoint_config_field=None,
        client_base_path="/v1",
    )


def _configurable_preset_origin() -> object:
    return resolution._PresetOriginInputs(
        fixed_base_url=None,
        base_url_env=None,
        endpoint_config_field="base_url",
        client_base_path="/v1",
    )


def _assert_resolution_error(
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


def _contains_any(annotation: object) -> bool:
    return annotation is typing.Any or any(
        _contains_any(argument) for argument in typing.get_args(annotation)
    )
