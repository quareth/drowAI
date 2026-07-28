"""Facade handler for process-local Scout recon runs and parent handoff.

The handler keeps the original parent turn open while Scout executes. Scout
streams its own attributed events, returns a bounded ``AgentResult``, and the
handler projects that result into the parent context before running the existing
main finalizer. Scout graph execution and lifecycle cleanup remain launcher
responsibilities.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import uuid4

from agent.graph import InteractiveState
from agent.graph.builders.parent_handoff_builder import build_parent_handoff_graph
from agent.graph.graph_names import GRAPH_NAME_SIMPLE_TOOL
from agent.graph.streaming import build_agent_turn_metadata
from backend.services.agent_runs.contracts import (
    AgentAssignment,
    AgentCredentialReference,
    AgentResult,
    AgentRuntimeIdentity,
    ReconCapability,
    agent_display_name,
)
from backend.services.agent_runs.event_projection import build_agent_run_lifecycle_event
from backend.services.agent_runs.execution_config import build_child_execution_config
from backend.services.agent_runs.launcher import (
    AgentRunLauncher,
    ScoutRunPaused,
    ScoutRunWorker,
)
from backend.services.agent_runs.result_projection import (
    AgentRunResultProjector,
    CompletedAgentResultHandoff,
    attach_completed_agent_results_to_context,
)
from backend.services.agent_runs.registry import (
    LocalAgentRun,
    ProcessLocalAgentRunRegistry,
)
from backend.services.agent_runs.scout_worker import ProcessLocalScoutRunWorker
from backend.services.chat.event_builders import attach_conversation_ids
from backend.services.langgraph_chat.contracts import (
    ExecutionMode,
    LangGraphChatResult,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.execution.completion_callback import (
    StreamEmitter,
    run_turn_with_completion_callback,
)
from backend.services.langgraph_chat.checkpoint.thread_identity import (
    generate_graph_thread_id,
)
from backend.services.langgraph_chat.facade_helpers import (
    build_result,
    build_thread_config,
)
from backend.services.llm_provider.runtime_services import attach_runtime_services

from .base_handler import BaseLangGraphHandler
from .normal_chat_handler import _extract_usage_from_state
from .turn_runtime import (
    apply_agent_thread_config,
    build_cancelled_result,
    build_initial_interactive_state,
    build_or_reuse_state_container,
    drain_completion_callback,
    ensure_turn_identity,
    merge_execution_metadata,
    new_captured_state,
    parse_interactive_state_from_final,
    prefill_reasoning_tokens_from,
    record_execution_metadata,
)

logger = logging.getLogger(__name__)


LifecyclePublisher = Callable[[int, dict[str, Any]], Awaitable[None]]


class ReconAgentHandler(BaseLangGraphHandler):
    """Run Scout and finalize its bounded result in the original parent turn."""

    def __init__(
        self,
        *args: Any,
        registry: ProcessLocalAgentRunRegistry,
        launcher: Any = None,
        worker: ScoutRunWorker | None = None,
        lifecycle_publisher: LifecyclePublisher | None = None,
        result_projector: AgentRunResultProjector | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._publish_lifecycle = lifecycle_publisher or _publish_lifecycle_to_hub
        self._registry = registry
        self._result_projector = result_projector or AgentRunResultProjector(
            registry=registry
        )
        if launcher is not None:
            self._launcher = launcher
        else:
            resolved_worker = worker or ProcessLocalScoutRunWorker(
                registry=registry,
                checkpointer_service=self._checkpointer,
                executor=self._executor,
            )
            self._launcher = AgentRunLauncher(
                registry=registry,
                worker=resolved_worker,
                lifecycle_publisher=self._publish_lifecycle,
            )

    async def handle(
        self, runtime_config: LangGraphRuntimeConfig
    ) -> LangGraphChatResult:
        """Run Scout, hand its bounded result to the parent, then finalize."""
        chat_inputs = runtime_config.chat_inputs
        task_id = chat_inputs.task_id
        turn = ensure_turn_identity(runtime_config, logger_=logger)

        assignment = _build_assignment(runtime_config, parent_turn_id=str(turn.turn_id))
        child_graph_thread_id = _new_child_graph_thread_id()
        queued = await self._registry.register(
            assignment,
            graph_thread_id=child_graph_thread_id,
        )
        await self._publish_entry_lifecycle(queued, runtime_config)

        try:
            child_runtime_config = await build_child_execution_config(
                assignment=assignment,
                runtime_config=runtime_config,
                registry=self._registry,
                graph_thread_id=child_graph_thread_id,
            )
            if runtime_config.runtime_services is not None:
                child_runtime_config = attach_runtime_services(
                    child_runtime_config,
                    runtime_config.runtime_services,
                )
            running = await self._registry.mark_running(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
            )
            await self._publish_entry_lifecycle(running, runtime_config)
            child_task = await self._launcher.launch(
                assignment=assignment,
                runtime_config=child_runtime_config,
                graph_thread_id=child_graph_thread_id,
                parent_run_id=_parent_run_id(runtime_config.metadata),
            )
        except Exception as exc:
            logger.warning(
                "Failed to launch Scout run %s for task %s",
                assignment.agent_run_id,
                task_id,
                exc_info=True,
            )
            failed = await self._registry.mark_failed(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
                safe_error=_safe_launch_error(exc),
            )
            await self._publish_entry_lifecycle(failed, runtime_config)
            return _ack_result(
                runtime_config,
                turn_id=str(turn.turn_id),
                turn_sequence=turn.turn_number if isinstance(turn.turn_number, int) else None,
                agent_run_id=assignment.agent_run_id,
                graph_thread_id=child_graph_thread_id,
                status="failed",
            )

        try:
            child_result = await _require_child_task(child_task)
        except ScoutRunPaused:
            return _ack_result(
                runtime_config,
                turn_id=str(turn.turn_id),
                turn_sequence=(
                    turn.turn_number if isinstance(turn.turn_number, int) else None
                ),
                agent_run_id=assignment.agent_run_id,
                graph_thread_id=child_graph_thread_id,
                status="waiting_for_approval",
            )
        except asyncio.CancelledError:
            return _ack_result(
                runtime_config,
                turn_id=str(turn.turn_id),
                turn_sequence=(
                    turn.turn_number if isinstance(turn.turn_number, int) else None
                ),
                agent_run_id=assignment.agent_run_id,
                graph_thread_id=child_graph_thread_id,
                status="cancelled",
            )
        except Exception:
            logger.warning(
                "Scout run %s failed before parent handoff for task %s",
                assignment.agent_run_id,
                task_id,
                exc_info=True,
            )
            return _ack_result(
                runtime_config,
                turn_id=str(turn.turn_id),
                turn_sequence=(
                    turn.turn_number if isinstance(turn.turn_number, int) else None
                ),
                agent_run_id=assignment.agent_run_id,
                graph_thread_id=child_graph_thread_id,
                status="failed",
            )

        handoff = CompletedAgentResultHandoff(
            results=(self._result_projector.project_result(child_result),),
            agent_run_ids=(assignment.agent_run_id,),
        )
        attach_completed_agent_results_to_context(runtime_config.metadata, handoff)
        parent_result = await self._finalize_parent_handoff(
            runtime_config,
            turn=turn,
            child_result=child_result,
            child_graph_thread_id=child_graph_thread_id,
        )
        await self._consume_completed_handoff(
            assignment=assignment,
            handoff=handoff,
        )
        return parent_result

    async def _finalize_parent_handoff(
        self,
        runtime_config: LangGraphRuntimeConfig,
        *,
        turn: Any,
        child_result: AgentResult,
        child_graph_thread_id: str,
    ) -> LangGraphChatResult:
        """Run the canonical main finalizer over one completed child result."""
        chat_inputs = runtime_config.chat_inputs
        task_id = chat_inputs.task_id
        initial_state, _injected_tokens = build_initial_interactive_state(
            runtime_config
        )
        starting_state = InteractiveState.from_mapping(initial_state)

        config = build_thread_config(runtime_config, task_id)
        thread_id = apply_agent_thread_config(
            config,
            task_id=task_id,
            graph_name=GRAPH_NAME_SIMPLE_TOOL,
            turn=turn,
            conversation_id=chat_inputs.conversation_id,
        )
        graph_input = starting_state.as_graph_state()
        captured_state = new_captured_state()
        reserved_message_id = turn.metadata.get("reserved_message_id")
        state_container = build_or_reuse_state_container(
            runtime_config,
            reserved_message_id=reserved_message_id,
        )
        result_holder: dict[str, Any] = {}
        cancellation_checker = self._build_cancellation_checker(
            task_id,
            str(turn.turn_id),
        )

        async def execute_graph(
            emitter: StreamEmitter,
            callback_result_holder: dict[str, Any],
        ) -> str:
            _ = (emitter, callback_result_holder)
            execution_result = await self._executor.stream_graph(
                build_parent_handoff_graph(),
                graph_input,
                config,
                task_id,
                state_container=state_container,
                should_cancel=cancellation_checker,
            )
            record_execution_metadata(captured_state, execution_result.metadata)
            interactive_state = parse_interactive_state_from_final(
                final_state=execution_result.final_state,
                starting_state=starting_state,
                deterministic_mode=False,
                state_container=state_container,
                task_id=task_id,
                missing_state_message=(
                    f"Parent finalizer did not capture final state for task {task_id}"
                ),
            )
            captured_state["final_state"] = execution_result.final_state
            captured_state["interactive_state"] = interactive_state
            return interactive_state.trace.final_text or interactive_state.facts.message

        await drain_completion_callback(
            callback_runner=run_turn_with_completion_callback,
            turn=turn,
            task_id=task_id,
            conversation_id=chat_inputs.conversation_id or "",
            llm_func=execute_graph,
            should_cancel=cancellation_checker,
            state_container=state_container,
            reserved_message_id=reserved_message_id,
            result_holder=result_holder,
            prefill_reasoning_tokens=prefill_reasoning_tokens_from(turn.metadata),
        )

        if result_holder.get("cancelled") is True:
            return build_cancelled_result(
                chat_inputs=chat_inputs,
                thread_id=thread_id,
                graph_name=GRAPH_NAME_SIMPLE_TOOL,
                captured_state=captured_state,
            )

        interactive_state = captured_state["interactive_state"]
        if not isinstance(interactive_state, InteractiveState):
            raise RuntimeError(
                f"Parent finalizer did not capture interactive state for task {task_id}"
            )
        final_text = interactive_state.trace.final_text or interactive_state.facts.message
        interactive_state.trace.final_text = final_text

        result_metadata = attach_conversation_ids(
            {
                "role": "assistant",
                "streaming": False,
                "mode": ExecutionMode.SIMPLE_TOOL.value,
                "branch": "recon_agent",
                "status": "completed",
                "handoff_agent_run_id": child_result.agent_run_id,
                "handoff_agent_kind": child_result.agent_kind,
                "handoff_graph_thread_id": child_graph_thread_id,
            },
            chat_inputs.conversation_id or "",
        )
        merge_execution_metadata(result_metadata, captured_state)
        for key, value in build_agent_turn_metadata(interactive_state).items():
            if value is not None:
                result_metadata[key] = value

        usage = _extract_usage_from_state(
            interactive_state,
            execution_branch="recon_agent_parent_finalizer",
            turn_index=(
                turn.turn_number if isinstance(turn.turn_number, int) else None
            ),
        )
        result = build_result(
            final_text=final_text,
            conversation_id=chat_inputs.conversation_id,
            interactive_state=interactive_state,
            metadata=result_metadata,
            events=[],
            turn_id=turn.turn_id,
            usage=usage,
        )
        result.persistence_handled = True
        return result

    async def _consume_completed_handoff(
        self,
        *,
        assignment: AgentAssignment,
        handoff: CompletedAgentResultHandoff,
    ) -> None:
        """Consume the registry result after the parent finalizer succeeds."""
        for _ in range(100):
            entry = await self._registry.get(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
            )
            if entry is not None and entry.status == "completed":
                await self._result_projector.mark_consumed(
                    tenant_id=assignment.tenant_id,
                    task_id=assignment.task_id,
                    handoff=handoff,
                )
                return
            await asyncio.sleep(0)
        logger.debug(
            "Scout result %s was not registry-settled after parent finalization",
            assignment.agent_run_id,
        )

    async def _publish_entry_lifecycle(
        self,
        entry: LocalAgentRun,
        runtime_config: LangGraphRuntimeConfig,
    ) -> None:
        event = build_agent_run_lifecycle_event(
            entry,
            parent_run_id=_parent_run_id(runtime_config.metadata),
        )
        await self._publish_lifecycle(entry.task_id, event)


async def _require_child_task(value: Any) -> AgentResult:
    """Await and validate the launcher's terminal Scout result."""
    if not isinstance(value, Awaitable):
        raise RuntimeError("Scout launcher did not return an awaitable result task")
    result = await value
    if not isinstance(result, AgentResult):
        raise RuntimeError("Scout launcher returned an invalid terminal result")
    return result


