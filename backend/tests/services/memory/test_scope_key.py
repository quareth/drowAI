"""Unit tests for deterministic semantic memory scope-key formatting."""

from __future__ import annotations

import hashlib

import pytest

from backend.services.memory.memory_models import MemoryTier
from backend.services.memory.memory_store import _compute_scope_key


def test_user_profile_scope_key_format() -> None:
    key = _compute_scope_key(
        MemoryTier.USER_PROFILE,
        user_id=7,
        tenant_id=None,
        engagement_id=None,
        task_id=None,
        content_hash="abc",
    )
    assert key == "up:7:abc"


def test_task_engagement_scope_key_format() -> None:
    key = _compute_scope_key(
        MemoryTier.TASK_ENGAGEMENT,
        user_id=7,
        tenant_id=3,
        engagement_id=99,
        task_id=None,
        content_hash="abc",
    )
    assert key == "te:3:eng:99:abc"


def test_scope_key_deterministic() -> None:
    first = _compute_scope_key(
        MemoryTier.USER_PROFILE,
        user_id=7,
        tenant_id=None,
        engagement_id=None,
        task_id=None,
        content_hash="same",
    )
    second = _compute_scope_key(
        MemoryTier.USER_PROFILE,
        user_id=7,
        tenant_id=None,
        engagement_id=None,
        task_id=None,
        content_hash="same",
    )
    assert first == second


def test_scope_key_different_content_different_key() -> None:
    hash_a = hashlib.sha256(b"alpha").hexdigest()
    hash_b = hashlib.sha256(b"bravo").hexdigest()
    first = _compute_scope_key(
        MemoryTier.USER_PROFILE,
        user_id=7,
        tenant_id=None,
        engagement_id=None,
        task_id=None,
        content_hash=hash_a,
    )
    second = _compute_scope_key(
        MemoryTier.USER_PROFILE,
        user_id=7,
        tenant_id=None,
        engagement_id=None,
        task_id=None,
        content_hash=hash_b,
    )
    assert first != second


def test_scope_key_different_scope_different_key() -> None:
    content_hash = hashlib.sha256(b"shared").hexdigest()
    user_key = _compute_scope_key(
        MemoryTier.USER_PROFILE,
        user_id=7,
        tenant_id=None,
        engagement_id=None,
        task_id=None,
        content_hash=content_hash,
    )
    engagement_key = _compute_scope_key(
        MemoryTier.TASK_ENGAGEMENT,
        user_id=7,
        tenant_id=3,
        engagement_id=101,
        task_id=None,
        content_hash=content_hash,
    )
    assert user_key != engagement_key


def test_scope_key_requires_engagement_for_task_tier() -> None:
    with pytest.raises(ValueError, match="engagement_id or task_id is required"):
        _compute_scope_key(
            MemoryTier.TASK_ENGAGEMENT,
            user_id=7,
            tenant_id=3,
            engagement_id=None,
            task_id=None,
            content_hash="abc",
        )
