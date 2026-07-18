"""LLM identity, selection, conversation, and usage SQLAlchemy ORM models.

Scope:
- Declares user-owned inference connections, deployments, routes, and observed
  capabilities for deployment-aware text LLM resolution.
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
    CheckConstraint,
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

from backend.database import Base, GUID


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


class LLMInferenceConnection(Base):
    """User-owned, revisioned configuration for one inference endpoint."""

    __tablename__ = "llm_inference_connections"

    id = Column(GUID(), primary_key=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    display_name = Column(String(255), nullable=False)
    connection_preset_id = Column(String(100), nullable=False)
    runtime_family_id = Column(String(100), nullable=False)
    serving_operator_id = Column(String(100), nullable=True)
    transport_origin = Column(String(32), nullable=False, default="backend")
    endpoint_url = Column(Text, nullable=True)
    endpoint_policy_id = Column(String(100), nullable=True)
    config_schema_version = Column(Integer, nullable=False, default=1)
    non_secret_config = Column(JSON, nullable=True)
    state = Column(String(32), nullable=False, default="draft")
    revision = Column(Integer, nullable=False, default=1)
    legacy_default_provider = Column(String(50), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('draft', 'disabled', 'enabled')",
            name="ck_llm_inference_connections_state",
        ),
        CheckConstraint(
            "revision > 0",
            name="ck_llm_inference_connections_revision",
        ),
        UniqueConstraint(
            "user_id",
            "legacy_default_provider",
            name="uq_llm_inference_connections_legacy_default",
        ),
        Index(
            "ix_llm_inference_connections_user_state",
            "user_id",
            "state",
        ),
    )


class LLMModelDeployment(Base):
    """Exact wire-model identity available through an inference connection."""

    __tablename__ = "llm_model_deployments"

    id = Column(GUID(), primary_key=True)
    connection_id = Column(
        GUID(),
        ForeignKey("llm_inference_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    wire_model_id = Column(String(512), nullable=False)
    canonical_model_id = Column(String(255), nullable=True)
    display_name = Column(String(255), nullable=False)
    discovery_source = Column(String(50), nullable=False)
    source_metadata = Column(JSON, nullable=True)
    lifecycle_state = Column(String(32), nullable=False, default="active")
    availability_state = Column(String(32), nullable=False, default="unknown")
    enabled = Column(Boolean, nullable=False, default=True)
    revision = Column(Integer, nullable=False, default=1)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "revision > 0",
            name="ck_llm_model_deployments_revision",
        ),
        UniqueConstraint(
            "connection_id",
            "wire_model_id",
            name="uq_llm_model_deployments_connection_wire_model",
        ),
        Index(
            "ix_llm_model_deployments_connection_enabled",
            "connection_id",
            "enabled",
        ),
    )


class LLMDeploymentRoute(Base):
    """Registered adapter and protocol route for one model deployment."""

    __tablename__ = "llm_deployment_routes"

    id = Column(GUID(), primary_key=True)
    deployment_id = Column(
        GUID(),
        ForeignKey("llm_model_deployments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    adapter_id = Column(String(100), nullable=False)
    adapter_version = Column(String(50), nullable=False)
    api_surface = Column(String(100), nullable=False)
    dialect_policy_id = Column(String(100), nullable=False)
    billing_provider_id = Column(String(100), nullable=True)
    route_config = Column(JSON, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "deployment_id",
            "adapter_id",
            "api_surface",
            "dialect_policy_id",
            name="uq_llm_deployment_routes_protocol",
        ),
        Index(
            "ix_llm_deployment_routes_deployment_enabled",
            "deployment_id",
            "enabled",
        ),
    )


class LLMCapabilityObservation(Base):
    """Revisioned capability evidence observed for a deployment route."""

    __tablename__ = "llm_capability_observations"

    id = Column(GUID(), primary_key=True)
    deployment_id = Column(
        GUID(),
        ForeignKey("llm_model_deployments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    route_id = Column(
        GUID(),
        ForeignKey("llm_deployment_routes.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    capability = Column(String(100), nullable=False)
    support_state = Column(String(32), nullable=False, default="unknown")
    constraints = Column(JSON, nullable=True)
    source = Column(String(100), nullable=False)
    observed_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    expires_at = Column(DateTime(timezone=True), nullable=True)
    revision = Column(Integer, nullable=False, default=1)
    fingerprint = Column(String(128), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "support_state IN ('supported', 'unsupported', 'unknown')",
            name="ck_llm_capability_observations_support_state",
        ),
        CheckConstraint(
            "revision > 0",
            name="ck_llm_capability_observations_revision",
        ),
        UniqueConstraint(
            "deployment_id",
            "route_id",
            "capability",
            "revision",
            name="uq_llm_capability_observations_revision",
        ),
        Index(
            "ix_llm_capability_observations_lookup",
            "deployment_id",
            "capability",
            "observed_at",
        ),
    )


class UserLLMSelection(Base):
    """Selected conversation provider and model for a user."""

    __tablename__ = "user_llm_selections"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String(50), nullable=False, default="openai")
    model = Column(String(100), nullable=False)
    deployment_id = Column(
        GUID(),
        ForeignKey("llm_model_deployments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
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
    deployment_id = Column(
        GUID(),
        ForeignKey("llm_model_deployments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
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
    gate_deployment_id = Column(
        GUID(),
        ForeignKey("llm_model_deployments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    extraction_deployment_id = Column(
        GUID(),
        ForeignKey("llm_model_deployments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
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
    """Task conversation continuity with optional deployment identity."""

    __tablename__ = "llm_conversations"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider = Column(String(50), default="openai", nullable=False)
    model = Column(String(100), nullable=True)
    connection_id = Column(
        GUID(),
        ForeignKey("llm_inference_connections.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    deployment_id = Column(
        GUID(),
        ForeignKey("llm_model_deployments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    route_id = Column(
        GUID(),
        ForeignKey("llm_deployment_routes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    origin_revision = Column(Integer, nullable=True)
    origin_deployment_revision = Column(Integer, nullable=True)
    remote_resource_id = Column(String(256), nullable=True, index=True)
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
    connection_id = Column(
        GUID(),
        ForeignKey("llm_inference_connections.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    deployment_id = Column(
        GUID(),
        ForeignKey("llm_model_deployments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    route_id = Column(
        GUID(),
        ForeignKey("llm_deployment_routes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
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
