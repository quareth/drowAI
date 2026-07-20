"""Bounded transport for code-owned LLM connection operations.

The transport resolves endpoints through the immutable operation registry,
revalidates public DNS, disables redirects and proxy inheritance, applies typed
provider authentication, and returns bounded bodies with sanitized failures.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
from time import monotonic
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

import httpx
import requests

from .egress_policy import EgressPolicyError, FixedProviderEgressPolicy
from .operation_registry import (
    ConnectionOperationRegistry,
    OperationRegistryError,
)
from .types import (
    GuardedEgressBounds,
    GuardedEgressTimeouts,
    GuardedHTTPResponse,
    LLMConnectionOperation,
    ProviderSecret,
    RegisteredLLMOperationTarget,
)


class _SessionLike(Protocol):
    """Minimal requests-compatible session contract used by guarded transport."""

    trust_env: bool

    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...

    def close(self) -> None: ...


SessionFactory = Callable[[], _SessionLike]
AsyncClientFactory = Callable[..., httpx.AsyncClient]
_DEFAULT_INFERENCE_TIMEOUTS = GuardedEgressTimeouts(
    connect_seconds=5.0,
    read_seconds=120.0,
    total_seconds=300.0,
)


class GuardedTransportError(RuntimeError):
    """Sanitized guarded transport failure with an opaque correlation ID."""

    def __init__(
        self,
        message: str,
        *,
        audit_id: str,
        status_code: int | None = None,
    ) -> None:
        self.audit_id = audit_id
        self.status_code = status_code
        super().__init__(f"{message} (audit_id={audit_id})")


class GuardedTransport:
    """Execute only registered provider operations through fixed secure controls."""

    def __init__(
        self,
        *,
        registry: ConnectionOperationRegistry | None = None,
        egress_policy: FixedProviderEgressPolicy | None = None,
        session_factory: SessionFactory | None = None,
        timeouts: GuardedEgressTimeouts | None = None,
        bounds: GuardedEgressBounds | None = None,
    ) -> None:
        self._registry = registry or ConnectionOperationRegistry()
        self._egress_policy = egress_policy or FixedProviderEgressPolicy()
        self._session_factory = session_factory or requests.Session
        self._timeouts = timeouts or GuardedEgressTimeouts()
        self._bounds = bounds or GuardedEgressBounds()

    def execute(
        self,
        operation: LLMConnectionOperation | str,
        *,
        provider: str,
        secret: ProviderSecret,
        resource_id: str | None = None,
        json_body: Mapping[str, Any] | None = None,
        operation_target: RegisteredLLMOperationTarget | None = None,
    ) -> GuardedHTTPResponse:
        """Execute a registered operation without accepting raw URLs or headers."""

        audit_id = uuid4().hex
        started_at = monotonic()
        response: Any = None
        session: _SessionLike | None = None
        try:
            target = operation_target or self._registry.resolve(
                operation,
                provider=provider,
                resource_id=resource_id,
            )
            _validate_operation_target(
                target,
                operation=operation,
                provider=provider,
                resource_id=resource_id,
                audit_id=audit_id,
            )
            _validate_secret(
                secret,
                expected_provider=target.provider,
                audit_id=audit_id,
            )
            _validate_request_body(json_body, bounds=self._bounds)
            validated_target = self._egress_policy.validate_endpoint(
                target.url,
                expected_host=target.expected_host,
                allowed_ports=target.allowed_ports,
                allowed_path_prefixes=target.allowed_path_prefixes,
                network_scope=target.network_scope,
            )
            self._egress_policy.revalidate(validated_target)

            session = self._session_factory()
            session.trust_env = False
            response = session.request(
                target.method,
                validated_target.url,
                headers=_provider_headers(target.provider, secret.value, json_body),
                json=json_body,
                allow_redirects=False,
                timeout=(
                    self._timeouts.connect_seconds,
                    self._timeouts.read_seconds,
                ),
                stream=True,
                verify=True,
            )
            _require_total_duration(started_at, self._timeouts.total_seconds)
            _validate_response_status(response.status_code, audit_id=audit_id)
            _validate_headers(response.headers, self._bounds, audit_id=audit_id)
            body = _read_bounded_body(
                response,
                bounds=self._bounds,
                started_at=started_at,
                total_seconds=self._timeouts.total_seconds,
                audit_id=audit_id,
            )
            return GuardedHTTPResponse(
                status_code=int(response.status_code),
                body=body,
                audit_id=audit_id,
            )
        except GuardedTransportError:
            raise
        except (EgressPolicyError, OperationRegistryError, requests.RequestException):
            raise GuardedTransportError(
                "Guarded outbound operation failed",
                audit_id=audit_id,
            ) from None
        except Exception:
            raise GuardedTransportError(
                "Guarded outbound operation failed",
                audit_id=audit_id,
            ) from None
        finally:
            cleanup_failed = False
            if response is not None:
                try:
                    response.close()
                except Exception:
                    cleanup_failed = True
            if session is not None:
                try:
                    session.close()
                except Exception:
                    cleanup_failed = True
            if cleanup_failed:
                raise GuardedTransportError(
                    "Guarded transport cleanup failed",
                    audit_id=audit_id,
                ) from None


class GuardedAsyncInferenceTransport:
    """Run one authorized inference target without blocking or buffering SSE."""

    def __init__(
        self,
        *,
        operation_target: RegisteredLLMOperationTarget,
        secret: ProviderSecret,
        egress_policy: FixedProviderEgressPolicy | None = None,
        client_factory: AsyncClientFactory | None = None,
        timeouts: GuardedEgressTimeouts | None = None,
        bounds: GuardedEgressBounds | None = None,
    ) -> None:
        self._operation_target = operation_target
        self._secret = secret
        self._egress_policy = egress_policy or FixedProviderEgressPolicy()
        self._client_factory = client_factory or httpx.AsyncClient
        self._timeouts = timeouts or _DEFAULT_INFERENCE_TIMEOUTS
        self._bounds = bounds or GuardedEgressBounds()

    async def request_json(self, json_body: Mapping[str, Any]) -> Any:
        """Return one bounded decoded response while yielding to the event loop."""

        audit_id = uuid4().hex
        started_at = monotonic()
        try:
            url, headers = self._prepare_request(json_body, audit_id=audit_id)
            async with self._client() as client:
                async with asyncio.timeout(self._timeouts.total_seconds):
                    async with client.stream(
                        self._operation_target.method,
                        url,
                        headers=headers,
                        json=json_body,
                    ) as response:
                        _validate_response_status(response.status_code, audit_id=audit_id)
                        _validate_headers(response.headers, self._bounds, audit_id=audit_id)
                        body = await _read_bounded_async_body(
                            response,
                            bounds=self._bounds,
                            started_at=started_at,
                            total_seconds=self._timeouts.total_seconds,
                            audit_id=audit_id,
                        )
            return json.loads(body.decode("utf-8"))
        except GuardedTransportError:
            raise
        except TimeoutError:
            raise GuardedTransportError(
                "Guarded operation timed out",
                audit_id=audit_id,
            ) from None
        except (EgressPolicyError, httpx.HTTPError, UnicodeError, ValueError):
            raise GuardedTransportError(
                "Guarded outbound operation failed",
                audit_id=audit_id,
            ) from None
        except Exception:
            raise GuardedTransportError(
                "Guarded outbound operation failed",
                audit_id=audit_id,
            ) from None

    async def stream_json_events(
        self,
        json_body: Mapping[str, Any],
    ) -> AsyncIterator[Any]:
        """Yield each bounded SSE JSON event immediately after it arrives."""

        audit_id = uuid4().hex
        started_at = monotonic()
        try:
            url, headers = self._prepare_request(json_body, audit_id=audit_id)
            async with self._client() as client:
                async with client.stream(
                    self._operation_target.method,
                    url,
                    headers=headers,
                    json=json_body,
                ) as response:
                    _validate_response_status(response.status_code, audit_id=audit_id)
                    _validate_headers(response.headers, self._bounds, audit_id=audit_id)
                    async for event in _iter_bounded_sse_json(
                        response,
                        bounds=self._bounds,
                        started_at=started_at,
                        total_seconds=self._timeouts.total_seconds,
                        audit_id=audit_id,
                    ):
                        yield event
        except GuardedTransportError:
            raise
        except TimeoutError:
            raise GuardedTransportError(
                "Guarded operation timed out",
                audit_id=audit_id,
            ) from None
        except (EgressPolicyError, httpx.HTTPError, UnicodeError, ValueError):
            raise GuardedTransportError(
                "Guarded outbound operation failed",
                audit_id=audit_id,
            ) from None
        except Exception:
            raise GuardedTransportError(
                "Guarded outbound operation failed",
                audit_id=audit_id,
            ) from None

    def _prepare_request(
        self,
        json_body: Mapping[str, Any],
        *,
        audit_id: str,
    ) -> tuple[str, dict[str, str]]:
        """Validate the bound target, credential, body, and current DNS answers."""

        target = self._operation_target
        _validate_operation_target(
            target,
            operation=LLMConnectionOperation.INFERENCE,
            provider=target.provider,
            resource_id=None,
            audit_id=audit_id,
        )
        _validate_secret(
            self._secret,
            expected_provider=target.provider,
            audit_id=audit_id,
        )
        _validate_request_body(json_body, bounds=self._bounds)
        validated_target = self._egress_policy.validate_endpoint(
            target.url,
            expected_host=target.expected_host,
            allowed_ports=target.allowed_ports,
            allowed_path_prefixes=target.allowed_path_prefixes,
            network_scope=target.network_scope,
        )
        self._egress_policy.revalidate(validated_target)
        return (
            validated_target.url,
            _provider_headers(target.provider, self._secret.value, json_body),
        )

    def _client(self) -> httpx.AsyncClient:
        """Build one proxy-free, redirect-free TLS-validating async client."""

        timeout = httpx.Timeout(
            connect=self._timeouts.connect_seconds,
            read=self._timeouts.read_seconds,
            write=self._timeouts.read_seconds,
            pool=self._timeouts.connect_seconds,
        )
        return self._client_factory(
            follow_redirects=False,
            trust_env=False,
            verify=True,
            timeout=timeout,
        )


def _validate_secret(
    secret: ProviderSecret,
    *,
    expected_provider: str,
    audit_id: str,
) -> None:
    """Bind one non-empty short-lived credential to its provider origin."""

    if (
        not isinstance(secret, ProviderSecret)
        or secret.provider.strip().lower() != expected_provider
        or not isinstance(secret.value, str)
        or not secret.value.strip()
    ):
        raise GuardedTransportError(
            "Guarded provider credential rejected",
            audit_id=audit_id,
        )


def _validate_operation_target(
    target: RegisteredLLMOperationTarget,
    *,
    operation: LLMConnectionOperation | str,
    provider: str,
    resource_id: str | None,
    audit_id: str,
) -> None:
    """Accept only a typed registry target matching the requested operation."""

    try:
        requested_operation = (
            operation
            if isinstance(operation, LLMConnectionOperation)
            else LLMConnectionOperation(str(operation))
        )
    except ValueError:
        raise GuardedTransportError(
            "Guarded outbound operation failed",
            audit_id=audit_id,
        ) from None
    if not isinstance(target, RegisteredLLMOperationTarget):
        raise GuardedTransportError(
            "Guarded outbound operation failed",
            audit_id=audit_id,
        )
    if target.operation != requested_operation or target.provider != provider:
        raise GuardedTransportError(
            "Guarded outbound operation failed",
            audit_id=audit_id,
        )
    if resource_id is not None:
        raise GuardedTransportError(
            "Guarded outbound operation failed",
            audit_id=audit_id,
        )


def _validate_request_body(
    body: Mapping[str, Any] | None,
    *,
    bounds: GuardedEgressBounds,
) -> None:
    """Reject non-mapping or oversized JSON request bodies before transport."""

    if body is None:
        return
    if not isinstance(body, Mapping):
        raise ValueError("json_body must be a mapping")
    try:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("json_body must be JSON serializable") from exc
    if len(encoded) > bounds.max_request_bytes:
        raise ValueError("json_body exceeds guarded request bound")


def _provider_headers(
    provider: str,
    secret: str,
    body: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Build the fixed provider-approved header set."""

    headers = {"accept": "application/json"}
    if body is not None:
        headers["content-type"] = "application/json"
    if provider == "openai" or _is_bearer_api_key_connection_preset(provider):
        headers["authorization"] = f"Bearer {secret}"
    elif provider == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        headers["x-api-key"] = secret
    else:
        raise ValueError("Unsupported fixed provider")
    return headers


