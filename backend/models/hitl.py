"""HITL/workflow SQLAlchemy ORM models.

Scope:
- Declares durable workflow state rows (`TurnWorkflow`) and interrupt ticket
  identity/lifecycle rows (`InterruptTicket`) for pause/resume orchestration.
- Defines `InterruptTicketState` as the canonical enum used by the
  `interrupt_tickets.state` ORM column.

Boundaries:
- ORM table definitions and enum values only; no workflow orchestration,
  interrupt handling services, or router logic.
- Runtime resume/pause behavior lives in `backend.services.langgraph_chat.*`.
"""

from enum import Enum

from sqlalchemy import (
    Column,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.sql import func

from backend.database import Base


class TurnWorkflow(Base):
    """Durable HITL turn workflow state machine records."""

    __tablename__ = "turn_workflows"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    conversation_id = Column(String(255), nullable=False, default="")
    turn_id = Column(String(255), nullable=False)
    turn_sequence = Column(Integer, nullable=True)
    state = Column(String(64), nullable=False)

    graph_name = Column(String(128), nullable=True)
    checkpoint_id = Column(String(255), nullable=True)
    interrupt_type = Column(String(64), nullable=True)
    reserved_message_id = Column(Integer, nullable=True)
    resume_key = Column(String(255), nullable=True)
    workflow_metadata = Column(JSON, nullable=True)

    waiting_at = Column(DateTime(timezone=True), nullable=True)
    resumed_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    failed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("task_id", "turn_id", name="ux_turn_workflows_task_turn_id"),
        Index("ix_turn_workflows_tenant_task_turn_sequence", "tenant_id", "task_id", "turn_sequence"),
        Index("ix_turn_workflows_task_state", "task_id", "state"),
        Index("ix_turn_workflows_task_resume_key", "task_id", "resume_key"),
        Index("ix_turn_workflows_task_checkpoint", "task_id", "checkpoint_id"),
        Index("ix_turn_workflows_task_turn_sequence", "task_id", "turn_sequence"),
    )


class InterruptTicketState(str, Enum):
    """Allowed lifecycle states for durable interrupt tickets."""

    PENDING = "PENDING"
    RESUMING = "RESUMING"
    RESUMED = "RESUMED"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class InterruptTicket(Base):
    """Durable per-interrupt identity with lifecycle authority constraints.

    Invariants:
    - `interrupt_id` is globally unique to prevent duplicate identity rows.
    - `(task_id, state)` is indexed to keep authoritative pending lookups fast.
    """

    __tablename__ = "interrupt_tickets"

    id = Column(Integer, primary_key=True, index=True)
    interrupt_id = Column(String(255), nullable=False)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    graph_name = Column(String(128), nullable=False)
    interrupt_type = Column(String(64), nullable=False)
    checkpoint_id = Column(String(255), nullable=True)
    thread_id = Column(String(255), nullable=True)
    turn_id = Column(String(255), nullable=True)
    turn_sequence = Column(Integer, nullable=True)
    tool_call_id = Column(String(255), nullable=True)
    state = Column(
        SQLEnum(
            InterruptTicketState,
            name="interrupt_ticket_state",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default=InterruptTicketState.PENDING,
    )
    payload_snapshot = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("interrupt_id", name="ux_interrupt_tickets_interrupt_id"),
        Index(
            "ux_interrupt_tickets_task_pending",
            "task_id",
            unique=True,
            postgresql_where=text("state = 'PENDING'"),
            sqlite_where=text("state = 'PENDING'"),
        ),
        Index("ix_interrupt_tickets_interrupt_id", "interrupt_id"),
        Index("ix_interrupt_tickets_tenant_task_state", "tenant_id", "task_id", "state"),
        Index("ix_interrupt_tickets_task_state", "task_id", "state"),
        Index("ix_interrupt_tickets_task_turn_sequence", "task_id", "turn_sequence"),
        Index("ix_interrupt_tickets_tool_call_id", "tool_call_id"),
    )
