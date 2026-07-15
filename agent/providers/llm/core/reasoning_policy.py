"""Provider-profile-backed reasoning effort validation.

This module owns reasoning-effort normalization that depends on provider/model
profiles. It deliberately avoids backend imports, credential access, and client
construction.
"""

from __future__ import annotations

from typing import Optional

from .capabilities import LLMCapability
from .exceptions import LLMProfileNotFoundError
from .identity import OPENAI_PROVIDER_ID, ProviderModelRef, normalize_model_id
from ..profiles import require_model_profile

CANONICAL_REASONING_EFFORT_VALUES: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

ROLE_POLICY_XHIGH_ERROR = (
    "Invalid reasoning_effort 'xhigh'. This value is only allowed for models that support xhigh."
)
OPENAI_RESPONSES_XHIGH_ERROR = (
    "reasoning_effort 'xhigh' is only allowed for models that support xhigh."
)


def validate_reasoning_effort_for_provider_model(
    *,
    effort: Optional[str],
    model: Optional[str],
    provider: Optional[str] = None,
    xhigh_error_message: str = ROLE_POLICY_XHIGH_ERROR,
) -> Optional[str]:
    """Validate and normalize a reasoning effort using provider/model profiles."""
    if effort is None:
        return None

    normalized_effort = _normalize_reasoning_effort(effort)
    if normalized_effort is None:
        allowed = "|".join(CANONICAL_REASONING_EFFORT_VALUES)
        raise ValueError(f"Invalid reasoning_effort '{effort}'. Allowed values: {allowed}.")

    normalized_provider = _normalize_provider(provider)
    normalized_model = _normalize_optional_model(model)
    profile_found, profile_supports_reasoning, profile_efforts = _profile_reasoning_policy(
        provider=normalized_provider,
        model=normalized_model,
    )
    normalized_effort = _coerce_openai_reasoning_effort(
        provider=normalized_provider,
        model=normalized_model,
        effort=normalized_effort,
    )

    if (
        (profile_found or normalized_provider != OPENAI_PROVIDER_ID)
        and not profile_supports_reasoning
        and profile_efforts == frozenset()
    ):
        raise ValueError(
            f"reasoning_effort is not supported for provider '{normalized_provider}' "
            f"and model '{normalized_model}'."
        )
    if profile_efforts and normalized_effort not in profile_efforts:
        if normalized_effort == "xhigh":
            raise ValueError(xhigh_error_message)
        allowed = "|".join(sorted(profile_efforts))
        raise ValueError(
            f"Invalid reasoning_effort '{effort}' for model '{normalized_model}'. "
            f"Allowed values: {allowed}."
        )
    if not profile_efforts and normalized_effort == "xhigh":
        raise ValueError(xhigh_error_message)
    return normalized_effort


def _normalize_reasoning_effort(effort: str) -> Optional[str]:
    normalized = effort.strip().lower()
    if not normalized:
        return None
    if normalized not in CANONICAL_REASONING_EFFORT_VALUES:
        return None
    return normalized


def _normalize_provider(provider: Optional[str]) -> str:
    if isinstance(provider, str) and provider.strip():
        return provider.strip().lower()
    return OPENAI_PROVIDER_ID


def _normalize_optional_model(model: Optional[str]) -> Optional[str]:
    if not isinstance(model, str) or not model.strip():
        return None
    return normalize_model_id(model)


def _profile_reasoning_policy(
    *,
    provider: str,
    model: Optional[str],
) -> tuple[bool, bool, frozenset[str]]:
    if model is None:
        return False, False, frozenset()
    try:
        profile = require_model_profile(ProviderModelRef(provider, model))
    except LLMProfileNotFoundError:
        return False, False, frozenset()
    return True, profile.supports(LLMCapability.REASONING_EFFORT), profile.reasoning_efforts


def _coerce_openai_reasoning_effort(
    *,
    provider: str,
    model: Optional[str],
    effort: str,
) -> str:
    if provider != OPENAI_PROVIDER_ID or model is None:
        return effort
    if model in {"gpt-5.2-pro", "gpt-5.4-pro"} and effort in {"minimal", "none"}:
        return "medium"
    if model == "gpt-5.5-pro" and effort in {"minimal", "none"}:
        return "high"
    if effort == "minimal" and model == "gpt-5.2":
        return "none"
    if effort == "minimal" and model.startswith("gpt-5.2"):
        return "medium"
    if effort == "minimal" and model.startswith(("gpt-5.4", "gpt-5.5")):
        return "medium"
    return effort


__all__ = [
    "CANONICAL_REASONING_EFFORT_VALUES",
    "OPENAI_RESPONSES_XHIGH_ERROR",
    "ROLE_POLICY_XHIGH_ERROR",
    "validate_reasoning_effort_for_provider_model",
]
