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

from agent.graph import InteractiveState
from agent.graph.builders.common_edges import ensure_metadata_runtime_budgets
from agent.graph.builders.parent_handoff_builder import build_parent_handoff_graph
from agent.graph.graph_names import GRAPH_NAME_SIMPLE_TOOL
from agent.graph.streaming import build_agent_turn_metadata
from agent.graph.utils.event_identity import (
    POST_ACTION_STREAM_SEQUENCE_METADATA_KEY,
)
from agent.subagents.registry import SubagentRegistry, get_subagent_registry
from backend.services.agent_runs.assignment_builder import parent_run_id_from_metadata
from backend.services.agent_runs.contracts import (
    AgentAssignment,
    AgentResult,
)
from backend.services.agent_runs.dispatch_plan import (
    PlannedAgentInvocation,
    build_dispatch_plan,
    routing_metadata_from_decision,
    runtime_config_with_subagent_routing,
    stable_par_assignment_identity,
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
from backend.services.agent_runs.ownership_policy import (
    normalize_agent_handoff_entries,
    resolve_subagent_handoff,
)
from backend.services.agent_runs.parent_handoff_coordinator import (
    ParentFollowupDelegation,
    ParentHandoffCoordinator,
    ParentHandoffOutcome,
)
from backend.services.agent_runs.result_projection import (
    AgentRunResultProjector,
)
from backend.services.agent_runs.registry import (
    ACTIVE_AGENT_RUN_STATUSES,
    TERMINAL_AGENT_RUN_STATUSES,
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
ReadyHandoffProcessor = Callable[
    [tuple[AgentRunCompletion, ...], bool], Awaitable[ParentHandoffOutcome | None]
]


@dataclass(frozen=True, slots=True)
class _DispatchPlanResult:
    """Completed launch plan with the latest parent handoff outcome, if any."""

    child_completions: tuple[AgentRunCompletion, ...]
    parent_handoff_outcome: ParentHandoffOutcome | None


@dataclass(frozen=True, slots=True)
class _LaunchBatchFailure:
    """Terminal launch failure plus sibling results preserved for parent PAR."""

    child_completions: tuple[AgentRunCompletion, ...]


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
        self._parent_handoff_coordinator = ParentHandoffCoordinator(
            registry=registry,
            result_projector=self._result_projector,
            parent_progress_publisher=self._publish_parent_progress,
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

        plan = build_dispatch_plan(
            runtime_config,
            parent_turn_id=str(turn.turn_id),
            subagent_registry=self._subagent_registry,
        )
        tenant_id = int(runtime_config.metadata["tenant_id"])
        child_completion_by_run_id: dict[str, AgentRunCompletion] = {}

        async def run_parent_continuation(
            handoff: Any,
            _active_runs: tuple[dict[str, Any], ...],
        ) -> LangGraphChatResult:
            child_completions = await self._child_completions_for_handoff(
                handoff,
                tenant_id=tenant_id,
                task_id=chat_inputs.task_id,
                completion_by_run_id=child_completion_by_run_id,
            )
            return await self._finalize_parent_handoff(
                runtime_config,
                turn=turn,
                child_completions=child_completions,
            )

        async def process_ready_handoffs(
            child_completions: tuple[AgentRunCompletion, ...],
            wait_for_initial_handoff: bool = False,
        ) -> ParentHandoffOutcome | None:
            for completion in child_completions:
                child_completion_by_run_id[completion.result.agent_run_id] = completion
            return await self._parent_handoff_coordinator.process_ready_handoffs(
                tenant_id=tenant_id,
                task_id=chat_inputs.task_id,
                conversation_id=chat_inputs.conversation_id or "",
                parent_turn_id=str(turn.turn_id),
                metadata=runtime_config.metadata,
                run_parent_continuation=run_parent_continuation,
                dispatch_followup_delegation=lambda agent_handoff, decision_id: (
                    self._dispatch_par_followup_delegation(
                        runtime_config,
                        turn=turn,
                        agent_handoff=agent_handoff,
                        decision_id=decision_id,
                    )
                ),
                child_completions=child_completions,
                wait_for_initial_handoff=wait_for_initial_handoff,
            )

        dispatch_result = await self._run_dispatch_plan(
            plan,
            runtime_config,
            turn=turn,
            process_ready_handoffs=process_ready_handoffs,
        )
        if isinstance(dispatch_result, LangGraphChatResult):
            return dispatch_result

        child_completions = dispatch_result.child_completions
        outcome = dispatch_result.parent_handoff_outcome
        if outcome is None:
            outcome = await process_ready_handoffs(child_completions, False)
        if outcome is None:
            raise RuntimeError("No completed subagent handoff was available to process")
        return outcome.result

    async def _dispatch_par_followup_delegation(
        self,
        runtime_config: LangGraphRuntimeConfig,
        *,
        turn: Any,
        agent_handoff: Mapping[str, Any],
        decision_id: str,
    ) -> ParentFollowupDelegation:
        """Resolve and launch a PAR-authored follow-up via the normal dispatch path."""
        try:
            normalized = normalize_agent_handoff_entries(
                agent_handoff,
                max_handoffs=1,
                reject_invalid=True,
            )
        except ValueError as exc:
            raise RuntimeError(
                "PAR follow-up delegation rejected: invalid_handoff_plan"
            ) from exc
        if not normalized:
            raise RuntimeError("PAR follow-up delegation rejected: invalid_handoff_plan")

        _, stable_agent_run_id = stable_par_assignment_identity(
            delegation_decision_id=decision_id,
            agent_id=normalized[0]["subagent"],
            objective=normalized[0]["objective"],
        )
        existing = await self._registry.get(
            tenant_id=int(runtime_config.metadata["tenant_id"]),
            task_id=runtime_config.chat_inputs.task_id,
            agent_run_id=stable_agent_run_id,
        )
        if existing is not None:
            return ParentFollowupDelegation(
                agent_run_ids=(stable_agent_run_id,),
                launched_agent_run_ids=(),
            )

        active_counts = await self._active_counts_for_plan(runtime_config)
        decision = resolve_subagent_handoff(
            runtime_config.metadata,
            registry=self._subagent_registry,
            active_runs_by_agent_id=active_counts,
            handoff_entries=agent_handoff,
            require_direct_executor=False,
        )
        if not decision.should_delegate:
            raise RuntimeError(
                f"PAR follow-up delegation rejected: {decision.reason}"
            )

        followup_config = runtime_config_with_subagent_routing(
            runtime_config,
            routing_metadata_from_decision(
                decision,
                delegation_source="par",
                delegation_decision_id=decision_id,
            ),
        )
        plan = build_dispatch_plan(
            followup_config,
            parent_turn_id=str(turn.turn_id),
            subagent_registry=self._subagent_registry,
        )
        launch_result = await self._launch_batch(
            list(plan),
            followup_config,
            turn=turn,
        )
        if isinstance(launch_result, LangGraphChatResult):
            raise RuntimeError(
                "PAR follow-up delegation launch failed: "
                f"{launch_result.metadata.get('status') or 'unknown'}"
            )
        if isinstance(launch_result, _LaunchBatchFailure):
            return ParentFollowupDelegation(
                agent_run_ids=tuple(item.assignment.agent_run_id for item in plan),
                launched_agent_run_ids=(),
            )
        launched = {item.assignment.agent_run_id for item, _task in launch_result}
        return ParentFollowupDelegation(
            agent_run_ids=tuple(item.assignment.agent_run_id for item in plan),
            launched_agent_run_ids=tuple(
                item.assignment.agent_run_id
                for item in plan
                if item.assignment.agent_run_id in launched
            ),
        )

    async def _run_dispatch_plan(
        self,
        plan: tuple[PlannedAgentInvocation, ...],
        runtime_config: LangGraphRuntimeConfig,
        *,
        turn: Any,
        process_ready_handoffs: ReadyHandoffProcessor,
    ) -> _DispatchPlanResult | LangGraphChatResult:
        """Launch validated invocations in concurrency-limited ordered batches."""
        completions: list[AgentRunCompletion | None] = [None] * len(plan)
        pending = list(plan)
        active_counts = await self._active_counts_for_plan(runtime_config)
        parent_handoff_outcome: ParentHandoffOutcome | None = None

        while pending:
            batch: list[PlannedAgentInvocation] = []
            deferred: list[PlannedAgentInvocation] = []
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
            if isinstance(launch_result, _LaunchBatchFailure):
                batch_completions = launch_result.child_completions
                for completion in batch_completions:
                    for item in batch:
                        if (
                            item.assignment.agent_run_id
                            == completion.result.agent_run_id
                        ):
                            completions[item.index] = completion
                            break
                batch_outcome = await process_ready_handoffs(batch_completions, False)
                return _DispatchPlanResult(
                    child_completions=tuple(
                        completion
                        for completion in completions
                        if isinstance(completion, AgentRunCompletion)
                    ),
                    parent_handoff_outcome=batch_outcome,
                )

            child_tasks = launch_result
            ready_handoff_task: asyncio.Task[ParentHandoffOutcome | None] | None = None
            if len(child_tasks) > 1:
                ready_handoff_task = asyncio.create_task(
                    process_ready_handoffs((), True)
                )

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

            paused = False
            for item, batch_result in zip(batch, batch_results, strict=True):
                active_counts[item.assignment.agent_id] = max(
                    0,
                    active_counts.get(item.assignment.agent_id, 0) - 1,
                )
                if isinstance(batch_result, BaseException):
                    if isinstance(batch_result, SubagentRunPaused):
                        paused = True
                        continue
                    terminal_completion = await self._completion_for_terminal_exception(
                        batch_result,
                        item=item,
                    )
                    if terminal_completion is not None:
                        completions[item.index] = terminal_completion
                        continue
                    await _cancel_ready_handoff_task(ready_handoff_task)
                    return _ack_for_child_exception(
                        batch_result,
                        item=item,
                        runtime_config=runtime_config,
                        turn=turn,
                    )
                completions[item.index] = batch_result

            batch_completions = tuple(
                completions[item.index]
                for item in batch
                if isinstance(completions[item.index], AgentRunCompletion)
            )
            if paused:
                await _cancel_ready_handoff_task(ready_handoff_task)
                resumed_outcome = await process_ready_handoffs(
                    batch_completions,
                    True,
                )
                if resumed_outcome is None:
                    raise RuntimeError(
                        "Subagent approval resume completed without a parent handoff"
                    )
                return _DispatchPlanResult(
                    child_completions=tuple(
                        completion
                        for completion in completions
                        if isinstance(completion, AgentRunCompletion)
                    ),
                    parent_handoff_outcome=resumed_outcome,
                )
            if ready_handoff_task is not None:
                early_outcome = await ready_handoff_task
                if early_outcome is not None:
                    parent_handoff_outcome = early_outcome
                    irrelevant_run_ids = _irrelevant_active_run_ids_from_outcome(
                        early_outcome
                    )
                    if irrelevant_run_ids:
                        await self._consume_irrelevant_terminal_results(
                            runtime_config,
                            irrelevant_run_ids=irrelevant_run_ids,
                            already_processed_run_ids=early_outcome.agent_run_ids,
                        )
                        return _DispatchPlanResult(
                            child_completions=tuple(
                                completion
                                for completion in completions
                                if isinstance(completion, AgentRunCompletion)
                            ),
                            parent_handoff_outcome=parent_handoff_outcome,
                        )
            batch_outcome = await process_ready_handoffs(batch_completions, False)
            if batch_outcome is not None:
                parent_handoff_outcome = batch_outcome

            pending = deferred

        return _DispatchPlanResult(
            child_completions=tuple(
                completion
                for completion in completions
                if isinstance(completion, AgentRunCompletion)
            ),
            parent_handoff_outcome=parent_handoff_outcome,
        )

    async def _completion_for_terminal_exception(
        self,
        exc: BaseException,
        *,
        item: PlannedAgentInvocation,
    ) -> AgentRunCompletion | None:
        """Return a launcher-recorded failed/cancelled completion for parent PAR."""
        if isinstance(exc, SubagentRunPaused):
            return None
        if not isinstance(exc, (SubagentRunCancelled, SubagentRunFailed)):
            return None

        terminal = await self._observe_terminal_entry(item.assignment)
        if (
            terminal is None
            or terminal.result is None
            or terminal.status not in {"failed", "cancelled"}
        ):
            return None

        usage_records = child_usage_records_from_state(
            getattr(exc.execution_result, "final_state", None),
            assignment=item.assignment,
            graph_thread_id=item.graph_thread_id,
        )
        return AgentRunCompletion(
            result=terminal.result,
            usage_records=usage_records,
            graph_thread_id=item.graph_thread_id,
        )

    async def _consume_irrelevant_terminal_results(
        self,
        runtime_config: LangGraphRuntimeConfig,
        *,
        irrelevant_run_ids: tuple[str, ...],
        already_processed_run_ids: tuple[str, ...],
    ) -> None:
        """Suppress later same-turn PAR cycles for PAR-declared irrelevant runs."""
        processed = set(already_processed_run_ids)
        tenant_id = int(runtime_config.metadata["tenant_id"])
        task_id = runtime_config.chat_inputs.task_id
        for agent_run_id in irrelevant_run_ids:
            if agent_run_id in processed:
                continue
            await self._registry.consume_result(
                tenant_id=tenant_id,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )

    async def _child_completions_for_handoff(
        self,
        handoff: Any,
        *,
        tenant_id: int,
        task_id: int,
        completion_by_run_id: dict[str, AgentRunCompletion],
    ) -> tuple[AgentRunCompletion, ...]:
        """Return concrete completions matching a claimed handoff batch."""
        completions: list[AgentRunCompletion] = []
        for agent_run_id in getattr(handoff, "agent_run_ids", ()):
            cached = completion_by_run_id.get(agent_run_id)
            if cached is not None:
                completions.append(cached)
                continue
            entry = await self._registry.get(
                tenant_id=tenant_id,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
            if entry is None or entry.result is None:
                continue
            completion = build_agent_run_completion(
                result=entry.result,
                assignment=entry.assignment,
                graph_thread_id=entry.graph_thread_id,
                final_state={},
            )
            completion_by_run_id[agent_run_id] = completion
            completions.append(completion)
        return tuple(completions)

    async def _launch_batch(
        self,
        batch: list[PlannedAgentInvocation],
        runtime_config: LangGraphRuntimeConfig,
        *,
        turn: Any,
    ) -> (
        tuple[tuple[PlannedAgentInvocation, Awaitable[Any]], ...]
        | LangGraphChatResult
        | _LaunchBatchFailure
    ):
        """Register, mark running, and launch one already-validated batch."""
        launched: list[tuple[PlannedAgentInvocation, Awaitable[Any]]] = []
        for item in batch:
            assignment = item.assignment
            queued: LocalAgentRun | None = None

            try:
                spec = self._subagent_registry.require(assignment.agent_id)
                existing = await self._existing_replayable_followup(assignment)
                if existing is not None:
                    logger.info(
                        "Skipping replayed PAR follow-up launch for %s",
                        assignment.agent_run_id,
                    )
                    continue
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
                    parent_run_id=parent_run_id_from_metadata(
                        runtime_config.metadata
                    ),
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
                settled_completions = await self._settle_launched_batch_on_failure(
                    launched
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
                    if failed.result is not None:
                        settled_completions = (
                            *settled_completions,
                            AgentRunCompletion(
                                result=failed.result,
                                usage_records=(),
                                graph_thread_id=item.graph_thread_id,
                            ),
                        )
                    return _LaunchBatchFailure(
                        child_completions=settled_completions
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
            launched.append((item, child_task))
        return tuple(launched)

    async def _existing_replayable_followup(
        self,
        assignment: AgentAssignment,
    ) -> LocalAgentRun | None:
        """Return an existing stable PAR follow-up run to suppress replay launch."""
        if assignment.relevant_context.get("delegation_source") != "par":
            return None
        return await self._registry.get(
            tenant_id=assignment.tenant_id,
            task_id=assignment.task_id,
            agent_run_id=assignment.agent_run_id,
        )

    async def _settle_launched_batch_on_failure(
        self,
        launched: list[tuple[PlannedAgentInvocation, Awaitable[Any]]],
    ) -> tuple[AgentRunCompletion, ...]:
        """Terminally settle earlier launches without consuming parent handoffs."""
        if not launched:
            return ()

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
        completions: list[AgentRunCompletion] = []
        for (item, _child_task), result in zip(launched, settled, strict=True):
            assignment = item.assignment
            if isinstance(result, AgentRunCompletion):
                await self._observe_terminal_entry(assignment)
                completions.append(result)
                continue

            if isinstance(
                result,
                (SubagentRunCancelled, SubagentRunPaused, SubagentRunFailed),
            ):
                terminal = await self._observe_terminal_entry(assignment)
                if terminal is not None and terminal.result is not None:
                    usage_records = child_usage_records_from_state(
                        getattr(result.execution_result, "final_state", None),
                        assignment=assignment,
                        graph_thread_id=item.graph_thread_id,
                    )
                    completions.append(
                        AgentRunCompletion(
                            result=terminal.result,
                            usage_records=usage_records,
                            graph_thread_id=item.graph_thread_id,
                        )
                    )
                continue
            terminal = await self._observe_terminal_entry(assignment)
            if terminal is not None and terminal.result is not None:
                completions.append(
                    AgentRunCompletion(
                        result=terminal.result,
                        usage_records=(),
                        graph_thread_id=item.graph_thread_id,
                    )
                )
        return tuple(completions)

    async def _observe_terminal_entry(
        self,
        assignment: AgentAssignment,
    ) -> LocalAgentRun | None:
        """Observe the launcher's terminal registry transition without mutating it."""
        for _ in range(100):
            entry = await self._registry.get(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
            )
            if entry is not None and entry.status in TERMINAL_AGENT_RUN_STATUSES:
                return entry
            await asyncio.sleep(0)
        logger.debug(
            "subagent run %s did not terminally settle before handler observation",
            assignment.agent_run_id,
        )
        return None

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
        starting_metadata = starting_state.facts.ensure_metadata()
        persisted_runtime_budgets = runtime_config.metadata.get("runtime_budgets")
        if isinstance(persisted_runtime_budgets, Mapping):
            starting_metadata["runtime_budgets"] = dict(persisted_runtime_budgets)
        ensure_metadata_runtime_budgets(starting_metadata)

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
        next_post_action_stream_sequence = (
            interactive_state.facts.safe_metadata.get(
                POST_ACTION_STREAM_SEQUENCE_METADATA_KEY
            )
        )
        if isinstance(next_post_action_stream_sequence, int):
            runtime_config.metadata[POST_ACTION_STREAM_SEQUENCE_METADATA_KEY] = (
                next_post_action_stream_sequence
            )
        final_runtime_budgets = interactive_state.facts.safe_metadata.get(
            "runtime_budgets"
        )
        if isinstance(final_runtime_budgets, Mapping):
            runtime_config.metadata["runtime_budgets"] = dict(final_runtime_budgets)
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
        router_outcome = interactive_state.facts.safe_metadata.get("router_outcome")
        if isinstance(router_outcome, Mapping):
            result_metadata.setdefault("router_outcome", dict(router_outcome))
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

    async def _publish_entry_lifecycle(
        self,
        entry: LocalAgentRun,
        runtime_config: LangGraphRuntimeConfig,
    ) -> None:
        event = build_agent_run_lifecycle_event(
            entry,
            parent_run_id=parent_run_id_from_metadata(runtime_config.metadata),
        )
        await self._publish_lifecycle(entry.task_id, event)

    async def _publish_parent_progress(
        self,
        task_id: int,
        events: tuple[dict[str, Any], ...],
    ) -> None:
        """Publish parent-owned handoff progress through the task stream."""
        for event in events:
            await self._publish_lifecycle(task_id, event)


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


async def _cancel_ready_handoff_task(
    task: asyncio.Task[ParentHandoffOutcome | None] | None,
) -> None:
    """Cancel a coordinator wait after a child failure path takes over."""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return


def _irrelevant_active_run_ids_from_outcome(
    outcome: ParentHandoffOutcome,
) -> tuple[str, ...]:
    """Return PAR-declared irrelevant active run IDs from the parent outcome."""
    for key in ("router_outcome", "parent_control_outcome", "candidate_decision"):
        source = outcome.result.metadata.get(key)
        if not isinstance(source, Mapping):
            continue
        raw_ids = source.get("par_irrelevant_active_agent_run_ids")
        if raw_ids is None:
            raw_ids = source.get("irrelevant_active_agent_run_ids")
        run_ids = _normalized_non_empty_strings(raw_ids)
        if run_ids:
            return run_ids
    return ()


def _normalized_non_empty_strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list | tuple | set | frozenset):
        return ()
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text and text not in normalized:
            normalized.append(text)
    return tuple(normalized)


def _ack_for_child_exception(
    exc: BaseException,
    *,
    item: PlannedAgentInvocation,
    runtime_config: LangGraphRuntimeConfig,
    turn: Any,
) -> LangGraphChatResult:
    """Return the same bounded non-finalizer result used by singular runs."""
    assignment = item.assignment
    turn_sequence = turn.turn_number if isinstance(turn.turn_number, int) else None
    usage: list[Any] | None = None
    if isinstance(exc, SubagentRunPaused | SubagentRunCancelled | SubagentRunFailed):
        usage = _usage_from_child_execution_result(
            exc.execution_result,
            assignment=assignment,
            graph_thread_id=item.graph_thread_id,
            turn_index=turn_sequence,
        )
        status = (
            "waiting_for_approval"
            if isinstance(exc, SubagentRunPaused)
            else "cancelled"
            if isinstance(exc, SubagentRunCancelled)
            else "failed"
        )
    elif isinstance(exc, asyncio.CancelledError):
        status = "cancelled"
    else:
        status = "failed"
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
        status=status,
        usage=usage,
    )


async def _publish_lifecycle_to_hub(task_id: int, event: dict[str, Any]) -> None:
    """Publish lifecycle events through the existing task stream hub."""
    from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub

    await get_in_memory_stream_hub().publish(task_id, event)


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


def _safe_launch_error(exc: Exception, *, agent_display_name: str) -> str:
    _ = exc
    return f"{agent_display_name} launch failed"


__all__ = [
    "SubagentHandler",
    "build_agent_run_lifecycle_event",
]
