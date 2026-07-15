"""Pure formatting helpers for long-term memory retrieval summaries."""

from __future__ import annotations

import math
from typing import Any, Sequence


def split_retrieval_limits(total_results: int) -> tuple[int, int]:
    """Split total retrieval budget across user-profile and engagement tiers."""

    total = max(0, int(total_results))
    if total == 0:
        return 0, 0
    user_profile_max = int(math.ceil(total * 0.6))
    task_engagement_max = max(0, total - user_profile_max)
    if task_engagement_max == 0:
        task_engagement_max = 1
        user_profile_max = max(0, total - task_engagement_max)
    return user_profile_max, task_engagement_max


def render_memory_summary(
    user_profile_results: Sequence[Any],
    engagement_results: Sequence[Any],
    *,
    max_chars: int,
) -> str:
    """Render retrieved memory records into the bounded graph summary string."""

    if not user_profile_results and not engagement_results:
        return ""

    sections: list[str] = []
    if user_profile_results:
        user_items = [f"- {result.content}" for result in user_profile_results]
        sections.append("User Preferences:\n" + "\n".join(user_items))

    if engagement_results:
        engagement_items = [f"- {result.content}" for result in engagement_results]
        sections.append("Engagement Context:\n" + "\n".join(engagement_items))

    rendered = "\n\n".join(sections)
    if len(rendered) > max_chars:
        if max_chars <= 3:
            return "." * max(0, max_chars)
        rendered = rendered[: max_chars - 3] + "..."
    return rendered


__all__ = ["render_memory_summary", "split_retrieval_limits"]
