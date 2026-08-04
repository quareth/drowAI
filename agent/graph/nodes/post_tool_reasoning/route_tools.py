"""Provider-neutral commit contract for combined post-action reasoning.

The function here is an internal graph decision, never an executable runtime
tool. It validates one completed commit and tolerates malformed optional
candidate observations without weakening required routing fields.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from agent.graph.config.token_limits import LIMITS
from agent.providers.llm.contracts.tool_contracts import FunctionToolSpec, ToolChoice
from agent.providers.llm.core.base import ToolCall
from core.llm.structured_schemas import build_post_tool_decision_structured_output

from .core import sanitize_post_tool_decision_payload
from .models import PostToolReasoningDecisionOutput

PTR_COMMIT_TOOL_NAME = "ptr_commit"
_COMMIT_RECOVERY_SUFFIX = """

Recovery instruction: the external tool or delegated action already completed.
Do not repeat it and do not emit narrative text. Recover only the missing
internal control decision by calling `ptr_commit` exactly once from the supplied
evidence.
""".strip()


class PTRCommitError(ValueError):
    """Describe an invalid or missing internal PTR commit."""

    def __init__(self, message: str, *, code: str) -> None:
        self.code = code
        super().__init__(message)


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
    if not tool_calls:
        raise PTRCommitError("PTR commit was not completed", code="missing_commit")
    if len(tool_calls) != 1:
        raise PTRCommitError(
            f"Expected one PTR commit, received {len(tool_calls)}",
            code="multiple_commits",
        )

    tool_call = tool_calls[0]
    if tool_call.name != PTR_COMMIT_TOOL_NAME:
        raise PTRCommitError(
            f"Unexpected PTR commit tool: {tool_call.name}",
            code="wrong_commit_tool",
        )

    try:
        arguments = json.loads(tool_call.arguments)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PTRCommitError(
            f"PTR commit arguments are incomplete or invalid JSON: {exc}",
            code="invalid_commit_json",
        ) from exc
    if not isinstance(arguments, dict):
        raise PTRCommitError(
            "PTR commit arguments must decode to an object",
            code="invalid_commit_payload",
        )

    sanitized = sanitize_post_tool_decision_payload(arguments)
    try:
        return PostToolReasoningDecisionOutput.model_validate(sanitized)
    except Exception as exc:
        raise PTRCommitError(
            f"PTR commit failed schema validation: {exc}",
            code="invalid_commit_schema",
        ) from exc


async def recover_post_tool_commit(
    *,
    llm_client: Any,
    system_prompt: str,
    user_prompt: str,
    route_tools: list[FunctionToolSpec],
    reasoning_effort: str | None,
) -> tuple[PostToolReasoningDecisionOutput, Any]:
    """Run one internal commit-only recovery call over existing evidence."""
    result = await llm_client.chat_with_tools_with_usage(
        f"{system_prompt.rstrip()}\n\n{_COMMIT_RECOVERY_SUFFIX}",
        user_prompt,
        route_tools,
        # Only ptr_commit is exposed, so provider-portable ``required`` is as
        # restrictive as a provider-specific named-tool choice.
        tool_choice=ToolChoice(mode="required"),
        temperature=0.0,
        max_tokens=LIMITS.post_tool_reasoning,
        reasoning_effort=reasoning_effort,
        parallel_tool_calls=False,
    )
    outcome = getattr(result, "outcome", None)
    if getattr(outcome, "status", None) == "incomplete":
        reason = getattr(outcome, "reason", None) or "unknown"
        raise PTRCommitError(
            f"PTR commit recovery was truncated ({reason})",
            code="truncated_commit_recovery",
        )
    return parse_post_tool_commit_call(result.tool_calls), result.usage


__all__ = [
    "PTR_COMMIT_TOOL_NAME",
    "PTRCommitError",
    "build_post_tool_commit_tool",
    "parse_post_tool_commit_call",
    "recover_post_tool_commit",
]
