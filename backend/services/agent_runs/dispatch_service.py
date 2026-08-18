"""Dispatch validated subagent plans through the process-local run lifecycle.

The service owns capacity-aware scheduling and typed application outcomes. It
delegates one-batch launch and settlement mechanics to their canonical
collaborators and deliberately has no knowledge of chat response formatting.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from agent.subagents.registry import SubagentRegistry
from core.skills.registry import SkillRegistry, get_skill_registry
from backend.services.langgraph_chat.contracts import LangGraphRuntimeConfig

from .completion import AgentRunCompletion
from .dispatch_batch import DispatchBatchExecutor
from .dispatch_contracts import (
    AgentRunDispatchResult,
    AgentRunDispatchStop,
    AgentRunLaunchService,
    DispatchBatchLaunchFailure,
    ReadyHandoffProjector,
    ReadyHandoffProcessor,
)
from .dispatch_followup import FollowupDispatcher
from .dispatch_plan import PlannedAgentInvocation
from .dispatch_settlement import DispatchSettlement
from .launcher import LifecyclePublisher
from .parent_handoff_coordinator import (
    ParentFollowupDelegation,
    ParentHandoffOutcome,
)
from .registry import ProcessLocalAgentRunRegistry
from .registry_contracts import (
    ACTIVE_AGENT_RUN_STATUSES,
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
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        self._registry = registry
        self._subagent_registry = subagent_registry
        resolved_skill_registry = skill_registry or get_skill_registry()
        self._settlement = DispatchSettlement(registry=registry)
        self._batch_executor = DispatchBatchExecutor(
            registry=registry,
            launcher=launcher,
            subagent_registry=subagent_registry,
            lifecycle_publisher=lifecycle_publisher,
            settlement=self._settlement,
        )
        self._followup_dispatcher = FollowupDispatcher(
            registry=registry,
            subagent_registry=subagent_registry,
            batch_executor=self._batch_executor,
            active_count_reader=self._active_counts_for_plan,
            skill_registry=resolved_skill_registry,
        )

    async def dispatch(
        self,
        plan: tuple[PlannedAgentInvocation, ...],
        runtime_config: LangGraphRuntimeConfig,
        *,
        parent_turn_sequence: int | None,
        process_ready_handoffs: ReadyHandoffProcessor,
        project_ready_handoffs: ReadyHandoffProjector | None = None,
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
                        status="interrupted",
                    )
                )

            launch_result = await self._batch_executor.launch_batch(
                batch,
                runtime_config,
            )
            if isinstance(launch_result, DispatchBatchLaunchFailure):
                if launch_result.stop is not None:
                    return AgentRunDispatchResult(stop=launch_result.stop)
                batch_completions = launch_result.child_completions
                self._settlement.record_batch_completions(
                    completions,
                    batch=batch,
                    batch_completions=batch_completions,
                )
                batch_outcome = await process_ready_handoffs(batch_completions, False)
                return AgentRunDispatchResult(
                    child_completions=self._settlement.completed_entries(completions),
                    parent_handoff_outcome=batch_outcome,
                )

            child_tasks = launch_result.children
            should_process_parent = len(child_tasks) > 1 and not deferred
            result_tasks = tuple(
                asyncio.create_task(
                    self._settlement.require_child_task_result(
                        child.terminal,
                        assignment=child.invocation.assignment,
                        graph_thread_id=child.invocation.graph_thread_id,
                    )
                )
                for child in child_tasks
            )
            pending_results = set(result_tasks)
            try:
                while pending_results:
                    done, pending_results = await asyncio.wait(
                        pending_results,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if (
                        project_ready_handoffs is not None
                        and should_process_parent
                        and pending_results
                    ):
                        partial_completions: list[AgentRunCompletion] = []
                        for task in result_tasks:
                            if task not in done:
                                continue
                            result = task.result()
                            if isinstance(result, AgentRunCompletion):
                                partial_completions.append(result)
                        if partial_completions:
                            await project_ready_handoffs(
                                tuple(partial_completions)
                            )
            except BaseException:
                for task in pending_results:
                    task.cancel()
                await asyncio.gather(*result_tasks, return_exceptions=True)
                raise
            batch_results = tuple(task.result() for task in result_tasks)

            paused = False
            for child, batch_result in zip(child_tasks, batch_results, strict=True):
                item = child.invocation
                active_counts[item.assignment.agent_id] = max(
                    0,
                    active_counts.get(item.assignment.agent_id, 0) - 1,
                )
                settlement = await self._settlement.settle_child_result(
                    batch_result,
                    item=item,
                    task_id=runtime_config.chat_inputs.task_id,
                    turn_index=parent_turn_sequence,
                )
                if settlement.paused:
                    paused = True
                    continue
                if settlement.completion is not None:
                    completions[item.index] = settlement.completion
                    continue
                if settlement.stop is not None:
                    return AgentRunDispatchResult(stop=settlement.stop)

            batch_completions = tuple(
                completions[item.index]
                for item in batch
                if isinstance(completions[item.index], AgentRunCompletion)
            )
            if paused:
                resumed_outcome = await process_ready_handoffs(
                    batch_completions,
                    True,
                )
                if resumed_outcome is None:
                    raise RuntimeError(
                        "Subagent approval resume completed without a parent handoff"
                    )
                return AgentRunDispatchResult(
                    child_completions=self._settlement.completed_entries(completions),
                    parent_handoff_outcome=resumed_outcome,
                )
            if should_process_parent:
                early_outcome = await process_ready_handoffs(
                    batch_completions,
                    True,
                )
                if early_outcome is not None:
                    parent_handoff_outcome = early_outcome

            pending = deferred

        return AgentRunDispatchResult(
            child_completions=self._settlement.completed_entries(completions),
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
        return await self._followup_dispatcher.dispatch_followup(
            runtime_config,
            parent_turn_id=parent_turn_id,
            agent_handoff=agent_handoff,
            decision_id=decision_id,
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
        return await self._settlement.completions_for_handoff(
            handoff,
            tenant_id=tenant_id,
            task_id=task_id,
            completion_by_run_id=completion_by_run_id,
        )

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


__all__ = [
    "SubagentDispatchService",
]
