"""Typed contracts for subagent dispatch coordination.

This module owns dispatch result, stop, launch-boundary, and ephemeral
batch/settlement facts only. It does not schedule runs, mutate the registry,
publish lifecycle events, launch child work, or process parent handoffs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from backend.services.langgraph_chat.contracts import LangGraphRuntimeConfig

from .completion import AgentRunCompletion
from .contracts import AgentAssignment
from .dispatch_plan import PlannedAgentInvocation
from .parent_handoff_coordinator import ParentHandoffOutcome


ReadyHandoffProcessor = Callable[
    [tuple[AgentRunCompletion, ...], bool], Awaitable[ParentHandoffOutcome | None]
]
DispatchStopStatus = Literal["failed", "cancelled", "waiting_for_approval"]


class AgentRunLaunchService(Protocol):
    """Minimal launch boundary required by the dispatch service."""

    async def launch(
        self,
        *,
        assignment: AgentAssignment,
        runtime_config: LangGraphRuntimeConfig,
        graph_thread_id: str,
        parent_run_id: str | None = None,
    ) -> Awaitable[AgentRunCompletion]:
        """Launch one registered assignment and return its terminal awaitable."""


@dataclass(frozen=True, slots=True)
class AgentRunDispatchStop:
    """A dispatch-level terminal state requiring adapter presentation."""

    invocation: PlannedAgentInvocation
    status: DispatchStopStatus
    usage: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentRunDispatchResult:
    """Completed dispatch plan with any parent outcome or terminal stop."""

    child_completions: tuple[AgentRunCompletion, ...] = ()
    parent_handoff_outcome: ParentHandoffOutcome | None = None
    stop: AgentRunDispatchStop | None = None


@dataclass(frozen=True, slots=True)
class DispatchBatchChild:
    """One launched invocation and its launcher-owned terminal awaitable."""

    invocation: PlannedAgentInvocation
    terminal: Awaitable[Any]


@dataclass(frozen=True, slots=True)
class DispatchBatchLaunch:
    """Successfully launched children in admitted batch order."""

    children: tuple[DispatchBatchChild, ...]


@dataclass(frozen=True, slots=True)
class DispatchBatchLaunchFailure:
    """Launch failure plus sibling completions or a dispatch stop."""

    child_completions: tuple[AgentRunCompletion, ...] = ()
    stop: AgentRunDispatchStop | None = None


@dataclass(frozen=True, slots=True)
class DispatchChildSettlement:
    """Typed translation of one gathered child in batch order."""

    invocation: PlannedAgentInvocation
    completion: AgentRunCompletion | None = None
    stop: AgentRunDispatchStop | None = None
    paused: bool = False

    def __post_init__(self) -> None:
        meaningful_fields = (
            int(self.completion is not None)
            + int(self.stop is not None)
            + int(self.paused)
        )
        if meaningful_fields != 1:
            raise ValueError(
                "DispatchChildSettlement requires exactly one of completion, "
                "stop, or paused"
            )


__all__ = [
    "AgentRunDispatchResult",
    "AgentRunDispatchStop",
    "AgentRunLaunchService",
    "DispatchBatchChild",
    "DispatchBatchLaunch",
    "DispatchBatchLaunchFailure",
    "DispatchChildSettlement",
    "DispatchStopStatus",
    "ReadyHandoffProcessor",
]
