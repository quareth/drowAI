"""Shared LLM timeout configuration for backend and agent runtime roles.

This module is the single authority for app-level LLM timeout policy.
Named timeout constants default to ``DEFAULT_LLM_TIMEOUT_SEC`` (120 seconds).
Legacy env names such as ``LANGGRAPH_INTENT_CLASSIFIER_TIMEOUT_SEC`` and
``LLM_TOOL_SELECTION_TIMEOUT`` are not supported; import constants from here.
"""

from __future__ import annotations

import os

DEFAULT_LLM_TIMEOUT_SEC = 120


def _read_positive_int_env(
    primary_key: str,
    default: int,
    *,
    fallback_keys: tuple[str, ...] = (),
) -> int:
    """Return the first positive integer env value or the provided default."""
    for key in (primary_key, *fallback_keys):
        raw = os.getenv(key)
        if raw is None:
            continue
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return default


LLM_TIMEOUT_INTENT_CLASSIFIER_SEC = DEFAULT_LLM_TIMEOUT_SEC
LLM_TIMEOUT_CONVERSATION_MAIN_SEC = _read_positive_int_env(
    "LLM_TIMEOUT_CONVERSATION_MAIN_SEC",
    DEFAULT_LLM_TIMEOUT_SEC,
)
LLM_STREAM_IDLE_TIMEOUT_CONVERSATION_MAIN_SEC = _read_positive_int_env(
    "LLM_STREAM_IDLE_TIMEOUT_CONVERSATION_MAIN_SEC",
    DEFAULT_LLM_TIMEOUT_SEC,
)
LLM_TIMEOUT_REASONING_MAIN_SEC = _read_positive_int_env(
    "LLM_TIMEOUT_REASONING_MAIN_SEC",
    DEFAULT_LLM_TIMEOUT_SEC,
)
LLM_TIMEOUT_REFLECT_SEC = _read_positive_int_env(
    "LLM_TIMEOUT_REFLECT_SEC",
    300,
)
LLM_TIMEOUT_POST_TOOL_OBSERVATION_SEC = _read_positive_int_env(
    "LLM_TIMEOUT_POST_TOOL_OBSERVATION_SEC",
    DEFAULT_LLM_TIMEOUT_SEC,
)
LLM_STREAM_IDLE_TIMEOUT_POST_TOOL_OBSERVATION_SEC = _read_positive_int_env(
    "LLM_STREAM_IDLE_TIMEOUT_POST_TOOL_OBSERVATION_SEC",
    DEFAULT_LLM_TIMEOUT_SEC,
)
LLM_TIMEOUT_POST_TOOL_ARTICULATOR_SEC = _read_positive_int_env(
    "LLM_TIMEOUT_POST_TOOL_ARTICULATOR_SEC",
    DEFAULT_LLM_TIMEOUT_SEC,
)
LLM_STREAM_IDLE_TIMEOUT_POST_TOOL_ARTICULATOR_SEC = _read_positive_int_env(
    "LLM_STREAM_IDLE_TIMEOUT_POST_TOOL_ARTICULATOR_SEC",
    DEFAULT_LLM_TIMEOUT_SEC,
)
LLM_TIMEOUT_CONTEXT_COMPRESSOR_SEC = _read_positive_int_env(
    "LLM_TIMEOUT_CONTEXT_COMPRESSOR_SEC",
    DEFAULT_LLM_TIMEOUT_SEC,
)
LLM_TIMEOUT_TOOL_OUTPUT_COMPRESSOR_SEC = _read_positive_int_env(
    "LLM_TIMEOUT_TOOL_OUTPUT_COMPRESSOR_SEC",
    DEFAULT_LLM_TIMEOUT_SEC,
    fallback_keys=("TOOL_PROCESSOR_TIMEOUT",),
)
LLM_TIMEOUT_TOOL_CATEGORY_SELECTOR_SEC = _read_positive_int_env(
    "LLM_TIMEOUT_TOOL_CATEGORY_SELECTOR_SEC",
    DEFAULT_LLM_TIMEOUT_SEC,
)
LLM_TIMEOUT_PLANNER_TOOL_SELECTION_SEC = _read_positive_int_env(
    "LLM_TIMEOUT_PLANNER_TOOL_SELECTION_SEC",
    DEFAULT_LLM_TIMEOUT_SEC,
)
LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC = _read_positive_int_env(
    "LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC",
    DEFAULT_LLM_TIMEOUT_SEC,
    fallback_keys=("PLANNER_TOOL_CALL_TIMEOUT_SEC", "TOOL_CALL_TIMEOUT"),
)
LLM_TIMEOUT_MEMORY_GATE_SEC = _read_positive_int_env(
    "LLM_TIMEOUT_MEMORY_GATE_SEC",
    DEFAULT_LLM_TIMEOUT_SEC,
)
LLM_TIMEOUT_MEMORY_EXTRACTION_SEC = _read_positive_int_env(
    "LLM_TIMEOUT_MEMORY_EXTRACTION_SEC",
    DEFAULT_LLM_TIMEOUT_SEC,
)
LLM_TIMEOUT_KNOWLEDGE_CANDIDATE_EXTRACTION_SEC = _read_positive_int_env(
    "LLM_TIMEOUT_KNOWLEDGE_CANDIDATE_EXTRACTION_SEC",
    DEFAULT_LLM_TIMEOUT_SEC,
)


