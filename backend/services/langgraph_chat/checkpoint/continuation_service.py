"""Service that continues a LangGraph run from persisted checkpoint state.

Owns the resume-from-interrupt and retry-from-checkpoint flows. Both share
the same inner continuation: build run config, compile graph, stream graph,
parse final state, hydrate container if needed, persist, build result.
"""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Mapping, Optional

from agent.graph.infrastructure.state_models import checkpoint_safe_llm_runtime_selection
from agent.subagents.registry import SubagentRegistry, get_subagent_registry
from backend.config import E2E_DETERMINISTIC_MODE
from backend.database import SessionLocal
from backend.services.agent_runs.cancellation import AsyncCancellationProbe
from backend.services.agent_runs.continuation import (
    SUBAGENT_PARENT_CONTINUATION_PENDING,
    SubagentContinuationContext,
    SubagentContinuationError,
    build_subagent_continuation_attribution,
    cancel_subagent_continuation,
    complete_subagent_continuation,
    is_subagent_continuation_cancel_requested,
    is_subagent_graph_name,
    pause_subagent_continuation,
    prepare_subagent_resume,
    resume_subagent_continuation,
)
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.services.agent_runs.parent_handoff_continuation import (
    ParentHandoffContinuationBroker,
    ParentHandoffContinuationSession,
)
from backend.services.agent_runs.completion import (
    AgentRunCompletion,
    usage_envelopes_from_child_records,
)
from backend.services.chat.message_service import ChatMessageService
from backend.services.langgraph_chat.checkpoint.runtime_selection_resolver import (
    resolve_checkpoint_runtime_selection,
)
from backend.services.langgraph_chat.contracts import LangGraphChatResult
from backend.services.langgraph_chat.exceptions import HITLError
from backend.services.langgraph_chat.hitl_constants import (
    DEFAULT_GRAPH_NAME,
    GRAPH_NAME_DEEP_REASONING,
    GRAPH_NAME_INTERRUPT_RESUME,
    GRAPH_NAME_PARENT_HANDOFF,
    GRAPH_NAME_SIMPLE_TOOL,
)
from backend.services.langgraph_chat.execution.scenario_factory import get_scenario_graph
from backend.services.langgraph_chat.execution.graph_executor import (
    GraphExecutionCancelled,
    GraphExecutionResult,
)
from backend.services.langgraph_chat.runtime.state_container import ChatStateContainer
from backend.services.llm_provider.runtime_config_service import LLMRuntimeConfigService
from backend.services.llm_provider.types import (
    CredentialNotFoundError,
    ProviderConfigurationError,
)

if TYPE_CHECKING:
    from agent.graph import InteractiveState

logger = logging.getLogger("backend.services.langgraph_chat.facade")


def extract_resume_conversation_id(final_state: Any) -> str:
    """Extract conversation id from resume final_state, when present.

    Args:
        final_state: Final LangGraph state payload.

    Returns:
        The conversation id when present, otherwise an empty string.
    """
    if isinstance(final_state, dict):
        facts = final_state.get("facts")
        if isinstance(facts, dict):
            value = facts.get("conversation_id")
            if isinstance(value, str) and value.strip():
                return value
    return ""


def resolve_resume_turn_number(*, reserved_message_id: Optional[int]) -> int:
    """Resolve persisted turn number for resume completion callbacks.

    Args:
        reserved_message_id: Reserved assistant message id, when present.

    Returns:
        Persisted turn number, ``0`` without a reserved message, or the
        reserved id as fallback.
    """
    if reserved_message_id is None:
        return 0
    db_lookup = SessionLocal()
    try:
        chat_svc = ChatMessageService(db_lookup)
        turn_number = chat_svc.get_turn_number(reserved_message_id)
        if turn_number is not None:
            return int(turn_number)
    except Exception:
        logger.debug(
            "[HITL] Failed to resolve turn_number for message %s during resume persistence",
            reserved_message_id,
            exc_info=True,
        )
    finally:
        try:
            db_lookup.close()
        except Exception:
            pass
    return int(reserved_message_id)


