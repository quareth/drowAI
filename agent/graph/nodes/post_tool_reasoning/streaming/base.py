"""Shared streaming adapter implementation for post-tool observation events."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from agent.providers.llm.core.base import LLMClient
    from agent.providers.llm.contracts.tool_contracts import FunctionToolSpec
    from langgraph.types import StreamWriter
    from ...state import InteractiveState

from agent.graph.config.token_limits import LIMITS
from agent.graph.emission.factory import EventEmitterFactory
from agent.graph.utils.event_identity import resolve_identity_from_config, resolve_stream_identifiers
from agent.graph.utils.streaming_usage import (
    require_final_stream_usage,
    require_usage_aware_streaming,
)
from core.llm import (
    LLM_STREAM_IDLE_TIMEOUT_POST_TOOL_OBSERVATION_SEC,
    LLM_TIMEOUT_POST_TOOL_ARTICULATOR_SEC,
    iter_with_idle_timeout,
    wait_for_with_timeout,
)
from ...node_utils import _usage_to_dict
from agent.providers.llm.contracts.tool_contracts import ToolChoice
from ..models import (
    PostToolReasoningDecisionOutput,
    PostToolReasoningError,
)
from ..route_tools import (
    PTRCommitError,
    parse_post_tool_commit_call,
    recover_post_tool_commit,
)

logger = logging.getLogger(__name__)

MAX_REASONING_TOKENS = LIMITS.post_tool_reasoning
STREAMING_STEP_NAME = "post_tool_reasoning"


def _decision_observation_fallback(
    decision: PostToolReasoningDecisionOutput,
) -> str:
    """Return complete LLM-authored commit text suitable for the trace model."""
    observation = decision.action_reasoning.strip()
    if len(observation) >= 10:
        return observation
    return f"{observation} Next action: {decision.next_action}.".strip()


class StreamingAdapter:
    """Shared observation streaming implementation.

    Capability-specific adapters should only provide usage/log labels via the
    constructor while reusing the same streaming behavior.
    """

    def __init__(self, usage_source: str, log_prefix: str) -> None:
        self._usage_source = usage_source
        self._log_prefix = log_prefix

    async def stream_observation_with_route(
        self,
        writer: "StreamWriter",
        llm_client: "LLMClient",
        system_prompt: str,
        user_prompt: str,
        route_tools: list["FunctionToolSpec"],
        conversation_id: str,
        turn_id: str,
        sequence: Optional[int],
        sub_turn_index: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        task_id: Optional[Any] = None,
        call_settings: Any = None,
        *,
        suppress_observation_start: bool = False,
    ) -> tuple[
        str,
        PostToolReasoningDecisionOutput,
        bool,
        Optional[Dict[str, Any]],
        Optional[Dict[str, Any]],
    ]:
        """Stream optional narrative and obtain one durable PTR commit.

        If the provider omits or truncates only the internal commit, one
        non-streaming commit-only recovery call reuses the existing evidence.
        No external runtime tool is available to or invoked by this method.
        """
        all_chunks: List[str] = []
        streamed_observation = False
        primary_usage: Optional[Dict[str, Any]] = None
        recovery_usage: Optional[Dict[str, Any]] = None
        emitter = EventEmitterFactory.create_from_identity(
            writer,
            conversation_id,
            turn_id,
            turn_sequence=sequence,
            sub_turn_index=sub_turn_index,
        )
        if not suppress_observation_start:
            emitter.emit_observation_start(STREAMING_STEP_NAME)

        try:
            require_usage_aware_streaming(
                llm_client,
                call_settings,
                operation="post_tool_route_stream",
                task_id=task_id,
            )
            stream_response = await wait_for_with_timeout(
                llm_client.stream_chat_with_tools_with_usage(
                    system_prompt,
                    user_prompt,
                    route_tools,
                    tool_choice=ToolChoice(mode="required"),
                    temperature=0.3,
                    max_tokens=MAX_REASONING_TOKENS,
                    reasoning_effort=reasoning_effort,
                    parallel_tool_calls=False,
                ),
                timeout_sec=LLM_TIMEOUT_POST_TOOL_ARTICULATOR_SEC,
                component="POST_TOOL_OBSERVATION",
                operation="route_stream_setup",
                logger=logger,
                task_id=task_id,
                outcome="post_tool_route_timeout",
            )
            async for chunk in _iter_stream_chunks_with_setup_timeout(
                stream_response.content_iterator,
                setup_timeout_sec=LLM_TIMEOUT_POST_TOOL_ARTICULATOR_SEC,
                setup_component="POST_TOOL_OBSERVATION",
                setup_operation="route_stream_first_chunk",
                setup_outcome="post_tool_route_timeout",
                timeout_sec=LLM_STREAM_IDLE_TIMEOUT_POST_TOOL_OBSERVATION_SEC,
                component="POST_TOOL_OBSERVATION",
                operation="route_stream",
                logger=logger,
                task_id=task_id,
                outcome="stream_idle_timeout",
            ):
                if not chunk:
                    continue
                all_chunks.append(chunk)
                emitter.emit_observation_delta(chunk)
                streamed_observation = True

            usage = require_final_stream_usage(
                stream_response.get_final_usage(),
                call_settings,
                operation="post_tool_route_stream",
                task_id=task_id,
            )
            primary_usage = _usage_to_dict(
                usage,
                self._usage_source,
                request_mode="streaming",
            )

            final_calls = stream_response.get_final_tool_calls()
            get_outcome = getattr(stream_response, "get_final_outcome", None)
            outcome = get_outcome() if callable(get_outcome) else None
            try:
                decision = parse_post_tool_commit_call(final_calls)
            except PTRCommitError as exc:
                incomplete = getattr(outcome, "status", None) == "incomplete"
                recoverable = incomplete or exc.code in {
                    "missing_commit",
                    "invalid_commit_json",
                }
                if not recoverable:
                    raise
                reason = getattr(outcome, "reason", None) if incomplete else exc.code
                logger.warning(
                    "[%s] Recovering PTR commit only (reason=%s); completed "
                    "external work will not be rerun",
                    self._log_prefix,
                    reason or "missing_commit",
                )
                decision, recovered_usage = await recover_post_tool_commit(
                    llm_client=llm_client,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    route_tools=route_tools,
                    reasoning_effort=reasoning_effort,
                )
                if recovered_usage is not None:
                    recovery_usage = _usage_to_dict(
                        recovered_usage,
                        self._usage_source,
                        request_mode="non_streaming",
                    )

            observation = "".join(all_chunks).strip()
            if not observation:
                observation = _decision_observation_fallback(decision)
                emitter.emit_observation_delta(observation)
                streamed_observation = True
        except Exception as exc:
            error_message = _format_stream_error(exc)
            logger.error(
                "[%s] Route streaming failed: %s",
                self._log_prefix,
                error_message,
                exc_info=True,
            )
            emitter.emit_stream_error(
                error=f"Post-action reasoning failed: {error_message}",
                recoverable=False,
            )
            emitter.emit_observation_section_end(STREAMING_STEP_NAME)
            if isinstance(exc, PostToolReasoningError):
                raise
            raise PostToolReasoningError(
                f"LLM route streaming failed: {error_message}"
            ) from exc

        emitter.emit_observation_snapshot(observation, step=STREAMING_STEP_NAME)
        emitter.emit_observation_section_end(STREAMING_STEP_NAME)
        return (
            observation,
            decision,
            streamed_observation,
            primary_usage,
            recovery_usage,
        )

    async def stream_observation_text(
        self,
        writer: "StreamWriter",
        llm_client: "LLMClient",
        system_prompt: str,
        user_prompt: str,
        conversation_id: str,
        turn_id: str,
        sequence: Optional[int],
        sub_turn_index: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        task_id: Optional[Any] = None,
        call_settings: Any = None,
        *,
        suppress_observation_start: bool = False,
    ) -> Tuple[str, bool, Optional[Dict[str, Any]]]:
        """Stream plain-text observation text and return final observation content."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        all_chunks: List[str] = []
        streamed_observation = False
        captured_usage: Optional[Dict[str, Any]] = None

        emitter = EventEmitterFactory.create_from_identity(
            writer,
            conversation_id,
            turn_id,
            turn_sequence=sequence,
            sub_turn_index=sub_turn_index,
        )
        if not suppress_observation_start:
            emitter.emit_observation_start(STREAMING_STEP_NAME)

        try:
            require_usage_aware_streaming(
                llm_client,
                call_settings,
                operation="post_tool_observation_stream",
                task_id=task_id,
            )
            stream_response = await wait_for_with_timeout(
                llm_client.stream_chat_messages_with_usage(
                    messages,
                    temperature=0.3,
                    max_tokens=MAX_REASONING_TOKENS,
                    reasoning_effort=reasoning_effort,
                ),
                timeout_sec=LLM_TIMEOUT_POST_TOOL_ARTICULATOR_SEC,
                component="POST_TOOL_ARTICULATOR",
                operation="observation_stream_setup",
                logger=logger,
                task_id=task_id,
                outcome="post_tool_articulation_timeout",
            )
            async for chunk in _iter_stream_chunks_with_setup_timeout(
                stream_response.content_iterator,
                setup_timeout_sec=LLM_TIMEOUT_POST_TOOL_ARTICULATOR_SEC,
                setup_component="POST_TOOL_ARTICULATOR",
                setup_operation="observation_stream_first_chunk",
                setup_outcome="post_tool_articulation_timeout",
                timeout_sec=LLM_STREAM_IDLE_TIMEOUT_POST_TOOL_OBSERVATION_SEC,
                component="POST_TOOL_OBSERVATION",
                operation="observation_stream",
                logger=logger,
                task_id=task_id,
                outcome="stream_idle_timeout",
            ):
                if not chunk:
                    continue
                all_chunks.append(chunk)
                emitter.emit_observation_delta(chunk)
                streamed_observation = True

            usage = require_final_stream_usage(
                stream_response.get_final_usage(),
                call_settings,
                operation="post_tool_observation_stream",
                task_id=task_id,
            )
            captured_usage = _usage_to_dict(
                usage,
                self._usage_source,
                request_mode="streaming",
            )
            if captured_usage:
                logger.debug(
                    "[%s] Captured usage: %s tokens",
                    self._log_prefix,
                    captured_usage.get("total_tokens", 0),
                )

        except Exception as exc:
            error_message = _format_stream_error(exc)
            logger.error("[%s] Streaming failed: %s", self._log_prefix, error_message, exc_info=True)
            emitter.emit_observation_section_end(STREAMING_STEP_NAME)
            raise PostToolReasoningError(f"LLM streaming failed: {error_message}") from exc

        if not all_chunks:
            emitter.emit_observation_section_end(STREAMING_STEP_NAME)
            raise PostToolReasoningError("LLM returned empty response during streaming")

        full_response = "".join(all_chunks).strip()
        emitter.emit_observation_snapshot(full_response, step=STREAMING_STEP_NAME)
        emitter.emit_observation_section_end(STREAMING_STEP_NAME)
        return full_response, streamed_observation, captured_usage

    def get_stream_identifiers(
        self,
        interactive: "InteractiveState",
        config: Optional[Any],
    ) -> tuple:
        """Get canonical identifiers from config with fallback for standalone runs."""
        cfg_conv, cfg_turn, _ = resolve_identity_from_config(config)
        if cfg_turn:
            return (cfg_conv, cfg_turn)
        return resolve_stream_identifiers(interactive, config)


