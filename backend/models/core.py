"""Core identity and lifecycle SQLAlchemy ORM models.

Scope:
- Declares core relational tables for users, tasks, engagements, task history,
  turn counters, and reports.
- Registers all core ORM models on the shared `Base` from `backend.database`.

Boundaries:
- ORM table definitions and relationships only; no query services, no router
  behavior, and no business orchestration logic.
- Task lifecycle transition rules belong to `backend.domain.task_lifecycle`.
"""

import uuid as uuid_lib

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from backend.database import Base
from backend.domain.task_lifecycle import TaskStatus


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(255), unique=True, index=True, nullable=False)
    password = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    is_active = Column(Boolean, default=True)
    # Nullable per-user quota override; NULL means use tenant/config default resolution.
    max_concurrent_tasks = Column(Integer, nullable=True)

    # Relationships
    tasks = relationship("Task", back_populates="user")
    engagements = relationship("Engagement", back_populates="user")
    tenant_memberships = relationship(
        "TenantMembership",
        back_populates="user",
        foreign_keys="TenantMembership.user_id",
    )
    reports = relationship("Report", back_populates="user")
    settings = relationship("UserSettings", back_populates="user", uselist=False)
    refresh_sessions = relationship(
        "UserSession",
        back_populates="user",
        cascade="all, delete-orphan",
    )


class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    refresh_token_hash = Column(String(128), nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_activity_at = Column(DateTime(timezone=True), nullable=False)
    idle_expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    absolute_expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True, index=True)

    user = relationship("User", back_populates="refresh_sessions")


class UserSettings(Base):
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)

    # Other API Keys
    shodan_api_key = Column(Text, nullable=True)

    # General Settings
    session_timeout = Column(Integer, default=1800)  # 30 minutes
    theme = Column(String(20), default="dark")
    timezone = Column(String(50), default="UTC")

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    user = relationship("User", back_populates="settings")


