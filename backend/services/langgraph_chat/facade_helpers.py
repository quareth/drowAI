"""
Shared helper functions for facade and handlers.

These methods are used by both the facade and handler classes to build
configurations, metadata, and results. Extracted here to avoid circular dependencies.
"""

from __future__ import annotations

import logging
import re
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
)

from agent.graph import InteractiveState
from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    update_prior_turn_references,
)
from agent.graph.context.runtime_state import refresh_bundle_from_working_memory
from agent.graph.infrastructure.state_models import (
    GraphRuntimeContext,
    checkpoint_safe_llm_runtime_selection,
)
from backend.services.metrics.utils import safe_inc
from backend.services.langgraph_chat.checkpoint.thread_identity import format_graph_thread_id

from .contracts import (
    ChatInputs,
    LangGraphChatResult,
    LangGraphRuntimeConfig,
    runtime_warmup_status_from_steps,
)
from .hitl_constants import GRAPH_RECURSION_LIMIT
from backend.services.langgraph_chat.intent.briefs import (
    METADATA_KEY_INTENT_BRIEF_SEED,
    METADATA_KEY_INTENT_TARGET_CONTINUITY,
    METADATA_KEY_INTENT_TARGET_RESOLUTION,
    METADATA_KEY_REQUEST_CONTRACT,
    METADATA_KEY_TURN_INTERPRETATION,
)

if TYPE_CHECKING:
    from backend.services.usage_tracking.models import UsageData

logger = logging.getLogger(__name__)
_METRIC_LABEL_SANITIZE_RE = re.compile(r"[^a-z0-9_]+")


