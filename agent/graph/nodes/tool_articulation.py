"""Tool articulation node for natural language explanation of tool execution intent.

The articulation prompt is not a transcript consumer. The node reads
the classifier-derived ``working_memory.intent_brief`` and passes it to
``build_tool_articulation_prompt`` together with the selected tool and
its resolved parameters.

There is no bundle-projection helper on this module: articulation
sits outside the two full-history seams documented in
``docs/plans/intent_interpretation_wiring.md`` (intent classifier and
deep-reasoning finalizer) and cannot reach transcript text on the hot
path even by accident.

``runtime_state`` remains a kwarg on ``build_tool_articulation_prompt``
for callers that want to supply a compact non-transcript runtime-state
slice, but this node does not source one from the bundle — the brief
already carries resolved intent, next operational goal, success
condition, constraints, and target metadata.

Captures token usage from LLM calls for cost tracking.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional

from langgraph.config import get_stream_writer

from ..infrastructure.state_models import GraphRuntimeContext
from ..state import InteractiveState
from ..utils.llm_resolver import (
    ROLE_CONVERSATION_MAIN,
    get_llm_reasoning_effort,
    resolve_llm_call_settings,
    resolve_llm_client,
)
from ..utils.streaming_usage import require_final_stream_usage, require_usage_aware_streaming
from ..emission.factory import EventEmitterFactory
from ..emission.reasoning_section import reasoning_section
from agent.providers.llm.core.exceptions import LLMConfigurationError
from agent.tool_runtime.batch.plan_view import primary_tool_call_from_metadata
from agent.graph.config.token_limits import LIMITS
from core.llm import (
    LLM_STREAM_IDLE_TIMEOUT_CONVERSATION_MAIN_SEC,
    LLM_TIMEOUT_CONVERSATION_MAIN_SEC,
    iter_with_idle_timeout,
    wait_for_with_timeout,
)
from core.prompts.constants import (
    TOOL_ARTICULATION_SYSTEM_PROMPT,
    build_tool_articulation_prompt,
)
from .node_utils import _usage_to_dict

if TYPE_CHECKING:
    from langgraph.types import StreamWriter

logger = logging.getLogger(__name__)


def _resolve_tool_params_for_articulation(interactive: InteractiveState, selected_tool: str) -> Dict[str, Any]:
    """Resolve the most faithful parameter view for user-facing articulation.

    Reads canonical serialized ToolBatch call parameters only.
    """
    metadata = interactive.facts.safe_metadata
    primary_call = primary_tool_call_from_metadata(metadata)
    if primary_call is not None and primary_call.tool_id == selected_tool:
        return dict(primary_call.parameters)

    return {}


async def articulate_tool_intent(
    state: Mapping[str, Any] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
    config: Optional[Dict[str, Any]] = None,
    writer: Optional["StreamWriter"] = None,
) -> Dict[str, Any]:
    """Generate natural language articulation of tool execution intent.

    This node implements two-step articulation pattern:
    1. Decision already made (tool selected, params determined)
    2. Articulate that decision in 1-2 sentences for user-facing display

    Streams reasoning tokens in real-time to "Thinking" container.

    Args:
        state: Current node state
        context: Runtime context (optional)
        config: LangGraph config (injected by framework)

    Returns:
        State update dict with articulation text
    """
    interactive = InteractiveState.from_mapping(state)
    metadata = interactive.facts.ensure_metadata()

    # Extract tool selection from the canonical batch manifest.
    primary_call = primary_tool_call_from_metadata(metadata)
    selected_tool = primary_call.tool_id if primary_call is not None else "unknown tool"
    tool_params = _resolve_tool_params_for_articulation(interactive, selected_tool)

    # Prefer wrapped-writer injection; fall back to LangGraph context lookup.
    stream_writer = writer if writer is not None else get_stream_writer()
    has_writer = stream_writer is not None

    logger.info(
        f"[ARTICULATION] Streaming: {'enabled' if has_writer else 'disabled'}, "
        f"selected_tool: {selected_tool}"
    )

    # Articulation is brief-only: the classifier-derived
    # ``working_memory.intent_brief`` is the sole source of user-intent context
    # on this hot path. No transcript, no bundle projection, no
    # runtime-state carryover from the bundle.
    intent_brief: Mapping[str, Any] = {}
    working_memory = metadata.get("working_memory")
    if isinstance(working_memory, Mapping):
        raw_intent_brief = working_memory.get("intent_brief")
        if isinstance(raw_intent_brief, Mapping):
            intent_brief = raw_intent_brief
    articulation_prompt = build_tool_articulation_prompt(
        selected_tool=selected_tool,
        tool_params=tool_params,
        intent_brief=intent_brief,
    )

    articulation_text = ""
    captured_usage: Optional[Dict[str, Any]] = None

    try:
        # Get LLMClient via resolver
        try:
            llm_client = resolve_llm_client(
                metadata,
                context,
                config=config,
                role=ROLE_CONVERSATION_MAIN,
            )
            call_settings = resolve_llm_call_settings(
                metadata,
                context,
                role=ROLE_CONVERSATION_MAIN,
            )
            reasoning_effort = get_llm_reasoning_effort(llm_client, call_settings)
        except LLMConfigurationError:
            # Fallback: use hardcoded articulation when no API key
            articulation_text = f"To meet your request, I will execute {selected_tool}."
            interactive.trace.reasoning.append(f"[ARTICULATION] {articulation_text}")
            return interactive.as_graph_update()

        if has_writer:
            # === STREAMING PATH ===
            logger.info("[ARTICULATION] Using streaming path")
            emitter = EventEmitterFactory.create(stream_writer, interactive, config, context)
            stream_failure: Optional[BaseException] = None
            emitter.emit_reasoning_start("tool_intent")
            try:
                # Build messages for streaming API
                messages = [
                    {"role": "system", "content": TOOL_ARTICULATION_SYSTEM_PROMPT},
                    {"role": "user", "content": articulation_prompt},
                ]

                # Stream articulation token-by-token with usage capture (Phase 3)
                chunks = []
                chunk_count = 0

                require_usage_aware_streaming(
                    llm_client,
                    call_settings,
                    operation="tool_articulation_stream",
                    task_id=interactive.facts.task_id,
                )
                stream_response = await wait_for_with_timeout(
                    llm_client.stream_chat_messages_with_usage(
                        messages,
                        temperature=0.3,
                        reasoning_effort=reasoning_effort,
                    ),
                    timeout_sec=LLM_TIMEOUT_CONVERSATION_MAIN_SEC,
                    component="CONVERSATION_MAIN",
                    operation="tool_articulation_stream_setup",
                    logger=logger,
                    task_id=interactive.facts.task_id,
                    outcome="tool_articulation_timeout",
                )
                async for chunk in iter_with_idle_timeout(
                    stream_response.content_iterator,
                    timeout_sec=LLM_STREAM_IDLE_TIMEOUT_CONVERSATION_MAIN_SEC,
                    component="CONVERSATION_MAIN",
                    operation="tool_articulation_stream",
                    logger=logger,
                    task_id=interactive.facts.task_id,
                    outcome="stream_idle_timeout",
                ):
                    chunk_count += 1
                    if chunk and isinstance(chunk, str):
                        emitter.emit_reasoning_delta(chunk)
                        chunks.append(chunk)

                # Capture usage after stream completes
                usage = require_final_stream_usage(
                    stream_response.get_final_usage(),
                    call_settings,
                    operation="tool_articulation_stream",
                    task_id=interactive.facts.task_id,
                )
                captured_usage = _usage_to_dict(
                    usage,
                    "tool_articulation",
                    request_mode="streaming",
                )
                if captured_usage:
                    logger.debug(
                        f"[ARTICULATION] Captured usage: "
                        f"{captured_usage.get('total_tokens', 0)} tokens"
                    )

                articulation_text = "".join(chunks)

                # Log streaming results for debugging - NO FALLBACK
                logger.info(
                    f"[ARTICULATION] Streaming completed: "
                    f"chunk_count={chunk_count}, "
                    f"valid_chunks={len(chunks)}, "
                    f"total_chars={len(articulation_text)}"
                )

                if not articulation_text.strip():
                    # FAIL LOUDLY - no fallback
                    logger.error(
                        f"[ARTICULATION] STREAMING YIELDED NO TEXT! "
                        f"chunk_count={chunk_count}, model={llm_client.model}. "
                        f"This indicates a problem with the LLM provider's streaming implementation."
                    )
            except BaseException as exc:
                stream_failure = exc
                raise
            finally:
                try:
                    emitter.emit_reasoning_section_end("tool_intent")
                except Exception:
                    if stream_failure is None:
                        raise
                    logger.exception(
                        "[ARTICULATION] Failed to emit tool_intent section end after stream failure"
                    )

            emitter.emit_reasoning_snapshot(articulation_text, step="tool_intent")
            interactive.trace.reasoning.append(f"[ARTICULATION] {articulation_text} (streaming)")
        else:
            # === NON-STREAMING PATH (fallback) with usage tracking (Phase 7) ===
            logger.info("[ARTICULATION] Using non-streaming fallback")
            async with reasoning_section(
                stream_writer,
                state=interactive,
                step="tool_intent",
                label="Preparing tool intent summary.",
                config=config,
                context=context,
            ):
                llm_response = await wait_for_with_timeout(
                    llm_client.chat_with_usage(
                        "You are explaining tool execution intent.",
                        articulation_prompt,
                        max_tokens=LIMITS.tool_articulation,
                        temperature=0.3,
                        reasoning_effort=reasoning_effort,
                    ),
                    timeout_sec=LLM_TIMEOUT_CONVERSATION_MAIN_SEC,
                    component="CONVERSATION_MAIN",
                    operation="tool_articulation_non_stream_llm_call",
                    logger=logger,
                    task_id=interactive.facts.task_id,
                    outcome="tool_articulation_timeout",
                )
            articulation_text = llm_response.content.strip()
            # Capture usage from non-streaming fallback
            if llm_response.usage:
                captured_usage = _usage_to_dict(
                    llm_response.usage,
                    "tool_articulation",
                    request_mode="non_streaming",
                )
            interactive.trace.reasoning.append(f"[ARTICULATION] {articulation_text}")

    except Exception as exc:
        logger.error(f"[ARTICULATION] Error: {exc}", exc_info=True)
        raise

    # Store articulation in metadata for later use
    metadata["tool_articulation"] = articulation_text
    interactive.facts.metadata = metadata

    # Store usage in trace (Phase 3)
    if captured_usage:
        interactive.trace.usage_records.append(captured_usage)

    return interactive.as_graph_update()


__all__ = ["articulate_tool_intent"]
