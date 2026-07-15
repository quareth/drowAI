"""Chat/conversation SQLAlchemy ORM models.

Scope:
- Declares durable chat continuity rows (`ChatMessage`), nested tool call rows
  (`ToolCall`), canonical per-turn detail events (`ChatTurnEvent`), and legacy
  reasoning/event log rows (`AgentLog`).
- Registers all chat/conversation ORM models on the shared `Base` from
  `backend.database`.

Boundaries:
- ORM table definitions and relationships only; no chat orchestration,
  transcript rebuild logic, or router/service behavior.
- Runtime chat services remain in `backend.services.langgraph_chat.*`.
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
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from backend.database import Base


class AgentLog(Base):
    """Agent reasoning and execution event log.

    DEPRECATED for chat continuity. Use ChatMessage instead.

    AgentLog is retained for:
    - Analytics and debugging
    - Reasoning panel display (/reasoning/history endpoint)
    - Audit logs and compliance

    For chat history, message replay, and persistence across refreshes,
    use ChatMessage as the single source of truth.

    See: ChatMessage, ConversationHistoryReader, GET /tasks/{id}/chat/history
    """

    __tablename__ = "agent_logs"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    # Phase 2: monotonic per-task sequence for ordering/resume
    sequence = Column(BigInteger, nullable=True)
    type = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    log_metadata = Column(JSON)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

    # Conversation grouping (e.g. LangGraph thread_id); nullable for legacy rows
    conversation_id = Column(String(255), nullable=True)

    # Turn metadata columns for grouping events into logical turns (required after backfill)
    # turn_id: Unique identifier for the turn (e.g., "task-123-turn-5")
    turn_id = Column(String(255), nullable=False)
    # turn_number: Sequential turn number within task (1, 2, 3, ...)
    turn_number = Column(Integer, nullable=False)
    # parent_event_id: Links nested events (e.g., tool observation -> tool start event)
    parent_event_id = Column(Integer, ForeignKey("agent_logs.id"), nullable=True)

    # Relationships
    task = relationship("Task", back_populates="agent_logs")
    # Self-referential relationship for parent/child events
    parent_event = relationship("AgentLog", remote_side=[id], backref="child_events")

    __table_args__ = (
        UniqueConstraint("task_id", "sequence", name="ux_agent_logs_task_sequence"),
        Index("ix_agent_logs_tenant_task_sequence", "tenant_id", "task_id", "sequence"),
        Index("ix_agent_logs_task_sequence", "task_id", "sequence"),
        Index("ix_agent_logs_task_timestamp", "task_id", "timestamp"),
        Index("ix_agent_logs_sequence_timestamp", "sequence", "timestamp"),
        Index("ix_agent_logs_conversation_id", "conversation_id"),
        Index("ix_agent_logs_task_conversation", "task_id", "conversation_id"),
        Index("ix_agent_logs_turn_id", "turn_id"),
        Index("ix_agent_logs_task_turn", "task_id", "turn_number"),
    )


class ChatMessage(Base):
    """Message-centric chat continuity (Phase 1). Tree structure for branching."""

    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    conversation_id = Column(String(255), nullable=False)
    # Canonical per-task turn sequence (aligned with TurnNumberService)
    turn_number = Column(Integer, nullable=True)
    parent_message_id = Column(
        Integer, ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    latest_child_message_id = Column(
        Integer, ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    message_type = Column(String(20), nullable=False)
    message = Column(Text, nullable=False)
    token_count = Column(Integer, default=0)
    reasoning_tokens = Column(Text, nullable=True)
    observation_tokens = Column(Text, nullable=True)
    citations = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    parent_message = relationship(
        "ChatMessage",
        remote_side=[id],
        foreign_keys=[parent_message_id],
        backref="children",
    )
    latest_child_message = relationship(
        "ChatMessage",
        remote_side=[id],
        foreign_keys=[latest_child_message_id],
    )
    tool_calls = relationship("ToolCall", back_populates="chat_message", cascade="all, delete-orphan")
    tool_executions = relationship("ToolExecution", back_populates="chat_message")
    chat_turn_events = relationship(
        "ChatTurnEvent", back_populates="chat_message", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_chat_messages_task_conversation", "task_id", "conversation_id"),
        Index("ix_chat_messages_tenant_task_created", "tenant_id", "task_id", "created_at"),
        Index("ix_chat_messages_task_turn", "task_id", "turn_number"),
        Index("ix_chat_messages_parent", "parent_message_id"),
        Index("ix_chat_messages_created", "created_at"),
    )


class ChatTurnEvent(Base):
    """Canonical per-turn ordered chat detail events for transcript rebuild."""

    __tablename__ = "chat_turn_events"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    conversation_id = Column(String(255), nullable=False)
    chat_message_id = Column(Integer, ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False)
    turn_number = Column(Integer, nullable=False)
    phase_sequence = Column(Integer, nullable=False)
    kind = Column(String(32), nullable=False)  # tool | observation
    sub_turn_index = Column(Integer, nullable=True)
    tool_call_id = Column(String(255), nullable=True)
    content = Column(Text, nullable=True)
    event_metadata = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    task = relationship("Task", back_populates="chat_turn_events")
    chat_message = relationship("ChatMessage", back_populates="chat_turn_events")

    __table_args__ = (
        UniqueConstraint(
            "chat_message_id",
            "phase_sequence",
            name="ux_chat_turn_events_message_phase_sequence",
        ),
        Index(
            "ix_chat_turn_events_task_conv_turn_phase",
            "task_id",
            "conversation_id",
            "turn_number",
            "phase_sequence",
        ),
        Index(
            "ix_chat_turn_events_tenant_task_created",
            "tenant_id",
            "task_id",
            "created_at",
        ),
    )


class ToolCall(Base):
    """Nested tool calls linked to ChatMessage (Phase 1)."""

    __tablename__ = "tool_calls"

    id = Column(Integer, primary_key=True, index=True)
    chat_message_id = Column(Integer, ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    parent_tool_call_id = Column(Integer, ForeignKey("tool_calls.id", ondelete="CASCADE"), nullable=True)
    tool_call_id = Column(String(255), nullable=False)
    tool_id = Column(Integer, nullable=True)
    tool_name = Column(String(255), nullable=False)
    tool_arguments = Column(JSON, nullable=False)
    tool_result = Column(Text, nullable=True)
    turn_index = Column(Integer, nullable=False)
    tab_index = Column(Integer, nullable=True)
    reasoning_tokens = Column(Text, nullable=True)
    generated_images = Column(JSON, nullable=True)
    tool_call_tokens = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    chat_message = relationship("ChatMessage", back_populates="tool_calls")
    parent_tool_call = relationship("ToolCall", remote_side=[id], backref="child_calls")

    __table_args__ = (
        UniqueConstraint("chat_message_id", "tool_call_id", name="ux_tool_calls_chat_message_tool_call_id"),
        Index("ix_tool_calls_tenant_message_created", "tenant_id", "chat_message_id", "created_at"),
        Index("ix_tool_calls_message", "chat_message_id"),
        Index("ix_tool_calls_parent", "parent_tool_call_id"),
    )