async def _publish_lifecycle_to_hub(task_id: int, event: dict[str, Any]) -> None:
    """Publish lifecycle events through the existing task stream hub."""
    from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub

    await get_in_memory_stream_hub().publish(task_id, event)


def _build_assignment(
    runtime_config: LangGraphRuntimeConfig,
    *,
    parent_turn_id: str,
) -> AgentAssignment:
    metadata = runtime_config.metadata
    chat_inputs = runtime_config.chat_inputs
    ownership = metadata.get("subagent_routing")
    if not isinstance(ownership, Mapping) or not ownership.get("should_delegate"):
        raise RuntimeError("Recon agent branch requires a positive ownership decision")
    if ownership.get("subagent_name") != "scout":
        raise RuntimeError("Recon agent branch requires the Scout registration")
    if ownership.get("agent_kind") != "recon":
        raise RuntimeError("Recon agent branch requires recon agent kind")

    tenant_id = _required_int(metadata.get("tenant_id"), "tenant_id")
    task_id = int(chat_inputs.task_id)
    agent_run_id = _new_agent_run_id()
    runtime_identity = AgentRuntimeIdentity(
        tenant_id=tenant_id,
        task_id=task_id,
        user_id=chat_inputs.user_id,
        workspace_id=_required_string(metadata.get("workspace_id"), "workspace_id"),
        workspace_path=_optional_string(metadata.get("workspace_path")),
        runtime_placement_mode=_required_string(
            metadata.get("runtime_placement_mode"),
            "runtime_placement_mode",
        ),
        actor_type=_required_string(metadata.get("actor_type"), "actor_type"),
        actor_id=_required_string(metadata.get("actor_id"), "actor_id"),
        runner_id=_optional_string(metadata.get("runner_id")),
        execution_site_id=_optional_string(metadata.get("execution_site_id")),
        provider=_optional_string(chat_inputs.provider or metadata.get("provider")),
        model=_optional_string(chat_inputs.model or metadata.get("runtime_model")),
        reasoning_effort=_optional_string(chat_inputs.reasoning_effort),
        feature_flags=_assignment_feature_flags(metadata),
        credential_ref=_credential_ref_from_input(chat_inputs.credential_ref),
    )
    return AgentAssignment(
        assignment_id=f"assignment-{uuid4().hex}",
        agent_run_id=agent_run_id,
        agent_kind="recon",
        task_id=task_id,
        tenant_id=tenant_id,
        conversation_id=_required_string(
            chat_inputs.conversation_id,
            "conversation_id",
        ),
        parent_turn_id=parent_turn_id,
        parent_graph_thread_id=_required_string(
            metadata.get("graph_thread_id"),
            "graph_thread_id",
        ),
        objective=_optional_string(ownership.get("objective")) or chat_inputs.message,
        targets=tuple(_string_list(ownership.get("targets"))),
        suggested_capabilities=tuple(
            _recon_capabilities(ownership.get("capabilities"))
        ),
        scope_summary=_scope_summary(ownership.get("targets")),
        relevant_context={
            "classifier_label": _optional_string(
                metadata.get("intent_classifier_label")
            )
            or _optional_string(
                (metadata.get("intent_hints") or {}).get("classifier_label")
                if isinstance(metadata.get("intent_hints"), Mapping)
                else None
            ),
            "ownership_reason": _optional_string(ownership.get("reason")),
            "parent_run_id": _parent_run_id(metadata),
            "turn_sequence": metadata.get("turn_sequence"),
        },
        runtime_identity=runtime_identity,
    )


