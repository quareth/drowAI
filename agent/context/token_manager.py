"""Dynamic token budget manager."""

from __future__ import annotations

import json
from typing import Any, Dict, Tuple
from pydantic import BaseModel

from .token_counter_registry import get_token_counter_for_model

DEFAULT_TOKEN_LIMITS = {
    "target": 3000,
    "breakdown": {
        "system_context": 600,
        "recent_cycles": 1200,
        "tool_results": 400,
        "artifacts": 800,
    },
    "importance_weights": {
        "system_context": 1.0,
        "recent_cycles": 0.9,
        "tool_results": 0.8,
        "artifacts": 0.75,
    },
}

PRIORITY = ["artifacts", "tool_results", "recent_cycles", "system_context"]


class TokenManager:
    """Manage token budgets for context sections."""

    def __init__(self, provider: str = "openai", config: Dict[str, Any] | None = None) -> None:
        self.provider = provider
        self.config = json.loads(json.dumps(DEFAULT_TOKEN_LIMITS))
        if config:
            for key, value in config.items():
                self.config[key] = value
        self._apply_provider_defaults(provider)
        self.target = self.config.get("target", 3000)
        self.breakdown = self.config.get("breakdown", {})
        self.weights = self.config.get("importance_weights", {})

        # Initialize token counter based on provider.
        self.model = self._get_model_for_provider(provider)
        self.token_counter = get_token_counter_for_model(
            provider=self.provider,
            model=self.model,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def fit_to_budget(self, context: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
        """Ensure provided context fits within the target token budget."""
        counts = self._measure_context(context)
        total = sum(counts.values())
        if total <= self.target:
            return context, total

        budgets = dict(self.breakdown)
        unused = 0
        deficits: Dict[str, int] = {}
        for section, alloc in self.breakdown.items():
            actual = counts.get(section, 0)
            if actual < alloc:
                unused += alloc - actual
            elif actual > alloc:
                deficits[section] = actual - alloc
        if deficits and unused:
            weight_sum = sum(self.weights.get(s, 1.0) for s in deficits)
            for sec, deficit in deficits.items():
                extra = int(unused * self.weights.get(sec, 1.0) / weight_sum)
                budgets[sec] += extra

        # Apply truncation if needed
        trimmed = {**context}
        for sec in PRIORITY:
            if sec == "system_context":
                continue
            allowed = budgets.get(sec, 0)
            trimmed[sec] = self._trim_section(trimmed.get(sec), allowed)
        if self._context_tokens(trimmed) > self.target:
            trimmed = self._emergency_truncate(trimmed)
        return trimmed, self._context_tokens(trimmed)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _get_model_for_provider(self, provider: str) -> str:
        """Get the appropriate model name for token counting based on provider."""
        provider_lower = provider.lower()
        if provider_lower.startswith("anthropic"):
            return "claude-sonnet-4-6"
        elif provider_lower.startswith("gemini"):
            return "gemini-pro"
        else:
            return "gpt-4"

    def _apply_provider_defaults(self, provider: str) -> None:
        if provider.lower().startswith("anthropic"):
            self.config["target"] = 3500
        elif provider.lower().startswith("gemini"):
            self.config["target"] = 2800

    def _measure_context(self, context: Dict[str, Any]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for section in ("system_context", "recent_cycles", "tool_results", "artifacts"):
            counts[section] = self._context_tokens(context.get(section))
        return counts

    def _approx_tokens(self, data: Any) -> int:
        """Legacy method for backward compatibility - now uses accurate counting."""
        return self._context_tokens(data)

    def _to_serializable(self, data: Any) -> Any:
        """Convert data to a JSON-serializable structure."""
        if isinstance(data, BaseModel):
            return data.model_dump()
        if isinstance(data, dict):
            return {k: self._to_serializable(v) for k, v in data.items()}
        if isinstance(data, list):
            return [self._to_serializable(v) for v in data]
        return data

    def _context_tokens(self, data: Any) -> int:
        """Count tokens in context data using accurate counting."""
        if data is None:
            return 0
        
        return self.token_counter.count_json(self._to_serializable(data)).tokens

    def _trim_section(self, data: Any, limit: int) -> Any:
        if data is None:
            return data
        while self._context_tokens(data) > limit:
            if isinstance(data, list) and data:
                data = data[1:]
            elif isinstance(data, dict) and len(data) > 1:
                # remove the first key alphabetically for deterministic behaviour
                key = sorted(data.keys())[0]
                data.pop(key)
            else:
                break
        return data

    def _emergency_truncate(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Apply progressive truncation when context still exceeds the limit."""
        trimmed = {**context}
        if self._context_tokens(trimmed.get("system_context")) >= self.target:
            # system context alone exceeds budget; nothing we can safely drop
            return trimmed

        while self._context_tokens(trimmed) > self.target:
            for sec in PRIORITY:
                if sec == "system_context":
                    continue
                val = trimmed.get(sec)
                if isinstance(val, list) and val:
                    trimmed[sec] = val[1:]
                    break
            else:
                break
        return trimmed