class Task(Base):
    __tablename__ = "tasks"

    # Core identification fields
    id = Column(Integer, primary_key=True, index=True)
    graph_thread_id = Column(String(64), nullable=False, unique=True, default=lambda: uuid_lib.uuid4().hex)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True, default=1, server_default="1")
    engagement_id = Column(Integer, ForeignKey("engagements.id", ondelete="SET NULL"), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    scope = Column(Text)

    # Status tracking with new enum (Step 1.2)
    status = Column(String(50), default=TaskStatus.CREATED.value)

    # Lifecycle timestamps (Step 1.2)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    paused_at = Column(DateTime(timezone=True), nullable=True)
    stopped_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Execution metadata (Step 1.2)
    container_id = Column(String(255), nullable=True)  # Docker container ID
    agent_pid = Column(Integer, nullable=True)  # Agent process ID
    resource_usage = Column(JSON, nullable=True)  # CPU, memory, disk usage

    # Error tracking (Step 1.2)
    error_message = Column(Text, nullable=True)  # Last error message
    failure_reason = Column(String(255), nullable=True)  # Categorized failure reason
    retry_count = Column(Integer, default=0)  # Number of retry attempts

    # Progress tracking (Step 1.2)
    current_step = Column(String(255), nullable=True)  # Current execution step
    total_steps = Column(Integer, nullable=True)  # Total planned steps
    progress_percentage = Column(Integer, default=0)  # 0-100 completion percentage

    # Execution configuration (Step 1.2)
    timeout_seconds = Column(Integer, default=3600)  # Max execution time
    max_retries = Column(Integer, default=3)  # Maximum retry attempts
    priority = Column(Integer, default=1)  # Task priority (1=high, 3=low)

    # Execution mode column kept for compatibility; interactive is the default path.
    mode = Column(String(20), default="interactive")
    # Legacy `local` default is kept for historical/dev compatibility only.
    # Product task creation paths must explicitly set runtime placement.
    runtime_placement_mode = Column(String(32), nullable=False, default="local", server_default="local")
    runner_id = Column(String(255), nullable=True)
    execution_site_id = Column(String(255), nullable=True)
    workspace_id = Column(String(255), nullable=True)

    # VPN Configuration (NEW)
    vpn_enabled = Column(Boolean, default=False)  # Enable VPN for this task
    vpn_provider = Column(String(50), nullable=True)  # "htb", "tryhackme", "custom"
    vpn_config_data = Column(Text, nullable=True)  # Base64 encoded OVPN content
    vpn_connection_status = Column(String(50), default="disconnected")  # Connection state
    vpn_ip_address = Column(String(45), nullable=True)  # Assigned VPN IP
    vpn_connected_at = Column(DateTime(timezone=True), nullable=True)  # Connection timestamp
    vpn_error_message = Column(Text, nullable=True)  # Last VPN error

    # Relationships
    user = relationship("User", back_populates="tasks")
    tenant = relationship("Tenant", back_populates="tasks")
    engagement = relationship("Engagement", back_populates="tasks")
    agent_logs = relationship("AgentLog", back_populates="task", cascade="all, delete-orphan")
    reports = relationship("Report", back_populates="task", cascade="all, delete-orphan")
    status_history = relationship("TaskHistory", back_populates="task", cascade="all, delete-orphan")
    tool_executions = relationship("ToolExecution", back_populates="task", cascade="all, delete-orphan")
    chat_turn_events = relationship("ChatTurnEvent", back_populates="task", cascade="all, delete-orphan")

    @hybrid_property
    def engagement_name(self) -> str | None:
        return self.engagement.name if self.engagement else None


class Engagement(Base):
    """Durable owner boundary for engagement-scoped knowledge."""

    __tablename__ = "engagements"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True, default=1, server_default="1")
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default="active")
    engagement_metadata = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="engagements")
    tenant = relationship("Tenant", back_populates="engagements")
    tasks = relationship("Task", back_populates="engagement")
    knowledge_ingestion_runs = relationship("KnowledgeIngestionRun", back_populates="engagement")
    knowledge_evidence_archives = relationship("KnowledgeEvidenceArchive", back_populates="engagement")
    knowledge_observations = relationship("KnowledgeObservation", back_populates="engagement")
    knowledge_assets = relationship("KnowledgeAsset", back_populates="engagement")
    knowledge_services = relationship("KnowledgeService", back_populates="engagement")
    knowledge_findings = relationship("KnowledgeFinding", back_populates="engagement")
    knowledge_relationships = relationship("KnowledgeRelationship", back_populates="engagement")
    semantic_memories = relationship("SemanticMemory", back_populates="engagement")
    engagement_asset_links = relationship(
        "EngagementAssetLink", back_populates="engagement", cascade="all, delete-orphan"
    )
    engagement_service_links = relationship(
        "EngagementServiceLink", back_populates="engagement", cascade="all, delete-orphan"
    )
    engagement_finding_links = relationship(
        "EngagementFindingLink", back_populates="engagement", cascade="all, delete-orphan"
    )
    engagement_web_path_links = relationship(
        "EngagementWebPathLink", back_populates="engagement", cascade="all, delete-orphan"
    )


class TaskHistory(Base):
    """
    Task State History Model (Step 1.3)

    Provides audit trail for task state changes with complete context
    tracking including who made the change, when, and why.
    """

    __tablename__ = "task_history"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # Who triggered the change

    # State transition tracking
    old_status = Column(String(50), nullable=True)  # Previous status (null for initial creation)
    new_status = Column(String(50), nullable=False)  # New status
    transition_reason = Column(Text, nullable=True)  # Why the transition occurred

    # Change metadata
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    change_source = Column(String(50), default="manual")  # manual, automatic, system, error

    # Additional context (JSON for flexibility)
    change_metadata = Column(JSON, nullable=True)  # Additional context about the change

    # Relationships
    task = relationship("Task", back_populates="status_history")
    user = relationship("User")  # User who triggered the change

    __table_args__ = (
        Index("ix_task_history_tenant_task_timestamp", "tenant_id", "task_id", "timestamp"),
    )


class TaskTurnCounter(Base):
    """Per-task counter for atomic turn number allocation.

    One row per task. Used by TurnNumberService with
    INSERT ... ON CONFLICT DO UPDATE for serialized allocation under concurrency.
    """

    __tablename__ = "task_turn_counter"

    task_id = Column(Integer, primary_key=True)
    next_turn = Column(Integer, nullable=False, server_default="1")


class Report(Base):
    __tablename__ = "reports"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)
    findings = Column(JSON)
    severity = Column(String(20), default="info")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    task = relationship("Task", back_populates="reports")
    user = relationship("User", back_populates="reports")

    __table_args__ = (
        Index("ix_reports_tenant_task_created", "tenant_id", "task_id", "created_at"),
    )