def _ack_result(
    runtime_config: LangGraphRuntimeConfig,
    *,
    turn_id: str,
    turn_sequence: int | None,
    agent_run_id: str,
    graph_thread_id: str,
    status: str,
) -> LangGraphChatResult:
    conversation_id = runtime_config.chat_inputs.conversation_id
    metadata = attach_conversation_ids(
        {
            "role": "assistant",
            "streaming": False,
            "mode": ExecutionMode.SIMPLE_TOOL.value,
            "branch": "recon_agent",
            "agent_run_id": agent_run_id,
            "agent_kind": "recon",
            "agent_display_name": agent_display_name("recon"),
            "graph_thread_id": graph_thread_id,
            "status": status,
            "id": turn_id,
        },
        conversation_id or "",
    )
    if turn_sequence is not None:
        metadata["turn_sequence"] = turn_sequence
    display_name = agent_display_name("recon")
    return LangGraphChatResult(
        final_text={
            "failed": f"{display_name} could not complete the recon run.",
            "cancelled": f"{display_name} recon was cancelled.",
            "waiting_for_approval": f"{display_name} is waiting for tool approval.",
            "running": (
                f"{display_name} has started a recon run and will hand off findings "
                "when it finishes."
            ),
        }.get(status, f"{display_name} recon status changed."),
        conversation_id=conversation_id,
        metadata=metadata,
    )


