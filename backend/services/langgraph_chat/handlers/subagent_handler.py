"""Facade handler for process-local subagent runs and parent handoff.

The handler keeps the original parent turn open while the subagent executes. It
streams its own attributed events, returns a bounded ``AgentResult``, and the
handler projects that result into the parent context before running the existing
main finalizer. Subagent graph execution and lifecycle cleanup remain launcher
responsibilities.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from agent.graph import InteractiveState
from agent.graph.builders.parent_handoff_builder import build_parent_handoff_graph
from agent.graph.graph_names import GRAPH_NAME_SIMPLE_TOOL
from agent.graph.streaming import build_agent_turn_metadata
from agent.subagents.registry import SubagentRegistry, get_subagent_registry
from backend.services.agent_runs.contracts import (
    AgentAssignment,
    AgentCapability,
    AgentCredentialReference,
    AgentResult,
    AgentRuntimeIdentity,
    agent_display_name,
)
from backend.services.agent_runs.completion import (
    AgentRunCompletion,
    build_agent_run_completion,
    child_usage_records_from_state,
    usage_envelopes_from_child_records,
)
from backend.services.agent_runs.event_projection import build_agent_run_lifecycle_event
from backend.services.agent_runs.execution_config import build_child_execution_config
from backend.services.agent_runs.launcher import (
    AgentRunLauncher,
    AgentRunWorker,
    SubagentRunCancelled,
    SubagentRunFailed,
    SubagentRunPaused,
)
from backend.services.agent_runs.ownership_policy import MAX_AGENT_HANDOFFS
from backend.services.agent_runs.result_projection import (
    AgentRunResultProjector,
    CompletedAgentResultHandoff,
    attach_completed_agent_results_to_context,
)
from backend.services.agent_runs.registry import (
    ACTIVE_AGENT_RUN_STATUSES,
    LocalAgentRun,
    ProcessLocalAgentRunRegistry,
)
from backend.services.agent_runs.worker import ProcessLocalAgentRunWorker
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


@dataclass(frozen=True, slots=True)
class _PlannedInvocation:
    """One immutable subagent invocation prepared before launch."""

    index: int
    assignment: AgentAssignment
    display_name: str
    graph_thread_id: str


class SubagentHandler(BaseLangGraphHandler):
    """Run subagent and finalize its bounded result in the original parent turn."""

    def __init__(
        self,
        *args: Any,
        registry: ProcessLocalAgentRunRegistry,
        launcher: Any = None,
        worker: AgentRunWorker | None = None,
        lifecycle_publisher: LifecyclePublisher | None = None,
        result_projector: AgentRunResultProjector | None = None,
        subagent_registry: SubagentRegistry | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._publish_lifecycle = lifecycle_publisher or _publish_lifecycle_to_hub
        self._registry = registry
        self._subagent_registry = subagent_registry or get_subagent_registry()
        self._result_projector = result_projector or AgentRunResultProjector(
            registry=registry
        )
        if launcher is not None:
            self._launcher = launcher
        else:
            resolved_worker = worker or ProcessLocalAgentRunWorker(
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
        """Run requested subagents, hand bounded results to the parent, then finalize."""
        chat_inputs = runtime_config.chat_inputs
        turn = ensure_turn_identity(runtime_config, logger_=logger)

        plan = _build_dispatch_plan(
            runtime_config,
            parent_turn_id=str(turn.turn_id),
            subagent_registry=self._subagent_registry,
        )
        completion_result = await self._run_dispatch_plan(
            plan,
            runtime_config,
            turn=turn,
        )
        if isinstance(completion_result, LangGraphChatResult):
            return completion_result

        child_completions = completion_result
        child_results = tuple(completion.result for completion in child_completions)
        handoff = CompletedAgentResultHandoff(
            results=tuple(
                self._result_projector.project_result(result)
                for result in child_results
            ),
            agent_run_ids=tuple(
                completion.agent_run_id for completion in child_completions
            ),
        )
        attach_completed_agent_results_to_context(runtime_config.metadata, handoff)
        parent_result = await self._finalize_parent_handoff(
            runtime_config,
            turn=turn,
            child_completions=child_completions,
        )
        await self._consume_completed_handoff(
            assignments=tuple(item.assignment for item in plan),
            handoff=handoff,
        )
        return parent_result

    async def _run_dispatch_plan(
        self,
        plan: tuple["_PlannedInvocation", ...],
        runtime_config: LangGraphRuntimeConfig,
        *,
        turn: Any,
    ) -> tuple[AgentRunCompletion, ...] | LangGraphChatResult:
        """Launch validated invocations in concurrency-limited ordered batches."""
        completions: list[AgentRunCompletion | None] = [None] * len(plan)
        pending = list(plan)
        active_counts = await self._active_counts_for_plan(runtime_config)

        while pending:
            batch: list[_PlannedInvocation] = []
            deferred: list[_PlannedInvocation] = []
            for item in pending:
                spec = self._subagent_registry.require(item.assignment.agent_id)
                count = active_counts.get(item.assignment.agent_id, 0)
                if count < spec.max_active_runs_per_task:
                    batch.append(item)
                    active_counts[item.assignment.agent_id] = count + 1
                else:
                    deferred.append(item)

            if not batch:
                item = pending[0]
                assignment = item.assignment
                return _ack_result(
                    runtime_config,
                    turn_id=str(turn.turn_id),
                    turn_sequence=(
                        turn.turn_number if isinstance(turn.turn_number, int) else None
                    ),
                    agent_run_id=assignment.agent_run_id,
                    agent_id=assignment.agent_id,
                    agent_kind=assignment.agent_kind,
                    agent_display_name=item.display_name,
                    graph_thread_id=item.graph_thread_id,
                    status="failed",
                )

            launch_result = await self._launch_batch(batch, runtime_config, turn=turn)
            if isinstance(launch_result, LangGraphChatResult):
                return launch_result

            child_tasks = launch_result
            batch_results = await asyncio.gather(
                *(
                    _require_child_task_result(
                        child_task,
                        assignment=item.assignment,
                        graph_thread_id=item.graph_thread_id,
                    )
                    for item, child_task in child_tasks
                )
            )

            for item, batch_result in zip(batch, batch_results, strict=True):
                active_counts[item.assignment.agent_id] = max(
                    0,
                    active_counts.get(item.assignment.agent_id, 0) - 1,
                )
                if isinstance(batch_result, BaseException):
                    return _ack_for_child_exception(
                        batch_result,
                        item=item,
                        runtime_config=runtime_config,
                        turn=turn,
                    )
                completions[item.index] = batch_result

            pending = deferred

        return tuple(
            completion
            for completion in completions
            if isinstance(completion, AgentRunCompletion)
        )

    async def _launch_batch(
        self,
        batch: list["_PlannedInvocation"],
        runtime_config: LangGraphRuntimeConfig,
        *,
        turn: Any,
    ) -> tuple[tuple["_PlannedInvocation", Awaitable[Any]], ...] | LangGraphChatResult:
        """Register, mark running, and launch one already-validated batch."""
        launched: list[tuple[_PlannedInvocation, Awaitable[Any]]] = []
        for item in batch:
            assignment = item.assignment
            queued: LocalAgentRun | None = None

            try:
                spec = self._subagent_registry.require(assignment.agent_id)
                queued = await self._registry.register(
                    assignment,
                    graph_thread_id=item.graph_thread_id,
                    max_active_runs_per_task=spec.max_active_runs_per_task,
                )
                await self._publish_entry_lifecycle(queued, runtime_config)
                child_runtime_config = await build_child_execution_config(
                    assignment=assignment,
                    runtime_config=runtime_config,
                    registry=self._registry,
                    graph_thread_id=item.graph_thread_id,
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
                    graph_thread_id=item.graph_thread_id,
                    parent_run_id=_parent_run_id(runtime_config.metadata),
                )
            except Exception as exc:
                logger.warning(
                    "Failed to launch subagent run %s for task %s",
                    assignment.agent_run_id,
                    runtime_config.chat_inputs.task_id,
                    exc_info=True,
                )
                turn_sequence = (
                    turn.turn_number if isinstance(turn.turn_number, int) else None
                )
                usage = await self._settle_launched_batch_on_failure(
                    launched,
                    runtime_config,
                    turn_index=turn_sequence,
                )
                if queued is not None:
                    failed = await self._registry.mark_failed(
                        tenant_id=assignment.tenant_id,
                        task_id=assignment.task_id,
                        agent_run_id=assignment.agent_run_id,
                        safe_error=_safe_launch_error(
                            exc,
                            agent_display_name=item.display_name,
                        ),
                    )
                    await self._publish_entry_lifecycle(failed, runtime_config)
                return _ack_result(
                    runtime_config,
                    turn_id=str(turn.turn_id),
                    turn_sequence=turn_sequence,
                    agent_run_id=assignment.agent_run_id,
                    agent_id=assignment.agent_id,
                    agent_kind=assignment.agent_kind,
                    agent_display_name=item.display_name,
                    graph_thread_id=item.graph_thread_id,
                    status="failed",
                    usage=usage,
                )
            launched.append((item, child_task))
        return tuple(launched)

    async def _settle_launched_batch_on_failure(
        self,
        launched: list[tuple["_PlannedInvocation", Awaitable[Any]]],
        runtime_config: LangGraphRuntimeConfig,
        *,
        turn_index: int | None,
    ) -> list[Any] | None:
        """Terminally settle earlier launches when a later launch fails."""
        if not launched:
            return None

        for _item, child_task in launched:
            cancel = getattr(child_task, "cancel", None)
            done = getattr(child_task, "done", None)
            if callable(cancel) and (not callable(done) or not done()):
                cancel()

        settled = await asyncio.gather(
            *(
                _require_child_task_result(
                    child_task,
                    assignment=item.assignment,
                    graph_thread_id=item.graph_thread_id,
                )
                for item, child_task in launched
            )
        )
        await asyncio.sleep(0)
        usage: list[Any] = []
        for (item, _child_task), result in zip(launched, settled, strict=True):
            assignment = item.assignment
            if isinstance(result, AgentRunCompletion):
                usage.extend(
                    usage_envelopes_from_child_records(
                        result.usage_records,
                        execution_branch="subagent_child",
                        turn_index=turn_index,
                    )
                )
                before = await self._registry.get(
                    tenant_id=assignment.tenant_id,
                    task_id=assignment.task_id,
                    agent_run_id=assignment.agent_run_id,
                )
                completed = await self._registry.mark_completed(
                    tenant_id=assignment.tenant_id,
                    task_id=assignment.task_id,
                    agent_run_id=assignment.agent_run_id,
                    result=result.result,
                )
                if (
                    before is None
                    or completed.lifecycle_version != before.lifecycle_version
                ):
                    await self._publish_entry_lifecycle(completed, runtime_config)
                await self._registry.consume_result(
                    tenant_id=assignment.tenant_id,
                    task_id=assignment.task_id,
                    agent_run_id=assignment.agent_run_id,
                )
                continue

            before = await self._registry.get(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
            )
            if isinstance(
                result,
                (SubagentRunCancelled, SubagentRunPaused, SubagentRunFailed),
            ):
                usage.extend(
                    _usage_from_child_execution_result(
                        result.execution_result,
                        assignment=assignment,
                        graph_thread_id=item.graph_thread_id,
                        turn_index=turn_index,
                    )
                    or []
                )
            if isinstance(
                result,
                (asyncio.CancelledError, SubagentRunCancelled, SubagentRunPaused),
            ):
                entry = await self._registry.mark_cancelled(
                    tenant_id=assignment.tenant_id,
                    task_id=assignment.task_id,
                    agent_run_id=assignment.agent_run_id,
                )
            else:
                entry = await self._registry.mark_failed(
                    tenant_id=assignment.tenant_id,
                    task_id=assignment.task_id,
                    agent_run_id=assignment.agent_run_id,
                    safe_error="Subagent batch launch failed before parent handoff",
                )
            if before is None or entry.lifecycle_version != before.lifecycle_version:
                await self._publish_entry_lifecycle(entry, runtime_config)
        return usage or None

    async def _active_counts_for_plan(
        self,
        runtime_config: LangGraphRuntimeConfig,
    ) -> dict[str, int]:
        try:
            tenant_id = int(runtime_config.metadata.get("tenant_id"))
        except (TypeError, ValueError):
            return {}
        try:
            entries = await self._registry.list_task_runs(
                tenant_id=tenant_id,
                task_id=runtime_config.chat_inputs.task_id,
            )
        except Exception:
            logger.debug(
                "Failed to inspect local subagent registry for task %s",
                runtime_config.chat_inputs.task_id,
                exc_info=True,
            )
            return {}
        counts: dict[str, int] = {}
        for entry in entries:
            if entry.status in ACTIVE_AGENT_RUN_STATUSES:
                counts[entry.agent_id] = counts.get(entry.agent_id, 0) + 1
        return counts

    async def _finalize_parent_handoff(
        self,
        runtime_config: LangGraphRuntimeConfig,
        *,
        turn: Any,
        child_completions: tuple[AgentRunCompletion, ...],
    ) -> LangGraphChatResult:
        """Run the canonical main finalizer over completed child results once."""
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
                "branch": "subagent",
                "status": "completed",
                "handoff_agent_run_id": child_completions[0].result.agent_run_id,
                "handoff_agent_id": child_completions[0].result.agent_id,
                "handoff_agent_kind": child_completions[0].result.agent_kind,
                "handoff_graph_thread_id": child_completions[0].graph_thread_id,
                "handoff_agent_run_ids": [
                    completion.result.agent_run_id
                    for completion in child_completions
                ],
                "handoff_agent_ids": [
                    completion.result.agent_id
                    for completion in child_completions
                ],
                "handoff_agent_kinds": [
                    completion.result.agent_kind
                    for completion in child_completions
                ],
                "handoff_graph_thread_ids": [
                    completion.graph_thread_id
                    for completion in child_completions
                ],
            },
            chat_inputs.conversation_id or "",
        )
        merge_execution_metadata(result_metadata, captured_state)
        for key, value in build_agent_turn_metadata(interactive_state).items():
            if value is not None:
                result_metadata[key] = value

        turn_index = turn.turn_number if isinstance(turn.turn_number, int) else None
        parent_usage = _extract_usage_from_state(
            interactive_state,
            execution_branch="subagent_parent_finalizer",
            turn_index=turn_index,
        )
        child_usage = [
            usage
            for completion in child_completions
            for usage in usage_envelopes_from_child_records(
                completion.usage_records,
                execution_branch="subagent_child",
                turn_index=turn_index,
            )
        ]
        usage = [*child_usage, *(parent_usage or [])] or None
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
        assignments: tuple[AgentAssignment, ...],
        handoff: CompletedAgentResultHandoff,
    ) -> None:
        """Consume the registry result after the parent finalizer succeeds."""
        for _ in range(100):
            entries = [
                await self._registry.get(
                    tenant_id=assignment.tenant_id,
                    task_id=assignment.task_id,
                    agent_run_id=assignment.agent_run_id,
                )
                for assignment in assignments
            ]
            if entries and all(
                entry is not None and entry.status == "completed"
                for entry in entries
            ):
                first = assignments[0]
                await self._result_projector.mark_consumed(
                    tenant_id=first.tenant_id,
                    task_id=first.task_id,
                    handoff=handoff,
                )
                return
            await asyncio.sleep(0)
        logger.debug(
            "subagent results %s were not registry-settled after parent finalization",
            handoff.agent_run_ids,
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


async def _require_child_task(
    value: Any,
    *,
    assignment: AgentAssignment,
    graph_thread_id: str,
) -> AgentRunCompletion:
    """Await and validate the launcher's terminal subagent result."""
    if not isinstance(value, Awaitable):
        raise RuntimeError("Subagent launcher did not return an awaitable result task")
    result = await value
    if isinstance(result, AgentRunCompletion):
        return result
    if isinstance(result, AgentResult):
        return build_agent_run_completion(
            result=result,
            assignment=assignment,
            graph_thread_id=graph_thread_id,
        )
    raise RuntimeError("Subagent launcher returned an invalid terminal result")


