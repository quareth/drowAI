"""Unified finalizer node for both simple-tool and deep reasoning graphs.

This is the canonical "stream the final answer" node. It replaces the two
prior implementations:

- ``finalize_tool_results`` (simple-tool path)
- ``finalize_deep_reasoning`` (deep reasoning path)

The unified flow:

1. Read capability from ``facts.capability``.
2. Collect a capability-conditional context bundle (PTR analyst surfaces +
   referenced prior turns for simple-tool; the bundle-projected transcript,
   plan, todo list, iteration records, observations, executed tools, and
   targets for deep reasoning).
3. Build the operator-voice 4-part prompt via
   ``core.prompts.builders.finalize.build_finalize_prompts``.
4. Stream the LLM response with capability-branched emitter selection
   (``create_simple`` for simple-tool, ``create_turn_level`` for deep
   reasoning) so existing UI behavior is preserved.
5. Persist ``trace.final_text`` and the usage record.
6. Clear simple-tool retry metadata.

Memory extraction is intentionally NOT triggered here. Both graphs route
through ``finalize_turn`` (the cheap suffixer), which owns the single
canonical ``enqueue_memory_extraction`` call.

Full-history seam (runner_control exception)
-------------------------------------------
For the deep reasoning capability this node is one of the two intentional
full-transcript consumers per ``docs/plans/intent_interpretation_wiring.md``
runner_control cutover. The other is the intent classifier. Recent-turn continuity for the
final answer is sourced from the shared hot-path
``ConversationContextBundle`` via ``project_for_articulation`` so the final
answer stays aligned with the user's full conversation arc.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Dict, List, Mapping, Optional, Tuple

from langgraph.config import get_stream_writer

from agent.graph.config.token_limits import LIMITS
from agent.graph.context.builder import METADATA_CONTEXT_BUNDLE_KEY
from agent.graph.context.projections import project_for_articulation
from agent.graph.context.serialization import (
    SECTION_RECENT_TRANSCRIPT,
    SECTION_REFERENCED_PRIOR_TURNS,
    SECTION_RUNTIME_STATE,
    render_referenced_prior_turns_section,
    serialize_projection_to_section_map,
)
from agent.graph.emission import EventEmitterFactory
from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.memory.findings import build_relevant_findings_for_prompt
from agent.graph.state import FactsState, InteractiveState
from agent.graph.utils.event_identity import resolve_turn_sequence
from agent.graph.utils.llm_resolver import (
    ROLE_CONVERSATION_MAIN,
    get_llm_reasoning_effort,
    resolve_llm_call_settings,
    resolve_llm_client,
)
from agent.graph.utils.streaming_usage import (
    require_final_stream_usage,
    require_usage_aware_streaming,
)
from core.llm import (
    LLM_STREAM_IDLE_TIMEOUT_CONVERSATION_MAIN_SEC,
    LLM_TIMEOUT_CONVERSATION_MAIN_SEC,
    iter_with_idle_timeout,
    wait_for_with_timeout,
)
from core.prompts.builders.finalize import build_finalize_prompts

from ._finalize_helpers import resolve_simple_tool_retry_context
from .node_utils import _usage_to_dict, normalize_stream_chunk

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------


def _is_deep_reasoning(facts: FactsState) -> bool:
    return str(facts.capability or "").strip().lower() == "deep_reasoning"


# ---------------------------------------------------------------------------
# Context collection helpers
# ---------------------------------------------------------------------------


def _collect_simple_tool_context(
    interactive: InteractiveState,
    context: Optional[GraphRuntimeContext],
) -> Dict[str, Any]:
    """Gather simple-tool prompt inputs (mutates metadata for retry caching)."""
    facts = interactive.facts
    metadata = facts.ensure_metadata()
    synthesized = dict(metadata.get("synthesized_output") or {})
    last_result = dict(metadata.get("last_tool_result") or {})

    retry_context = resolve_simple_tool_retry_context(metadata, synthesized)

    requested_output_format = None
    requested_format_value = metadata.get("requested_output_format")
    if isinstance(requested_format_value, str):
        requested_output_format = requested_format_value.strip().lower() or None

    referenced_prior_turns = _referenced_prior_turns_from_bundle(metadata)

    return {
        "user_message": facts.message or "",
        "synthesized": synthesized,
        "last_result": last_result,
        "retry_attempts": retry_context["retry_attempts"],
        "aggregated_findings": retry_context["aggregated_findings"],
        "requested_output_format": requested_output_format,
        "referenced_prior_turns": referenced_prior_turns,
        "metadata": metadata,
        "relevant_findings": build_relevant_findings_for_prompt(interactive),
        "current_goal": str(facts.current_goal or "").strip(),
        "turn_sequence": resolve_turn_sequence(context, metadata),
    }


def _collect_deep_reasoning_context(
    interactive: InteractiveState,
) -> Dict[str, Any]:
    """Gather deep reasoning prompt inputs."""
    facts = interactive.facts
    trace = interactive.trace
    metadata = facts.safe_metadata

    transcript_text, runtime_state_text, referenced_prior_turns = (
        _resolve_finalizer_bundle_sections(metadata)
    )

    return {
        "user_message": facts.message or "",
        "referenced_prior_turns": referenced_prior_turns,
        "transcript_text": transcript_text,
        "runtime_state_text": runtime_state_text,
        "targets": _extract_targets(facts),
        "plan": list(facts.plan or []),
        "todo_list": list(facts.safe_todo_list),
        "dr_iteration_records": dict(metadata.get("dr_iteration_records") or {}),
        "observations": list(trace.observations or []),
        "executed_tools": list(trace.executed_tools or []),
    }


def _referenced_prior_turns_from_bundle(metadata: Mapping[str, Any]) -> str:
    """Render canonical referenced prior turns from the context bundle."""
    bundle = metadata.get(METADATA_CONTEXT_BUNDLE_KEY)
    if not isinstance(bundle, Mapping):
        return ""
    return render_referenced_prior_turns_section(
        {"prior_turn_references": bundle.get("prior_turn_references")}
    )


def _resolve_finalizer_bundle_sections(
    metadata: Mapping[str, Any],
) -> Tuple[str, str, str]:
    """Project the conversation bundle into transcript / runtime / refs slices.

    Reads ``metadata[METADATA_CONTEXT_BUNDLE_KEY]`` and projects it for
    the articulation role (same projection the tool-articulation node
    consumes, for cross-role consistency). Returns ``("", "", "")`` when
    the bundle is absent — the finalizer then falls back to its
    structured trace sections without continuity context rather than
    aborting the final answer.
    """
    bundle = metadata.get(METADATA_CONTEXT_BUNDLE_KEY)
    if not isinstance(bundle, Mapping):
        return "", "", ""
    projection = project_for_articulation(dict(bundle))
    section_map = serialize_projection_to_section_map(projection)
    transcript_text = section_map.get(SECTION_RECENT_TRANSCRIPT, "") or ""
    runtime_state_text = section_map.get(SECTION_RUNTIME_STATE, "") or ""
    referenced_prior_turns_text = section_map.get(SECTION_REFERENCED_PRIOR_TURNS, "") or ""
    return transcript_text, runtime_state_text, referenced_prior_turns_text


def _extract_targets(facts: FactsState) -> List[str]:
    """Return engagement target list (intent hints + metadata.targets)."""
    targets: List[str] = []
    intent_targets = facts.intent_hints.get("targets") if facts.intent_hints else None
    if isinstance(intent_targets, list):
        targets.extend(str(target) for target in intent_targets if target)
    meta_targets = facts.safe_metadata.get("targets")
    if isinstance(meta_targets, list):
        targets.extend(str(target) for target in meta_targets if target)
    return list(dict.fromkeys(targets))


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def _build_prompts(interactive: InteractiveState) -> Tuple[str, str]:
    """Render the unified finalizer prompts for ``InteractiveState``.

    Capability-aware: routes to the deep-reasoning context when
    ``facts.capability == "deep_reasoning"`` and to the simple-tool
    context otherwise. This is the canonical builder entrypoint relied
    on by the runner_control finalizer guardrail tests; it is intentionally
    side-effect free and does not consume a runtime context (the
    runtime path uses :func:`_build_messages` instead).
    """
    capability = "deep_reasoning" if _is_deep_reasoning(interactive.facts) else "simple_tool_execution"
    system_prompt, user_prompt, _ = _build_messages(
        interactive=interactive,
        context=None,
        capability=capability,
    )
    return system_prompt, user_prompt


def _build_messages(
    *,
    interactive: InteractiveState,
    context: Optional[GraphRuntimeContext],
    capability: str,
) -> Tuple[str, str, Dict[str, Any]]:
    """Build messages for the unified finalizer call.

    Returns ``(system_prompt, user_prompt, collected_context)``. The
    collected context is exposed so the caller can persist any metadata
    side-effects (e.g. ``aggregated_findings`` on simple-tool path).
    """
    is_dr = capability == "deep_reasoning"

    if is_dr:
        ctx = _collect_deep_reasoning_context(interactive)
        system_prompt, user_prompt = build_finalize_prompts(
            user_message=ctx["user_message"],
            referenced_prior_turns=ctx["referenced_prior_turns"],
            capability=capability,
            plan=ctx["plan"],
            todo_list=ctx["todo_list"],
            dr_iteration_records=ctx["dr_iteration_records"],
            observations=ctx["observations"],
            executed_tools=ctx["executed_tools"],
            transcript_text=ctx["transcript_text"],
            runtime_state_text=ctx["runtime_state_text"],
            targets=ctx["targets"],
        )
        return system_prompt, user_prompt, ctx

    ctx = _collect_simple_tool_context(interactive, context)
    system_prompt, user_prompt = build_finalize_prompts(
        user_message=ctx["user_message"],
        synthesized=ctx["synthesized"],
        last_result=ctx["last_result"],
        retry_attempts=ctx["retry_attempts"],
        aggregated_findings=ctx["aggregated_findings"],
        requested_output_format=ctx["requested_output_format"],
        referenced_prior_turns=ctx["referenced_prior_turns"],
        metadata=ctx["metadata"],
        relevant_findings=ctx["relevant_findings"],
        current_goal=ctx["current_goal"],
        turn_sequence=ctx["turn_sequence"],
        capability=capability,
    )
    return system_prompt, user_prompt, ctx


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


async def _stream_and_capture_usage(
    *,
    llm_client: Any,
    call_settings: Any,
    messages: List[Dict[str, Any]],
    interactive: InteractiveState,
    writer: Any,
    emitter: Any,
    operation_label: str,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Run the LLM stream, push deltas through the emitter, capture usage."""
    reasoning_effort = get_llm_reasoning_effort(llm_client)
    final_text = ""
    captured_usage: Optional[Dict[str, Any]] = None

    require_usage_aware_streaming(
        llm_client,
        call_settings,
        operation=f"{operation_label}_stream",
        task_id=interactive.facts.task_id,
    )
    stream_response_candidate = llm_client.stream_chat_messages_with_usage(
        messages,
        temperature=0.35,
        max_tokens=LIMITS.deep_reasoning_final,
        reasoning_effort=reasoning_effort,
    )
    stream_response = (
        await wait_for_with_timeout(
            stream_response_candidate,
            timeout_sec=LLM_TIMEOUT_CONVERSATION_MAIN_SEC,
            component="CONVERSATION_MAIN",
            operation=f"{operation_label}_stream_setup",
            logger=logger,
            task_id=interactive.facts.task_id,
            outcome=f"{operation_label}_timeout",
        )
        if inspect.isawaitable(stream_response_candidate)
        else stream_response_candidate
    )
    async for chunk in iter_with_idle_timeout(
        stream_response.content_iterator,
        timeout_sec=LLM_STREAM_IDLE_TIMEOUT_CONVERSATION_MAIN_SEC,
        component="CONVERSATION_MAIN",
        operation=f"{operation_label}_stream",
        logger=logger,
        task_id=interactive.facts.task_id,
        outcome="stream_idle_timeout",
    ):
        text = normalize_stream_chunk(chunk)
        if not text:
            continue
        final_text += text
        if writer and emitter:
            emitter.emit_message_delta(text)

    usage = require_final_stream_usage(
        stream_response.get_final_usage(),
        call_settings,
        operation=f"{operation_label}_stream",
        task_id=interactive.facts.task_id,
    )
    captured_usage = _usage_to_dict(
        usage,
        operation_label,
        request_mode="streaming",
    )
    if captured_usage:
        logger.debug(
            "[FINALIZE] Captured streaming usage: %s tokens",
            captured_usage.get("total_tokens", 0),
        )

    return final_text, captured_usage


