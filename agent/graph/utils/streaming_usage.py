"""Usage-aware streaming enforcement for LangGraph runtime nodes.

This module keeps the runtime contract small and provider-neutral: streamed
LLM calls in graph nodes must expose final usage, and missing capability or
missing final usage is treated as an explicit provider/runtime error.
"""

from __future__ import annotations

import logging
from typing import Any

from agent.graph.utils.llm_resolver import supports_usage_aware_streaming
from agent.providers.llm.core.exceptions import LLMConfigurationError, LLMResponseError

logger = logging.getLogger(__name__)


def require_usage_aware_streaming(
    llm_client: Any,
    call_settings: Any,
    *,
    operation: str,
    task_id: Any = None,
) -> None:
    """Raise when a runtime streaming call cannot report final usage."""
    if supports_usage_aware_streaming(llm_client, call_settings):
        return

    provider = _provider_from(call_settings)
    model = _model_from(llm_client, call_settings)
    logger.error(
        "Usage-aware streaming is required but unavailable: "
        "operation=%s task_id=%s provider=%s model=%s",
        operation,
        task_id,
        provider,
        model,
    )
    raise LLMConfigurationError(
        (
            "Usage-aware streaming is required for LangGraph runtime calls "
            f"(operation={operation}, task_id={task_id}, model={model})."
        ),
        provider=provider,
    )


def require_final_stream_usage(
    usage: Any,
    call_settings: Any,
    *,
    operation: str,
    task_id: Any = None,
) -> Any:
    """Return final stream usage or raise when the provider omitted it."""
    if usage is not None:
        return usage

    provider = _provider_from(call_settings)
    model = _model_from(None, call_settings)
    logger.error(
        "Usage-aware stream completed without final usage: "
        "operation=%s task_id=%s provider=%s model=%s",
        operation,
        task_id,
        provider,
        model,
    )
    raise LLMResponseError(
        (
            "Usage-aware stream completed without final usage "
            f"(operation={operation}, task_id={task_id}, model={model})."
        ),
        provider=provider,
    )


def _provider_from(call_settings: Any) -> str:
    provider = getattr(call_settings, "provider", None)
    return str(provider or "unknown").strip().lower() or "unknown"


def _model_from(llm_client: Any, call_settings: Any) -> str:
    model = getattr(call_settings, "model", None)
    if not model and llm_client is not None:
        model = getattr(llm_client, "model", None)
    return str(model or "unknown").strip() or "unknown"


__all__ = ["require_final_stream_usage", "require_usage_aware_streaming"]
