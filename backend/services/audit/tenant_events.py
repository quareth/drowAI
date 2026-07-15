"""Tenant audit event shape and redaction helpers.

Responsibilities:
- Build a stable tenant-scoped audit envelope with required Tenant Isolation fields.
- Redact secrets and signed URLs from metadata before events are emitted.

Boundaries:
- Produces in-memory event dictionaries only.
- Does not persist records or depend on router/ORM modules.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlparse
import re

DEFAULT_AUDIT_REASON_CODE = "NONE"
_REDACTED = "<REDACTED>"
_SENSITIVE_KEY_PARTS = (
    "secret",
    "password",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "access_key",
    "credential",
    "authorization",
    "cookie",
)
_SENSITIVE_VALUE_PREFIXES = (
    "rit_",
    "rsec_",
    "bearer ",
)
_SIGNED_URL_QUERY_KEYS = frozenset(
    {
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
        "x-goog-signature",
        "x-goog-credential",
        "sig",
        "signature",
        "token",
    }
)
_EVENT_ACTION_MAP = {
    "accepted": "accept",
    "applied": "apply",
    "assigned": "assign",
    "connected": "connect",
    "created": "create",
    "credential_revoked": "revoke",
    "disconnected": "disconnect",
    "heartbeat": "heartbeat",
    "install_token_created": "create",
    "offline": "expire",
    "protocol_violation": "validate",
    "registered": "register",
    "rejected": "reject",
}
_RESULT_BY_SUFFIX = {
    "rejected": "failure",
    "protocol_violation": "failure",
}


def build_tenant_audit_event(
    *,
    event_type: str,
    tenant_id: int,
    metadata: Mapping[str, Any] | None = None,
    actor_user_id: int | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    action: str | None = None,
    result: str | None = None,
    reason_code: str | None = None,
    runner_id: str | None = None,
    task_id: int | None = None,
    runtime_job_id: str | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Return one normalized tenant audit event with redacted metadata."""

    redacted_metadata = redact_audit_metadata(dict(metadata or {}))
    inferred_actor_user_id = _resolve_actor_user_id(actor_user_id=actor_user_id, metadata=redacted_metadata)
    normalized_action = _normalize_optional_text(action) or _infer_action(event_type)
    normalized_result = _normalize_optional_text(result) or _infer_result(event_type, redacted_metadata)
    normalized_reason_code = _normalize_reason_code(
        reason_code
        or _read_reason_code_from_metadata(redacted_metadata)
        or (DEFAULT_AUDIT_REASON_CODE if normalized_result == "success" else "UNSPECIFIED")
    )
    normalized_resource_type, normalized_resource_id = _resolve_resource_target(
        resource_type=resource_type,
        resource_id=resource_id,
        runner_id=runner_id,
        task_id=task_id,
        runtime_job_id=runtime_job_id,
        metadata=redacted_metadata,
    )

    return {
        "emitted_at": datetime.now(tz=UTC).isoformat(),
        "event_type": _normalize_required_text(event_type),
        "tenant_id": int(tenant_id),
        "actor_user_id": inferred_actor_user_id,
        "resource_type": normalized_resource_type,
        "resource_id": normalized_resource_id,
        "action": normalized_action,
        "result": normalized_result,
        "reason_code": normalized_reason_code,
        "runner_id": _normalize_optional_text(runner_id),
        "task_id": int(task_id) if task_id is not None else None,
        "runtime_job_id": _normalize_optional_text(runtime_job_id),
        "correlation_id": _normalize_optional_text(correlation_id),
        "metadata": redacted_metadata,
    }


