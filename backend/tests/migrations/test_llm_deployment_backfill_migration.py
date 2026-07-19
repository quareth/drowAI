"""Migration tests for deterministic legacy deployment identity backfill."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid5

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text


_REPO_ROOT = Path(__file__).resolve().parents[3]
_BACKEND_DIR = _REPO_ROOT / "backend"
_NAMESPACE = UUID("155b4c21-9f15-4c52-bfec-7fbf407bc63d")


def _run_alembic(database_url: str, *args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(_BACKEND_DIR),
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
    )


def _script_directory() -> ScriptDirectory:
    config = Config(str(_BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_DIR / "migrations"))
    return ScriptDirectory.from_config(config)


def test_backfill_revision_is_append_only_after_identity_schema() -> None:
    """The data backfill is the sole child of the deployment identity schema."""

    script = _script_directory()
    revision = script.get_revision("0007_llm_deployment_backfill")

    assert revision is not None
    assert revision.down_revision == "0006_llm_deployment_identity"
    assert script.get_heads() == ["0010_gpt_oss_agent_dialect"]


def test_migration_maps_exact_models_preserves_ciphertext_and_reruns(
    tmp_path: Path,
) -> None:
    """Upgrade is deterministic, additive, and safe to retry after downgrade."""

    database_url = f"sqlite:///{tmp_path / 'deployment-backfill.sqlite'}"
    _run_alembic(database_url, "upgrade", "0006_llm_deployment_identity")
    engine = create_engine(database_url)
    ciphertext = "migration-ciphertext-must-remain-exact"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, password) VALUES "
                "(930, 'mapped-user', 'hashed'), "
                "(931, 'unmapped-user', 'hashed')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO user_settings "
                "(user_id, openai_api_key, openai_model) VALUES "
                "(930, :ciphertext, 'Org/Model-Case:Exact')"
            ),
            {"ciphertext": ciphertext},
        )
        connection.execute(
            text(
                "INSERT INTO user_llm_selections "
                "(user_id, provider, model) VALUES "
                "(931, 'anthropic', 'claude-unmapped-exact')"
            )
        )

    _run_alembic(database_url, "upgrade", "head")
    connection_id = uuid5(_NAMESPACE, "legacy-connection:930:openai")
    deployment_id = uuid5(
        _NAMESPACE,
        f"legacy-deployment:{connection_id}:Org/Model-Case:Exact",
    )
    with engine.connect() as connection:
        mapped_connection = connection.execute(
            text(
                "SELECT id, user_id, legacy_default_provider FROM "
                "llm_inference_connections WHERE user_id = 930"
            )
        ).one()
        assert tuple(mapped_connection) == (str(connection_id), 930, "openai")
        mapped_deployment = connection.execute(
            text(
                "SELECT id, wire_model_id FROM llm_model_deployments "
                "WHERE connection_id = :connection_id"
            ),
            {"connection_id": str(connection_id)},
        ).one()
        assert tuple(mapped_deployment) == (
            str(deployment_id),
            "Org/Model-Case:Exact",
        )
        selections = connection.execute(
            text(
                "SELECT user_id, model, deployment_id FROM user_llm_selections "
                "ORDER BY user_id"
            )
        ).all()
        assert tuple(selections[0]) == (
            930,
            "Org/Model-Case:Exact",
            str(deployment_id),
        )
        assert tuple(selections[1]) == (931, "claude-unmapped-exact", None)
        assert connection.execute(
            text(
                "SELECT encrypted_api_key FROM user_llm_provider_credentials "
                "WHERE user_id = 930"
            )
        ).scalar_one() == ciphertext

    _run_alembic(database_url, "downgrade", "0006_llm_deployment_identity")
    _run_alembic(database_url, "upgrade", "head")
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM llm_inference_connections")
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT COUNT(*) FROM llm_model_deployments")
        ).scalar_one() == 1
