"""Shared predicates for post-tool metadata flags used by graph routing.

This module exposes small, side-effect-free predicates that read PTR-set
metadata flags. Builders (deep_reasoning_builder, simple_tool_builder)
and the decision-router guardrails consume these predicates so terminal
metadata semantics stay consistent across post-tool routing surfaces.

Boundary
--------
This module is intentionally metadata-only. It must not import builders,
graph nodes, backend services, LangGraph, prompt builders, or any
runtime-side helpers. Helpers accept ``Mapping[str, Any]`` and return
``bool``; they never mutate the metadata they read.
"""

from __future__ import annotations

from typing import Any, Mapping


USER_GOAL_ACHIEVED_METADATA_KEY = "user_goal_achieved"
REQUEST_CONTRACT_TERMINAL_METADATA_KEY = "request_contract_terminal"


def user_goal_achieved(metadata: Mapping[str, Any]) -> bool:
    """Return True when PTR marked the user's requested goal complete.

    Reads ``metadata["user_goal_achieved"]``. PTR sets this flag when the
    LLM-driven post-tool reasoning concludes the user's goal has been
    satisfied; it is a hard finalize signal for graph routing.
    """
    return metadata.get(USER_GOAL_ACHIEVED_METADATA_KEY) is True


def request_contract_terminal(metadata: Mapping[str, Any]) -> bool:
    """Return True when request-contract policy marked this turn terminal.

    Reads ``metadata["request_contract_terminal"]``. Set by the PTR
    request-contract policy when the recorded request contract resolves to
    a terminal answer (binary or short determinations). Treated as a hard
    finalize signal alongside ``user_goal_achieved``.
    """
    return metadata.get(REQUEST_CONTRACT_TERMINAL_METADATA_KEY) is True


def post_tool_terminal(metadata: Mapping[str, Any]) -> bool:
    """Return True when post-tool metadata requires terminal routing.

    The combined terminal predicate: True when either ``user_goal_achieved``
    or ``request_contract_terminal`` is set in metadata. Used by builders
    to short-circuit post-tool routing to the graph's finalize target
    before consulting ``decision_history``.
    """
    return user_goal_achieved(metadata) or request_contract_terminal(metadata)


__all__ = [
    "USER_GOAL_ACHIEVED_METADATA_KEY",
    "REQUEST_CONTRACT_TERMINAL_METADATA_KEY",
    "user_goal_achieved",
    "request_contract_terminal",
    "post_tool_terminal",
]
