"""Configuration system for advanced context management."""

from __future__ import annotations

import json
import os
from typing import Any, Dict


class ContextConfig:
    """Adaptive configuration loaded from environment variables."""

    VALID_PROFILES = {"dev", "prod", "test"}
    VALID_COMPRESSION = {"none", "balanced", "aggressive"}

    def __init__(self, custom_config: Dict[str, Any] | None = None) -> None:
        self.target_token_limit = int(os.getenv("CONTEXT_TOKEN_LIMIT", "3000"))
        self.enable_compression = (
            os.getenv("ENABLE_COMPRESSION", "true").lower() == "true"
        )
        self.compression_level = os.getenv("COMPRESSION_LEVEL", "balanced")
        self.profile = os.getenv("CONTEXT_PROFILE", "prod")

        # Memory management configuration
        self.enable_memory_management = (
            os.getenv("ENABLE_MEMORY_MANAGEMENT", "true").lower() == "true"
        )
        self.token_history_limit = int(os.getenv("TOKEN_HISTORY_LIMIT", "100"))
        self.quality_metrics_limit = int(os.getenv("QUALITY_METRICS_LIMIT", "50"))
        self.working_memory_limit = int(os.getenv("WORKING_MEMORY_LIMIT", "10"))
        self.compressed_memories_limit = int(os.getenv("COMPRESSED_MEMORIES_LIMIT", "500"))
        self.tool_cache_limit = int(os.getenv("TOOL_CACHE_LIMIT", "100"))
        self.tool_cache_ttl = int(os.getenv("TOOL_CACHE_TTL", "3600"))  # 1 hour in seconds
        
        # Memory cleanup configuration
        self.enable_periodic_cleanup = (
            os.getenv("ENABLE_PERIODIC_CLEANUP", "true").lower() == "true"
        )
        self.cleanup_interval = int(os.getenv("CLEANUP_INTERVAL", "300"))  # 5 minutes
        self.memory_warning_threshold = float(os.getenv("MEMORY_WARNING_THRESHOLD", "80.0"))  # 80%
        self.memory_critical_threshold = float(os.getenv("MEMORY_CRITICAL_THRESHOLD", "95.0"))  # 95%

        self.breakdown: Dict[str, int] = {
            "system_context": 600,
            "recent_cycles": 1200,
            "tool_results": 400,
            # New budget slice for artifact retrieval (digests + excerpts)
            "artifacts": int(os.getenv("CONTEXT_ARTIFACTS_BUDGET", "800")),
        }

        if custom_config:
            self._apply_custom_config(custom_config)

    # ------------------------------------------------------------------
    # Runtime configuration
    # ------------------------------------------------------------------
    def update_from_env(self) -> None:
        """Reload configuration from environment variables."""
        self.__init__()

    def _apply_custom_config(self, cfg: Dict[str, Any]) -> None:
        for key, value in cfg.items():
            if key == "breakdown" and isinstance(value, dict):
                self.breakdown.update(value)
            else:
                setattr(self, key, value)

    def get_memory_config(self) -> Dict[str, Any]:
        """Get memory management configuration."""
        return {
            "token_history_limit": self.token_history_limit,
            "quality_metrics_limit": self.quality_metrics_limit,
            "working_memory_limit": self.working_memory_limit,
            "compressed_memories_limit": self.compressed_memories_limit,
            "tool_cache_limit": self.tool_cache_limit,
            "tool_cache_ttl": self.tool_cache_ttl,
        }

    def validate(self) -> bool:
        """Validate configuration values."""
        try:
            if self.profile not in self.VALID_PROFILES:
                return False
            if self.compression_level not in self.VALID_COMPRESSION:
                return False
            if self.target_token_limit <= 0:
                return False
            if self.token_history_limit <= 0:
                return False
            if self.quality_metrics_limit <= 0:
                return False
            if self.working_memory_limit <= 0:
                return False
            if self.compressed_memories_limit <= 0:
                return False
            if self.tool_cache_limit <= 0:
                return False
            if self.tool_cache_ttl <= 0:
                return False
            if self.cleanup_interval <= 0:
                return False
            if not (0 <= self.memory_warning_threshold <= 100):
                return False
            if not (0 <= self.memory_critical_threshold <= 100):
                return False
            if self.memory_warning_threshold >= self.memory_critical_threshold:
                return False  # Warning should be less than critical
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Provider optimization
    # ------------------------------------------------------------------
    def adjust_for_provider(self, provider: str) -> None:
        provider = provider.lower()
        if provider == "anthropic":
            self.target_token_limit = 3500
            # keep artifacts budget proportional
            self.breakdown["artifacts"] = max(self.breakdown.get("artifacts", 800), 800)
        elif provider == "gemini":
            self.target_token_limit = 2800
            self.compression_level = "aggressive"

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------
    def to_json(self) -> str:
        """Return JSON representation."""
        return json.dumps(
            {
                "target_token_limit": self.target_token_limit,
                "enable_compression": self.enable_compression,
                "compression_level": self.compression_level,
                "profile": self.profile,
                "breakdown": self.breakdown,
            },
            ensure_ascii=False,
        )


