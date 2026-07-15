"""Pydantic schemas for tenant data management settings APIs.

These contracts expose lifecycle policy settings without persistence logic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from backend.config.retention import (
    MAX_RETENTION_BATCH_SIZE_PER_TENANT,
    MAX_RETENTION_DAYS,
    MIN_RETENTION_BATCH_SIZE_PER_TENANT,
    MIN_RETENTION_DAYS,
)


RetentionDays = Annotated[int, Field(ge=MIN_RETENTION_DAYS, le=MAX_RETENTION_DAYS)]
RetentionBatchSize = Annotated[
    int,
    Field(
        ge=MIN_RETENTION_BATCH_SIZE_PER_TENANT,
        le=MAX_RETENTION_BATCH_SIZE_PER_TENANT,
    ),
]


class _DataManagementSchema(BaseModel):
    """Base model configuration for data management API schemas."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class TenantDataManagementSettingsUpdateRequest(_DataManagementSchema):
    """Mutable tenant data management settings payload."""

    report_retention_enabled: bool | None = None
    operational_log_retention_days: RetentionDays | None = None
    runner_control_retention_days: RetentionDays | None = None
    checkpoint_retention_days_after_terminal: RetentionDays | None = None
    task_retention_days_after_terminal: RetentionDays | None = None
    chat_transcript_retention_days_after_terminal: RetentionDays | None = None
    artifact_payload_retention_days: RetentionDays | None = None
    artifact_metadata_retention_days_after_terminal: RetentionDays | None = None
    report_history_retention_days: RetentionDays | None = None
    report_job_retention_days: RetentionDays | None = None
    task_memo_history_retention_days: RetentionDays | None = None
    semantic_memory_stale_retention_days: RetentionDays | None = None
    usage_record_retention_days: RetentionDays | None = None
    retention_batch_size_per_tenant: RetentionBatchSize | None = None


class TenantDataManagementSettingsResponse(_DataManagementSchema):
    """Tenant data management settings response."""

    tenant_id: int
    report_retention_enabled: bool
    operational_log_retention_days: RetentionDays
    runner_control_retention_days: RetentionDays
    checkpoint_retention_days_after_terminal: RetentionDays
    task_retention_days_after_terminal: RetentionDays
    chat_transcript_retention_days_after_terminal: RetentionDays
    artifact_payload_retention_days: RetentionDays
    artifact_metadata_retention_days_after_terminal: RetentionDays
    report_history_retention_days: RetentionDays
    report_job_retention_days: RetentionDays
    task_memo_history_retention_days: RetentionDays
    semantic_memory_stale_retention_days: RetentionDays
    usage_record_retention_days: RetentionDays
    retention_batch_size_per_tenant: RetentionBatchSize
    created_at: datetime
    updated_at: datetime


__all__ = [
    "TenantDataManagementSettingsResponse",
    "TenantDataManagementSettingsUpdateRequest",
]