async def _require_child_task_result(
    value: Any,
    *,
    assignment: AgentAssignment,
    graph_thread_id: str,
) -> AgentRunCompletion | BaseException:
    """Return child completion or the original terminal exception instance."""
    try:
        return await _require_child_task(
            value,
            assignment=assignment,
            graph_thread_id=graph_thread_id,
        )
    except BaseException as exc:
        return exc


def _ack_for_child_exception(
    exc: BaseException,
    *,
    item: _PlannedInvocation,
    runtime_config: LangGraphRuntimeConfig,
    turn: Any,
) -> LangGraphChatResult:
    """Return the same bounded non-finalizer result used by singular runs."""
    assignment = item.assignment
    turn_sequence = turn.turn_number if isinstance(turn.turn_number, int) else None
    if isinstance(exc, SubagentRunPaused):
        usage = _usage_from_child_execution_result(
            exc.execution_result,
            assignment=assignment,
            graph_thread_id=item.graph_thread_id,
            turn_index=turn_sequence,
        )
        return _ack_result(
            runtime_config,
            turn_id=str(turn.turn_id),
            turn_sequence=turn_sequence,
            agent_run_id=assignment.agent_run_id,
            agent_id=assignment.agent_id,
            agent_kind=assignment.agent_kind,
            agent_display_name=item.display_name,
            graph_thread_id=item.graph_thread_id,
            status="waiting_for_approval",
            usage=usage,
        )
    if isinstance(exc, SubagentRunCancelled):
        usage = _usage_from_child_execution_result(
            exc.execution_result,
            assignment=assignment,
            graph_thread_id=item.graph_thread_id,
            turn_index=turn_sequence,
        )
        return _ack_result(
            runtime_config,
            turn_id=str(turn.turn_id),
            turn_sequence=turn_sequence,
            agent_run_id=assignment.agent_run_id,
            agent_id=assignment.agent_id,
            agent_kind=assignment.agent_kind,
            agent_display_name=item.display_name,
            graph_thread_id=item.graph_thread_id,
            status="cancelled",
            usage=usage,
        )
    if isinstance(exc, SubagentRunFailed):
        usage = _usage_from_child_execution_result(
            exc.execution_result,
            assignment=assignment,
            graph_thread_id=item.graph_thread_id,
            turn_index=turn_sequence,
        )
        return _ack_result(
            runtime_config,
            turn_id=str(turn.turn_id),
            turn_sequence=turn_sequence,
            agent_run_id=assignment.agent_run_id,
            agent_id=assignment.agent_id,
            agent_kind=assignment.agent_kind,
            agent_display_name=item.display_name,
            graph_thread_id=item.graph_thread_id,
            status="failed",
            usage=usage,
        )
    if isinstance(exc, asyncio.CancelledError):
        return _ack_result(
            runtime_config,
            turn_id=str(turn.turn_id),
            turn_sequence=turn_sequence,
            agent_run_id=assignment.agent_run_id,
            agent_id=assignment.agent_id,
            agent_kind=assignment.agent_kind,
            agent_display_name=item.display_name,
            graph_thread_id=item.graph_thread_id,
            status="cancelled",
        )

    logger.warning(
        "subagent run %s failed before parent handoff for task %s",
        assignment.agent_run_id,
        runtime_config.chat_inputs.task_id,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return _ack_result(
        runtime_config,
        turn_id=str(turn.turn_id),
        turn_sequence=turn_sequence,
        agent_run_id=assignment.agent_run_id,
        agent_id=assignment.agent_id,
        agent_kind=assignment.agent_kind,
        agent_display_name=item.display_name,
        graph_thread_id=item.graph_thread_id,
        status="failed",
    )


async def _publish_lifecycle_to_hub(task_id: int, event: dict[str, Any]) -> None:
    """Publish lifecycle events through the existing task stream hub."""
    from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub

    await get_in_memory_stream_hub().publish(task_id, event)


def _build_assignment(
    runtime_config: LangGraphRuntimeConfig,
    *,
    parent_turn_id: str,
    subagent_registry: SubagentRegistry | None = None,
) -> AgentAssignment:
    return _build_dispatch_plan(
        runtime_config,
        parent_turn_id=parent_turn_id,
        subagent_registry=subagent_registry,
    )[0].assignment


def _build_dispatch_plan(
    runtime_config: LangGraphRuntimeConfig,
    *,
    parent_turn_id: str,
    subagent_registry: SubagentRegistry | None = None,
) -> tuple[_PlannedInvocation, ...]:
    metadata = runtime_config.metadata
    ownership = metadata.get("subagent_routing")
    if not isinstance(ownership, Mapping) or not ownership.get("should_delegate"):
        raise RuntimeError("Subagent branch requires a positive ownership decision")

    raw_handoffs = ownership.get("handoffs")
    if isinstance(raw_handoffs, list | tuple) and raw_handoffs:
        requested_handoffs = tuple(raw_handoffs)
    else:
        requested_handoffs = (ownership,)

    if len(requested_handoffs) > MAX_AGENT_HANDOFFS:
        raise RuntimeError("Subagent dispatch plan has too many handoffs")

    registry = subagent_registry or get_subagent_registry()
    validated: list[tuple[Mapping[str, Any], Any]] = []
    for raw_handoff in requested_handoffs:
        if not isinstance(raw_handoff, Mapping):
            raise RuntimeError("Subagent dispatch plan contains an invalid handoff")
        agent_id = _required_string(raw_handoff.get("agent_id"), "agent_id")
        try:
            spec = registry.require(agent_id)
        except KeyError as exc:
            raise RuntimeError(
                f"subagent is not registered or enabled: {agent_id}"
            ) from exc
        if raw_handoff.get("agent_kind") != spec.kind:
            raise RuntimeError("Subagent branch agent kind does not match registry")
        if spec.requires_resolved_target and not _string_list(
            raw_handoff.get("targets")
        ):
            raise RuntimeError("Subagent dispatch plan requires resolved targets")
        validated.append((raw_handoff, spec))

    return tuple(
        _PlannedInvocation(
            index=index,
            assignment=_build_assignment_from_handoff(
                runtime_config,
                parent_turn_id=parent_turn_id,
                ownership=raw_handoff,
                spec=spec,
            ),
            display_name=spec.display_name,
            graph_thread_id=_new_child_graph_thread_id(),
        )
        for index, (raw_handoff, spec) in enumerate(validated)
    )


def _build_assignment_from_handoff(
    runtime_config: LangGraphRuntimeConfig,
    *,
    parent_turn_id: str,
    ownership: Mapping[str, Any],
    spec: Any,
) -> AgentAssignment:
    metadata = runtime_config.metadata
    chat_inputs = runtime_config.chat_inputs
    agent_id = _required_string(ownership.get("agent_id"), "agent_id")

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
        agent_id=agent_id,
        agent_kind=spec.kind,
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
            _agent_capabilities(
                ownership.get("capabilities"),
                allowed=spec.supported_task_categories,
            )
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
    agent_id: str,
    agent_kind: str,
    agent_display_name: str,
    graph_thread_id: str,
    status: str,
    usage: list[Any] | None = None,
) -> LangGraphChatResult:
    conversation_id = runtime_config.chat_inputs.conversation_id
    metadata = attach_conversation_ids(
        {
            "role": "assistant",
            "streaming": False,
            "mode": ExecutionMode.SIMPLE_TOOL.value,
            "branch": "subagent",
            "agent_run_id": agent_run_id,
            "agent_id": agent_id,
            "agent_kind": agent_kind,
            "agent_display_name": agent_display_name,
            "graph_thread_id": graph_thread_id,
            "status": status,
            "id": turn_id,
        },
        conversation_id or "",
    )
    if turn_sequence is not None:
        metadata["turn_sequence"] = turn_sequence
    display_name = agent_display_name
    return LangGraphChatResult(
        final_text={
            "failed": f"{display_name} could not complete the subagent run.",
            "cancelled": f"{display_name} subagent run was cancelled.",
            "waiting_for_approval": f"{display_name} is waiting for tool approval.",
            "running": (
                f"{display_name} has started a subagent run and will hand off findings "
                "when it finishes."
            ),
        }.get(status, f"{display_name} subagent status changed."),
        conversation_id=conversation_id,
        metadata=metadata,
        usage=usage,
    )


