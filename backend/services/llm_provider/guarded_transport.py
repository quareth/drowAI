"""Bounded transport for code-owned LLM connection operations.

The transport resolves endpoints through the immutable operation registry,
revalidates public DNS, disables redirects and proxy inheritance, applies typed
provider authentication, and returns bounded bodies with sanitized failures.
"""

from __future__ import annotations

import json
from time import monotonic
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

import requests

from .egress_policy import EgressPolicyError, FixedProviderEgressPolicy
from .operation_registry import ConnectionOperationRegistry, OperationRegistryError
from .types import (
    GuardedEgressBounds,
    GuardedEgressTimeouts,
    GuardedHTTPResponse,
    LLMConnectionOperation,
    ProviderSecret,
)


class _SessionLike(Protocol):
    """Minimal requests-compatible session contract used by guarded transport."""

    trust_env: bool

    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...

    def close(self) -> None: ...


SessionFactory = Callable[[], _SessionLike]


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
    ) -> GuardedHTTPResponse:
        """Execute a registered operation without accepting raw URLs or headers."""

        audit_id = uuid4().hex
        started_at = monotonic()
        response: Any = None
        session: _SessionLike | None = None
        try:
            target = self._registry.resolve(
                operation,
                provider=provider,
                resource_id=resource_id,
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
    if provider == "openai":
        headers["authorization"] = f"Bearer {secret}"
    elif provider == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        headers["x-api-key"] = secret
    else:
        raise ValueError("Unsupported fixed provider")
    return headers


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
    "GuardedTransport",
    "GuardedTransportError",
    "SessionFactory",
]
