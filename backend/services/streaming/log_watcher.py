"""Reasoning log tailing and full-file parsing helpers.

This module owns provider-mediated interaction with per-task ``log.txt`` files.
It provides both live tail streaming for fallback SSE delivery and a shared
full-file parser for reasoning history/replay consumers.
"""

import asyncio
import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Dict, List, Optional

from collections import defaultdict

from backend.config import REASONING_DB_PERSIST
from backend.database import SessionLocal
from backend.services.runtime_provider.contracts import RuntimeActorType
from backend.services.runtime_provider.operations import RuntimeOperationService
from .reasoning_store import AgentReasoningStore

logger = logging.getLogger("backend.services.agent_log_watcher")


def _run_sync(coro):
    """Run a provider coroutine from sync fallback readers."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except Exception as exc:  # pragma: no cover - defensive bridge
            result["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _extract_file_content(metadata: Dict[str, Any]) -> str:
    delegate = metadata.get("delegate_result")
    if isinstance(delegate, dict):
        content = delegate.get("content")
        if isinstance(content, str):
            return content
    content = metadata.get("content")
    if isinstance(content, str):
        return content
    return ""


async def _read_reasoning_log_content(task_id: int) -> str:
    db = SessionLocal()
    try:
        service = RuntimeOperationService(db)
        context = service.context_for_internal_task(
            task_id=int(task_id),
            actor_type=RuntimeActorType.SYSTEM,
            actor_id="reasoning-log",
        )
        result = await service.run_for_context(
            context=context,
            operation="read_runtime_artifact_file",
            call=lambda provider, request: provider.read_runtime_artifact_file(request),
            payload={"path": "log.txt", "encoding": "utf-8"},
        )
        if not result.ok:
            return ""
        return _extract_file_content(dict(result.metadata or {}))
    except Exception:
        logger.debug("Provider reasoning log read failed for task %s", task_id, exc_info=True)
        return ""
    finally:
        db.close()


def read_reasoning_log_entries(task_id: int) -> List[Dict[str, Any]]:
    """Read full log history and return ``react_step`` entries with line sequences."""
    content = _run_sync(_read_reasoning_log_content(task_id))
    items: List[Dict[str, Any]] = []
    for idx, line in enumerate(content.splitlines(), start=1):
        try:
            data = json.loads(line)
        except Exception:
            continue
        if isinstance(data, dict) and data.get("type") == "react_step":
            items.append({**data, "sequence": idx})
    return items


@dataclass
class LogLine:
    """Representation of a single log entry."""

    json_data: Optional[dict] = None
    text: Optional[str] = None


class AgentLogWatcher:
    """Watch agent log files and stream new lines to subscribers."""

    def __init__(self, poll_interval: float = 1.0) -> None:
        self.poll_interval = poll_interval
        self._queues: Dict[int, List[asyncio.Queue[LogLine]]] = defaultdict(list)
        self._tasks: Dict[int, asyncio.Task] = {}
        self._positions: Dict[int, int] = {}

    def _parse_line(self, line: str) -> LogLine:
        """Parse a line into structured or plain text representation."""
        try:
            data = json.loads(line)
            if isinstance(data, dict):
                return LogLine(json_data=data)
        except json.JSONDecodeError:
            pass
        return LogLine(text=line.rstrip("\n"))

    async def _watch_file(self, task_id: int) -> None:
        position = self._positions.get(task_id, 0)
        while True:
            try:
                content = await _read_reasoning_log_content(task_id)
                if len(content) < position:
                    position = 0
                if len(content) > position:
                    lines = content[position:].splitlines(keepends=True)
                    position = len(content)
                    self._positions[task_id] = position
                    for line in lines:
                        parsed = self._parse_line(line)
                        for q in self._queues[task_id]:
                            await q.put(parsed)
                        # Phase 2 dual-write: mirror to DB if enabled and is reasoning step
                        if REASONING_DB_PERSIST and parsed.json_data and parsed.json_data.get("type") == "react_step":
                            db = None
                            try:
                                db = SessionLocal()
                                AgentReasoningStore(db).append_step(task_id, parsed.json_data)
                            except Exception:
                                logger.debug("AgentLogWatcher DB mirror failed", exc_info=True)
                            finally:
                                if db is not None:
                                    try:
                                        db.close()
                                    except Exception:
                                        pass
            except Exception:
                logger.debug("AgentLogWatcher provider poll failed", exc_info=True)
            await asyncio.sleep(self.poll_interval)

    async def stream_lines(self, task_id: int) -> AsyncGenerator[LogLine, None]:
        queue: asyncio.Queue[LogLine] = asyncio.Queue()
        self._queues[task_id].append(queue)
        if task_id not in self._tasks:
            # Start at end of file for memory efficiency
            self._positions[task_id] = len(await _read_reasoning_log_content(task_id))
            self._tasks[task_id] = asyncio.create_task(self._watch_file(task_id))
        try:
            while True:
                line = await queue.get()
                yield line
        finally:
            self._queues[task_id].remove(queue)
            if not self._queues[task_id]:
                task = self._tasks.pop(task_id, None)
                if task:
                    task.cancel()
                self._positions.pop(task_id, None)
