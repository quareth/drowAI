"""Streaming/system SQLAlchemy ORM models.

Scope:
- Declares durable system-level event rows (`SystemLog`) and replayable stream
  packet rows (`StreamEvent`) used for task event ordering and replay.
- Registers streaming ORM models on the shared `Base` from `backend.database`.

Boundaries:
- ORM table definitions and relationships only; no stream fanout, ingestion,
  replay orchestration, or transport-layer behavior.
- Streaming runtime behavior remains in `backend.services.*`.
"""

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from backend.database import Base


class SystemLog(Base):
    """Non-chat system events: agent reasoning, container status, etc. (Phase 1)."""

    __tablename__ = "system_logs"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    sequence = Column(BigInteger, nullable=False)
    type = Column(String(50), nullable=False)
    content = Column(Text, nullable=True)
    log_metadata = Column(JSON, nullable=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("task_id", "sequence", name="ux_system_logs_task_sequence"),
        Index("ix_system_logs_tenant_task_sequence", "tenant_id", "task_id", "sequence"),
        Index("ix_system_logs_task_sequence", "task_id", "sequence"),
        Index("ix_system_logs_timestamp", "timestamp"),
    )


class StreamEvent(Base):
    """Persisted stream packets for chat/agent event replay (Phase 1)."""

    __tablename__ = "stream_events"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    sequence = Column(BigInteger, nullable=False)
    event_type = Column(String(50), nullable=True)
    conversation_id = Column(String(255), nullable=True)
    turn_id = Column(String(255), nullable=True)
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("task_id", "sequence", name="ux_stream_events_task_sequence"),
        Index("ix_stream_events_tenant_task_sequence", "tenant_id", "task_id", "sequence"),
        Index("ix_stream_events_task_sequence", "task_id", "sequence"),
        Index("ix_stream_events_task_conversation", "task_id", "conversation_id"),
        Index("ix_stream_events_task_turn", "task_id", "turn_id"),
        Index("ix_stream_events_timestamp", "created_at"),
    )
