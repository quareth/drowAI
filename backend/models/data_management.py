"""Tenant-scoped data management settings ORM models.

This module stores configurable lifecycle policy values for tenant-owned data.
It defines table shape only; validation and defaults live in services.
"""

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer
from sqlalchemy.sql import func

from backend.config.retention import (
    DEFAULT_REPORT_RETENTION_ENABLED,
    RETENTION_POLICY_DEFAULTS,
)
from backend.database import Base


class TenantDataManagementSettings(Base):
    """Per-tenant data lifecycle policy settings."""

    __tablename__ = "tenant_data_management_settings"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    report_retention_enabled = Column(
        Boolean,
        nullable=False,
        default=DEFAULT_REPORT_RETENTION_ENABLED,
        server_default="true",
    )
    operational_log_retention_days = Column(
        Integer,
        nullable=False,
        default=RETENTION_POLICY_DEFAULTS["operational_log_retention_days"],
        server_default=str(RETENTION_POLICY_DEFAULTS["operational_log_retention_days"]),
    )
    runner_control_retention_days = Column(
        Integer,
        nullable=False,
        default=RETENTION_POLICY_DEFAULTS["runner_control_retention_days"],
        server_default=str(RETENTION_POLICY_DEFAULTS["runner_control_retention_days"]),
    )
    checkpoint_retention_days_after_terminal = Column(
        Integer,
        nullable=False,
        default=RETENTION_POLICY_DEFAULTS["checkpoint_retention_days_after_terminal"],
        server_default=str(
            RETENTION_POLICY_DEFAULTS["checkpoint_retention_days_after_terminal"]
        ),
    )
    task_retention_days_after_terminal = Column(
        Integer,
        nullable=False,
        default=RETENTION_POLICY_DEFAULTS["task_retention_days_after_terminal"],
        server_default=str(
            RETENTION_POLICY_DEFAULTS["task_retention_days_after_terminal"]
        ),
    )
    chat_transcript_retention_days_after_terminal = Column(
        Integer,
        nullable=False,
        default=RETENTION_POLICY_DEFAULTS[
            "chat_transcript_retention_days_after_terminal"
        ],
        server_default=str(
            RETENTION_POLICY_DEFAULTS["chat_transcript_retention_days_after_terminal"]
        ),
    )
    artifact_payload_retention_days = Column(
        Integer,
        nullable=False,
        default=RETENTION_POLICY_DEFAULTS["artifact_payload_retention_days"],
        server_default=str(RETENTION_POLICY_DEFAULTS["artifact_payload_retention_days"]),
    )
    artifact_metadata_retention_days_after_terminal = Column(
        Integer,
        nullable=False,
        default=RETENTION_POLICY_DEFAULTS[
            "artifact_metadata_retention_days_after_terminal"
        ],
        server_default=str(
            RETENTION_POLICY_DEFAULTS[
                "artifact_metadata_retention_days_after_terminal"
            ]
        ),
    )
    report_history_retention_days = Column(
        Integer,
        nullable=False,
        default=RETENTION_POLICY_DEFAULTS["report_history_retention_days"],
        server_default=str(RETENTION_POLICY_DEFAULTS["report_history_retention_days"]),
    )
    report_job_retention_days = Column(
        Integer,
        nullable=False,
        default=RETENTION_POLICY_DEFAULTS["report_job_retention_days"],
        server_default=str(RETENTION_POLICY_DEFAULTS["report_job_retention_days"]),
    )
    task_memo_history_retention_days = Column(
        Integer,
        nullable=False,
        default=RETENTION_POLICY_DEFAULTS["task_memo_history_retention_days"],
        server_default=str(
            RETENTION_POLICY_DEFAULTS["task_memo_history_retention_days"]
        ),
    )
    semantic_memory_stale_retention_days = Column(
        Integer,
        nullable=False,
        default=RETENTION_POLICY_DEFAULTS["semantic_memory_stale_retention_days"],
        server_default=str(
            RETENTION_POLICY_DEFAULTS["semantic_memory_stale_retention_days"]
        ),
    )
    usage_record_retention_days = Column(
        Integer,
        nullable=False,
        default=RETENTION_POLICY_DEFAULTS["usage_record_retention_days"],
        server_default=str(RETENTION_POLICY_DEFAULTS["usage_record_retention_days"]),
    )
    retention_batch_size_per_tenant = Column(
        Integer,
        nullable=False,
        default=RETENTION_POLICY_DEFAULTS["retention_batch_size_per_tenant"],
        server_default=str(
            RETENTION_POLICY_DEFAULTS["retention_batch_size_per_tenant"]
        ),
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index(
            "ix_tenant_data_management_settings_tenant_id",
            "tenant_id",
            unique=True,
        ),
    )


__all__ = ["TenantDataManagementSettings"]
