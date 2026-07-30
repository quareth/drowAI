"""Coordinate parent processing of completed process-local subagent handoffs.

The coordinator owns registry claim lifecycle and serialized parent continuation
entry for one parent task. It does not build prompts, launch child runs, or mutate
registry internals outside the public claim API.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from backend.services.langgraph_chat.contracts import LangGraphChatResult
from backend.services.agent_runs.ownership_policy import normalize_agent_handoff_entries
from backend.services.metrics.utils import safe_gauge, safe_inc

from .completion import AgentRunCompletion
from .event_projection import build_parent_handoff_progress_events
from .registry import ClaimedHandoffBatch, ProcessLocalAgentRunRegistry
from .result_projection import (
    AgentRunResultProjector,
    CompletedAgentResultHandoff,
    attach_active_agent_runs_to_context,
    attach_completed_agent_results_to_context,
)


ParentContinuationRunner = Callable[
    [CompletedAgentResultHandoff, tuple[dict[str, Any], ...]],
    Awaitable[LangGraphChatResult],
]
FollowupDelegationDispatcher = Callable[
    [Mapping[str, Any], str],
    Awaitable["ParentFollowupDelegation"],
]
ParentProgressPublisher = Callable[[int, tuple[dict[str, Any], ...]], Awaitable[None]]


logger = logging.getLogger(__name__)


_GUARDS_LOCK = asyncio.Lock()
_GUARDS: dict[tuple[int, int], asyncio.Lock] = {}


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
class _ParentControlOutcome:
    """Backend-owned parent-control action returned by the PAR graph."""

    action: str
    agent_handoff: Mapping[str, Any] | None = None
    decision_id: str = ""


class ParentHandoffCoordinator:
    """Serialize ready handoff delivery into one parent continuation cycle."""

    def __init__(
        self,
        *,
        registry: ProcessLocalAgentRunRegistry,
        result_projector: AgentRunResultProjector | None = None,
        parent_progress_publisher: ParentProgressPublisher | None = None,
    ) -> None:
        self._registry = registry
        self._result_projector = result_projector or AgentRunResultProjector(
            registry=registry
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
        """Claim currently ready handoffs and run one parent continuation.

        The registry claim is released if the continuation raises or returns a
        cancelled result, leaving handoffs claimable by a later retry.
        """
        guard = await _guard_for(
            tenant_id=tenant_id,
            task_id=task_id,
        )
        async with guard:
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
                    action="evaluating",
                )

                claim_acknowledged = False
                try:
                    result = await run_parent_continuation(handoff, active_runs)
                    control = _extract_parent_control_outcome(
                        result,
                        parent_turn_id=parent_turn_id,
                        claim=claim,
                    )
                    if control.action == "delegate_subagent":
                        if dispatch_followup_delegation is None:
                            safe_inc(
                                "post_action_reasoning_followup_delegation_rejected"
                            )
                            raise RuntimeError(
                                "PAR follow-up delegation has no dispatcher"
                            )
                        assert control.agent_handoff is not None
                        try:
                            delegation = await dispatch_followup_delegation(
                                control.agent_handoff,
                                control.decision_id,
                            )
                        except Exception:
                            safe_inc(
                                "post_action_reasoning_followup_delegation_rejected"
                            )
                            logger.info(
                                "PAR follow-up delegation rejected "
                                "tenant_id=%s task_id=%s claim_id=%s "
                                "decision_id=%s",
                                tenant_id,
                                task_id,
                                claim.claim_id,
                                control.decision_id,
                            )
                            raise
                        if delegation.agent_run_ids:
                            safe_inc(
                                "post_action_reasoning_followup_delegation_accepted"
                            )
                        else:
                            safe_inc(
                                "post_action_reasoning_followup_delegation_rejected"
                            )
                        logger.info(
                            "PAR follow-up delegation processed "
                            "tenant_id=%s task_id=%s claim_id=%s decision_id=%s "
                            "accepted=%s launched_run_count=%s",
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
                            "launched_agent_run_ids": list(
                                delegation.launched_agent_run_ids
                            ),
                            "completed_agent_run_ids": list(claim.agent_run_ids),
                            "active_agent_run_ids": [
                                run["agent_run_id"]
                                for run in active_runs
                                if isinstance(run.get("agent_run_id"), str)
                            ],
                        }
                        await self._registry.acknowledge_handoffs(claim.claim_id)
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
                        if not active_runs:
                            raise RuntimeError(
                                "PAR wait_for_subagents outcome had no active "
                                "subagent runs in the claimed snapshot"
                            )
                        metadata["last_parent_control_outcome"] = {
                            "action": control.action,
                            "decision_id": control.decision_id,
                            "completed_agent_run_ids": list(claim.agent_run_ids),
                            "active_agent_run_ids": [
                                run["agent_run_id"]
                                for run in active_runs
                                if isinstance(run.get("agent_run_id"), str)
                            ],
                        }
                        await self._registry.acknowledge_handoffs(claim.claim_id)
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

                if _is_cancelled_result(result):
                    _record_claim_release_after_parent_exit(
                        task_id=task_id,
                        claim=claim,
                        cause="cancellation",
                    )
                    await self._registry.release_handoffs(claim.claim_id)
                else:
                    await self._registry.acknowledge_handoffs(claim.claim_id)
                    safe_inc("post_action_reasoning_parent_finalization_count")

                return ParentHandoffOutcome(
                    result=result,
                    claim_id=claim.claim_id,
                    agent_run_ids=claim.agent_run_ids,
                    child_completions=child_completions,
                )

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
    ) -> tuple[dict[str, Any], ...]:
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
    ) -> str:
        started_at = perf_counter()
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
        active_runs: tuple[dict[str, Any], ...],
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


async def _guard_for(
    *,
    tenant_id: int,
    task_id: int,
) -> asyncio.Lock:
    key = (tenant_id, task_id)
    current_loop = asyncio.get_running_loop()
    async with _GUARDS_LOCK:
        guard = _GUARDS.get(key)
        bound_loop = getattr(guard, "_loop", None) if guard is not None else None
        if (
            guard is not None
            and bound_loop is not None
            and bound_loop is not current_loop
            and not guard.locked()
        ):
            guard = None
        if guard is None:
            guard = asyncio.Lock()
            _GUARDS[key] = guard
        return guard


def _is_cancelled_result(result: LangGraphChatResult) -> bool:
    return result.metadata.get("status") == "cancelled"


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


def _extract_parent_control_outcome(
    result: LangGraphChatResult,
    *,
    parent_turn_id: str,
    claim: ClaimedHandoffBatch,
) -> _ParentControlOutcome:
    """Return a backend-owned PAR control outcome from result metadata."""
    source = _control_source(result.metadata)
    if source is None:
        return _ParentControlOutcome(action="")

    action = _control_action(source)
    if action not in {"delegate_subagent", "wait_for_subagents"}:
        return _ParentControlOutcome(action="")

    decision_id = _control_decision_id(
        source,
        action=action,
        parent_turn_id=parent_turn_id,
        claim=claim,
    )
    if action == "wait_for_subagents":
        return _ParentControlOutcome(action=action, decision_id=decision_id)

    normalized = normalize_agent_handoff_entries(
        source.get("agent_handoff"),
        max_handoffs=1,
        reject_invalid=True,
    )
    if not normalized:
        safe_inc("post_action_reasoning_followup_delegation_rejected")
        raise RuntimeError("PAR delegate_subagent outcome missing agent_handoff")
    return _ParentControlOutcome(
        action=action,
        agent_handoff=normalized[0],
        decision_id=decision_id,
    )


def _control_source(metadata: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for key in ("router_outcome", "candidate_decision", "parent_control_outcome"):
        value = metadata.get(key)
        if isinstance(value, Mapping):
            return value
    return metadata


def _control_action(source: Mapping[str, Any]) -> str:
    for key in ("action", "next_action", "last_post_tool_action"):
        value = source.get(key)
        if isinstance(value, str):
            normalized = value.strip().lower().replace(" ", "_")
            if normalized:
                return normalized
    return ""


def _control_decision_id(
    source: Mapping[str, Any],
    *,
    action: str,
    parent_turn_id: str,
    claim: ClaimedHandoffBatch,
) -> str:
    for key in ("decision_id", "candidate_id", "id"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    identity = {
        "action": action,
        "parent_turn_id": parent_turn_id,
        "claimed_agent_run_ids": list(claim.agent_run_ids),
        "agent_handoff": source.get("agent_handoff"),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:32]
    return f"par-decision-{digest}"


__all__ = [
    "FollowupDelegationDispatcher",
    "ParentFollowupDelegation",
    "ParentContinuationRunner",
    "ParentHandoffCoordinator",
    "ParentHandoffOutcome",
    "ParentProgressPublisher",
]