async def _iter_stream_chunks_with_setup_timeout(
    async_iterable: AsyncIterator[Any],
    *,
    setup_timeout_sec: float,
    setup_component: str,
    setup_operation: str,
    setup_outcome: str,
    timeout_sec: float,
    component: str,
    operation: str,
    logger: Any,
    task_id: Optional[Any] = None,
    outcome: str = "stream_idle_timeout",
) -> AsyncIterator[Any]:
    """Yield chunks, timing the first chunk as stream setup and later chunks as idle."""
    iterator = async_iterable.__aiter__()
    try:
        first_item = await wait_for_with_timeout(
            iterator.__anext__(),
            timeout_sec=setup_timeout_sec,
            component=setup_component,
            operation=setup_operation,
            logger=logger,
            task_id=task_id,
            outcome=setup_outcome,
        )
    except StopAsyncIteration:
        return
    yield first_item

    async for item in iter_with_idle_timeout(
        iterator,
        timeout_sec=timeout_sec,
        component=component,
        operation=operation,
        logger=logger,
        task_id=task_id,
        outcome=outcome,
    ):
        yield item


def _format_stream_error(exc: BaseException) -> str:
    """Return a non-empty streaming error message for logs and user-safe errors."""
    if isinstance(exc, asyncio.TimeoutError):
        return "timed out waiting for LLM stream data"
    message = str(exc).strip()
    if message:
        return message
    return exc.__class__.__name__


__all__ = ["StreamingAdapter"]
