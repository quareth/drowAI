"""Pure-function tests for memory retrieval summary rendering."""

from __future__ import annotations

from datetime import datetime, timezone

from agent.graph.nodes.memory_retrieval import _render_memory_summary
from backend.services.memory.memory_models import MemorySearchResult, MemoryTier


def _result(content: str, tier: MemoryTier) -> MemorySearchResult:
    return MemorySearchResult(
        id=f"id-{content[:8]}",
        content=content,
        memory_tier=tier,
        similarity_score=0.9,
        created_at=datetime.now(timezone.utc),
    )


def test_render_empty_results() -> None:
    rendered = _render_memory_summary([], [])
    assert rendered == ""


def test_render_user_profile_only() -> None:
    rendered = _render_memory_summary(
        [_result("Prefers concise responses.", MemoryTier.USER_PROFILE)],
        [],
    )
    assert "User Preferences:" in rendered
    assert "Prefers concise responses." in rendered
    assert "Engagement Context:" not in rendered


def test_render_engagement_only() -> None:
    rendered = _render_memory_summary(
        [],
        [_result("Current task is SSH hardening.", MemoryTier.TASK_ENGAGEMENT)],
    )
    assert "Engagement Context:" in rendered
    assert "Current task is SSH hardening." in rendered
    assert "User Preferences:" not in rendered


def test_render_both_tiers() -> None:
    rendered = _render_memory_summary(
        [_result("Uses nmap first.", MemoryTier.USER_PROFILE)],
        [_result("Engagement focuses on firewall rules.", MemoryTier.TASK_ENGAGEMENT)],
    )
    assert "User Preferences:" in rendered
    assert "Engagement Context:" in rendered
    assert "\n\nEngagement Context:" in rendered


def test_render_truncates_at_max_chars() -> None:
    rendered = _render_memory_summary(
        [_result("A" * 200, MemoryTier.USER_PROFILE)],
        [],
        max_chars=40,
    )
    assert len(rendered) == 40
    assert rendered.endswith("...")
