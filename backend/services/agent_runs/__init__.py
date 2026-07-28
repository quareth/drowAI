"""Process-local asynchronous services for subagent runs."""

from .execution_config import ChildExecutionConfigError, build_child_execution_config
from .event_projection import build_agent_run_lifecycle_event
from .launcher import AgentRunLauncher, AgentRunWorker, LifecyclePublisher
from .result_projection import (
    COMPLETED_AGENT_RESULTS_KEY,
    AgentRunResultProjector,
    CompletedAgentResultHandoff,
    attach_completed_agent_results_to_context,
)
from .registry import (
    ActiveAgentRunExistsError,
    AgentRunNotFoundError,
    LocalAgentRun,
    ProcessLocalAgentRunRegistry,
)

__all__ = [
    "ActiveAgentRunExistsError",
    "AgentRunLauncher",
    "AgentRunNotFoundError",
    "AgentRunResultProjector",
    "COMPLETED_AGENT_RESULTS_KEY",
    "ChildExecutionConfigError",
    "CompletedAgentResultHandoff",
    "AgentRunWorker",
    "LifecyclePublisher",
    "LocalAgentRun",
    "ProcessLocalAgentRunRegistry",
    "attach_completed_agent_results_to_context",
    "build_agent_run_lifecycle_event",
    "build_child_execution_config",
]
