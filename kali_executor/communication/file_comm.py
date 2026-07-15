"""File-based command/result transport used inside Kali task runtimes.

Provides lock-protected JSONL queues for executor commands, results, and
cancellation messages under the task workspace.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import ValidationError

from runtime_shared.file_comm_contracts import (
    CancellationMessage,
    CommandMessage,
    FileCommWorkspacePaths,
    ResultMessage,
)

# Platform-specific imports for file locking
try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    # Windows doesn't have fcntl, we'll use file-based locking
    HAS_FCNTL = False

MAX_RETRIES = 3


class _FileLock:
    """Cross-platform file locking context manager."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: Optional[Any] = None
        self.lock_file = path.with_suffix(path.suffix + '.lock')

    def __enter__(self) -> Any:
        if HAS_FCNTL:
            # Unix-style fcntl locking
            self.fd = open(self.path, "a+")
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            return self.fd
        else:
            # Windows-style file-based locking
            retry_count = 0
            while retry_count < MAX_RETRIES:
                try:
                    # Try to create lock file exclusively
                    self.fd = open(self.lock_file, "x")
                    # Open the actual file
                    actual_fd = open(self.path, "a+")
                    return actual_fd
                except FileExistsError:
                    # Lock file exists, wait and retry
                    time.sleep(0.1)
                    retry_count += 1
            raise OSError(f"Could not acquire lock for {self.path}")

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fd:
            if HAS_FCNTL:
                # Unix-style fcntl unlocking
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                self.fd.close()
            else:
                # Windows-style: close and remove lock file
                try:
                    self.fd.close()
                    if self.lock_file.exists():
                        self.lock_file.unlink()
                except Exception:
                    pass


def _cleanup_file(file_path: Path, lock_path: Path, keep_ids: set[str]) -> None:
    if not file_path.exists():
        return
    with _FileLock(lock_path):
        with open(file_path, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        lines = [entry for entry in lines if entry.get("id") not in keep_ids]
        with open(file_path, "w", encoding="utf-8") as f:
            for entry in lines:
                f.write(json.dumps(entry) + "\n")


def _cleanup_cancellations(file_path: Path, lock_path: Path, command_id: str) -> None:
    if not file_path.exists():
        return
    with _FileLock(lock_path):
        with open(file_path, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        lines = [entry for entry in lines if str(entry.get("command_id") or "") != command_id]
        with open(file_path, "w", encoding="utf-8") as f:
            for entry in lines:
                f.write(json.dumps(entry) + "\n")


class FileCommExecutor:
    """Executor-side file communication interface."""

    def __init__(self, workspace_path: str = "/workspace") -> None:
        paths = FileCommWorkspacePaths.from_workspace(workspace_path)
        self.commands_file = paths.commands_file
        self.results_file = paths.results_file
        self.cancellations_file = paths.cancellations_file
        self.commands_lock = paths.commands_lock
        self.results_lock = paths.results_lock
        self.cancellations_lock = paths.cancellations_lock

        self._processed_command_ids: set[str] = set()
        self._logger = logging.getLogger(__name__)

    def _append_line(self, file_path: Path, lock_path: Path, line: str) -> None:
        os.makedirs(file_path.parent, exist_ok=True)
        for _ in range(MAX_RETRIES):
            try:
                with _FileLock(lock_path):
                    with open(file_path, "a", encoding="utf-8") as f:
                        f.write(line)
                return
            except OSError:
                time.sleep(0.1)
        raise OSError(f"Failed to append to {file_path}")

    def _read_all(self, file_path: Path, lock_path: Path) -> List[Dict[str, Any]]:
        if not file_path.exists():
            return []
        with _FileLock(lock_path):
            with open(file_path, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
        data = []
        for line in lines:
            try:
                data.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return data

    async def get_pending_commands(self) -> List[Dict[str, Any]]:
        commands = await asyncio.to_thread(self._read_all, self.commands_file, self.commands_lock)
        raw_pending = [c for c in commands if c.get("id") not in self._processed_command_ids]
        pending: List[Dict[str, Any]] = []
        for cmd in raw_pending:
            try:
                CommandMessage.model_validate(cmd)
                pending.append(cmd)
            except ValidationError as e:
                self._logger.warning("Skipping malformed command %s: %s", cmd.get("id", "<unknown>"), e)
                continue
        self._processed_command_ids.update(c.get("id") for c in pending)
        await asyncio.to_thread(_cleanup_file, self.commands_file, self.commands_lock, self._processed_command_ids)
        return pending

    async def send_result(self, command_id: str, result: Dict[str, Any]) -> None:
        result["id"] = command_id
        result.setdefault("timestamp", datetime.utcnow().isoformat())
        try:
            ResultMessage.model_validate(result)
        except ValidationError as e:
            raise ValueError(f"Invalid result schema: {e}")

        line = json.dumps(result) + "\n"
        await asyncio.to_thread(self._append_line, self.results_file, self.results_lock, line)

    async def is_cancel_requested(self, command_id: str) -> bool:
        """Return whether a cancellation request exists for one command id."""
        return await asyncio.to_thread(self.is_cancel_requested_sync, command_id)

    def is_cancel_requested_sync(self, command_id: str) -> bool:
        """Return whether a cancellation request exists for one command id."""
        resolved_command_id = str(command_id or "").strip()
        if not resolved_command_id:
            return False
        rows = self._read_all(self.cancellations_file, self.cancellations_lock)
        for row in rows:
            try:
                message = CancellationMessage.model_validate(row)
            except ValidationError:
                continue
            if message.command_id == resolved_command_id:
                return True
        return False

    async def acknowledge_cancellation(self, command_id: str) -> None:
        """Remove consumed cancellation rows for one command id."""
        resolved_command_id = str(command_id or "").strip()
        if not resolved_command_id:
            return
        await asyncio.to_thread(
            _cleanup_cancellations,
            self.cancellations_file,
            self.cancellations_lock,
            resolved_command_id,
        )