def coerce_turn_sequence(value: Any) -> Optional[int]:
    """Return an integer turn sequence when runtime metadata provides one."""
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def build_metadata(
    chat_inputs: ChatInputs,
    runtime_config: LangGraphRuntimeConfig,
) -> Dict[str, Any]:
    """Compose metadata payload inserted into the initial interactive state."""
    metadata_source = runtime_config.metadata or {}
    safe_runtime_selection = checkpoint_safe_llm_runtime_selection(
        chat_inputs.llm_runtime_selection,
    )

    metadata = {
        "history_turns": len(list(chat_inputs.history)),
        "conversation_history": list(chat_inputs.history),
        "user_id": chat_inputs.user_id,
        "execution_mode": runtime_config.execution_mode.value,
    }

    if chat_inputs.provider:
        metadata["provider"] = chat_inputs.provider
    if chat_inputs.model:
        metadata["model"] = chat_inputs.model
    if safe_runtime_selection:
        metadata["llm_runtime_selection"] = safe_runtime_selection
    provider = metadata_source.get("provider")
    if isinstance(provider, str) and provider.strip():
        metadata["provider"] = provider.strip()
    runtime_provider = metadata_source.get("runtime_provider")
    if isinstance(runtime_provider, str) and runtime_provider.strip():
        metadata["runtime_provider"] = runtime_provider.strip()
    runtime_model = metadata_source.get("runtime_model")
    if isinstance(runtime_model, str) and runtime_model.strip():
        metadata["runtime_model"] = runtime_model.strip()
    if chat_inputs.reasoning_effort:
        metadata["reasoning_effort"] = chat_inputs.reasoning_effort

    feature_flags = dict(runtime_config.metadata.get("feature_flags", {}))
    if "mode" not in feature_flags:
        feature_flags["mode"] = runtime_config.execution_mode.value
    normalized_flags: Dict[str, bool] = {}
    for key, value in feature_flags.items():
        if isinstance(value, bool):
            normalized_flags[key] = value
        else:
            normalized_flags[key] = bool(value)
    turn_id = metadata_source.get("turn_id")
    turn_sequence = metadata_source.get("turn_sequence")
    if turn_sequence is None:
        turn_sequence = metadata_source.get("turn_number")
    reserved_message_id = metadata_source.get("reserved_message_id")
    if not turn_id:
        thread_cfg = (
            metadata_source.get("thread_config", {})
            if isinstance(metadata_source.get("thread_config"), dict)
            else {}
        )
        configurable = (
            thread_cfg.get("configurable")
            if isinstance(thread_cfg.get("configurable"), dict)
            else {}
        )
        turn_id = configurable.get("thread_id") or turn_id
    runtime_context = GraphRuntimeContext(
        task_id=chat_inputs.task_id,
        user_id=chat_inputs.user_id,
        graph_thread_id=metadata_source.get("graph_thread_id"),
        tenant_id=metadata_source.get("tenant_id"),
        runtime_placement_mode=metadata_source.get("runtime_placement_mode"),
        workspace_id=metadata_source.get("workspace_id"),
        actor_type=metadata_source.get("actor_type"),
        actor_id=metadata_source.get("actor_id"),
        runner_id=metadata_source.get("runner_id"),
        execution_site_id=metadata_source.get("execution_site_id"),
        feature_flags=normalized_flags,
        workspace_path=metadata_source.get("workspace_path"),
        provider=metadata.get("provider"),
        model=chat_inputs.model,
        llm_runtime_selection=safe_runtime_selection,
        reasoning_effort=chat_inputs.reasoning_effort,
        turn_id=turn_id,
        turn_sequence=turn_sequence,
        reserved_message_id=reserved_message_id,
    )
    runtime_context_payload = runtime_context.model_dump()
    runtime_context_payload.pop("credential_ref", None)
    metadata["graph_runtime_context"] = runtime_context_payload
    if turn_sequence is not None:
        metadata["turn_sequence"] = turn_sequence
    if turn_id:
        metadata["turn_id"] = turn_id
    if reserved_message_id is not None:
        metadata["reserved_message_id"] = reserved_message_id
    runtime_config.metadata["graph_runtime_context"] = runtime_context_payload
    if runtime_config.metadata.get("force_simple_chat"):
        metadata["force_simple_chat"] = True
    for key in (
        "intent_signal_cache",
        "intent_hints",
        "risk_flags",
        "eligible_routes",
        "intent_signals",
        "forced_capability",
        "simple_chat_forced",
        "intent_classifier_reasoning",
        "intent_classifier_label",
        "intent_classifier_raw_label",
        "intent_classifier_route_forced",
        "intent_classifier_route_force_source",
        "intent_classifier_skipped",
        "intent_classifier_bypassed",
        "intent_classifier_raw_response",
        "intent_prior_turn_reference",
        "execution_route_policy",
        "environment_info",
        METADATA_KEY_TURN_INTERPRETATION,
        METADATA_KEY_INTENT_BRIEF_SEED,
        METADATA_KEY_REQUEST_CONTRACT,
        METADATA_KEY_INTENT_TARGET_RESOLUTION,
        METADATA_KEY_INTENT_TARGET_CONTINUITY,
        "agent_mode",  # HITL: Pass agent mode to graph for approval checks
        "plan_mode",
        "plan_review_required",
    ):
        if key in runtime_config.metadata:
            metadata[key] = runtime_config.metadata[key]

    # Phase 4 Task 4.1: derive a transient graph-entry override from
    # the user-surface route policy. ``execution_route_policy`` is the
    # only durable forced-route authority; the graph-internal override
    # is a derived, ephemeral signal that lets ``intent_router`` /
    # graph classification honor the same route the facade enforces.
    #
    # Only `plan` requires this — the deep-reasoning graph still runs
    # its own intent classification at graph entry and would otherwise
    # be free to resolve to ``respond_only`` / ``fallback_finalize``
    # even when the facade picked ``DeepReasoningHandler``. `chat`
    # does NOT need a graph-entry override because the normal-chat
    # handler enters the simple-chat graph directly.
    policy = runtime_config.metadata.get("execution_route_policy")
    if isinstance(policy, dict):
        forced_label = str(policy.get("forced_classifier_label") or "").strip().lower()
        if forced_label == "plan_executor":
            metadata["intent_router_graph_entry_override"] = "deep_reasoning"

    # Single assembly authority: ``LangGraphContextBuilder.build_runtime_config``
    # is the only place that builds the ``ConversationContextBundle``
    # for a turn. We copy the pre-built bundle from ``runtime_config.metadata``
    # into the initial graph state so every prompt-authoritative role
    # reads the same bundle object. If working memory has been seeded
    # between runtime_config assembly and initial-state assembly
    # (e.g. by the intent classifier path), refresh runtime-state /
    # evidence refs in place so the graph's opening bundle stays aligned
    # with canonical working memory without re-running the builder.
    bundle = metadata_source.get(METADATA_CONTEXT_BUNDLE_KEY)
    if not isinstance(bundle, dict):
        raise RuntimeError(
            "facade_helpers.build_metadata: runtime_config.metadata "
            "is missing the ConversationContextBundle. The context "
            "builder (LangGraphContextBuilder.build_runtime_config) "
            "must populate it before initial graph state is assembled."
        )
    metadata[METADATA_CONTEXT_BUNDLE_KEY] = bundle
    refresh_bundle_from_working_memory(metadata_source)
    prior_turn_references = metadata_source.get("prior_turn_references")
    if isinstance(prior_turn_references, dict):
        update_prior_turn_references(bundle, prior_turn_references)
    return metadata


