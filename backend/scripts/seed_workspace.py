"""
Developer utility: Seed a task workspace with minimal files for local testing.

Usage (examples):
  python -m backend.scripts.seed_workspace --task-id 1001 --name "Test Task"

This script does not touch the database; it only ensures the workspace
directory structure, writes a basic config.json and a scope.md, and appends
some example reasoning log entries to log.txt for SSE tests.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from backend.services.workspace.manager import WorkspaceManager
from backend.config.workspace_config import WorkspaceConfig


def seed_workspace(task_id: int, name: str, scope: str | None = None) -> Path:
    manager = WorkspaceManager()
    manager.create_workspace(task_id)

    manager.save_config_file(
        task_id,
        {
            "task_name": name,
            "description": "Seeded task for local testing",
            "scope": scope or "# Default Scope\n- example.com",
            "timeout_seconds": 3600,
            "max_retries": 0,
            "priority": 1,
        },
    )

    manager.save_scope_file(task_id, scope or "# Default Scope\n- example.com")

    # Append a few example reasoning lines to log.txt
    log_file = WorkspaceConfig.get_task_workspace_path(task_id) / "log.txt"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        {
            "type": "react_step",
            "timestamp": datetime.utcnow().isoformat(),
            "content": "Initializing environment",
            "metadata": {"phase": "init"},
        },
        {
            "type": "react_step",
            "timestamp": datetime.utcnow().isoformat(),
            "content": "Loading scope and planning",
            "metadata": {"targets": 1},
        },
    ]
    with open(log_file, "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    return log_file


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed task workspace")
    parser.add_argument("--task-id", type=int, required=True)
    parser.add_argument("--name", type=str, default="Seeded Task")
    parser.add_argument("--scope", type=str, default=None)
    args = parser.parse_args()

    path = seed_workspace(args.task_id, args.name, args.scope)
    print(f"Seeded workspace for task {args.task_id}. Log file: {path}")


if __name__ == "__main__":
    main()


