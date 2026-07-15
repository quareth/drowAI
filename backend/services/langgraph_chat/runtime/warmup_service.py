"""
Runtime warmup orchestration for LangGraph chat dependencies.

This service centralizes prewarm calls for checkpointer, tool-catalog metadata,
and optional PTY session setup. It provides per-task idempotency and
step-isolated failure reporting so callers can warm runtime dependencies without
failing the request path.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
from typing import Any, Dict, Optional

from agent.tools.tool_registry import warm_catalog_metadata_snapshot
from backend.services.langgraph_chat.checkpoint.checkpointer_service import (
    CheckpointerService,
    get_shared_checkpointer_service,
)
from backend.services.langgraph_chat.hitl_constants import (
    GRAPH_NAME_DEEP_REASONING,
    GRAPH_NAME_SIMPLE_TOOL,
)
from backend.services.langgraph_chat.runtime.tool_catalog import build_tool_catalog

logger = logging.getLogger(__name__)

_STEP_CHECKPOINTER = "checkpointer"
_STEP_TOOL_CATALOG = "tool_catalog"
_STEP_PTY_SESSION = "pty_session"
_WARMUP_STEPS = (_STEP_CHECKPOINTER, _STEP_TOOL_CATALOG, _STEP_PTY_SESSION)


def _empty_status() -> Dict[str, Dict[str, Any]]:
    return {
        step: {
            "ready": False,
            "error": None,
            "skipped": False,
        }
        for step in _WARMUP_STEPS
    }


class RuntimeWarmupService:
    """Warm cold runtime dependencies with per-task idempotency."""

    def __init__(self, checkpointer_service: Optional[CheckpointerService] = None) -> None:
        self._checkpointer_service = checkpointer_service or get_shared_checkpointer_service()
        self._status_by_task: Dict[int, Dict[str, Dict[str, Any]]] = {}
        self._task_locks: Dict[int, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    async def warm_task_runtime(
        self,
        task_id: int,
        graph_name: Optional[str] = None,
        workspace_path: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Warm runtime dependencies for a task and return per-step status.

        Warmup is idempotent per task: successful steps are not repeated on
        subsequent calls for the same task.
        """
        task_lock = await self._get_task_lock(task_id)
        async with task_lock:
            status = self._status_by_task.setdefault(task_id, _empty_status())

            await self._run_step(
                task_id=task_id,
                status=status,
                step_name=_STEP_CHECKPOINTER,
                runner=lambda: self._warm_checkpointer(task_id),
            )
            await self._run_step(
                task_id=task_id,
                status=status,
                step_name=_STEP_TOOL_CATALOG,
                runner=lambda: self._warm_tool_catalog_metadata(task_id, graph_name),
            )

            if self._is_pty_warmup_required(graph_name):
                await self._run_step(
                    task_id=task_id,
                    status=status,
                    step_name=_STEP_PTY_SESSION,
                    runner=lambda: self._warm_pty_session(task_id, workspace_path),
                )
            else:
                pty_step = status[_STEP_PTY_SESSION]
                # Do not downgrade an already-warmed PTY session when a caller
                # invokes generic warmup without a PTY-requiring graph context.
                if pty_step.get("ready"):
                    pty_step["error"] = None
                    pty_step["skipped"] = False
                else:
                    pty_step["ready"] = False
                    pty_step["error"] = None
                    pty_step["skipped"] = True

            return copy.deepcopy(status)

    def get_warmup_status(self, task_id: int) -> Dict[str, Dict[str, Any]]:
        """Return the latest warmup status snapshot for a task."""
        status = self._status_by_task.get(task_id)
        if status is None:
            return _empty_status()
        return copy.deepcopy(status)

    async def _get_task_lock(self, task_id: int) -> asyncio.Lock:
        async with self._lock:
            lock = self._task_locks.get(task_id)
            if lock is None:
                lock = asyncio.Lock()
                self._task_locks[task_id] = lock
            return lock

    async def _run_step(
        self,
        *,
        task_id: int,
        status: Dict[str, Dict[str, Any]],
        step_name: str,
        runner: Any,
    ) -> None:
        step_status = status[step_name]
        if step_status["ready"]:
            step_status["error"] = None
            step_status["skipped"] = False
            return

        try:
            await runner()
            step_status["ready"] = True
            step_status["error"] = None
            step_status["skipped"] = False
        except Exception as exc:
            logger.warning(
                "Runtime warmup step failed (task_id=%s step=%s): %s",
                task_id,
                step_name,
                exc,
                exc_info=True,
            )
            step_status["ready"] = False
            step_status["error"] = str(exc)
            step_status["skipped"] = False

    async def _warm_checkpointer(self, task_id: int) -> None:
        # Schema initialization belongs exclusively to application startup.
        # Request-time warmup verifies that a task checkpointer can be acquired.
        async with self._checkpointer_service.get_checkpointer(task_id):
            pass

    async def _warm_tool_catalog_metadata(self, task_id: int, graph_name: Optional[str]) -> None:
        warm_catalog_metadata_snapshot()
        metadata = {"task_id": task_id}
        if graph_name:
            metadata["graph_name"] = graph_name
        build_tool_catalog(capability=None, metadata=metadata, limit=1)

    async def _warm_pty_session(self, task_id: int, workspace_path: Optional[str]) -> None:
        from backend.services.terminal_session_manager import terminal_session_manager

        await terminal_session_manager.prepare_agent_session(
            task_id=task_id,
            workspace_path=workspace_path,
        )

    @staticmethod
    def _is_pty_warmup_required(graph_name: Optional[str]) -> bool:
        if os.getenv("ENABLE_PTY_EXECUTION", "false").strip().lower() != "true":
            return False
        if graph_name is None:
            return False
        normalized = graph_name.strip().lower()
        return normalized in {GRAPH_NAME_SIMPLE_TOOL, GRAPH_NAME_DEEP_REASONING}


_SHARED_RUNTIME_WARMUP_SERVICE: Optional[RuntimeWarmupService] = None


def get_shared_runtime_warmup_service() -> RuntimeWarmupService:
    """Return process-level shared RuntimeWarmupService."""
    global _SHARED_RUNTIME_WARMUP_SERVICE
    if _SHARED_RUNTIME_WARMUP_SERVICE is None:
        _SHARED_RUNTIME_WARMUP_SERVICE = RuntimeWarmupService()
    return _SHARED_RUNTIME_WARMUP_SERVICE


__all__ = ["RuntimeWarmupService", "get_shared_runtime_warmup_service"]
