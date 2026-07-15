"""Generic security classifiers for flattened packet fields."""

from __future__ import annotations

import re
from typing import Any

from .contracts import CredentialEvent, FieldRecord

_PRIVATE_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_AUTHORIZATION_HEADER_RE = re.compile(r"(?i)\b((?:proxy-)?authorization\s*:\s*)([^\r\n]+)")
_COOKIE_HEADER_RE = re.compile(r"(?i)\b((?:set-)?cookie\s*:\s*)([^\r\n]+)")
_API_KEY_HEADER_RE = re.compile(
    r"(?i)\b((?:x[-_])?(?:api[-_]?key|auth[-_]?token|access[-_]?token|refresh[-_]?token|id[-_]?token)\s*:\s*)([^\r\n]+)"
)
_BEARER_TOKEN_RE = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=\-]{6,})")
_SECRET_KV_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|session[_-]?id|session|token|secret)\b(\s*[:=]\s*[\"']?)([^\"'\s;&]+)"
)
_USERNAME_KV_RE = re.compile(r"(?i)\b(user(?:name)?|login)\b(\s*[:=]\s*[\"']?)([^\"'\s;&]+)")
_SENSITIVE_FIELD_NAME_RE = re.compile(
    r"(?i)(authorization|cookie|passwd|password|pwd|api[_-]?key|token|secret|private[_-]?key|auth(?:entication)?(?:_|\.)?(?:arg|parameter))"
)
_USERNAME_FIELD_NAME_RE = re.compile(r"(?i)(^|[._-])(user(?:name)?|login)([._-]|$)")


def classify_direct_field(record: FieldRecord) -> tuple[str, str] | None:
    """Classify one flattened field by generic name/value security signals."""

    field = record.field_name.strip()
    field_lower = field.lower()
    value = record.value.strip()
    if not value:
        return None

    if "user_agent" in field_lower or "user-agent" in field_lower:
        return None
    if "authorization" in field_lower:
        return "authorization_header", "secret"
    if "cookie" in field_lower or "session" in field_lower:
        return "cookie", "secret"
    if "private" in field_lower and "key" in field_lower:
        return "private_key", "secret"
    if "api" in field_lower and "key" in field_lower:
        return "api_key", "secret"
    if "token" in field_lower:
        return "bearer_token", "secret"
    if "password" in field_lower or "passwd" in field_lower or field_lower.endswith(".pwd"):
        return "password", "password"
    if _USERNAME_FIELD_NAME_RE.search(field_lower):
        return "username", "username"
    if _SENSITIVE_FIELD_NAME_RE.search(field):
        return "secret", "secret"

    if _PRIVATE_KEY_BLOCK_RE.search(value):
        return "private_key", "secret"
    if _AUTHORIZATION_HEADER_RE.search(value):
        return "authorization_header", "secret"
    if _COOKIE_HEADER_RE.search(value):
        return "cookie", "secret"
    if _API_KEY_HEADER_RE.search(value):
        return "api_key", "secret"
    if _BEARER_TOKEN_RE.search(value):
        return "bearer_token", "secret"
    if _SECRET_KV_RE.search(value):
        return "secret", "secret"
    if _USERNAME_KV_RE.search(value):
        return "username", "username"
    return None


def credential_event_from_field(
    record: FieldRecord,
    *,
    kind: str,
    role: str,
    command: str | None = None,
    extraction_filter: str | None = None,
) -> CredentialEvent:
    """Build a normalized credential event from one classified field."""

    context = record.context
    return CredentialEvent(
        frame=context.frame,
        time=context.time,
        stream=context.stream,
        protocol=context.app_protocol or context.protocol,
        src=context.src,
        dst=context.dst,
        flow_key=context.flow_key,
        field=normalize_field_path(record.field_name),
        kind=normalize_kind(kind),
        role=normalize_role(role),
        value=record.value,
        command=command,
        extraction_filter=extraction_filter or normalize_field_path(record.field_name),
    )


def normalize_field_path(value: Any) -> str:
    """Normalize repeated TShark dotted path fragments."""

    parts = [part for part in str(value or "").split(".") if part]
    if len(parts) >= 2 and parts[0] == parts[1]:
        parts = parts[1:]
    return ".".join(parts) if parts else str(value or "")


def normalize_kind(value: Any) -> str:
    """Normalize classifier kind labels."""

    text = re.sub(r"[^a-z0-9_]+", "_", str(value or "secret").lower()).strip("_")
    return text or "secret"


def normalize_role(value: Any) -> str:
    """Normalize credential event role labels."""

    role = normalize_kind(value)
    if role in {"password", "username", "secret", "success"}:
        return role
    return "secret"


def auth_mechanism(kind: str, role: str) -> str:
    """Return legacy auth-indicator mechanism text for a classified event."""

    normalized_kind = normalize_kind(kind)
    normalized_role = normalize_role(role)
    if normalized_role == "username":
        return "username"
    if normalized_role == "password":
        return "protocol_auth"
    if normalized_kind == "cookie":
        return "cookie"
    if normalized_kind in {"authorization_header", "bearer_token"}:
        return "authorization"
    return normalized_kind
