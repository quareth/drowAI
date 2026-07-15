"""Task notification event helpers for browser-facing stream updates.

Responsibility:
- Build small task-scoped notification events from backend side effects.
- Schedule those events on the runtime stream hub without coupling domain
  services to websocket mechanics.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from backend.core.time_utils import format_iso, utc_now

logger = logging.getLogger(__name__)


def build_task_notification_event(
    *,
    task_id: int,
    category: str,
    title: str,
    body: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a normalized stream status event for one task notification."""
    normalized_category = str(category or "").strip().lower()
    normalized_title = str(title or "").strip()
    normalized_body = str(body or "").strip()
    if not normalized_category or not normalized_title:
        return None

    return {
        "type": "status",
        "content": "task_notification",
        "metadata": {
            "task_id": int(task_id),
            "category": normalized_category,
            "title": normalized_title,
            "body": normalized_body,
            "created_at": format_iso(utc_now()),
            "streaming": False,
            **dict(metadata or {}),
        },
    }


def build_projection_notification_event(
    *,
    task_id: int,
    engagement_id: int | None,
    ingestion_run_id: str | None,
    source_execution_id: str | None,
    tool_name: str | None,
    asset_insert_count: int,
    finding_insert_count: int,
) -> dict[str, Any] | None:
    """Build a generic notification for newly inserted projected entities."""
    new_assets = max(0, int(asset_insert_count))
    new_findings = max(0, int(finding_insert_count))
    if new_assets == 0 and new_findings == 0:
        return None

    parts: list[str] = []
    if new_assets > 0:
        parts.append(f"{new_assets} new asset{'' if new_assets == 1 else 's'}")
    if new_findings > 0:
        parts.append(f"{new_findings} new finding{'' if new_findings == 1 else 's'}")

    return build_task_notification_event(
        task_id=task_id,
        category="knowledge_delta",
        title="New task intelligence",
        body=" · ".join(parts),
        metadata={
            "engagement_id": int(engagement_id) if engagement_id is not None else None,
            "ingestion_run_id": str(ingestion_run_id) if ingestion_run_id else None,
            "source_execution_id": str(source_execution_id) if source_execution_id else None,
            "tool_name": str(tool_name) if tool_name else None,
            "asset_insert_count": new_assets,
            "finding_insert_count": new_findings,
        },
    )


def schedule_task_notification_event(
    *,
    task_id: int,
    event: Mapping[str, Any] | None,
    publish_loop: asyncio.AbstractEventLoop | None,
) -> None:
    """Publish one task notification from a background worker."""
    if publish_loop is None or event is None:
        return

    try:
        from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub
    except Exception:
        logger.debug("Notification stream hub unavailable", exc_info=True)
        return

    async def publish() -> None:
        try:
            await get_in_memory_stream_hub().publish(task_id, dict(event))
        except Exception:
            logger.debug("Notification publish failed for task %s", task_id, exc_info=True)

    publish_loop.call_soon_threadsafe(asyncio.create_task, publish())


def schedule_projection_notification_from_result(
    *,
    task_id: int,
    ingestion_result: Mapping[str, Any],
    tool_name: str | None,
    publish_loop: asyncio.AbstractEventLoop | None,
) -> None:
    """Create and publish a task notification from an ingestion result."""
    if not bool(ingestion_result.get("ok")):
        return
    event = build_projection_notification_event(
        task_id=task_id,
        engagement_id=_coerce_optional_int(ingestion_result.get("engagement_id")),
        ingestion_run_id=_coerce_optional_str(ingestion_result.get("ingestion_run_id")),
        source_execution_id=_coerce_optional_str(ingestion_result.get("source_execution_id")),
        tool_name=tool_name,
        asset_insert_count=_coerce_nonnegative_int(ingestion_result.get("asset_insert_count")),
        finding_insert_count=_coerce_nonnegative_int(ingestion_result.get("finding_insert_count")),
    )
    schedule_task_notification_event(task_id=task_id, event=event, publish_loop=publish_loop)


def _coerce_optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed


def _coerce_nonnegative_int(value: Any) -> int:
    parsed = _coerce_optional_int(value)
    return max(0, int(parsed or 0))


def _coerce_optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None
