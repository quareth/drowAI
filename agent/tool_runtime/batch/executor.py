"""Batch execution scheduler (Phase 5 Task 5.1).

The :class:`BatchExecutor` is a *callback-driven scheduler*. It does not
import the coordinator, the graph tool executor, or the unified emitter —
all per-call work (id mint, approval gate, projection, budget decrement,
todo update) lives in the orchestrator's ``run_one_call`` callback. This
keeps every existing per-call wrapper alive when batches grow past a
single call (Phase 7).

Failure policy (locked by Phase 5 + Phase 6 partial-failure rules): per-
call tool failures are *terminal results* inside the batch, not executor
control-flow. The executor never retries failed calls and never raises for
ordinary tool failures returned by the callback (``status=FAILED/DENIED/
CANCELLED``). Sequential batches keep going through the manifest because
validated batch calls are independent; parallel batches wait for
already-started siblings to finish. Recovery is PTR's job, exercised
through the planner.

The parallel branch is gated by ``AgentConfig.parallel_execution_enabled``
upstream. The executor itself trusts the strategy passed in by the
orchestrator (the validator + compatibility checker have already approved
it). The parallel timeout is supplied via ``parallel_timeout_s`` so no
literal lives in this module.

Phase 7 Task 7.3: the executor accepts an optional ``cancel_check`` hook
that the orchestrator wires to its per-task cancellation channel. When
the hook returns ``True`` before dispatch, every call is short-circuited
with ``ToolCallStatus.CANCELLED`` and no callback tasks are launched.
Sequential mode also polls between calls so pending tail calls are marked
cancelled after partial progress. The aggregated status becomes
``BatchStatus.CANCELLED`` if no successes preceded cancellation (or
``COMPLETED_WITH_ERRORS`` when partial successes exist). The orchestrator
emits a single ``tool_batch_end`` event with ``status="cancelled"`` and
runs PTR once on the partial aggregate.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional, Sequence

from agent.execution_strategy import ExecutionStrategy
from agent.providers.llm.core.exceptions import LLMRefusalError
from agent.tool_runtime.batch.types import (
    BatchResult,
    BatchStatus,
    ToolBatch,
    ToolCall,
    ToolCallResult,
    ToolCallStatus,
)


_LOGGER = logging.getLogger(__name__)

#: Callback invoked for each ``ToolCall``. The orchestrator wires this to
#: its elevated per-call body (mint → approval → coordinator.run →
#: projection → budget decrement → todo update).
RunOneCall = Callable[[ToolCall], Awaitable[ToolCallResult]]

#: Optional callable returning ``True`` when the batch should be cancelled
#: before the next pending call starts. The orchestrator wires this to its
#: per-task cancellation channel.
CancelCheck = Callable[[], bool]


def _aggregate_status(results: Sequence[ToolCallResult]) -> BatchStatus:
    """Map per-call terminal statuses to the batch-level rollup status."""
    if not results:
        return BatchStatus.FAILED
    statuses = [r.status for r in results]
    if all(s is ToolCallStatus.DENIED for s in statuses):
        return BatchStatus.DENIED
    successes = [s for s in statuses if s is ToolCallStatus.SUCCESS]
    failures = [
        s
        for s in statuses
        if s in (ToolCallStatus.FAILED, ToolCallStatus.CANCELLED, ToolCallStatus.DENIED)
    ]
    if successes and not failures:
        return BatchStatus.COMPLETED
    if successes and failures:
        return BatchStatus.COMPLETED_WITH_ERRORS
    if not successes and any(s is ToolCallStatus.CANCELLED for s in statuses):
        return BatchStatus.CANCELLED
    return BatchStatus.FAILED


class BatchExecutor:
    """Schedules per-call execution for a validated :class:`ToolBatch`."""

    async def execute(
        self,
        batch: ToolBatch,
        *,
        run_one_call: RunOneCall,
        strategy: ExecutionStrategy,
        parallel_timeout_s: float,
        cancel_check: Optional[CancelCheck] = None,
    ) -> BatchResult:
        """Run ``batch`` and return the aggregated :class:`BatchResult`.

        Sequential mode runs the manifest in order; a failed independent
        call does not abort the loop. Parallel mode launches every call
        concurrently with an outer timeout (``parallel_timeout_s``). The
        executor never retries failed calls.

        ``cancel_check`` is an optional zero-arg callable returning ``True``
        when the batch should stop. Both strategies short-circuit before
        dispatch when it is already cancelled, and sequential mode polls it
        before each call.
        """
        if not batch.tool_calls:
            return BatchResult(
                tool_batch_id=batch.tool_batch_id,
                status=BatchStatus.FAILED,
                call_results=tuple(),
                effective_execution_strategy=strategy,
                requested_execution_strategy=batch.requested_execution_strategy,
            )

        if cancel_check is not None and cancel_check():
            results = [
                ToolCallResult(
                    tool_call_id=call.tool_call_id,
                    tool_id=call.tool_id,
                    status=ToolCallStatus.CANCELLED,
                    failure_category="batch_cancelled",
                )
                for call in batch.tool_calls
            ]
        elif strategy is ExecutionStrategy.SEQUENTIAL:
            results = await self._run_sequential(
                batch.tool_calls,
                run_one_call,
                cancel_check=cancel_check,
            )
        else:
            results = await self._run_parallel(
                batch.tool_calls, run_one_call, timeout_s=parallel_timeout_s
            )

        return BatchResult(
            tool_batch_id=batch.tool_batch_id,
            status=_aggregate_status(results),
            call_results=tuple(results),
            effective_execution_strategy=strategy,
            requested_execution_strategy=batch.requested_execution_strategy,
        )

    @staticmethod
    async def _run_sequential(
        calls: Sequence[ToolCall],
        run_one_call: RunOneCall,
        *,
        cancel_check: Optional[CancelCheck] = None,
    ) -> list[ToolCallResult]:
        results: list[ToolCallResult] = []
        cancelled = False
        for call in calls:
            if cancelled or (cancel_check is not None and cancel_check()):
                cancelled = True
                results.append(
                    ToolCallResult(
                        tool_call_id=call.tool_call_id,
                        tool_id=call.tool_id,
                        status=ToolCallStatus.CANCELLED,
                        failure_category="batch_cancelled",
                    )
                )
                continue
            try:
                results.append(await run_one_call(call))
            except LLMRefusalError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                _LOGGER.exception(
                    "BatchExecutor sequential callback raised for tool_call_id=%s",
                    call.tool_call_id,
                )
                results.append(
                    ToolCallResult(
                        tool_call_id=call.tool_call_id,
                        tool_id=call.tool_id,
                        status=ToolCallStatus.FAILED,
                        failure_category="executor_callback_error",
                        error_message=str(exc),
                    )
                )
        return results

    @staticmethod
    async def _run_parallel(
        calls: Sequence[ToolCall],
        run_one_call: RunOneCall,
        *,
        timeout_s: float,
    ) -> list[ToolCallResult]:
        async def _wrapped(call: ToolCall) -> ToolCallResult:
            try:
                return await run_one_call(call)
            except LLMRefusalError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                _LOGGER.exception(
                    "BatchExecutor parallel callback raised for tool_call_id=%s",
                    call.tool_call_id,
                )
                return ToolCallResult(
                    tool_call_id=call.tool_call_id,
                    tool_id=call.tool_id,
                    status=ToolCallStatus.FAILED,
                    failure_category="executor_callback_error",
                    error_message=str(exc),
                )

        tasks = [asyncio.create_task(_wrapped(call)) for call in calls]
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=False),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            # Already-started siblings finish, but we mark unfinished ones.
            results = []
            for call, task in zip(calls, tasks):
                if task.done() and not task.cancelled():
                    try:
                        results.append(task.result())
                    except LLMRefusalError:
                        raise
                    except Exception as exc:  # pragma: no cover - defensive
                        results.append(
                            ToolCallResult(
                                tool_call_id=call.tool_call_id,
                                tool_id=call.tool_id,
                                status=ToolCallStatus.FAILED,
                                failure_category="executor_callback_error",
                                error_message=str(exc),
                            )
                        )
                else:
                    task.cancel()
                    results.append(
                        ToolCallResult(
                            tool_call_id=call.tool_call_id,
                            tool_id=call.tool_id,
                            status=ToolCallStatus.CANCELLED,
                            failure_category="parallel_batch_timeout",
                        )
                    )
        return results


__all__ = ["BatchExecutor", "RunOneCall", "CancelCheck"]
