"""Observation text generation for post-tool reasoning. Handles non-streaming LLM articulation calls and fallback observation construction when articulation is unavailable or fails."""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from core.llm import LLM_TIMEOUT_POST_TOOL_OBSERVATION_SEC, wait_for_with_timeout
from ....state import InteractiveState
from ..models import PostToolReasoningDecisionOutput, PostToolReasoningError

logger = logging.getLogger(__name__)
MAX_OBSERVATION_TOKENS = 400


def _make_fallback_observation(
    interactive: InteractiveState,
    decision_output: PostToolReasoningDecisionOutput,
) -> str:
    """Build a non-empty fallback observation when a separate articulation call
    is not yet executed.
    """
    metadata = interactive.facts.safe_metadata
    synthesized = metadata.get("synthesized_output")
    if isinstance(synthesized, Mapping):
        source_observation = synthesized.get("observation_text")
        if isinstance(source_observation, str) and source_observation.strip():
            return _ensure_min_length_observation(
                source_observation.strip(),
                decision_output,
            )

        summary = synthesized.get("summary")
        if isinstance(summary, str) and summary.strip():
            return _ensure_min_length_observation(
                summary.strip(),
                decision_output,
            )

    if decision_output.tool_intent is not None:
        details = [decision_output.tool_intent.description]
        if decision_output.tool_intent.target:
            details.append(f"target={decision_output.tool_intent.target}")
        if decision_output.tool_intent.focus:
            details.append(f"focus={decision_output.tool_intent.focus}")
        tool_focus = ", ".join(details)
        return (
            f"Decision: {decision_output.next_action}. "
            f"Reasoning: {decision_output.action_reasoning}. "
            f"Tool intent: {tool_focus}"
        )

    return (
        f"Decision: {decision_output.next_action}. "
        f"Reasoning: {decision_output.action_reasoning}"
    )


def _ensure_min_length_observation(
    observation: str,
    decision_output: PostToolReasoningDecisionOutput,
) -> str:
    """Ensure fallback observations satisfy model minimum length requirements."""
    base = str(observation or "").strip()
    if len(base) >= 10:
        return base

    parts = [
        base or "Tool result observed.",
        f"Action: {decision_output.next_action}.",
        f"Reasoning: {decision_output.action_reasoning}",
    ]

    if decision_output.tool_intent:
        intent_bits = [decision_output.tool_intent.description]
        if decision_output.tool_intent.target:
            intent_bits.append(f"target={decision_output.tool_intent.target}")
        if decision_output.tool_intent.focus:
            intent_bits.append(f"focus={decision_output.tool_intent.focus}")
        parts.append("Intent: " + ", ".join(intent_bits))

    return " ".join(parts).strip()


async def _generate_observation_text(
    llm_client: Any,
    system_prompt: str,
    user_prompt: str,
    *,
    interactive: InteractiveState,
    reasoning_effort: Optional[str] = None,
) -> str:
    """Generate plain-text observation via the dedicated articulation prompt."""
    if hasattr(llm_client, "chat_with_usage"):
        response_obj = await wait_for_with_timeout(
            llm_client.chat_with_usage(
                system_prompt,
                user_prompt,
                temperature=0.3,
                max_tokens=MAX_OBSERVATION_TOKENS,
                reasoning_effort=reasoning_effort,
            ),
            timeout_sec=LLM_TIMEOUT_POST_TOOL_OBSERVATION_SEC,
            component="POST_TOOL_OBSERVATION",
            operation="observation_text_llm_call",
            logger=logger,
            task_id=getattr(getattr(interactive, "facts", None), "task_id", None),
            outcome="post_tool_observation_timeout",
        )
        content = response_obj.content

        if interactive is not None and getattr(response_obj, "usage", None):
            from ...node_utils import append_usage_to_state

            append_usage_to_state(
                interactive,
                response_obj.usage,
                "post_tool_observation",
                request_mode="non_streaming",
            )
    else:
        content = await wait_for_with_timeout(
            llm_client.chat(
                system_prompt,
                user_prompt,
                temperature=0.3,
                max_tokens=MAX_OBSERVATION_TOKENS,
                reasoning_effort=reasoning_effort,
            ),
            timeout_sec=LLM_TIMEOUT_POST_TOOL_OBSERVATION_SEC,
            component="POST_TOOL_OBSERVATION",
            operation="observation_text_llm_call",
            logger=logger,
            task_id=getattr(getattr(interactive, "facts", None), "task_id", None),
            outcome="post_tool_observation_timeout",
        )

    observation = str(content or "").strip()
    if not observation:
        raise PostToolReasoningError("Articulation LLM returned empty observation")
    return observation
