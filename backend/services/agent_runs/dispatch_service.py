"""Dispatch validated subagent plans through the process-local run lifecycle.

The service owns capacity-aware scheduling, registry transitions, child launch
settlement, and follow-up replay suppression. It returns typed application
outcomes and deliberately has no knowledge of chat response formatting.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Mapping
from typing import Any

from agent.subagents.registry import SubagentRegistry
from backend.services.langgraph_chat.contracts import LangGraphRuntimeConfig
from backend.services.llm_provider.runtime_services import attach_runtime_services

from .assignment_builder import parent_run_id_from_metadata
from .completion import (
    AgentRunCompletion,
    build_agent_run_completion,
    child_usage_records_from_state,
    usage_envelopes_from_child_records,
)
from .contracts import AgentAssignment, AgentResult
from .dispatch_contracts import (
    AgentRunDispatchResult,
    AgentRunDispatchStop,
    AgentRunLaunchService,
    DispatchBatchLaunchFailure,
    DispatchStopStatus,
    ReadyHandoffProcessor,
)
from .dispatch_plan import (
    PlannedAgentInvocation,
    build_dispatch_plan,
    routing_metadata_from_decision,
    runtime_config_with_subagent_routing,
    stable_par_assignment_identity,
)
from .event_projection import build_agent_run_lifecycle_event
from .execution_config import build_child_execution_config
from .launcher import (
    LifecyclePublisher,
    SubagentRunCancelled,
    SubagentRunFailed,
    SubagentRunPaused,
)
from .ownership_policy import normalize_agent_handoff_entries, resolve_subagent_handoff
from .parent_handoff_coordinator import (
    ParentFollowupDelegation,
    ParentHandoffOutcome,
)
from .registry import ProcessLocalAgentRunRegistry
from .registry_contracts import (
    ACTIVE_AGENT_RUN_STATUSES,
    LocalAgentRun,
)
from .result_projection import CompletedAgentResultHandoff

logger = logging.getLogger(__name__)


class SubagentDispatchService:
    """Schedule and settle subagent invocations without presentation concerns."""

    def __init__(
        self,
        *,
        registry: ProcessLocalAgentRunRegistry,
        launcher: AgentRunLaunchService,
        subagent_registry: SubagentRegistry,
        lifecycle_publisher: LifecyclePublisher,
    ) -> None:
        self._registry = registry
        self._launcher = launcher
        self._subagent_registry = subagent_registry
        self._publish_lifecycle = lifecycle_publisher

    async def dispatch(
        self,
        plan: tuple[PlannedAgentInvocation, ...],
        runtime_config: LangGraphRuntimeConfig,
        *,
        parent_turn_sequence: int | None,
        process_ready_handoffs: ReadyHandoffProcessor,
    ) -> AgentRunDispatchResult:
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
                return AgentRunDispatchResult(
                    stop=AgentRunDispatchStop(
                        invocation=pending[0],
                        status="failed",
                    )
                )

            launch_result = await self._launch_batch(
                batch,
                runtime_config,
            )
            if isinstance(launch_result, DispatchBatchLaunchFailure):
                if launch_result.stop is not None:
                    return AgentRunDispatchResult(stop=launch_result.stop)
                batch_completions = launch_result.child_completions
                self._record_batch_completions(
                    completions,
                    batch=batch,
                    batch_completions=batch_completions,
                )
                batch_outcome = await process_ready_handoffs(batch_completions, False)
                return AgentRunDispatchResult(
                    child_completions=_completed_entries(completions),
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
                    return AgentRunDispatchResult(
                        stop=_stop_for_child_exception(
                            batch_result,
                            item=item,
                            task_id=runtime_config.chat_inputs.task_id,
                            turn_index=parent_turn_sequence,
                        )
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
                return AgentRunDispatchResult(
                    child_completions=_completed_entries(completions),
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
                        return AgentRunDispatchResult(
                            child_completions=_completed_entries(completions),
                            parent_handoff_outcome=parent_handoff_outcome,
                        )
            batch_outcome = await process_ready_handoffs(batch_completions, False)
            if batch_outcome is not None:
                parent_handoff_outcome = batch_outcome

            pending = deferred

        return AgentRunDispatchResult(
            child_completions=_completed_entries(completions),
            parent_handoff_outcome=parent_handoff_outcome,
        )

    async def dispatch_followup(
        self,
        runtime_config: LangGraphRuntimeConfig,
        *,
        parent_turn_id: str,
        agent_handoff: Mapping[str, Any],
        decision_id: str,
    ) -> ParentFollowupDelegation:
        """Resolve and launch a PAR-authored follow-up through normal dispatch."""
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
            raise RuntimeError(f"PAR follow-up delegation rejected: {decision.reason}")

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
            parent_turn_id=parent_turn_id,
            subagent_registry=self._subagent_registry,
        )
        launch_result = await self._launch_batch(
            list(plan),
            followup_config,
        )
        if isinstance(launch_result, DispatchBatchLaunchFailure):
            if launch_result.stop is not None:
                raise RuntimeError(
                    "PAR follow-up delegation launch failed: "
                    f"{launch_result.stop.status}"
                )
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

    async def completions_for_handoff(
        self,
        handoff: CompletedAgentResultHandoff,
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
    ) -> (
        tuple[tuple[PlannedAgentInvocation, Awaitable[Any]], ...]
        | DispatchBatchLaunchFailure
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
                    subagent_registry=self._subagent_registry,
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
                    parent_run_id=parent_run_id_from_metadata(runtime_config.metadata),
                )
            except Exception as exc:
                logger.warning(
                    "Failed to launch subagent run %s for task %s",
                    assignment.agent_run_id,
                    runtime_config.chat_inputs.task_id,
                    exc_info=True,
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
                    return DispatchBatchLaunchFailure(
                        child_completions=settled_completions
                    )
                return DispatchBatchLaunchFailure(
                    stop=AgentRunDispatchStop(
                        invocation=item,
                        status="failed",
                    )
                )
            launched.append((item, child_task))
        return tuple(launched)

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

        terminal = await self._registry.get(
            tenant_id=item.assignment.tenant_id,
            task_id=item.assignment.task_id,
            agent_run_id=item.assignment.agent_run_id,
        )
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
        """Suppress later same-turn PAR cycles for declared irrelevant runs."""
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

    async def _existing_replayable_followup(
        self,
        assignment: AgentAssignment,
    ) -> LocalAgentRun | None:
        """Return an existing stable PAR follow-up to suppress replay launch."""
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
        completions: list[AgentRunCompletion] = []
        for (item, _child_task), result in zip(launched, settled, strict=True):
            assignment = item.assignment
            if isinstance(result, AgentRunCompletion):
                completions.append(result)
                continue

            if isinstance(
                result,
                (SubagentRunCancelled, SubagentRunPaused, SubagentRunFailed),
            ):
                terminal = await self._registry.get(
                    tenant_id=assignment.tenant_id,
                    task_id=assignment.task_id,
                    agent_run_id=assignment.agent_run_id,
                )
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
            terminal = await self._registry.get(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
            )
            if terminal is not None and terminal.result is not None:
                completions.append(
                    AgentRunCompletion(
                        result=terminal.result,
                        usage_records=(),
                        graph_thread_id=item.graph_thread_id,
                    )
                )
        return tuple(completions)

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

    async def _publish_entry_lifecycle(
        self,
        entry: LocalAgentRun,
        runtime_config: LangGraphRuntimeConfig,
    ) -> None:
        event = build_agent_run_lifecycle_event(
            entry,
            display_metadata=self._subagent_registry.display_metadata(entry.agent_id),
            parent_run_id=parent_run_id_from_metadata(runtime_config.metadata),
        )
        await self._publish_lifecycle(entry.task_id, event)

    @staticmethod
    def _record_batch_completions(
        completions: list[AgentRunCompletion | None],
        *,
        batch: list[PlannedAgentInvocation],
        batch_completions: tuple[AgentRunCompletion, ...],
    ) -> None:
        for completion in batch_completions:
            for item in batch:
                if item.assignment.agent_run_id == completion.result.agent_run_id:
                    completions[item.index] = completion
                    break


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


def _stop_for_child_exception(
    exc: BaseException,
    *,
    item: PlannedAgentInvocation,
    task_id: int,
    turn_index: int | None,
) -> AgentRunDispatchStop:
    """Map one terminal child exception into a transport-neutral stop."""
    assignment = item.assignment
    usage: tuple[Any, ...] = ()
    if isinstance(exc, (SubagentRunPaused, SubagentRunCancelled, SubagentRunFailed)):
        records = child_usage_records_from_state(
            getattr(exc.execution_result, "final_state", None),
            assignment=assignment,
            graph_thread_id=item.graph_thread_id,
        )
        usage = tuple(
            usage_envelopes_from_child_records(
                records,
                execution_branch="subagent_child",
                turn_index=turn_index,
            )
        )
        status: DispatchStopStatus = (
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
            task_id,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    return AgentRunDispatchStop(
        invocation=item,
        status=status,
        usage=usage,
    )


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
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text and text not in normalized:
            normalized.append(text)
    return tuple(normalized)


def _completed_entries(
    completions: list[AgentRunCompletion | None],
) -> tuple[AgentRunCompletion, ...]:
    return tuple(
        completion
        for completion in completions
        if isinstance(completion, AgentRunCompletion)
    )


def _safe_launch_error(exc: Exception, *, agent_display_name: str) -> str:
    _ = exc
    return f"{agent_display_name} launch failed"


__all__ = [
    "SubagentDispatchService",
]
