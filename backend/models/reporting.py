"""Reporting storage SQLAlchemy ORM table definitions only.

This module defines durable reporting storage rows and indexes. Ready content
versions are intended to be immutable by service convention except current
pointer and future cache/export metadata bookkeeping.
"""

from __future__ import annotations

import uuid as uuid_lib

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    and_,
    false,
)
from sqlalchemy.sql import func

from backend.database import Base, GUID


class TaskClosureMemo(Base):
    """Task-owned reporting memo content/version row."""

    __tablename__ = "task_closure_memos"

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    schema_version = Column(String(64), nullable=False, default="1", server_default="1")
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    engagement_id = Column(Integer, ForeignKey("engagements.id", ondelete="CASCADE"), nullable=False)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    version = Column(Integer, nullable=False)
    is_current = Column(Boolean, nullable=False, default=False, server_default=false())
    status = Column(String(32), nullable=False)
    memo_mode = Column(String(32), nullable=False)
    source_watermark = Column(JSON, nullable=False, default=dict)
    memo = Column(JSON, nullable=False, default=dict)
    generation_metadata = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    generated_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_task_closure_memos_tenant_engagement_task", "tenant_id", "engagement_id", "task_id"),
        Index(
            "ix_task_closure_memos_tenant_user_engagement_task_current",
            "tenant_id",
            "user_id",
            "engagement_id",
            "task_id",
            "is_current",
        ),
        Index("ix_task_closure_memos_tenant_engagement_status", "tenant_id", "engagement_id", "status"),
        Index("ix_task_closure_memos_tenant_user_updated", "tenant_id", "user_id", "updated_at"),
        Index(
            "ux_task_closure_memos_version",
            "tenant_id",
            "user_id",
            "engagement_id",
            "task_id",
            "version",
            unique=True,
        ),
        Index(
            "ux_task_closure_memos_current_ready",
            "tenant_id",
            "user_id",
            "engagement_id",
            "task_id",
            unique=True,
            sqlite_where=and_(status == "ready", is_current.is_(True)),
            postgresql_where=and_(status == "ready", is_current.is_(True)),
        ),
        Index(
            "ux_task_closure_memos_preparing",
            "tenant_id",
            "user_id",
            "engagement_id",
            "task_id",
            unique=True,
            sqlite_where=and_(status == "preparing"),
            postgresql_where=and_(status == "preparing"),
        ),
    )


class EngagementReport(Base):
    """Tenant/user-owned generated report artifact with source engagement lineage."""

    __tablename__ = "engagement_reports"

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    schema_version = Column(String(64), nullable=False, default="1", server_default="1")
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    engagement_id = Column(Integer, nullable=False)
    engagement_name_snapshot = Column(String(255), nullable=True)
    engagement_status_snapshot = Column(String(32), nullable=True)
    report_type = Column(String(64), nullable=False)
    version = Column(Integer, nullable=False)
    status = Column(String(32), nullable=False)
    is_current = Column(Boolean, nullable=False, default=False, server_default=false())
    title = Column(String(255), nullable=False)
    sections = Column(JSON, nullable=False, default=list)
    markdown_snapshot = Column(Text, nullable=True)
    source_task_memo_ids = Column(JSON, nullable=False, default=list)
    source_knowledge_refs = Column(JSON, nullable=False, default=list)
    source_evidence_refs = Column(JSON, nullable=False, default=list)
    generation_metadata = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    delete_scheduled_at = Column(DateTime(timezone=True), nullable=True)
    delete_undo_until = Column(DateTime(timezone=True), nullable=True)
    deletion_finalized_at = Column(DateTime(timezone=True), nullable=True)
    deleted_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    deletion_reason = Column(String(64), nullable=True)
    deletion_metadata = Column(JSON, nullable=True)
    deletion_original_is_current = Column(Boolean, nullable=False, default=False, server_default=false())
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    generated_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_engagement_reports_tenant_engagement_created", "tenant_id", "engagement_id", "created_at"),
        Index("ix_engagement_reports_tenant_delete_undo", "tenant_id", "delete_undo_until"),
        Index("ix_engagement_reports_tenant_deletion_finalized", "tenant_id", "deletion_finalized_at"),
        Index(
            "ix_engagement_reports_tenant_user_engagement_type_current",
            "tenant_id",
            "user_id",
            "engagement_id",
            "report_type",
            "is_current",
        ),
        Index("ix_engagement_reports_tenant_user_created", "tenant_id", "user_id", "created_at"),
        Index("ix_engagement_reports_tenant_status", "tenant_id", "status"),
        Index(
            "ux_engagement_reports_version",
            "tenant_id",
            "user_id",
            "engagement_id",
            "report_type",
            "version",
            unique=True,
        ),
        Index(
            "ux_engagement_reports_current_ready",
            "tenant_id",
            "user_id",
            "engagement_id",
            "report_type",
            unique=True,
            sqlite_where=and_(status == "ready", is_current.is_(True)),
            postgresql_where=and_(status == "ready", is_current.is_(True)),
        ),
    )


class EngagementReportJob(Base):
    """Durable engagement report generation job state row."""

    __tablename__ = "engagement_report_jobs"

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    schema_version = Column(String(64), nullable=False, default="1", server_default="1")
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    requested_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    engagement_id = Column(Integer, ForeignKey("engagements.id", ondelete="CASCADE"), nullable=False)
    report_id = Column(GUID(), ForeignKey("engagement_reports.id", ondelete="SET NULL"), nullable=True)
    report_type = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False)
    generation_phase = Column(String(32), nullable=False, default="sections", server_default="sections")
    idempotency_key = Column(String(255), nullable=False)
    selected_task_memo_ids = Column(JSON, nullable=False, default=list)
    include_candidate_findings = Column(Boolean, nullable=False, default=False, server_default=false())
    llm_runtime_selection = Column(JSON, nullable=True)
    source_watermark = Column(JSON, nullable=False, default=dict)
    current_section_id = Column(String(128), nullable=True)
    completed_sections = Column(JSON, nullable=False, default=list)
    total_sections = Column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    locked_by = Column(String(255), nullable=True)
    locked_at = Column(DateTime(timezone=True), nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    max_attempts = Column(Integer, nullable=False, default=3, server_default="3")
    last_error_code = Column(String(128), nullable=True)
    error_message = Column(Text, nullable=True)
    last_error_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_engagement_report_jobs_tenant_engagement_created", "tenant_id", "engagement_id", "created_at"),
        Index("ix_engagement_report_jobs_tenant_status_created", "tenant_id", "status", "created_at"),
        Index("ix_engagement_report_jobs_tenant_user_status", "tenant_id", "user_id", "status"),
        Index("ix_engagement_report_jobs_status_created", "status", "created_at"),
        Index(
            "ix_engagement_report_jobs_status_next_attempt_created",
            "status",
            "next_attempt_at",
            "created_at",
        ),
        Index("ix_engagement_report_jobs_locked_at", "locked_at"),
        Index(
            "ux_engagement_report_jobs_tenant_idempotency",
            "tenant_id",
            "idempotency_key",
            unique=True,
            sqlite_where=idempotency_key.is_not(None),
            postgresql_where=idempotency_key.is_not(None),
        ),
    )


__all__ = [
    "EngagementReport",
    "EngagementReportJob",
    "TaskClosureMemo",
]
