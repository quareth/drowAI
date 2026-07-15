"""Pure budget decisions for provider/model LLM runtime calls.

This module owns max-output validation against model profiles. It does not
construct clients, read credentials, or call provider SDKs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.context.context_window_policy import ContextFitDecision, evaluate_context_fit
from agent.providers.llm.core.identity import (
    OPENAI_PROVIDER_ID,
    ProviderModelRef,
    normalize_provider_id,
)
from agent.providers.llm.profiles.registry import ModelProfile, require_model_profile


@dataclass(frozen=True, slots=True)
class OutputBudgetDecision:
    """Decision for one requested LLM output-token budget."""

    provider: str
    model: str
    role: str
    requested_max_tokens: int | None
    accepted_max_tokens: int | None
    model_max_output_tokens: int
    context_window_tokens: int
    context_fit: ContextFitDecision | None
    reason: str
    clamped: bool
    should_fail: bool


def decide_output_budget(
    *,
    provider: str,
    model: str,
    role: str | None,
    requested_max_output_tokens: Any,
    context_estimate_tokens: int | None = None,
    model_profile: ModelProfile | None = None,
) -> OutputBudgetDecision:
    """Return the accepted output budget or a pre-provider failure decision."""

    profile = model_profile or require_model_profile(ProviderModelRef(provider, model))
    normalized_provider = normalize_provider_id(provider)
    normalized_role = str(role).strip() if role else "unspecified"
    requested = _coerce_optional_positive_int(requested_max_output_tokens)
    if requested is None:
        return OutputBudgetDecision(
            provider=normalized_provider,
            model=profile.ref.model,
            role=normalized_role,
            requested_max_tokens=None,
            accepted_max_tokens=None,
            model_max_output_tokens=profile.max_output_tokens,
            context_window_tokens=profile.context_window_tokens,
            context_fit=None,
            reason="no_explicit_output_budget",
            clamped=False,
            should_fail=False,
        )

    if requested <= 0:
        return OutputBudgetDecision(
            provider=normalized_provider,
            model=profile.ref.model,
            role=normalized_role,
            requested_max_tokens=requested,
            accepted_max_tokens=None,
            model_max_output_tokens=profile.max_output_tokens,
            context_window_tokens=profile.context_window_tokens,
            context_fit=None,
            reason="non_positive_output_budget",
            clamped=False,
            should_fail=True,
        )

    accepted = requested
    clamped = False
    reason = "accepted"
    should_fail = False
    if requested > profile.max_output_tokens:
        if _allows_legacy_clamp(normalized_provider):
            accepted = profile.max_output_tokens
            clamped = True
            reason = "clamped_to_model_max_output"
        else:
            accepted = None
            should_fail = True
            reason = "exceeds_model_max_output"

    context_fit: ContextFitDecision | None = None
    if not should_fail and context_estimate_tokens is not None and accepted is not None:
        context_fit = evaluate_context_fit(
            provider=normalized_provider,
            model=profile.ref.model,
            context_estimate_tokens=context_estimate_tokens,
            requested_output_tokens=accepted,
            model_profile=profile,
        )
        if not context_fit.fits:
            should_fail = True
            reason = "context_window_exceeded"

    return OutputBudgetDecision(
        provider=normalized_provider,
        model=profile.ref.model,
        role=normalized_role,
        requested_max_tokens=requested,
        accepted_max_tokens=accepted,
        model_max_output_tokens=profile.max_output_tokens,
        context_window_tokens=profile.context_window_tokens,
        context_fit=context_fit,
        reason=reason,
        clamped=clamped,
        should_fail=should_fail,
    )


def _coerce_optional_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _allows_legacy_clamp(provider: str) -> bool:
    return provider == OPENAI_PROVIDER_ID


__all__ = ["OutputBudgetDecision", "decide_output_budget"]
