"""
Error handling service for LangGraph turn execution flows.

This module centralizes failure classification and terminal error side effects
for start/resume/checkpoint-retry orchestration. It resolves turn context,
updates durable workflow/interrupt state, persists assistant error content to
reserved rows, and publishes error boundary completion events.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from backend.database import SessionLocal
from backend.services.chat.message_service import ChatMessageService
from backend.services.chat.turn_identity_resolver import (
    resolve_turn_identity_from_reserved_message_best_effort,
)
from backend.services.chat.event_builders import attach_conversation_ids
from backend.services.langgraph_chat.checkpoint.anchor_service import (
    resolve_latest_checkpoint_anchor_best_effort,
)
from backend.services.langgraph_chat.compression.context_models import (
    CompressionRequiredError,
)
from backend.services.langgraph_chat.checkpoint.interrupt_ticket_service import (
    mark_interrupt_ticket_failed_best_effort,
)
from backend.services.langgraph_chat.checkpoint.turn_workflow_service import TurnWorkflowService
from backend.services.langgraph_chat.checkpoint.turn_workflow_service import (
    mark_turn_workflow_failed_best_effort,
)

logger = logging.getLogger(__name__)

_COMPRESSION_REQUIRED_FAILED = "compression_required_failed"
_COMPRESSION_PERSIST_FAILED = "compression_persist_failed"
_CONTEXT_UNCOMPACTABLE = "context_uncompactable"
_RETRYABLE_POST_TOOL_ERROR_CODE = "provider_structured_output_parse"
_RETRYABLE_STRUCTURED_CONTRACT_ERROR_CODE = "structured_contract_semantic_validation"
_LLM_TIMEOUT_ERROR_CODE = "llm_timeout"

# Retry Continuation Context Contract — these are the only fields the
# write path may persist on ``workflow_metadata['last_failure']``. Raw
# request/response payloads, headers, JWTs, cookies, and API keys must
# never be persisted here; the read path (``sanitize_previous_failure``)
# already filters anything outside this whitelist, but the writer also
# enforces it so we never store secrets at rest.
_LAST_FAILURE_WHITELIST: tuple[str, ...] = (
    "error_code",
    "failure_stage",
    "graph_name",
    "tool_name",
    "tool_call_id",
    "summary",
)
_COMPRESSION_FAILURE_CODES: frozenset[str] = frozenset(
    {
        _COMPRESSION_REQUIRED_FAILED,
        _COMPRESSION_PERSIST_FAILED,
        _CONTEXT_UNCOMPACTABLE,
    }
)

PublishBoundaryCompletionEvents = Callable[..., Awaitable[None]]
ResolveCheckpointAnchor = Callable[..., Awaitable[Optional[Any]]]


def _normalize_optional_str(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        if value is None:
            return None
        value = str(value)
    cleaned = value.strip()
    return cleaned or None


class TurnExecutionErrorService:
    """Owns failure classification and terminal turn error side effects."""

    @staticmethod
    def resolve_compression_error_code(
        exc: CompressionRequiredError,
        *,
        default: str,
    ) -> str:
        reason = getattr(exc, "reason", None)
        if reason in _COMPRESSION_FAILURE_CODES:
            return str(reason)
        return default

    @staticmethod
    def iter_exception_chain(exc: BaseException):
        """Yield one exception and its linked causes/contexts without revisiting nodes."""
        seen: set[int] = set()
        stack: list[BaseException] = [exc]
        while stack:
            current = stack.pop()
            current_id = id(current)
            if current_id in seen:
                continue
            seen.add(current_id)
            yield current
            cause = getattr(current, "__cause__", None)
            if isinstance(cause, BaseException):
                stack.append(cause)
            context = getattr(current, "__context__", None)
            if isinstance(context, BaseException):
                stack.append(context)

    @classmethod
    def extract_retryable_post_tool_failure(
        cls,
        exc: BaseException,
    ) -> Optional[Dict[str, Any]]:
        """Extract retryable structured-contract failure details from nested exceptions."""
        try:
            from agent.graph.nodes.post_tool_reasoning.models import (
                RetryablePostToolReasoningError,
            )
        except Exception:
            RetryablePostToolReasoningError = None  # type: ignore[assignment]

        try:
            from agent.reasoning.structured_contract_recovery import (
                StructuredContractViolationError,
            )
        except Exception:
            StructuredContractViolationError = None  # type: ignore[assignment]

        try:
            from core.llm.timeout_runtime import LLMTimeoutError
        except Exception:
            LLMTimeoutError = None  # type: ignore[assignment]

        for candidate in cls.iter_exception_chain(exc):
            if LLMTimeoutError and isinstance(candidate, LLMTimeoutError):
                diagnostics = getattr(candidate, "diagnostics", None)
                return {
                    "error_code": _LLM_TIMEOUT_ERROR_CODE,
                    "retry_mode": "checkpoint",
                    "graph_name": None,
                    "diagnostics": dict(diagnostics)
                    if isinstance(diagnostics, dict)
                    else {},
                    "internal_error_message": str(candidate).strip()
                    or _LLM_TIMEOUT_ERROR_CODE,
                }

            if RetryablePostToolReasoningError and isinstance(
                candidate, RetryablePostToolReasoningError
            ):
                error_code = getattr(candidate, "error_code", None)
                retry_mode = getattr(candidate, "retry_mode", None)
                graph_name = getattr(candidate, "graph_name", None)
                diagnostics = getattr(candidate, "diagnostics", None)
                return {
                    "error_code": (
                        error_code.strip()
                        if isinstance(error_code, str) and error_code.strip()
                        else _RETRYABLE_POST_TOOL_ERROR_CODE
                    ),
                    "retry_mode": (
                        retry_mode.strip()
                        if isinstance(retry_mode, str) and retry_mode.strip()
                        else "checkpoint"
                    ),
                    "graph_name": (
                        graph_name.strip()
                        if isinstance(graph_name, str) and graph_name.strip()
                        else None
                    ),
                    "diagnostics": dict(diagnostics)
                    if isinstance(diagnostics, dict)
                    else {},
                    "internal_error_message": str(candidate).strip()
                    or _RETRYABLE_POST_TOOL_ERROR_CODE,
                }

            if StructuredContractViolationError and isinstance(
                candidate, StructuredContractViolationError
            ):
                if not bool(getattr(candidate, "retryable", False)):
                    continue
                error_code = getattr(candidate, "error_code", None)
                retry_mode = getattr(candidate, "retry_mode", None)
                graph_name = getattr(candidate, "graph_name", None)
                diagnostics = getattr(candidate, "diagnostics", None)
                enriched_diagnostics = (
                    dict(diagnostics) if isinstance(diagnostics, dict) else {}
                )
                stage = getattr(candidate, "stage", None)
                contract = getattr(candidate, "contract", None)
                kind = getattr(candidate, "kind", None)
                if isinstance(stage, str) and stage.strip():
                    enriched_diagnostics.setdefault("stage", stage.strip())
                if isinstance(contract, str) and contract.strip():
                    enriched_diagnostics.setdefault("contract", contract.strip())
                if isinstance(kind, str) and kind.strip():
                    enriched_diagnostics.setdefault("kind", kind.strip())
                return {
                    "error_code": (
                        error_code.strip()
                        if isinstance(error_code, str) and error_code.strip()
                        else _RETRYABLE_STRUCTURED_CONTRACT_ERROR_CODE
                    ),
                    "retry_mode": (
                        retry_mode.strip()
                        if isinstance(retry_mode, str) and retry_mode.strip()
                        else "checkpoint"
                    ),
                    "graph_name": (
                        graph_name.strip()
                        if isinstance(graph_name, str) and graph_name.strip()
                        else None
                    ),
                    "diagnostics": enriched_diagnostics,
                    "internal_error_message": (
                        str(candidate).strip()
                        or _RETRYABLE_STRUCTURED_CONTRACT_ERROR_CODE
                    ),
                }
        return None

    @staticmethod
    def resolve_failure_context(
        *,
        task_id: int,
        workflow_id: Optional[int],
        reserved_message_id: Optional[int],
        conversation_id: Optional[str],
        turn_id: Optional[str],
        turn_sequence: Optional[int],
    ) -> Dict[str, Any]:
        """Resolve canonical conversation/turn identity for terminal outcomes."""
        resolved_conversation_id = (
            conversation_id.strip() if isinstance(conversation_id, str) else ""
        )
        resolved_turn_id = (
            turn_id.strip() if isinstance(turn_id, str) and turn_id.strip() else None
        )
        resolved_turn_sequence = (
            turn_sequence if isinstance(turn_sequence, int) else None
        )
        resolved_reserved_message_id = (
            reserved_message_id if isinstance(reserved_message_id, int) else None
        )
        resolved_graph_name: Optional[str] = None
        resolved_checkpoint_id: Optional[str] = None

        db_session = None
        try:
            from backend.models.chat import ChatMessage

            db_session = SessionLocal()
            if workflow_id is not None:
                workflow_row = TurnWorkflowService(db_session).get_workflow(workflow_id)
                if workflow_row is not None:
                    workflow_conversation_id = getattr(
                        workflow_row, "conversation_id", None
                    )
                    workflow_turn_id = getattr(workflow_row, "turn_id", None)
                    workflow_turn_sequence = getattr(
                        workflow_row, "turn_sequence", None
                    )
                    workflow_reserved_message_id = getattr(
                        workflow_row, "reserved_message_id", None
                    )
                    workflow_graph_name = getattr(workflow_row, "graph_name", None)
                    workflow_checkpoint_id = getattr(
                        workflow_row, "checkpoint_id", None
                    )
                    if (
                        not resolved_conversation_id
                        and isinstance(workflow_conversation_id, str)
                        and workflow_conversation_id.strip()
                    ):
                        resolved_conversation_id = workflow_conversation_id.strip()
                    if (
                        resolved_turn_id is None
                        and isinstance(workflow_turn_id, str)
                        and workflow_turn_id.strip()
                    ):
                        resolved_turn_id = workflow_turn_id.strip()
                    if resolved_turn_sequence is None and isinstance(
                        workflow_turn_sequence, int
                    ):
                        resolved_turn_sequence = workflow_turn_sequence
                    if resolved_reserved_message_id is None and isinstance(
                        workflow_reserved_message_id, int
                    ):
                        resolved_reserved_message_id = workflow_reserved_message_id
                    if resolved_graph_name is None:
                        resolved_graph_name = _normalize_optional_str(
                            workflow_graph_name
                        )
                    if resolved_checkpoint_id is None:
                        resolved_checkpoint_id = _normalize_optional_str(
                            workflow_checkpoint_id
                        )

            if (
                not resolved_conversation_id
                and resolved_reserved_message_id is not None
            ):
                message_row = db_session.get(ChatMessage, resolved_reserved_message_id)
                if message_row is not None:
                    message_conversation_id = getattr(
                        message_row, "conversation_id", None
                    )
                    if (
                        isinstance(message_conversation_id, str)
                        and message_conversation_id.strip()
                    ):
                        resolved_conversation_id = message_conversation_id.strip()
        except Exception:
            logger.debug(
                "Failed to resolve failure context (task=%s workflow_id=%s reserved_message_id=%s)",
                task_id,
                workflow_id,
                resolved_reserved_message_id,
                exc_info=True,
            )
        finally:
            if db_session is not None:
                try:
                    db_session.close()
                except Exception:
                    pass

        if resolved_reserved_message_id is not None and (
            resolved_turn_id is None or resolved_turn_sequence is None
        ):
            resolved_identity = resolve_turn_identity_from_reserved_message_best_effort(
                task_id=task_id,
                reserved_message_id=resolved_reserved_message_id,
            )
            if resolved_turn_id is None:
                resolved_turn_id = resolved_identity[0]
            if resolved_turn_sequence is None:
                resolved_turn_sequence = resolved_identity[1]

        return {
            "conversation_id": resolved_conversation_id,
            "turn_id": resolved_turn_id,
            "turn_sequence": resolved_turn_sequence,
            "reserved_message_id": resolved_reserved_message_id,
            "graph_name": resolved_graph_name,
            "checkpoint_id": resolved_checkpoint_id,
        }

    # Backward-compatible private alias retained for existing test and caller
    # injection points while refusal handling uses the public outcome-neutral name.
    _resolve_failure_context = resolve_failure_context

    @staticmethod
    def _build_last_failure_block(
        *,
        error_code: str,
        graph_name: Optional[str],
        content: str,
        diagnostics: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Build the sanitized ``last_failure`` projection for retryable terminal failures.

        Only the whitelisted fields (``error_code``, ``failure_stage``,
        ``graph_name``, ``tool_name``, ``tool_call_id``, ``summary``) are
        emitted. Diagnostics are never copied wholesale — only the
        offending tool identity is extracted when the failure stage points
        at a tool/graph_continuation. Compression failures use
        ``failure_stage="compression"`` with no tool fields.
        """
        normalized_error = (error_code or "").strip()
        if not normalized_error:
            return None

        diagnostics_map = diagnostics if isinstance(diagnostics, dict) else {}

        # Stage classification: compression failures are stage="compression",
        # everything else falls back to "graph_continuation" (the canonical
        # retry-trigger stage in the Retry Continuation Context Contract)
        # unless diagnostics surface a more specific stage hint.
        stage_hint_raw = diagnostics_map.get("stage")
        stage_hint = stage_hint_raw.strip() if isinstance(stage_hint_raw, str) else ""
        if normalized_error in _COMPRESSION_FAILURE_CODES:
            failure_stage = "compression"
        elif stage_hint:
            failure_stage = stage_hint
        else:
            failure_stage = "graph_continuation"

        block: Dict[str, Any] = {
            "error_code": normalized_error,
            "failure_stage": failure_stage,
        }

        normalized_graph = (
            graph_name.strip()
            if isinstance(graph_name, str) and graph_name.strip()
            else None
        )
        if normalized_graph:
            block["graph_name"] = normalized_graph

        # Compression failures intentionally carry no tool fields.
        if failure_stage != "compression":
            tool_name_raw = diagnostics_map.get("tool_name")
            if isinstance(tool_name_raw, str) and tool_name_raw.strip():
                block["tool_name"] = tool_name_raw.strip()
            tool_call_id_raw = diagnostics_map.get("tool_call_id")
            if isinstance(tool_call_id_raw, str) and tool_call_id_raw.strip():
                block["tool_call_id"] = tool_call_id_raw.strip()

        summary = (content or "").strip()
        if summary:
            # Hard cap so a long error message can never silently bloat
            # workflow_metadata. The retry consumer only needs a concise
            # one-line hint.
            if len(summary) > 500:
                summary = summary[:500].rstrip()
            block["summary"] = summary

        # Defensive: the writer must only ever emit whitelisted keys. If a
        # future caller adds an unexpected key via ``diagnostics`` or
        # similar, this filter strips it before the row is persisted.
        return {
            key: value for key, value in block.items() if key in _LAST_FAILURE_WHITELIST
        }

    @staticmethod
    def _build_error_boundary_metadata(
        *,
        conversation_id: str,
        turn_id: Optional[str],
        turn_sequence: Optional[int],
        error_code: str,
        error_message: str,
        retryable: bool,
        retry_mode: Optional[str],
        graph_name: Optional[str],
        checkpoint_id: Optional[str],
        retry_unavailable_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build canonical assistant error metadata for retryable and non-retryable failures."""
        metadata = attach_conversation_ids(
            {
                "role": "assistant",
                "status": "error",
                "error": error_code,
                "error_code": error_code,
                "error_message": error_message,
                "stop_reason": "error",
                "streaming": False,
            },
            conversation_id,
        )
        if turn_id:
            metadata["id"] = turn_id
            metadata["turn_id"] = turn_id
        if turn_sequence is not None:
            metadata["turn_sequence"] = turn_sequence
        if graph_name:
            metadata["graph_name"] = graph_name
        if checkpoint_id:
            metadata["checkpoint_id"] = checkpoint_id
        if retryable:
            metadata["retryable"] = True
        if retry_mode:
            metadata["retry_mode"] = retry_mode
        if retry_unavailable_reason:
            metadata["retry_unavailable_reason"] = retry_unavailable_reason
        return metadata

    @staticmethod
    def _persist_assistant_error_message(
        *,
        reserved_message_id: Optional[int],
        content: str,
        error_code: str,
    ) -> None:
        """Persist terminal assistant error text onto the reserved assistant row."""
        if reserved_message_id is None:
            return
        db_session = SessionLocal()
        try:
            chat_svc = ChatMessageService(db_session)
            chat_svc.update_message(
                reserved_message_id,
                content,
                error=error_code,
            )
            db_session.commit()
        except Exception:
            db_session.rollback()
            logger.debug(
                "Failed to persist assistant error message (reserved_message_id=%s, error=%s)",
                reserved_message_id,
                error_code,
                exc_info=True,
            )
        finally:
            try:
                db_session.close()
            except Exception:
                pass

    async def handle_terminal_turn_error(
        self,
        *,
        task_id: int,
        hub: Any,
        workflow_id: Optional[int],
        reserved_message_id: Optional[int],
        failure_source: str,
        error_code: str,
        content: str,
        retryable: bool,
        retry_mode: Optional[str],
        graph_name: Optional[str],
        publish_boundary_completion_events: PublishBoundaryCompletionEvents,
        conversation_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        turn_sequence: Optional[int] = None,
        checkpoint_id: Optional[str] = None,
        diagnostics: Optional[Dict[str, Any]] = None,
        extra_workflow_metadata: Optional[Dict[str, Any]] = None,
        extra_boundary_metadata: Optional[Dict[str, Any]] = None,
        mark_turn_workflow_failed: Optional[Callable[..., None]] = None,
        resolve_checkpoint_anchor: Optional[ResolveCheckpointAnchor] = None,
        interrupt_id: Optional[str] = None,
        mark_interrupt_ticket_failed: Optional[Callable[..., None]] = None,
    ) -> None:
        """Persist one canonical terminal assistant error and publish one boundary event."""
        resolved = self._resolve_failure_context(
            task_id=task_id,
            workflow_id=workflow_id,
            reserved_message_id=reserved_message_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
        )
        resolved_conversation_id = str(resolved.get("conversation_id") or "")
        resolved_turn_id = resolved.get("turn_id")
        resolved_turn_sequence = resolved.get("turn_sequence")
        resolved_reserved_message_id = resolved.get("reserved_message_id")
        resolved_graph_name = _normalize_optional_str(resolved.get("graph_name"))
        resolved_checkpoint_id = (
            _normalize_optional_str(checkpoint_id)
            or _normalize_optional_str(
                extra_workflow_metadata.get("checkpoint_id")
                if isinstance(extra_workflow_metadata, dict)
                else None
            )
            or _normalize_optional_str(resolved.get("checkpoint_id"))
        )
        effective_graph_name = (
            _normalize_optional_str(graph_name) or resolved_graph_name
        )
        normalized_retry_mode = _normalize_optional_str(retry_mode)
        effective_retryable = bool(retryable)
        retry_unavailable_reason: Optional[str] = None

        if effective_retryable and normalized_retry_mode == "checkpoint":
            if workflow_id is None:
                effective_retryable = False
                retry_unavailable_reason = "missing_workflow"
            elif resolved_checkpoint_id is None:
                resolver = (
                    resolve_checkpoint_anchor
                    or resolve_latest_checkpoint_anchor_best_effort
                )
                try:
                    anchor = await resolver(
                        task_id=task_id,
                        graph_name=effective_graph_name,
                    )
                except Exception:
                    logger.debug(
                        "Failed to resolve retry checkpoint anchor (task=%s workflow_id=%s)",
                        task_id,
                        workflow_id,
                        exc_info=True,
                    )
                    anchor = None
                if anchor is not None:
                    resolved_checkpoint_id = _normalize_optional_str(
                        getattr(anchor, "checkpoint_id", None)
                        if not isinstance(anchor, dict)
                        else anchor.get("checkpoint_id")
                    )
                    effective_graph_name = (
                        effective_graph_name
                        or _normalize_optional_str(
                            getattr(anchor, "graph_name", None)
                            if not isinstance(anchor, dict)
                            else anchor.get("graph_name")
                        )
                    )
            if effective_retryable and resolved_checkpoint_id is None:
                effective_retryable = False
                retry_unavailable_reason = "missing_checkpoint"

        workflow_metadata: Dict[str, Any] = {
            "failure_source": failure_source,
            "error": error_code,
            "error_message": content,
        }
        if effective_graph_name:
            workflow_metadata["graph_name"] = effective_graph_name
        if resolved_checkpoint_id:
            workflow_metadata["checkpoint_id"] = resolved_checkpoint_id
        if effective_retryable:
            workflow_metadata["retryable"] = True
        if normalized_retry_mode:
            workflow_metadata["retry_mode"] = normalized_retry_mode
        if retry_unavailable_reason:
            workflow_metadata["retry_unavailable_reason"] = retry_unavailable_reason
        if diagnostics:
            workflow_metadata["diagnostics"] = dict(diagnostics)
        if extra_workflow_metadata:
            workflow_metadata.update(extra_workflow_metadata)
        if effective_graph_name:
            workflow_metadata["graph_name"] = effective_graph_name
        if resolved_checkpoint_id:
            workflow_metadata["checkpoint_id"] = resolved_checkpoint_id
        if retry_unavailable_reason:
            workflow_metadata.pop("last_failure", None)
            workflow_metadata["retryable"] = False
            workflow_metadata["retry_unavailable_reason"] = retry_unavailable_reason

        # Retry Continuation Context Contract: when the terminal failure is
        # marked retryable, persist a sanitized ``last_failure`` projection
        # so the retry route's ``previous_failure`` carrier is populated on
        # the next claim. Only whitelisted fields are stored — never raw
        # payloads, headers, or secrets.
        if effective_retryable:
            last_failure_block = self._build_last_failure_block(
                error_code=error_code,
                graph_name=effective_graph_name,
                content=content,
                diagnostics=diagnostics,
            )
            if last_failure_block:
                workflow_metadata["last_failure"] = last_failure_block

        workflow_failed_fn = (
            mark_turn_workflow_failed or mark_turn_workflow_failed_best_effort
        )
        workflow_failed_kwargs: Dict[str, Any] = {
            "workflow_id": workflow_id,
            "metadata": workflow_metadata,
        }
        if resolved_checkpoint_id:
            workflow_failed_kwargs["checkpoint_id"] = resolved_checkpoint_id
        if effective_graph_name:
            workflow_failed_kwargs["graph_name"] = effective_graph_name
        workflow_failed_fn(**workflow_failed_kwargs)

        if interrupt_id is not None:
            interrupt_failed_fn = (
                mark_interrupt_ticket_failed or mark_interrupt_ticket_failed_best_effort
            )
            interrupt_failed_fn(task_id=task_id, interrupt_id=interrupt_id)

        self._persist_assistant_error_message(
            reserved_message_id=resolved_reserved_message_id,
            content=content,
            error_code=error_code,
        )

        boundary_metadata = self._build_error_boundary_metadata(
            conversation_id=resolved_conversation_id,
            turn_id=resolved_turn_id if isinstance(resolved_turn_id, str) else None,
            turn_sequence=resolved_turn_sequence
            if isinstance(resolved_turn_sequence, int)
            else None,
            error_code=error_code,
            error_message=content,
            retryable=effective_retryable,
            retry_mode=normalized_retry_mode,
            graph_name=effective_graph_name,
            checkpoint_id=resolved_checkpoint_id,
            retry_unavailable_reason=retry_unavailable_reason,
        )
        if extra_boundary_metadata:
            boundary_metadata.update(extra_boundary_metadata)

        if hub is None:
            return
        try:
            await publish_boundary_completion_events(
                task_id=task_id,
                hub=hub,
                content=content,
                conversation_id=resolved_conversation_id,
                turn_id=boundary_metadata.get("id"),
                turn_sequence=boundary_metadata.get("turn_sequence"),
                base_metadata=boundary_metadata,
            )
        except Exception:
            logger.debug(
                "Failed to publish terminal error boundary for task %s",
                task_id,
                exc_info=True,
            )

    def handle_resume_hub_unavailable(
        self,
        *,
        task_id: int,
        workflow_id: Optional[int],
        interrupt_id: Optional[str],
        mark_turn_workflow_failed: Optional[Callable[..., None]] = None,
        mark_interrupt_ticket_failed: Optional[Callable[..., None]] = None,
    ) -> None:
        """Apply canonical resume setup failure side effects when stream hub is unavailable."""
        workflow_failed_fn = (
            mark_turn_workflow_failed or mark_turn_workflow_failed_best_effort
        )
        interrupt_failed_fn = (
            mark_interrupt_ticket_failed or mark_interrupt_ticket_failed_best_effort
        )
        workflow_failed_fn(
            workflow_id=workflow_id,
            metadata={
                "failure_source": "resume_generation",
                "error": "resume_hub_unavailable",
            },
        )
        interrupt_failed_fn(task_id=task_id, interrupt_id=interrupt_id)
