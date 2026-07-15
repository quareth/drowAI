"""Payload transport encoding and sanitization for cloud runner operations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

from .constants import (
    _FORBIDDEN_TOOL_COMMAND_PARAM_IDENTITY_FIELDS,
    _SENSITIVE_KEY_PARTS,
)


def _prepare_transport_params(params: Mapping[str, Any]) -> dict[str, Any]:
    transport: dict[str, Any] = {}
    for key, value in params.items():
        transport[str(key)] = _coerce_transport_value(value)
    return transport


def _sanitize_params(params: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in params.items():
        text_key = str(key)
        safe[text_key] = _sanitize_key_value(text_key=text_key, value=value)
    return safe


def _sanitize_key_value(*, text_key: str, value: Any) -> Any:
    key_lower = text_key.strip().lower()
    if any(part in key_lower for part in _SENSITIVE_KEY_PARTS):
        return "<redacted>"
    return _sanitize_value(value)


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(k): _sanitize_key_value(text_key=str(k), value=v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _coerce_transport_value(value: Any) -> Any:
    if hasattr(value, "model_dump") and callable(value.model_dump):
        dumped = value.model_dump()
        if isinstance(dumped, Mapping):
            return {
                str(k): _coerce_transport_value(v)
                for k, v in dumped.items()
            }
    if hasattr(value, "dict") and callable(value.dict):
        dumped = value.dict()
        if isinstance(dumped, Mapping):
            return {
                str(k): _coerce_transport_value(v)
                for k, v in dumped.items()
            }
    if is_dataclass(value):
        dumped = asdict(value)
        if isinstance(dumped, Mapping):
            return {
                str(k): _coerce_transport_value(v)
                for k, v in dumped.items()
            }
    if isinstance(value, Mapping):
        return {
            str(k): _coerce_transport_value(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_coerce_transport_value(item) for item in value]
    if isinstance(value, tuple):
        return [_coerce_transport_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _contains_secret_bearing_args(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, inner in value.items():
            key_text = str(key).strip().lower()
            if any(part in key_text for part in _SENSITIVE_KEY_PARTS):
                return True
            if _contains_secret_bearing_args(inner):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_bearing_args(item) for item in value)
    return False


def _collect_forbidden_tool_command_param_identity_keys(value: object) -> set[str]:
    forbidden: set[str] = set()
    if isinstance(value, Mapping):
        for key, inner in value.items():
            key_text = str(key).strip().lower()
            if key_text in _FORBIDDEN_TOOL_COMMAND_PARAM_IDENTITY_FIELDS:
                forbidden.add(key_text)
            forbidden.update(_collect_forbidden_tool_command_param_identity_keys(inner))
        return forbidden
    if isinstance(value, (list, tuple)):
        for item in value:
            forbidden.update(_collect_forbidden_tool_command_param_identity_keys(item))
    return forbidden
