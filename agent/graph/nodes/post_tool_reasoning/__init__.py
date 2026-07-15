"""Post-tool reasoning package for deep reasoning flows.

This package provides unified post-tool reasoning with a decision-only
structured call plus separate observation articulation.

The package is modularized into:
- models.py: Pydantic output models (PostToolReasoningOutput, ToolIntent, etc.)
- parser.py: Response parsing and validation logic
- history.py: Conversation history building
- progress.py: Todo progress tracking
- node.py: Main node function and supporting logic

All public symbols are re-exported here for backward compatibility.
External code should import from this package:

    from agent.graph.nodes.post_tool_reasoning import (
        post_tool_reasoning,
        PostToolReasoningOutput,
        PostToolReasoningError,
    )
"""

# Re-export models
from .models import (
    PostToolReasoningError,
    PostToolReasoningDecisionOutput,
    PostToolReasoningOutput,
    map_decision_output_to_post_tool_reasoning_output,
    TodoProgress,
    ToolIntent,
)

# Re-export parser functions and constants
from .parser import (
    VALID_POST_TOOL_ACTIONS,
    VALID_TODO_STATUSES,
    extract_json_from_text,
    parse_reasoning_response,
    split_observation_and_decision,
)

# Re-export history functions and constants
from .history import (
    MAX_HISTORY_CONTENT_CHARS,
    MAX_HISTORY_ENTRIES,
    build_conversation_history,
    build_conversation_history_from_state,
    truncate_content,
)

# Re-export progress functions
from .progress import (
    apply_progress_updates,
    build_progress_summary,
)

# Re-export streaming constants
from .streaming.base import MAX_REASONING_TOKENS, STREAMING_STEP_NAME
from .streaming_compat import non_streaming_call, stream_and_parse_response

# Re-export moved helpers/constants
from ...utils.event_identity import derive_dr_stream_identifiers
from .core.observation import MAX_OBSERVATION_TOKENS

# Re-export recorder functions
from .recorders import (
    format_tool_intent_for_hint,
    record_decision,
    record_observation,
)

# Re-export constants and functions from node
from .node import (
    # Constants
    MAX_TODOS_IN_PROMPT,
    # Public functions
    post_tool_reasoning,
)

# Backward compatibility aliases for internal functions
# (tests may import these with underscore prefix)
_extract_json_from_text = extract_json_from_text
_parse_reasoning_response = parse_reasoning_response
_split_observation_and_decision = split_observation_and_decision
_truncate_content = truncate_content
_build_conversation_history = build_conversation_history
_apply_progress_updates = apply_progress_updates
_build_progress_summary = build_progress_summary
_stream_and_parse_response = stream_and_parse_response
_non_streaming_call = non_streaming_call
_format_tool_intent_for_hint = format_tool_intent_for_hint
_record_decision = record_decision
_record_observation = record_observation

__all__ = [
    # Exceptions
    "PostToolReasoningError",
    # Models
    "PostToolReasoningOutput",
    "PostToolReasoningDecisionOutput",
    "TodoProgress",
    "ToolIntent",
    # Constants
    "MAX_HISTORY_CONTENT_CHARS",
    "MAX_HISTORY_ENTRIES",
    "MAX_OBSERVATION_TOKENS",
    "MAX_REASONING_TOKENS",
    "MAX_TODOS_IN_PROMPT",
    "STREAMING_STEP_NAME",
    "VALID_POST_TOOL_ACTIONS",
    "VALID_TODO_STATUSES",
    # Public functions
    "apply_progress_updates",
    "build_conversation_history",
    "build_conversation_history_from_state",
    "build_progress_summary",
    "map_decision_output_to_post_tool_reasoning_output",
    "extract_json_from_text",
    "format_tool_intent_for_hint",
    "non_streaming_call",
    "parse_reasoning_response",
    "post_tool_reasoning",
    "record_decision",
    "record_observation",
    "split_observation_and_decision",
    "stream_and_parse_response",
    "truncate_content",
    "derive_dr_stream_identifiers",
    # Internal functions exposed for testing (backward compatibility)
    "_apply_progress_updates",
    "_build_conversation_history",
    "_build_progress_summary",
    "_extract_json_from_text",
    "_format_tool_intent_for_hint",
    "_non_streaming_call",
    "_parse_reasoning_response",
    "_record_decision",
    "_record_observation",
    "_split_observation_and_decision",
    "_stream_and_parse_response",
    "_truncate_content",
]