def read_llm_timeout_planner_tool_selection_sec(
    default: int = LLM_TIMEOUT_PLANNER_TOOL_SELECTION_SEC,
) -> int:
    """Read planner tool-selection timeout from canonical env."""
    return _read_positive_int_env(
        "LLM_TIMEOUT_PLANNER_TOOL_SELECTION_SEC",
        default,
    )


def read_llm_timeout_planner_parameter_resolution_sec(
    default: int = LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC,
) -> int:
    """Read planner parameter-resolution timeout with legacy-env compatibility."""
    return _read_positive_int_env(
        "LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC",
        default,
        fallback_keys=("PLANNER_TOOL_CALL_TIMEOUT_SEC", "TOOL_CALL_TIMEOUT"),
    )


__all__ = [
    "DEFAULT_LLM_TIMEOUT_SEC",
    "LLM_STREAM_IDLE_TIMEOUT_CONVERSATION_MAIN_SEC",
    "LLM_STREAM_IDLE_TIMEOUT_POST_TOOL_ARTICULATOR_SEC",
    "LLM_STREAM_IDLE_TIMEOUT_POST_TOOL_OBSERVATION_SEC",
    "LLM_TIMEOUT_CONTEXT_COMPRESSOR_SEC",
    "LLM_TIMEOUT_CONVERSATION_MAIN_SEC",
    "LLM_TIMEOUT_INTENT_CLASSIFIER_SEC",
    "LLM_TIMEOUT_KNOWLEDGE_CANDIDATE_EXTRACTION_SEC",
    "LLM_TIMEOUT_MEMORY_EXTRACTION_SEC",
    "LLM_TIMEOUT_MEMORY_GATE_SEC",
    "LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC",
    "LLM_TIMEOUT_PLANNER_TOOL_SELECTION_SEC",
    "LLM_TIMEOUT_POST_TOOL_ARTICULATOR_SEC",
    "LLM_TIMEOUT_POST_TOOL_OBSERVATION_SEC",
    "LLM_TIMEOUT_REFLECT_SEC",
    "LLM_TIMEOUT_REASONING_MAIN_SEC",
    "LLM_TIMEOUT_TOOL_CATEGORY_SELECTOR_SEC",
    "LLM_TIMEOUT_TOOL_OUTPUT_COMPRESSOR_SEC",
    "_read_positive_int_env",
    "read_llm_timeout_planner_parameter_resolution_sec",
    "read_llm_timeout_planner_tool_selection_sec",
]
