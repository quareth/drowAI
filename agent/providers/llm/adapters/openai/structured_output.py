"""OpenAI structured-output request payload helpers.

This module owns OpenAI-specific json_schema request shaping and strict-schema
validation. Provider-neutral parsing stays in ``structured_output.py`` so future
providers do not inherit OpenAI request payload rules by default.
"""

from __future__ import annotations

from typing import Any, Dict

from ...contracts.structured_output_strategy import select_structured_output_strategy
from ...core.base import StructuredOutputSpec
from ...core.capabilities import LLMCapability
from ...core.identity import OPENAI_PROVIDER_ID, ProviderModelRef
from ...profiles import require_model_profile


class StructuredOutputSchemaError(ValueError):
    """Raised when a schema contract is invalid for OpenAI strict JSON mode."""

    def __init__(self, message: str, reason: str = "invalid_schema_contract") -> None:
        super().__init__(message)
        self.reason = reason


def build_responses_text_format(spec: StructuredOutputSpec) -> Dict[str, Any]:
    """Build Responses API ``text.format`` payload for json_schema mode."""
    return {
        "type": "json_schema",
        "name": spec.name,
        "strict": spec.strict,
        "schema": spec.schema,
    }


def build_chat_response_format(spec: StructuredOutputSpec) -> Dict[str, Any]:
    """Build Chat Completions ``response_format`` payload for json_schema mode."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": spec.name,
            "strict": spec.strict,
            "schema": spec.schema,
        },
    }


def require_openai_native_structured_output_strategy(
    spec: StructuredOutputSpec,
    *,
    model: str,
) -> None:
    """Require the profiled OpenAI native-schema strategy for this model."""
    profile = require_model_profile(ProviderModelRef(OPENAI_PROVIDER_ID, model))
    select_structured_output_strategy(
        spec,
        allowed_strategies=profile.structured_output_strategies,
        supports_native_schema=profile.supports(LLMCapability.STRUCTURED_OUTPUT_NATIVE),
        supports_tool_fallback=False,
        provider=OPENAI_PROVIDER_ID,
        model=model,
    )


def _collect_required_coverage_errors(schema: Dict[str, Any], path: str = "$") -> list[str]:
    """Collect strict-mode required coverage violations recursively."""
    errors: list[str] = []

    properties = schema.get("properties")
    if isinstance(properties, dict):
        if schema.get("additionalProperties") is not False:
            errors.append(f"{path}: additionalProperties must be false")
        required = schema.get("required")
        if not isinstance(required, list):
            errors.append(f"{path}: required missing or not a list")
        else:
            missing_keys = sorted(set(properties.keys()) - set(required))
            if missing_keys:
                errors.append(f"{path}: required missing keys {missing_keys}")
        for key, child in properties.items():
            if isinstance(child, dict):
                errors.extend(
                    _collect_required_coverage_errors(child, f"{path}.properties.{key}")
                )

    items = schema.get("items")
    if isinstance(items, dict):
        errors.extend(_collect_required_coverage_errors(items, f"{path}.items"))

    for key in ("anyOf", "allOf", "oneOf"):
        branch_list = schema.get(key)
        if isinstance(branch_list, list):
            for index, branch in enumerate(branch_list):
                if isinstance(branch, dict):
                    errors.extend(
                        _collect_required_coverage_errors(
                            branch,
                            f"{path}.{key}[{index}]",
                        )
                    )

    return errors


def validate_openai_strict_schema(spec: StructuredOutputSpec) -> None:
    """Validate schema compatibility for OpenAI strict JSON schema mode."""
    schema = spec.schema if isinstance(spec.schema, dict) else {}
    errors = _collect_required_coverage_errors(schema, path=spec.name)
    if errors:
        preview = "; ".join(errors[:3])
        raise StructuredOutputSchemaError(
            f"Invalid strict schema for '{spec.name}': {preview}",
            reason="missing_required_properties",
        )


__all__ = [
    "StructuredOutputSchemaError",
    "build_chat_response_format",
    "build_responses_text_format",
    "require_openai_native_structured_output_strategy",
    "validate_openai_strict_schema",
]