def _create_emitter(
    *,
    capability: str,
    writer: Any,
    interactive: InteractiveState,
    config: Optional[Dict[str, Any]],
    context: Optional[GraphRuntimeContext],
) -> Any:
    """Branch emitter creation by capability to preserve UI semantics."""
    if not writer:
        return None
    if capability == "deep_reasoning":
        return EventEmitterFactory.create_turn_level(writer, interactive, config, context)
    return EventEmitterFactory.create_simple(writer, interactive, config, context)


def _todo_failed_final_response(metadata: Mapping[str, Any]) -> str | None:
    """Return deterministic todo-bootstrap failure text when configured."""
    if metadata.get("bootstrap_mode") != "todo_failed":
        return None
    text = str(metadata.get("todo_failed_final_response") or "").strip()
    if text:
        return text
    return "I need a valid clarification before I can safely continue."


def _finalize_todo_failed(
    *,
    interactive: InteractiveState,
    final_text: str,
    writer: Any,
    emitter: Any,
) -> dict:
    """Finalize a quick-bootstrap failure without invoking the finalizer LLM."""
    if writer and emitter:
        emitter.emit_message_start()
        emitter.emit_message_delta(final_text)
        emitter.emit_section_end("final_answer")

    interactive.trace.final_text = final_text
    interactive.trace.reasoning.append(
        "Returned deterministic quick-bootstrap clarification fallback."
    )
    return interactive.as_graph_update()


