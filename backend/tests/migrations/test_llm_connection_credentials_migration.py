"""Migration coverage for connection-owned LLM credential storage."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


_REPO_ROOT = Path(__file__).resolve().parents[3]
_BACKEND_DIR = _REPO_ROOT / "backend"


def _run_alembic(database_url: str, *args: str) -> None:
    """Run one Alembic command against an isolated database."""

    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(_BACKEND_DIR),
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
    )


def test_connection_credential_schema_is_one_to_one_and_cascades(
    tmp_path: Path,
) -> None:
    """Fresh schema binds one credential to one connection lifecycle."""

    database_url = f"sqlite:///{tmp_path / 'connection-credentials.sqlite'}"
    _run_alembic(database_url, "upgrade", "0010_gpt_oss_agent_dialect")
    engine = create_engine(database_url)
    connection_id = "00000000-0000-0000-0000-000000001101"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, password) "
                "VALUES (1101, 'connection-credential-user', 'hashed')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO llm_inference_connections "
                "(id, user_id, display_name, connection_preset_id, "
                "runtime_family_id, state, revision) VALUES "
                "(:connection_id, 1101, 'Managed endpoint', "
                "'custom_openai_compatible_chat', 'openai_compatible_chat', "
                "'draft', 1)"
            ),
            {"connection_id": connection_id},
        )

    _run_alembic(database_url, "upgrade", "head")
    inspector = inspect(engine)
    assert inspector.has_table("llm_connection_credentials")
    assert inspector.get_pk_constraint("llm_connection_credentials")[
        "constrained_columns"
    ] == ["connection_id"]
    assert inspector.get_foreign_keys("llm_connection_credentials") == [
        {
            "name": None,
            "constrained_columns": ["connection_id"],
            "referred_schema": None,
            "referred_table": "llm_inference_connections",
            "referred_columns": ["id"],
            "options": {"ondelete": "CASCADE"},
        }
    ]

    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        connection.execute(
            text(
                "INSERT INTO llm_connection_credentials "
                "(connection_id, encrypted_api_key, enabled) "
                "VALUES (:connection_id, 'encrypted-placeholder', 1)"
            ),
            {"connection_id": connection_id},
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
            connection.execute(
                text(
                    "INSERT INTO llm_connection_credentials "
                    "(connection_id, encrypted_api_key, enabled) "
                    "VALUES (:connection_id, 'duplicate-placeholder', 1)"
                ),
                {"connection_id": connection_id},
            )

    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        connection.execute(
            text("DELETE FROM llm_inference_connections WHERE id = :connection_id"),
            {"connection_id": connection_id},
        )
        assert connection.execute(
            text("SELECT COUNT(*) FROM llm_connection_credentials")
        ).scalar_one() == 0

    _run_alembic(database_url, "downgrade", "0010_gpt_oss_agent_dialect")
    assert not inspect(engine).has_table("llm_connection_credentials")
    engine.dispose()
