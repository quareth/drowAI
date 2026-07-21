"""Migration tests for retiring legacy OpenAI settings mirrors."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text


_REPO_ROOT = Path(__file__).resolve().parents[3]
_BACKEND_DIR = _REPO_ROOT / "backend"


def _run_alembic(database_url: str, *args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(_BACKEND_DIR),
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
    )


def test_legacy_text_llm_settings_columns_and_credential_unique_are_retired(
    tmp_path: Path,
) -> None:
    """Head schema removes settings mirrors and permits duplicate provider rows."""

    database_url = f"sqlite:///{tmp_path / 'llm-legacy-retirement.sqlite'}"

    _run_alembic(database_url, "upgrade", "head")

    engine = create_engine(database_url)
    with engine.begin() as connection:
        inspector = inspect(connection)
        settings_columns = {
            column["name"] for column in inspector.get_columns("user_settings")
        }
        assert "openai_api_key" not in settings_columns
        assert "openai_model" not in settings_columns
        assert "enable_ai" not in settings_columns

        unique_names = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(
                "user_llm_provider_credentials"
            )
        }
        assert "uq_user_llm_provider_credentials_user_provider" not in unique_names

        connection.execute(
            text("INSERT INTO users (id, username, password) VALUES (940, 'dup', 'x')")
        )
        connection.execute(
            text(
                "INSERT INTO user_llm_provider_credentials "
                "(user_id, provider, encrypted_api_key, enabled) VALUES "
                "(940, 'openai', 'first', 0), "
                "(940, 'openai', 'second', 1)"
            )
        )
        assert connection.execute(
            text(
                "SELECT COUNT(*) FROM user_llm_provider_credentials "
                "WHERE user_id = 940 AND provider = 'openai'"
            )
        ).scalar_one() == 2
