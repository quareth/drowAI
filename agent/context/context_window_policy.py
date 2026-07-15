"""Provider-aware context-window estimation helpers.

This module keeps reusable context token estimation separate from the backend
compression manager so all callers share one provider-aware policy surface.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, cast

from agent.context.token_counter_registry import (
    TokenEstimate,
    TokenEstimatePrecision,
    estimate_json_tokens,
)
from agent.providers.llm.core.identity import ProviderModelRef, normalize_provider_id
from agent.providers.llm.profiles.registry import ModelProfile, require_model_profile


@dataclass(frozen=True, slots=True)
class ContextFitDecision:
    """Context-window fit decision for estimated history plus output budget."""

    provider: str
    model: str
    context_estimate_tokens: int
    requested_output_tokens: int
    context_window_tokens: int
    fits: bool
    overflow_tokens: int
    recommended_action: str


def estimate_chat_history_tokens(
    *,
    provider: str,
    model: str,
    history: list[dict[str, Any]],
    projected_user_message: str | None = None,
) -> TokenEstimate:
    """Estimate OpenAI-style chat history tokens for any selected provider."""
    estimates: list[TokenEstimate] = [
        estimate_json_tokens(message, provider=provider, model=model)
        for message in history
    ]
    if projected_user_message is not None:
        estimates.append(
            estimate_json_tokens(
                {"role": "user", "content": projected_user_message},
                provider=provider,
                model=model,
            )
        )
    if not estimates:
        return estimate_json_tokens("", provider=provider, model=model)

    first = estimates[0]
    return replace(
        first,
        tokens=sum(max(0, estimate.tokens) for estimate in estimates),
        precision=_least_precise(estimate.precision for estimate in estimates),
        safety_margin_applied=max(
            estimate.safety_margin_applied for estimate in estimates
        ),
    )


def evaluate_context_fit(
    *,
    provider: str,
    model: str,
    context_estimate_tokens: int,
    requested_output_tokens: int,
    model_profile: ModelProfile | None = None,
) -> ContextFitDecision:
    """Evaluate whether estimated context plus output budget fits the profile."""

    profile = model_profile or require_model_profile(ProviderModelRef(provider, model))
    normalized_context = max(0, int(context_estimate_tokens or 0))
    normalized_output = max(0, int(requested_output_tokens or 0))
    projected_total = normalized_context + normalized_output
    overflow = max(0, projected_total - profile.context_window_tokens)
    return ContextFitDecision(
        provider=normalize_provider_id(provider),
        model=profile.ref.model,
        context_estimate_tokens=normalized_context,
        requested_output_tokens=normalized_output,
        context_window_tokens=profile.context_window_tokens,
        fits=overflow == 0,
        overflow_tokens=overflow,
        recommended_action="none" if overflow == 0 else "compress",
    )


def _least_precise(precisions: Iterable[str]) -> TokenEstimatePrecision:
    order = {"exact": 0, "approximate": 1, "heuristic": 2}
    selected: TokenEstimatePrecision = "exact"
    selected_rank = 0
    for precision in precisions:
        candidate = str(precision)
        rank = order.get(candidate, 2)
        if rank >= selected_rank:
            selected = cast(
                TokenEstimatePrecision,
                candidate if candidate in order else "heuristic",
            )
            selected_rank = rank
    return selected


__all__ = [
    "ContextFitDecision",
    "estimate_chat_history_tokens",
    "evaluate_context_fit",
]
