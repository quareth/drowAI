"""Compatibility helpers for post-tool decision streaming/non-streaming calls.

This module preserves legacy helper function names while using the current
decision-json parsing contract and streaming adapter internals.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple, TYPE_CHECKING

from core.llm import LLM_TIMEOUT_POST_TOOL_OBSERVATION_SEC, wait_for_with_timeout
from .models import PostToolReasoningOutput
from .parser import parse_reasoning_response
from .streaming.base import MAX_REASONING_TOKENS, StreamingAdapter

if TYPE_CHECKING:
    from langgraph.types import StreamWriter
    from agent.providers.llm.core.base import LLMClient


logger = logging.getLogger(__name__)


async def stream_and_parse_response(
    writer: "StreamWriter",
    llm_client: "LLMClient",
    system_prompt: str,
    user_prompt: str,
    conversation_id: str,
    turn_id: str,
    sequence: Optional[int],
    *,
    sub_turn_index: Optional[int] = None,
    reasoning_effort: Optional[str] = None,
    task_id: Optional[Any] = None,
    call_settings: Optional[Any] = None,
    suppress_observation_start: bool = False,
) -> Tuple[PostToolReasoningOutput, bool, Optional[Dict[str, Any]]]:
    """Compatibility wrapper that streams and parses post-tool decision output."""
    adapter = StreamingAdapter(
        usage_source="post_tool_reasoning",
        log_prefix="POST_TOOL_STREAM_COMPAT",
    )
    return await adapter.stream_observation(
        writer=writer,
        llm_client=llm_client,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        conversation_id=conversation_id,
        turn_id=turn_id,
        sequence=sequence,
        sub_turn_index=sub_turn_index,
        reasoning_effort=reasoning_effort,
        task_id=task_id,
        call_settings=call_settings,
        suppress_observation_start=suppress_observation_start,
    )


async def non_streaming_call(
    llm_client: "LLMClient",
    system_prompt: str,
    user_prompt: str,
    *,
    interactive: Optional[Any] = None,
    reasoning_effort: Optional[str] = None,
) -> PostToolReasoningOutput:
    """Compatibility wrapper for non-streaming post-tool parsing."""
    usage = None
    if hasattr(llm_client, "chat_with_usage"):
        llm_response = await wait_for_with_timeout(
            llm_client.chat_with_usage(
                system_prompt,
                user_prompt,
                temperature=0.3,
                max_tokens=MAX_REASONING_TOKENS,
                reasoning_effort=reasoning_effort,
            ),
            timeout_sec=LLM_TIMEOUT_POST_TOOL_OBSERVATION_SEC,
            component="POST_TOOL_OBSERVATION",
            operation="non_streaming_reasoning_call",
            logger=logger,
            task_id=getattr(getattr(interactive, "facts", None), "task_id", None),
            outcome="post_tool_decision_timeout",
        )
        response_text = llm_response.content
        usage = llm_response.usage
    else:
        response_text = await wait_for_with_timeout(
            llm_client.chat(
                system_prompt,
                user_prompt,
                temperature=0.3,
                max_tokens=MAX_REASONING_TOKENS,
                reasoning_effort=reasoning_effort,
            ),
            timeout_sec=LLM_TIMEOUT_POST_TOOL_OBSERVATION_SEC,
            component="POST_TOOL_OBSERVATION",
            operation="non_streaming_reasoning_call",
            logger=logger,
            task_id=getattr(getattr(interactive, "facts", None), "task_id", None),
            outcome="post_tool_decision_timeout",
        )

    if interactive is not None and usage:
        from ..node_utils import append_usage_to_state

        append_usage_to_state(
            interactive,
            usage,
            "post_tool_reasoning",
            request_mode="non_streaming",
        )

    return parse_reasoning_response(response_text)


__all__ = ["stream_and_parse_response", "non_streaming_call"]
