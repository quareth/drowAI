"""Internal commit-tool contract for one-turn post-action reasoning.

The tool in this module is a control-plane decision, not an executable pentest
tool. It lets a model emit ordinary visible text and commit exactly one
provider-neutral graph route in the same assistant turn.
"""

from __future__ import annotations

import json
from typing import Sequence

from agent.providers.llm.contracts.tool_contracts import FunctionToolSpec
from agent.providers.llm.core.base import ToolCall
from core.llm.structured_schemas import build_post_tool_decision_structured_output

from .models import PostToolReasoningDecisionOutput

PTR_COMMIT_TOOL_NAME = "ptr_commit"


def build_post_tool_commit_tool(
    subagent_names: Sequence[str] = (),
) -> FunctionToolSpec:
    """Build the single expanded PTR decision function."""
    decision_schema = build_post_tool_decision_structured_output(
        subagent_names
    ).schema
    return FunctionToolSpec(
        tool_id=PTR_COMMIT_TOOL_NAME,
        name=PTR_COMMIT_TOOL_NAME,
        description=(
            "Commit exactly one post-action graph decision after evaluating "
            "the supplied evidence."
        ),
        parameters_schema=decision_schema,
    )


def parse_post_tool_commit_call(
    tool_calls: list[ToolCall] | None,
) -> PostToolReasoningDecisionOutput:
    """Validate exactly one completed internal commit call."""
    if not tool_calls or len(tool_calls) != 1:
        count = len(tool_calls or [])
        raise ValueError(f"Expected exactly one PTR commit tool call, received {count}")

    tool_call = tool_calls[0]
    if tool_call.name != PTR_COMMIT_TOOL_NAME:
        raise ValueError(f"Unknown PTR commit tool: {tool_call.name}")

    try:
        arguments = json.loads(tool_call.arguments)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"PTR route arguments are invalid JSON: {exc}") from exc
    if not isinstance(arguments, dict):
        raise ValueError("PTR commit arguments must decode to an object")

    return PostToolReasoningDecisionOutput.model_validate(dict(arguments))


__all__ = [
    "PTR_COMMIT_TOOL_NAME",
    "build_post_tool_commit_tool",
    "parse_post_tool_commit_call",
]