def _assignment_feature_flags(metadata: Mapping[str, Any]) -> dict[str, bool]:
    flags = metadata.get("feature_flags")
    return {
        str(key): bool(value)
        for key, value in (flags.items() if isinstance(flags, Mapping) else ())
        if isinstance(key, str)
    }


def _credential_ref_from_input(value: Any) -> AgentCredentialReference | None:
    if not isinstance(value, Mapping):
        return None
    provider = _optional_string(value.get("provider"))
    credential_id = _optional_string(value.get("credential_id"))
    if not provider or not credential_id:
        return None
    return AgentCredentialReference(provider=provider, credential_id=credential_id)


def _safe_launch_error(exc: Exception) -> str:
    _ = exc
    return f"{agent_display_name('recon')} launch failed"


def _new_agent_run_id() -> str:
    return f"scout-{uuid4().hex}"


def _new_child_graph_thread_id() -> str:
    return generate_graph_thread_id()


def _parent_run_id(metadata: Mapping[str, Any]) -> str | None:
    for key in ("parent_run_id", "run_id", "turn_id"):
        value = _optional_string(metadata.get(key))
        if value:
            return value
    return None


def _scope_summary(value: Any) -> str | None:
    targets = _string_list(value)
    if not targets:
        return None
    return "Targets: " + ", ".join(targets)


def _recon_capabilities(value: Any) -> list[ReconCapability]:
    allowed = {"host_discovery", "port_scan", "service_enum"}
    return [
        capability
        for capability in _string_list(value)
        if capability in allowed
    ]  # type: ignore[list-item]


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list | tuple):
        values = list(value)
    else:
        values = []
    return [str(item).strip() for item in values if str(item).strip()]


def _required_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Recon assignment requires {field_name}") from exc


def _required_string(value: Any, field_name: str) -> str:
    normalized = _optional_string(value)
    if not normalized:
        raise RuntimeError(f"Recon assignment requires {field_name}")
    return normalized


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


__all__ = [
    "ReconAgentHandler",
    "build_agent_run_lifecycle_event",
]