def redact_audit_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a recursively redacted copy of audit metadata."""

    result: dict[str, Any] = {}
    for key, value in (metadata or {}).items():
        normalized_key = str(key)
        if _is_sensitive_key(normalized_key):
            result[normalized_key] = _REDACTED
            continue
        result[normalized_key] = _redact_value(value=value, key_hint=normalized_key)
    return result


def _redact_value(*, value: Any, key_hint: str | None = None) -> Any:
    if isinstance(value, Mapping):
        return redact_audit_metadata(value)
    if isinstance(value, list):
        return [_redact_value(value=item, key_hint=key_hint) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(value=item, key_hint=key_hint) for item in value)
    if isinstance(value, str):
        return _redact_string_value(value=value, key_hint=key_hint)
    return value


def _redact_string_value(*, value: str, key_hint: str | None) -> str:
    normalized = value.strip()
    lowered = normalized.lower()
    for prefix in _SENSITIVE_VALUE_PREFIXES:
        if lowered.startswith(prefix):
            return _REDACTED
    if _is_signed_url(value=normalized, key_hint=key_hint):
        return _REDACTED
    return value


def _is_signed_url(*, value: str, key_hint: str | None) -> bool:
    normalized_key = str(key_hint or "").strip().lower()
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    query_keys = {str(key).strip().lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    has_signed_params = bool(query_keys.intersection(_SIGNED_URL_QUERY_KEYS))
    if has_signed_params:
        return True
    return normalized_key in {"signed_url", "presigned_url", "upload_url", "download_url"}


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower()
    if normalized == "id" or normalized.endswith("_id") or normalized.endswith("_count"):
        return False
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _normalize_required_text(value: str) -> str:
    normalized = str(value or "").strip()
    return normalized or "unknown"


def _normalize_optional_text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _resolve_actor_user_id(*, actor_user_id: int | None, metadata: Mapping[str, Any]) -> int | None:
    if actor_user_id is not None:
        return int(actor_user_id)
    for key in ("actor_user_id", "created_by_user_id", "user_id"):
        raw = metadata.get(key)
        if raw is None:
            continue
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            continue
        return parsed
    return None


def _read_reason_code_from_metadata(metadata: Mapping[str, Any]) -> str | None:
    for key in ("reason_code", "error_code", "reason"):
        candidate = _normalize_optional_text(metadata.get(key))
        if candidate:
            return candidate
    return None


def _normalize_reason_code(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", str(value or "").strip()).strip("_")
    if not normalized:
        return DEFAULT_AUDIT_REASON_CODE
    return normalized.upper()


def _infer_action(event_type: str) -> str:
    suffix = _event_suffix(event_type)
    return _EVENT_ACTION_MAP.get(suffix, suffix or "observe")


def _infer_result(event_type: str, metadata: Mapping[str, Any]) -> str:
    metadata_result = _normalize_optional_text(metadata.get("result"))
    if metadata_result:
        return metadata_result.lower()
    metadata_success = metadata.get("success")
    if isinstance(metadata_success, bool):
        return "success" if metadata_success else "failure"
    suffix = _event_suffix(event_type)
    return _RESULT_BY_SUFFIX.get(suffix, "success")


def _event_suffix(event_type: str) -> str:
    normalized = _normalize_required_text(event_type)
    return normalized.split(".")[-1].lower()


def _resolve_resource_target(
    *,
    resource_type: str | None,
    resource_id: str | None,
    runner_id: str | None,
    task_id: int | None,
    runtime_job_id: str | None,
    metadata: Mapping[str, Any],
) -> tuple[str, str]:
    normalized_resource_type = _normalize_optional_text(resource_type)
    normalized_resource_id = _normalize_optional_text(resource_id)
    if normalized_resource_type and normalized_resource_id:
        return normalized_resource_type, normalized_resource_id
    if runtime_job_id is not None:
        return "runtime_job", _normalize_required_text(str(runtime_job_id))
    if runner_id is not None:
        return "runner", _normalize_required_text(str(runner_id))
    if task_id is not None:
        return "task", str(int(task_id))
    if metadata.get("execution_site_id") is not None:
        return "execution_site", _normalize_required_text(str(metadata["execution_site_id"]))
    if metadata.get("install_token_id") is not None:
        return "runner_install_token", _normalize_required_text(str(metadata["install_token_id"]))
    if metadata.get("credential_id") is not None:
        return "runner_credential", _normalize_required_text(str(metadata["credential_id"]))
    return normalized_resource_type or "tenant", normalized_resource_id or "tenant"

