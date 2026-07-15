"""Provider-neutral helpers for structured JSON output parsing.

This module owns JSON decoding and schema validation of provider responses.
Provider-native request payload builders live in provider-specific modules.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict

from jsonschema import ValidationError, validate

from ..core.base import StructuredOutputSpec


class StructuredOutputParseError(ValueError):
    """Normalized parse/validation error for structured-output responses."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


_FENCED_JSON_RE = re.compile(r"^\s*```(?:json|JSON)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _decode_json(raw: str, spec: StructuredOutputSpec) -> Any:
    """Decode plain JSON or a single common model wrapper."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as direct_exc:
        fenced_match = _FENCED_JSON_RE.match(raw)
        if fenced_match is not None:
            try:
                return json.loads(fenced_match.group(1).strip())
            except json.JSONDecodeError as fenced_exc:
                raise StructuredOutputParseError(
                    message=(
                        f"Structured output fenced block is not valid JSON for "
                        f"schema '{spec.name}': {fenced_exc}"
                    ),
                    reason="json_decode_error",
                ) from fenced_exc

        return _decode_single_embedded_object(raw, spec, direct_exc)


def _decode_single_embedded_object(
    raw: str,
    spec: StructuredOutputSpec,
    direct_exc: json.JSONDecodeError,
) -> Any:
    """Decode exactly one top-level JSON object embedded in surrounding prose."""
    candidates = _find_embedded_object_candidates(raw)
    if not candidates:
        raise StructuredOutputParseError(
            message=f"Structured output is not valid JSON for schema '{spec.name}': {direct_exc}",
            reason="json_decode_error",
        ) from direct_exc

    if len(candidates) > 1:
        raise StructuredOutputParseError(
            message=(
                f"Structured output contains multiple JSON object candidates for "
                f"schema '{spec.name}'"
            ),
            reason="ambiguous_json_object",
        )

    candidate = candidates[0]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as embedded_exc:
        raise StructuredOutputParseError(
            message=(
                f"Structured output embedded object is not valid JSON for "
                f"schema '{spec.name}': {embedded_exc}"
            ),
            reason="json_decode_error",
        ) from embedded_exc


def _find_embedded_object_candidates(raw: str) -> list[str]:
    """Return clear JSON object candidates without treating arrays as objects."""
    candidates: list[str] = []
    depth = 0
    start: int | None = None
    in_string = False
    escaped = False

    for index, char in enumerate(raw):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue

        if char == "{":
            if depth == 0:
                start = index
            depth += 1
            continue

        if char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                if _has_clear_object_boundaries(raw, start, index + 1):
                    candidates.append(raw[start : index + 1])
                start = None

    return candidates


def _has_clear_object_boundaries(raw: str, start: int, end: int) -> bool:
    """Reject object snippets that are clearly elements inside another JSON value."""
    before = raw[:start].rstrip()
    after = raw[end:].lstrip()
    if before.endswith(("[", ",")):
        return False
    if after.startswith(("]", ",")):
        return False
    return True


def parse_structured_content(content: str, spec: StructuredOutputSpec) -> Dict[str, Any]:
    """Decode and validate JSON content against a schema contract."""
    raw = str(content or "").strip()
    if not raw:
        raise StructuredOutputParseError(
            message=f"Structured output empty for schema '{spec.name}'",
            reason="empty_content",
        )

    parsed = _decode_json(raw, spec)

    if not isinstance(parsed, dict):
        raise StructuredOutputParseError(
            message=(
                f"Structured output must decode to an object for schema '{spec.name}', "
                f"got {type(parsed).__name__}"
            ),
            reason="non_object_json",
        )

    try:
        validate(instance=parsed, schema=spec.schema)
    except ValidationError as exc:
        field_path = ".".join(str(item) for item in exc.path) or "$"
        raise StructuredOutputParseError(
            message=(
                f"Structured output failed schema validation for '{spec.name}' "
                f"at {field_path}: {exc.message}"
            ),
            reason="schema_validation_error",
        ) from exc

    return parsed


from ..adapters.openai.structured_output import (  # noqa: E402
    StructuredOutputSchemaError,
    build_chat_response_format,
    build_responses_text_format,
    validate_openai_strict_schema,
)

__all__ = [
    "StructuredOutputParseError",
    "StructuredOutputSchemaError",
    "build_chat_response_format",
    "build_responses_text_format",
    "parse_structured_content",
    "validate_openai_strict_schema",
]
