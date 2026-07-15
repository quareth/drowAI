"""Provenance/execution SQLAlchemy ORM models.

Scope:
- Declares durable tool execution rows (`ToolExecution`) and produced artifact
  rows (`ExecutionArtifact`) for execution provenance and replay.
- Registers provenance ORM models on the shared `Base` from `backend.database`.

Boundaries:
- ORM table definitions and relationships only; no execution orchestration,
  artifact capture logic, or repository/service behavior.
- Runtime provenance workflows remain in `backend.services.*`.
"""

import uuid as uuid_lib

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    true,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from backend.database import Base, GUID


class ToolExecution(Base):
    """Durable provenance record for each tool execution."""

    __tablename__ = "tool_executions"

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    runtime_job_id = Column(GUID(), ForeignKey("runtime_jobs.id", ondelete="SET NULL"), nullable=True, index=True)
    runner_id = Column(GUID(), ForeignKey("runners.id", ondelete="SET NULL"), nullable=True, index=True)
    execution_site_id = Column(GUID(), ForeignKey("execution_sites.id", ondelete="SET NULL"), nullable=True, index=True)
    command_id = Column(String(255), nullable=True, index=True)
    workspace_id = Column(String(255), nullable=True, index=True)
    chat_message_id = Column(Integer, ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True)
    tool_call_id = Column(String(255), nullable=True)
    conversation_id = Column(String(255), nullable=True)
    turn_id = Column(String(255), nullable=True)
    turn_sequence = Column(Integer, nullable=True)
    tool_name = Column(String(255), nullable=False)
    tool_arguments = Column(JSON, nullable=False)
    purpose = Column(Text, nullable=True)
    agent_path = Column(String(50), nullable=False)
    execution_transport = Column(String(50), nullable=True)
    workspace_path = Column(Text, nullable=True)
    container_path = Column(Text, nullable=True)
    status = Column(String(50), nullable=False)
    exit_code = Column(Integer, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, nullable=True)
    execution_metadata = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    task = relationship("Task", back_populates="tool_executions")
    chat_message = relationship("ChatMessage", back_populates="tool_executions")
    artifacts = relationship("ExecutionArtifact", back_populates="execution", cascade="all, delete-orphan")

    __table_args__ = (
        Index(
            "ux_tool_executions_task_tool_call_id",
            "task_id",
            "tool_call_id",
            unique=True,
        ),
        Index("ix_tool_executions_task_created", "task_id", "created_at"),
        Index("ix_tool_executions_tenant_task_created", "tenant_id", "task_id", "created_at"),
        Index("ix_tool_executions_tenant_runtime_job", "tenant_id", "runtime_job_id"),
        Index("ix_tool_executions_tenant_command", "tenant_id", "command_id"),
        Index("ix_tool_executions_task_tool_created", "task_id", "tool_name", "created_at"),
        Index("ix_tool_executions_conversation_turn", "conversation_id", "turn_id"),
        Index("ix_tool_executions_task_turn_seq", "task_id", "turn_sequence"),
        Index("ix_tool_executions_status_created", "status", "created_at"),
        Index("ix_tool_executions_chat_message", "chat_message_id"),
    )


class ArtifactManifest(Base):
    """Durable runner artifact manifest record for idempotent upload workflows."""

    __tablename__ = "artifact_manifests"

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    runtime_job_id = Column(GUID(), ForeignKey("runtime_jobs.id", ondelete="SET NULL"), nullable=True, index=True)
    runner_id = Column(GUID(), ForeignKey("runners.id", ondelete="SET NULL"), nullable=True, index=True)
    command_id = Column(String(255), nullable=False)
    workspace_id = Column(String(255), nullable=False)
    message_id = Column(String(255), nullable=False)
    idempotency_key = Column(String(255), nullable=True)
    status = Column(String(32), nullable=False, server_default="accepted")
    manifest_json = Column(JSON, nullable=True)
    manifest_metadata = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    artifacts = relationship("ExecutionArtifact", back_populates="manifest")

    __table_args__ = (
        Index("ix_artifact_manifests_tenant_task_created", "tenant_id", "task_id", "created_at"),
        Index("ix_artifact_manifests_tenant_runtime_job", "tenant_id", "runtime_job_id"),
        Index("ix_artifact_manifests_tenant_idempotency", "tenant_id", "idempotency_key"),
        Index(
            "ux_artifact_manifests_tenant_runtime_command_workspace_message",
            "tenant_id",
            "runtime_job_id",
            "command_id",
            "workspace_id",
            "message_id",
            unique=True,
        ),
    )


class ExecutionArtifact(Base):
    """Durable provenance record for each artifact produced by a tool execution."""

    __tablename__ = "execution_artifacts"

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    execution_id = Column(GUID(), ForeignKey("tool_executions.id", ondelete="CASCADE"), nullable=False)
    manifest_id = Column(GUID(), ForeignKey("artifact_manifests.id", ondelete="SET NULL"), nullable=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    runtime_job_id = Column(GUID(), ForeignKey("runtime_jobs.id", ondelete="SET NULL"), nullable=True, index=True)
    runner_id = Column(GUID(), ForeignKey("runners.id", ondelete="SET NULL"), nullable=True, index=True)
    command_id = Column(String(255), nullable=True, index=True)
    artifact_kind = Column(String(50), nullable=False)
    relative_path = Column(Text, nullable=True)
    source_path = Column(Text, nullable=True)
    fallback_path = Column(Text, nullable=True)
    object_key = Column(Text, nullable=True)
    storage_backend = Column(String(64), nullable=True)
    upload_status = Column(String(32), nullable=False, server_default="inline")
    uploaded_at = Column(DateTime(timezone=True), nullable=True)
    content_text = Column(Text, nullable=True)
    content_sha256 = Column(String(64), nullable=True)
    byte_size = Column(BigInteger, nullable=True)
    mime_type = Column(String(255), nullable=True)
    is_text = Column(Boolean, nullable=False, server_default=true())
    artifact_metadata = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    execution = relationship("ToolExecution", back_populates="artifacts")
    manifest = relationship("ArtifactManifest", back_populates="artifacts")

    __table_args__ = (
        Index("ix_execution_artifacts_execution_kind", "execution_id", "artifact_kind"),
        Index("ix_execution_artifacts_task_created", "task_id", "created_at"),
        Index("ix_execution_artifacts_tenant_task_created", "tenant_id", "task_id", "created_at"),
        Index("ix_execution_artifacts_tenant_runtime_job", "tenant_id", "runtime_job_id"),
        Index("ix_execution_artifacts_tenant_command", "tenant_id", "command_id"),
        Index("ix_execution_artifacts_tenant_object_key", "tenant_id", "object_key"),
        Index("ix_execution_artifacts_task_kind_created", "task_id", "artifact_kind", "created_at"),
    )
