"""Unit tests for runner-control cloud-runner schema readiness gating."""

from __future__ import annotations

import pytest

import backend.database as database


class _InspectorStub:
    def __init__(self, tables: dict[str, set[str]]) -> None:
        self._tables = tables

    def has_table(self, table_name: str) -> bool:
        return table_name in self._tables

    def get_columns(self, table_name: str):
        return [{"name": name} for name in sorted(self._tables.get(table_name, set()))]


def test_runner_control_readiness_skips_checks_when_cloud_runner_control_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(database, "is_cloud_runner_control_enabled", lambda: False)

    def _unexpected_inspect(_engine):
        raise AssertionError("inspect should not be called when cloud runner control is disabled")

    monkeypatch.setattr(database, "inspect", _unexpected_inspect)

    database.ensure_runner_control_schema_ready()


def test_runner_control_readiness_reports_missing_tables_and_columns_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(database, "is_cloud_runner_control_enabled", lambda: True)

    inspector = _InspectorStub(
        {
            "execution_sites": {"id", "tenant_id"},
            "runners": {"id", "tenant_id"},
            "runner_credentials": {"id", "tenant_id", "runner_id", "secret"},
        }
    )
    monkeypatch.setattr(database, "inspect", lambda _engine: inspector)

    with pytest.raises(RuntimeError) as error:
        database.ensure_runner_control_schema_ready()

    message = str(error.value)
    assert "Runner control-plane schema is not applied" in message
    assert "missing table `runner_install_tokens`" in message
    assert "missing table `runtime_jobs`" in message
    assert "missing table `runner_connections`" in message
    assert "missing table `runner_control_messages`" in message
    assert "missing column `runners.execution_site_id`" in message
    assert "missing column `runner_credentials.secret_hash`" in message
    assert "disallowed plaintext column `runner_credentials.secret`" in message


def test_runner_control_readiness_requires_delivery_attempt_count_on_runner_control_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(database, "is_cloud_runner_control_enabled", lambda: True)

    inspector = _InspectorStub(
        {
            "execution_sites": {"id", "tenant_id"},
            "runners": {"id", "tenant_id", "execution_site_id"},
            "runner_credentials": {"id", "tenant_id", "runner_id", "secret_hash"},
            "runner_install_tokens": {"id", "tenant_id", "execution_site_id", "token_hash"},
            "runtime_jobs": {"id", "tenant_id", "job_type", "idempotency_key"},
            "runner_connections": {"id", "tenant_id", "runner_id", "pod_id", "connection_id"},
            "runner_control_messages": {"id", "tenant_id", "runner_id", "message_id", "direction"},
        }
    )
    monkeypatch.setattr(database, "inspect", lambda _engine: inspector)

    with pytest.raises(RuntimeError) as error:
        database.ensure_runner_control_schema_ready()

    assert "missing column `runner_control_messages.delivery_attempt_count`" in str(error.value)