# ---------------------------------------------------------------------------
# Public node
# ---------------------------------------------------------------------------


async def finalize_results(
    state: Mapping[str, object] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
    config: Optional[Dict[str, Any]] = None,
) -> dict:
    """Run a single LLM pass producing the streamed user-facing final answer.

    The capability ladder governs section selection and emitter choice:

    - ``deep_reasoning``: bundle-driven transcript + DR-only sections, the
      turn-level emitter, and the operator-voice ``addendum_dr`` system
      directive.
    - any other capability: simple-tool section spine (PTR surfaces, phase
      memory, retry aggregation), the simple emitter, and no DR addendum.
    """

    interactive = InteractiveState.from_mapping(state)
    facts = interactive.facts
    metadata = facts.ensure_metadata()
    writer = get_stream_writer()

    capability = "deep_reasoning" if _is_deep_reasoning(facts) else "simple_tool_execution"
    operation_label = (
        "deep_reasoning_finalizer"
        if capability == "deep_reasoning"
        else "finalize_tool_results"
    )

    emitter = _create_emitter(
        capability=capability,
        writer=writer,
        interactive=interactive,
        config=config,
        context=context,
    )

    todo_failed_text = _todo_failed_final_response(metadata)
    if todo_failed_text is not None:
        return _finalize_todo_failed(
            interactive=interactive,
            final_text=todo_failed_text,
            writer=writer,
            emitter=emitter,
        )

    system_prompt, user_prompt, _collected = _build_messages(
        interactive=interactive,
        context=context,
        capability=capability,
    )

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

        if writer and emitter:
            emitter.emit_message_start()

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        final_text, captured_usage = await _stream_and_capture_usage(
            llm_client=llm_client,
            call_settings=call_settings,
            messages=messages,
            interactive=interactive,
            writer=writer,
            emitter=emitter,
            operation_label=operation_label,
        )

        final_text = final_text.strip()
        if not final_text:
            raise RuntimeError("Final LLM response was empty.")

        if writer and emitter:
            emitter.emit_section_end("final_answer")

    except Exception as exc:
        logger.error(
            "Unified finalizer (%s) failed: %s", capability, exc, exc_info=True
        )
        raise

    interactive.trace.final_text = final_text
    interactive.trace.reasoning.append(
        f"Generated final response via unified finalizer ({capability})."
    )

    if captured_usage:
        interactive.trace.usage_records.append(captured_usage)

    # Simple-tool retry housekeeping: clear retry-related metadata now that
    # the turn is complete, ensuring fresh state for the next turn. DR runs
    # do not populate these keys, so the pop is a safe no-op for them.
    metadata.pop("plan_retry_corrected", None)
    metadata.pop("retry_attempts", None)
    interactive.facts.metadata = metadata

    return interactive.as_graph_update()


__all__ = [
    "_build_prompts",
    "_extract_targets",
    "_resolve_finalizer_bundle_sections",
    "finalize_results",
]
