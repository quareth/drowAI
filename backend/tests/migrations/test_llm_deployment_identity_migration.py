"""Migration tests for append-only deployment-aware LLM identity schema."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


_REPO_ROOT = Path(__file__).resolve().parents[3]
_BACKEND_DIR = _REPO_ROOT / "backend"
_REVISION_PATH = (
    _BACKEND_DIR / "migrations/versions/0006_llm_deployment_identity.py"
)


def _script_directory() -> ScriptDirectory:
    """Load the backend Alembic graph."""

    config = Config(str(_BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_DIR / "migrations"))
    return ScriptDirectory.from_config(config)


def _run_alembic(database_url: str, *args: str) -> None:
    """Run one isolated Alembic command against a temporary database."""

    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(_BACKEND_DIR),
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
    )


def test_revision_is_append_only_after_resumable_reports() -> None:
    """The identity migration remains directly after the reviewed prior head."""

    script = _script_directory()
    revision = script.get_revision("0006_llm_deployment_identity")

    assert _REVISION_PATH.exists()
    assert revision is not None
    assert revision.down_revision == "0005_resumable_reports"
    assert script.get_heads() == ["0012_llm_connector_singletons"]


def test_upgrade_adds_identity_tables_and_preserves_legacy_rows(
    tmp_path: Path,
) -> None:
    """Upgrade is additive and leaves provider/model and embedding snapshots intact."""

    database_url = f"sqlite:///{tmp_path / 'llm-identity.sqlite'}"
    _run_alembic(database_url, "upgrade", "0005_resumable_reports")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, password) "
                "VALUES (920, 'identity-user', 'hashed')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO user_llm_selections (user_id, provider, model) "
                "VALUES (920, 'openai', 'gpt-5.2')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO user_embedding_selections "
                "(user_id, provider, model, dimensions, vector_family) "
                "VALUES (920, 'openai', 'text-embedding-3-small', 1536, "
                "'openai:text-embedding-3-small:1536')"
            )
        )
        embedding_columns_before = {
            column["name"]
            for column in inspect(connection).get_columns(
                "user_embedding_selections"
            )
        }

    engine.dispose()
    _run_alembic(database_url, "upgrade", "head")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        inspector = inspect(connection)
        for table_name in (
            "llm_inference_connections",
            "llm_model_deployments",
            "llm_deployment_routes",
            "llm_capability_observations",
        ):
            assert inspector.has_table(table_name)

        assert {"deployment_id"}.issubset(
            {
                column["name"]
                for column in inspector.get_columns("user_llm_selections")
            }
        )
        assert {"gate_deployment_id", "extraction_deployment_id"}.issubset(
            {
                column["name"]
                for column in inspector.get_columns(
                    "user_memory_llm_selections"
                )
            }
        )
        for table_name in ("llm_conversations", "llm_usage_records"):
            assert {"connection_id", "deployment_id", "route_id"}.issubset(
                {
                    column["name"]
                    for column in inspector.get_columns(table_name)
                }
            )

        embedding_columns_after = {
            column["name"]
            for column in inspector.get_columns("user_embedding_selections")
        }
        assert embedding_columns_after == embedding_columns_before

        legacy_selection = connection.execute(
            text(
                "SELECT provider, model, deployment_id "
                "FROM user_llm_selections WHERE user_id = 920"
            )
        ).one()
        assert tuple(legacy_selection) == ("openai", "gpt-5.2", None)

        connection.execute(
            text(
                "INSERT INTO llm_inference_connections "
                "(id, user_id, display_name, connection_preset_id, "
                "runtime_family_id, state, revision, "
                "legacy_default_provider) VALUES "
                "('00000000-0000-0000-0000-000000000921', 920, "
                "'Legacy OpenAI', 'openai', 'openai', 'draft', 1, 'openai')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO llm_model_deployments "
                "(id, connection_id, wire_model_id, display_name, "
                "discovery_source, lifecycle_state, availability_state, "
                "enabled, revision) VALUES "
                "('00000000-0000-0000-0000-000000000922', "
                "'00000000-0000-0000-0000-000000000921', "
                "'Org/Model-Case:Exact', 'Exact', 'operator', 'active', "
                "'unknown', 1, 1)"
            )
        )
        wire_model_id = connection.execute(
            text(
                "SELECT wire_model_id FROM llm_model_deployments WHERE id = "
                "'00000000-0000-0000-0000-000000000922'"
            )
        ).scalar_one()
        assert wire_model_id == "Org/Model-Case:Exact"

        try:
            connection.execute(
                text(
                    "INSERT INTO llm_inference_connections "
                    "(id, user_id, display_name, connection_preset_id, "
                    "runtime_family_id, state, revision, "
                    "legacy_default_provider) VALUES "
                    "('00000000-0000-0000-0000-000000000923', 920, "
                    "'Duplicate', 'openai', 'openai', 'draft', 1, 'openai')"
                )
            )
        except IntegrityError:
            pass
        else:
            raise AssertionError("duplicate legacy default must be rejected")