def _usage_from_child_execution_result(
    execution_result: Any,
    *,
    assignment: AgentAssignment,
    graph_thread_id: str,
    turn_index: int | None,
) -> list[Any] | None:
    records = child_usage_records_from_state(
        getattr(execution_result, "final_state", None),
        assignment=assignment,
        graph_thread_id=graph_thread_id,
    )
    return (
        usage_envelopes_from_child_records(
            records,
            execution_branch="subagent_child",
            turn_index=turn_index,
        )
        or None
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


def _safe_launch_error(exc: Exception, *, agent_display_name: str) -> str:
    _ = exc
    return f"{agent_display_name} launch failed"


def _new_agent_run_id() -> str:
    return f"agent-run-{uuid4().hex}"


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


def _agent_capabilities(
    value: Any,
    *,
    allowed: tuple[str, ...],
) -> list[AgentCapability]:
    allowed_set = set(allowed)
    return [
        capability
        for capability in _string_list(value)
        if capability in allowed_set
    ]


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
        raise RuntimeError(f"Subagent assignment requires {field_name}") from exc


def _required_string(value: Any, field_name: str) -> str:
    normalized = _optional_string(value)
    if not normalized:
        raise RuntimeError(f"Subagent assignment requires {field_name}")
    return normalized


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


__all__ = [
    "SubagentHandler",
    "build_agent_run_lifecycle_event",
]
