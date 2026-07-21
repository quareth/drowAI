"""Migration tests for approved GPT-OSS agent-dialect route updates."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import create_engine, text


_REPO_ROOT = Path(__file__).resolve().parents[3]
_BACKEND_DIR = _REPO_ROOT / "backend"
_CONSERVATIVE_DIALECT = "openai_compatible_chat.conservative_v1"
_AGENT_DIALECT = "openai_compatible_chat.agent_v1"


def _run_alembic(database_url: str, *args: str) -> None:
    """Run one Alembic command against the isolated test database."""

    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(_BACKEND_DIR),
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
    )


def test_agent_dialect_migration_updates_only_approved_presets(tmp_path: Path) -> None:
    """Approved routes advance while arbitrary custom routes remain conservative."""

    database_url = f"sqlite:///{tmp_path / 'gpt-oss-agent-dialect.sqlite'}"
    _run_alembic(database_url, "upgrade", "0009_llm_legacy_retirement")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, password) "
                "VALUES (940, 'dialect-user', 'hashed')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO llm_inference_connections "
                "(id, user_id, display_name, connection_preset_id, "
                "runtime_family_id, state, revision) VALUES "
                "('00000000-0000-0000-0000-000000000941', 940, 'NVIDIA', "
                "'nvidia_nim_openai_compatible_chat', 'openai_compatible', "
                "'enabled', 1), "
                "('00000000-0000-0000-0000-000000000942', 940, 'Custom', "
                "'custom_openai_compatible_chat', 'openai_compatible', "
                "'enabled', 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO llm_model_deployments "
                "(id, connection_id, wire_model_id, display_name, discovery_source, "
                "lifecycle_state, availability_state, enabled, revision) VALUES "
                "('00000000-0000-0000-0000-000000000943', "
                "'00000000-0000-0000-0000-000000000941', "
                "'openai/gpt-oss-20b', 'GPT-OSS 20B', 'preset', "
                "'active', 'available', 1, 1), "
                "('00000000-0000-0000-0000-000000000944', "
                "'00000000-0000-0000-0000-000000000942', "
                "'custom/model', 'Custom Model', 'operator', "
                "'active', 'available', 1, 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO llm_deployment_routes "
                "(id, deployment_id, adapter_id, adapter_version, api_surface, "
                "dialect_policy_id, enabled) VALUES "
                "('00000000-0000-0000-0000-000000000945', "
                "'00000000-0000-0000-0000-000000000943', "
                "'openai_compatible_chat', '1', 'chat_completions', "
                ":dialect, 1), "
                "('00000000-0000-0000-0000-000000000946', "
                "'00000000-0000-0000-0000-000000000944', "
                "'openai_compatible_chat', '1', 'chat_completions', "
                ":dialect, 1)"
            ),
            {"dialect": _CONSERVATIVE_DIALECT},
        )

    _run_alembic(database_url, "upgrade", "head")
    with engine.connect() as connection:
        dialects = dict(
            connection.execute(
                text(
                    "SELECT connection.connection_preset_id, route.dialect_policy_id "
                    "FROM llm_deployment_routes AS route "
                    "JOIN llm_model_deployments AS deployment "
                    "ON deployment.id = route.deployment_id "
                    "JOIN llm_inference_connections AS connection "
                    "ON connection.id = deployment.connection_id"
                )
            ).all()
        )

    assert dialects == {
        "nvidia_nim_openai_compatible_chat": _AGENT_DIALECT,
        "custom_openai_compatible_chat": _CONSERVATIVE_DIALECT,
    }
