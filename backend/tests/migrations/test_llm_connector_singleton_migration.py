"""Migration coverage for per-user LLM connector singleton enforcement."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
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


def test_migration_consolidates_duplicates_and_enforces_singletons(
    tmp_path: Path,
) -> None:
    """Upgrade preserves selected identity, latest secrets, and future uniqueness."""

    database_url = f"sqlite:///{tmp_path / 'connector-singletons.sqlite'}"
    _run_alembic(database_url, "upgrade", "0011_connection_credentials")
    engine = create_engine(database_url)
    first_connection = "00000000-0000-0000-0000-000000001201"
    second_connection = "00000000-0000-0000-0000-000000001202"
    first_deployment = "00000000-0000-0000-0000-000000001211"
    second_deployment = "00000000-0000-0000-0000-000000001212"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, password) "
                "VALUES (1201, 'connector-singleton-user', 'hashed')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO user_llm_provider_credentials "
                "(id, user_id, provider, encrypted_api_key, enabled, updated_at) "
                "VALUES "
                "(1201, 1201, 'openai', 'old-provider-secret', 1, '2026-01-01'), "
                "(1202, 1201, 'openai', 'new-provider-secret', 1, '2026-01-02')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO llm_inference_connections "
                "(id, user_id, display_name, connection_preset_id, "
                "runtime_family_id, serving_operator_id, state, revision, updated_at) "
                "VALUES "
                "(:first, 1201, 'Selected NVIDIA', "
                "'nvidia_nim_openai_compatible_chat', 'openai_compatible_chat', "
                "'nvidia_nim', 'enabled', 2, '2026-01-01'), "
                "(:second, 1201, 'Duplicate NVIDIA', "
                "'nvidia_nim_openai_compatible_chat', 'openai_compatible_chat', "
                "'nvidia_nim', 'enabled', 2, '2026-01-02')"
            ),
            {"first": first_connection, "second": second_connection},
        )
        connection.execute(
            text(
                "INSERT INTO llm_connection_credentials "
                "(connection_id, encrypted_api_key, enabled, updated_at) VALUES "
                "(:first, 'old-connection-secret', 1, '2026-01-01'), "
                "(:second, 'new-connection-secret', 1, '2026-01-02')"
            ),
            {"first": first_connection, "second": second_connection},
        )
        connection.execute(
            text(
                "INSERT INTO llm_model_deployments "
                "(id, connection_id, wire_model_id, canonical_model_id, "
                "display_name, discovery_source, lifecycle_state, "
                "availability_state, enabled, revision) VALUES "
                "(:first_deployment, :first_connection, 'openai/gpt-oss-20b', "
                "'openai/gpt-oss-20b', 'GPT-OSS 20B', 'preset', 'active', "
                "'available', 1, 1), "
                "(:second_deployment, :second_connection, 'openai/gpt-oss-20b', "
                "'openai/gpt-oss-20b', 'GPT-OSS 20B', 'preset', 'active', "
                "'available', 1, 1)"
            ),
            {
                "first_connection": first_connection,
                "second_connection": second_connection,
                "first_deployment": first_deployment,
                "second_deployment": second_deployment,
            },
        )
        connection.execute(
            text(
                "INSERT INTO user_llm_selections "
                "(user_id, provider, model, deployment_id) VALUES "
                "(1201, 'nvidia_nim_openai_compatible_chat', "
                "'openai/gpt-oss-20b', :deployment)"
            ),
            {"deployment": first_deployment},
        )

    _run_alembic(database_url, "upgrade", "head")
    with engine.connect() as connection:
        remaining = connection.execute(
            text(
                "SELECT id FROM llm_inference_connections "
                "WHERE user_id = 1201 AND "
                "connection_preset_id = 'nvidia_nim_openai_compatible_chat'"
            )
        ).scalars().all()
        assert remaining == [first_connection]
        assert connection.execute(
            text(
                "SELECT encrypted_api_key FROM llm_connection_credentials "
                "WHERE connection_id = :connection_id"
            ),
            {"connection_id": first_connection},
        ).scalar_one() == "new-connection-secret"
        assert connection.execute(
            text(
                "SELECT encrypted_api_key FROM user_llm_provider_credentials "
                "WHERE user_id = 1201 AND provider = 'openai'"
            )
        ).scalar_one() == "new-provider-secret"
        assert connection.execute(
            text(
                "SELECT COUNT(*) FROM llm_model_deployments "
                "WHERE connection_id = :connection_id"
            ),
            {"connection_id": first_connection},
        ).scalar_one() == 1
        assert connection.execute(
            text(
                "SELECT deployment_id FROM user_llm_selections WHERE user_id = 1201"
            )
        ).scalar_one() == first_deployment

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO llm_inference_connections "
                    "(id, user_id, display_name, connection_preset_id, "
                    "runtime_family_id, state, revision) VALUES "
                    "('00000000-0000-0000-0000-000000001299', 1201, "
                    "'Forbidden duplicate', "
                    "'nvidia_nim_openai_compatible_chat', "
                    "'openai_compatible_chat', 'draft', 1)"
                )
            )
    engine.dispose()
