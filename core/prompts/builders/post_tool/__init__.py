"""Public facade for post-tool prompt builder exports.

This package preserves the historical import surface while splitting the
implementation into small responsibility-focused modules.
"""

from ._formatting import MAX_TODOS_IN_PROMPT
from .builder import PostToolReasoningPromptBuilder
from .templates import (
    ARTICULATION_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    TASK_INSTRUCTION_PROMPT,
)

__all__ = [
    "PostToolReasoningPromptBuilder",
    "ARTICULATION_SYSTEM_PROMPT",
    "SYSTEM_PROMPT",
    "TASK_INSTRUCTION_PROMPT",
    "MAX_TODOS_IN_PROMPT",
]
