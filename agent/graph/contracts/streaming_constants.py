"""Canonical streaming event constants shared across LangGraph graphs.

These constants define the phases (`ind` values) and step type labels that
backend helpers and frontend consumers rely on. All graphs should emit events
using these values so the UI remains graph-agnostic.
"""

REASONING_PHASE_INDEX = 0
TOOL_PHASE_INDEX = 1
ANSWER_PHASE_INDEX = 2
OBSERVATION_PHASE_INDEX = 3

STEP_MESSAGE_START = "message_start"
STEP_MESSAGE_DELTA = "message_delta"
STEP_MESSAGE_SECTION_END = "message_section_end"
STEP_ASSISTANT_DELTA = "assistant_delta"
STEP_ASSISTANT_MESSAGE = "assistant_message"

STEP_REASONING_START = "reasoning_start"
STEP_REASONING_DELTA = "reasoning_delta"
STEP_REASONING_SECTION_END = "reasoning_section_end"

STEP_TOOL_START = "tool_start"
STEP_TOOL_DELTA = "tool_delta"
STEP_TOOL_END = "tool_end"

STEP_OBSERVATION_START = "observation_start"
STEP_OBSERVATION_DELTA = "observation_delta"
STEP_OBSERVATION_SECTION_END = "observation_section_end"

STEP_RETRY_START = "retry_start"
STEP_RETRY_ATTEMPT = "retry_attempt"

__all__ = [
    "REASONING_PHASE_INDEX",
    "TOOL_PHASE_INDEX",
    "ANSWER_PHASE_INDEX",
    "OBSERVATION_PHASE_INDEX",
    "STEP_MESSAGE_START",
    "STEP_MESSAGE_DELTA",
    "STEP_MESSAGE_SECTION_END",
    "STEP_ASSISTANT_DELTA",
    "STEP_ASSISTANT_MESSAGE",
    "STEP_REASONING_START",
    "STEP_REASONING_DELTA",
    "STEP_REASONING_SECTION_END",
    "STEP_TOOL_START",
    "STEP_TOOL_DELTA",
    "STEP_TOOL_END",
    "STEP_OBSERVATION_START",
    "STEP_OBSERVATION_DELTA",
    "STEP_OBSERVATION_SECTION_END",
    "STEP_RETRY_START",
    "STEP_RETRY_ATTEMPT",
]

