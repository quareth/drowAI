"""Barrier and coordinate completed process-local subagent handoffs.

The coordinator owns the all-runs-terminal barrier, registry claim lifecycle,
and serialized parent continuation entry for one parent task. It does not build
prompts, launch child runs, or mutate registry internals outside the public
claim API.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from backend.services.langgraph_chat.contracts import LangGraphChatResult
from agent.graph.context.contracts import ActiveAgentRun
from agent.subagents.registry import SubagentRegistry
from backend.services.metrics.utils import safe_gauge, safe_inc

from .completion import AgentRunCompletion
from .event_projection import build_parent_handoff_progress_events
from .parent_control import ParentControlOutcome, parse_parent_control_outcome
from .registry import ProcessLocalAgentRunRegistry
from .registry_contracts import ClaimedHandoffBatch
from .result_projection import (
    AgentRunResultProjector,
    CompletedAgentResultHandoff,
    attach_active_agent_runs_to_context,
    attach_completed_agent_results_to_context,
)


ParentContinuationRunner = Callable[
    [CompletedAgentResultHandoff, tuple[ActiveAgentRun, ...]],
    Awaitable[LangGraphChatResult],
]
FollowupDelegationDispatcher = Callable[
    [Mapping[str, Any], str],
    Awaitable["ParentFollowupDelegation"],
]
ParentProgressPublisher = Callable[[int, tuple[dict[str, Any], ...]], Awaitable[None]]


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ParentHandoffGuardEntry:
    """One task guard plus its current holder and waiter count."""

    lock: asyncio.Lock
    users: int = 0


class ParentHandoffGuardPool:
    """Serialize parent handoffs per task and discard guards when idle."""

    def __init__(self) -> None:
        self._entries_lock = asyncio.Lock()
        self._entries: dict[tuple[int, int], _ParentHandoffGuardEntry] = {}

    @asynccontextmanager
    async def acquire(
        self,
        *,
        tenant_id: int,
        task_id: int,
    ) -> AsyncIterator[None]:
        """Hold the shared guard for one tenant/task while counting waiters."""
        key = (tenant_id, task_id)
        async with self._entries_lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _ParentHandoffGuardEntry(lock=asyncio.Lock())
                self._entries[key] = entry
            entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            async with self._entries_lock:
                entry.users -= 1
                if entry.users == 0:
                    self._entries.pop(key, None)


@dataclass(frozen=True, slots=True)
class ParentHandoffOutcome:
    """Result of one serialized parent handoff processing cycle."""

    result: LangGraphChatResult
    claim_id: str
    agent_run_ids: tuple[str, ...]
    child_completions: tuple[AgentRunCompletion, ...]


@dataclass(frozen=True, slots=True)
class ParentFollowupDelegation:
    """Stable launch summary for one PAR-authored follow-up delegation."""

    agent_run_ids: tuple[str, ...]
    launched_agent_run_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PreparedParentHandoff:
    """Typed context prepared from one claimed registry snapshot."""

    handoff: CompletedAgentResultHandoff
    active_runs: tuple[ActiveAgentRun, ...]


class ParentHandoffCoordinator:
    """Wait for scoped runs and serialize one aggregate parent continuation."""

    def __init__(
        self,
        *,
        registry: ProcessLocalAgentRunRegistry,
        subagent_registry: SubagentRegistry,
        guard_pool: ParentHandoffGuardPool,
        result_projector: AgentRunResultProjector | None = None,
        parent_progress_publisher: ParentProgressPublisher | None = None,
    ) -> None:
        self._registry = registry
        self._guard_pool = guard_pool
        self._result_projector = result_projector or AgentRunResultProjector(
            registry=registry,
            subagent_registry=subagent_registry,
        )
        self._publish_parent_progress = parent_progress_publisher

    async def process_ready_handoffs(
        self,
        *,
        tenant_id: int,
        task_id: int,
        conversation_id: str,
        parent_turn_id: str,
        metadata: dict[str, Any],
        run_parent_continuation: ParentContinuationRunner,
        dispatch_followup_delegation: FollowupDelegationDispatcher | None = None,
        child_completions: tuple[AgentRunCompletion, ...] = (),
        wait_for_initial_handoff: bool = False,
        wait_timeout_seconds: float | None = None,
    ) -> ParentHandoffOutcome | None:
        """Wait for scoped runs to finish, then process one aggregate claim.

        Partial claims may emit deterministic progress but are released while
        any scoped run remains active. The final claim is released if the
        continuation raises or returns a cancelled result, leaving its
        handoffs claimable by a later retry.
        """
        async with self._guard_pool.acquire(
            tenant_id=tenant_id,
            task_id=task_id,
        ):
            wait_for_next_handoff = False
            while True:
                claim = await self._registry.claim_ready_handoffs(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    conversation_id=conversation_id,
                )
                if claim is None:
                    if not wait_for_next_handoff and not wait_for_initial_handoff:
                        return None
                    version = await self._registry.state_version()
                    claim = await self._registry.claim_ready_handoffs(
                        tenant_id=tenant_id,
                        task_id=task_id,
                        conversation_id=conversation_id,
                    )
                    if claim is None:
                        wait_status = await self._wait_for_relevant_registry_change(
                            tenant_id=tenant_id,
                            task_id=task_id,
                            conversation_id=conversation_id,
                            after_version=version,
                            wait_timeout_seconds=wait_timeout_seconds,
                        )
                        if wait_status == "inactive":
                            raise RuntimeError(
                                "PAR wait ended with no active subagent runs "
                                "and no ready handoffs"
                            )
                        continue
                wait_for_initial_handoff = False

                prepared = await self._prepare_claim_context(
                    metadata=metadata,
                    conversation_id=conversation_id,
                    parent_turn_id=parent_turn_id,
                    task_id=task_id,
                    claim=claim,
                )

                if prepared.active_runs:
                    await self._registry.release_handoffs(claim.claim_id)
                    version = await self._registry.state_version()
                    await self._wait_for_relevant_registry_change(
                        tenant_id=tenant_id,
                        task_id=task_id,
                        conversation_id=conversation_id,
                        after_version=version,
                        wait_timeout_seconds=wait_timeout_seconds,
                        require_inactive=True,
                    )
                    wait_for_initial_handoff = True
                    continue

                claim_acknowledged = False
                try:
                    result = await run_parent_continuation(
                        prepared.handoff,
                        prepared.active_runs,
                    )
                    control = parse_parent_control_outcome(
                        result.metadata,
                        parent_turn_id=parent_turn_id,
                        claimed_agent_run_ids=claim.agent_run_ids,
                    )
                    if control.action == "delegate_subagent":
                        delegation = await self._dispatch_followup(
                            tenant_id=tenant_id,
                            task_id=task_id,
                            metadata=metadata,
                            claim=claim,
                            active_runs=prepared.active_runs,
                            control=control,
                            dispatcher=dispatch_followup_delegation,
                        )
                        claim_acknowledged = True
                        if not delegation.agent_run_ids:
                            return ParentHandoffOutcome(
                                result=result,
                                claim_id=claim.claim_id,
                                agent_run_ids=claim.agent_run_ids,
                                child_completions=child_completions,
                            )
                        wait_for_next_handoff = True
                        continue

                    if control.action == "wait_for_subagents":
                        await self._acknowledge_wait_control(
                            metadata=metadata,
                            claim=claim,
                            active_runs=prepared.active_runs,
                            control=control,
                        )
                        claim_acknowledged = True
                        version = await self._registry.state_version()
                        wait_status = await self._wait_for_relevant_registry_change(
                            tenant_id=tenant_id,
                            task_id=task_id,
                            conversation_id=conversation_id,
                            after_version=version,
                            wait_timeout_seconds=wait_timeout_seconds,
                        )
                        if wait_status == "inactive":
                            raise RuntimeError(
                                "PAR wait ended with no active subagent runs "
                                "and no ready handoffs"
                            )
                        wait_for_next_handoff = True
                        continue
                except BaseException as exc:
                    if not claim_acknowledged:
                        _record_claim_release_after_parent_exit(
                            task_id=task_id,
                            claim=claim,
                            cause=(
                                "cancellation"
                                if isinstance(exc, asyncio.CancelledError)
                                else "error"
                            ),
                        )
                        await self._registry.release_handoffs(claim.claim_id)
                    raise

                await self._settle_parent_result(
                    task_id=task_id,
                    claim=claim,
                    result=result,
                )

                return ParentHandoffOutcome(
                    result=result,
                    claim_id=claim.claim_id,
                    agent_run_ids=claim.agent_run_ids,
                    child_completions=child_completions,
                )

    async def _prepare_claim_context(
        self,
        *,
        task_id: int,
        conversation_id: str,
        parent_turn_id: str,
        metadata: dict[str, Any],
        claim: ClaimedHandoffBatch,
    ) -> _PreparedParentHandoff:
        """Project, attach, and announce one claimed handoff snapshot."""
        handoff = self._handoff_from_claim(claim)
        active_runs = self._active_runs_from_claim(claim)
        _record_claim_observed(
            task_id=task_id,
            claim=claim,
            active_run_count=len(active_runs),
        )
        attach_completed_agent_results_to_context(metadata, handoff)
        attach_active_agent_runs_to_context(metadata, active_runs)
        await self._emit_parent_progress(
            task_id=task_id,
            conversation_id=conversation_id,
            parent_turn_id=parent_turn_id,
            metadata=metadata,
            claim=claim,
            handoff=handoff,
            active_runs=active_runs,
            action="waiting" if active_runs else "evaluating",
        )
        return _PreparedParentHandoff(
            handoff=handoff,
            active_runs=active_runs,
        )

    async def _dispatch_followup(
        self,
        *,
        tenant_id: int,
        task_id: int,
        metadata: dict[str, Any],
        claim: ClaimedHandoffBatch,
        active_runs: tuple[ActiveAgentRun, ...],
        control: ParentControlOutcome,
        dispatcher: FollowupDelegationDispatcher | None,
    ) -> ParentFollowupDelegation:
        """Dispatch one validated follow-up and consume its current claim."""
        if dispatcher is None:
            safe_inc("post_action_reasoning_followup_delegation_rejected")
            raise RuntimeError("PAR follow-up delegation has no dispatcher")
        assert control.agent_handoff is not None
        try:
            delegation = await dispatcher(
                control.agent_handoff,
                control.decision_id,
            )
        except Exception:
            safe_inc("post_action_reasoning_followup_delegation_rejected")
            logger.info(
                "PAR follow-up delegation rejected tenant_id=%s task_id=%s "
                "claim_id=%s decision_id=%s",
                tenant_id,
                task_id,
                claim.claim_id,
                control.decision_id,
            )
            raise
        metric = (
            "post_action_reasoning_followup_delegation_accepted"
            if delegation.agent_run_ids
            else "post_action_reasoning_followup_delegation_rejected"
        )
        safe_inc(metric)
        logger.info(
            "PAR follow-up delegation processed tenant_id=%s task_id=%s "
            "claim_id=%s decision_id=%s accepted=%s launched_run_count=%s",
            tenant_id,
            task_id,
            claim.claim_id,
            control.decision_id,
            bool(delegation.agent_run_ids),
            len(delegation.launched_agent_run_ids),
        )
        metadata["last_parent_control_outcome"] = {
            "action": control.action,
            "decision_id": control.decision_id,
            "agent_run_ids": list(delegation.agent_run_ids),
            "launched_agent_run_ids": list(delegation.launched_agent_run_ids),
            "completed_agent_run_ids": list(claim.agent_run_ids),
            "active_agent_run_ids": _active_agent_run_ids(active_runs),
        }
        await self._registry.acknowledge_handoffs(claim.claim_id)
        return delegation

    async def _acknowledge_wait_control(
        self,
        *,
        metadata: dict[str, Any],
        claim: ClaimedHandoffBatch,
        active_runs: tuple[ActiveAgentRun, ...],
        control: ParentControlOutcome,
    ) -> None:
        """Validate a wait decision, record it, and consume its current claim."""
        if not active_runs:
            raise RuntimeError(
                "PAR wait_for_subagents outcome had no active subagent runs "
                "in the claimed snapshot"
            )
        metadata["last_parent_control_outcome"] = {
            "action": control.action,
            "decision_id": control.decision_id,
            "completed_agent_run_ids": list(claim.agent_run_ids),
            "active_agent_run_ids": _active_agent_run_ids(active_runs),
        }
        await self._registry.acknowledge_handoffs(claim.claim_id)

    async def _settle_parent_result(
        self,
        *,
        task_id: int,
        claim: ClaimedHandoffBatch,
        result: LangGraphChatResult,
    ) -> None:
        """Release cancelled work or acknowledge a finalized parent result."""
        if _is_cancelled_result(result):
            _record_claim_release_after_parent_exit(
                task_id=task_id,
                claim=claim,
                cause="cancellation",
            )
            await self._registry.release_handoffs(claim.claim_id)
            return
        await self._registry.acknowledge_handoffs(claim.claim_id)
        safe_inc("post_action_reasoning_parent_finalization_count")

    def _handoff_from_claim(
        self, claim: ClaimedHandoffBatch
    ) -> CompletedAgentResultHandoff:
        return CompletedAgentResultHandoff(
            results=tuple(
                self._result_projector.project_result(result)
                for result in claim.results
            ),
            agent_run_ids=claim.agent_run_ids,
        )

    def _active_runs_from_claim(
        self, claim: ClaimedHandoffBatch
    ) -> tuple[ActiveAgentRun, ...]:
        return tuple(
            self._result_projector.project_active_run(entry)
            for entry in claim.active_runs
        )

    async def _wait_for_relevant_registry_change(
        self,
        *,
        tenant_id: int,
        task_id: int,
        conversation_id: str,
        after_version: int,
        wait_timeout_seconds: float | None,
        require_inactive: bool = False,
    ) -> str:
        started_at = perf_counter()
        if require_inactive:
            wait_coro = self._registry.wait_for_inactive_handoffs(
                tenant_id=tenant_id,
                task_id=task_id,
                conversation_id=conversation_id,
                after_version=after_version,
            )
        else:
            wait_coro = self._registry.wait_for_ready_handoffs_or_inactive(
                tenant_id=tenant_id,
                task_id=task_id,
                conversation_id=conversation_id,
                after_version=after_version,
            )
        resume_cause = "unknown"
        try:
            if wait_timeout_seconds is None:
                wait_status = await wait_coro
            else:
                wait_status = await asyncio.wait_for(
                    wait_coro,
                    timeout=wait_timeout_seconds,
                )
            resume_cause = wait_status
            return wait_status
        except asyncio.TimeoutError:
            resume_cause = "timeout"
            raise
        except asyncio.CancelledError:
            resume_cause = "cancelled"
            raise
        except Exception:
            resume_cause = "error"
            raise
        finally:
            elapsed_ms = max(0, int((perf_counter() - started_at) * 1000))
            safe_gauge("post_action_reasoning_wait_duration_ms", elapsed_ms)
            safe_inc(f"post_action_reasoning_wait_resume_cause_{resume_cause}")
            logger.info(
                "PAR wait completed tenant_id=%s task_id=%s conversation_id=%s "
                "resume_cause=%s duration_ms=%s",
                tenant_id,
                task_id,
                conversation_id,
                resume_cause,
                elapsed_ms,
            )

    async def _emit_parent_progress(
        self,
        *,
        task_id: int,
        conversation_id: str,
        parent_turn_id: str,
        metadata: Mapping[str, Any],
        claim: ClaimedHandoffBatch,
        handoff: CompletedAgentResultHandoff,
        active_runs: tuple[ActiveAgentRun, ...],
        action: str,
    ) -> None:
        if self._publish_parent_progress is None:
            return
        events = build_parent_handoff_progress_events(
            completed_results=handoff.results,
            active_runs=active_runs,
            conversation_id=conversation_id,
            parent_turn_id=parent_turn_id,
            claim_id=claim.claim_id,
            action=action,
            turn_sequence=_turn_sequence_from_metadata(metadata),
        )
        try:
            await self._publish_parent_progress(task_id, events)
        except Exception:
            logger.debug(
                "Failed to publish parent handoff progress for task %s claim %s",
                task_id,
                claim.claim_id,
                exc_info=True,
            )


def _is_cancelled_result(result: LangGraphChatResult) -> bool:
    return result.metadata.get("status") == "cancelled"


def _active_agent_run_ids(
    active_runs: tuple[ActiveAgentRun, ...],
) -> list[str]:
    """Return serialized run identities from typed active-run context."""
    return [
        run["agent_run_id"]
        for run in active_runs
        if isinstance(run.get("agent_run_id"), str)
    ]


def _record_claim_observed(
    *,
    task_id: int,
    claim: ClaimedHandoffBatch,
    active_run_count: int,
) -> None:
    """Record bounded telemetry for the claimed handoff batch."""
    safe_inc("post_action_reasoning_handoff_claim_observed")
    safe_gauge("post_action_reasoning_handoff_batch_size", len(claim.agent_run_ids))
    safe_gauge("post_action_reasoning_active_run_count", active_run_count)
    logger.info(
        "PAR handoff claim observed tenant_id=%s task_id=%s claim_id=%s "
        "handoff_batch_size=%s active_run_count=%s",
        claim.tenant_id,
        task_id,
        claim.claim_id,
        len(claim.agent_run_ids),
        active_run_count,
    )


def _record_claim_release_after_parent_exit(
    *,
    task_id: int,
    claim: ClaimedHandoffBatch,
    cause: str,
) -> None:
    """Record retryable handoff claim release after parent exit."""
    safe_inc("post_action_reasoning_claim_release_after_error_or_cancellation")
    safe_inc(f"post_action_reasoning_claim_release_after_{cause}")
    logger.info(
        "PAR handoff claim released tenant_id=%s task_id=%s claim_id=%s "
        "cause=%s batch_size=%s",
        claim.tenant_id,
        task_id,
        claim.claim_id,
        cause,
        len(claim.agent_run_ids),
    )


def _turn_sequence_from_metadata(metadata: Mapping[str, Any]) -> int | None:
    value = metadata.get("turn_sequence")
    if isinstance(value, int):
        return value
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "FollowupDelegationDispatcher",
    "ParentFollowupDelegation",
    "ParentContinuationRunner",
    "ParentHandoffCoordinator",
    "ParentHandoffGuardPool",
    "ParentHandoffOutcome",
    "ParentProgressPublisher",
]
