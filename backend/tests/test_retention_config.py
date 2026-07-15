"""Tests for retention policy defaults and typed rollout config."""

from __future__ import annotations

from backend.config.retention import (
    DEFAULT_OPERATIONAL_LOG_RETENTION_DAYS,
    DEFAULT_RETENTION_BATCH_SIZE_PER_TENANT,
    MAX_RETENTION_BATCH_SIZE_PER_TENANT,
    MAX_RETENTION_DAYS,
    MIN_RETENTION_BATCH_SIZE_PER_TENANT,
    MIN_RETENTION_DAYS,
    RETENTION_BATCH_SIZE_PER_TENANT_BOUNDS,
    RETENTION_DAY_FIELD_BOUNDS,
    RETENTION_POLICY_DEFAULTS,
    get_retention_runtime_config,
)
from backend.models.data_management import TenantDataManagementSettings


MVP_RETENTION_FIELDS = {
    "operational_log_retention_days",
    "runner_control_retention_days",
    "checkpoint_retention_days_after_terminal",
    "task_retention_days_after_terminal",
    "chat_transcript_retention_days_after_terminal",
    "artifact_payload_retention_days",
    "artifact_metadata_retention_days_after_terminal",
    "report_history_retention_days",
    "report_job_retention_days",
    "task_memo_history_retention_days",
    "semantic_memory_stale_retention_days",
    "usage_record_retention_days",
    "retention_batch_size_per_tenant",
}


def test_retention_policy_defaults_cover_all_mvp_fields() -> None:
    assert set(RETENTION_POLICY_DEFAULTS) == MVP_RETENTION_FIELDS
    assert (
        RETENTION_POLICY_DEFAULTS["operational_log_retention_days"]
        == DEFAULT_OPERATIONAL_LOG_RETENTION_DAYS
        == 30
    )
    assert (
        RETENTION_POLICY_DEFAULTS["retention_batch_size_per_tenant"]
        == DEFAULT_RETENTION_BATCH_SIZE_PER_TENANT
        == 100
    )


def test_retention_bounds_are_named_and_shared() -> None:
    assert RETENTION_DAY_FIELD_BOUNDS == (MIN_RETENTION_DAYS, MAX_RETENTION_DAYS)
    assert RETENTION_BATCH_SIZE_PER_TENANT_BOUNDS == (
        MIN_RETENTION_BATCH_SIZE_PER_TENANT,
        MAX_RETENTION_BATCH_SIZE_PER_TENANT,
    )


def test_retention_runtime_config_parses_rollout_flags_without_secrets() -> None:
    config = get_retention_runtime_config(
        {
            "RETENTION_ORCHESTRATOR_ENABLED": "true",
            "RETENTION_DRY_RUN_ONLY": "false",
            "RETENTION_ROLLOUT_STAGE": "beta",
        }
    )

    assert config.orchestrator_enabled is True
    assert config.dry_run_only is False
    assert config.rollout_stage == "beta"


def test_tenant_data_management_model_stores_retention_policy_fields() -> None:
    table = TenantDataManagementSettings.__table__
    tenant_index = next(
        index
        for index in table.indexes
        if index.name == "ix_tenant_data_management_settings_tenant_id"
    )

    assert set(RETENTION_POLICY_DEFAULTS).issubset(table.columns.keys())
    assert tenant_index.unique is True
    assert [column.name for column in tenant_index.columns] == ["tenant_id"]

    for field_name, default_value in RETENTION_POLICY_DEFAULTS.items():
        column = table.columns[field_name]

        assert column.nullable is False
        assert column.default is not None
        assert column.default.arg == default_value
        assert column.server_default is not None
        assert str(column.server_default.arg) == str(default_value)
