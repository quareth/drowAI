"""Migration tests for remote conversation lifecycle origin snapshots."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect


_REPO_ROOT = Path(__file__).resolve().parents[3]
_BACKEND_DIR = _REPO_ROOT / "backend"


def _run_alembic(database_url: str, *args: str) -> None:
    """Run one isolated Alembic command against a temporary database."""

    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(_BACKEND_DIR),
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
    )


def test_upgrade_and_downgrade_remote_conversation_origin_columns(
    tmp_path: Path,
) -> None:
    """The origin revision adds nullable snapshots and reverses cleanly."""

    database_url = f"sqlite:///{tmp_path / 'conversation-origin.sqlite'}"
    _run_alembic(database_url, "upgrade", "0007_llm_deployment_backfill")
    engine = create_engine(database_url)

    before = inspect(engine)
    before_columns = {
        column["name"] for column in before.get_columns("llm_conversations")
    }
    assert "origin_revision" not in before_columns
    assert "origin_deployment_revision" not in before_columns
    assert "remote_resource_id" not in before_columns

    _run_alembic(database_url, "upgrade", "head")
    after = inspect(engine)
    columns = {
        column["name"]: column
        for column in after.get_columns("llm_conversations")
    }
    assert columns["origin_revision"]["nullable"] is True
    assert columns["origin_deployment_revision"]["nullable"] is True
    assert columns["remote_resource_id"]["nullable"] is True
    assert "ix_llm_conversations_remote_resource_id" in {
        index["name"] for index in after.get_indexes("llm_conversations")
    }

    _run_alembic(database_url, "downgrade", "0007_llm_deployment_backfill")
    downgraded_columns = {
        column["name"]
        for column in inspect(engine).get_columns("llm_conversations")
    }
    assert "origin_revision" not in downgraded_columns
    assert "origin_deployment_revision" not in downgraded_columns
    assert "remote_resource_id" not in downgraded_columns
