"""LangGraph node that applies simple chat results to the interactive state.

This node executes basic chat (simple LLM response) and captures token usage
from the LLM API response for accurate cost tracking."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Mapping, Optional

from langgraph.config import get_stream_writer

from agent.graph.context.builder import METADATA_CONTEXT_BUNDLE_KEY
from agent.graph.context.serialization import render_referenced_prior_turns_section
from agent.providers.llm.core.exceptions import LLMRefusalError
from core.llm import (
    LLM_STREAM_IDLE_TIMEOUT_CONVERSATION_MAIN_SEC,
    LLM_TIMEOUT_CONVERSATION_MAIN_SEC,
    iter_with_idle_timeout,
    wait_for_with_timeout,
)

from ..config.token_limits import LIMITS
from ..emission import EventEmitterFactory
from ..infrastructure.state_models import GraphRuntimeContext
from ..state import InteractiveState
from ..utils.llm_resolver import (
    ROLE_CONVERSATION_MAIN,
    get_llm_reasoning_effort,
    resolve_llm_call_settings,
    resolve_llm_client,
    supports_usage_aware_streaming,
)
from ..utils.retry_context import RetryContext, read_retry_context
from ..utils.streaming_usage import require_final_stream_usage

from .node_utils import _usage_to_dict

from core.prompts.constants import SIMPLE_CHAT_DEFAULT_SYSTEM_PROMPT

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = SIMPLE_CHAT_DEFAULT_SYSTEM_PROMPT


def _build_retry_guidance_message(retry_context: RetryContext) -> Optional[str]:
    """Render a one-line sanitized retry guidance hint, if applicable.

    Phase 2.4 of the checkpoint-retry foundation surfaces sanitized
    previous-failure context onto the run config. Graph nodes consume
    that context via :func:`read_retry_context` and surface a single
    guidance line so the LLM does not blindly repeat the prior failing
    action. Only the whitelisted previous-failure fields reach the
    prompt; raw payloads / secrets are never exposed (see
    ``_PREVIOUS_FAILURE_WHITELIST`` in ``retry_context``).
    """
    if not retry_context.is_retry:
        return None

    failure = retry_context.previous_failure or {}
    parts: list[str] = []
    error_code = failure.get("error_code")
    failure_stage = failure.get("failure_stage")
    tool_name = failure.get("tool_name")
    summary = failure.get("summary")

    descriptor_segments: list[str] = []
    if error_code:
        descriptor_segments.append(str(error_code))
    if failure_stage:
        descriptor_segments.append(str(failure_stage))
    if tool_name:
        descriptor_segments.append(f"tool={tool_name}")
    descriptor = "/".join(descriptor_segments) if descriptor_segments else "previous attempt"

    summary_text = str(summary).strip() if isinstance(summary, str) else ""
    base = f"Previous attempt failed: {descriptor}"
    if summary_text:
        parts.append(f"{base}: {summary_text}")
    else:
        parts.append(base)
    parts.append(
        "Choose a corrected or alternate path on this retry rather than repeating "
        "the same action."
    )
    return " ".join(parts)


def _build_simple_chat_messages(
    history: Iterable[Dict[str, Any]],
    current_user_turn: Optional[Dict[str, Any]],
    referenced_prior_turns: str = "",
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    retry_guidance: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Build message list for simple chat: system + history + current turn.

    ``history`` is the prior-turn message slice produced by the shared
    transcript-window authority (``ConversationContextBundle.transcript_window``),
    not raw chat-input history — see ``_messages_from_bundle`` below.

    ``current_user_turn`` is the in-flight user message read from
    ``ConversationContextBundle.current_user_turn``. Simple chat uses
    the structured OpenAI-style message surface (no text rendering),
    so the unified "one conversation stream" contract is honoured here
    by appending that same single-source turn as the final message
    rather than reading ``facts.message`` independently.

    Phase 5 cutover: long-term memory summary is no longer injected
    into the hot-path prompt (see ``no-ltm-in-hot-path``). The LTM
    write/store pipeline is unchanged; it simply does not feed the
    simple-chat prompt assembly anymore.
    """
    msgs: List[Dict[str, Any]] = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    if retry_guidance and retry_guidance.strip():
        msgs.append({"role": "system", "content": retry_guidance.strip()})
    if referenced_prior_turns.strip():
        msgs.append(
            {
                "role": "system",
                "content": (
                    referenced_prior_turns.strip()
                    + "\n\nUse this section as canonical prior conversation context. "
                    "Do not claim an exact quote unless this text supports it."
                ),
            }
        )
    for m in history:
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant", "system") and content is not None:
            msgs.append({"role": role, "content": content})
    if current_user_turn is not None:
        content = current_user_turn.get("content")
        if content is not None:
            role = current_user_turn.get("role") or "user"
            msgs.append({"role": role, "content": content})
    return msgs


