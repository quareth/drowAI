"""
Application-layer authority for context-window policy decisions.

This module centralizes policy configuration for the context token ceiling
(default ``128000``) and exposes deterministic evaluation/snapshot helpers for
chat-scoped tracking keyed by ``(task_id, conversation_id)``.

Reuse gate contract:
- Token counting for conversation-compression decisions must reuse this module's
  provider-aware history estimation path (``estimate_history_tokens`` /
  ``evaluate_history``) instead of introducing duplicate estimators. The
  OpenAI-named methods remain compatibility wrappers only.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

from agent.context.context_window_policy import estimate_chat_history_tokens
from agent.context.token_counter_registry import TokenEstimate
from agent.providers.llm.core.exceptions import LLMProfileNotFoundError
from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID, ProviderModelRef
from agent.providers.llm.profiles.registry import resolve_context_window_tokens
from backend.services.langgraph_chat.compression.window_models import (
    ContextWindowDecision,
    ContextWindowSnapshot,
)

DEFAULT_CONTEXT_WINDOW_MAX_TOKENS = 128_000
DEFAULT_CONTEXT_COMPACTION_TRIGGER_RATIO = 0.80
TARGET_COMPACTION_RETAINED_TURNS = 5
MINIMUM_COMPACTION_RETAINED_TURNS = 3
CONTEXT_COMPACTION_TRIGGER_TOKENS_OVERRIDE = (
    "LANGGRAPH_CONTEXT_COMPACTION_TRIGGER_TOKENS_OVERRIDE"
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ClassifierPromptBudget:
    """Resolved hard prompt budget and soft compaction trigger for one request."""

    context_limit_tokens: int
    reserved_output_tokens: int
    usable_prompt_tokens: int
    trigger_tokens: int
    override_active: bool


def resolve_classifier_prompt_budget(
    *,
    context_limit_tokens: int,
    reserved_output_tokens: int,
    env_getter: Callable[[str], Optional[str]] = os.getenv,
) -> ClassifierPromptBudget:
    """Resolve the classifier's usable prompt budget and soft trigger."""
    if (
        isinstance(context_limit_tokens, bool)
        or not isinstance(context_limit_tokens, int)
        or context_limit_tokens <= 0
    ):
        raise ValueError("context_limit_tokens must be a positive integer")
    if (
        isinstance(reserved_output_tokens, bool)
        or not isinstance(reserved_output_tokens, int)
        or reserved_output_tokens <= 0
    ):
        raise ValueError("reserved_output_tokens must be a positive integer")

    usable_prompt_tokens = context_limit_tokens - reserved_output_tokens
    if usable_prompt_tokens <= 0:
        raise ValueError("reserved_output_tokens must be below context_limit_tokens")

    trigger_tokens = math.floor(
        usable_prompt_tokens * DEFAULT_CONTEXT_COMPACTION_TRIGGER_RATIO
    )
    override_active = False
    raw_override = env_getter(CONTEXT_COMPACTION_TRIGGER_TOKENS_OVERRIDE)
    if isinstance(raw_override, str) and raw_override.strip():
        try:
            override_tokens = int(raw_override.strip())
        except ValueError:
            override_tokens = 0
        if 0 < override_tokens < usable_prompt_tokens:
            trigger_tokens = override_tokens
            override_active = True
        else:
            logger.warning(
                "context_compaction.trigger_override override_active=false reason=invalid_value"
            )

    logger.info(
        "context_compaction.trigger_policy override_active=%s trigger_tokens=%s "
        "usable_prompt_tokens=%s context_limit_tokens=%s reserved_output_tokens=%s",
        override_active,
        trigger_tokens,
        usable_prompt_tokens,
        context_limit_tokens,
        reserved_output_tokens,
    )
    return ClassifierPromptBudget(
        context_limit_tokens=context_limit_tokens,
        reserved_output_tokens=reserved_output_tokens,
        usable_prompt_tokens=usable_prompt_tokens,
        trigger_tokens=trigger_tokens,
        override_active=override_active,
    )


