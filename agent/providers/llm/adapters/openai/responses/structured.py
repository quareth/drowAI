"""Handle structured-output helpers for the OpenAI Responses provider.

This module keeps provider-local metric emission, schema attachment, parse
diagnostics, and strict structured-output parsing behavior for Responses API
calls.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

from ....core.base import StructuredOutputSpec
from ....core.exceptions import (
    LLMConfigurationError,
    LLMStructuredOutputParseError,
)
from ....contracts.structured_output import (
    StructuredOutputParseError,
    parse_structured_content,
)
from ..structured_output import (
    StructuredOutputSchemaError,
    build_responses_text_format,
    require_openai_native_structured_output_strategy,
    validate_openai_strict_schema,
)


def safe_inc(metric_name: str) -> None:
    """Increment metrics when backend metrics utilities are available."""
    try:
        from backend.services.metrics.utils import safe_inc as backend_safe_inc

        backend_safe_inc(metric_name)
    except Exception:
        return


def structured_metric_suffix(schema_name: str) -> str:
    """Return metric-safe schema suffix."""
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", str(schema_name).strip()).strip("_")
    return normalized or "unknown"


def get_structured_output_spec(
    kwargs: Dict[str, Any],
) -> Optional[StructuredOutputSpec]:
    """Extract and type-check structured output spec from call kwargs."""
    spec = kwargs.get("structured_output")
    if spec is None:
        return None
    if not isinstance(spec, StructuredOutputSpec):
        raise LLMConfigurationError(
            "structured_output must be StructuredOutputSpec",
            provider="OpenAI",
        )
    return spec


def attach_structured_output_format(
    request_kwargs: Dict[str, Any],
    structured_spec: Optional[StructuredOutputSpec],
) -> None:
    """Attach Responses API json_schema format payload when requested."""
    if structured_spec is None:
        return
    require_openai_native_structured_output_strategy(
        structured_spec,
        model=str(request_kwargs.get("model") or ""),
    )
    try:
        validate_openai_strict_schema(structured_spec)
    except StructuredOutputSchemaError as exc:
        raise LLMConfigurationError(str(exc), provider="OpenAI") from exc
    request_kwargs["text"] = {"format": build_responses_text_format(structured_spec)}
    suffix = structured_metric_suffix(structured_spec.name)
    safe_inc(f"llm_structured_request_openai_responses_{suffix}")


def parse_structured_output(
    content: str,
    structured_spec: Optional[StructuredOutputSpec],
    *,
    raw_response: Any | None = None,
    logger: logging.Logger,
) -> Optional[Dict[str, Any]]:
    """Parse structured content when schema contract is provided."""
    if structured_spec is None:
        return None
    try:
        return parse_structured_content(content, structured_spec)
    except StructuredOutputParseError as exc:
        suffix = structured_metric_suffix(structured_spec.name)
        safe_inc(f"llm_structured_parse_failure_openai_responses_{suffix}")
        diagnostics = build_structured_output_diagnostics(raw_response)
        logger.warning(
            "Structured output parse failed (provider=openai_responses schema=%s reason=%s response_id=%s status=%s)",
            structured_spec.name,
            exc.reason,
            diagnostics.get("response_id"),
            diagnostics.get("status"),
        )
        raise LLMStructuredOutputParseError(
            str(exc),
            provider="OpenAI",
            schema_name=structured_spec.name,
            parse_reason=exc.reason,
            raw_content=content,
            diagnostics=diagnostics,
        ) from exc


def build_structured_output_diagnostics(response: Any | None) -> Dict[str, object]:
    """Extract best-effort diagnostics for structured parse failures."""
    if response is None:
        return {}

    diagnostics: Dict[str, object] = {}
    response_id = getattr(response, "id", None)
    if isinstance(response_id, str) and response_id.strip():
        diagnostics["response_id"] = response_id.strip()

    status = getattr(response, "status", None)
    if isinstance(status, str) and status.strip():
        diagnostics["status"] = status.strip()

    incomplete_details = getattr(response, "incomplete_details", None)
    if incomplete_details is not None:
        if isinstance(incomplete_details, dict):
            diagnostics["incomplete_details"] = dict(incomplete_details)
        else:
            try:
                diagnostics["incomplete_details"] = dict(incomplete_details)
            except Exception:
                diagnostics["incomplete_details"] = str(incomplete_details)

    return diagnostics