class CheckpointContinuationService:
    """Resume/retry a LangGraph run from a stored checkpoint."""

    def __init__(
        self,
        *,
        checkpointer_service: Any,
        executor: Any,
        streaming_adapter: Any,
        build_checkpoint_execution_config: Callable[..., Dict[str, Any]],
        hydrate_container_from_checkpoint_state: Callable[..., None],
        extract_resume_conversation_id: Callable[[Any], str],
        resolve_resume_turn_number: Callable[..., int],
        persist_chat_message_from_container: Callable[..., None],
        build_result: Callable[..., LangGraphChatResult],
        agent_run_registry: Optional[ProcessLocalAgentRunRegistry] = None,
        subagent_registry: SubagentRegistry | None = None,
        agent_run_lifecycle_publisher: Optional[
            Callable[[int, dict[str, Any]], Awaitable[None]]
        ] = None,
        parent_handoff_continuation_broker: (
            ParentHandoffContinuationBroker | None
        ) = None,
    ) -> None:
        """Initialize continuation service dependencies.

        Args:
            checkpointer_service: Service that provides graph checkpointers.
            executor: Graph executor used for streaming continuation.
            streaming_adapter: Streaming adapter dependency kept with facade DI.
            build_checkpoint_execution_config: Callback that builds run config.
            hydrate_container_from_checkpoint_state: Callback for retry hydration.
            extract_resume_conversation_id: Callback for fallback conversation ids.
            resolve_resume_turn_number: Callback for turn-number lookup.
            persist_chat_message_from_container: Callback for persistence writes.
            build_result: Callback that constructs chat results.
            agent_run_lifecycle_publisher: Optional task-stream lifecycle publisher.
        """
        self._checkpointer_service = checkpointer_service
        self._executor = executor
        self._streaming_adapter = streaming_adapter
        self._build_checkpoint_execution_config = build_checkpoint_execution_config
        self._hydrate_container_from_checkpoint_state = (
            hydrate_container_from_checkpoint_state
        )
        self._extract_resume_conversation_id = extract_resume_conversation_id
        self._resolve_resume_turn_number = resolve_resume_turn_number
        self._persist_chat_message_from_container = persist_chat_message_from_container
        self._build_result = build_result
        self._agent_run_registry = agent_run_registry
        self._subagent_registry = subagent_registry or get_subagent_registry()
        self._agent_run_lifecycle_publisher = agent_run_lifecycle_publisher
        self._parent_handoff_continuation_broker = (
            parent_handoff_continuation_broker
            or ParentHandoffContinuationBroker()
        )

    async def resume_from_interrupt(
        self,
        *,
        task_id: int,
        user_id: Optional[int] = None,
        graph_thread_id: Optional[str] = None,
        response: Dict[str, Any],
        tenant_id: Optional[int] = None,
        runtime_placement_mode: Optional[str] = None,
        workspace_id: Optional[str] = None,
        actor_type: Optional[str] = None,
        actor_id: Optional[str] = None,
        runner_id: Optional[str] = None,
        execution_site_id: Optional[str] = None,
        graph_name: Optional[str] = None,
        checkpoint_id: Optional[int | str] = None,
        reserved_message_id: Optional[int] = None,
        approval_received_at: Optional[float] = None,
        resume_worker_start_at: Optional[float] = None,
        interrupt_id: Optional[str] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
        replace_turn_events: bool = False,
        llm_runtime_selection: Optional[Mapping[str, Any]] = None,
        runtime_services: Any = None,
    ) -> LangGraphChatResult:
        """Resume graph execution from an interrupt point.

        Args:
            task_id: Task ID with pending interrupt.
            response: User response to the interrupt.
            graph_name: Optional graph name; defaults to simple tool.
            checkpoint_id: Optional checkpoint id to pin continuation.
            reserved_message_id: Reserved assistant message id.
            approval_received_at: Optional approval timestamp.
            resume_worker_start_at: Optional worker-start timestamp.
            interrupt_id: Optional interrupt id.
            should_cancel: Optional cancellation callback.
            replace_turn_events: Whether to replace canonical turn events.

        Returns:
            LangGraphChatResult from continued execution.

        Raises:
            GraphExecutionCancelled: If the parent turn is cancelled.
            HITLError: If resume fails.
        """
        from langgraph.types import Command

        graph_name = graph_name or DEFAULT_GRAPH_NAME
        try:
            subagent_context = await self._prepare_subagent_resume_context(
                task_id=task_id,
                tenant_id=tenant_id,
                graph_name=graph_name,
                interrupt_id=interrupt_id,
                checkpoint_id=checkpoint_id,
            )
            if subagent_context is not None:
                graph_thread_id = subagent_context.graph_thread_id
                checkpoint_id = subagent_context.checkpoint_id
                await resume_subagent_continuation(
                    registry=self._require_agent_run_registry(),
                    subagent_registry=self._subagent_registry,
                    context=subagent_context,
                    lifecycle_publisher=(
                        self._require_agent_run_lifecycle_publisher()
                    ),
                )
            return await self.continue_from_checkpoint(
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
                graph_input=Command(resume=response),
                reserved_message_id=reserved_message_id,
                checkpoint_id=checkpoint_id,
                approval_received_at=approval_received_at,
                resume_worker_start_at=resume_worker_start_at,
                interrupt_id=interrupt_id,
                should_cancel=should_cancel,
                interrupt_persist_reason="resume_hitl_interrupt",
                success_persist_reason="resume_normal",
                replace_turn_events=replace_turn_events,
                llm_runtime_selection=llm_runtime_selection,
                runtime_services=runtime_services,
                subagent_continuation_context=subagent_context,
            )
        except GraphExecutionCancelled:
            raise
        except Exception as exc:
            if graph_name == GRAPH_NAME_PARENT_HANDOFF:
                self._parent_handoff_continuation_broker.fail(
                    task_id=task_id,
                    graph_thread_id=graph_thread_id,
                    error=exc,
                )
            msg = f"[HITL] Resume failed for task {task_id}: {exc}"
            logger.error(msg, exc_info=True)
            raise HITLError(msg) from exc

    async def retry_from_checkpoint(
        self,
        *,
        task_id: int,
        user_id: Optional[int] = None,
        graph_thread_id: Optional[str] = None,
        graph_name: str,
        tenant_id: Optional[int] = None,
        runtime_placement_mode: Optional[str] = None,
        workspace_id: Optional[str] = None,
        actor_type: Optional[str] = None,
        actor_id: Optional[str] = None,
        runner_id: Optional[str] = None,
        execution_site_id: Optional[str] = None,
        checkpoint_id: Optional[int | str] = None,
        retry_context: Optional[Mapping[str, Any]] = None,
        reserved_message_id: Optional[int] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
        llm_runtime_selection: Optional[Mapping[str, Any]] = None,
        runtime_services: Any = None,
        subagent_continuation_context: SubagentContinuationContext | None = None,
    ) -> LangGraphChatResult:
        """Retry a failed turn from a stored checkpoint.

        Args:
            task_id: Task identifier.
            graph_name: Graph name being retried.
            checkpoint_id: Optional checkpoint id to pin continuation.
            retry_context: Optional sanitized checkpoint-retry context.
            reserved_message_id: Reserved assistant message id.
            should_cancel: Optional cancellation callback.

        Returns:
            LangGraphChatResult from continued execution.

        Raises:
            HITLError: If retry fails.
        """
        try:
            return await self.continue_from_checkpoint(
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
                graph_input=None,
                reserved_message_id=reserved_message_id,
                checkpoint_id=checkpoint_id,
                retry_context=retry_context,
                should_cancel=should_cancel,
                interrupt_persist_reason="checkpoint_retry_interrupt",
                success_persist_reason="checkpoint_retry",
                replace_turn_events=True,
                llm_runtime_selection=llm_runtime_selection,
                runtime_services=runtime_services,
            )
        except Exception as exc:
            msg = f"[HITL] Checkpoint retry failed for task {task_id}: {exc}"
            logger.error(msg, exc_info=True)
            raise HITLError(msg) from exc

    async def continue_from_checkpoint(
        self,
        *,
        task_id: int,
        user_id: Optional[int],
        graph_thread_id: Optional[str],
        graph_name: str,
        graph_input: Any,
        tenant_id: Optional[int] = None,
        runtime_placement_mode: Optional[str] = None,
        workspace_id: Optional[str] = None,
        actor_type: Optional[str] = None,
        actor_id: Optional[str] = None,
        runner_id: Optional[str] = None,
        execution_site_id: Optional[str] = None,
        reserved_message_id: Optional[int],
        checkpoint_id: Optional[int | str] = None,
        approval_received_at: Optional[float] = None,
        resume_worker_start_at: Optional[float] = None,
        interrupt_id: Optional[str] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
        retry_context: Optional[Mapping[str, Any]] = None,
        interrupt_persist_reason: str,
        success_persist_reason: str,
        replace_turn_events: bool = False,
        llm_runtime_selection: Optional[Mapping[str, Any]] = None,
        runtime_services: Any = None,
        subagent_continuation_context: SubagentContinuationContext | None = None,
    ) -> LangGraphChatResult:
        """Continue a graph from persisted checkpoint state.

        Args:
            task_id: Task identifier.
            graph_name: Graph name to continue.
            graph_input: LangGraph input or resume command.
            reserved_message_id: Reserved assistant message id.
            checkpoint_id: Optional checkpoint id.
            approval_received_at: Optional approval timestamp.
            resume_worker_start_at: Optional worker-start timestamp.
            interrupt_id: Optional interrupt id.
            should_cancel: Optional cancellation callback.
            retry_context: Optional checkpoint retry context.
            interrupt_persist_reason: Persistence reason for interrupt.
            success_persist_reason: Persistence reason for success.
            replace_turn_events: Whether to replace canonical turn events.
            subagent_continuation_context: Verified subagent run context for local
                registry status updates during approval resume.

        Returns:
            LangGraphChatResult from continued execution.

        Raises:
            GraphExecutionCancelled: If the parent turn is cancelled.
            HITLError: If continuation cannot produce/parse final state.
        """
        from agent.graph import InteractiveState
        parent_session: ParentHandoffContinuationSession | None = None
        if graph_name == GRAPH_NAME_PARENT_HANDOFF:
            parent_session = self._parent_handoff_continuation_broker.require(
                task_id=task_id,
                graph_thread_id=graph_thread_id,
            )
        state_container = (
            parent_session.state_container
            if parent_session is not None
            else ChatStateContainer(reserved_message_id=reserved_message_id)
        )
        try:
            (
                execution_result,
                llm_runtime_selection,
                subagent_event_attribution,
            ) = await self._execute_continuation(
                task_id=task_id,
                user_id=user_id,
                graph_thread_id=graph_thread_id,
                graph_name=graph_name,
                graph_input=graph_input,
                tenant_id=tenant_id,
                runtime_placement_mode=runtime_placement_mode,
                workspace_id=workspace_id,
                actor_type=actor_type,
                actor_id=actor_id,
                runner_id=runner_id,
                execution_site_id=execution_site_id,
                checkpoint_id=checkpoint_id,
                approval_received_at=approval_received_at,
                resume_worker_start_at=resume_worker_start_at,
                interrupt_id=interrupt_id,
                should_cancel=should_cancel,
                retry_context=retry_context,
                llm_runtime_selection=llm_runtime_selection,
                runtime_services=runtime_services,
                state_container=state_container,
                subagent_continuation_context=subagent_continuation_context,
            )
        except GraphExecutionCancelled as cancellation:
            cancelled_result = await self._resolve_continuation_cancellation(
                graph_name=graph_name,
                execution_result=cancellation.execution_result,
                subagent_context=subagent_continuation_context,
                should_cancel=should_cancel,
                reserved_message_id=reserved_message_id,
                cancellation=cancellation,
            )
            assert cancelled_result is not None
            return cancelled_result

        cancelled_result = await self._resolve_continuation_cancellation(
            graph_name=graph_name,
            execution_result=execution_result,
            subagent_context=subagent_continuation_context,
            should_cancel=should_cancel,
            reserved_message_id=reserved_message_id,
        )
        if cancelled_result is not None:
            return cancelled_result

        if not execution_result.final_state:
            if execution_result.interrupted:
                msg = f"[HITL] Continuation interrupt missing state for task {task_id}"
            else:
                msg = f"[HITL] Continuation did not capture final state for task {task_id}"
            logger.error(msg)
            raise HITLError(msg)

        if parent_session is not None and not execution_result.interrupted:
            self._parent_handoff_continuation_broker.deliver(
                parent_session,
                execution_result,
            )
            metadata = dict(execution_result.metadata or {})
            metadata.update(
                {
                    SUBAGENT_PARENT_CONTINUATION_PENDING: True,
                    "graph_name": GRAPH_NAME_PARENT_HANDOFF,
                    "parent_handoff_continuation_resumed": True,
                }
            )
            return LangGraphChatResult(
                final_text=None,
                conversation_id=None,
                metadata=metadata,
                persistence_handled=True,
            )

        # Parse the checkpoint state once for both branches. The interrupt
        # branch needs it to hydrate ``state_container`` with cached
        # tool/reasoning/observation rows (otherwise the resync-driven
        # re-bootstrap during HITL pause renders a blank turn). The success
        # branch still needs it to synthesize the final result; parse failure
        # there is fatal because downstream code can't continue without the
        # parsed state. On the interrupt branch, parse failure degrades to
        # best-effort: we still persist whatever the live adapter captured.
        interactive_state: Optional[InteractiveState] = None
        try:
            interactive_state = InteractiveState.from_mapping(
                execution_result.final_state
            )
        except Exception as parse_exc:
            if not execution_result.interrupted:
                logger.error(
                    "[HITL] Failed to parse InteractiveState for task %s: %s",
                    task_id,
                    parse_exc,
                )
                logger.error("[HITL] Result content: %s", execution_result.final_state)
                raise HITLError(
                    f"Failed to parse result state: {parse_exc}"
                ) from parse_exc
            logger.warning(
                "[HITL] Failed to parse InteractiveState on interrupt path for task %s "
                "(best-effort, hydration skipped): %s",
                task_id,
                parse_exc,
            )

        # Hydrate the state container from cached checkpoint state when the
        # live adapter didn't see prior events (rewind past tool/reasoning
        # nodes that don't re-execute). Gated on ``replace_turn_events``
        # because we only hydrate paths that overwrite canonical rows
        # wholesale (checkpoint retry / retry-resume). On the merge path
        # (initial HITL resume) the original turn's live-captured rows are
        # already in chat_turn_events and hydrating with synthetic ids
        # would produce duplicates. The helper itself is also idempotent
        # against live captures within the current run.
        if interactive_state is not None and replace_turn_events:
            self._hydrate_container_from_checkpoint_state(
                state_container,
                interactive_state,
                task_id=task_id,
            )

        if execution_result.interrupted:
            return await self._build_interrupted_continuation_result(
                task_id=task_id,
                graph_name=graph_name,
                execution_result=execution_result,
                interactive_state=interactive_state,
                state_container=state_container,
                reserved_message_id=reserved_message_id,
                interrupt_persist_reason=interrupt_persist_reason,
                replace_turn_events=replace_turn_events,
                llm_runtime_selection=llm_runtime_selection,
                event_attribution=subagent_event_attribution,
                subagent_context=subagent_continuation_context,
            )

        assert interactive_state is not None
        return await self._build_completed_continuation_result(
            task_id=task_id,
            graph_name=graph_name,
            execution_result=execution_result,
            interactive_state=interactive_state,
            state_container=state_container,
            reserved_message_id=reserved_message_id,
            success_persist_reason=success_persist_reason,
            replace_turn_events=replace_turn_events,
            llm_runtime_selection=llm_runtime_selection,
            event_attribution=subagent_event_attribution,
            subagent_context=subagent_continuation_context,
        )

    async def _resolve_continuation_cancellation(
        self,
        *,
        graph_name: str,
        execution_result: GraphExecutionResult,
        subagent_context: SubagentContinuationContext | None,
        should_cancel: Optional[Callable[[], bool]],
        reserved_message_id: Optional[int],
        cancellation: GraphExecutionCancelled | None = None,
    ) -> LangGraphChatResult | None:
        """Settle child cancellation and preserve parent cancellation precedence."""
        child_cancelled = (
            await is_subagent_continuation_cancel_requested(
                registry=self._require_agent_run_registry(),
                context=subagent_context,
            )
            if subagent_context is not None
            else False
        )
        parent_cancelled = bool(should_cancel and should_cancel())

        if not child_cancelled and not parent_cancelled:
            if cancellation is not None:
                raise cancellation
            return None

        cancelled_result: LangGraphChatResult | None = None
        if child_cancelled and subagent_context is not None:
            completion = await cancel_subagent_continuation(
                registry=self._require_agent_run_registry(),
                subagent_registry=self._subagent_registry,
                context=subagent_context,
                final_state=execution_result.final_state,
                lifecycle_publisher=self._require_agent_run_lifecycle_publisher(),
            )
            cancelled_result = self._build_cancelled_subagent_result(
                graph_name=graph_name,
                execution_result=execution_result,
                subagent_context=subagent_context,
                completion=completion,
                reserved_message_id=reserved_message_id,
            )

        if parent_cancelled:
            raise cancellation or GraphExecutionCancelled(execution_result)
        return cancelled_result

    def _build_cancelled_subagent_result(
        self,
        *,
        graph_name: str,
        execution_result: GraphExecutionResult,
        subagent_context: SubagentContinuationContext,
        completion: AgentRunCompletion,
        reserved_message_id: Optional[int],
    ) -> LangGraphChatResult:
        """Return a cancelled-child handoff without finalizing the parent turn."""
        turn_number = self._resolve_resume_turn_number(
            reserved_message_id=reserved_message_id
        )
        turn_index = turn_number if isinstance(turn_number, int) else None
        usage = usage_envelopes_from_child_records(
            completion.usage_records,
            execution_branch="subagent_child",
            turn_index=turn_index,
        )
        metadata = {
            SUBAGENT_PARENT_CONTINUATION_PENDING: True,
            "graph_name": graph_name,
            "status": "cancelled",
            "cancel_requested": True,
        }
        if isinstance(execution_result.metadata, dict):
            metadata.update(execution_result.metadata)
        return LangGraphChatResult(
            final_text=None,
            conversation_id=subagent_context.entry.conversation_id,
            metadata=metadata,
            persistence_handled=True,
            usage=usage or None,
        )

    async def _build_interrupted_continuation_result(
        self,
        *,
        task_id: int,
        graph_name: str,
        execution_result: Any,
        interactive_state: Optional["InteractiveState"],
        state_container: ChatStateContainer,
        reserved_message_id: Optional[int],
        interrupt_persist_reason: str,
        replace_turn_events: bool,
        llm_runtime_selection: Optional[Mapping[str, Any]],
        event_attribution: dict[str, Any] | None,
        subagent_context: SubagentContinuationContext | None,
    ) -> LangGraphChatResult:
        """Persist and return a continuation that paused on another interrupt."""
        interrupted_usage = None
        if subagent_context is not None:
            interrupted_usage = await pause_subagent_continuation(
                registry=self._require_agent_run_registry(),
                context=subagent_context,
                final_state=execution_result.final_state,
            )
        usage_records = (
            interrupted_usage.unaccounted_records
            if interrupted_usage is not None
            else ()
        )
        logger.info("[HITL] Continuation hit another interrupt for task %s", task_id)
        conversation_id = (
            interactive_state.facts.conversation_id
            if interactive_state is not None
            else self._extract_resume_conversation_id(execution_result.final_state)
        ) or ""
        turn_number = self._resolve_resume_turn_number(
            reserved_message_id=reserved_message_id
        )
        turn_id = (
            f"task-{task_id}-turn-{turn_number}"
            if turn_number
            else f"task-{task_id}"
        )
        usage = None
        if usage_records:
            usage = usage_envelopes_from_child_records(
                usage_records,
                execution_branch="subagent_child",
                turn_index=turn_number if isinstance(turn_number, int) else None,
            )
        self._persist_chat_message_from_container(
            task_id=task_id,
            turn_id=turn_id,
            reserved_message_id=reserved_message_id,
            state_container=state_container,
            final_message=None,
            error="interrupted",
            reason=interrupt_persist_reason,
            conversation_id=conversation_id,
            turn_number=turn_number,
            replace_turn_events=replace_turn_events,
            event_attribution=event_attribution,
        )
        metadata = {"interrupt": True, "graph_name": graph_name}
        if isinstance(execution_result.metadata, dict):
            metadata.update(execution_result.metadata)
        safe_runtime_selection = checkpoint_safe_llm_runtime_selection(
            llm_runtime_selection
        )
        if safe_runtime_selection:
            metadata["llm_runtime_selection"] = safe_runtime_selection
        return LangGraphChatResult(
            final_text=None,
            conversation_id=None,
            metadata=metadata,
            persistence_handled=True,
            usage=usage,
        )

    async def _build_completed_continuation_result(
        self,
        *,
        task_id: int,
        graph_name: str,
        execution_result: Any,
        interactive_state: "InteractiveState",
        state_container: ChatStateContainer,
        reserved_message_id: Optional[int],
        success_persist_reason: str,
        replace_turn_events: bool,
        llm_runtime_selection: Optional[Mapping[str, Any]],
        event_attribution: dict[str, Any] | None,
        subagent_context: SubagentContinuationContext | None,
    ) -> LangGraphChatResult:
        """Persist and return one successfully completed continuation."""
        from backend.services.chat.event_builders import attach_conversation_ids
        from backend.services.langgraph_chat.handlers.normal_chat_handler import (
            _extract_usage_from_state,
        )

        logger.info(
            "[HITL] Continuation completed for task %s, parsing state...", task_id
        )
        final_text = interactive_state.trace.final_text or interactive_state.facts.message
        interactive_state.trace.final_text = final_text
        conversation_id = interactive_state.facts.conversation_id
        turn_number = self._resolve_resume_turn_number(
            reserved_message_id=reserved_message_id
        )
        turn_id = (
            f"task-{task_id}-turn-{turn_number}" if turn_number else f"task-{task_id}"
        )
        self._persist_chat_message_from_container(
            task_id=task_id,
            turn_id=turn_id,
            reserved_message_id=reserved_message_id,
            state_container=state_container,
            final_message=None if subagent_context is not None else final_text,
            error=None,
            reason=success_persist_reason,
            conversation_id=conversation_id or "",
            turn_number=turn_number,
            replace_turn_events=replace_turn_events,
            event_attribution=event_attribution,
        )
        subagent_completion: AgentRunCompletion | None = None
        if subagent_context is not None:
            subagent_completion = await complete_subagent_continuation(
                registry=self._require_agent_run_registry(),
                subagent_registry=self._subagent_registry,
                context=subagent_context,
                final_state=execution_result.final_state,
                lifecycle_publisher=self._require_agent_run_lifecycle_publisher(),
            )
        metadata = attach_conversation_ids(
            {"role": "assistant", "streaming": False, "graph_name": graph_name},
            conversation_id or "",
        )
        if reserved_message_id is not None:
            metadata["turn_sequence"] = turn_number
            metadata["id"] = turn_id
        if isinstance(execution_result.metadata, dict):
            metadata.update(execution_result.metadata)
        safe_runtime_selection = checkpoint_safe_llm_runtime_selection(
            llm_runtime_selection
        )
        if safe_runtime_selection:
            metadata["llm_runtime_selection"] = safe_runtime_selection
        if subagent_completion is not None:
            metadata[SUBAGENT_PARENT_CONTINUATION_PENDING] = True

        resume_execution_branch = (
            GRAPH_NAME_DEEP_REASONING
            if graph_name == GRAPH_NAME_DEEP_REASONING
            else GRAPH_NAME_SIMPLE_TOOL
            if graph_name == GRAPH_NAME_SIMPLE_TOOL
            else "unknown"
        )
        turn_index = turn_number if isinstance(turn_number, int) else None
        usage = (
            usage_envelopes_from_child_records(
                subagent_completion.usage_records,
                execution_branch="subagent_child",
                turn_index=turn_index,
            )
            if subagent_completion is not None
            else _extract_usage_from_state(
                interactive_state,
                execution_branch=resume_execution_branch,
                turn_index=turn_index,
            )
        )
        if usage:
            logger.info(
                "[HITL] Extracted %s usage records for task %s, total_tokens=%s",
                len(usage),
                task_id,
                sum(entry.usage.total_tokens for entry in usage),
            )
        result = self._build_result(
            final_text=None if subagent_completion is not None else final_text,
            conversation_id=conversation_id,
            interactive_state=interactive_state,
            metadata=metadata,
            events=[],
            turn_id=turn_id,
            usage=usage,
        )
        result.persistence_handled = True
        return result

    async def _execute_continuation(
        self,
        *,
        task_id: int,
        user_id: Optional[int],
        graph_thread_id: Optional[str],
        graph_name: str,
        graph_input: Any,
        tenant_id: Optional[int],
        runtime_placement_mode: Optional[str],
        workspace_id: Optional[str],
        actor_type: Optional[str],
        actor_id: Optional[str],
        runner_id: Optional[str],
        execution_site_id: Optional[str],
        checkpoint_id: Optional[int | str],
        approval_received_at: Optional[float],
        resume_worker_start_at: Optional[float],
        interrupt_id: Optional[str],
        should_cancel: Optional[Callable[[], bool]],
        retry_context: Optional[Mapping[str, Any]],
        llm_runtime_selection: Optional[Mapping[str, Any]],
        runtime_services: Any,
        state_container: ChatStateContainer,
        subagent_continuation_context: SubagentContinuationContext | None,
    ) -> tuple[Any, Optional[Mapping[str, Any]], dict[str, Any] | None]:
        """Compile and execute one checkpoint continuation with live dependencies."""
        executor_should_cancel = should_cancel
        if subagent_continuation_context is not None:
            child_cancel_probe = AsyncCancellationProbe(
                lambda: is_subagent_continuation_cancel_requested(
                    registry=self._require_agent_run_registry(),
                    context=subagent_continuation_context,
                )
            )

            def combined_should_cancel() -> bool:
                return bool(should_cancel and should_cancel()) or child_cancel_probe()

            executor_should_cancel = combined_should_cancel
        attribution = build_subagent_continuation_attribution(
            subagent_continuation_context,
            subagent_registry=self._subagent_registry,
        )
        async with self._checkpointer_service.get_checkpointer(task_id) as checkpointer:
            compiled = await self._compile_graph_for_name(
                task_id=task_id,
                graph_name=graph_name,
                checkpointer=checkpointer,
                subagent_agent_id=(
                    subagent_continuation_context.entry.agent_id
                    if subagent_continuation_context is not None
                    else None
                ),
            )
            runtime_dependency_cleanup: Optional[Callable[[], None]] = None
            try:
                checkpoint_hint = await self._load_checkpoint_runtime_hint(
                    compiled=compiled,
                    task_id=task_id,
                    graph_thread_id=graph_thread_id,
                    graph_name=graph_name,
                    checkpoint_id=checkpoint_id,
                )
                (
                    llm_runtime_selection,
                    runtime_services,
                    runtime_dependency_cleanup,
                ) = self._prepare_runtime_dependencies(
                    user_id=user_id,
                    llm_runtime_selection=llm_runtime_selection,
                    runtime_services=runtime_services,
                    checkpoint_hint=checkpoint_hint,
                )
                config = self._build_checkpoint_execution_config(
                    task_id=task_id,
                    graph_name=graph_name,
                    graph_thread_id=graph_thread_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    runtime_placement_mode=runtime_placement_mode,
                    workspace_id=workspace_id,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    runner_id=runner_id,
                    execution_site_id=execution_site_id,
                    llm_runtime_selection=llm_runtime_selection,
                    runtime_services=runtime_services,
                    checkpoint_id=checkpoint_id,
                    interrupt_id=interrupt_id,
                    approval_received_at=approval_received_at,
                    resume_worker_start_at=resume_worker_start_at,
                    retry_context=retry_context,
                )
                if attribution is not None:
                    config["configurable"].update(attribution)
                if resume_worker_start_at is not None:
                    logger.info(
                        "[HITL] Continue using checkpoint_id=%s for task %s",
                        config["configurable"].get("checkpoint_id", "latest"),
                        task_id,
                    )
                logger.info("[HITL] Continuing graph=%s for task %s", graph_name, task_id)
                execution_result = await self._executor.stream_graph(
                    compiled,
                    graph_input,
                    config,
                    task_id,
                    state_container=state_container,
                    should_cancel=executor_should_cancel,
                )
                return execution_result, llm_runtime_selection, attribution
            finally:
                if runtime_dependency_cleanup is not None:
                    runtime_dependency_cleanup()

    def _prepare_runtime_dependencies(
        self,
        *,
        user_id: Optional[int],
        llm_runtime_selection: Optional[Mapping[str, Any]],
        runtime_services: Any,
        checkpoint_hint: Optional[Mapping[str, Any]] = None,
    ) -> tuple[Optional[Mapping[str, Any]], Any, Optional[Callable[[], None]]]:
        """Rebuild live provider runtime dependencies for continuation runs."""

        if user_id is None:
            return llm_runtime_selection, runtime_services, None
        if (
            checkpoint_hint is None
            and llm_runtime_selection is not None
            and runtime_services is not None
        ):
            return llm_runtime_selection, runtime_services, None

        db = SessionLocal()
        keep_db_open = False

        def cleanup() -> None:
            try:
                db.close()
            except Exception:
                pass

        try:
            runtime_config_service = LLMRuntimeConfigService(db)
            if checkpoint_hint is not None:
                try:
                    selection = runtime_config_service.build_continuation_selection(
                        user_id=user_id,
                        checkpoint_hint=checkpoint_hint,
                    )
                    llm_runtime_selection = selection.to_dict()
                except CredentialNotFoundError:
                    raise
                except ProviderConfigurationError:
                    logger.info(
                        "[HITL] Checkpoint runtime hint is not runnable; "
                        "continuation requires user reselection",
                        exc_info=True,
                    )
                    raise
            elif llm_runtime_selection is None:
                selection = runtime_config_service.build_continuation_selection(
                    user_id=user_id,
                )
                llm_runtime_selection = selection.to_dict()
            if runtime_services is None:
                runtime_services = runtime_config_service.build_runtime_services()
                keep_db_open = True
            if not keep_db_open:
                cleanup()
            return (
                llm_runtime_selection,
                runtime_services,
                cleanup if keep_db_open else None,
            )
        except Exception:
            cleanup()
            raise

    async def _load_checkpoint_runtime_hint(
        self,
        *,
        compiled: Any,
        task_id: int,
        graph_thread_id: Optional[str],
        graph_name: str,
        checkpoint_id: Optional[int | str],
    ) -> Optional[Dict[str, Any]]:
        """Read non-secret provider/model hints from checkpoint state."""

        state_reader = getattr(compiled, "aget_state", None)
        use_async_reader = callable(state_reader)
        if not use_async_reader:
            state_reader = getattr(compiled, "get_state", None)
        if not callable(state_reader):
            return None

        config = self._build_checkpoint_execution_config(
            task_id=task_id,
            graph_name=graph_name,
            graph_thread_id=graph_thread_id,
            checkpoint_id=checkpoint_id,
        )
        try:
            snapshot_or_awaitable = state_reader(config)
            snapshot = (
                await snapshot_or_awaitable
                if inspect.isawaitable(snapshot_or_awaitable)
                else snapshot_or_awaitable
            )
        except Exception:
            logger.debug(
                "[HITL] Failed to read checkpoint runtime hint for task %s",
                task_id,
                exc_info=True,
            )
            return None

        values = getattr(snapshot, "values", None)
        if values is None and isinstance(snapshot, Mapping):
            values = snapshot.get("values") or snapshot
        return self._extract_checkpoint_runtime_hint(values)

    @staticmethod
    def _extract_checkpoint_runtime_hint(values: Any) -> Optional[Dict[str, Any]]:
        """Resolve the authoritative non-secret runtime identity from state."""

        return resolve_checkpoint_runtime_selection(values)

    async def _compile_graph_for_name(
        self,
        *,
        task_id: int,
        graph_name: str,
        checkpointer: Any,
        subagent_agent_id: str | None = None,
    ) -> Any:
        """Compile the requested graph against the provided checkpointer.

        Args:
            task_id: Task id for signature compatibility.
            graph_name: Graph name to compile.
            checkpointer: Checkpointer instance.

        Returns:
            Compiled LangGraph graph.
        """
        from agent.graph.builders.deep_reasoning_builder import (
            compile_deep_reasoning_graph,
        )
        from agent.graph.builders.simple_tool_builder import build_simple_tool_graph
        from agent.graph.builders.parent_handoff_builder import (
            build_parent_handoff_graph,
        )
        from agent.subagents.runtime.graph import build_subagent_graph

        if E2E_DETERMINISTIC_MODE or graph_name == GRAPH_NAME_INTERRUPT_RESUME:
            return get_scenario_graph(GRAPH_NAME_INTERRUPT_RESUME, checkpointer)
        if graph_name == GRAPH_NAME_DEEP_REASONING:
            return compile_deep_reasoning_graph(checkpointer=checkpointer)
        if graph_name == GRAPH_NAME_PARENT_HANDOFF:
            return build_parent_handoff_graph(checkpointer=checkpointer)
        if is_subagent_graph_name(graph_name):
            definition_registry = self._subagent_registry
            if subagent_agent_id is None:
                definitions = definition_registry.definitions()
                if len(definitions) != 1:
                    raise RuntimeError(
                        "Subagent graph continuation requires registered agent identity"
                    )
                definition = definitions[0]
            else:
                definition = definition_registry.require(subagent_agent_id)
            return build_subagent_graph(definition, checkpointer=checkpointer)
        return build_simple_tool_graph(checkpointer=checkpointer)

    async def _prepare_subagent_resume_context(
        self,
        *,
        task_id: int,
        tenant_id: Optional[int],
        graph_name: str,
        interrupt_id: Optional[str],
        checkpoint_id: Optional[int | str],
    ) -> SubagentContinuationContext | None:
        if not is_subagent_graph_name(graph_name):
            return None
        try:
            return await prepare_subagent_resume(
                registry=self._require_agent_run_registry(),
                task_id=task_id,
                tenant_id=tenant_id,
                graph_name=graph_name,
                interrupt_id=interrupt_id,
                checkpoint_id=checkpoint_id,
            )
        except SubagentContinuationError:
            raise

    def _require_agent_run_registry(self) -> ProcessLocalAgentRunRegistry:
        if self._agent_run_registry is None:
            raise SubagentContinuationError(
                "Subagent resume requires the process-local agent-run registry"
            )
        return self._agent_run_registry

    def _require_agent_run_lifecycle_publisher(
        self,
    ) -> Callable[[int, dict[str, Any]], Awaitable[None]]:
        """Return the configured publisher for a verified subagent continuation."""
        if self._agent_run_lifecycle_publisher is None:
            raise RuntimeError(
                "Subagent continuation requires an agent-run lifecycle publisher"
            )
        return self._agent_run_lifecycle_publisher


__all__ = [
    "CheckpointContinuationService",
    "extract_resume_conversation_id",
    "resolve_resume_turn_number",
]