def _is_bearer_api_key_connection_preset(provider: str) -> bool:
    """Return whether a reviewed preset declares bearer API-key auth."""

    try:
        preset = ConnectionOperationRegistry().get_connection_preset(provider)
    except OperationRegistryError:
        return False
    return preset.auth_mode == "bearer_api_key"


def _validate_response_status(status_code: int, *, audit_id: str) -> None:
    """Reject redirects and upstream errors without exposing response details."""

    if 300 <= int(status_code) < 400:
        raise GuardedTransportError(
            "Guarded redirect rejected",
            audit_id=audit_id,
            status_code=int(status_code),
        )
    if int(status_code) < 200 or int(status_code) >= 300:
        raise GuardedTransportError(
            "Guarded upstream response rejected",
            audit_id=audit_id,
            status_code=int(status_code),
        )


def _validate_headers(
    headers: Mapping[str, Any],
    bounds: GuardedEgressBounds,
    *,
    audit_id: str,
) -> None:
    """Enforce header and declared response sizes before body consumption."""

    header_bytes = sum(
        len(str(name).encode("utf-8")) + len(str(value).encode("utf-8"))
        for name, value in headers.items()
    )
    if header_bytes > bounds.max_header_bytes:
        raise GuardedTransportError(
            "Guarded response exceeds bounds",
            audit_id=audit_id,
        )

    content_length = headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except (TypeError, ValueError):
            raise GuardedTransportError(
                "Guarded response has invalid length",
                audit_id=audit_id,
            ) from None
        if declared_size < 0 or declared_size > bounds.max_response_bytes:
            raise GuardedTransportError(
                "Guarded response exceeds bounds",
                audit_id=audit_id,
            )