def inject_intent_classifier_usage(
    *,
    initial_state: Dict[str, Any],
    runtime_config: LangGraphRuntimeConfig,
) -> Optional[int]:
    """Inject intent-classifier usage into initial graph state trace records.

    Returns:
        Total token count when usage was injected, otherwise ``None``.
    """
    intent_usage = runtime_config.metadata.get("_intent_classifier_usage")
    if not intent_usage or not hasattr(intent_usage, "prompt_tokens"):
        return None

    usage_dict = {
        "prompt_tokens": intent_usage.prompt_tokens,
        "completion_tokens": intent_usage.completion_tokens,
        "total_tokens": intent_usage.total_tokens,
        "model": getattr(intent_usage, "model", "unknown"),
        "provider": getattr(intent_usage, "provider", "openai"),
        "cached_tokens": getattr(intent_usage, "cached_tokens", 0),
        "reasoning_tokens": getattr(intent_usage, "reasoning_tokens", 0),
        # Propagate the cache-reporting signal stamped by the extractor
        # (``UsageData.from_openai_*``). If the intent-classifier usage was
        # built without those fields (legacy callers), the handler-boundary
        # builder falls back to the ``(provider, api_surface)`` classifier
        # or, failing that, the explicit ``"unknown"`` bucket — never a
        # silent ``"reported"`` zero.
        "api_surface": getattr(intent_usage, "api_surface", "unknown"),
        "cache_reporting": getattr(intent_usage, "cache_reporting", "unknown"),
        "request_mode": "non_streaming",
        "source": "intent_classifier",
    }
    provider_usage_components = getattr(
        intent_usage, "provider_usage_components", None
    )
    if provider_usage_components is not None:
        to_dict = getattr(provider_usage_components, "to_dict", None)
        if callable(to_dict):
            try:
                serialized_components = to_dict()
            except Exception:
                serialized_components = None
            if isinstance(serialized_components, dict):
                usage_dict["provider_usage_components"] = serialized_components

    trace = initial_state.setdefault("trace", {})
    if not isinstance(trace, dict):
        return None
    usage_records = trace.setdefault("usage_records", [])
    if not isinstance(usage_records, list):
        return None
    usage_records.append(usage_dict)
    return int(getattr(intent_usage, "total_tokens", 0))


