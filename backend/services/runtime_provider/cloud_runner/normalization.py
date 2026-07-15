"""Pure normalization helpers for cloud runner provider inputs."""

from __future__ import annotations

from typing import Any
from uuid import UUID


def _normalize_tenant_id(value: object) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("tenant_id must be an integer for cloud runner control.") from exc
    if normalized <= 0:
        raise ValueError("tenant_id must be a positive integer for cloud runner control.")
    return normalized


def _normalize_optional_uuid(value: object) -> UUID | None:
    text = _resolve_optional_text(value)
    if text is None:
        return None
    return UUID(text)


def _resolve_optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _resolve_optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_positive_int(value: Any, *, default: int) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return default
    if coerced < 1:
        return default
    return coerced


def _coerce_int_or_default(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_non_negative_float(value: Any, *, default: float) -> float:
    try:
        coerced = float(value)
    except (TypeError, ValueError):
        return default
    if coerced < 0:
        return default
    return coerced
