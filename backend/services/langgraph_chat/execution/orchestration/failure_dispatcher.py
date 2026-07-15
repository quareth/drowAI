"""Failure mapping and terminal dispatch helpers for turn execution flows.

This module centralizes flow-specific failure mapping and dispatch argument
construction while keeping ``TurnExecutionErrorService`` as the side-effect
authority for workflow/interrupt updates and boundary publication.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

from backend.services.langgraph_chat.compression.context_models import CompressionRequiredError
from backend.services.langgraph_chat.exceptions import PlanModeUnavailableError
from backend.services.langgraph_chat.execution.error_service import TurnExecutionErrorService
from backend.services.langgraph_chat.execution.refusal_service import (
    TurnExecutionRefusalService,
)

logger = logging.getLogger(__name__)

PublishBoundaryCompletionEvents = Callable[..., Awaitable[None]]

_LLM_TIMEOUT_ERROR_CODE = "llm_timeout"
_LLM_TIMEOUT_ERROR_MESSAGE = (
    "The request is taking too much time to generate a response."
)


# Canonical sanitized retry-failure metadata keys persisted on the failed
# workflow row. Anything outside this projection (raw provider payloads,
# headers, cookies, JWTs, API keys) is intentionally not included.
_RETRY_FAILURE_DIAGNOSTIC_KEYS: tuple[str, ...] = (
    "retry_state",
    "failure_stage",
    "retry_mode",
    "retry_attempt",
    "retry_max_attempts",
    "checkpoint_id",
    "graph_name",
    "workflow_id",
    "previous_failure",
    "retry_exhausted",
    "another_retry_allowed",
    "active_retry",
)

_RETRY_FAILURE_BOUNDARY_KEYS: tuple[str, ...] = (
    "retry_state",
    "failure_stage",
    "retry_mode",
    "retry_attempt",
    "retry_max_attempts",
    "checkpoint_id",
    "graph_name",
    "workflow_id",
    "retry_exhausted",
    "another_retry_allowed",
)


def _build_retry_failure_diagnostics(
    *,
    failure_stage: str,
    retry_mode: Optional[str],
    retry_attempt: Optional[int],
    retry_max_attempts: Optional[int],
    checkpoint_id: Optional[str],
    graph_name: Optional[str],
    workflow_id: Optional[int],
    previous_failure: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Project retry-failure context onto the sanitized whitelist.

    The output is the ``extra_workflow_metadata`` payload merged into the
    failed workflow row alongside ``failure_source="checkpoint_retry"``.
    ``previous_failure`` is forwarded only when already sanitized upstream
    (the worker carrier calls ``sanitize_previous_failure``); no raw provider
    payload is reconstructed here.
    """
    # Clear the in-flight ``active_retry`` block on terminal retry failures so
    # transcript bootstrap derives the post-failure overlay from one workflow
    # row read without scanning stream events.
    diagnostics: Dict[str, Any] = {
        "retry_state": "failed",
        "failure_stage": failure_stage,
        "active_retry": None,
    }
    if retry_mode:
        diagnostics["retry_mode"] = retry_mode
    if isinstance(retry_attempt, int):
        diagnostics["retry_attempt"] = retry_attempt
    if isinstance(retry_max_attempts, int):
        diagnostics["retry_max_attempts"] = retry_max_attempts
        # Compute whether another retry is allowed without leaking budget
        # internals — the frontend can read this directly.
        if isinstance(retry_attempt, int):
            diagnostics["retry_exhausted"] = retry_attempt >= retry_max_attempts
            diagnostics["another_retry_allowed"] = retry_attempt < retry_max_attempts
    if checkpoint_id:
        diagnostics["checkpoint_id"] = str(checkpoint_id)
    if graph_name:
        diagnostics["graph_name"] = graph_name
    if isinstance(workflow_id, int):
        diagnostics["workflow_id"] = workflow_id
    if isinstance(previous_failure, Mapping):
        # Re-project through the canonical sanitized whitelist as a defense
        # in depth — any caller that forgot to sanitize upstream has its
        # extras dropped here before the value lands on the workflow row.
        from backend.services.langgraph_chat.checkpoint.turn_workflow_service import (
            sanitize_previous_failure,
        )

        sanitized = sanitize_previous_failure(dict(previous_failure))
        if sanitized:
            diagnostics["previous_failure"] = sanitized
    return diagnostics


