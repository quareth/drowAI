"""Validate semantic memory Pydantic data-contract behavior."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.services.memory.memory_models import (
    MemoryCreateRequest,
    MemorySearchFilters,
    MemorySearchResult,
    MemoryTier,
)


def test_memory_tier_values() -> None:
    assert [tier.value for tier in MemoryTier] == ["user_profile", "task_engagement"]


def test_create_request_valid_user_profile() -> None:
    request = MemoryCreateRequest(
        content="Stable identity trait",
        memory_tier=MemoryTier.USER_PROFILE,
        user_id=11,
    )
    assert request.engagement_id is None


def test_create_request_task_engagement_requires_tenant_scope() -> None:
    with pytest.raises(ValidationError):
        MemoryCreateRequest(
            content="Engagement scoped note",
            memory_tier=MemoryTier.TASK_ENGAGEMENT,
            user_id=11,
            engagement_id=22,
        )


def test_create_request_user_profile_rejects_engagement_id() -> None:
    with pytest.raises(ValidationError):
        MemoryCreateRequest(
            content="User profile should not carry engagement scope",
            memory_tier=MemoryTier.USER_PROFILE,
            user_id=11,
            engagement_id=22,
        )


def test_create_request_user_profile_rejects_tenant_id() -> None:
    with pytest.raises(ValidationError):
        MemoryCreateRequest(
            content="User profile should stay private",
            memory_tier=MemoryTier.USER_PROFILE,
            user_id=11,
            tenant_id=2,
        )


def test_search_filters_default_max_results() -> None:
    filters = MemorySearchFilters(user_id=11)
    assert filters.max_results == 5


@pytest.mark.parametrize("invalid", [0, 21])
def test_search_filters_rejects_out_of_range(invalid: int) -> None:
    with pytest.raises(ValidationError):
        MemorySearchFilters(user_id=11, max_results=invalid)


def test_search_filters_task_engagement_requires_tenant_and_parent() -> None:
    with pytest.raises(ValidationError):
        MemorySearchFilters(memory_tier=MemoryTier.TASK_ENGAGEMENT, tenant_id=3)


def test_search_result_from_attributes() -> None:
    class _Row:
        id = "abc"
        content = "A memory"
        memory_tier = MemoryTier.USER_PROFILE
        similarity_score = 0.8
        created_at = datetime(2026, 3, 23, tzinfo=timezone.utc)
        metadata = {"source": "unit-test"}

    result = MemorySearchResult.model_validate(_Row())
    assert result.id == "abc"
    assert result.memory_tier == MemoryTier.USER_PROFILE
    assert result.metadata == {"source": "unit-test"}
