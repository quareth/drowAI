"""Public compatibility facade for turn execution orchestration.

This module preserves the stable public API consumed by routers/workers while
delegating flow control to ``turn_execution.orchestrator`` and focused
internal services.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional

from backend.database import SessionLocal
from backend.services.chat.event_builders import (
    attach_conversation_ids,
)
from backend.services.langgraph_chat import AgentMode, ExecutionMode, LangGraphChatFacade
from backend.services.langgraph_chat.facade_helpers import (
    emit_hitl_stage_timing as emit_hitl_stage_timing_helper,
    emit_resume_worker_queue_metric as emit_resume_worker_queue_metric_helper,
)
from backend.services.langgraph_chat.compression.context_service import ContextCompressionService
from backend.services.langgraph_chat.compression.snapshot_repository import (
    CompressionSnapshotRepository,
)
from backend.services.langgraph_chat.compression.turn_service import TurnCompressionService
from backend.services.langgraph_chat.compression.window_manager import ContextWindowManager
from backend.services.langgraph_chat.execution.error_service import TurnExecutionErrorService
from backend.services.langgraph_chat.execution.orchestration.bootstrap_service import (
    TurnExecutionBootstrapService,
)
from backend.services.langgraph_chat.execution.orchestration.cancel_checker import (
    build_cancel_checker,
)
from backend.services.langgraph_chat.execution.orchestration.failure_dispatcher import (
    TurnExecutionFailureDispatcher,
)
from backend.services.langgraph_chat.execution.orchestration.orchestrator import (
    TurnExecutionOrchestrator,
)
from backend.services.langgraph_chat.execution.orchestration.result_service import (
    TurnExecutionResultService,
)
from backend.services.langgraph_chat.execution.orchestration.waiting_transition_service import (
    TurnExecutionWaitingTransitionService,
)
from backend.services.langgraph_chat.checkpoint.interrupt_ticket_service import (
    mark_interrupt_ticket_completed_best_effort,
    mark_interrupt_ticket_failed_best_effort,
    mark_interrupt_ticket_resumed_best_effort,
    resolve_interrupt_tool_call_id_best_effort,
)
from backend.services.langgraph_chat.streaming.status_events import emit_context_window_event
from backend.services.langgraph_chat.checkpoint.turn_workflow_service import (
    mark_turn_workflow_completed_best_effort,
    mark_turn_workflow_failed_best_effort,
    mark_turn_workflow_waiting_best_effort,
    start_turn_workflow_best_effort,
)
from backend.services.langgraph_chat.streaming.publisher import TurnStreamPublisher
from backend.services.langgraph_chat.runtime.usage_middleware import record_usage_list_best_effort
from backend.services.llm_provider.runtime_config_service import LLMRuntimeConfigService
from backend.services.llm_provider.types import LLMRuntimeSelection

_COMPRESSION_REQUIRED_FAILED = "compression_required_failed"
_COMPRESSION_PERSIST_FAILED = "compression_persist_failed"
_RETRYABLE_POST_TOOL_ERROR_MESSAGE = (
    "[Error] A structured response failed validation. Retry to continue from the latest checkpoint."
)
_GENERATION_FAILED_ERROR_MESSAGE = "[Error] Failed to generate response."
_RESUME_FAILED_ERROR_MESSAGE = "[Error] Failed to complete tool execution."
_CHECKPOINT_RETRY_FAILED_ERROR_MESSAGE = "[Error] Failed to continue from the latest checkpoint."


class TurnExecutionService:
    """Application service that executes start/resume orchestration loops."""

    def __init__(
        self,
        error_service: Optional[TurnExecutionErrorService] = None,
        turn_compression_service: Optional[TurnCompressionService] = None,
        turn_stream_publisher: Optional[TurnStreamPublisher] = None,
        bootstrap_service: Optional[TurnExecutionBootstrapService] = None,
        waiting_transition_service: Optional[TurnExecutionWaitingTransitionService] = None,
        result_service: Optional[TurnExecutionResultService] = None,
        failure_dispatcher: Optional[TurnExecutionFailureDispatcher] = None,
        orchestrator: Optional[TurnExecutionOrchestrator] = None,
    ) -> None:
        self._error_service = error_service or TurnExecutionErrorService()
        self._turn_compression_service = (
            turn_compression_service
            or TurnCompressionService(
                context_window_manager_factory=lambda max_tokens: ContextWindowManager(max_tokens=max_tokens)
                if max_tokens is not None
                else ContextWindowManager(),
                context_compression_service_factory=lambda: ContextCompressionService(),
                compression_snapshot_repository_factory=CompressionSnapshotRepository,
                session_factory=SessionLocal,
            )
        )
        self._turn_stream_publisher = turn_stream_publisher or TurnStreamPublisher()
        self._bootstrap_service = bootstrap_service or TurnExecutionBootstrapService()
        self._waiting_transition_service = (
            waiting_transition_service or TurnExecutionWaitingTransitionService()
        )
        self._result_service = result_service or TurnExecutionResultService()
        self._failure_dispatcher = failure_dispatcher or TurnExecutionFailureDispatcher(
            error_service=self._error_service
        )
        self._orchestrator = orchestrator or TurnExecutionOrchestrator()

    def _extract_context_window_metadata(
        self,
        *,
        metadata: Any,
        fallback_conversation_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Normalize context-window metadata from result payloads."""
        return self._turn_compression_service.extract_context_window_metadata(
            metadata=metadata,
            fallback_conversation_id=fallback_conversation_id,
        )

    def _context_window_handoff_fields(
        self,
        metadata: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Return normalized handoff fields for workflow metadata."""
        return self._turn_compression_service.context_window_handoff_fields(metadata)

    def _compression_handoff_fields(self, metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Return normalized compression metadata for workflow audit records."""
        return self._turn_compression_service.compression_handoff_fields(metadata)

    def _emit_context_window_event(
        self,
        task_id: int,
        metadata: Optional[Dict[str, Any]] = None,
        **event_kwargs: Any,
    ) -> None:
        """Emit additive context-window status for both metadata and flattened callback forms."""
        if metadata is not None:
            self._turn_compression_service.emit_context_window_event(
                task_id=task_id,
                metadata=metadata,
                emit_context_window_event=emit_context_window_event,
            )
            return
        if event_kwargs:
            emit_context_window_event(task_id=task_id, **event_kwargs)

    def _extract_and_emit_context_window_metadata(
        self,
        *,
        task_id: int,
        metadata: Any,
        fallback_conversation_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Normalize context metadata, emit event, and inject handoff metadata."""
        return self._turn_compression_service.extract_and_emit_context_window_metadata(
            task_id=task_id,
            metadata=metadata,
            fallback_conversation_id=fallback_conversation_id,
            emit_context_window_event=self._emit_context_window_event,
        )

    @staticmethod
    def _build_cancel_checker(
        lifecycle: Any,
        *,
        task_id: int,
        lifecycle_turn_id: Optional[str],
        throttle_seconds: float = 0.25,
    ) -> Callable[[], bool]:
        """Create a throttled cancel-checked callback for resume/retry loops."""
        return build_cancel_checker(
            lifecycle,
            task_id=task_id,
            lifecycle_turn_id=lifecycle_turn_id,
            throttle_seconds=throttle_seconds,
        )

    async def _finalize_successful_turn_result(
        self,
        *,
        task_id: int,
        user_id: int,
        hub: Any,
        final_content: str,
        result: Any,
        conversation_id: Optional[str],
        turn_id: Optional[str],
        turn_sequence: Optional[int],
        workflow_id: Optional[int],
        mark_turn_workflow_completed: Optional[Callable[..., None]],
        completion_source: str,
        context_window_metadata: Optional[Dict[str, Any]],
        model: Optional[str] = None,
        emit_token_metrics: bool = False,
        base_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Publish the success boundary and record turn completion."""
        resolved_conversation_id = conversation_id or ""
        result_metadata = result.metadata if isinstance(result.metadata, dict) else {}

        completion_metadata = (
            dict(base_metadata)
            if isinstance(base_metadata, dict)
            else attach_conversation_ids(
                dict(result_metadata),
                resolved_conversation_id,
            )
        )
        if isinstance(turn_id, str) and turn_id.strip():
            completion_metadata.setdefault("id", turn_id.strip())
        completion_metadata.setdefault("role", "assistant")
        completion_metadata.setdefault("streaming", False)
        if turn_sequence is not None:
            completion_metadata.setdefault("turn_sequence", turn_sequence)

        await self._publish_boundary_completion_events(
            task_id=task_id,
            hub=hub,
            content=final_content,
            conversation_id=resolved_conversation_id,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            base_metadata=completion_metadata,
        )

        if emit_token_metrics:
            try:
                from backend.services.metrics.utils import safe_inc as safe_inc_metric

                safe_inc_metric("final_messages_persisted")
                safe_inc_metric("final_message_chars", len(final_content))
            except Exception:
                pass

        record_usage_list_best_effort(
            task_id=task_id,
            user_id=user_id,
            usage_list=result.usage,
            source="langgraph",
            conversation_id=resolved_conversation_id,
            model=model,
        )
        workflow_completed_fn = mark_turn_workflow_completed or mark_turn_workflow_completed_best_effort
        completed_metadata: Dict[str, Any] = {"completion_source": completion_source}
        completed_metadata.update(self._context_window_handoff_fields(context_window_metadata))
        completed_metadata.update(self._compression_handoff_fields(result_metadata))
        # Phase 4.3: when the completion finalizes a checkpoint retry, clear
        # the in-flight ``active_retry`` block on the workflow row and stamp
        # ``retry_state=completed`` so transcript bootstrap derives the
        # post-retry state from a single workflow read.
        if completion_source in {"checkpoint_retry", "checkpoint_retry_resume"}:
            completed_metadata["active_retry"] = None
            completed_metadata["retry_state"] = "completed"
        workflow_completed_fn(
            workflow_id=workflow_id,
            metadata=completed_metadata,
        )

    async def start_turn_generation(
        self,
        *,
        task_id: int,
        user_id: int,
        tenant_id: Optional[int] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        runtime_selection: Optional[Mapping[str, Any]] = None,
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
        reserve_chat_turn: Optional[Callable[..., tuple[int, int, str, int]]] = None,
        start_turn_workflow: Optional[Callable[..., Optional[int]]] = None,
        mark_turn_workflow_waiting: Optional[Callable[..., None]] = None,
        mark_turn_workflow_completed: Optional[Callable[..., None]] = None,
        mark_turn_workflow_failed: Optional[Callable[..., None]] = None,
    ) -> None:
        """Start a new turn generation and stream boundary events."""
        runtime_db = None
        runtime_selection_payload: Optional[Dict[str, Any]] = None
        runtime_services: Any = None
        resolved_provider = provider
        resolved_model = model

        runtime_db = SessionLocal()
        try:
            runtime_config_service = LLMRuntimeConfigService(runtime_db)
            if runtime_selection is not None:
                runtime_selection_value = LLMRuntimeSelection.from_mapping(
                    dict(runtime_selection)
                )
            else:
                runtime_selection_value = runtime_config_service.build_runtime_selection(
                    user_id=user_id,
                    provider=provider,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    require_enabled_credential=not deterministic_mode,
                )
            runtime_selection_payload = runtime_selection_value.to_dict()
            runtime_services = runtime_config_service.build_runtime_services()
            resolved_provider = runtime_selection_value.provider
            resolved_model = runtime_selection_value.model
            # Deprecated compatibility parameter: raw secrets must not enter
            # graph/runtime state. Provider-neutral credentials are the
            # runtime authority after Execution Plane migration.
            _ = api_key
            if not resolved_model:
                raise ValueError("model is required for turn generation")

            start_turn_workflow_fn = start_turn_workflow or start_turn_workflow_best_effort
            mark_turn_workflow_waiting_fn = mark_turn_workflow_waiting or mark_turn_workflow_waiting_best_effort
            mark_turn_workflow_completed_fn = mark_turn_workflow_completed or mark_turn_workflow_completed_best_effort
            mark_turn_workflow_failed_fn = mark_turn_workflow_failed or mark_turn_workflow_failed_best_effort
            await self._orchestrator.start_turn_generation(
                service=self,
                task_id=task_id,
                user_id=user_id,
                tenant_id=tenant_id,
                provider=resolved_provider,
                model=resolved_model,
                runtime_selection=runtime_selection_payload,
                runtime_services=runtime_services,
                message=message,
                conversation_id=conversation_id,
                history=history,
                history_source_message_ids=history_source_message_ids,
                anchor_sequence=anchor_sequence,
                requested_mode=requested_mode,
                agent_mode=agent_mode,
                plan_mode=plan_mode,
                turn_id=turn_id,
                turn_number=turn_number,
                reserved_message_id=reserved_message_id,
                reasoning_effort=reasoning_effort,
                deterministic_mode=deterministic_mode,
                facade_class=lambda: LangGraphChatFacade(
                    turn_compression_service=self._turn_compression_service
                ),
                reserve_chat_turn=reserve_chat_turn,
                start_turn_workflow=start_turn_workflow_fn,
                mark_turn_workflow_waiting=mark_turn_workflow_waiting_fn,
                mark_turn_workflow_completed=mark_turn_workflow_completed_fn,
                mark_turn_workflow_failed=mark_turn_workflow_failed_fn,
                compression_required_failed_error_code=_COMPRESSION_REQUIRED_FAILED,
                retryable_post_tool_error_message=_RETRYABLE_POST_TOOL_ERROR_MESSAGE,
                generation_failed_error_message=_GENERATION_FAILED_ERROR_MESSAGE,
            )
        finally:
            if runtime_db is not None:
                runtime_db.close()

    async def resume_turn_generation(
        self,
        *,
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
        emit_hitl_stage_timing: Optional[Callable[..., None]] = None,
        emit_resume_worker_queue_metric: Optional[Callable[..., None]] = None,
        mark_turn_workflow_waiting: Optional[Callable[..., None]] = None,
        mark_turn_workflow_completed: Optional[Callable[..., None]] = None,
        mark_turn_workflow_failed: Optional[Callable[..., None]] = None,
        mark_interrupt_ticket_resumed: Optional[Callable[..., None]] = None,
        mark_interrupt_ticket_completed: Optional[Callable[..., None]] = None,
        mark_interrupt_ticket_failed: Optional[Callable[..., None]] = None,
    ) -> None:
        """Resume a paused turn generation from an interrupt."""
        emit_hitl_stage_timing_fn = emit_hitl_stage_timing or emit_hitl_stage_timing_helper
        emit_resume_worker_queue_metric_fn = (
            emit_resume_worker_queue_metric or emit_resume_worker_queue_metric_helper
        )
        mark_turn_workflow_waiting_fn = mark_turn_workflow_waiting or mark_turn_workflow_waiting_best_effort
        mark_turn_workflow_completed_fn = mark_turn_workflow_completed or mark_turn_workflow_completed_best_effort
        mark_turn_workflow_failed_fn = mark_turn_workflow_failed or mark_turn_workflow_failed_best_effort
        mark_interrupt_ticket_resumed_fn = (
            mark_interrupt_ticket_resumed or mark_interrupt_ticket_resumed_best_effort
        )
        mark_interrupt_ticket_completed_fn = (
            mark_interrupt_ticket_completed or mark_interrupt_ticket_completed_best_effort
        )
        mark_interrupt_ticket_failed_fn = (
            mark_interrupt_ticket_failed or mark_interrupt_ticket_failed_best_effort
        )
        await self._orchestrator.resume_turn_generation(
            service=self,
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
            resume_key=resume_key,
            workflow_id=workflow_id,
            interrupt_id=interrupt_id,
            approval_received_at=approval_received_at,
            facade_class=LangGraphChatFacade,
            resolve_interrupt_tool_call_id=resolve_interrupt_tool_call_id_best_effort,
            emit_hitl_stage_timing=emit_hitl_stage_timing_fn,
            emit_resume_worker_queue_metric=emit_resume_worker_queue_metric_fn,
            mark_turn_workflow_waiting=mark_turn_workflow_waiting_fn,
            mark_turn_workflow_completed=mark_turn_workflow_completed_fn,
            mark_turn_workflow_failed=mark_turn_workflow_failed_fn,
            mark_interrupt_ticket_resumed=mark_interrupt_ticket_resumed_fn,
            mark_interrupt_ticket_completed=mark_interrupt_ticket_completed_fn,
            mark_interrupt_ticket_failed=mark_interrupt_ticket_failed_fn,
            compression_persist_failed_error_code=_COMPRESSION_PERSIST_FAILED,
            retryable_post_tool_error_message=_RETRYABLE_POST_TOOL_ERROR_MESSAGE,
            resume_failed_error_message=_RESUME_FAILED_ERROR_MESSAGE,
        )

    async def retry_turn_from_checkpoint(
        self,
        *,
        task_id: int,
        user_id: int,
        workflow_id: int,
        graph_thread_id: Optional[str] = None,
        turn_id: Optional[str],
        turn_sequence: Optional[int],
        graph_name: str,
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
    ) -> None:
        """Retry a failed turn from the latest stable checkpoint without replaying interrupt input.

        ``checkpoint_id`` pins the stored workflow checkpoint so the worker
        does not implicitly fall back to "latest". ``retry_attempt`` /
        ``retry_max_attempts`` and the sanitized ``previous_failure`` mapping
        carry the canonical retry identity built by Phase 1 so downstream
        graph runtime can choose a corrected/alternate path instead of blindly
        replaying the same failing step. All four are optional so the legacy
        non-checkpoint retry path keeps working when the carrier is absent.
        """
        mark_turn_workflow_waiting_fn = mark_turn_workflow_waiting or mark_turn_workflow_waiting_best_effort
        mark_turn_workflow_completed_fn = mark_turn_workflow_completed or mark_turn_workflow_completed_best_effort
        mark_turn_workflow_failed_fn = mark_turn_workflow_failed or mark_turn_workflow_failed_best_effort
        await self._orchestrator.retry_turn_from_checkpoint(
            service=self,
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
            workflow_id=workflow_id,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            graph_name=graph_name,
            facade_class=LangGraphChatFacade,
            reserved_message_id=reserved_message_id,
            checkpoint_id=checkpoint_id,
            retry_attempt=retry_attempt,
            retry_max_attempts=retry_max_attempts,
            previous_failure=previous_failure,
            mark_turn_workflow_waiting=mark_turn_workflow_waiting_fn,
            mark_turn_workflow_completed=mark_turn_workflow_completed_fn,
            mark_turn_workflow_failed=mark_turn_workflow_failed_fn,
            compression_persist_failed_error_code=_COMPRESSION_PERSIST_FAILED,
            retryable_post_tool_error_message=_RETRYABLE_POST_TOOL_ERROR_MESSAGE,
            checkpoint_retry_failed_error_message=_CHECKPOINT_RETRY_FAILED_ERROR_MESSAGE,
        )

    async def _publish_boundary_completion_events(
        self,
        *,
        task_id: int,
        hub: Any,
        content: str,
        conversation_id: str,
        turn_id: Optional[str],
        turn_sequence: Optional[int],
        base_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        await self._turn_stream_publisher.publish_boundary_completion_events(
            task_id=task_id,
            hub=hub,
            content=content,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            base_metadata=base_metadata,
        )

_SHARED_TURN_EXECUTION_SERVICE = TurnExecutionService()


def get_turn_execution_service() -> TurnExecutionService:
    """Return shared turn execution orchestration service."""
    return _SHARED_TURN_EXECUTION_SERVICE


async def run_langgraph_generation(
    task_id: int,
    user_id: int,
    tenant_id: int | None = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    message: str = "",
    conversation_id: Optional[str] = None,
    history: Optional[List[Dict[str, Any]]] = None,
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
    provider: Optional[str] = None,
    runtime_selection: Optional[Mapping[str, Any]] = None,
) -> None:
    """Compatibility helper for background generation entrypoints."""
    await get_turn_execution_service().start_turn_generation(
        task_id=task_id,
        user_id=user_id,
        tenant_id=tenant_id,
        api_key=api_key,
        model=model,
        provider=provider,
        runtime_selection=runtime_selection,
        reasoning_effort=reasoning_effort,
        message=message,
        conversation_id=conversation_id,
        history=history or [],
        history_source_message_ids=history_source_message_ids,
        anchor_sequence=anchor_sequence,
        requested_mode=requested_mode,
        agent_mode=agent_mode,
        plan_mode=plan_mode,
        turn_id=turn_id,
        turn_number=turn_number,
        reserved_message_id=reserved_message_id,
        deterministic_mode=deterministic_mode,
    )


async def run_resume_generation(
    task_id: int,
    user_id: int,
    response: dict,
    graph_thread_id: str | None = None,
    tenant_id: int | None = None,
    runtime_placement_mode: str | None = None,
    workspace_id: str | None = None,
    actor_type: str | None = None,
    actor_id: str | None = None,
    runner_id: str | None = None,
    execution_site_id: str | None = None,
    graph_name: str | None = None,
    checkpoint_id: int | str | None = None,
    reserved_message_id: int | None = None,
    resume_key: str | None = None,
    workflow_id: int | None = None,
    interrupt_id: str | None = None,
    approval_received_at: float | None = None,
) -> None:
    """Compatibility helper used by task resume routers."""
    await get_turn_execution_service().resume_turn_generation(
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
        resume_key=resume_key,
        workflow_id=workflow_id,
        interrupt_id=interrupt_id,
        approval_received_at=approval_received_at,
        emit_hitl_stage_timing=emit_hitl_stage_timing_helper,
        emit_resume_worker_queue_metric=emit_resume_worker_queue_metric_helper,
    )


async def run_checkpoint_retry_generation(
    task_id: int,
    user_id: int,
    workflow_id: int,
    graph_thread_id: str | None,
    turn_id: str | None,
    turn_sequence: int | None,
    graph_name: str,
    tenant_id: int | None = None,
    runtime_placement_mode: str | None = None,
    workspace_id: str | None = None,
    actor_type: str | None = None,
    actor_id: str | None = None,
    runner_id: str | None = None,
    execution_site_id: str | None = None,
    reserved_message_id: int | None = None,
    checkpoint_id: int | str | None = None,
    retry_attempt: int | None = None,
    retry_max_attempts: int | None = None,
    previous_failure: Mapping[str, Any] | None = None,
) -> None:
    """Compatibility helper for checkpoint retry background workers.

    Phase 2.1 makes the canonical retry carrier explicit on the worker
    entrypoint: ``checkpoint_id`` pins the stored workflow checkpoint,
    ``retry_attempt`` / ``retry_max_attempts`` carry the backend-owned
    retry identity, and ``previous_failure`` is the sanitized projection
    built in Phase 1.1. All four default to ``None`` so the legacy
    non-checkpoint retry path keeps the same behavior when the carrier
    is absent.
    """
    await get_turn_execution_service().retry_turn_from_checkpoint(
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
        workflow_id=workflow_id,
        turn_id=turn_id,
        turn_sequence=turn_sequence,
        graph_name=graph_name,
        reserved_message_id=reserved_message_id,
        checkpoint_id=checkpoint_id,
        retry_attempt=retry_attempt,
        retry_max_attempts=retry_max_attempts,
        previous_failure=previous_failure,
    )
