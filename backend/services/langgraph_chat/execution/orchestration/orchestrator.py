"""High-level orchestration flows for start/resume/retry turn execution.

This module owns control-flow orchestration while delegating shared side
effects and helpers to ``TurnExecutionService`` collaborators.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional

from backend.database import SessionLocal
from backend.services.chat.turn_identity_resolver import (
    resolve_turn_identity_from_reserved_message_best_effort,
)
from backend.services.langgraph_chat import AgentMode, ChatInputs, ExecutionMode
from backend.services.langgraph_chat.compression.context_models import (
    CompressionRequiredError,
)
from backend.services.langgraph_chat.facade_helpers import (
    emit_hitl_stage_timing as emit_hitl_stage_timing_helper,
    emit_resume_worker_queue_metric as emit_resume_worker_queue_metric_helper,
)
from backend.services.langgraph_chat.checkpoint.interrupt_ticket_service import (
    mark_interrupt_ticket_completed_best_effort,
    mark_interrupt_ticket_failed_best_effort,
    mark_interrupt_ticket_resumed_best_effort,
)
from backend.services.langgraph_chat.model_role_registry import (
    DEFAULT_USER_SELECTED_REASONING_EFFORT,
    ROLE_CONVERSATION_MAIN,
)
from backend.services.langgraph_chat.runtime.run_lifecycle import get_run_lifecycle_service
from backend.services.langgraph_chat.streaming.status_events import (
    publish_checkpoint_rewind_state_event,
    publish_retry_state_event,
)
from backend.services.langgraph_chat.execution.error_service import (
    TurnExecutionErrorService,
)
from backend.services.langgraph_chat.execution.orchestration.retry_lifecycle import (
    RetryLifecyclePublisher,
    build_retry_terminal_metadata,
    retry_mode_from_identity,
)
from backend.services.langgraph_chat.checkpoint.turn_workflow_service import (
    mark_turn_workflow_failed_best_effort,
    mark_turn_workflow_waiting_best_effort,
    resolve_checkpoint_retry_identity_best_effort,
    resolve_reserved_message_id_from_workflow_best_effort,
    resolve_turn_id_from_workflow_best_effort,
    start_turn_workflow_best_effort,
)
from backend.services.llm_provider.runtime_config_service import LLMRuntimeConfigService

if TYPE_CHECKING:
    from backend.services.langgraph_chat.execution.turn_service import (
        TurnExecutionService,
    )

logger = logging.getLogger(__name__)


@dataclass
class _ContinuationRuntimeContext:
    """Continuation runtime objects that must be forwarded and closed together."""

    runtime_db: Any
    selection_payload: Dict[str, Any]
    runtime_services: Any
    provider: Optional[str]
    model: Optional[str]


def _positive_int(value: Any) -> Optional[int]:
    """Return a positive integer from loose retry identity values."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _is_checkpoint_retry_resume(identity: Optional[Mapping[str, Any]]) -> bool:
    """Return True when a HITL resume belongs to a checkpoint retry attempt."""
    if not isinstance(identity, Mapping):
        return False
    retry_mode = str(identity.get("retry_mode") or "").strip().lower()
    return (
        retry_mode == "checkpoint"
        and _positive_int(identity.get("retry_attempt")) is not None
    )