def _retry_allowed_after_failure(
    retry_diagnostics: Mapping[str, Any],
    *,
    default: bool,
) -> bool:
    """Return whether terminal retry-failure metadata may still advertise Retry."""
    if retry_diagnostics.get("another_retry_allowed") is True:
        return default
    if retry_diagnostics.get("another_retry_allowed") is False:
        return False
    if retry_diagnostics.get("retry_exhausted") is True:
        return False
    # A checkpoint retry that failed without budget diagnostics should fail
    # closed. The next POST would be rejected by the claim path anyway; do not
    # advertise a CTA from an incomplete terminal payload.
    return False


def _build_retry_failure_boundary_metadata(
    retry_diagnostics: Mapping[str, Any],
    *,
    retryable: bool,
) -> Dict[str, Any]:
    """Project retry failure diagnostics onto the streamed assistant boundary."""
    metadata: Dict[str, Any] = {
        key: retry_diagnostics[key]
        for key in _RETRY_FAILURE_BOUNDARY_KEYS
        if key in retry_diagnostics
    }
    metadata["retryable"] = bool(retryable)
    return metadata


def _retryable_failure_content(
    *,
    retryable_failure: Dict[str, Any],
    default_content: str,
) -> str:
    """Return the user-facing content for a retryable terminal failure."""
    if str(retryable_failure.get("error_code") or "") == _LLM_TIMEOUT_ERROR_CODE:
        return _LLM_TIMEOUT_ERROR_MESSAGE
    return default_content


