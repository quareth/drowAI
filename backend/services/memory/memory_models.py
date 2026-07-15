"""Define semantic memory data contracts for service boundaries.

This module owns request/filter/result models and tier enums for memory
operations. It is intentionally database-agnostic and contains no ORM/query
logic.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MemoryTier(str, Enum):
    """Supported semantic memory tiers."""

    USER_PROFILE = "user_profile"
    TASK_ENGAGEMENT = "task_engagement"


class MemoryCreateRequest(BaseModel):
    """Input contract for creating one semantic memory row."""

    content: str
    memory_tier: MemoryTier
    user_id: int
    tenant_id: int | None = None
    engagement_id: int | None = None
    task_id: int | None = None
    source_type: str = "chat_extraction"
    conversation_id: str | None = None
    source_turn_id: str | None = None
    metadata: dict | None = None

    @model_validator(mode="after")
    def _validate_engagement_scope(self) -> "MemoryCreateRequest":
        if self.memory_tier == MemoryTier.TASK_ENGAGEMENT:
            if self.tenant_id is None:
                raise ValueError("tenant_id is required for task_engagement memories")
            if self.engagement_id is None and self.task_id is None:
                raise ValueError(
                    "engagement_id or task_id is required for task_engagement memories"
                )
        if self.memory_tier == MemoryTier.USER_PROFILE:
            if self.engagement_id is not None:
                raise ValueError("engagement_id must be omitted for user_profile memories")
            if self.tenant_id is not None:
                raise ValueError("tenant_id must be omitted for user_profile memories")
        return self


class MemorySearchFilters(BaseModel):
    """Input contract for semantic memory retrieval filters."""

    user_id: int | None = None
    tenant_id: int | None = None
    memory_tier: MemoryTier | None = None
    engagement_id: int | None = None
    task_id: int | None = None
    max_results: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def _validate_scope_filters(self) -> "MemorySearchFilters":
        if self.memory_tier == MemoryTier.USER_PROFILE:
            if self.user_id is None:
                raise ValueError("user_id is required for user_profile memory queries")
            if self.tenant_id is not None:
                raise ValueError("tenant_id must be omitted for user_profile memory queries")
            if self.engagement_id is not None or self.task_id is not None:
                raise ValueError(
                    "engagement_id/task_id must be omitted for user_profile memory queries"
                )
        if self.memory_tier == MemoryTier.TASK_ENGAGEMENT:
            if self.tenant_id is None:
                raise ValueError("tenant_id is required for task_engagement memory queries")
            if self.engagement_id is None and self.task_id is None:
                raise ValueError(
                    "engagement_id or task_id is required for task_engagement memory queries"
                )
        if self.memory_tier is None and self.user_id is None and self.tenant_id is None:
            raise ValueError("user_id or tenant_id is required for memory queries")
        return self


class MemorySearchResult(BaseModel):
    """Output contract returned by memory store operations."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    content: str
    memory_tier: MemoryTier
    similarity_score: float
    created_at: datetime
    metadata: dict | None = None
    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_vector_family: str = "openai:text-embedding-3-small:1536"