def resolve_context_window_max_tokens(
    default: int = DEFAULT_CONTEXT_WINDOW_MAX_TOKENS,
    *,
    explicit_max_tokens: Optional[int] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> int:
    """Resolve max context tokens from explicit input, model profile, or default."""
    if explicit_max_tokens is not None:
        try:
            parsed_explicit = int(explicit_max_tokens)
        except (TypeError, ValueError):
            parsed_explicit = default
        return parsed_explicit if parsed_explicit > 0 else default

    profile_tokens = _resolve_profile_context_tokens(
        provider=provider,
        model=model,
    )
    return profile_tokens if profile_tokens is not None else default


def _resolve_profile_context_tokens(
    *,
    provider: Optional[str],
    model: Optional[str],
) -> Optional[int]:
    """Return a profile-backed context limit when provider/model are known."""
    if not model or not isinstance(model, str) or not model.strip():
        return None
    normalized_provider = provider if isinstance(provider, str) and provider.strip() else OPENAI_PROVIDER_ID
    try:
        return resolve_context_window_tokens(
            ProviderModelRef(normalized_provider, model.strip())
        )
    except (LLMProfileNotFoundError, TypeError, ValueError):
        return None


class ContextWindowManager:
    """Single authority for non-blocking context-window policy decisions."""

    def __init__(
        self,
        *,
        max_tokens: Optional[int] = None,
        policy_resolver: Optional[Callable[[], int]] = None,
    ) -> None:
        self._uses_default_policy_resolver = max_tokens is None and policy_resolver is None
        resolved_max_tokens = max_tokens
        if resolved_max_tokens is None:
            resolver = policy_resolver or resolve_context_window_max_tokens
            resolved_max_tokens = resolver()
        self._max_tokens = max(1, int(resolved_max_tokens))

    @property
    def max_tokens(self) -> int:
        """Configured context ceiling for this manager instance."""
        return self._max_tokens

    def evaluate(self, *, task_id: int, conversation_id: str, used_tokens: int) -> ContextWindowDecision:
        """Evaluate projected usage and return deterministic non-blocking decision."""
        return self._evaluate_with_max_tokens(
            task_id=task_id,
            conversation_id=conversation_id,
            used_tokens=used_tokens,
            max_tokens=self._max_tokens,
        )

    def evaluate_classifier_prompt(
        self,
        *,
        task_id: int,
        conversation_id: str,
        prompt_tokens: int,
        reserved_output_tokens: int,
        env_getter: Callable[[str], Optional[str]] = os.getenv,
    ) -> tuple[ContextWindowDecision, ClassifierPromptBudget]:
        """Evaluate exact classifier prompt usage against its soft trigger."""
        budget = resolve_classifier_prompt_budget(
            context_limit_tokens=self._max_tokens,
            reserved_output_tokens=reserved_output_tokens,
            env_getter=env_getter,
        )
        snapshot = self._build_snapshot_with_max_tokens(
            task_id=task_id,
            conversation_id=conversation_id,
            used_tokens=prompt_tokens,
            max_tokens=budget.context_limit_tokens,
        )
        trigger_reached = snapshot.used_tokens >= budget.trigger_tokens
        return (
            ContextWindowDecision(
                snapshot=snapshot,
                ceiling_reached=trigger_reached,
                recommended_next_action="compress" if trigger_reached else "none",
                compression_candidate=trigger_reached,
            ),
            budget,
        )

    def estimate_tokens_from_openai_history(
        self,
        *,
        history: list[dict[str, Any]],
        model: str,
        projected_user_message: Optional[str] = None,
    ) -> int:
        """Estimate context tokens from OpenAI-style history and projected user text."""
        return self.estimate_tokens_from_history(
            history=history,
            provider=OPENAI_PROVIDER_ID,
            model=model,
            projected_user_message=projected_user_message,
        )

    def estimate_history_tokens(
        self,
        *,
        history: list[dict[str, Any]],
        provider: str,
        model: str,
        projected_user_message: Optional[str] = None,
    ) -> TokenEstimate:
        """Return a provider-aware token estimate for chat history."""
        return estimate_chat_history_tokens(
            provider=provider,
            model=model,
            history=history,
            projected_user_message=projected_user_message,
        )

    def estimate_tokens_from_history(
        self,
        *,
        history: list[dict[str, Any]],
        provider: str,
        model: str,
        projected_user_message: Optional[str] = None,
    ) -> int:
        """Estimate context tokens from selected provider/model history."""
        return self.estimate_history_tokens(
            history=history,
            provider=provider,
            model=model,
            projected_user_message=projected_user_message,
        ).tokens

    def evaluate_openai_history(
        self,
        *,
        task_id: int,
        conversation_id: str,
        history: list[dict[str, Any]],
        model: str,
        projected_user_message: Optional[str] = None,
    ) -> ContextWindowDecision:
        """Evaluate context usage directly from OpenAI-style history payload."""
        return self.evaluate_history(
            task_id=task_id,
            conversation_id=conversation_id,
            history=history,
            provider=OPENAI_PROVIDER_ID,
            model=model,
            projected_user_message=projected_user_message,
        )

    def evaluate_history(
        self,
        *,
        task_id: int,
        conversation_id: str,
        history: list[dict[str, Any]],
        provider: str,
        model: str,
        projected_user_message: Optional[str] = None,
    ) -> ContextWindowDecision:
        """Evaluate context usage from selected provider/model history."""
        used_tokens = self.estimate_tokens_from_history(
            history=history,
            provider=provider,
            model=model,
            projected_user_message=projected_user_message,
        )
        max_tokens = (
            resolve_context_window_max_tokens(provider=provider, model=model)
            if self._uses_default_policy_resolver
            else self._max_tokens
        )
        return self._evaluate_with_max_tokens(
            task_id=task_id,
            conversation_id=conversation_id,
            used_tokens=used_tokens,
            max_tokens=max_tokens,
        )

    def build_snapshot(self, *, task_id: int, conversation_id: str, used_tokens: int) -> ContextWindowSnapshot:
        """Build a normalized context-window snapshot for one chat identity."""
        normalized_used_tokens = max(0, int(used_tokens))
        return self._build_snapshot_with_max_tokens(
            task_id=task_id,
            conversation_id=conversation_id,
            used_tokens=normalized_used_tokens,
            max_tokens=self._max_tokens,
        )

    def _evaluate_with_max_tokens(
        self,
        *,
        task_id: int,
        conversation_id: str,
        used_tokens: int,
        max_tokens: int,
    ) -> ContextWindowDecision:
        """Evaluate projected usage against the supplied ceiling."""
        snapshot = self._build_snapshot_with_max_tokens(
            task_id=task_id,
            conversation_id=conversation_id,
            used_tokens=used_tokens,
            max_tokens=max_tokens,
        )
        return ContextWindowDecision(
            snapshot=snapshot,
            ceiling_reached=snapshot.ceiling_reached,
            recommended_next_action="compress" if snapshot.ceiling_reached else "none",
            compression_candidate=snapshot.ceiling_reached,
        )

    @staticmethod
    def _build_snapshot_with_max_tokens(
        *,
        task_id: int,
        conversation_id: str,
        used_tokens: int,
        max_tokens: int,
    ) -> ContextWindowSnapshot:
        """Build a normalized snapshot against a caller-selected ceiling."""
        normalized_max_tokens = max(1, int(max_tokens))
        normalized_used_tokens = max(0, int(used_tokens))
        remaining_tokens = max(0, normalized_max_tokens - normalized_used_tokens)
        ratio = min(1.0, normalized_used_tokens / normalized_max_tokens)
        ceiling_reached = normalized_used_tokens >= normalized_max_tokens
        return ContextWindowSnapshot(
            task_id=task_id,
            conversation_id=conversation_id,
            max_tokens=normalized_max_tokens,
            used_tokens=normalized_used_tokens,
            remaining_tokens=remaining_tokens,
            ratio=ratio,
            ceiling_reached=ceiling_reached,
        )
