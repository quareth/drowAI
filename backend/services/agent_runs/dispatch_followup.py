"""Replay-stable PAR follow-up dispatch coordination.

This module owns parent-action-reasoning follow-up validation, stable replay
identity lookup, capacity-aware plan construction, and batch launch outcome
projection. It does not own initial dispatch admission, child settlement,
parent-handoff coordination, registry mutation mechanics, or presentation
formatting.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from agent.subagents.registry import SubagentRegistry
from core.skills.registry import SkillRegistry, get_skill_registry
from backend.services.langgraph_chat.contracts import LangGraphRuntimeConfig

from .dispatch_batch import DispatchBatchExecutor
from .dispatch_contracts import DispatchBatchLaunchFailure
from .dispatch_plan import (
    build_dispatch_plan,
    routing_metadata_from_decision,
    runtime_config_with_subagent_routing,
    stable_par_assignment_identity,
)
from .ownership_policy import normalize_agent_handoff_entries, resolve_subagent_handoff
from .parent_handoff_coordinator import ParentFollowupDelegation
from .registry import ProcessLocalAgentRunRegistry


ActiveCountReader = Callable[
    [LangGraphRuntimeConfig],
    Awaitable[Mapping[str, int]],
]


class FollowupDispatcher:
    """Resolve and launch one PAR-authored follow-up through batch dispatch."""

    def __init__(
        self,
        *,
        registry: ProcessLocalAgentRunRegistry,
        subagent_registry: SubagentRegistry,
        batch_executor: DispatchBatchExecutor,
        active_count_reader: ActiveCountReader,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        self._registry = registry
        self._subagent_registry = subagent_registry
        self._batch_executor = batch_executor
        self._active_count_reader = active_count_reader
        self._skill_registry = skill_registry or get_skill_registry()

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
            requested_skill_ids=tuple(normalized[0]["skill_ids"]),
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

        active_counts = await self._active_count_reader(runtime_config)
        decision = resolve_subagent_handoff(
            runtime_config.metadata,
            registry=self._subagent_registry,
            skill_registry=self._skill_registry,
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
        launch_result = await self._batch_executor.launch_batch(
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
        launched = {
            child.invocation.assignment.agent_run_id
            for child in launch_result.children
        }
        return ParentFollowupDelegation(
            agent_run_ids=tuple(item.assignment.agent_run_id for item in plan),
            launched_agent_run_ids=tuple(
                item.assignment.agent_run_id
                for item in plan
                if item.assignment.agent_run_id in launched
            ),
        )


__all__ = [
    "ActiveCountReader",
    "FollowupDispatcher",
]