def _read_bounded_body(
    response: Any,
    *,
    bounds: GuardedEgressBounds,
    started_at: float,
    total_seconds: float,
    audit_id: str,
) -> bytes:
    """Read the decompressed stream without exceeding total response bounds."""

    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(chunk_size=bounds.read_chunk_bytes):
        _require_total_duration(started_at, total_seconds, audit_id=audit_id)
        if not chunk:
            continue
        size += len(chunk)
        if size > bounds.max_response_bytes:
            raise GuardedTransportError(
                "Guarded response exceeds bounds",
                audit_id=audit_id,
            )
        chunks.append(bytes(chunk))
    return b"".join(chunks)


async def _read_bounded_async_body(
    response: httpx.Response,
    *,
    bounds: GuardedEgressBounds,
    started_at: float,
    total_seconds: float,
    audit_id: str,
) -> bytes:
    """Read a decompressed async response within byte and duration bounds."""

    chunks: list[bytes] = []
    async for chunk in _iter_bounded_async_bytes(
        response,
        bounds=bounds,
        started_at=started_at,
        total_seconds=total_seconds,
        audit_id=audit_id,
    ):
        chunks.append(chunk)
    return b"".join(chunks)


async def _iter_bounded_async_bytes(
    response: httpx.Response,
    *,
    bounds: GuardedEgressBounds,
    started_at: float,
    total_seconds: float,
    audit_id: str,
) -> AsyncIterator[bytes]:
    """Yield response chunks while enforcing total decompressed byte limits."""

    size = 0
    iterator = response.aiter_bytes()
    while True:
        remaining = total_seconds - (monotonic() - started_at)
        if remaining <= 0:
            raise GuardedTransportError(
                "Guarded operation timed out",
                audit_id=audit_id,
            )
        try:
            async with asyncio.timeout(remaining):
                chunk = await anext(iterator)
        except StopAsyncIteration:
            return
        if not chunk:
            continue
        size += len(chunk)
        if size > bounds.max_response_bytes:
            raise GuardedTransportError(
                "Guarded response exceeds bounds",
                audit_id=audit_id,
            )
        yield bytes(chunk)


