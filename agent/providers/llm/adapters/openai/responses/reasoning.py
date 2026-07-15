"""Encapsulate reasoning-effort policy for the OpenAI Responses provider.

This module keeps provider-local normalization and validation behavior for GPT-5
Responses API calls. It does not use shared cross-provider role policy.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from ....core.exceptions import LLMConfigurationError, LLMProfileNotFoundError
from ....core.identity import OPENAI_PROVIDER_ID, ProviderModelRef
from ....core.reasoning_policy import (
    CANONICAL_REASONING_EFFORT_VALUES,
    OPENAI_RESPONSES_XHIGH_ERROR,
    validate_reasoning_effort_for_provider_model,
)
from ....profiles.registry import require_model_profile

DEFAULT_REASONING_EFFORT = "minimal"


def default_reasoning_effort(model: str) -> str:
    """Return the exact model profile default, with legacy fallback."""
    try:
        profile = require_model_profile(ProviderModelRef(OPENAI_PROVIDER_ID, model))
    except (LLMProfileNotFoundError, TypeError, ValueError):
        return DEFAULT_REASONING_EFFORT
    return profile.default_reasoning_effort or DEFAULT_REASONING_EFFORT


def validate_reasoning_effort(effort: str, model: str) -> str:
    """Validate canonical reasoning effort values and model compatibility."""
    try:
        resolved_effort = validate_reasoning_effort_for_provider_model(
            effort=str(effort),
            provider=OPENAI_PROVIDER_ID,
            model=model,
            xhigh_error_message=OPENAI_RESPONSES_XHIGH_ERROR,
        )
    except ValueError as exc:
        raise LLMConfigurationError(
            str(exc),
            provider="OpenAI",
        ) from exc
    if resolved_effort is None:
        raise LLMConfigurationError(
            f"Invalid reasoning_effort '{effort}'. "
            f"Allowed values: {', '.join(CANONICAL_REASONING_EFFORT_VALUES)}.",
            provider="OpenAI",
        )
    return resolved_effort


def resolve_reasoning_effort(
    kwargs: Dict[str, Any],
    *,
    default_effort: str,
    model: str,
    logger: logging.Logger,
    resolution_role: str,
    resolution_source: str,
) -> str:
    """Resolve and validate per-request effort, defaulting to client policy."""
    effort = kwargs.get("reasoning_effort", default_effort)
    if effort is None:
        effort = default_effort
    resolved_effort = validate_reasoning_effort(str(effort), model)
    logger.debug(
        "OpenAI Responses call settings: role=%s model=%s effort=%s source=%s",
        resolution_role,
        model,
        resolved_effort,
        resolution_source,
    )
    return resolved_effort