class TurnExecutionFailureDispatcher:
    """Maps flow failures to canonical terminal error dispatch calls."""

    def __init__(
        self,
        *,
        error_service: Optional[TurnExecutionErrorService] = None,
        refusal_service: Optional[TurnExecutionRefusalService] = None,
    ) -> None:
        self._error_service = error_service or TurnExecutionErrorService()
        self._refusal_service = refusal_service or TurnExecutionRefusalService(
            error_service=self._error_service
        )

    async def _dispatch_provider_refusal(
        self,
        *,
        exc: BaseException,
        task_id: int,
        hub: Any,
        workflow_id: Optional[int],
        reserved_message_id: Optional[int],
        publish_boundary_completion_events: PublishBoundaryCompletionEvents,
        conversation_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        turn_sequence: Optional[int] = None,
        graph_name: Optional[str] = None,
        checkpoint_id: Optional[str] = None,
        mark_turn_workflow_failed: Optional[Callable[..., None]] = None,
        interrupt_id: Optional[str] = None,
        mark_interrupt_ticket_failed: Optional[Callable[..., None]] = None,
    ) -> bool:
        """Dispatch a nested provider refusal before all generic failure mapping."""
        outcome = self._refusal_service.extract_refusal_outcome(exc)
        if outcome is None:
            return False
        await self._refusal_service.handle_terminal_turn_refusal(
            outcome=outcome,
            task_id=task_id,
            hub=hub,
            workflow_id=workflow_id,
            reserved_message_id=reserved_message_id,
            publish_boundary_completion_events=publish_boundary_completion_events,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            graph_name=graph_name,
            checkpoint_id=checkpoint_id,
            mark_turn_workflow_failed=mark_turn_workflow_failed,
            interrupt_id=interrupt_id,
            mark_interrupt_ticket_failed=mark_interrupt_ticket_failed,
        )
        return True

    @staticmethod
    def _extract_result_failure_context(
        *,
        result: Any,
    ) -> tuple[str, Optional[str], Optional[int], Dict[str, Any]]:
        """Extract conversation and turn identity context from an optional result object."""
        result_metadata: Dict[str, Any] = {}
        if result and isinstance(getattr(result, "metadata", None), dict):
            result_metadata = dict(result.metadata)
        conversation_id = (result.conversation_id if result else "") or ""
        turn_id = (
            str(result_metadata["id"])
            if isinstance(result_metadata.get("id"), str)
            else None
        )
        turn_sequence = (
            result_metadata["turn_sequence"]
            if isinstance(result_metadata.get("turn_sequence"), int)
            else None
        )
        return conversation_id, turn_id, turn_sequence, result_metadata

    @staticmethod
    def _build_retryable_post_tool_dispatch_kwargs(
        *,
        retryable_failure: Dict[str, Any],
        retryable_post_tool_error_message: str,
        fallback_graph_name: Optional[str],
        conversation_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        turn_sequence: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Build normalized terminal error kwargs for retryable post-tool failures."""
        return {
            "error_code": str(retryable_failure["error_code"]),
            "content": _retryable_failure_content(
                retryable_failure=retryable_failure,
                default_content=retryable_post_tool_error_message,
            ),
            "retryable": True,
            "retry_mode": (
                str(retryable_failure["retry_mode"])
                if retryable_failure.get("retry_mode")
                else "checkpoint"
            ),
            "graph_name": (
                str(retryable_failure["graph_name"])
                if retryable_failure.get("graph_name")
                else fallback_graph_name
            ),
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "turn_sequence": turn_sequence,
            "diagnostics": (
                dict(retryable_failure["diagnostics"])
                if isinstance(retryable_failure.get("diagnostics"), dict)
                else None
            ),
            "extra_workflow_metadata": {
                "internal_error_message": retryable_failure["internal_error_message"],
                "provider_error_message": retryable_failure["internal_error_message"],
            },
        }

    async def dispatch_start_compression_failure(
        self,
        *,
        compression_exc: CompressionRequiredError,
        default_error_code: str,
        task_id: int,
        hub: Any,
        workflow_id: Optional[int],
        reserved_message_id: Optional[int],
        generation_failed_error_message: str,
        conversation_id: Optional[str],
        turn_id: Optional[str],
        turn_sequence: Optional[int],
        mark_turn_workflow_failed: Optional[Callable[..., None]],
        publish_boundary_completion_events: PublishBoundaryCompletionEvents,
    ) -> bool:
        """Dispatch start-flow compression failures with canonical error mapping."""
        if await self._dispatch_provider_refusal(
            exc=compression_exc,
            task_id=task_id,
            hub=hub,
            workflow_id=workflow_id,
            reserved_message_id=reserved_message_id,
            publish_boundary_completion_events=publish_boundary_completion_events,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            mark_turn_workflow_failed=mark_turn_workflow_failed,
        ):
            return True
        error_code = self._error_service.resolve_compression_error_code(
            compression_exc,
            default=default_error_code,
        )
        logger.exception(
            "LangGraph-backed generation blocked by compression failure (task=%s, error=%s)",
            task_id,
            error_code,
        )
        await self._error_service.handle_terminal_turn_error(
            task_id=task_id,
            hub=hub,
            workflow_id=workflow_id,
            reserved_message_id=reserved_message_id,
            failure_source="initial_generation",
            error_code=error_code,
            content=generation_failed_error_message,
            retryable=False,
            retry_mode=None,
            graph_name=None,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            mark_turn_workflow_failed=mark_turn_workflow_failed,
            publish_boundary_completion_events=publish_boundary_completion_events,
        )
        return False

    async def dispatch_start_exception(
        self,
        *,
        exc: BaseException,
        task_id: int,
        hub: Any,
        workflow_id: Optional[int],
        reserved_message_id: Optional[int],
        retryable_post_tool_error_message: str,
        generation_failed_error_message: str,
        conversation_id: Optional[str],
        turn_id: Optional[str],
        turn_sequence: Optional[int],
        mark_turn_workflow_failed: Optional[Callable[..., None]],
        publish_boundary_completion_events: PublishBoundaryCompletionEvents,
    ) -> bool:
        """Dispatch a start failure and report whether refusal handling consumed it."""
        if await self._dispatch_provider_refusal(
            exc=exc,
            task_id=task_id,
            hub=hub,
            workflow_id=workflow_id,
            reserved_message_id=reserved_message_id,
            publish_boundary_completion_events=publish_boundary_completion_events,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            mark_turn_workflow_failed=mark_turn_workflow_failed,
        ):
            return True

        if isinstance(exc, PlanModeUnavailableError):
            logger.warning(
                "Plan-mode turn rejected by facade fail-closed gate (task=%s): %s",
                task_id,
                exc,
            )
            await self._error_service.handle_terminal_turn_error(
                task_id=task_id,
                hub=hub,
                workflow_id=workflow_id,
                reserved_message_id=reserved_message_id,
                failure_source="initial_generation",
                error_code="plan_mode_unavailable",
                content=str(exc),
                retryable=False,
                retry_mode=None,
                graph_name=None,
                conversation_id=conversation_id,
                turn_id=turn_id,
                turn_sequence=turn_sequence,
                mark_turn_workflow_failed=mark_turn_workflow_failed,
                publish_boundary_completion_events=publish_boundary_completion_events,
            )
            return False

        retryable_failure = self._error_service.extract_retryable_post_tool_failure(exc)
        if retryable_failure is not None:
            logger.warning(
                "Retryable post-tool failure detected during initial generation "
                "(task=%s, error_code=%s, graph_name=%s)",
                task_id,
                retryable_failure["error_code"],
                retryable_failure.get("graph_name"),
            )
            await self._error_service.handle_terminal_turn_error(
                task_id=task_id,
                hub=hub,
                workflow_id=workflow_id,
                reserved_message_id=reserved_message_id,
                failure_source="initial_generation",
                mark_turn_workflow_failed=mark_turn_workflow_failed,
                publish_boundary_completion_events=publish_boundary_completion_events,
                **self._build_retryable_post_tool_dispatch_kwargs(
                    retryable_failure=retryable_failure,
                    retryable_post_tool_error_message=retryable_post_tool_error_message,
                    fallback_graph_name=None,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    turn_sequence=turn_sequence,
                ),
            )
            return False

        await self._error_service.handle_terminal_turn_error(
            task_id=task_id,
            hub=hub,
            workflow_id=workflow_id,
            reserved_message_id=reserved_message_id,
            failure_source="initial_generation",
            error_code="generation_failed",
            content=generation_failed_error_message,
            retryable=False,
            retry_mode=None,
            graph_name=None,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            mark_turn_workflow_failed=mark_turn_workflow_failed,
            publish_boundary_completion_events=publish_boundary_completion_events,
        )
        return False

    def dispatch_resume_hub_unavailable(
        self,
        *,
        task_id: int,
        workflow_id: Optional[int],
        interrupt_id: Optional[str],
        mark_turn_workflow_failed: Optional[Callable[..., None]],
        mark_interrupt_ticket_failed: Optional[Callable[..., None]],
    ) -> None:
        """Dispatch canonical resume hub-unavailable workflow/interrupt failure side effects."""
        self._error_service.handle_resume_hub_unavailable(
            task_id=task_id,
            workflow_id=workflow_id,
            interrupt_id=interrupt_id,
            mark_turn_workflow_failed=mark_turn_workflow_failed,
            mark_interrupt_ticket_failed=mark_interrupt_ticket_failed,
        )

    async def dispatch_resume_compression_failure(
        self,
        *,
        compression_exc: CompressionRequiredError,
        default_error_code: str,
        task_id: int,
        hub: Any,
        workflow_id: Optional[int],
        reserved_message_id: Optional[int],
        resume_failed_error_message: str,
        graph_name: Optional[str],
        result: Any,
        mark_turn_workflow_failed: Optional[Callable[..., None]],
        interrupt_id: Optional[str],
        mark_interrupt_ticket_failed: Optional[Callable[..., None]],
        publish_boundary_completion_events: PublishBoundaryCompletionEvents,
        failure_source: str = "resume_generation",
        extra_workflow_metadata: Optional[Dict[str, Any]] = None,
        extra_boundary_metadata: Optional[Dict[str, Any]] = None,
        resolved_error_code: Optional[str] = None,
    ) -> bool:
        """Dispatch resume-flow compression failures with canonical context fallback."""
        conversation_id, turn_id, turn_sequence, _ = self._extract_result_failure_context(
            result=result
        )
        if await self._dispatch_provider_refusal(
            exc=compression_exc,
            task_id=task_id,
            hub=hub,
            workflow_id=workflow_id,
            reserved_message_id=reserved_message_id,
            publish_boundary_completion_events=publish_boundary_completion_events,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            graph_name=graph_name,
            mark_turn_workflow_failed=mark_turn_workflow_failed,
            interrupt_id=interrupt_id,
            mark_interrupt_ticket_failed=mark_interrupt_ticket_failed,
        ):
            return True
        error_code = (
            resolved_error_code
            or self._error_service.resolve_compression_error_code(
                compression_exc,
                default=default_error_code,
            )
        )
        logger.exception(
            "[CHAT-RESUME] Compression commit failed for task %s with error %s",
            task_id,
            error_code,
        )
        await self._error_service.handle_terminal_turn_error(
            task_id=task_id,
            hub=hub,
            workflow_id=workflow_id,
            reserved_message_id=reserved_message_id,
            failure_source=failure_source,
            error_code=error_code,
            content=resume_failed_error_message,
            retryable=False,
            retry_mode=None,
            graph_name=graph_name,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            mark_turn_workflow_failed=mark_turn_workflow_failed,
            interrupt_id=interrupt_id,
            mark_interrupt_ticket_failed=mark_interrupt_ticket_failed,
            extra_workflow_metadata=extra_workflow_metadata,
            extra_boundary_metadata=extra_boundary_metadata,
            publish_boundary_completion_events=publish_boundary_completion_events,
        )
        return False

    async def dispatch_resume_exception(
        self,
        *,
        exc: BaseException,
        task_id: int,
        hub: Any,
        workflow_id: Optional[int],
        reserved_message_id: Optional[int],
        graph_name: Optional[str],
        retryable_post_tool_error_message: str,
        resume_failed_error_message: str,
        result: Any,
        mark_turn_workflow_failed: Optional[Callable[..., None]],
        interrupt_id: Optional[str],
        mark_interrupt_ticket_failed: Optional[Callable[..., None]],
        publish_boundary_completion_events: PublishBoundaryCompletionEvents,
        failure_source: str = "resume_generation",
        extra_workflow_metadata: Optional[Dict[str, Any]] = None,
        extra_boundary_metadata: Optional[Dict[str, Any]] = None,
        resolved_error_code: Optional[str] = None,
        retryable_failure: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Dispatch a resume failure and report whether refusal handling consumed it."""
        conversation_id, turn_id, turn_sequence, _ = self._extract_result_failure_context(result=result)
        if await self._dispatch_provider_refusal(
            exc=exc,
            task_id=task_id,
            hub=hub,
            workflow_id=workflow_id,
            reserved_message_id=reserved_message_id,
            publish_boundary_completion_events=publish_boundary_completion_events,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            graph_name=graph_name,
            mark_turn_workflow_failed=mark_turn_workflow_failed,
            interrupt_id=interrupt_id,
            mark_interrupt_ticket_failed=mark_interrupt_ticket_failed,
        ):
            return True

        retryable_failure = (
            retryable_failure
            if retryable_failure is not None
            else self._error_service.extract_retryable_post_tool_failure(exc)
        )
        if retryable_failure is not None:
            retryable_kwargs = self._build_retryable_post_tool_dispatch_kwargs(
                retryable_failure=retryable_failure,
                retryable_post_tool_error_message=retryable_post_tool_error_message,
                fallback_graph_name=graph_name,
                conversation_id=conversation_id,
                turn_id=turn_id,
                turn_sequence=turn_sequence,
            )
            if extra_workflow_metadata:
                merged_extra = dict(
                    retryable_kwargs.get("extra_workflow_metadata") or {}
                )
                merged_extra.update(extra_workflow_metadata)
                retryable_kwargs["extra_workflow_metadata"] = merged_extra
            if extra_boundary_metadata:
                merged_boundary = dict(
                    retryable_kwargs.get("extra_boundary_metadata") or {}
                )
                merged_boundary.update(extra_boundary_metadata)
                retryable_kwargs["extra_boundary_metadata"] = merged_boundary
            await self._error_service.handle_terminal_turn_error(
                task_id=task_id,
                hub=hub,
                workflow_id=workflow_id,
                reserved_message_id=reserved_message_id,
                failure_source=failure_source,
                mark_turn_workflow_failed=mark_turn_workflow_failed,
                interrupt_id=interrupt_id,
                mark_interrupt_ticket_failed=mark_interrupt_ticket_failed,
                publish_boundary_completion_events=publish_boundary_completion_events,
                **retryable_kwargs,
            )
            return False

        await self._error_service.handle_terminal_turn_error(
            task_id=task_id,
            hub=hub,
            workflow_id=workflow_id,
            reserved_message_id=reserved_message_id,
            failure_source=failure_source,
            error_code=resolved_error_code or "resume_failed",
            content=resume_failed_error_message,
            retryable=False,
            retry_mode=None,
            graph_name=graph_name,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            mark_turn_workflow_failed=mark_turn_workflow_failed,
            interrupt_id=interrupt_id,
            mark_interrupt_ticket_failed=mark_interrupt_ticket_failed,
            extra_workflow_metadata=extra_workflow_metadata,
            extra_boundary_metadata=extra_boundary_metadata,
            publish_boundary_completion_events=publish_boundary_completion_events,
        )
        return False

    async def dispatch_retry_hub_unavailable(
        self,
        *,
        task_id: int,
        workflow_id: int,
        reserved_message_id: Optional[int],
        checkpoint_retry_failed_error_message: str,
        graph_name: str,
        turn_id: Optional[str],
        turn_sequence: Optional[int],
        mark_turn_workflow_failed: Optional[Callable[..., None]],
        publish_boundary_completion_events: PublishBoundaryCompletionEvents,
        retry_attempt: Optional[int] = None,
        retry_max_attempts: Optional[int] = None,
        checkpoint_id: Optional[str] = None,
        retry_mode: Optional[str] = None,
        previous_failure: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Dispatch checkpoint-retry hub-unavailable failure."""
        retry_diagnostics = _build_retry_failure_diagnostics(
            failure_stage="hub_unavailable",
            retry_mode=retry_mode,
            retry_attempt=retry_attempt,
            retry_max_attempts=retry_max_attempts,
            checkpoint_id=checkpoint_id,
            graph_name=graph_name,
            workflow_id=workflow_id,
            previous_failure=previous_failure,
        )
        await self._error_service.handle_terminal_turn_error(
            task_id=task_id,
            hub=None,
            workflow_id=workflow_id,
            reserved_message_id=reserved_message_id,
            failure_source="checkpoint_retry",
            error_code="retry_hub_unavailable",
            content=checkpoint_retry_failed_error_message,
            retryable=False,
            retry_mode=retry_mode,
            graph_name=graph_name,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            extra_workflow_metadata=retry_diagnostics,
            extra_boundary_metadata=_build_retry_failure_boundary_metadata(
                retry_diagnostics,
                retryable=False,
            ),
            mark_turn_workflow_failed=mark_turn_workflow_failed,
            publish_boundary_completion_events=publish_boundary_completion_events,
        )

    async def dispatch_retry_compression_failure(
        self,
        *,
        compression_exc: CompressionRequiredError,
        default_error_code: str,
        task_id: int,
        hub: Any,
        workflow_id: int,
        reserved_message_id: Optional[int],
        checkpoint_retry_failed_error_message: str,
        graph_name: str,
        turn_id: Optional[str],
        turn_sequence: Optional[int],
        mark_turn_workflow_failed: Optional[Callable[..., None]],
        publish_boundary_completion_events: PublishBoundaryCompletionEvents,
        retry_attempt: Optional[int] = None,
        retry_max_attempts: Optional[int] = None,
        checkpoint_id: Optional[str] = None,
        retry_mode: Optional[str] = None,
        previous_failure: Optional[Mapping[str, Any]] = None,
        resolved_error_code: Optional[str] = None,
    ) -> bool:
        """Dispatch checkpoint-retry compression persistence failures."""
        if await self._dispatch_provider_refusal(
            exc=compression_exc,
            task_id=task_id,
            hub=hub,
            workflow_id=workflow_id,
            reserved_message_id=reserved_message_id,
            publish_boundary_completion_events=publish_boundary_completion_events,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            graph_name=graph_name,
            checkpoint_id=checkpoint_id,
            mark_turn_workflow_failed=mark_turn_workflow_failed,
        ):
            return True
        error_code = (
            resolved_error_code
            or self._error_service.resolve_compression_error_code(
                compression_exc,
                default=default_error_code,
            )
        )
        logger.exception(
            "[CHECKPOINT-RETRY] Compression commit failed for task %s with error %s",
            task_id,
            error_code,
        )
        retry_diagnostics = _build_retry_failure_diagnostics(
            failure_stage="compression",
            retry_mode=retry_mode,
            retry_attempt=retry_attempt,
            retry_max_attempts=retry_max_attempts,
            checkpoint_id=checkpoint_id,
            graph_name=graph_name,
            workflow_id=workflow_id,
            previous_failure=previous_failure,
        )
        await self._error_service.handle_terminal_turn_error(
            task_id=task_id,
            hub=hub,
            workflow_id=workflow_id,
            reserved_message_id=reserved_message_id,
            failure_source="checkpoint_retry",
            error_code=error_code,
            content=checkpoint_retry_failed_error_message,
            retryable=False,
            retry_mode=retry_mode,
            graph_name=graph_name,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            extra_workflow_metadata=retry_diagnostics,
            extra_boundary_metadata=_build_retry_failure_boundary_metadata(
                retry_diagnostics,
                retryable=False,
            ),
            mark_turn_workflow_failed=mark_turn_workflow_failed,
            publish_boundary_completion_events=publish_boundary_completion_events,
        )
        return False

    async def dispatch_retry_exception(
        self,
        *,
        exc: BaseException,
        task_id: int,
        hub: Any,
        workflow_id: int,
        reserved_message_id: Optional[int],
        graph_name: str,
        retryable_post_tool_error_message: str,
        checkpoint_retry_failed_error_message: str,
        turn_id: Optional[str],
        turn_sequence: Optional[int],
        mark_turn_workflow_failed: Optional[Callable[..., None]],
        publish_boundary_completion_events: PublishBoundaryCompletionEvents,
        retry_attempt: Optional[int] = None,
        retry_max_attempts: Optional[int] = None,
        checkpoint_id: Optional[str] = None,
        retry_mode: Optional[str] = None,
        previous_failure: Optional[Mapping[str, Any]] = None,
        resolved_error_code: Optional[str] = None,
        retryable_failure: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Dispatch a retry failure and report whether refusal handling consumed it."""
        if await self._dispatch_provider_refusal(
            exc=exc,
            task_id=task_id,
            hub=hub,
            workflow_id=workflow_id,
            reserved_message_id=reserved_message_id,
            publish_boundary_completion_events=publish_boundary_completion_events,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            graph_name=graph_name,
            checkpoint_id=checkpoint_id,
            mark_turn_workflow_failed=mark_turn_workflow_failed,
        ):
            return True

        retryable_failure = (
            retryable_failure
            if retryable_failure is not None
            else self._error_service.extract_retryable_post_tool_failure(exc)
        )
        if retryable_failure is not None:
            retryable_kwargs = self._build_retryable_post_tool_dispatch_kwargs(
                retryable_failure=retryable_failure,
                retryable_post_tool_error_message=retryable_post_tool_error_message,
                fallback_graph_name=graph_name,
                turn_id=turn_id,
                turn_sequence=turn_sequence,
            )
            # Merge retry-failure diagnostics into the existing
            # extra_workflow_metadata payload built by the retryable
            # post-tool helper (which carries internal/provider error
            # message). Diagnostic keys win for retry-state reporting.
            existing_extra = retryable_kwargs.get("extra_workflow_metadata") or {}
            retry_diagnostics = _build_retry_failure_diagnostics(
                failure_stage="exception",
                retry_mode=(
                    str(retryable_failure.get("retry_mode"))
                    if retryable_failure.get("retry_mode")
                    else retry_mode
                ),
                retry_attempt=retry_attempt,
                retry_max_attempts=retry_max_attempts,
                checkpoint_id=checkpoint_id,
                graph_name=(
                    str(retryable_failure.get("graph_name"))
                    if retryable_failure.get("graph_name")
                    else graph_name
                ),
                workflow_id=workflow_id,
                previous_failure=previous_failure,
            )
            merged_extra: Dict[str, Any] = dict(existing_extra)
            merged_extra.update(retry_diagnostics)
            retryable_kwargs["extra_workflow_metadata"] = merged_extra
            retryable_kwargs["retryable"] = _retry_allowed_after_failure(
                retry_diagnostics,
                default=bool(retryable_kwargs.get("retryable")),
            )
            retryable_kwargs["extra_boundary_metadata"] = _build_retry_failure_boundary_metadata(
                retry_diagnostics,
                retryable=bool(retryable_kwargs["retryable"]),
            )
            await self._error_service.handle_terminal_turn_error(
                task_id=task_id,
                hub=hub,
                workflow_id=workflow_id,
                reserved_message_id=reserved_message_id,
                failure_source="checkpoint_retry",
                mark_turn_workflow_failed=mark_turn_workflow_failed,
                publish_boundary_completion_events=publish_boundary_completion_events,
                **retryable_kwargs,
            )
            return False

        retry_diagnostics = _build_retry_failure_diagnostics(
            failure_stage="exception",
            retry_mode=retry_mode,
            retry_attempt=retry_attempt,
            retry_max_attempts=retry_max_attempts,
            checkpoint_id=checkpoint_id,
            graph_name=graph_name,
            workflow_id=workflow_id,
            previous_failure=previous_failure,
        )
        await self._error_service.handle_terminal_turn_error(
            task_id=task_id,
            hub=hub,
            workflow_id=workflow_id,
            reserved_message_id=reserved_message_id,
            failure_source="checkpoint_retry",
            error_code=resolved_error_code or "checkpoint_retry_failed",
            content=checkpoint_retry_failed_error_message,
            retryable=False,
            retry_mode=retry_mode,
            graph_name=graph_name,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            extra_workflow_metadata=retry_diagnostics,
            extra_boundary_metadata=_build_retry_failure_boundary_metadata(
                retry_diagnostics,
                retryable=False,
            ),
            mark_turn_workflow_failed=mark_turn_workflow_failed,
            publish_boundary_completion_events=publish_boundary_completion_events,
        )
        return False
