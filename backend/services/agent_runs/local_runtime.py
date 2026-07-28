"""Shared process-local subagent runtime objects for the backend process.

This module owns the single in-memory registry used by process-local subagent
runs when no test-specific registry is injected. The objects are intentionally
process-local and are not durable or distributed coordination primitives.
"""

from __future__ import annotations

from backend.services.langgraph_chat.checkpoint.checkpointer_service import (
    get_shared_checkpointer_service,
)
from backend.services.langgraph_chat.execution.graph_executor import LangGraphExecutor
from backend.services.langgraph_chat.streaming.adapter import LangGraphStreamingAdapter

from .launcher import AgentRunLauncher
from .registry import ProcessLocalAgentRunRegistry
from .worker import ProcessLocalAgentRunWorker

_PROCESS_LOCAL_AGENT_RUN_REGISTRY = ProcessLocalAgentRunRegistry()
_PROCESS_LOCAL_SUBAGENT_STREAMING_ADAPTER = LangGraphStreamingAdapter()
_PROCESS_LOCAL_AGENT_RUN_WORKER = ProcessLocalAgentRunWorker(
    registry=_PROCESS_LOCAL_AGENT_RUN_REGISTRY,
    checkpointer_service=get_shared_checkpointer_service(),
    executor=LangGraphExecutor(
        streaming_adapter=_PROCESS_LOCAL_SUBAGENT_STREAMING_ADAPTER
    ),
)
_PROCESS_LOCAL_AGENT_RUN_LAUNCHER = AgentRunLauncher(
    registry=_PROCESS_LOCAL_AGENT_RUN_REGISTRY,
    worker=_PROCESS_LOCAL_AGENT_RUN_WORKER,
)


def get_process_local_agent_run_registry() -> ProcessLocalAgentRunRegistry:
    """Return this process' shared subagent registry."""
    return _PROCESS_LOCAL_AGENT_RUN_REGISTRY


def get_process_local_agent_run_launcher() -> AgentRunLauncher:
    """Return this process' shared launcher facade for scoped cancellation."""
    return _PROCESS_LOCAL_AGENT_RUN_LAUNCHER


__all__ = [
    "get_process_local_agent_run_launcher",
    "get_process_local_agent_run_registry",
]
