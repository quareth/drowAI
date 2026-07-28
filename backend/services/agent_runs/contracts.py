"""Backend-facing contract exports for process-local Scout agent runs.

The backend service layer imports these shared data shapes when registering,
streaming, and projecting subagent runs. Definitions live in
`agent.subagents.contracts` so the agent runtime and backend control plane use
one validated contract set during the migration-free pilot.
"""

from __future__ import annotations

from agent.subagents.contracts import (
    AGENT_DISPLAY_NAMES,
    AgentAssignment,
    AgentCredentialReference,
    AgentKind,
    AgentResult,
    AgentResultProjection,
    AgentRunLifecycleProjection,
    AgentRunOutcome,
    AgentRunStatus,
    AgentRuntimeIdentity,
    JsonValue,
    ReconCapability,
    agent_display_name,
)

__all__ = [
    "AGENT_DISPLAY_NAMES",
    "AgentAssignment",
    "AgentCredentialReference",
    "AgentKind",
    "AgentResult",
    "AgentResultProjection",
    "AgentRunLifecycleProjection",
    "AgentRunOutcome",
    "AgentRunStatus",
    "AgentRuntimeIdentity",
    "JsonValue",
    "ReconCapability",
    "agent_display_name",
]
