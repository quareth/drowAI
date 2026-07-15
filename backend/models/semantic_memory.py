"""Semantic memory ORM model definitions.

This module defines durable long-term memory rows with explicit ownership
boundaries: `user_profile` rows are user-private, while `task_engagement` rows
are tenant-owned and must carry tenant plus parent scope.
"""

import uuid as uuid_lib

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from backend.database import Base, GUID


class SemanticMemory(Base):
    """Durable semantic memory record with tier-specific ownership boundaries."""

    __tablename__ = "semantic_memories"

    id = Column(GUID(), primary_key=True, default=uuid_lib.uuid4)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    engagement_id = Column(Integer, ForeignKey("engagements.id"), nullable=True)
    task_id = Column(Integer, nullable=True)
    memory_tier = Column(String(32), nullable=False)
    content = Column(Text, nullable=False)
    scope_key = Column(String(512), nullable=False)
    content_hash = Column(String(64), nullable=False)
    embedding = Column(Vector(1536), nullable=False)
    embedding_provider = Column(
        String(50), nullable=False, server_default="openai"
    )
    embedding_model = Column(
        String(100), nullable=False, server_default="text-embedding-3-small"
    )
    embedding_dimensions = Column(Integer, nullable=False, server_default="1536")
    embedding_vector_family = Column(
        String(255),
        nullable=False,
        server_default="openai:text-embedding-3-small:1536",
    )
    source_type = Column(String(32), nullable=False)
    conversation_id = Column(String(255), nullable=True)
    source_turn_id = Column(String(255), nullable=True)
    memory_metadata = Column("metadata", JSON, nullable=True)
    last_accessed_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    access_count = Column(Integer, server_default="0", nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    engagement = relationship("Engagement", back_populates="semantic_memories")

    __table_args__ = (
        CheckConstraint(
            "("
            "memory_tier != 'task_engagement' "
            "OR (tenant_id IS NOT NULL AND (engagement_id IS NOT NULL OR task_id IS NOT NULL))"
            ")",
            name="ck_semantic_memories_task_engagement_tenant_scope",
        ),
        CheckConstraint(
            "("
            "memory_tier != 'user_profile' "
            "OR (tenant_id IS NULL AND engagement_id IS NULL)"
            ")",
            name="ck_semantic_memories_user_profile_private_scope",
        ),
        UniqueConstraint(
            "scope_key",
            "embedding_provider",
            "embedding_model",
            "embedding_dimensions",
            "embedding_vector_family",
            name="ux_semantic_memories_scope_key_identity",
        ),
        Index("ix_semantic_memories_user_tier", "user_id", "memory_tier"),
        Index(
            "ix_semantic_memories_embedding_identity",
            "user_id",
            "memory_tier",
            "embedding_provider",
            "embedding_model",
            "embedding_dimensions",
        ),
        Index(
            "ix_semantic_memories_tenant_scope",
            "tenant_id",
            "memory_tier",
            "engagement_id",
            "task_id",
            postgresql_where=text("tenant_id IS NOT NULL"),
        ),
        Index(
            "ix_semantic_memories_user_engagement",
            "user_id",
            "engagement_id",
            postgresql_where=text("engagement_id IS NOT NULL"),
        ),
        Index("ix_semantic_memories_user_created", "user_id", "created_at"),
    )
