"""Backend-facing contract exports for process-local subagent runs.

The backend service layer imports these shared data shapes when registering,
streaming, and projecting subagent runs. Definitions live in
`agent.subagents.contracts` so the agent runtime and backend control plane use
one validated contract set.
"""

from __future__ import annotations

from agent.subagents.contracts import (
    AgentCapability,
    AgentAssignment,
    AgentCredentialReference,
    AgentId,
    AgentKind,
    AgentResult,
    AgentResultProjection,
    AgentRunLifecycleProjection,
    AgentRunOutcome,
    AgentRunStatus,
    AgentRuntimeIdentity,
    JsonValue,
    agent_display_name,
    agent_icon_key,
)

__all__ = [
    "AgentCapability",
    "AgentAssignment",
    "AgentCredentialReference",
    "AgentId",
    "AgentKind",
    "AgentResult",
    "AgentResultProjection",
    "AgentRunLifecycleProjection",
    "AgentRunOutcome",
    "AgentRunStatus",
    "AgentRuntimeIdentity",
    "JsonValue",
    "agent_display_name",
    "agent_icon_key",
]
