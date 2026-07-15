"""Service that continues a LangGraph run from persisted checkpoint state.

Owns the resume-from-interrupt and retry-from-checkpoint flows. Both share
the same inner continuation: build run config, compile graph, stream graph,
parse final state, hydrate container if needed, persist, build result.
"""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, Mapping, Optional

from backend.config import E2E_DETERMINISTIC_MODE
from backend.database import SessionLocal
from backend.services.chat.message_service import ChatMessageService
from backend.services.langgraph_chat.contracts import LangGraphChatResult
from backend.services.langgraph_chat.exceptions import HITLError
from backend.services.langgraph_chat.hitl_constants import (
    DEFAULT_GRAPH_NAME,
    GRAPH_NAME_DEEP_REASONING,
    GRAPH_NAME_INTERRUPT_RESUME,
    GRAPH_NAME_SIMPLE_TOOL,
)
from backend.services.langgraph_chat.execution.scenario_factory import get_scenario_graph
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
            HITLError: If resume fails.
        """
        from langgraph.types import Command

        graph_name = graph_name or DEFAULT_GRAPH_NAME
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
            )
        except Exception as exc:
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

        Returns:
            LangGraphChatResult from continued execution.

        Raises:
            HITLError: If continuation cannot produce/parse final state.
        """
        from agent.graph import InteractiveState
        from backend.services.chat.event_builders import attach_conversation_ids

        state_container = ChatStateContainer(reserved_message_id=reserved_message_id)

        async with self._checkpointer_service.get_checkpointer(task_id) as checkpointer:
            compiled = await self._compile_graph_for_name(
                task_id=task_id,
                graph_name=graph_name,
                checkpointer=checkpointer,
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
                    should_cancel=should_cancel,
                )
            finally:
                if runtime_dependency_cleanup is not None:
                    runtime_dependency_cleanup()

        if not execution_result.final_state:
            if execution_result.interrupted:
                msg = f"[HITL] Continuation interrupt missing state for task {task_id}"
            else:
                msg = f"[HITL] Continuation did not capture final state for task {task_id}"
            logger.error(msg)
            raise HITLError(msg)

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
            logger.info(
                "[HITL] Continuation hit another interrupt for task %s", task_id
            )
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
            )

            interrupt_metadata = {"interrupt": True, "graph_name": graph_name}
            if isinstance(execution_result.metadata, dict):
                interrupt_metadata.update(execution_result.metadata)
            if isinstance(llm_runtime_selection, Mapping):
                interrupt_metadata["llm_runtime_selection"] = dict(llm_runtime_selection)

            return LangGraphChatResult(
                final_text=None,
                conversation_id=None,
                metadata=interrupt_metadata,
                persistence_handled=True,
            )

        logger.info(
            "[HITL] Continuation completed for task %s, parsing state...", task_id
        )
        # Success path: parse failure raised above for non-interrupt paths,
        # so ``interactive_state`` is guaranteed non-None here.
        assert interactive_state is not None

        final_text = (
            interactive_state.trace.final_text or interactive_state.facts.message
        )
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
            final_message=final_text,
            error=None,
            reason=success_persist_reason,
            conversation_id=conversation_id or "",
            turn_number=turn_number,
            replace_turn_events=replace_turn_events,
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
        if isinstance(llm_runtime_selection, Mapping):
            metadata["llm_runtime_selection"] = dict(llm_runtime_selection)

        from backend.services.langgraph_chat.handlers.normal_chat_handler import (
            _extract_usage_from_state,
        )

        # Map the HITL graph_name to the canonical branch label used by the
        # non-interrupt handlers. graph_name mirrors the branch name for
        # deep_reasoning/simple_tool; anything else (including the E2E
        # "interrupt_resume" scenario) falls back to "unknown" so downstream
        # metadata stays honest rather than silently claiming simple_chat.
        resume_execution_branch = (
            GRAPH_NAME_DEEP_REASONING
            if graph_name == GRAPH_NAME_DEEP_REASONING
            else GRAPH_NAME_SIMPLE_TOOL
            if graph_name == GRAPH_NAME_SIMPLE_TOOL
            else "unknown"
        )
        usage = _extract_usage_from_state(
            interactive_state,
            execution_branch=resume_execution_branch,
            turn_index=turn_number if isinstance(turn_number, int) else None,
        )
        if usage:
            logger.info(
                "[HITL] Extracted %s usage records for task %s, total_tokens=%s",
                len(usage),
                task_id,
                sum(entry.usage.total_tokens for entry in usage),
            )

        result_obj = self._build_result(
            final_text=final_text,
            conversation_id=conversation_id,
            interactive_state=interactive_state,
            metadata=metadata,
            events=[],
            turn_id=turn_id,
            usage=usage,
        )
        result_obj.persistence_handled = True
        return result_obj

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
                        "[HITL] Ignoring invalid checkpoint runtime hint for task "
                        "continuation; resolving current user selection"
                    )
                    if llm_runtime_selection is None:
                        selection = runtime_config_service.build_continuation_selection(
                            user_id=user_id,
                        )
                        llm_runtime_selection = selection.to_dict()
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

    @classmethod
    def _extract_checkpoint_runtime_hint(cls, values: Any) -> Optional[Dict[str, Any]]:
        """Extract the provider/model continuation hint from checkpoint values."""

        if not isinstance(values, Mapping):
            return None

        candidates: list[Mapping[str, Any]] = []
        cls._append_runtime_hint_candidates(candidates, values)
        facts = values.get("facts")
        if isinstance(facts, Mapping):
            cls._append_runtime_hint_candidates(candidates, facts)
            metadata = facts.get("metadata")
        else:
            metadata = getattr(facts, "metadata", None)
        if isinstance(metadata, Mapping):
            cls._append_runtime_hint_candidates(candidates, metadata)

        for candidate in candidates:
            hint = cls._sanitize_runtime_hint(candidate)
            if hint:
                return hint
        return None

    @staticmethod
    def _append_runtime_hint_candidates(
        candidates: list[Mapping[str, Any]],
        source: Mapping[str, Any],
    ) -> None:
        """Collect possible runtime hint mappings from a checkpoint payload."""

        candidates.append(source)
        for key in ("llm_runtime_selection", "graph_runtime_context"):
            value = source.get(key)
            if isinstance(value, Mapping):
                candidates.append(value)

    @staticmethod
    def _sanitize_runtime_hint(source: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """Return only non-secret runtime hint fields used for fresh resolution."""

        hint: Dict[str, Any] = {}
        for key in ("provider", "model", "reasoning_effort"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                hint[key] = value.strip()
        return hint or None

    async def _compile_graph_for_name(
        self,
        *,
        task_id: int,
        graph_name: str,
        checkpointer: Any,
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

        if E2E_DETERMINISTIC_MODE or graph_name == GRAPH_NAME_INTERRUPT_RESUME:
            return get_scenario_graph(GRAPH_NAME_INTERRUPT_RESUME, checkpointer)
        if graph_name == GRAPH_NAME_DEEP_REASONING:
            return compile_deep_reasoning_graph(checkpointer=checkpointer)
        return build_simple_tool_graph(checkpointer=checkpointer)


__all__ = [
    "CheckpointContinuationService",
    "extract_resume_conversation_id",
    "resolve_resume_turn_number",
]
