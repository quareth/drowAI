"""Public package exports for Kali executor components."""

from __future__ import annotations

from typing import Any

__all__ = ["FileCommExecutor", "run_daemon", "process_commands_once"]


def __getattr__(name: str) -> Any:
    if name == "FileCommExecutor":
        from .communication.file_comm import FileCommExecutor

        return FileCommExecutor
    if name in {"run_daemon", "process_commands_once"}:
        from .executor_daemon import process_commands_once, run_daemon

        return {"run_daemon": run_daemon, "process_commands_once": process_commands_once}[name]
    raise AttributeError(name)