def _runtime_selection_from_metadata(
    metadata: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    """Return resolved continuation runtime selection from result metadata."""

    selection = metadata.get("llm_runtime_selection")
    if not isinstance(selection, Mapping):
        return None
    return dict(selection)


def _build_continuation_runtime_context(*, user_id: int) -> _ContinuationRuntimeContext:
    """Build runtime config/services for resume and retry continuation flows."""

    runtime_db = SessionLocal()
    try:
        runtime_config_service = LLMRuntimeConfigService(runtime_db)
        runtime_selection = runtime_config_service.build_continuation_selection(
            user_id=user_id,
        )
        return _ContinuationRuntimeContext(
            runtime_db=runtime_db,
            selection_payload=runtime_selection.to_dict(),
            runtime_services=runtime_config_service.build_runtime_services(),
            provider=runtime_selection.provider,
            model=runtime_selection.model,
        )
    except Exception:
        runtime_db.close()
        raise


def _apply_runtime_selection_from_result(
    context: _ContinuationRuntimeContext,
    result_metadata: Mapping[str, Any],
) -> None:
    """Apply facade-resolved runtime selection overrides to a continuation context."""

    resolved_runtime_selection = _runtime_selection_from_metadata(result_metadata)
    if resolved_runtime_selection is None:
        return
    context.selection_payload = resolved_runtime_selection
    context.provider = (
        resolved_runtime_selection.get("provider")
        if isinstance(resolved_runtime_selection.get("provider"), str)
        else context.provider
    )
    context.model = (
        resolved_runtime_selection.get("model")
        if isinstance(resolved_runtime_selection.get("model"), str)
        else context.model
    )


async def _publish_turn_result_events_for_sequence(
    *,
    service: TurnExecutionService,
    hub: Any,
    task_id: int,
    result: Any,
    stream_sequence: Optional[int],
) -> None:
    """Publish final turn result events using the existing stream sequence rule."""

    if isinstance(stream_sequence, int):
        await service._turn_stream_publisher.publish_turn_result_events(
            hub=hub,
            task_id=task_id,
            result=result,
            turn_sequence=stream_sequence,
        )
    else:
        await service._turn_stream_publisher.publish_turn_result_events(
            hub=hub,
            task_id=task_id,
            result=result,
            turn_sequence=None,
        )


class TurnExecutionOrchestrator:
    """Orchestrates start/resume/retry execution flows using injected service state."""

    async def start_turn_generation(
        self,
        *,
        service: TurnExecutionService,
        task_id: int,
        user_id: int,
        tenant_id: Optional[int] = None,
        provider: Optional[str],
        model: str,
        runtime_selection: Optional[Dict[str, Any]] = None,
        runtime_services: Any = None,
        message: str,
        conversation_id: Optional[str],
        history: List[Dict[str, Any]],
        history_source_message_ids: Optional[List[int]] = None,
        anchor_sequence: Optional[int] = None,
        requested_mode: Optional[ExecutionMode] = None,
        agent_mode: Optional[AgentMode] = None,
        plan_mode: bool = False,
        turn_id: Optional[str] = None,
        turn_number: Optional[int] = None,
        reserved_message_id: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        deterministic_mode: bool = False,
        facade_class: Callable[[], Any],
        reserve_chat_turn: Optional[Callable[..., tuple[int, int, str, int]]] = None,
        start_turn_workflow: Optional[Callable[..., Optional[int]]] = None,
        mark_turn_workflow_waiting: Optional[Callable[..., None]] = None,
        mark_turn_workflow_completed: Optional[Callable[..., None]] = None,
        mark_turn_workflow_failed: Optional[Callable[..., None]] = None,
        compression_required_failed_error_code: str,
        retryable_post_tool_error_message: str,
        generation_failed_error_message: str,
    ) -> None:
        """Start a new turn generation and stream boundary events."""
        logger.info(
            "[CHAT] start_turn_generation started for task %s, agent_mode=%s",
            task_id,
            agent_mode,
        )
        try:
            from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub

            hub = get_in_memory_stream_hub()
        except Exception as exc:
            logger.exception("Failed to import LangGraph components - cannot proceed")
            raise RuntimeError("LangGraph components unavailable") from exc
        service._turn_stream_publisher.set_streaming_active(task_id=task_id, hub=hub)

        facade = facade_class()
        resolved_conversation_id = conversation_id
        workflow_failed_fn = (
            mark_turn_workflow_failed or mark_turn_workflow_failed_best_effort
        )
        if reserved_message_id is None:
            try:
                (
                    resolved_conversation_id,
                    reserved_message_id,
                    anchor_sequence,
                    turn_id,
                    turn_number,
                ) = service._bootstrap_service.reserve_start_turn_if_needed(
                    task_id=task_id,
                    conversation_id=resolved_conversation_id,
                    message=message,
                    anchor_sequence=anchor_sequence,
                    turn_id=turn_id,
                    turn_number=turn_number,
                    reserved_message_id=reserved_message_id,
                    reserve_chat_turn=reserve_chat_turn,
                )
            except Exception as reserve_exc:
                logger.error(
                    "[CHAT] Failed to reserve ChatMessage inside start_turn_generation "
                    "(task=%s): %s",
                    task_id,
                    reserve_exc,
                    exc_info=True,
                )
                try:
                    await service._error_service.handle_terminal_turn_error(
                        task_id=task_id,
                        hub=hub,
                        workflow_id=None,
                        reserved_message_id=reserved_message_id,
                        failure_source="initial_generation",
                        error_code="generation_failed",
                        content=generation_failed_error_message,
                        retryable=False,
                        retry_mode=None,
                        graph_name=None,
                        conversation_id=resolved_conversation_id,
                        turn_id=turn_id,
                        turn_sequence=turn_number,
                        mark_turn_workflow_failed=workflow_failed_fn,
                        publish_boundary_completion_events=service._publish_boundary_completion_events,
                    )
                finally:
                    service._turn_stream_publisher.set_streaming_inactive(
                        task_id=task_id,
                        hub=hub,
                        warn_on_error=False,
                    )
                return

        conversation_id = resolved_conversation_id
        thread_config, turn_number, turn_id, turn_sequence = (
            service._bootstrap_service.resolve_start_turn_identity(
                task_id=task_id,
                conversation_id=conversation_id,
                anchor_sequence=anchor_sequence,
                turn_id=turn_id,
                turn_number=turn_number,
                reserved_message_id=reserved_message_id,
            )
        )

        workflow_id: Optional[int] = None
        lifecycle = get_run_lifecycle_service()
        run_status = "failed"
        pre_classifier_context_handoff: Dict[str, Any] = {}
        if turn_id:
            lifecycle.start_run(
                task_id=task_id,
                turn_id=turn_id,
                conversation_id=conversation_id,
            )
            workflow_start_fn = start_turn_workflow or start_turn_workflow_best_effort
            workflow_id = workflow_start_fn(
                task_id=task_id,
                conversation_id=conversation_id or "",
                turn_id=turn_id,
                turn_sequence=turn_sequence,
                graph_name=requested_mode.value
                if isinstance(requested_mode, ExecutionMode)
                else None,
                reserved_message_id=reserved_message_id,
                metadata={"source": "start_turn_generation"},
            )

        def _mark_turn_workflow_failed_with_context(**kwargs: Any) -> None:
            """Persist the measured pre-classifier snapshot on terminal failure."""

            metadata_candidate = kwargs.get("metadata")
            failure_metadata = (
                dict(metadata_candidate)
                if isinstance(metadata_candidate, dict)
                else {}
            )
            context_window_candidate = pre_classifier_context_handoff.get(
                "context_window"
            )
            if isinstance(context_window_candidate, dict):
                failure_metadata.update(
                    service._context_window_handoff_fields(
                        dict(context_window_candidate)
                    )
                )
            kwargs["metadata"] = failure_metadata
            workflow_failed_fn(**kwargs)

        try:
            chat_inputs = ChatInputs(
                task_id=task_id,
                user_id=user_id,
                message=message,
                conversation_id=conversation_id,
                history=history,
                history_source_message_ids=history_source_message_ids or (),
                provider=provider,
                model=model,
                credential_ref=(
                    runtime_selection.get("credential_ref")
                    if isinstance(runtime_selection, dict)
                    else None
                ),
                llm_runtime_selection=runtime_selection,
                reasoning_effort=reasoning_effort,
                anchor_sequence=anchor_sequence,
                requested_mode=requested_mode,
                agent_mode=agent_mode or AgentMode.FULL_ACCESS,
                plan_mode=bool(plan_mode),
            )
            logger.info(
                "[CHAT] turn request diagnostics: role=%s model=%s effort=%s source=%s",
                ROLE_CONVERSATION_MAIN,
                model,
                reasoning_effort or DEFAULT_USER_SELECTED_REASONING_EFFORT,
                "user_selected",
            )
            facade_metadata: Dict[str, Any] = {
                "thread_config": thread_config,
                "turn_id": turn_id,
                "turn_number": turn_number,
                "turn_sequence": turn_sequence,
                "reserved_message_id": reserved_message_id,
                "deterministic_mode": deterministic_mode,
                "tenant_id": tenant_id,
            }
            result = await facade.handle_turn(
                chat_inputs,
                metadata=facade_metadata,
                runtime_services=runtime_services,
                pre_classifier_context_handoff=pre_classifier_context_handoff,
            )
            result_metadata = (
                result.metadata if isinstance(result.metadata, dict) else {}
            )
            context_window_candidate = pre_classifier_context_handoff.get(
                "context_window"
            )
            context_window_metadata: Optional[Dict[str, Any]] = (
                dict(context_window_candidate)
                if isinstance(context_window_candidate, dict)
                else None
            )
            compression_candidate = pre_classifier_context_handoff.get("compression")
            compression_metadata: Dict[str, Any] = (
                dict(compression_candidate)
                if isinstance(compression_candidate, dict)
                else {"applied": False, "reason": "below_trigger"}
            )
            pre_turn_context_event_emitted = bool(
                pre_classifier_context_handoff.get("context_event_emitted")
            )
            checkpoint_context_window_metadata = None
            if context_window_metadata is None:
                checkpoint_context_window_metadata = (
                    service._extract_and_emit_context_window_metadata(
                        task_id=task_id,
                        metadata=result_metadata,
                        fallback_conversation_id=result.conversation_id
                        or conversation_id
                        or "",
                    )
                )
            if checkpoint_context_window_metadata is not None:
                context_window_metadata = checkpoint_context_window_metadata
                checkpoint_compression = context_window_metadata.get("compression")
                if isinstance(checkpoint_compression, dict):
                    compression_metadata = dict(checkpoint_compression)
            elif (
                not pre_turn_context_event_emitted
                and context_window_metadata is not None
            ):
                service._emit_context_window_event(task_id, context_window_metadata)
            if (
                isinstance(result_metadata, dict)
                and context_window_metadata is not None
            ):
                result_metadata.update(
                    service._context_window_handoff_fields(context_window_metadata)
                )
            if result_metadata:
                result_level_compression = result_metadata.get("compression")
                if isinstance(result_level_compression, dict):
                    compression_metadata = dict(result_level_compression)
                result_metadata.setdefault("compression", dict(compression_metadata))
                result.metadata = result_metadata
            waiting_applied, reserved_message_id = (
                service._waiting_transition_service.handle_start_interruption(
                    task_id=task_id,
                    workflow_id=workflow_id,
                    turn_id=turn_id,
                    reserved_message_id=reserved_message_id,
                    result_metadata=result_metadata,
                    context_window_metadata=context_window_metadata,
                    compression_metadata=compression_metadata,
                    mark_turn_workflow_waiting=mark_turn_workflow_waiting,
                    mark_turn_workflow_waiting_best_effort=mark_turn_workflow_waiting_best_effort,
                    context_window_handoff_fields=service._context_window_handoff_fields,
                    compression_handoff_fields=service._compression_handoff_fields,
                )
            )
            if waiting_applied:
                service._turn_stream_publisher.set_streaming_inactive(
                    task_id=task_id, hub=hub
                )
                run_status = "waiting_for_human"
                return

            if result_metadata and result_metadata.get("cancelled"):
                _mark_turn_workflow_failed_with_context(
                    workflow_id=workflow_id,
                    metadata={
                        "failure_source": "initial_generation",
                        "error": "run_cancelled",
                    },
                )
                run_status = "cancelled"
                return

            final_content = service._result_service.extract_final_content(
                result=result,
                failure_message="LangGraph facade produced empty response",
            )
            resolved_conversation_id = result.conversation_id or conversation_id or ""
            (
                completion_metadata,
                stream_sequence,
                boundary_turn_sequence,
            ) = service._result_service.build_start_completion_metadata(
                result_metadata=result.metadata,
                conversation_id=resolved_conversation_id,
                anchor_sequence=anchor_sequence,
                turn_sequence=turn_sequence,
            )
            await _publish_turn_result_events_for_sequence(
                service=service,
                hub=hub,
                task_id=task_id,
                result=result,
                stream_sequence=stream_sequence,
            )
            await service._finalize_successful_turn_result(
                task_id=task_id,
                user_id=user_id,
                hub=hub,
                final_content=final_content,
                result=result,
                conversation_id=resolved_conversation_id,
                turn_id=turn_id,
                turn_sequence=boundary_turn_sequence,
                workflow_id=workflow_id,
                mark_turn_workflow_completed=mark_turn_workflow_completed,
                completion_source="initial_generation",
                context_window_metadata=context_window_metadata,
                model=model,
                emit_token_metrics=True,
                base_metadata=completion_metadata,
            )
            run_status = "completed"

        except asyncio.CancelledError:
            _mark_turn_workflow_failed_with_context(
                workflow_id=workflow_id,
                metadata={
                    "failure_source": "initial_generation",
                    "error": "run_cancelled",
                },
            )
            run_status = "cancelled"
            raise
        except CompressionRequiredError as compression_exc:
            if turn_id and lifecycle.is_cancel_requested(task_id=task_id, turn_id=turn_id):
                _mark_turn_workflow_failed_with_context(
                    workflow_id=workflow_id,
                    metadata={
                        "failure_source": "initial_generation",
                        "error": "run_cancelled",
                    },
                )
                run_status = "cancelled"
                return
            refusal_consumed = await service._failure_dispatcher.dispatch_start_compression_failure(
                compression_exc=compression_exc,
                default_error_code=compression_required_failed_error_code,
                task_id=task_id,
                hub=hub,
                workflow_id=workflow_id,
                reserved_message_id=reserved_message_id,
                generation_failed_error_message=generation_failed_error_message,
                conversation_id=conversation_id,
                turn_id=turn_id,
                turn_sequence=turn_sequence,
                mark_turn_workflow_failed=_mark_turn_workflow_failed_with_context,
                publish_boundary_completion_events=service._publish_boundary_completion_events,
            )
            if not refusal_consumed:
                try:
                    from backend.services.metrics.utils import safe_inc

                    safe_inc("langgraph_simple_chat_errors")
                except Exception:
                    pass
        except Exception as exc:
            if turn_id and lifecycle.is_cancel_requested(task_id=task_id, turn_id=turn_id):
                _mark_turn_workflow_failed_with_context(
                    workflow_id=workflow_id,
                    metadata={
                        "failure_source": "initial_generation",
                        "error": "run_cancelled",
                    },
                )
                run_status = "cancelled"
                return
            refusal_consumed = await service._failure_dispatcher.dispatch_start_exception(
                exc=exc,
                task_id=task_id,
                hub=hub,
                workflow_id=workflow_id,
                reserved_message_id=reserved_message_id,
                retryable_post_tool_error_message=retryable_post_tool_error_message,
                generation_failed_error_message=generation_failed_error_message,
                conversation_id=conversation_id,
                turn_id=turn_id,
                turn_sequence=turn_sequence,
                mark_turn_workflow_failed=_mark_turn_workflow_failed_with_context,
                publish_boundary_completion_events=service._publish_boundary_completion_events,
            )
            if not refusal_consumed:
                try:
                    from backend.services.metrics.utils import safe_inc

                    safe_inc("langgraph_simple_chat_errors")
                except Exception:
                    pass
                logger.exception("LangGraph-backed generation failed")
        finally:
            if turn_id:
                lifecycle.end_run(task_id=task_id, turn_id=turn_id, status=run_status)
            service._turn_stream_publisher.set_streaming_inactive(
                task_id=task_id, hub=hub
            )

    async def resume_turn_generation(
        self,
        *,
        service: TurnExecutionService,
        task_id: int,
        user_id: int,
        response: dict,
        graph_thread_id: Optional[str] = None,
        tenant_id: Optional[int] = None,
        runtime_placement_mode: Optional[str] = None,
        workspace_id: Optional[str] = None,
        actor_type: Optional[str] = None,
        actor_id: Optional[str] = None,
        runner_id: Optional[str] = None,
        execution_site_id: Optional[str] = None,
        graph_name: str | None = None,
        checkpoint_id: int | str | None = None,
        reserved_message_id: int | None = None,
        resume_key: str | None = None,
        workflow_id: int | None = None,
        interrupt_id: str | None = None,
        approval_received_at: float | None = None,
        facade_class: Callable[[], Any],
        resolve_interrupt_tool_call_id: Callable[..., Optional[str]],
        emit_hitl_stage_timing: Optional[Callable[..., None]] = None,
        emit_resume_worker_queue_metric: Optional[Callable[..., None]] = None,
        mark_turn_workflow_waiting: Optional[Callable[..., None]] = None,
        mark_turn_workflow_completed: Optional[Callable[..., None]] = None,
        mark_turn_workflow_failed: Optional[Callable[..., None]] = None,
        mark_interrupt_ticket_resumed: Optional[Callable[..., None]] = None,
        mark_interrupt_ticket_completed: Optional[Callable[..., None]] = None,
        mark_interrupt_ticket_failed: Optional[Callable[..., None]] = None,
        compression_persist_failed_error_code: str,
        retryable_post_tool_error_message: str,
        resume_failed_error_message: str,
    ) -> None:
        """Resume a paused turn generation from an interrupt."""
        resume_worker_start_at = time.perf_counter()
        logger.info("[CHAT-RESUME] Starting resume for task %s", task_id)
        tool_call_id = resolve_interrupt_tool_call_id(
            task_id=task_id,
            interrupt_id=interrupt_id,
        )
        emit_hitl_stage_timing_fn = (
            emit_hitl_stage_timing or emit_hitl_stage_timing_helper
        )
        emit_resume_worker_queue_metric_fn = (
            emit_resume_worker_queue_metric or emit_resume_worker_queue_metric_helper
        )
        emit_hitl_stage_timing_fn(
            stage="approval_received_at",
            timestamp=approval_received_at,
            task_id=task_id,
            interrupt_id=interrupt_id,
            tool_call_id=tool_call_id,
        )
        emit_hitl_stage_timing_fn(
            stage="resume_worker_start_at",
            timestamp=resume_worker_start_at,
            task_id=task_id,
            interrupt_id=interrupt_id,
            tool_call_id=tool_call_id,
        )
        emit_resume_worker_queue_metric_fn(
            approval_received_at=approval_received_at,
            resume_worker_start_at=resume_worker_start_at,
            task_id=task_id,
            graph_name=graph_name,
        )

        hub = None
        lifecycle = get_run_lifecycle_service()
        lifecycle_turn_id: Optional[str] = resolve_turn_id_from_workflow_best_effort(
            workflow_id
        )
        if lifecycle_turn_id is None and reserved_message_id is not None:
            lifecycle_turn_id, _ = (
                resolve_turn_identity_from_reserved_message_best_effort(
                    task_id=task_id,
                    reserved_message_id=reserved_message_id,
                )
            )
        lifecycle_status = "failed"
        workflow_failed_fn = (
            mark_turn_workflow_failed or mark_turn_workflow_failed_best_effort
        )
        interrupt_failed_fn = (
            mark_interrupt_ticket_failed or mark_interrupt_ticket_failed_best_effort
        )
        resume_should_cancel = service._build_cancel_checker(
            lifecycle,
            task_id=task_id,
            lifecycle_turn_id=lifecycle_turn_id,
        )
        resume_retry_identity = resolve_checkpoint_retry_identity_best_effort(
            workflow_id=workflow_id,
            task_id=task_id,
        )
        is_checkpoint_retry_resume = _is_checkpoint_retry_resume(resume_retry_identity)
        retry_lifecycle = (
            RetryLifecyclePublisher(
                task_id=task_id,
                retry_identity=resume_retry_identity,
                turn_id=lifecycle_turn_id,
                workflow_id=workflow_id,
                graph_name=graph_name,
                checkpoint_id=checkpoint_id,
                retry_mode=retry_mode_from_identity(resume_retry_identity),
                retry_attempt=_positive_int(
                    resume_retry_identity.get("retry_attempt")
                    if isinstance(resume_retry_identity, Mapping)
                    else None
                ),
                retry_max_attempts=_positive_int(
                    resume_retry_identity.get("retry_max_attempts")
                    if isinstance(resume_retry_identity, Mapping)
                    else None
                ),
                publish_checkpoint_rewind=publish_checkpoint_rewind_state_event,
                publish_retry_state=publish_retry_state_event,
            )
            if is_checkpoint_retry_resume
            else None
        )
        runtime_context: Optional[_ContinuationRuntimeContext] = None

        try:
            try:
                from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub

                hub = get_in_memory_stream_hub()
            except Exception:
                logger.exception("Failed to import stream hub for resume")
                service._failure_dispatcher.dispatch_resume_hub_unavailable(
                    task_id=task_id,
                    workflow_id=workflow_id,
                    interrupt_id=interrupt_id,
                    mark_turn_workflow_failed=workflow_failed_fn,
                    mark_interrupt_ticket_failed=interrupt_failed_fn,
                )
                return
            service._turn_stream_publisher.set_streaming_active(
                task_id=task_id, hub=hub
            )
            runtime_context = _build_continuation_runtime_context(
                user_id=user_id,
            )

            facade = facade_class()
            if reserved_message_id is None:
                reserved_message_id = (
                    resolve_reserved_message_id_from_workflow_best_effort(workflow_id)
                )
            if lifecycle_turn_id is None and reserved_message_id is not None:
                lifecycle_turn_id, _ = (
                    resolve_turn_identity_from_reserved_message_best_effort(
                        task_id=task_id,
                        reserved_message_id=reserved_message_id,
                    )
                )
            if lifecycle_turn_id:
                lifecycle.start_run(task_id=task_id, turn_id=lifecycle_turn_id)
            interrupt_resumed_fn = (
                mark_interrupt_ticket_resumed
                or mark_interrupt_ticket_resumed_best_effort
            )
            interrupt_resumed_fn(task_id=task_id, interrupt_id=interrupt_id)

            try:
                result = await facade.resume_from_interrupt(
                    task_id=task_id,
                    user_id=user_id,
                    graph_thread_id=graph_thread_id,
                    tenant_id=tenant_id,
                    runtime_placement_mode=runtime_placement_mode,
                    workspace_id=workspace_id,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    runner_id=runner_id,
                    execution_site_id=execution_site_id,
                    response=response,
                    graph_name=graph_name,
                    checkpoint_id=checkpoint_id,
                    reserved_message_id=reserved_message_id,
                    approval_received_at=approval_received_at,
                    resume_worker_start_at=resume_worker_start_at,
                    interrupt_id=interrupt_id,
                    should_cancel=resume_should_cancel,
                    replace_turn_events=is_checkpoint_retry_resume,
                    llm_runtime_selection=runtime_context.selection_payload,
                    runtime_services=runtime_context.runtime_services,
                )
                conversation_id = result.conversation_id or ""
                result_metadata = (
                    result.metadata if isinstance(result.metadata, dict) else {}
                )
                _apply_runtime_selection_from_result(
                    runtime_context,
                    result_metadata,
                )
                context_window_metadata = (
                    service._extract_and_emit_context_window_metadata(
                        task_id=task_id,
                        metadata=result_metadata,
                        fallback_conversation_id=conversation_id,
                    )
                )
                if result_metadata:
                    result.metadata = result_metadata
                waiting_applied, reserved_message_id = (
                    service._waiting_transition_service.handle_resume_interruption(
                        task_id=task_id,
                        workflow_id=workflow_id,
                        graph_name=graph_name,
                        checkpoint_id=checkpoint_id,
                        resume_key=resume_key,
                        reserved_message_id=reserved_message_id,
                        result_metadata=result_metadata,
                        context_window_metadata=context_window_metadata,
                        mark_turn_workflow_waiting=mark_turn_workflow_waiting,
                        mark_turn_workflow_waiting_best_effort=mark_turn_workflow_waiting_best_effort,
                        context_window_handoff_fields=service._context_window_handoff_fields,
                        compression_handoff_fields=service._compression_handoff_fields,
                    )
                )
                if waiting_applied:
                    if retry_lifecycle is not None:
                        await retry_lifecycle.publish(
                            "waiting_for_human",
                            transcript_resync_required=True,
                            turn_id=lifecycle_turn_id,
                        )
                    lifecycle_status = "waiting_for_human"
                    return

                final_content = service._result_service.extract_final_content(
                    result=result,
                    failure_message=f"Empty resume response for task {task_id}",
                )
                turn_id, turn_sequence = (
                    service._result_service.resolve_turn_identity_from_result(
                        task_id=task_id,
                        metadata=result_metadata,
                        reserved_message_id=reserved_message_id,
                        fallback_turn_id=None,
                        fallback_turn_sequence=None,
                    )
                )
                if (
                    lifecycle_turn_id is None
                    and isinstance(turn_id, str)
                    and turn_id.strip()
                ):
                    lifecycle_turn_id = turn_id.strip()

                completion_metadata, stream_sequence = (
                    service._result_service.build_completion_metadata(
                        result_metadata=result_metadata,
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        turn_sequence=turn_sequence,
                    )
                )
                await _publish_turn_result_events_for_sequence(
                    service=service,
                    hub=hub,
                    task_id=task_id,
                    result=result,
                    stream_sequence=stream_sequence,
                )

                await service._finalize_successful_turn_result(
                    task_id=task_id,
                    user_id=user_id,
                    hub=hub,
                    final_content=final_content,
                    conversation_id=conversation_id,
                    result=result,
                    turn_id=turn_id,
                    turn_sequence=turn_sequence,
                    workflow_id=workflow_id,
                    mark_turn_workflow_completed=mark_turn_workflow_completed,
                    completion_source=(
                        "checkpoint_retry_resume"
                        if is_checkpoint_retry_resume
                        else "resume_generation"
                    ),
                    context_window_metadata=context_window_metadata,
                    model=runtime_context.model,
                    base_metadata=completion_metadata,
                )
                interrupt_completed_fn = (
                    mark_interrupt_ticket_completed
                    or mark_interrupt_ticket_completed_best_effort
                )
                interrupt_completed_fn(task_id=task_id, interrupt_id=interrupt_id)
                if retry_lifecycle is not None:
                    await retry_lifecycle.publish(
                        "completed",
                        transcript_resync_required=True,
                        turn_id=turn_id or lifecycle_turn_id,
                    )
                lifecycle_status = "completed"

            except CompressionRequiredError as compression_exc:
                if lifecycle_turn_id and lifecycle.is_cancel_requested(
                    task_id=task_id, turn_id=lifecycle_turn_id
                ):
                    workflow_failed_fn(
                        workflow_id=workflow_id,
                        metadata=(
                            build_retry_terminal_metadata(
                                failure_source="checkpoint_retry_resume",
                                error="run_cancelled",
                                retry_state="cancelled",
                            )
                            if is_checkpoint_retry_resume
                            else {
                                "failure_source": "resume_generation",
                                "error": "run_cancelled",
                            }
                        ),
                    )
                    interrupt_failed_fn(task_id=task_id, interrupt_id=interrupt_id)
                    if retry_lifecycle is not None:
                        await retry_lifecycle.publish(
                            "cancelled",
                            transcript_resync_required=True,
                            failure_stage="compression",
                            turn_id=lifecycle_turn_id,
                        )
                    lifecycle_status = "cancelled"
                    return
                compression_error_code = (
                    TurnExecutionErrorService.resolve_compression_error_code(
                        compression_exc,
                        default=compression_persist_failed_error_code,
                    )
                )
                refusal_consumed = (
                    await service._failure_dispatcher.dispatch_resume_compression_failure(
                        compression_exc=compression_exc,
                        default_error_code=compression_persist_failed_error_code,
                        task_id=task_id,
                        hub=hub,
                        workflow_id=workflow_id,
                        reserved_message_id=reserved_message_id,
                        graph_name=graph_name,
                        resume_failed_error_message=resume_failed_error_message,
                        result=(result if "result" in locals() else None),
                        mark_turn_workflow_failed=workflow_failed_fn,
                        interrupt_id=interrupt_id,
                        mark_interrupt_ticket_failed=interrupt_failed_fn,
                        publish_boundary_completion_events=service._publish_boundary_completion_events,
                        failure_source=(
                            "checkpoint_retry_resume"
                            if is_checkpoint_retry_resume
                            else "resume_generation"
                        ),
                        extra_workflow_metadata=(
                            build_retry_terminal_metadata(
                                failure_source="checkpoint_retry_resume",
                                error=compression_error_code,
                                retry_state="failed",
                            )
                            if is_checkpoint_retry_resume
                            else None
                        ),
                        extra_boundary_metadata=(
                            {"retry_state": "failed"}
                            if is_checkpoint_retry_resume
                            else None
                        ),
                        resolved_error_code=compression_error_code,
                    )
                )
                if refusal_consumed:
                    lifecycle_status = "declined"
                if retry_lifecycle is not None:
                    await retry_lifecycle.publish(
                        "declined" if refusal_consumed else "failed",
                        transcript_resync_required=True,
                        failure_stage=None if refusal_consumed else "compression",
                        error_code=None if refusal_consumed else compression_error_code,
                        turn_id=lifecycle_turn_id,
                    )
            except Exception as exc:
                if lifecycle_turn_id and lifecycle.is_cancel_requested(
                    task_id=task_id, turn_id=lifecycle_turn_id
                ):
                    workflow_failed_fn(
                        workflow_id=workflow_id,
                        metadata=(
                            build_retry_terminal_metadata(
                                failure_source="checkpoint_retry_resume",
                                error="run_cancelled",
                                retry_state="cancelled",
                            )
                            if is_checkpoint_retry_resume
                            else {
                                "failure_source": "resume_generation",
                                "error": "run_cancelled",
                            }
                        ),
                    )
                    interrupt_failed_fn(task_id=task_id, interrupt_id=interrupt_id)
                    if retry_lifecycle is not None:
                        await retry_lifecycle.publish(
                            "cancelled",
                            transcript_resync_required=True,
                            failure_stage="exception",
                            turn_id=lifecycle_turn_id,
                        )
                    lifecycle_status = "cancelled"
                    return
                logger.exception("[CHAT-RESUME] Failed for task %s", task_id)
                retryable_failure = (
                    TurnExecutionErrorService.extract_retryable_post_tool_failure(exc)
                )
                resume_error_code = (
                    str(retryable_failure["error_code"])
                    if retryable_failure is not None
                    else "resume_failed"
                )
                refusal_consumed = (
                    await service._failure_dispatcher.dispatch_resume_exception(
                        exc=exc,
                        task_id=task_id,
                        hub=hub,
                        workflow_id=workflow_id,
                        reserved_message_id=reserved_message_id,
                        graph_name=graph_name,
                        retryable_post_tool_error_message=retryable_post_tool_error_message,
                        resume_failed_error_message=resume_failed_error_message,
                        result=(result if "result" in locals() else None),
                        mark_turn_workflow_failed=workflow_failed_fn,
                        interrupt_id=interrupt_id,
                        mark_interrupt_ticket_failed=interrupt_failed_fn,
                        publish_boundary_completion_events=service._publish_boundary_completion_events,
                        failure_source=(
                            "checkpoint_retry_resume"
                            if is_checkpoint_retry_resume
                            else "resume_generation"
                        ),
                        extra_workflow_metadata=(
                            build_retry_terminal_metadata(
                                failure_source="checkpoint_retry_resume",
                                error=resume_error_code,
                                retry_state="failed",
                            )
                            if is_checkpoint_retry_resume
                            else None
                        ),
                        extra_boundary_metadata=(
                            {"retry_state": "failed"}
                            if is_checkpoint_retry_resume
                            else None
                        ),
                        resolved_error_code=resume_error_code,
                        retryable_failure=retryable_failure,
                    )
                )
                if refusal_consumed:
                    lifecycle_status = "declined"
                if retry_lifecycle is not None:
                    await retry_lifecycle.publish(
                        "declined" if refusal_consumed else "failed",
                        transcript_resync_required=True,
                        failure_stage=None if refusal_consumed else "exception",
                        error_code=None if refusal_consumed else resume_error_code,
                        turn_id=lifecycle_turn_id,
                    )
        finally:
            if lifecycle_turn_id:
                lifecycle.end_run(
                    task_id=task_id, turn_id=lifecycle_turn_id, status=lifecycle_status
                )
            if hub is not None:
                service._turn_stream_publisher.set_streaming_inactive(
                    task_id=task_id, hub=hub
                )
            if runtime_context is not None:
                runtime_context.runtime_db.close()

    async def retry_turn_from_checkpoint(
        self,
        *,
        service: TurnExecutionService,
        task_id: int,
        user_id: int,
        workflow_id: int,
        graph_thread_id: Optional[str] = None,
        turn_id: Optional[str],
        turn_sequence: Optional[int],
        graph_name: str,
        facade_class: Callable[[], Any],
        tenant_id: Optional[int] = None,
        runtime_placement_mode: Optional[str] = None,
        workspace_id: Optional[str] = None,
        actor_type: Optional[str] = None,
        actor_id: Optional[str] = None,
        runner_id: Optional[str] = None,
        execution_site_id: Optional[str] = None,
        reserved_message_id: Optional[int] = None,
        checkpoint_id: Optional[int | str] = None,
        retry_attempt: Optional[int] = None,
        retry_max_attempts: Optional[int] = None,
        previous_failure: Optional[Mapping[str, Any]] = None,
        mark_turn_workflow_waiting: Optional[Callable[..., None]] = None,
        mark_turn_workflow_completed: Optional[Callable[..., None]] = None,
        mark_turn_workflow_failed: Optional[Callable[..., None]] = None,
        compression_persist_failed_error_code: str,
        retryable_post_tool_error_message: str,
        checkpoint_retry_failed_error_message: str,
    ) -> None:
        """Retry a failed turn from the latest stable checkpoint without replaying interrupt input."""
        # Build a sanitized retry context once so the facade and downstream
        # graph runtime see a consistent shape. ``previous_failure`` is
        # already sanitized by ``sanitize_previous_failure`` so we forward it
        # as-is without re-projecting.
        retry_context: Optional[Dict[str, Any]] = None
        if (
            retry_attempt is not None
            or retry_max_attempts is not None
            or previous_failure
        ):
            retry_context = {}
            if retry_attempt is not None:
                retry_context["retry_attempt"] = retry_attempt
            if retry_max_attempts is not None:
                retry_context["retry_max_attempts"] = retry_max_attempts
            if previous_failure:
                retry_context["previous_failure"] = dict(previous_failure)

        retry_identity = resolve_checkpoint_retry_identity_best_effort(
            workflow_id=workflow_id,
            task_id=task_id,
        )
        retry_lifecycle = RetryLifecyclePublisher(
            task_id=task_id,
            retry_identity=retry_identity,
            turn_id=turn_id,
            workflow_id=workflow_id,
            graph_name=graph_name,
            checkpoint_id=checkpoint_id,
            retry_mode=retry_mode_from_identity(retry_identity),
            retry_attempt=retry_attempt,
            retry_max_attempts=retry_max_attempts,
            publish_checkpoint_rewind=publish_checkpoint_rewind_state_event,
            publish_retry_state=publish_retry_state_event,
        )

        logger.info(
            "[CHECKPOINT-RETRY] Starting checkpoint retry for task %s turn_id=%s graph=%s "
            "checkpoint_id=%s retry_attempt=%s retry_max_attempts=%s",
            task_id,
            turn_id,
            graph_name,
            checkpoint_id,
            retry_attempt,
            retry_max_attempts,
        )
        hub = None
        lifecycle = get_run_lifecycle_service()
        lifecycle_turn_id = (
            turn_id.strip() if isinstance(turn_id, str) and turn_id.strip() else None
        )
        if lifecycle_turn_id is None:
            lifecycle_turn_id = resolve_turn_id_from_workflow_best_effort(workflow_id)
        if lifecycle_turn_id is None and reserved_message_id is not None:
            lifecycle_turn_id, _ = (
                resolve_turn_identity_from_reserved_message_best_effort(
                    task_id=task_id,
                    reserved_message_id=reserved_message_id,
                )
            )
        lifecycle_status = "failed"
        workflow_failed_fn = (
            mark_turn_workflow_failed or mark_turn_workflow_failed_best_effort
        )
        retry_should_cancel = service._build_cancel_checker(
            lifecycle,
            task_id=task_id,
            lifecycle_turn_id=lifecycle_turn_id,
        )
        runtime_context: Optional[_ContinuationRuntimeContext] = None

        try:
            try:
                from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub

                hub = get_in_memory_stream_hub()
            except Exception:
                logger.exception("Failed to import stream hub for checkpoint retry")
                await service._failure_dispatcher.dispatch_retry_hub_unavailable(
                    task_id=task_id,
                    workflow_id=workflow_id,
                    reserved_message_id=reserved_message_id,
                    checkpoint_retry_failed_error_message=checkpoint_retry_failed_error_message,
                    graph_name=graph_name,
                    turn_id=lifecycle_turn_id,
                    turn_sequence=turn_sequence,
                    mark_turn_workflow_failed=workflow_failed_fn,
                    publish_boundary_completion_events=service._publish_boundary_completion_events,
                    retry_attempt=retry_attempt,
                    retry_max_attempts=retry_max_attempts,
                    checkpoint_id=(
                        str(checkpoint_id)
                        if isinstance(checkpoint_id, (str, int))
                        else None
                    ),
                    retry_mode=retry_mode_from_identity(retry_identity),
                    previous_failure=previous_failure,
                )
                return

            service._turn_stream_publisher.set_streaming_active(
                task_id=task_id, hub=hub
            )
            runtime_context = _build_continuation_runtime_context(
                user_id=user_id,
            )

            if reserved_message_id is None:
                reserved_message_id = (
                    resolve_reserved_message_id_from_workflow_best_effort(workflow_id)
                )
            if lifecycle_turn_id is None and reserved_message_id is not None:
                lifecycle_turn_id, _ = (
                    resolve_turn_identity_from_reserved_message_best_effort(
                        task_id=task_id,
                        reserved_message_id=reserved_message_id,
                    )
                )
            if lifecycle_turn_id:
                lifecycle.start_run(task_id=task_id, turn_id=lifecycle_turn_id)

            await retry_lifecycle.publish("retrying", turn_id=lifecycle_turn_id)
            await retry_lifecycle.publish(
                "started",
                transcript_resync_required=True,
                turn_id=lifecycle_turn_id,
            )

            facade = facade_class()
            result = await facade.retry_from_checkpoint(
                task_id=task_id,
                user_id=user_id,
                graph_thread_id=graph_thread_id,
                tenant_id=tenant_id,
                runtime_placement_mode=runtime_placement_mode,
                workspace_id=workspace_id,
                actor_type=actor_type,
                actor_id=actor_id,
                runner_id=runner_id,
                execution_site_id=execution_site_id,
                graph_name=graph_name,
                reserved_message_id=reserved_message_id,
                should_cancel=retry_should_cancel,
                checkpoint_id=checkpoint_id,
                retry_context=retry_context,
                llm_runtime_selection=runtime_context.selection_payload,
                runtime_services=runtime_context.runtime_services,
            )
            conversation_id = result.conversation_id or ""
            result_metadata = (
                result.metadata if isinstance(result.metadata, dict) else {}
            )
            _apply_runtime_selection_from_result(
                runtime_context,
                result_metadata,
            )
            context_window_metadata = service._extract_and_emit_context_window_metadata(
                task_id=task_id,
                metadata=result_metadata,
                fallback_conversation_id=conversation_id,
            )
            if result_metadata:
                result.metadata = result_metadata

            waiting_applied, reserved_message_id = (
                service._waiting_transition_service.handle_retry_interruption(
                    task_id=task_id,
                    workflow_id=workflow_id,
                    graph_name=graph_name,
                    lifecycle_turn_id=lifecycle_turn_id,
                    reserved_message_id=reserved_message_id,
                    result_metadata=result_metadata,
                    context_window_metadata=context_window_metadata,
                    mark_turn_workflow_waiting=mark_turn_workflow_waiting,
                    mark_turn_workflow_waiting_best_effort=mark_turn_workflow_waiting_best_effort,
                    context_window_handoff_fields=service._context_window_handoff_fields,
                    compression_handoff_fields=service._compression_handoff_fields,
                )
            )
            if waiting_applied:
                await retry_lifecycle.publish(
                    "waiting_for_human",
                    transcript_resync_required=True,
                    turn_id=lifecycle_turn_id,
                )
                lifecycle_status = "waiting_for_human"
                return

            final_content = service._result_service.extract_final_content(
                result=result,
                failure_message=f"Empty checkpoint retry response for task {task_id}",
            )
            result_turn_id, result_turn_sequence = (
                service._result_service.resolve_turn_identity_from_result(
                    task_id=task_id,
                    metadata=result_metadata,
                    reserved_message_id=reserved_message_id,
                    fallback_turn_id=turn_id,
                    fallback_turn_sequence=turn_sequence,
                )
            )
            if (
                lifecycle_turn_id is None
                and isinstance(result_turn_id, str)
                and result_turn_id.strip()
            ):
                lifecycle_turn_id = result_turn_id.strip()

            completion_metadata, stream_sequence = (
                service._result_service.build_completion_metadata(
                    result_metadata=result_metadata,
                    conversation_id=conversation_id,
                    turn_id=result_turn_id,
                    turn_sequence=result_turn_sequence,
                )
            )
            await _publish_turn_result_events_for_sequence(
                service=service,
                hub=hub,
                task_id=task_id,
                result=result,
                stream_sequence=stream_sequence,
            )

            await service._finalize_successful_turn_result(
                task_id=task_id,
                user_id=user_id,
                hub=hub,
                final_content=final_content,
                result=result,
                conversation_id=conversation_id,
                turn_id=result_turn_id,
                turn_sequence=result_turn_sequence,
                workflow_id=workflow_id,
                mark_turn_workflow_completed=mark_turn_workflow_completed,
                completion_source="checkpoint_retry",
                context_window_metadata=context_window_metadata,
                model=runtime_context.model,
                base_metadata=completion_metadata,
            )
            await retry_lifecycle.publish(
                "completed",
                transcript_resync_required=True,
                turn_id=result_turn_id or lifecycle_turn_id,
            )
            lifecycle_status = "completed"

        except CompressionRequiredError as compression_exc:
            if lifecycle_turn_id and lifecycle.is_cancel_requested(
                task_id=task_id, turn_id=lifecycle_turn_id
            ):
                workflow_failed_fn(
                    workflow_id=workflow_id,
                    metadata=build_retry_terminal_metadata(
                        failure_source="checkpoint_retry",
                        error="run_cancelled",
                        retry_state="cancelled",
                    ),
                )
                await retry_lifecycle.publish(
                    "cancelled",
                    transcript_resync_required=True,
                    failure_stage="compression",
                    turn_id=lifecycle_turn_id,
                )
                lifecycle_status = "cancelled"
                return
            compression_error_code = (
                TurnExecutionErrorService.resolve_compression_error_code(
                    compression_exc,
                    default=compression_persist_failed_error_code,
                )
            )
            refusal_consumed = (
                await service._failure_dispatcher.dispatch_retry_compression_failure(
                    compression_exc=compression_exc,
                    default_error_code=compression_persist_failed_error_code,
                    task_id=task_id,
                    hub=hub,
                    workflow_id=workflow_id,
                    reserved_message_id=reserved_message_id,
                    checkpoint_retry_failed_error_message=checkpoint_retry_failed_error_message,
                    graph_name=graph_name,
                    turn_id=lifecycle_turn_id,
                    turn_sequence=turn_sequence,
                    mark_turn_workflow_failed=workflow_failed_fn,
                    publish_boundary_completion_events=service._publish_boundary_completion_events,
                    retry_attempt=retry_attempt,
                    retry_max_attempts=retry_max_attempts,
                    checkpoint_id=(
                        str(checkpoint_id)
                        if isinstance(checkpoint_id, (str, int))
                        else None
                    ),
                    retry_mode=retry_mode_from_identity(retry_identity),
                    previous_failure=previous_failure,
                    resolved_error_code=compression_error_code,
                )
            )
            if refusal_consumed:
                lifecycle_status = "declined"
            await retry_lifecycle.publish(
                "declined" if refusal_consumed else "failed",
                transcript_resync_required=True,
                failure_stage=None if refusal_consumed else "compression",
                error_code=None if refusal_consumed else compression_error_code,
                turn_id=lifecycle_turn_id,
            )
        except Exception as exc:
            if lifecycle_turn_id and lifecycle.is_cancel_requested(
                task_id=task_id, turn_id=lifecycle_turn_id
            ):
                workflow_failed_fn(
                    workflow_id=workflow_id,
                    metadata=build_retry_terminal_metadata(
                        failure_source="checkpoint_retry",
                        error="run_cancelled",
                        retry_state="cancelled",
                    ),
                )
                await retry_lifecycle.publish(
                    "cancelled",
                    transcript_resync_required=True,
                    failure_stage="exception",
                    turn_id=lifecycle_turn_id,
                )
                lifecycle_status = "cancelled"
                return
            logger.exception("[CHECKPOINT-RETRY] Failed for task %s", task_id)
            retryable_failure = (
                TurnExecutionErrorService.extract_retryable_post_tool_failure(exc)
            )
            error_code = (
                str(retryable_failure["error_code"])
                if retryable_failure is not None
                else "checkpoint_retry_failed"
            )
            refusal_consumed = (
                await service._failure_dispatcher.dispatch_retry_exception(
                    exc=exc,
                    task_id=task_id,
                    hub=hub,
                    workflow_id=workflow_id,
                    reserved_message_id=reserved_message_id,
                    graph_name=graph_name,
                    retryable_post_tool_error_message=retryable_post_tool_error_message,
                    checkpoint_retry_failed_error_message=checkpoint_retry_failed_error_message,
                    turn_id=lifecycle_turn_id,
                    turn_sequence=turn_sequence,
                    mark_turn_workflow_failed=workflow_failed_fn,
                    publish_boundary_completion_events=service._publish_boundary_completion_events,
                    retry_attempt=retry_attempt,
                    retry_max_attempts=retry_max_attempts,
                    checkpoint_id=(
                        str(checkpoint_id)
                        if isinstance(checkpoint_id, (str, int))
                        else None
                    ),
                    retry_mode=retry_mode_from_identity(retry_identity),
                    previous_failure=previous_failure,
                    resolved_error_code=error_code,
                    retryable_failure=retryable_failure,
                )
            )
            if refusal_consumed:
                lifecycle_status = "declined"
            await retry_lifecycle.publish(
                "declined" if refusal_consumed else "failed",
                transcript_resync_required=True,
                failure_stage=None if refusal_consumed else "exception",
                error_code=None if refusal_consumed else error_code,
                turn_id=lifecycle_turn_id,
            )
        finally:
            if lifecycle_turn_id:
                lifecycle.end_run(
                    task_id=task_id, turn_id=lifecycle_turn_id, status=lifecycle_status
                )
            if hub is not None:
                service._turn_stream_publisher.set_streaming_inactive(
                    task_id=task_id, hub=hub
                )
            if runtime_context is not None:
                runtime_context.runtime_db.close()
