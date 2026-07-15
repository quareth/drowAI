"""Shared turn-runtime helpers for LangGraph chat branch handlers.

This module owns orchestration mechanics that must stay consistent across
normal-chat, simple-tool, and deep-reasoning handlers: turn identity setup,
state-container reuse, checkpointer setup, completion-callback draining,
final-state parsing, cancellation/interrupt result construction, and usage
normalization. Branch-specific graph selection and result decoration remain in
the individual handlers.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from agent.graph import InteractiveInput, InteractiveState, build_initial_state
from backend.services.chat.turn_number_service import get_turn_number_service

from backend.services.langgraph_chat.execution.completion_callback import StreamEmitter
from backend.services.langgraph_chat.runtime.state_container import ChatStateContainer
from ..contracts import ChatInputs, LangGraphChatResult, LangGraphRuntimeConfig
from ..facade_helpers import (
    build_metadata,
    inject_intent_classifier_usage,
)

if TYPE_CHECKING:
    from backend.services.usage_tracking.insights_models import UsageRecordWithMetadata

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TurnIdentity:
    """Canonical turn identifiers resolved for a handler invocation."""

    turn_id: Any
    turn_number: Any
    metadata: Dict[str, Any]


def ensure_turn_identity(
    runtime_config: LangGraphRuntimeConfig,
    *,
    logger_: logging.Logger,
) -> TurnIdentity:
    """Resolve and write canonical turn identifiers into runtime metadata."""

    chat_inputs = runtime_config.chat_inputs
    task_id = chat_inputs.task_id
    metadata = runtime_config.metadata or {}
    turn_id = metadata.get("turn_id")
    turn_number = metadata.get("turn_number")

    if turn_id is None or turn_number is None:
        turn_number_service = get_turn_number_service()
        turn_number = turn_number_service.get_next_turn_number(
            task_id=task_id,
            conversation_id=chat_inputs.conversation_id,
        )
        turn_id = f"task-{task_id}-turn-{turn_number}"

    logger_.info(
        "[HANDLER] Assigned turn_number=%s turn_id=%s to task %s",
        turn_number,
        turn_id,
        task_id,
    )
    runtime_config.metadata.setdefault("turn_id", turn_id)
    runtime_config.metadata.setdefault("turn_number", turn_number)
    runtime_config.metadata.setdefault("turn_sequence", turn_number)

    return TurnIdentity(
        turn_id=turn_id,
        turn_number=turn_number,
        metadata=metadata,
    )


def build_initial_interactive_state(
    runtime_config: LangGraphRuntimeConfig,
) -> tuple[Dict[str, Any], Optional[int]]:
    """Build initial graph state and inject already-captured intent usage."""

    chat_inputs = runtime_config.chat_inputs
    payload = InteractiveInput(
        task_id=chat_inputs.task_id,
        message=chat_inputs.message,
        conversation_id=chat_inputs.conversation_id,
        metadata=build_metadata(chat_inputs, runtime_config),
    )
    initial_state = build_initial_state(payload)
    injected_tokens = inject_intent_classifier_usage(
        initial_state=initial_state,
        runtime_config=runtime_config,
    )
    return initial_state, injected_tokens


def apply_agent_thread_config(
    config: Dict[str, Any],
    *,
    task_id: int,
    graph_name: str,
    turn: TurnIdentity,
    conversation_id: Optional[str],
) -> str:
    """Attach graph and canonical turn identifiers to a thread config."""

    config.setdefault("configurable", {})
    configurable = config["configurable"]
    configurable["graph_name"] = graph_name
    configurable["canonical_turn_id"] = turn.turn_id
    configurable["canonical_turn_sequence"] = turn.turn_number
    configurable["canonical_conversation_id"] = conversation_id or ""
    thread_id = configurable.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise RuntimeError(f"Missing LangGraph thread_id for task {task_id}")
    return thread_id


def new_captured_state(*, include_interrupted: bool = False) -> Dict[str, Any]:
    """Return the mutable state holder used across callback execution."""

    captured_state: Dict[str, Any] = {
        "final_state": None,
        "interactive_state": None,
        "execution_metadata": {},
    }
    if include_interrupted:
        captured_state["interrupted"] = False
    return captured_state


def build_or_reuse_state_container(
    runtime_config: LangGraphRuntimeConfig,
    *,
    reserved_message_id: Optional[int],
) -> ChatStateContainer:
    """Reuse the facade turn container when present, otherwise create one."""

    shared = runtime_config.persistence.state_container
    if shared is not None:
        shared.reserved_message_id = reserved_message_id
        return shared
    return ChatStateContainer(reserved_message_id=reserved_message_id)


def record_execution_metadata(
    captured_state: Dict[str, Any],
    execution_metadata: Any,
) -> None:
    """Copy executor metadata into the captured-state holder when available."""

    if isinstance(execution_metadata, dict):
        captured_state["execution_metadata"] = dict(execution_metadata)


def merge_execution_metadata(
    metadata: Dict[str, Any],
    captured_state: Dict[str, Any],
) -> Dict[str, Any]:
    """Merge captured executor metadata into a result metadata mapping."""

    if isinstance(captured_state.get("execution_metadata"), dict):
        metadata.update(captured_state["execution_metadata"])
    return metadata


async def drain_completion_callback(
    *,
    callback_runner: Callable[..., AsyncIterator[Any]],
    turn: TurnIdentity,
    task_id: int,
    conversation_id: str,
    llm_func: Callable[[StreamEmitter, Dict[str, Any]], Any],
    should_cancel: Callable[[], bool],
    state_container: ChatStateContainer,
    reserved_message_id: Optional[int],
    result_holder: Dict[str, Any],
    prefill_reasoning_tokens: Optional[str],
) -> None:
    """Run the completion callback and discard already-streamed events."""

    async for _event in callback_runner(
        turn_id=turn.turn_id,
        turn_number=turn.turn_number,
        task_id=task_id,
        conversation_id=conversation_id,
        llm_func=llm_func,
        should_cancel=should_cancel,
        state_container=state_container,
        reserved_message_id=reserved_message_id,
        result_holder=result_holder,
        prefill_reasoning_tokens=prefill_reasoning_tokens,
    ):
        pass


def prefill_reasoning_tokens_from(metadata: Dict[str, Any]) -> Optional[str]:
    """Return the optional intent-phase reasoning prefill for persistence."""

    value = metadata.get("intent_phase_reasoning_text")
    return value if isinstance(value, str) else None


def parse_interactive_state_from_final(
    *,
    final_state: Any,
    starting_state: InteractiveState,
    deterministic_mode: bool,
    state_container: ChatStateContainer,
    task_id: int,
    missing_state_message: str,
    on_missing_state: Optional[Callable[[], None]] = None,
) -> InteractiveState:
    """Parse a final graph snapshot, including deterministic partial snapshots."""

    interactive_state: Optional[InteractiveState] = None
    if final_state:
        try:
            interactive_state = InteractiveState.from_mapping(final_state)
        except Exception:
            if not deterministic_mode:
                raise
            logger.debug(
                "[HANDLER] Deterministic final_state is not full InteractiveState for task %s",
                task_id,
                exc_info=True,
            )

    if interactive_state is not None:
        return interactive_state

    if not deterministic_mode:
        if on_missing_state is not None:
            on_missing_state()
        raise RuntimeError(missing_state_message)

    interactive_state = InteractiveState.from_mapping(starting_state.as_graph_state())
    snapshot_final = ""
    if isinstance(final_state, dict):
        trace = final_state.get("trace")
        if isinstance(trace, dict):
            snapshot_final = str(trace.get("final_text") or "")
    streamed_final = state_container.get_answer_tokens().strip()
    final_text = streamed_final or snapshot_final or interactive_state.facts.message
    interactive_state.trace.final_text = final_text
    interactive_state.facts.message = final_text
    return interactive_state


def build_cancelled_result(
    *,
    chat_inputs: ChatInputs,
    thread_id: str,
    graph_name: str,
    captured_state: Dict[str, Any],
) -> LangGraphChatResult:
    """Build the common cancelled-turn result payload."""

    cancel_metadata = {
        "cancelled": True,
        "interrupt_type": "run_cancelled",
        "thread_id": thread_id,
        "graph_name": graph_name,
    }
    merge_execution_metadata(cancel_metadata, captured_state)
    return LangGraphChatResult(
        final_text=None,
        conversation_id=chat_inputs.conversation_id,
        interactive_state=None,
        metadata=cancel_metadata,
        persistence_handled=True,
    )


def build_interrupted_result(
    *,
    chat_inputs: ChatInputs,
    thread_id: str,
    graph_name: str,
    captured_state: Dict[str, Any],
) -> LangGraphChatResult:
    """Build the common HITL tool-approval interrupt result payload."""

    interrupted_metadata = {
        "interrupted": True,
        "interrupt_type": "tool_approval",
        "thread_id": thread_id,
        "graph_name": graph_name,
    }
    merge_execution_metadata(interrupted_metadata, captured_state)
    return LangGraphChatResult(
        final_text=None,
        conversation_id=chat_inputs.conversation_id,
        interactive_state=None,
        metadata=interrupted_metadata,
        persistence_handled=True,
    )


def extract_usage_from_state(
    interactive_state: InteractiveState,
    *,
    execution_branch: str = "unknown",
    turn_index: Optional[int] = None,
) -> Optional[List["UsageRecordWithMetadata"]]:
    """Extract per-call usage with canonical metadata from trace records."""

    usage_records = getattr(interactive_state.trace, "usage_records", None)
    if not usage_records:
        return None

    try:
        from backend.services.usage_tracking.insights_models import (
            UsageRecordWithMetadata,
            build_usage_metadata_from_trace_record,
        )
        from backend.services.usage_tracking.models import (
            CACHE_REPORTING_UNKNOWN,
            ProviderUsageComponents,
            UsageData,
        )

        envelopes: List[UsageRecordWithMetadata] = []
        for record in usage_records:
            if not isinstance(record, dict):
                continue

            provider = record.get("provider", "openai")
            usage = UsageData(
                prompt_tokens=record.get("prompt_tokens", 0),
                completion_tokens=record.get("completion_tokens", 0),
                total_tokens=record.get("total_tokens", 0),
                model=record.get("model", "unknown"),
                provider=provider,
                cached_tokens=record.get("cached_tokens", 0),
                reasoning_tokens=record.get("reasoning_tokens", 0),
                api_surface=(
                    record.get("api_surface")
                    if isinstance(record.get("api_surface"), str)
                    else CACHE_REPORTING_UNKNOWN
                ),
                cache_reporting=(
                    record.get("cache_reporting")
                    if record.get("cache_reporting")
                    in ("reported", "not_reported", "unknown")
                    else CACHE_REPORTING_UNKNOWN
                ),
                provider_usage_components=ProviderUsageComponents.from_mapping(
                    record.get("provider_usage_components")
                ),
            )
            metadata = build_usage_metadata_from_trace_record(
                record,
                execution_branch=execution_branch,
                provider=provider if isinstance(provider, str) else "unknown",
                turn_index=turn_index,
            )
            envelopes.append(UsageRecordWithMetadata(usage=usage, metadata=metadata))

        return envelopes if envelopes else None

    except ImportError:
        logger.warning("[HANDLER] UsageData not available, skipping usage extraction")
        return None
    except Exception as exc:
        logger.warning("[HANDLER] Failed to extract usage from state: %s", exc)
        return None


__all__ = [
    "TurnIdentity",
    "apply_agent_thread_config",
    "build_cancelled_result",
    "build_initial_interactive_state",
    "build_interrupted_result",
    "build_or_reuse_state_container",
    "drain_completion_callback",
    "ensure_turn_identity",
    "extract_usage_from_state",
    "merge_execution_metadata",
    "new_captured_state",
    "parse_interactive_state_from_final",
    "prefill_reasoning_tokens_from",
    "record_execution_metadata",
]
