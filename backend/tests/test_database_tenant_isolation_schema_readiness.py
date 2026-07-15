"""Unit tests for tenant_isolation SaaS/RLS schema readiness gating."""

from __future__ import annotations

import pytest

from backend.config.retention import RETENTION_POLICY_DEFAULTS
import backend.database as database


class _InspectorStub:
    def __init__(self, tables: dict[str, set[str]]) -> None:
        self._tables = tables

    def has_table(self, table_name: str) -> bool:
        return table_name in self._tables

    def get_columns(self, table_name: str):
        return [{"name": name} for name in sorted(self._tables.get(table_name, set()))]


def test_tenant_isolation_readiness_skips_checks_when_tenant_isolation_flags_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TENANT_ISOLATION_MULTI_TENANT_CONTEXT_REQUIRED", raising=False)
    monkeypatch.delenv("TENANT_ISOLATION_RLS_ENABLED", raising=False)

    def _unexpected_inspect(_engine):
        raise AssertionError(
            "inspect should not be called when tenant_isolation readiness is disabled"
        )

    monkeypatch.setattr(database, "inspect", _unexpected_inspect)

    database.ensure_tenant_isolation_schema_ready()


def test_tenant_isolation_readiness_reports_missing_tenant_isolation_columns_when_multi_tenant_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TENANT_ISOLATION_MULTI_TENANT_CONTEXT_REQUIRED", "true")
    monkeypatch.delenv("TENANT_ISOLATION_RLS_ENABLED", raising=False)

    inspector = _InspectorStub(
        {
            "reports": {"id", "task_id"},
            "llm_conversations": {"id", "task_id", "tenant_id"},
        }
    )
    monkeypatch.setattr(database, "inspect", lambda _engine: inspector)

    with pytest.raises(RuntimeError) as error:
        database.ensure_tenant_isolation_schema_ready()

    message = str(error.value)
    assert "Tenant isolation schema is not applied" in message
    assert "missing column `reports.tenant_id`" in message
    assert "missing table `llm_usage_records`" in message
    assert "alembic upgrade head" in message


def test_tenant_isolation_readiness_runs_when_rls_mode_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TENANT_ISOLATION_MULTI_TENANT_CONTEXT_REQUIRED", raising=False)
    monkeypatch.setenv("TENANT_ISOLATION_RLS_ENABLED", "true")

    inspector = _InspectorStub({"reports": {"id", "task_id", "tenant_id"}})
    monkeypatch.setattr(database, "inspect", lambda _engine: inspector)

    with pytest.raises(RuntimeError) as error:
        database.ensure_tenant_isolation_schema_ready()

    assert "missing table `llm_conversations`" in str(error.value)


def test_tenant_baseline_readiness_calls_tenant_isolation_check_when_tenant_isolation_flags_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TENANT_ISOLATION_MULTI_TENANT_CONTEXT_REQUIRED", "true")
    monkeypatch.delenv("TENANT_ISOLATION_RLS_ENABLED", raising=False)

    inspector = _InspectorStub(
        {
            "tenants": {"id"},
            "tenant_memberships": {"id", "tenant_id", "user_id"},
            "tasks": {
                "id",
                "tenant_id",
                "runtime_placement_mode",
                "runner_id",
                "execution_site_id",
                "workspace_id",
                "graph_thread_id",
            },
            "engagements": {"id", "tenant_id"},
            "reports": {"id", "task_id"},
        }
    )
    monkeypatch.setattr(database, "inspect", lambda _engine: inspector)

    with pytest.raises(RuntimeError) as error:
        database.ensure_tenant_baseline_schema_ready()

    assert "Tenant isolation schema is not applied" in str(error.value)
    assert "missing column `reports.tenant_id`" in str(error.value)


def test_tenant_baseline_readiness_requires_task_graph_thread_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TENANT_ISOLATION_MULTI_TENANT_CONTEXT_REQUIRED", raising=False)
    monkeypatch.delenv("TENANT_ISOLATION_RLS_ENABLED", raising=False)

    inspector = _InspectorStub(
        {
            "tenants": {"id"},
            "tenant_memberships": {"id", "tenant_id", "user_id"},
            "tasks": {
                "id",
                "tenant_id",
                "runtime_placement_mode",
                "runner_id",
                "execution_site_id",
                "workspace_id",
            },
            "engagements": {"id", "tenant_id"},
        }
    )
    monkeypatch.setattr(database, "inspect", lambda _engine: inspector)

    with pytest.raises(RuntimeError) as error:
        database.ensure_tenant_baseline_schema_ready()

    assert "missing column `tasks.graph_thread_id`" in str(error.value)


def test_reporting_lifecycle_readiness_reports_missing_report_deletion_and_retention_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspector = _InspectorStub(
        {
            "engagement_reports": {"id", "tenant_id", "user_id"},
            "engagement_report_jobs": {"id", "generation_phase"},
            "tenant_data_management_settings": {
                "id",
                "tenant_id",
                "report_retention_enabled",
                "report_history_retention_days",
            },
        }
    )
    monkeypatch.setattr(database, "inspect", lambda _engine: inspector)

    with pytest.raises(RuntimeError) as error:
        database.ensure_reporting_lifecycle_schema_ready()

    message = str(error.value)
    assert "Reporting lifecycle schema is not applied" in message
    assert "missing column `engagement_reports.delete_scheduled_at`" in message
    assert "missing column `engagement_report_jobs.next_attempt_at`" in message
    assert (
        "missing column "
        "`tenant_data_management_settings.task_retention_days_after_terminal`"
    ) in message
    assert (
        "missing column "
        "`tenant_data_management_settings.retention_batch_size_per_tenant`"
    ) in message
    assert "PYTHONPATH=.. alembic upgrade head" in message


def test_reporting_lifecycle_readiness_accepts_current_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspector = _InspectorStub(
        {
            "engagement_reports": {
                "id",
                "delete_scheduled_at",
                "delete_undo_until",
                "deletion_finalized_at",
                "deleted_by_user_id",
                "deletion_reason",
                "deletion_metadata",
                "deletion_original_is_current",
            },
            "engagement_report_jobs": {
                "id",
                "generation_phase",
                "next_attempt_at",
                "last_error_at",
            },
            "tenant_data_management_settings": {
                "id",
                "tenant_id",
                "report_retention_enabled",
                *RETENTION_POLICY_DEFAULTS.keys(),
            },
        }
    )
    monkeypatch.setattr(database, "inspect", lambda _engine: inspector)

    database.ensure_reporting_lifecycle_schema_ready()
