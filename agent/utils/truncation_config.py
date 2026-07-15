"""Centralized truncation configuration for tool output processing.

This module provides a single source of truth for all truncation-related
settings across the codebase. Modern LLMs have 128K+ context windows,
so aggressive truncation is counterproductive - it often triggers
file-reading loops that cost more tokens than showing the full output.

Design principles:
1. Type-aware thresholds: Help/version output needs different limits than scan results
2. Soft margins: Don't truncate if slightly over limit (avoids unnecessary loops)
3. Informational messaging: Guide LLM without triggering defensive file-reading
"""

from __future__ import annotations

from typing import Dict, Final


# -----------------------------------------------------------------------------
# Output Type Thresholds (characters)
# -----------------------------------------------------------------------------

# These thresholds are deliberately high - modern LLMs handle large context well.
# The cost of file-reading loops far exceeds the cost of showing more output.

THRESHOLDS: Final[Dict[str, int]] = {
    # Help/version output - effectively never truncate
    # These are always small and complete; truncating causes unnecessary loops
    "help": 12000,
    
    # Scan results (nmap, gobuster, etc.) - high threshold
    # Users need to see findings; truncation loses critical data
    "scan": 10000,
    
    # Log files - moderate threshold
    # Usually only recent entries matter; tail is more useful than head
    "log": 8000,
    
    # Default for unknown output types
    "default": 10000,
}


# -----------------------------------------------------------------------------
# Soft Truncation Margin
# -----------------------------------------------------------------------------

# Don't truncate if output is within this percentage of the limit.
# Example: 10000 char limit with 20% margin = don't truncate if < 12000 chars
# This prevents wasteful loops for small overages (e.g., 2500 chars vs 2000 limit)
SOFT_MARGIN_PERCENT: Final[int] = 20


# -----------------------------------------------------------------------------
# File Reading Hint Thresholds
# -----------------------------------------------------------------------------

# Only suggest reading the full file if this many chars were truncated.
# Small truncations (< 2000 chars) rarely contain critical missing info.
MIN_TRUNCATION_FOR_FILE_HINT: Final[int] = 2000


# -----------------------------------------------------------------------------
# Legacy Compatibility (deprecated - use THRESHOLDS instead)
# -----------------------------------------------------------------------------

# These are kept for backward compatibility during migration.
# New code should use THRESHOLDS["default"] instead.
DEFAULT_TOTAL_LIMIT: Final[int] = THRESHOLDS["default"]
DEFAULT_HEAD_CHARS: Final[int] = 4000  # Increased from 800
DEFAULT_TAIL_CHARS: Final[int] = 4000  # Increased from 800

# Executor adapter limits (now derived from thresholds)
STDOUT_SNIPPET: Final[int] = THRESHOLDS["default"]
STDERR_SNIPPET: Final[int] = 4000  # Errors are important, keep high

# Prompt display limits
MAX_STDOUT_EXCERPT_CHARS: Final[int] = 6000  # Increased from 1500


# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------

def get_threshold_for_type(output_type: str) -> int:
    """Get the truncation threshold for a given output type.
    
    Args:
        output_type: One of 'help', 'scan', 'log', or 'default'.
        
    Returns:
        Character limit for that output type.
    """
    return THRESHOLDS.get(output_type, THRESHOLDS["default"])


def get_effective_limit(base_limit: int) -> int:
    """Get the effective limit including soft margin.
    
    The soft margin allows outputs slightly over the limit to pass
    through without truncation, preventing unnecessary file-reading loops.
    
    Args:
        base_limit: The base truncation limit in characters.
        
    Returns:
        Effective limit with soft margin applied.
    """
    margin = int(base_limit * SOFT_MARGIN_PERCENT / 100)
    return base_limit + margin


def should_suggest_file_reading(chars_truncated: int) -> bool:
    """Determine if file-reading should be suggested.
    
    Only suggest reading the full file if a significant amount was truncated.
    Small truncations rarely contain critical information worth the overhead
    of additional tool calls.
    
    Args:
        chars_truncated: Number of characters that were truncated.
        
    Returns:
        True if file-reading hint should be shown.
    """
    return chars_truncated >= MIN_TRUNCATION_FOR_FILE_HINT


# -----------------------------------------------------------------------------
# Exports
# -----------------------------------------------------------------------------

__all__ = [
    # Type thresholds
    "THRESHOLDS",
    "get_threshold_for_type",
    
    # Soft margin
    "SOFT_MARGIN_PERCENT",
    "get_effective_limit",
    
    # File reading hints
    "MIN_TRUNCATION_FOR_FILE_HINT",
    "should_suggest_file_reading",
    
    # Legacy compatibility
    "DEFAULT_TOTAL_LIMIT",
    "DEFAULT_HEAD_CHARS",
    "DEFAULT_TAIL_CHARS",
    "STDOUT_SNIPPET",
    "STDERR_SNIPPET",
    "MAX_STDOUT_EXCERPT_CHARS",
]


