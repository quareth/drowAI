"""Conversation history management for post-tool reasoning.

This module provides backward-compatible re-exports from the shared
history_formatter utility module.

The actual implementation is in agent/graph/utils/history_formatter.py
to follow DRY principles and allow reuse across different nodes.
"""

from __future__ import annotations

# Re-export from shared utility module for backward compatibility
from ...utils.history_formatter import (
    # Constants
    MAX_HISTORY_ENTRIES,
    MAX_HISTORY_CONTENT_CHARS,
    # Core functions
    truncate_content,
    # Within-turn history functions (renamed for backward compatibility)
    build_iteration_history as build_conversation_history,
    build_iteration_history_from_state as build_conversation_history_from_state,
)


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # Constants
    "MAX_HISTORY_ENTRIES",
    "MAX_HISTORY_CONTENT_CHARS",
    # Functions
    "truncate_content",
    "build_conversation_history",
    "build_conversation_history_from_state",
]

