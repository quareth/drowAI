"""One-batch launch coordination for subagent dispatch.

This module owns register-to-launch side effects for one already-admitted
ordered batch. It does not own admission, parent-handoff coordination,
settlement policy, facade outcomes, or presentation formatting.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Sequence
from typing import Any

from agent.subagents.registry import SubagentRegistry
from backend.services.langgraph_chat.contracts import LangGraphRuntimeConfig
from backend.services.llm_provider.runtime_services import attach_runtime_services

from .assignment_builder import parent_run_id_from_metadata
from .completion import AgentRunCompletion
from .contracts import AgentAssignment
from .dispatch_contracts import (
    AgentRunDispatchStop,
    AgentRunLaunchService,
    DispatchBatchChild,
    DispatchBatchLaunch,
    DispatchBatchLaunchFailure,
)
from .dispatch_plan import PlannedAgentInvocation
from .dispatch_settlement import DispatchSettlement
from .event_projection import build_agent_run_lifecycle_event
from .execution_config import build_child_execution_config
from .launcher import LifecyclePublisher
from .registry import ProcessLocalAgentRunRegistry
from .registry_contracts import LocalAgentRun

logger = logging.getLogger(__name__)


class DispatchBatchExecutor:
    """Launch one admitted batch while preserving registry and stream ordering."""

    def __init__(
        self,
        *,
        registry: ProcessLocalAgentRunRegistry,
        launcher: AgentRunLaunchService,
        subagent_registry: SubagentRegistry,
        lifecycle_publisher: LifecyclePublisher,
        settlement: DispatchSettlement,
    ) -> None:
        self._registry = registry
        self._launcher = launcher
        self._subagent_registry = subagent_registry
        self._publish_lifecycle = lifecycle_publisher
        self._settlement = settlement

    async def launch_batch(
        self,
        batch: Sequence[PlannedAgentInvocation],
        runtime_config: LangGraphRuntimeConfig,
    ) -> DispatchBatchLaunch | DispatchBatchLaunchFailure:
        """Register, mark running, and launch one already-admitted batch."""
        launched: list[tuple[PlannedAgentInvocation, Awaitable[Any]]] = []
        children: list[DispatchBatchChild] = []
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
                settled_completions = (
                    await self._settlement.settle_launched_batch_on_failure(launched)
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
            children.append(DispatchBatchChild(invocation=item, terminal=child_task))
        return DispatchBatchLaunch(children=tuple(children))

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


def _safe_launch_error(exc: Exception, *, agent_display_name: str) -> str:
    _ = exc
    return f"{agent_display_name} launch failed"


__all__ = [
    "DispatchBatchExecutor",
]
