"""Provider-aware token counting contracts and local estimator registry.

This module owns token estimator selection for context-window decisions. It
keeps OpenAI compatibility on tiktoken while making non-OpenAI estimates
explicitly heuristic instead of silently reusing an OpenAI tokenizer.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Literal, Protocol

try:
    import tiktoken
except ModuleNotFoundError:  # pragma: no cover - exercised in minimal test envs.
    tiktoken = None  # type: ignore[assignment]

from agent.providers.llm.core.identity import (
    ANTHROPIC_PROVIDER_ID,
    OPENAI_PROVIDER_ID,
    normalize_model_id,
    normalize_provider_id,
)

TokenEstimatePrecision = Literal["exact", "approximate", "heuristic"]


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    """Token estimate with provider/model provenance and estimator precision."""

    tokens: int
    provider: str
    model: str
    strategy: str
    precision: TokenEstimatePrecision
    safety_margin_applied: float = 0.0


class TokenCounter(Protocol):
    """Provider-aware token counter contract used by context policy code."""

    def count_text(self, text: str | None) -> TokenEstimate:
        """Estimate tokens for plain text."""

    def count_json(self, value: Any) -> TokenEstimate:
        """Estimate tokens for JSON-serializable data."""


class _TiktokenCounter:
    """OpenAI-compatible local tiktoken estimator."""

    def __init__(self, *, provider: str, model: str) -> None:
        self._provider = provider
        self._model = model
        if tiktoken is None:
            self._encoding = None
            self._strategy = "tiktoken_unavailable_heuristic"
            self._precision = "heuristic"
            self._fallback_counter = _HeuristicCounter(
                provider=provider,
                model=model,
                strategy=self._strategy,
                chars_per_token=3.5,
                safety_margin=0.25,
            )
            return
        try:
            self._encoding = tiktoken.encoding_for_model(model)
            self._strategy = "tiktoken_model"
            self._precision: TokenEstimatePrecision = "exact"
        except KeyError:
            self._encoding = tiktoken.get_encoding("cl100k_base")
            self._strategy = "tiktoken_base_compatibility"
            self._precision = "approximate"
        self._fallback_counter = None

    def count_text(self, text: str | None) -> TokenEstimate:
        if self._fallback_counter is not None:
            return self._fallback_counter.count_text(text)
        if not text:
            tokens = 0
        else:
            tokens = len(self._encoding.encode(str(text)))
        return TokenEstimate(
            tokens=max(0, tokens),
            provider=self._provider,
            model=self._model,
            strategy=self._strategy,
            precision=self._precision,
            safety_margin_applied=0.0,
        )

    def count_json(self, value: Any) -> TokenEstimate:
        if self._fallback_counter is not None:
            return self._fallback_counter.count_json(value)
        if value is None:
            return self.count_text("")
        if isinstance(value, str):
            return self.count_text(value)
        return self.count_text(_stable_json(value))


class _HeuristicCounter:
    """Conservative local estimator for providers without verified tokenizers."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        strategy: str,
        chars_per_token: float,
        safety_margin: float,
    ) -> None:
        self._provider = provider
        self._model = model
        self._strategy = strategy
        self._chars_per_token = chars_per_token
        self._safety_margin = safety_margin

    def count_text(self, text: str | None) -> TokenEstimate:
        if not text:
            tokens = 0
        else:
            raw_tokens = math.ceil(len(str(text)) / self._chars_per_token)
            tokens = math.ceil(raw_tokens * (1.0 + self._safety_margin))
        return TokenEstimate(
            tokens=max(0, tokens),
            provider=self._provider,
            model=self._model,
            strategy=self._strategy,
            precision="heuristic",
            safety_margin_applied=self._safety_margin,
        )

    def count_json(self, value: Any) -> TokenEstimate:
        if value is None:
            return self.count_text("")
        if isinstance(value, str):
            return self.count_text(value)
        return self.count_text(_stable_json(value))


def get_token_counter_for_model(*, provider: str, model: str) -> TokenCounter:
    """Return the local token estimator for a provider/model pair."""
    normalized_provider = normalize_provider_id(provider)
    normalized_model = normalize_model_id(model)
    if normalized_provider == OPENAI_PROVIDER_ID:
        return _TiktokenCounter(provider=normalized_provider, model=normalized_model)
    if normalized_provider == ANTHROPIC_PROVIDER_ID:
        return _HeuristicCounter(
            provider=normalized_provider,
            model=normalized_model,
            strategy="anthropic_char_heuristic",
            chars_per_token=3.5,
            safety_margin=0.15,
        )
    return _HeuristicCounter(
        provider=normalized_provider,
        model=normalized_model,
        strategy="provider_agnostic_char_heuristic",
        chars_per_token=3.5,
        safety_margin=0.25,
    )


def estimate_text_tokens(
    text: str | None,
    *,
    provider: str,
    model: str,
) -> TokenEstimate:
    """Estimate tokens for plain text with provider/model provenance."""
    return get_token_counter_for_model(provider=provider, model=model).count_text(text)


def estimate_json_tokens(
    value: Any,
    *,
    provider: str,
    model: str,
) -> TokenEstimate:
    """Estimate tokens for JSON-like values with provider/model provenance."""
    return get_token_counter_for_model(provider=provider, model=model).count_json(value)


def estimate_llm_request_tokens(
    *,
    system_prompt: str,
    user_prompt: str,
    structured_output: Any,
    provider: str,
    model: str,
) -> TokenEstimate:
    """Estimate the prompt-bearing fields of one structured LLM request."""
    normalized_structured_output = (
        asdict(structured_output)
        if is_dataclass(structured_output) and not isinstance(structured_output, type)
        else structured_output
    )
    return estimate_json_tokens(
        {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "structured_output": normalized_structured_output,
        },
        provider=provider,
        model=model,
    )


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "TokenCounter",
    "TokenEstimate",
    "TokenEstimatePrecision",
    "estimate_json_tokens",
    "estimate_llm_request_tokens",
    "estimate_text_tokens",
    "get_token_counter_for_model",
]
