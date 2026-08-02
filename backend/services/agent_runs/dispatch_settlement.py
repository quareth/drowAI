"""Settlement and reconciliation helpers for subagent dispatch.

This module owns child terminal-result validation, registry-backed completion
recovery, launch-failure sibling cleanup, handoff completion lookup, and
explicit irrelevant-result consumption. It does not schedule work, publish
lifecycle events, launch child runs, parse parent-outcome metadata, or process
parent handoffs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any

from backend.services.langgraph_chat.contracts import LangGraphRuntimeConfig

from .completion import (
    AgentRunCompletion,
    build_agent_run_completion,
    child_usage_records_from_state,
    usage_envelopes_from_child_records,
)
from .contracts import AgentAssignment, AgentResult
from .dispatch_contracts import (
    AgentRunDispatchStop,
    DispatchStopStatus,
)
from .dispatch_plan import PlannedAgentInvocation
from .launcher import SubagentRunCancelled, SubagentRunFailed, SubagentRunPaused
from .registry import ProcessLocalAgentRunRegistry
from .result_projection import CompletedAgentResultHandoff

logger = logging.getLogger(__name__)


class DispatchSettlement:
    """Translate and reconcile child terminal facts using the run registry."""

    def __init__(self, *, registry: ProcessLocalAgentRunRegistry) -> None:
        self._registry = registry

    async def require_child_task(
        self,
        value: Any,
        *,
        assignment: AgentAssignment,
        graph_thread_id: str,
    ) -> AgentRunCompletion:
        """Await and validate the launcher's terminal subagent result."""
        if not isinstance(value, Awaitable):
            raise RuntimeError(
                "Subagent launcher did not return an awaitable result task"
            )
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

    async def require_child_task_result(
        self,
        value: Any,
        *,
        assignment: AgentAssignment,
        graph_thread_id: str,
    ) -> AgentRunCompletion | BaseException:
        """Return child completion or the original terminal exception instance."""
        try:
            return await self.require_child_task(
                value,
                assignment=assignment,
                graph_thread_id=graph_thread_id,
            )
        except BaseException as exc:
            return exc

    async def completion_for_terminal_exception(
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

    def stop_for_child_exception(
        self,
        exc: BaseException,
        *,
        item: PlannedAgentInvocation,
        task_id: int,
        turn_index: int | None,
    ) -> AgentRunDispatchStop:
        """Map one terminal child exception into a transport-neutral stop."""
        assignment = item.assignment
        usage: tuple[Any, ...] = ()
        if isinstance(
            exc,
            (SubagentRunPaused, SubagentRunCancelled, SubagentRunFailed),
        ):
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

    async def settle_launched_batch_on_failure(
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
                self.require_child_task_result(
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

    def record_batch_completions(
        self,
        completions: list[AgentRunCompletion | None],
        *,
        batch: list[PlannedAgentInvocation],
        batch_completions: tuple[AgentRunCompletion, ...],
    ) -> None:
        """Place batch completions at their immutable plan indexes."""
        for completion in batch_completions:
            for item in batch:
                if item.assignment.agent_run_id == completion.result.agent_run_id:
                    completions[item.index] = completion
                    break

    def completed_entries(
        self,
        completions: list[AgentRunCompletion | None],
    ) -> tuple[AgentRunCompletion, ...]:
        """Return completed entries in their already-recorded plan order."""
        return tuple(
            completion
            for completion in completions
            if isinstance(completion, AgentRunCompletion)
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

    async def consume_irrelevant_terminal_results(
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


__all__ = [
    "DispatchSettlement",
]
