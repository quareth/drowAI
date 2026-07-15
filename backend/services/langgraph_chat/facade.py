"""Thin orchestration layer for LangGraph-backed chat execution."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Any, Callable, Dict, Mapping, Optional

from agent.context.token_counter_registry import estimate_llm_request_tokens
from agent.graph.context.builder import METADATA_CONTEXT_BUNDLE_KEY
from agent.graph.context.contracts import CLASSIFIER_TRANSCRIPT_WINDOW_KEY
from agent.graph.context.transcript import select_full_transcript_window
from backend.database import SessionLocal
from backend.services.chat.conversation_history_reader import ConversationHistoryReader

from backend.config import (
    ENABLE_LANGGRAPH_DEEP_REASONING,
    ENABLE_LANGGRAPH_SIMPLE_TOOL,
)
from core.llm import LLM_TIMEOUT_INTENT_CLASSIFIER_SEC

from backend.services.langgraph_chat.checkpoint.checkpointer_service import (
    CheckpointerService,
    get_shared_checkpointer_service,
)
from backend.services.langgraph_chat.checkpoint import continuation_service
from backend.services.langgraph_chat.checkpoint.continuation_service import (
    CheckpointContinuationService,
)
from backend.services.langgraph_chat.checkpoint.execution_config import (
    build_checkpoint_execution_config,
)
from backend.services.langgraph_chat.checkpoint.state_hydrator import (
    hydrate_container_from_checkpoint_state,
)
from backend.services.langgraph_chat import facade_helpers
from backend.services.langgraph_chat.compression.turn_service import (
    TurnCompressionService,
)
from backend.services.langgraph_chat.execution import completion_callback
from .context_builder import LangGraphContextBuilder
from .contracts import (
    ChatInputs,
    LangGraphChatResult,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.runtime.state_container import ChatStateContainer
from backend.services.langgraph_chat.execution.graph_executor import LangGraphExecutor
from .handlers import DeepReasoningHandler, NormalChatHandler, SimpleToolHandler
from backend.services.langgraph_chat.intent.briefs import (
    ensure_intent_brief_seed_present,
)
from backend.services.langgraph_chat.intent.classifier import (
    IntentClassifier,
    IntentClassifierRequest,
    resolve_intent_classifier_context_limit,
)
from backend.services.langgraph_chat.intent.phase_streamer import IntentPhaseStreamer
from backend.services.langgraph_chat.intent.prior_turn_references import (
    PriorTurnReferenceMaterializer,
)
from backend.services.langgraph_chat.routing.mode_policy import (
    enforce_plan_mode_availability,
)
from backend.services.langgraph_chat.routing.selectors import (
    ChatBranch,
    resolve_branch,
)
from backend.services.langgraph_chat.streaming.adapter import LangGraphStreamingAdapter

logger = logging.getLogger(__name__)


def _build_isolated_candidate_runtime_config(
    runtime_config: LangGraphRuntimeConfig,
    candidate_history: list[Dict[str, Any]],
) -> LangGraphRuntimeConfig:
    """Return a classifier-only config with a candidate transcript projection."""
    live_bundle = runtime_config.metadata.get(METADATA_CONTEXT_BUNDLE_KEY)
    if not isinstance(live_bundle, dict):
        raise RuntimeError("context bundle is required for candidate classification")
    candidate_bundle = dict(live_bundle)
    candidate_bundle[CLASSIFIER_TRANSCRIPT_WINDOW_KEY] = (
        select_full_transcript_window(candidate_history)
    )
    candidate_metadata = dict(runtime_config.metadata)
    candidate_metadata[METADATA_CONTEXT_BUNDLE_KEY] = candidate_bundle
    return replace(runtime_config, metadata=candidate_metadata)


class LangGraphChatFacade:
    """Entry point used by the backend router to run LangGraph chat turns."""

    def __init__(
        self,
        *,
        checkpointer_service: Optional[CheckpointerService] = None,
        executor: Optional[LangGraphExecutor] = None,
        context_builder: Optional[LangGraphContextBuilder] = None,
        streaming_adapter: Optional[LangGraphStreamingAdapter] = None,
        intent_classifier: Optional[IntentClassifier] = None,
        turn_compression_service: Optional[TurnCompressionService] = None,
        prior_turn_reference_materializer: Optional[
            PriorTurnReferenceMaterializer
        ] = None,
        session_factory: Optional[Callable[[], Any]] = None,
        conversation_history_reader_factory: Optional[
            Callable[[Any], ConversationHistoryReader]
        ] = None,
    ) -> None:
        """Initialize facade with injected dependencies."""
        self._checkpointer_service = (
            checkpointer_service or get_shared_checkpointer_service()
        )
        self._streaming_adapter = streaming_adapter or LangGraphStreamingAdapter()
        self._executor = executor or LangGraphExecutor(
            streaming_adapter=self._streaming_adapter
        )
        self._intent_phase_streamer = IntentPhaseStreamer(self._streaming_adapter)
        self._context_builder = context_builder or LangGraphContextBuilder()
        self._intent_classifier = intent_classifier or IntentClassifier(
            client_timeout=LLM_TIMEOUT_INTENT_CLASSIFIER_SEC,
        )
        self._turn_compression_service = turn_compression_service
        self._prior_turn_reference_materializer = (
            prior_turn_reference_materializer or PriorTurnReferenceMaterializer()
        )
        self._session_factory = session_factory or SessionLocal
        self._conversation_history_reader_factory = (
            conversation_history_reader_factory
            or (lambda db: ConversationHistoryReader(db))
        )

        self._handlers = {
            ChatBranch.NORMAL_CHAT: NormalChatHandler(
                self._checkpointer_service, self._executor, self._streaming_adapter
            ),
            ChatBranch.DEEP_REASONING: DeepReasoningHandler(
                self._checkpointer_service, self._executor, self._streaming_adapter
            ),
            ChatBranch.SIMPLE_TOOL: SimpleToolHandler(
                self._checkpointer_service, self._executor, self._streaming_adapter
            ),
        }

        self._continuation = CheckpointContinuationService(
            checkpointer_service=self._checkpointer_service,
            executor=self._executor,
            streaming_adapter=self._streaming_adapter,
            build_checkpoint_execution_config=build_checkpoint_execution_config,
            hydrate_container_from_checkpoint_state=hydrate_container_from_checkpoint_state,
            extract_resume_conversation_id=continuation_service.extract_resume_conversation_id,
            resolve_resume_turn_number=continuation_service.resolve_resume_turn_number,
            persist_chat_message_from_container=(
                completion_callback.persist_chat_message_from_container
            ),
            build_result=facade_helpers.build_result,
        )

    async def handle_turn(
        self,
        chat_inputs: ChatInputs,
        *,
        metadata: Optional[Dict[str, Any]] = None,
        runtime_services: Any = None,
        pre_classifier_context_handoff: Optional[Dict[str, Any]] = None,
    ) -> LangGraphChatResult:
        """Process a chat turn and return a LangGraph-aware result container."""
        start_time = time.perf_counter()
        logger.info(f"[FACADE] handle_turn called for task {chat_inputs.task_id}")

        config_start = time.perf_counter()
        runtime_config = self._context_builder.build_runtime_config(
            chat_inputs=chat_inputs,
            metadata=metadata,
        )
        runtime_config.runtime_services = runtime_services
        deterministic_mode = bool(runtime_config.metadata.get("deterministic_mode"))
        logger.warning(
            "[FACADE] Runtime config built for task %s in %.2f ms",
            chat_inputs.task_id,
            (time.perf_counter() - config_start) * 1000,
        )

        enforce_plan_mode_availability(
            runtime_config,
            deep_reasoning_enabled_default=ENABLE_LANGGRAPH_DEEP_REASONING,
        )

        shared_container = ChatStateContainer()
        runtime_config.persistence.state_container = shared_container

        classifier_result = None
        classifier_call_settings = None
        classifier_request = None
        async with self._intent_phase_streamer.stream(runtime_config, shared_container):
            if deterministic_mode:
                runtime_config.metadata["intent_classifier_skipped"] = (
                    "deterministic_mode"
                )
                logger.info(
                    "[FACADE] Skipping intent classifier in deterministic mode for task %s",
                    chat_inputs.task_id,
                )
            else:
                if self._turn_compression_service is not None:
                    classifier_call_settings = (
                        self._intent_classifier.resolve_call_settings(runtime_config)
                    )
                    classifier_context_limit = resolve_intent_classifier_context_limit(
                        classifier_call_settings
                    )
                    classifier_request = self._intent_classifier.prepare_request(
                        runtime_config,
                        call_settings=classifier_call_settings,
                    )
                    classifier_prompt_estimate = estimate_llm_request_tokens(
                        system_prompt=classifier_request.system_prompt,
                        user_prompt=classifier_request.user_prompt,
                        structured_output=classifier_request.structured_output,
                        provider=classifier_request.call_settings.provider,
                        model=classifier_request.call_settings.model,
                    )
                    candidate_classifier_request: Optional[
                        IntentClassifierRequest
                    ] = None
                    candidate_classifier_window: Optional[Dict[str, Any]] = None

                    def _count_candidate_classifier_prompt(
                        candidate_history: list[Dict[str, Any]],
                    ) -> int:
                        nonlocal candidate_classifier_request
                        nonlocal candidate_classifier_window
                        candidate_runtime_config = (
                            _build_isolated_candidate_runtime_config(
                                runtime_config,
                                candidate_history,
                            )
                        )
                        candidate_request = self._intent_classifier.prepare_request(
                            candidate_runtime_config,
                            call_settings=classifier_call_settings,
                        )
                        candidate_bundle = candidate_runtime_config.metadata[
                            METADATA_CONTEXT_BUNDLE_KEY
                        ]
                        candidate_classifier_request = candidate_request
                        candidate_classifier_window = candidate_bundle[
                            CLASSIFIER_TRANSCRIPT_WINDOW_KEY
                        ]
                        return estimate_llm_request_tokens(
                            system_prompt=candidate_request.system_prompt,
                            user_prompt=candidate_request.user_prompt,
                            structured_output=candidate_request.structured_output,
                            provider=candidate_request.call_settings.provider,
                            model=candidate_request.call_settings.model,
                        ).tokens

                    def _capture_context_window_snapshot(
                        snapshot: Dict[str, Any],
                    ) -> None:
                        if pre_classifier_context_handoff is not None:
                            pre_classifier_context_handoff["context_window"] = dict(
                                snapshot
                            )

                    (
                        history_for_facade,
                        context_window_metadata,
                        compression_metadata,
                        context_event_emitted,
                    ) = await self._turn_compression_service.prepare_preturn_history(
                        task_id=chat_inputs.task_id,
                        conversation_id=chat_inputs.conversation_id or "",
                        turn_id=runtime_config.metadata.get("turn_id"),
                        turn_sequence=runtime_config.metadata.get("turn_sequence"),
                        history=list(chat_inputs.history),
                        history_source_message_ids=(
                            list(chat_inputs.history_source_message_ids)
                        ),
                        model=classifier_call_settings.model,
                        context_limit_tokens=classifier_context_limit,
                        request_prompt_tokens=classifier_prompt_estimate.tokens,
                        reserved_output_tokens=classifier_request.max_tokens,
                        provider=classifier_call_settings.provider,
                        llm_runtime_selection=chat_inputs.llm_runtime_selection,
                        runtime_services=runtime_services,
                        runtime_user_id=chat_inputs.user_id,
                        candidate_classifier_prompt_counter=(
                            _count_candidate_classifier_prompt
                        ),
                        on_context_window_snapshot=(
                            _capture_context_window_snapshot
                        ),
                    )
                    if compression_metadata.get("candidate_request_fits") is True:
                        live_bundle = runtime_config.metadata.get(
                            METADATA_CONTEXT_BUNDLE_KEY
                        )
                        if not isinstance(live_bundle, dict):
                            raise RuntimeError(
                                "context bundle is required for classifier projection"
                            )
                        if (
                            candidate_classifier_request is None
                            or candidate_classifier_window is None
                        ):
                            raise RuntimeError(
                                "validated classifier candidate is unavailable"
                            )
                        live_bundle[CLASSIFIER_TRANSCRIPT_WINDOW_KEY] = (
                            candidate_classifier_window
                        )
                        classifier_request = candidate_classifier_request
                    chat_inputs.history = history_for_facade
                    runtime_config.metadata["compression"] = compression_metadata
                    runtime_config.metadata.update(
                        self._turn_compression_service.context_window_handoff_fields(
                            context_window_metadata
                        )
                    )
                    if pre_classifier_context_handoff is not None:
                        pre_classifier_context_handoff.update(
                            {
                                "context_window": context_window_metadata,
                                "compression": compression_metadata,
                                "context_event_emitted": context_event_emitted,
                            }
                        )
                intent_start = time.perf_counter()
                logger.info(
                    f"[FACADE] Calling intent classifier for task {chat_inputs.task_id}"
                )
                if classifier_call_settings is None:
                    classifier_result = (
                        await self._intent_classifier.enrich_runtime_config(runtime_config)
                    )
                else:
                    classifier_result = (
                        await self._intent_classifier.enrich_runtime_config(
                            runtime_config,
                            call_settings=classifier_call_settings,
                            prepared_request=classifier_request,
                        )
                    )
                logger.warning(
                    "[FACADE] Intent classifier completed for task %s in %.2f ms",
                    chat_inputs.task_id,
                    (time.perf_counter() - intent_start) * 1000,
                )

        if classifier_result and classifier_result.usage:
            runtime_config.metadata["_intent_classifier_usage"] = (
                classifier_result.usage
            )
            logger.debug(
                f"[FACADE] Intent classifier used {classifier_result.usage.total_tokens} tokens"
            )

        self._prior_turn_reference_materializer.materialize_for_runtime_config(
            runtime_config,
            session_factory=self._session_factory,
            history_reader_factory=self._conversation_history_reader_factory,
        )

        ensure_intent_brief_seed_present(runtime_config.metadata)

        branch = resolve_branch(
            runtime_config,
            deep_reasoning_enabled=ENABLE_LANGGRAPH_DEEP_REASONING,
            simple_tool_enabled=ENABLE_LANGGRAPH_SIMPLE_TOOL,
        )

        handler = self._handlers.get(branch)
        if not handler:
            raise NotImplementedError(f"Unsupported branch: {branch.value}")

        handler_start = time.perf_counter()
        logger.info(
            f"[FACADE] Routing to {branch.value} handler for task {chat_inputs.task_id}"
        )
        logger.info(
            f"[FACADE] agent_mode in metadata: {runtime_config.metadata.get('agent_mode')}"
        )
        result = await handler.handle(runtime_config)
        logger.warning(
            "[FACADE] Handler returned for task %s in %.2f ms, interrupted=%s",
            chat_inputs.task_id,
            (time.perf_counter() - handler_start) * 1000,
            result.metadata.get("interrupted", False),
        )
        logger.warning(
            "[FACADE] handle_turn completed for task %s in %.2f ms",
            chat_inputs.task_id,
            (time.perf_counter() - start_time) * 1000,
        )
        return result

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
        """Resume graph execution from an interrupt point."""
        return await self._continuation.resume_from_interrupt(
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
            should_cancel=should_cancel,
            replace_turn_events=replace_turn_events,
            llm_runtime_selection=llm_runtime_selection,
            runtime_services=runtime_services,
        )

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
        """Retry a failed turn from a stored checkpoint."""
        return await self._continuation.retry_from_checkpoint(
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
            checkpoint_id=checkpoint_id,
            retry_context=retry_context,
            reserved_message_id=reserved_message_id,
            should_cancel=should_cancel,
            llm_runtime_selection=llm_runtime_selection,
            runtime_services=runtime_services,
        )


__all__ = ["LangGraphChatFacade"]
