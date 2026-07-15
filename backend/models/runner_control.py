"""Runner control-plane SQLAlchemy ORM models for runner control.

Scope:
- Defines tenant-bound runner control-plane system-of-record tables used for
  execution site identity, runner registry state, credentials/install tokens,
  runtime job assignment records, active channel presence, and durable control
  message tracking.

Boundaries:
- ORM table/relationship definitions and schema constraints only.
- No registration workflows, assignment orchestration, or protocol logic.
"""

import uuid as uuid_lib

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from backend.database import Base, GUID


class ExecutionSite(Base):
    __tablename__ = "execution_sites"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_execution_sites_tenant_slug"),
        UniqueConstraint("tenant_id", "name", name="uq_execution_sites_tenant_name"),
        Index("ix_execution_sites_tenant_status", "tenant_id", "status"),
    )

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(128), nullable=False)
    network_label = Column(String(255), nullable=True)
    status = Column(String(32), nullable=False, default="active", server_default="active")
    labels_json = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    tenant = relationship("Tenant")
    runners = relationship("Runner", back_populates="execution_site")
    runtime_jobs = relationship("RuntimeJob", back_populates="execution_site")
    install_tokens = relationship("RunnerInstallToken", back_populates="execution_site")


class Runner(Base):
    __tablename__ = "runners"
    __table_args__ = (
        UniqueConstraint("tenant_id", "execution_site_id", "name", name="uq_runners_tenant_site_name"),
        Index("ix_runners_tenant_status", "tenant_id", "status"),
        Index("ix_runners_tenant_last_seen", "tenant_id", "last_seen_at"),
    )

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    execution_site_id = Column(GUID(), ForeignKey("execution_sites.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    status = Column(String(32), nullable=False, default="inactive", server_default="inactive")
    version = Column(String(64), nullable=True)
    capabilities_json = Column(JSON, nullable=True)
    labels_json = Column(JSON, nullable=True)
    # Nullable physical ceiling override; NULL means fallback to configured/global capacity defaults.
    max_active_tasks = Column(Integer, nullable=True)
    capacity_json = Column(JSON, nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    tenant = relationship("Tenant")
    execution_site = relationship("ExecutionSite", back_populates="runners")
    credentials = relationship("RunnerCredential", back_populates="runner", cascade="all, delete-orphan")
    runtime_jobs = relationship("RuntimeJob", back_populates="runner")
    connections = relationship("RunnerConnection", back_populates="runner", cascade="all, delete-orphan")
    control_messages = relationship("RunnerControlMessage", back_populates="runner")


class RunnerCredential(Base):
    __tablename__ = "runner_credentials"
    __table_args__ = (
        UniqueConstraint("tenant_id", "credential_fingerprint", name="uq_runner_credentials_tenant_fingerprint"),
        Index("ix_runner_credentials_tenant_runner", "tenant_id", "runner_id"),
    )

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    runner_id = Column(GUID(), ForeignKey("runners.id", ondelete="CASCADE"), nullable=False, index=True)
    credential_fingerprint = Column(String(128), nullable=False)
    secret_hash = Column(String(255), nullable=False)
    status = Column(String(32), nullable=False, default="active", server_default="active")
    expires_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    tenant = relationship("Tenant")
    runner = relationship("Runner", back_populates="credentials")


class RunnerInstallToken(Base):
    __tablename__ = "runner_install_tokens"
    __table_args__ = (
        UniqueConstraint("tenant_id", "token_hash", name="uq_runner_install_tokens_tenant_hash"),
        Index("ix_runner_install_tokens_tenant_site_status", "tenant_id", "execution_site_id", "status"),
    )

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    execution_site_id = Column(GUID(), ForeignKey("execution_sites.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String(255), nullable=False)
    status = Column(String(32), nullable=False, default="issued", server_default="issued")
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    tenant = relationship("Tenant")
    execution_site = relationship("ExecutionSite", back_populates="install_tokens")
    created_by_user = relationship("User")


class RuntimeJob(Base):
    __tablename__ = "runtime_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "job_type", "idempotency_key", name="uq_runtime_jobs_tenant_type_idempotency"),
        Index("ix_runtime_jobs_tenant_status", "tenant_id", "status"),
        Index("ix_runtime_jobs_tenant_runner_status", "tenant_id", "runner_id", "status"),
    )

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    runner_id = Column(GUID(), ForeignKey("runners.id", ondelete="SET NULL"), nullable=True, index=True)
    execution_site_id = Column(GUID(), ForeignKey("execution_sites.id", ondelete="SET NULL"), nullable=True, index=True)
    job_type = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="queued", server_default="queued")
    idempotency_key = Column(String(255), nullable=False)
    correlation_id = Column(String(255), nullable=True)
    payload_json = Column(JSON, nullable=True)
    result_json = Column(JSON, nullable=True)
    error_code = Column(String(128), nullable=True)
    error_message = Column(Text, nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    tenant = relationship("Tenant")
    task = relationship("Task")
    runner = relationship("Runner", back_populates="runtime_jobs")
    execution_site = relationship("ExecutionSite", back_populates="runtime_jobs")
    control_messages = relationship("RunnerControlMessage", back_populates="runtime_job")


class RunnerConnection(Base):
    __tablename__ = "runner_connections"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "runner_id",
            "pod_id",
            "connection_id",
            name="uq_runner_connections_tenant_runner_pod_connection",
        ),
        Index("ix_runner_connections_tenant_status", "tenant_id", "status"),
        Index("ix_runner_connections_tenant_runner_last_seen", "tenant_id", "runner_id", "last_seen_at"),
    )

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    runner_id = Column(GUID(), ForeignKey("runners.id", ondelete="CASCADE"), nullable=False, index=True)
    pod_id = Column(String(255), nullable=False)
    connection_id = Column(String(255), nullable=False)
    remote_ip_address = Column(String(45), nullable=True)
    status = Column(String(32), nullable=False, default="active", server_default="active")
    lease_expires_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    tenant = relationship("Tenant")
    runner = relationship("Runner", back_populates="connections")


class RunnerControlMessage(Base):
    __tablename__ = "runner_control_messages"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "runner_id",
            "direction",
            "message_id",
            name="uq_runner_control_messages_tenant_runner_direction_message",
        ),
        Index(
            "uq_runner_control_messages_outbound_idempotency",
            "tenant_id",
            "runner_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("direction = 'outbound' AND idempotency_key IS NOT NULL"),
            sqlite_where=text("direction = 'outbound' AND idempotency_key IS NOT NULL"),
        ),
        Index(
            "uq_runner_control_messages_inbound_idempotency",
            "tenant_id",
            "runner_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("direction = 'inbound' AND idempotency_key IS NOT NULL"),
            sqlite_where=text("direction = 'inbound' AND idempotency_key IS NOT NULL"),
        ),
        Index("ix_runner_control_messages_tenant_status", "tenant_id", "status"),
        Index("ix_runner_control_messages_runtime_job", "runtime_job_id"),
    )

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    runner_id = Column(GUID(), ForeignKey("runners.id", ondelete="CASCADE"), nullable=False, index=True)
    runtime_job_id = Column(GUID(), ForeignKey("runtime_jobs.id", ondelete="SET NULL"), nullable=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    message_id = Column(String(255), nullable=False)
    direction = Column(String(16), nullable=False)
    type = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="pending", server_default="pending")
    idempotency_key = Column(String(255), nullable=True)
    correlation_id = Column(String(255), nullable=True)
    payload_json = Column(JSON, nullable=True)
    delivery_attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    error_code = Column(String(128), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    tenant = relationship("Tenant")
    runner = relationship("Runner", back_populates="control_messages")
    runtime_job = relationship("RuntimeJob", back_populates="control_messages")
    task = relationship("Task")
