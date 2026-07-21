"""Runtime target resolution for reviewed LLM connection origins.

This module owns endpoint validation, origin classification, reviewed path
composition, and registered target construction only; it does not load
manifests, select operation rows, validate resource IDs, or import the facade.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit, urlunsplit

from backend.services.llm_provider._connection_operation_contracts import (
    OperationRegistryError,
    _valid_base_path,
)
from backend.services.llm_provider.types import (
    LLMEgressNetworkScope,
    LLMConnectionOperation,
    RegisteredLLMOperationTarget,
)

_HTTP_SCHEME = "http"
_HTTPS_SCHEME = "https"
_HTTP_DEFAULT_PORT = 80
_HTTPS_DEFAULT_PORT = 443
_LOOPBACK_HOSTNAME = "localhost"

EnvGetter = Callable[[str], str | None]


@dataclass(frozen=True, slots=True)
class _NativeEndpointOriginInputs:
    """Facade-selected native endpoint primitives for one target resolution."""

    default_base_url: str
    base_url_env: str
    client_base_path: str


@dataclass(frozen=True, slots=True)
class _PresetOriginInputs:
    """Facade-selected preset endpoint primitives for one target resolution."""

    fixed_base_url: str | None
    base_url_env: str | None
    endpoint_config_field: str | None
    client_base_path: str


@dataclass(frozen=True, slots=True)
class _ResolvedOperationOrigin:
    """Validated operation origin plus its exact network confinement."""

    base_url: str
    client_base_path: str
    port: int
    network_scope: LLMEgressNetworkScope


def _resolve_connection_operation_target(
    operation: LLMConnectionOperation,
    provider: str,
    *,
    method: str,
    operation_path: str,
    origin_inputs: _NativeEndpointOriginInputs | _PresetOriginInputs,
    env_getter: EnvGetter,
    base_url: str | None,
) -> RegisteredLLMOperationTarget:
    """Resolve a facade-selected operation and origin into a confined target."""

    origin = _origin_for(
        origin_inputs,
        env_getter=env_getter,
        base_url=base_url,
    )
    client_base_url = _join_declared_base_path(
        origin.base_url,
        origin.client_base_path,
    )
    relative_operation_path = _operation_path_relative_to_client_base(
        operation_path,
        origin.client_base_path,
    )
    url = _join_origin_path(client_base_url, relative_operation_path)
    parsed = urlsplit(url)
    return RegisteredLLMOperationTarget(
        operation=operation,
        provider=provider,
        method=method,
        url=url,
        client_base_url=client_base_url,
        expected_host=str(parsed.hostname or ""),
        allowed_ports=frozenset({origin.port}),
        allowed_path_prefixes=(parsed.path,),
        network_scope=origin.network_scope,
    )


def _origin_for(
    origin_inputs: _NativeEndpointOriginInputs | _PresetOriginInputs,
    *,
    env_getter: EnvGetter,
    base_url: str | None,
) -> _ResolvedOperationOrigin:
    """Resolve one facade-selected native endpoint or preset origin."""

    if isinstance(origin_inputs, _NativeEndpointOriginInputs):
        if base_url is not None:
            raise OperationRegistryError("Fixed provider target does not accept a base URL")
        operator_override = env_getter(origin_inputs.base_url_env)
        origin = (
            _validated_operator_base_url(operator_override)
            if operator_override
            else _public_https_origin(
                origin_inputs.default_base_url,
                "Native provider target",
            )
        )
        return _with_client_base_path(origin, origin_inputs.client_base_path)

    if origin_inputs.endpoint_config_field is not None:
        return _with_client_base_path(
            _public_https_origin(
                _validated_https_base_url(base_url, "Preset endpoint base URL"),
                "Preset endpoint base URL",
            ),
            origin_inputs.client_base_path,
        )
    if base_url is not None:
        raise OperationRegistryError("Fixed preset target does not accept a base URL")

    operator_override = (
        env_getter(origin_inputs.base_url_env)
        if origin_inputs.base_url_env is not None
        else None
    )
    if operator_override:
        return _with_client_base_path(
            _validated_operator_base_url(operator_override),
            origin_inputs.client_base_path,
        )
    if origin_inputs.fixed_base_url is not None:
        return _with_client_base_path(
            _public_https_origin(
                origin_inputs.fixed_base_url,
                "Preset fixed base URL",
            ),
            origin_inputs.client_base_path,
        )
    if origin_inputs.base_url_env is None:
        raise OperationRegistryError("Preset has no endpoint target")
    return _with_client_base_path(
        _public_https_origin(
            operator_override,
            "Proving endpoint base URL",
        ),
        origin_inputs.client_base_path,
    )


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
