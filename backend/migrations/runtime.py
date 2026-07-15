"""Runtime helper for applying Alembic migrations before backend startup.

This module keeps Python launchers on the same schema path as Docker: run
Alembic to head, then start the FastAPI application against the migrated DB.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path


def upgrade_database_to_head(
    *,
    env: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
    python_executable: str | None = None,
) -> None:
    """Run `alembic upgrade head` using the backend migration directory."""

    root = repo_root or Path(__file__).resolve().parents[2]
    backend_dir = root / "backend"
    process_env = dict(os.environ)
    if env is not None:
        process_env.update({str(key): str(value) for key, value in env.items()})
    subprocess.run(
        [
            python_executable or sys.executable,
            "-m",
            "alembic",
            "upgrade",
            "head",
        ],
        cwd=str(backend_dir),
        env=process_env,
        check=True,
    )