def build_thread_config(
    runtime_config: LangGraphRuntimeConfig,
    task_id: int,
) -> Dict[str, Any]:
    """Ensure consistent thread configuration for LangGraph runs.

    Builds LangGraph config with:
    - thread_id for conversation tracking
    - checkpoint_id for resumption (if anchor_sequence provided)
    - recursion_limit for safety

    NOTE: Checkpointer is no longer added here - graphs are compiled with
    persistent checkpointers directly in handler methods.

    Args:
        runtime_config: Runtime configuration with chat inputs and metadata
        task_id: Task ID for checkpoint isolation

    Returns:
        LangGraph config dict with thread settings
    """
    config = dict(runtime_config.metadata.get("thread_config") or {})
    configurable = dict(config.get("configurable") or {})

    expected_thread_id = format_graph_thread_id(
        runtime_config.metadata.get("graph_thread_id"),
        task_id=task_id,
    )
    existing_thread_id = configurable.get("thread_id")
    if existing_thread_id is not None and existing_thread_id != expected_thread_id:
        raise RuntimeError(
            f"Task {int(task_id)} checkpoint thread_id does not match graph_thread_id"
        )
    configurable["thread_id"] = expected_thread_id
    safe_runtime_selection = checkpoint_safe_llm_runtime_selection(
        runtime_config.llm_runtime_selection,
    )

    runtime_projection = {
        "task_id": runtime_config.chat_inputs.task_id,
        "user_id": runtime_config.chat_inputs.user_id,
        "graph_thread_id": runtime_config.metadata.get("graph_thread_id"),
        "provider": runtime_config.chat_inputs.provider,
        "model": runtime_config.chat_inputs.model,
        "reasoning_effort": runtime_config.chat_inputs.reasoning_effort,
        "tenant_id": runtime_config.metadata.get("tenant_id"),
        "runtime_placement_mode": runtime_config.metadata.get("runtime_placement_mode"),
        "workspace_id": runtime_config.metadata.get("workspace_id"),
        "actor_type": runtime_config.metadata.get("actor_type"),
        "actor_id": runtime_config.metadata.get("actor_id"),
        "runner_id": runtime_config.metadata.get("runner_id"),
        "execution_site_id": runtime_config.metadata.get("execution_site_id"),
        "workspace_path": runtime_config.metadata.get("workspace_path"),
    }
    if safe_runtime_selection:
        runtime_projection["llm_runtime_selection"] = safe_runtime_selection
    configurable.setdefault("runtime_projection", runtime_projection)
    if safe_runtime_selection:
        configurable.setdefault(
            "llm_runtime_selection",
            safe_runtime_selection,
        )
    if runtime_config.runtime_services is not None:
        configurable.setdefault("runtime_services", runtime_config.runtime_services)

    if "graph_runtime_context" not in configurable:
        context = runtime_config.metadata.get("graph_runtime_context")
        if context is not None:
            configurable["graph_runtime_context"] = context

    # Add checkpoint_id if resuming from specific checkpoint
    if hasattr(runtime_config, "persistence") and runtime_config.persistence:
        anchor_sequence = getattr(runtime_config.persistence, "anchor_sequence", None)
        if anchor_sequence:
            # CRITICAL: checkpoint_id must be a string, not an integer
            # PostgreSQL checkpointer expects text type for checkpoint_id
            configurable["checkpoint_id"] = str(anchor_sequence)
            logger.info(
                f"[FACADE] Resuming task {task_id} from checkpoint: {anchor_sequence}"
            )

    config["configurable"] = configurable

    # Add recursion limit for safety
    config.setdefault("recursion_limit", GRAPH_RECURSION_LIMIT)

    logger.debug(
        f"[FACADE] Built thread config for task {task_id}, "
        f"thread_id={configurable['thread_id']}"
    )

    return config


def build_intent_metadata(state: InteractiveState) -> Dict[str, Any]:
    """Build intent metadata from interactive state."""
    hints = state.facts.intent_hints or {}
    router = (
        state.facts.metadata.get("intent_router") if state.facts.metadata else {}
    ) or {}
    return {
        "capability": state.facts.capability or "respond_only",
        "classifier_label": hints.get("classifier_label"),
        "classifier_confidence": hints.get("classifier_confidence"),
        "tool_hints": hints.get("tool_hints") or [],
        "targets": hints.get("targets") or [],
        "risk_flags": state.facts.risk_flags or [],
        "router": router,
    }


def build_result(
    *,
    final_text: Optional[str],
    conversation_id: Optional[str],
    interactive_state: InteractiveState,
    metadata: Dict[str, Any],
    events: Iterable[Dict[str, Any]],
    turn_id: Optional[str] = None,
    streaming_adapter: Any = None,
    usage: Optional[List["UsageData"]] = None,
) -> LangGraphChatResult:
    """Create a LangGraphChatResult with an async iterator for events.

    Args:
        final_text: Final response text
        conversation_id: Conversation identifier
        interactive_state: Final interactive state from graph execution
        metadata: Additional metadata to include in result
        events: Events to make available via iterator
        turn_id: Optional turn identifier
        streaming_adapter: Optional streaming adapter (unused, kept for compatibility)
        usage: Optional list of UsageData from LLM calls during this turn

    Returns:
        LangGraphChatResult with all data populated
    """

    intent_metadata = build_intent_metadata(interactive_state)
    safe_inc(f"intent_route_{intent_metadata['capability']}")

    events_list = list(events)

    # Intent summary removed from user-facing response
    # Internal routing information should not be displayed to end users
    # The metadata is still available in event["metadata"]["intent_summary"] for debugging

    include_intent_metadata = (
        interactive_state.facts.capability
        and interactive_state.facts.capability != "respond_only"
    )
    for event in events_list:
        event.setdefault("metadata", {})
        if include_intent_metadata:
            event["metadata"].setdefault("intent_summary", intent_metadata)
        else:
            event["metadata"].pop("intent_summary", None)
        if turn_id and "id" not in event["metadata"]:
            event["metadata"]["id"] = turn_id

    def _event_iterator() -> AsyncIterator[Dict[str, Any]]:
        return _iterate_events(events_list)

    return LangGraphChatResult(
        final_text=final_text,
        conversation_id=conversation_id,
        interactive_state=interactive_state,
        metadata={**metadata, "intent_summary": intent_metadata},
        _event_iterator=_event_iterator,
        usage=usage,
    )


