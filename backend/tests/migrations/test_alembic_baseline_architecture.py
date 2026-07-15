"""Architecture tests for the Alembic baseline and linear migration graph.

These tests enforce the development-time migration reset contract: active
schema history starts from one static baseline and product startup does not
fall back to ORM table creation or Alembic stamping.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text


_REPO_ROOT = Path(__file__).resolve().parents[3]
_BACKEND_DIR = _REPO_ROOT / "backend"
_BASELINE_PATH = _BACKEND_DIR / "migrations/versions/0001_initial_current_schema.py"
_ENTRYPOINT_PATH = _BACKEND_DIR / "scripts/docker-entrypoint.sh"


def _script_directory() -> ScriptDirectory:
    config = Config(str(_BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_DIR / "migrations"))
    return ScriptDirectory.from_config(config)


def test_active_alembic_graph_is_linear_from_static_baseline() -> None:
    script = _script_directory()

    assert script.get_bases() == ["0001_initial_current_schema"]
    assert script.get_heads() == ["0005_resumable_reports"]
    assert [
        revision.revision for revision in reversed(list(script.walk_revisions()))
    ] == [
        "0001_initial_current_schema",
        "0002_setup_state",
        "0003_runner_peer_ip",
        "0004_purge_retired_sites",
        "0005_resumable_reports",
    ]


def test_retired_runner_site_migration_purges_only_legacy_registry_rows(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "retired-sites.sqlite"
    database_url = f"sqlite:///{db_path}"
    env = {**os.environ, "DATABASE_URL": database_url}

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0003_runner_peer_ip"],
        cwd=str(_BACKEND_DIR),
        env=env,
        check=True,
    )
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO execution_sites (id, tenant_id, name, slug, status) "
                "VALUES ('site-retired', 1, 'Retired', 'retired', 'retired'), "
                "('site-active', 1, 'Active', 'active', 'active')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO runners (id, tenant_id, execution_site_id, name, status) "
                "VALUES ('runner-retired', 1, 'site-retired', 'Retired Runner', 'offline')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO runtime_jobs "
                "(id, tenant_id, runner_id, execution_site_id, job_type, status, idempotency_key) "
                "VALUES ('job-retired', 1, 'runner-retired', 'site-retired', "
                "'runtime.start', 'succeeded', 'retired-job')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO users (id, username, password) "
                "VALUES (900, 'migration-user', 'hashed')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO tasks (id, graph_thread_id, user_id, tenant_id, name) "
                "VALUES (900, 'migration-task', 900, 1, 'Preserved Task')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO tool_executions "
                "(id, tenant_id, task_id, runtime_job_id, runner_id, execution_site_id, "
                "tool_name, tool_arguments, agent_path, status, started_at) "
                "VALUES ('tool-execution', 1, 900, 'job-retired', 'runner-retired', "
                "'site-retired', 'shell', '{}', 'direct', 'completed', CURRENT_TIMESTAMP)"
            )
        )

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(_BACKEND_DIR),
        env=env,
        check=True,
    )

    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM execution_sites WHERE id = 'site-retired'")
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM runners WHERE id = 'runner-retired'")
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM runtime_jobs WHERE id = 'job-retired'")
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM execution_sites WHERE id = 'site-active'")
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM tasks WHERE id = 900")
            ).scalar_one()
            == 1
        )
        preserved_execution = connection.execute(
            text(
                "SELECT runtime_job_id, runner_id, execution_site_id "
                "FROM tool_executions WHERE id = 'tool-execution'"
            )
        ).one()
        assert tuple(preserved_execution) == (None, None, None)


def test_resumable_report_migration_preserves_existing_job_progress(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "resumable-report.sqlite"
    database_url = f"sqlite:///{db_path}"
    env = {**os.environ, "DATABASE_URL": database_url}

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0004_purge_retired_sites"],
        cwd=str(_BACKEND_DIR),
        env=env,
        check=True,
    )
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, password) "
                "VALUES (901, 'report-migration-user', 'hashed')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO engagements (id, tenant_id, user_id, name, status) "
                "VALUES (901, 1, 901, 'Report Migration', 'active')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO engagement_report_jobs "
                "(id, tenant_id, user_id, requested_by_user_id, engagement_id, "
                "report_type, status, idempotency_key, selected_task_memo_ids, "
                "source_watermark, completed_sections, total_sections, attempt_count, "
                "max_attempts) VALUES "
                "('00000000-0000-0000-0000-000000000901', 1, 901, 901, 901, "
                "'pentest', 'queued', 'migration-job', '[]', '{}', "
                "'[\"executive_summary\"]', 7, 1, 3)"
            )
        )

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(_BACKEND_DIR),
        env=env,
        check=True,
    )

    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT generation_phase, completed_sections, total_sections, "
                "attempt_count, next_attempt_at, last_error_at "
                "FROM engagement_report_jobs WHERE id = "
                "'00000000-0000-0000-0000-000000000901'"
            )
        ).one()
        assert row.generation_phase == "sections"
        assert row.completed_sections == '["executive_summary"]'
        assert row.total_sections == 7
        assert row.attempt_count == 1
        assert row.next_attempt_at is None
        assert row.last_error_at is None


def test_baseline_contains_required_static_schema_objects() -> None:
    content = _BASELINE_PATH.read_text(encoding="utf-8")

    assert "CREATE EXTENSION IF NOT EXISTS vector" in content
    assert "INSERT INTO tenants (id, slug, name)" in content
    assert '"tasks"' in content
    assert "tenant_isolation_{table_name}_scope" in content
    assert "tenant_isolation_tenant_memberships_user_lookup_read" in content
    assert "tenant_isolation_semantic_memories_scope" in content
    assert "created by create_all first" not in content
    assert "create_all bootstrap" not in content
    assert "stamp baseline" not in content


def test_docker_entrypoint_has_no_hybrid_bootstrap_path() -> None:
    content = _ENTRYPOINT_PATH.read_text(encoding="utf-8")

    assert "alembic upgrade head" in content
    assert "init_db" not in content
    assert "create_all" not in content
    assert "alembic stamp" not in content


def test_empty_sqlite_database_upgrades_to_required_baseline_tables(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "drowai.sqlite"
    database_url = f"sqlite:///{db_path}"
    env = {**os.environ, "DATABASE_URL": database_url}

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(_BACKEND_DIR),
        env=env,
        check=True,
    )

    engine = create_engine(database_url)
    critical_tables = {
        "users",
        "tenants",
        "tenant_memberships",
        "tasks",
        "platform_installations",
        "execution_sites",
        "runners",
        "runner_credentials",
        "runner_install_tokens",
        "runtime_jobs",
        "runner_connections",
        "runner_control_messages",
        "user_llm_provider_credentials",
    }
    with engine.connect() as connection:
        inspector = inspect(connection)
        missing = sorted(
            table for table in critical_tables if not inspector.has_table(table)
        )
        assert missing == []
        runner_connection_columns = {
            column["name"] for column in inspector.get_columns("runner_connections")
        }
        assert "remote_ip_address" in runner_connection_columns
        report_job_columns = {
            column["name"] for column in inspector.get_columns("engagement_report_jobs")
        }
        assert {
            "generation_phase",
            "next_attempt_at",
            "last_error_at",
        }.issubset(report_job_columns)
        report_job_indexes = {
            index["name"] for index in inspector.get_indexes("engagement_report_jobs")
        }
        assert (
            "ix_engagement_report_jobs_status_next_attempt_created"
            in report_job_indexes
        )
        default_tenant = connection.execute(
            text("SELECT id, slug FROM tenants WHERE id = 1")
        ).one()
        assert tuple(default_tenant) == (1, "default")
