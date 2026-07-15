"""Intent contract policy helpers for post-tool reasoning.

The override policy that previously coerced LLM ``finalize`` decisions to
``call_tool`` based on regex/keyword matches over the literal user message
has been removed (see worktree-merge-recovery Bucket 1). The LLM is now the
sole authority for intent classification.

The contract evaluator (``_evaluate_simple_tool_intent_contract``) and its
extraction helpers stay because their output is consumed by the post-tool
prompt builder (see ``core/prompts/builders/post_tool/builder.py``) — the
LLM still receives "expected vs executed" as input on the next turn and
makes the call itself.
"""

from .extraction import (
    _PORT_FLAG_PATTERN,
    _PORT_WORD_PATTERN,
    _RELATIONAL_TARGET_PATTERN,
    _TARGET_TOKEN_PATTERN,
    _TOOL_ALIAS_NORMALIZATION,
    _dedupe_preserve,
    _extract_executed_ports,
    _extract_executed_targets,
    _extract_expected_ports,
    _extract_expected_targets,
    _extract_expected_tools,
    _extract_target_port,
    _normalize_target_token,
    _normalize_tool_alias,
    _parse_port_range,
    _parse_port_tokens,
)
from .matching import _evaluate_simple_tool_intent_contract, _ports_match

__all__ = [
    "_TARGET_TOKEN_PATTERN",
    "_RELATIONAL_TARGET_PATTERN",
    "_PORT_FLAG_PATTERN",
    "_PORT_WORD_PATTERN",
    "_TOOL_ALIAS_NORMALIZATION",
    "_dedupe_preserve",
    "_normalize_tool_alias",
    "_normalize_target_token",
    "_extract_target_port",
    "_parse_port_tokens",
    "_parse_port_range",
    "_extract_expected_tools",
    "_extract_expected_targets",
    "_extract_expected_ports",
    "_extract_executed_targets",
    "_extract_executed_ports",
    "_ports_match",
    "_evaluate_simple_tool_intent_contract",
]
