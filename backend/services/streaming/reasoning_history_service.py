"""Reasoning history read-model orchestration.

This service builds compatibility history pages from the active persisted stream
event store, the legacy reasoning DB store, or the file-backed log fallback
without moving response shaping into persistence classes.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from backend.config import REASONING_DB_PERSIST, REASONING_DB_STREAM
from .log_watcher import AgentLogWatcher, read_reasoning_log_entries
from .reasoning_store import AgentReasoningStore
from .event_store import StreamEventStore
from .stream_event_schema import normalize_stream_packet

logger = logging.getLogger("backend.services.agent_reasoning_history_service")


class AgentReasoningHistoryService:
    """Build reasoning history pages from stream DB, legacy DB, or file fallback."""

    def __init__(self, db: Session, *, log_watcher: AgentLogWatcher | None = None) -> None:
        self._db = db
        self._log_watcher = log_watcher

    def get_replay_history(
        self,
        task_id: int,
        *,
        after: int,
        limit: int,
    ) -> dict[str, Any]:
        """Return unfiltered persisted stream packets for authorized replay clients."""
        page = StreamEventStore(self._db).list_bootstrap_page(
            task_id=task_id,
            after=after,
            limit=limit,
        )
        items = [row.payload for row in page.rows if isinstance(row.payload, dict)]
        return {
            "items": items,
            "nextAfter": page.next_after,
            "hasMore": page.has_more,
        }

    def get_history(
        self,
        task_id: int,
        *,
        after: int | None,
        before: int | None,
        limit: int,
        order: str,
    ) -> dict[str, Any]:
        """Return a compatibility reasoning history page."""
        try:
            stream_store = StreamEventStore(self._db)
            if stream_store.has_events(task_id):
                return self._get_stream_event_history(
                    stream_store,
                    task_id,
                    after=after,
                    before=before,
                    limit=limit,
                    order=order,
                )
        except Exception:
            logger.debug("Stream event history unavailable; falling back", exc_info=True)

        if REASONING_DB_STREAM or REASONING_DB_PERSIST:
            try:
                return self._get_legacy_db_history(
                    task_id,
                    after=after,
                    before=before,
                    limit=limit,
                    order=order,
                )
            except Exception:
                logger.warning("DB history unavailable; falling back to file", exc_info=True)

        return self._get_file_history(
            task_id,
            after=after,
            before=before,
            limit=limit,
            order=order,
        )

    def _get_stream_event_history(
        self,
        store: StreamEventStore,
        task_id: int,
        *,
        after: int | None,
        before: int | None,
        limit: int,
        order: str,
    ) -> dict[str, Any]:
        page_limit = limit + 1
        if after is not None:
            rows = store.list_after(task_id, after, page_limit)
        elif before is not None:
            rows = store.list_before(task_id, before, page_limit)
        else:
            rows = store.list_after(task_id, 0, page_limit)

        has_more = len(rows) > limit
        if has_more:
            if before is not None:
                rows = rows[1:]
            else:
                rows = rows[:limit]

        items = [row.payload for row in rows if isinstance(row.payload, dict)]
        items = self._filter_history_items(items)
        if order == "desc":
            items = list(reversed(items))

        if before is not None:
            next_before = rows[0].sequence if rows else before
            return {"items": items, "nextBefore": next_before, "hasMore": has_more}

        next_after = rows[-1].sequence if rows else (after or 0)
        return {"items": items, "nextAfter": next_after, "hasMore": has_more}

    def _get_legacy_db_history(
        self,
        task_id: int,
        *,
        after: int | None,
        before: int | None,
        limit: int,
        order: str,
    ) -> dict[str, Any]:
        store = AgentReasoningStore(self._db)
        if after is not None:
            rows = store.list_after(task_id, after, limit)
            items_db = self._normalize_legacy_rows(rows, task_id)
            items = items_db if order == "asc" else list(reversed(items_db))
            next_after = items[-1]["sequence"] if items else (after or 0)
            has_more = len(items_db) >= limit
            return {"items": items, "nextAfter": next_after, "hasMore": has_more}

        if before is not None:
            rows = store.list_before(task_id, before, limit)
            items_db = self._normalize_legacy_rows(rows, task_id)
            items = items_db if order == "asc" else list(reversed(items_db))
            next_before = items[0]["sequence"] if items else before
            has_more = len(items_db) >= limit
            return {"items": items, "nextBefore": next_before, "hasMore": has_more}

        rows = store.list_after(task_id, 0, limit)
        items_db = self._normalize_legacy_rows(rows, task_id)
        items = items_db if order == "asc" else list(reversed(items_db))
        next_after = items[-1]["sequence"] if items else 0
        has_more = len(items_db) >= limit
        return {"items": items, "nextAfter": next_after, "hasMore": has_more}

    def _get_file_history(
        self,
        task_id: int,
        *,
        after: int | None,
        before: int | None,
        limit: int,
        order: str,
    ) -> dict[str, Any]:
        items = self._normalize_file_items(task_id)
        if order == "desc":
            items = list(reversed(items))

        result: List[Dict[str, Any]] = []
        if after is not None:
            for item in items:
                if item.get("sequence", 0) > after:
                    result.append(item)
                    if len(result) >= limit:
                        break
            next_after = result[-1]["sequence"] if result else after
            has_more = any(it.get("sequence", 0) > next_after for it in items)
            return {
                "items": result if order == "asc" else list(reversed(result)),
                "nextAfter": next_after,
                "hasMore": has_more,
            }

        if before is not None:
            filtered = [it for it in items if it.get("sequence", 0) < before]
            if order == "asc":
                result = filtered[-limit:]
                has_more = len(filtered) > len(result)
            else:
                result = filtered[:limit]
                has_more = len(filtered) > len(result)
            next_before = result[0]["sequence"] if result else before
            return {
                "items": result if order == "asc" else list(reversed(result)),
                "nextBefore": next_before,
                "hasMore": has_more,
            }

        base = items if order == "asc" else list(reversed(items))
        result = base[:limit]
        next_after = result[-1]["sequence"] if result else 0
        has_more = len(items) > len(result)
        return {
            "items": result if order == "asc" else list(reversed(result)),
            "nextAfter": next_after,
            "hasMore": has_more,
        }

    def _normalize_legacy_rows(self, rows: List[Any], task_id: int) -> List[Dict[str, Any]]:
        """Normalize SystemLog-like rows into the packet schema."""
        normalized: List[Dict[str, Any]] = []
        for row in rows:
            meta = dict(getattr(row, "log_metadata", None) or {})
            conv_id = meta.get("conversation_id") or meta.get("conversationId") or ""
            if conv_id:
                meta.setdefault("conversation_id", conv_id)
                meta.setdefault("conversationId", conv_id)
            content = getattr(row, "content", None) or ""
            event = {
                "sequence": getattr(row, "sequence", 0),
                "type": getattr(row, "type", ""),
                "content": content,
                "metadata": meta,
                "timestamp": row.timestamp.isoformat() if getattr(row, "timestamp", None) else None,
            }
            packet = normalize_stream_packet(event, task_id=task_id) or event
            normalized.append(packet)
        return self._filter_history_items(normalized)

    def _normalize_file_items(self, task_id: int) -> List[Dict[str, Any]]:
        normalized_items: List[Dict[str, Any]] = []
        for entry in read_reasoning_log_entries(task_id):
            meta = dict(entry.get("metadata") or {})
            conv_id = meta.get("conversation_id") or meta.get("conversationId") or ""
            if conv_id:
                meta.setdefault("conversation_id", conv_id)
                meta.setdefault("conversationId", conv_id)
            payload = dict(entry)
            payload["metadata"] = meta
            packet = normalize_stream_packet(payload, task_id=task_id) or payload
            normalized_items.append(packet)
        return normalized_items

    def _filter_history_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter out chat noise while preserving the existing result set exactly."""
        excluded_types = {
            "user_message",
            "assistant_delta",
            "assistant_message",
            "assistant_final",
            "assistant_stream",
            "message_start",
            "message_delta",
            "message_section_end",
            "section_end",
            "status",
        }
        filtered: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            obj = item.get("obj") if isinstance(item.get("obj"), dict) else None
            event = obj if obj is not None else item
            event_type = str(event.get("type") or "").lower()
            if event_type in excluded_types:
                continue
            if event_type == "status" and str(event.get("content", "")).strip().lower() == "waiting_for_user":
                continue
            filtered.append(item)
        return filtered
