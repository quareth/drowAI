"""Normalize narrowly defined content-encoded tool calls into provider-neutral calls.

This module owns the fail-closed compatibility boundary for models that return
a requested function call as JSON message content instead of their provider's
native tool-call field. It authorizes names against the request contracts and
validates arguments before producing canonical ``ToolCall`` values.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from jsonschema import SchemaError, ValidationError, validate

from ..core.base import ToolCall
from .tool_contracts import FunctionToolSpec, function_tool_spec_from_openai_dict

_LEAKED_FUNCTION_NAMESPACE = "functions."


class ContentEncodedToolCallError(ValueError):
    """Raised when JSON-like assistant content is not a safe tool-call envelope."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _requested_specs(tools: Sequence[Any]) -> dict[str, FunctionToolSpec]:
    """Index supported request tool contracts by their exact function name."""

    specs: dict[str, FunctionToolSpec] = {}
    for tool in tools:
        try:
            if isinstance(tool, FunctionToolSpec):
                spec = tool
            elif isinstance(tool, Mapping):
                spec = function_tool_spec_from_openai_dict(tool)
            else:
                raise TypeError("tool contract must be a mapping or FunctionToolSpec")
        except (TypeError, ValueError) as exc:
            raise ContentEncodedToolCallError("invalid_requested_tool_contract") from exc
        if spec.name in specs:
            raise ContentEncodedToolCallError("duplicate_requested_tool_name")
        specs[spec.name] = spec
    return specs


def _call_entries(payload: Any) -> list[Mapping[str, Any]]:
    """Extract ordered call entries from one supported JSON envelope shape."""

    raw_entries: Any
    if isinstance(payload, list):
        raw_entries = payload
    elif isinstance(payload, Mapping) and set(payload) == {"tool_calls"}:
        raw_entries = payload["tool_calls"]
    elif isinstance(payload, Mapping):
        raw_entries = [payload]
    else:
        raise ContentEncodedToolCallError("unsupported_envelope_shape")

    if not isinstance(raw_entries, list):
        raise ContentEncodedToolCallError("tool_calls_not_list")
    if not raw_entries:
        raise ContentEncodedToolCallError("empty_tool_calls")
    if not all(isinstance(entry, Mapping) for entry in raw_entries):
        raise ContentEncodedToolCallError("tool_call_not_object")
    return list(raw_entries)


def _function_payload(entry: Mapping[str, Any]) -> tuple[str | None, Mapping[str, Any]]:
    """Return an optional provider ID and a strict name/arguments mapping."""

    if set(entry) == {"name", "arguments"}:
        return None, entry

    allowed_keys = {"id", "type", "function"}
    if "function" not in entry or not set(entry).issubset(allowed_keys):
        raise ContentEncodedToolCallError("unsupported_call_shape")
    if entry.get("type", "function") != "function":
        raise ContentEncodedToolCallError("unsupported_call_type")

    function = entry.get("function")
    if not isinstance(function, Mapping) or set(function) != {"name", "arguments"}:
        raise ContentEncodedToolCallError("unsupported_function_shape")

    raw_id = entry.get("id")
    if raw_id is not None and (
        not isinstance(raw_id, str) or not raw_id.strip()
    ):
        raise ContentEncodedToolCallError("invalid_tool_call_id")
    return raw_id.strip() if isinstance(raw_id, str) else None, function


def _authorized_name(
    raw_name: Any,
    specs: Mapping[str, FunctionToolSpec],
) -> tuple[str, FunctionToolSpec]:
    """Resolve only exact request names or the observed ``functions.`` alias."""

    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ContentEncodedToolCallError("missing_tool_name")
    name = raw_name.strip()
    spec = specs.get(name)
    if spec is not None:
        return name, spec
    if name.startswith(_LEAKED_FUNCTION_NAMESPACE):
        unprefixed = name[len(_LEAKED_FUNCTION_NAMESPACE) :]
        spec = specs.get(unprefixed)
        if spec is not None:
            return unprefixed, spec
    raise ContentEncodedToolCallError("unknown_tool_name")


def _validated_arguments(
    raw_arguments: Any,
    spec: FunctionToolSpec,
) -> dict[str, Any]:
    """Decode an arguments object and validate it against the requested schema."""

    if isinstance(raw_arguments, str):
        try:
            decoded = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise ContentEncodedToolCallError("arguments_not_json") from exc
    else:
        decoded = raw_arguments

    if not isinstance(decoded, Mapping):
        raise ContentEncodedToolCallError("arguments_not_object")
    arguments = dict(decoded)
    try:
        validate(instance=arguments, schema=spec.parameters_schema)
    except ValidationError as exc:
        raise ContentEncodedToolCallError("arguments_schema_validation") from exc
    except SchemaError as exc:
        raise ContentEncodedToolCallError("invalid_requested_tool_schema") from exc
    return arguments


def normalize_content_encoded_tool_calls(
    content: Any,
    *,
    tools: Sequence[Any],
) -> list[ToolCall] | None:
    """Normalize an exact JSON call envelope after request-bound authorization.

    Ordinary text and Markdown are not interpreted. JSON-like content either
    normalizes completely or raises ``ContentEncodedToolCallError``; partial
    call recovery is deliberately forbidden.
    """

    if not isinstance(content, str):
        return None
    raw_content = content.strip()
    if not raw_content or raw_content[0] not in "{[":
        return None

    try:
        payload = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise ContentEncodedToolCallError("content_not_json") from exc

    specs = _requested_specs(tools)
    if not specs:
        raise ContentEncodedToolCallError("no_requested_tools")

    normalized: list[ToolCall] = []
    for index, entry in enumerate(_call_entries(payload), start=1):
        provider_id, function = _function_payload(entry)
        name, spec = _authorized_name(function.get("name"), specs)
        arguments = _validated_arguments(function.get("arguments"), spec)
        normalized.append(
            ToolCall(
                id=provider_id or f"content_tool_call_{index}",
                name=name,
                arguments=json.dumps(
                    arguments,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        )
    return normalized


__all__ = [
    "ContentEncodedToolCallError",
    "normalize_content_encoded_tool_calls",
]
