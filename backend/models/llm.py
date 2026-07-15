"""LLM provider, conversation, and usage SQLAlchemy ORM models.

Scope:
- Declares provider-neutral user LLM credential and model-selection rows.
- Declares durable conversation continuity rows (`LLMConversation`) and token
  usage accounting rows (`LLMUsageRecord`) for provider/model interactions.
- Registers all LLM ORM models on the shared `Base` from `backend.database`.

Boundaries:
- ORM table definitions only; no credential encryption/decryption, LLM request
  orchestration, pricing logic, token aggregation services, or API schema contracts.
- Runtime LLM behavior and analytics workflows remain in `backend.services.*`.
"""

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
    UniqueConstraint,
)
from sqlalchemy.sql import func

from backend.database import Base


class UserLLMProviderCredential(Base):
    """Encrypted user credential row for one provider."""

    __tablename__ = "user_llm_provider_credentials"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String(50), nullable=False)
    encrypted_api_key = Column(Text, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_user_llm_provider_credentials_user_provider"),
        Index("ix_user_llm_provider_credentials_provider", "provider"),
    )

    @property
    def has_api_key(self) -> bool:
        """Return whether encrypted credential material is present without exposing it."""

        return bool((self.encrypted_api_key or "").strip())


class UserLLMSelection(Base):
    """Selected conversation provider and model for a user."""

    __tablename__ = "user_llm_selections"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String(50), nullable=False, default="openai")
    model = Column(String(100), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_llm_selections_user_id"),
        Index("ix_user_llm_selections_provider_model", "provider", "model"),
    )


class UserReportingLLMSelection(Base):
    """Selected reporting provider/model for task memos and reports."""

    __tablename__ = "user_reporting_llm_selections"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String(50), nullable=False)
    model = Column(String(100), nullable=False)
    reasoning_effort = Column(String(32), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_reporting_llm_selections_user_id"),
        Index(
            "ix_user_reporting_llm_selections_provider_model",
            "provider",
            "model",
        ),
    )


class UserEmbeddingSelection(Base):
    """Selected semantic-memory embedding provider and model for a user."""

    __tablename__ = "user_embedding_selections"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String(50), nullable=False, default="openai")
    model = Column(String(100), nullable=False)
    dimensions = Column(Integer, nullable=False)
    vector_family = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_embedding_selections_user_id"),
        Index(
            "ix_user_embedding_selections_provider_model",
            "provider",
            "model",
        ),
    )


class UserMemoryLLMSelection(Base):
    """Selected semantic-memory LLM dependencies for a user."""

    __tablename__ = "user_memory_llm_selections"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String(50), nullable=False, default="openai")
    gate_model = Column(String(100), nullable=False)
    extraction_model = Column(String(100), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_memory_llm_selections_user_id"),
        Index(
            "ix_user_memory_llm_selections_provider_models",
            "provider",
            "gate_model",
            "extraction_model",
        ),
    )


class LLMConversation(Base):
    __tablename__ = "llm_conversations"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider = Column(String(50), default="openai", nullable=False)
    model = Column(String(100), nullable=True)
    conversation_id = Column(String(255), nullable=True)
    title = Column(String(255), nullable=True)
    status = Column(String(32), default="active")  # active|archived|reset
    is_active = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_llm_conversations_tenant_task_created", "tenant_id", "task_id", "created_at"),
        Index("ix_llm_conversations_task_user_provider", "task_id", "user_id", "provider"),
    )


class LLMUsageRecord(Base):
    """Record of token usage from a single LLM API call.

    Stores actual token counts captured from API responses (not estimates).
    Used for accurate cost tracking and billing visibility.

    Foreign keys use CASCADE delete so usage records are automatically
    removed when the parent task or user is deleted.
    """

    __tablename__ = "llm_usage_records"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    # Token counts (actual from API response.usage)
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    cached_tokens = Column(Integer, nullable=False, default=0)
    reasoning_tokens = Column(Integer, nullable=False, default=0)  # GPT-5 extended thinking

    # Context
    model = Column(String(100), nullable=False)
    provider = Column(String(50), nullable=False, default="openai")
    source = Column(String(50), nullable=False)  # langgraph_normal, langgraph_tool, chat_router
    conversation_id = Column(String(255), nullable=True)

    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    request_metadata = Column(JSON, nullable=True)  # Optional debug info

    __table_args__ = (
        Index("ix_llm_usage_tenant_task_created", "tenant_id", "task_id", "created_at"),
        Index("ix_llm_usage_task_created", "task_id", "created_at"),
        Index("ix_llm_usage_user_created", "user_id", "created_at"),
        Index("ix_llm_usage_task_model", "task_id", "model"),
    )