def _referenced_prior_turns_from_bundle(metadata: Mapping[str, Any]) -> str:
    """Render materialized prior-turn references from the bundle, if present."""
    bundle = metadata.get(METADATA_CONTEXT_BUNDLE_KEY)
    if not isinstance(bundle, Mapping):
        return ""
    return render_referenced_prior_turns_section(
        {"prior_turn_references": bundle.get("prior_turn_references")}
    )


def _messages_from_bundle(
    metadata: Mapping[str, Any],
) -> tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Resolve prior-turn history and in-flight user turn from the bundle.

    Simple chat shares the single transcript-window authority with the
    rest of the LangGraph hot path: the recent-turn selection performed
    by ``select_recent_transcript_window`` (target 10 turns / hard
    minimum 5) is surfaced on the bundle as
    ``transcript_window["turns"]`` (verbatim prior-turn OpenAI-style
    message dicts), and the in-flight user message is surfaced on the
    bundle as ``current_user_turn``. Other roles fold both into one
    text stream via the shared serializer; simple chat consumes the
    same two fields as structured messages because the chat-completion
    API is itself message-shaped.

    Raises ``RuntimeError`` if the bundle is missing — the production
    path always populates it via
    ``LangGraphContextBuilder.build_runtime_config``; tests that exercise
    the LLM path must seed it the same way classifier/category/planner
    direct-call tests do.
    """
    bundle = metadata.get(METADATA_CONTEXT_BUNDLE_KEY)
    if not isinstance(bundle, dict):
        raise RuntimeError(
            "simple_chat: ConversationContextBundle is missing from metadata. "
            "The hot-path bundle is the single transcript-window authority; "
            "callers must populate metadata['context_bundle'] before invoking "
            "the simple_chat LLM path (LangGraphContextBuilder.build_runtime_config "
            "wires this on the production path)."
        )
    transcript_window = bundle.get("transcript_window") or {}
    turns = transcript_window.get("turns") or []
    raw_current = bundle.get("current_user_turn")
    current_user_turn = raw_current if isinstance(raw_current, dict) else None
    return list(turns), current_user_turn


def _simple_chat_call_kwargs(reasoning_effort: Optional[str]) -> Dict[str, Any]:
    """Return provider-neutral call controls, omitting absent optional values."""

    kwargs: Dict[str, Any] = {
        "temperature": 0.2,
        "max_tokens": LIMITS.final_answer,
    }
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort
    return kwargs


async def _run_non_streaming_chat(
    llm_client: Any,
    messages: List[Dict[str, Any]],
    *,
    reasoning_effort: Optional[str],
    task_id: int,
) -> tuple[str, Any]:
    """Execute one usage-tracked non-streaming call with the legacy fallback."""

    call_kwargs = _simple_chat_call_kwargs(reasoning_effort)
    if hasattr(llm_client, "chat_messages_with_usage"):
        try:
            response = await wait_for_with_timeout(
                llm_client.chat_messages_with_usage(messages, **call_kwargs),
                timeout_sec=LLM_TIMEOUT_CONVERSATION_MAIN_SEC,
                component="CONVERSATION_MAIN",
                operation="simple_chat_non_stream_llm_call",
                logger=logger,
                task_id=task_id,
                outcome="simple_chat_timeout",
            )
            return str(response.content or ""), response.usage
        except LLMRefusalError:
            raise
        except Exception as exc:
            logger.warning("[SIMPLE_CHAT] Usage-aware call failed: %s", exc)

    content = await wait_for_with_timeout(
        llm_client.chat_messages(messages, **call_kwargs),
        timeout_sec=LLM_TIMEOUT_CONVERSATION_MAIN_SEC,
        component="CONVERSATION_MAIN",
        operation="simple_chat_non_stream_llm_call_fallback",
        logger=logger,
        task_id=task_id,
        outcome="simple_chat_timeout",
    )
    return str(content or ""), None


async def run_simple_chat(
    state: Mapping[str, Any] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Execute or apply the basic chat response within the LangGraph.
    
    Args:
        state: Current node state
        context: Runtime context (optional)
        config: LangGraph config (injected by framework)
        
    Returns:
        State update dict with response
    """

    interactive = InteractiveState.from_mapping(state)
    metadata = interactive.facts.safe_metadata
    runtime_config = dict(metadata.get("simple_chat_runtime") or {})

    # Phase 5 cutover: ``metadata["long_term_memory_summary"]`` is no
    # longer read here — hot-path prompt assembly does not depend on
    # long-term memory summaries (``no-ltm-in-hot-path``).

    final_text: Optional[str] = None
    conversation_id = interactive.facts.conversation_id
    
    # Get writer from LangGraph context. Direct unit calls can run outside a
    # runnable context where get_stream_writer raises RuntimeError.
    try:
        writer = get_stream_writer()
    except RuntimeError:
        writer = None
    has_writer = writer is not None
    emitter = None

    try:
        if "result" in runtime_config:
            # Preset result path (no streaming needed)
            result = runtime_config["result"] or {}
            final_text = str(result.get("content") or "").strip()
            conversation_id = result.get("conversation_id") or conversation_id
            interactive.trace.reasoning.append("Simple chat result applied from preset runtime payload.")
        else:
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
            history, current_user_turn = _messages_from_bundle(metadata)
            # Phase 2.4 retry-context consumer: when this run is a checkpoint
            # retry the run config carries sanitized previous-failure context
            # under ``configurable``. Surface a single guidance line so the
            # LLM does not blindly repeat the failing action.
            retry_context = read_retry_context(config)
            retry_guidance = _build_retry_guidance_message(retry_context)
            messages = _build_simple_chat_messages(
                history,
                current_user_turn,
                _referenced_prior_turns_from_bundle(metadata),
                retry_guidance=retry_guidance,
            )

            use_streaming = has_writer and supports_usage_aware_streaming(
                llm_client,
                call_settings,
            )
            if use_streaming:
                # === STREAMING PATH ===
                logger.info("[SIMPLE_CHAT] Using streaming path")
                emitter = EventEmitterFactory.create(writer, interactive, config, context)
                emitter.emit_message_start()

                chunks: List[str] = []
                stream_response = await wait_for_with_timeout(
                    llm_client.stream_chat_messages_with_usage(
                        messages,
                        **_simple_chat_call_kwargs(reasoning_effort),
                    ),
                    timeout_sec=LLM_TIMEOUT_CONVERSATION_MAIN_SEC,
                    component="CONVERSATION_MAIN",
                    operation="simple_chat_stream_setup",
                    logger=logger,
                    task_id=interactive.facts.task_id,
                    outcome="simple_chat_timeout",
                )
                async for chunk in iter_with_idle_timeout(
                    stream_response.content_iterator,
                    timeout_sec=LLM_STREAM_IDLE_TIMEOUT_CONVERSATION_MAIN_SEC,
                    component="CONVERSATION_MAIN",
                    operation="simple_chat_stream",
                    logger=logger,
                    task_id=interactive.facts.task_id,
                    outcome="stream_idle_timeout",
                ):
                    text = str(chunk) if chunk else ""
                    if text:
                        emitter.emit_message_delta(text)
                        chunks.append(text)
                last_usage = require_final_stream_usage(
                    stream_response.get_final_usage(),
                    call_settings,
                    operation="simple_chat_stream",
                    task_id=interactive.facts.task_id,
                )

                emitter.emit_section_end("final_answer")
                final_text = "".join(chunks)

                usage_dict = _usage_to_dict(
                    last_usage,
                    "simple_chat",
                    request_mode="streaming",
                )
                if usage_dict:
                    interactive.trace.usage_records.append(usage_dict)
                    logger.debug(
                        "[SIMPLE_CHAT] Captured streaming usage: %s tokens",
                        usage_dict.get("total_tokens", 0),
                    )

                interactive.trace.reasoning.append(
                    "Simple chat executed via LangGraph node (streaming)."
                )
            else:
                # === NON-STREAMING PATH (fallback) ===
                logger.info("[SIMPLE_CHAT] Using non-streaming fallback")
                if has_writer:
                    emitter = EventEmitterFactory.create(
                        writer,
                        interactive,
                        config,
                        context,
                    )
                    emitter.emit_message_start()
                content, last_usage = await _run_non_streaming_chat(
                    llm_client,
                    messages,
                    reasoning_effort=reasoning_effort,
                    task_id=interactive.facts.task_id,
                )
                final_text = content.strip()
                if emitter is not None:
                    if final_text:
                        emitter.emit_message_delta(final_text)
                    emitter.emit_section_end("final_answer")

                usage_dict = _usage_to_dict(
                    last_usage,
                    "simple_chat",
                    request_mode="non_streaming",
                )
                if usage_dict:
                    interactive.trace.usage_records.append(usage_dict)
                    logger.debug(
                        "[SIMPLE_CHAT] Captured non-streaming usage: %s tokens",
                        usage_dict.get("total_tokens", 0),
                    )

                interactive.trace.reasoning.append(
                    "Simple chat executed via LangGraph node (non-streaming)."
                )
                
    except LLMRefusalError:
        raise
    except Exception as exc:
        logger.error(f"[SIMPLE_CHAT] Error: {exc}", exc_info=True)
        
        # Emit error event if writer available
        if has_writer:
            err_emitter = (
                emitter
                if emitter is not None
                else EventEmitterFactory.create(writer, interactive, config, context)
            )
            err_emitter.emit_stream_error(
                error=str(exc),
                recoverable=False,
                details={"node": "simple_chat", "task_id": interactive.facts.task_id},
            )
        
        interactive.trace.reasoning.append(f"Simple chat handler failed: {exc}")
        interactive.trace.final_error = str(exc)
        final_text = final_text or ""
    finally:
        metadata.pop("simple_chat_runtime", None)

    if final_text is not None:
        interactive.trace.final_text = final_text
    if conversation_id:
        interactive.facts.conversation_id = conversation_id

    return interactive.as_graph_update()


__all__ = ["run_simple_chat"]