def emit_hitl_stage_timing(
    *,
    stage: str,
    timestamp: Optional[float],
    task_id: int,
    interrupt_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
) -> None:
    """Emit structured HITL timing markers with correlation identifiers."""
    if timestamp is None:
        return
    logger.info(
        "[HITL_TIMING] stage=%s task_id=%s interrupt_id=%s tool_call_id=%s ts=%.9f",
        stage,
        task_id,
        interrupt_id or "unknown",
        tool_call_id or "unknown",
        float(timestamp),
    )


def sanitize_metric_label(raw_value: Optional[str]) -> str:
    """Normalize free-form values into stable metric label suffixes."""
    normalized = _METRIC_LABEL_SANITIZE_RE.sub(
        "_", str(raw_value or "unknown").lower()
    ).strip("_")
    return normalized or "unknown"


def emit_labeled_latency_metric(
    metric_name: str,
    value_ms: float,
    *,
    graph_name: Optional[str],
    runtime_path: Optional[str],
) -> None:
    """Emit base and stable label-suffixed variants for latency gauges."""
    try:
        from backend.services.metrics.utils import safe_gauge

        numeric_value = max(0.0, float(value_ms))
        graph_label = sanitize_metric_label(graph_name)
        path_label = sanitize_metric_label(runtime_path)
        safe_gauge(metric_name, numeric_value)
        safe_gauge(f"{metric_name}_graph_{graph_label}", numeric_value)
        safe_gauge(f"{metric_name}_path_{path_label}", numeric_value)
    except Exception:
        return


def get_runtime_warmup_status(task_id: int) -> Any:
    """Fetch runtime warmup readiness flags for a task."""
    try:
        from backend.services.langgraph_chat.runtime.warmup_service import (
            get_shared_runtime_warmup_service,
        )

        raw_status = get_shared_runtime_warmup_service().get_warmup_status(task_id)
    except Exception:
        raw_status = {}
    return runtime_warmup_status_from_steps(raw_status)


def resolve_runtime_path_label(
    task_id: int,
    *,
    get_runtime_warmup_status_fn: Callable[[int], Any] = get_runtime_warmup_status,
) -> str:
    """Classify resume path as warm/cold for stable metric labels."""
    try:
        warmup = get_runtime_warmup_status_fn(task_id)
        return "warm" if warmup.runtime_warm else "cold"
    except Exception:
        return "unknown"


def emit_resume_worker_queue_metric(
    *,
    approval_received_at: Optional[float],
    resume_worker_start_at: Optional[float],
    task_id: int,
    graph_name: Optional[str],
    resolve_runtime_path_label_fn: Callable[[int], str] = resolve_runtime_path_label,
    emit_labeled_latency_metric_fn: Callable[..., None] = emit_labeled_latency_metric,
) -> None:
    """Record approval->resume worker queue delay for HITL dashboards."""
    if approval_received_at is None or resume_worker_start_at is None:
        return
    try:
        delay_ms = (
            float(resume_worker_start_at) - float(approval_received_at)
        ) * 1000.0
    except Exception:
        return
    runtime_path = resolve_runtime_path_label_fn(task_id)
    emit_labeled_latency_metric_fn(
        "resume_worker_queue_to_start_ms",
        delay_ms,
        graph_name=graph_name,
        runtime_path=runtime_path,
    )


async def _iterate_events(
    events: Iterable[Dict[str, Any]],
) -> AsyncIterator[Dict[str, Any]]:
    """Iterate over events as async generator."""
    for event in events:
        yield event


__all__ = [
    "build_metadata",
    "build_thread_config",
    "build_intent_metadata",
    "build_result",
    "coerce_turn_sequence",
    "emit_hitl_stage_timing",
    "sanitize_metric_label",
    "emit_labeled_latency_metric",
    "get_runtime_warmup_status",
    "resolve_runtime_path_label",
    "emit_resume_worker_queue_metric",
]
