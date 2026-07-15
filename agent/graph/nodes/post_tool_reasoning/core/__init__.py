"""Core capability-agnostic recovery logic.

This package contains pure functions for failure detection, retry logic,
and LLM analysis without any capability-specific conditionals.
"""

from .failure_detection import (
    FailureContext,
    detect_failure,
    classify_failure_category,
    build_failure_context_from_state,
)
from .retry_logic import (
    FAILURE_DETECTED_METADATA_KEY,
    MAX_RETRIES,
    RETRY_METADATA_KEY,
    RETRY_SUGGESTED_METADATA_KEY,
    can_retry,
    get_retry_count,
    increment_retry_count,
    retry_suggested,
)
from .llm_analysis import (
    MAX_REASONING_TOKENS,
    DEFAULT_TEMPERATURE,
    analyze_tool_result,
    analyze_tool_result_with_retry,
    build_analysis_context,
)
from .observation import (
    MAX_OBSERVATION_TOKENS,
    _ensure_min_length_observation,
    _generate_observation_text,
    _make_fallback_observation,
)

__all__ = [
    # Failure detection
    "FailureContext",
    "detect_failure",
    "classify_failure_category",
    "build_failure_context_from_state",
    # Retry logic
    "MAX_RETRIES",
    "RETRY_METADATA_KEY",
    "RETRY_SUGGESTED_METADATA_KEY",
    "FAILURE_DETECTED_METADATA_KEY",
    "retry_suggested",
    "get_retry_count",
    "can_retry",
    "increment_retry_count",
    # LLM analysis
    "MAX_REASONING_TOKENS",
    "DEFAULT_TEMPERATURE",
    "analyze_tool_result",
    "analyze_tool_result_with_retry",
    "build_analysis_context",
    # Observation generation
    "MAX_OBSERVATION_TOKENS",
    "_make_fallback_observation",
    "_ensure_min_length_observation",
    "_generate_observation_text",
]
