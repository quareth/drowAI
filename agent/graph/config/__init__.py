"""
Graph configuration module.

Centralizes all configurable parameters for LangGraph nodes.
"""

from .token_limits import LIMITS, TokenLimits

__all__ = ["LIMITS", "TokenLimits"]
