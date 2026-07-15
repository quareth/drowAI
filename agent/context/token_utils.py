"""OpenAI-compatible token counting utilities.

Legacy call sites use these helpers as integer-returning convenience wrappers.
Provider-aware context-window decisions should use
``agent.context.token_counter_registry`` instead.
"""

from __future__ import annotations

from typing import Any
import logging

from agent.context.token_counter_registry import estimate_json_tokens, estimate_text_tokens
from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID

logger = logging.getLogger(__name__)


class TokenCounter:
    """Accurate token counting using tiktoken with fallback."""
    
    def __init__(self, model: str = "gpt-4"):
        self.model = model
        self.provider = OPENAI_PROVIDER_ID
    
    def count_tokens(self, text: str) -> int:
        """Count tokens accurately using tiktoken or fallback to approximation."""
        estimate = estimate_text_tokens(text, provider=self.provider, model=self.model)
        logger.debug(
            "Token estimate for model %s used strategy=%s precision=%s",
            self.model,
            estimate.strategy,
            estimate.precision,
        )
        return estimate.tokens
    
    def count_tokens_json(self, data: Any) -> int:
        """Count tokens in JSON-serializable data."""
        return estimate_json_tokens(
            data,
            provider=self.provider,
            model=self.model,
        ).tokens


# Global instance for easy access
_default_counter = None


def get_token_counter(model: str = "gpt-4") -> TokenCounter:
    """Get or create a token counter instance."""
    global _default_counter
    if _default_counter is None or _default_counter.model != model:
        _default_counter = TokenCounter(model)
    return _default_counter


def count_tokens(text: str, model: str = "gpt-4") -> int:
    """Convenience function to count tokens in text."""
    counter = get_token_counter(model)
    return counter.count_tokens(text)


def count_tokens_json(data: Any, model: str = "gpt-4") -> int:
    """Convenience function to count tokens in JSON-serializable data."""
    counter = get_token_counter(model)
    return counter.count_tokens_json(data)
