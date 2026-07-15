"""Regression tests for streaming-related module imports.

These checks run imports in a fresh interpreter so package-level cycles in the
streaming stack fail deterministically during test execution.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.parametrize(
    "module_name",
    [
        "backend.services.streaming.in_memory_hub",
        "backend.services.langgraph_chat.facade",
    ],
)
def test_streaming_related_modules_import_in_fresh_interpreter(
    module_name: str,
    tmp_path: Path,
) -> None:
    """Fresh interpreter imports should not trip package initialization cycles."""
    env = os.environ.copy()
    db_path = tmp_path / "import-regression.sqlite3"
    env["DATABASE_URL"] = f"sqlite:///{db_path}"

    result = subprocess.run(
        [sys.executable, "-c", f"import {module_name}"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        f"fresh import failed for {module_name}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