async def _iter_bounded_sse_json(
    response: httpx.Response,
    *,
    bounds: GuardedEgressBounds,
    started_at: float,
    total_seconds: float,
    audit_id: str,
) -> AsyncIterator[Any]:
    """Incrementally parse bounded SSE data fields into decoded JSON events."""

    buffer = b""
    data_lines: list[bytes] = []
    saw_sse_field = False
    raw_body = bytearray()

    def _decode_event() -> Any | None:
        nonlocal data_lines
        if not data_lines:
            return None
        payload = b"\n".join(data_lines).decode("utf-8").strip()
        data_lines = []
        if payload == "[DONE]":
            return _SSE_DONE
        return json.loads(payload)

    async for chunk in _iter_bounded_async_bytes(
        response,
        bounds=bounds,
        started_at=started_at,
        total_seconds=total_seconds,
        audit_id=audit_id,
    ):
        if not saw_sse_field:
            raw_body.extend(chunk)
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.removesuffix(b"\r")
            if not line:
                event = _decode_event()
                if event is _SSE_DONE:
                    return
                if event is not None:
                    yield event
                continue
            if line.startswith(b":"):
                saw_sse_field = True
                raw_body.clear()
                continue
            field, separator, value = line.partition(b":")
            if field != b"data":
                continue
            saw_sse_field = True
            raw_body.clear()
            if separator and value.startswith(b" "):
                value = value[1:]
            data_lines.append(value)

    if buffer:
        line = buffer.removesuffix(b"\r")
        field, separator, value = line.partition(b":")
        if field == b"data":
            saw_sse_field = True
            raw_body.clear()
            if separator and value.startswith(b" "):
                value = value[1:]
            data_lines.append(value)
    event = _decode_event()
    if event is _SSE_DONE:
        return
    if event is not None:
        yield event
        return
    if not saw_sse_field:
        yield json.loads(bytes(raw_body).decode("utf-8"))


_SSE_DONE = object()


def _require_total_duration(
    started_at: float,
    total_seconds: float,
    *,
    audit_id: str | None = None,
) -> None:
    """Reject operations that exceed their total wall-clock budget."""

    if monotonic() - started_at > total_seconds:
        raise GuardedTransportError(
            "Guarded operation timed out",
            audit_id=audit_id or uuid4().hex,
        )


__all__ = [
    "AsyncClientFactory",
    "GuardedAsyncInferenceTransport",
    "GuardedTransport",
    "GuardedTransportError",
    "SessionFactory",
]
