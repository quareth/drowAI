"""Process-local asynchronous services for subagent runs."""

from .execution_config import ChildExecutionConfigError, build_child_execution_config
from .event_projection import build_agent_run_lifecycle_event
from .launcher import AgentRunLauncher, AgentRunWorker, LifecyclePublisher
from .parent_handoff_coordinator import ParentHandoffCoordinator, ParentHandoffOutcome
from .result_projection import (
    ACTIVE_AGENT_RUNS_KEY,
    COMPLETED_AGENT_RESULTS_KEY,
    AgentRunResultProjector,
    CompletedAgentResultHandoff,
    attach_active_agent_runs_to_context,
    attach_completed_agent_results_to_context,
)
from .registry import (
    ActiveAgentRunExistsError,
    AgentRunIdentityCollisionError,
    AgentRunNotFoundError,
    LocalAgentRun,
    ProcessLocalAgentRunRegistry,
)

__all__ = [
    "ActiveAgentRunExistsError",
    "AgentRunLauncher",
    "AgentRunIdentityCollisionError",
    "AgentRunNotFoundError",
    "AgentRunResultProjector",
    "ACTIVE_AGENT_RUNS_KEY",
    "COMPLETED_AGENT_RESULTS_KEY",
    "ChildExecutionConfigError",
    "CompletedAgentResultHandoff",
    "AgentRunWorker",
    "LifecyclePublisher",
    "LocalAgentRun",
    "ParentHandoffCoordinator",
    "ParentHandoffOutcome",
    "ProcessLocalAgentRunRegistry",
    "attach_active_agent_runs_to_context",
    "attach_completed_agent_results_to_context",
    "build_agent_run_lifecycle_event",
    "build_child_execution_config",
]
