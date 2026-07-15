#!/usr/bin/env python3
"""Backfill canonical chat_turn_events rows from legacy message detail payloads.

This script migrates existing assistant `ChatMessage` rows that predate canonical
`chat_turn_events` persistence. It derives deterministic tool/observation order
from `ToolCall` rows and `observation_tokens`, marks each generated event as
backfilled in metadata, and skips assistant messages that are already canonical.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

# Ensure backend package imports when executed as a script.
if __name__ == "__main__" and __package__ is None:
    import os

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if root not in sys.path:
        sys.path.insert(0, root)

from backend.database import SessionLocal
from backend.models.chat import ChatMessage, ChatTurnEvent, ToolCall
from backend.services.chat.observation_sections import parse_observation_sections

logger = logging.getLogger(__name__)

_ASSISTANT_MESSAGE_TYPES = {"assistant", "assistant_message"}
_PHASE_FALLBACK_BASE = 1_000_000


@dataclass
class BackfillStats:
    """Aggregate migration counters for one task."""

    task_id: int
    scanned_messages: int = 0
    skipped_canonical_messages: int = 0
    skipped_empty_messages: int = 0
    migrated_messages: int = 0
    migrated_rows: int = 0


def _coerce_non_negative_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if value.is_integer() and value >= 0:
            return int(value)
        return None
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        try:
            parsed = int(candidate)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except Exception:
        return str(value)


def _normalize_observation_section(*, section: Any, fallback_sub_turn_index: int) -> Optional[Dict[str, Any]]:
    sub_turn_index: Optional[int] = None
    content_value: Any = section
    if isinstance(section, dict):
        content_value = section.get("content", "")
        sub_turn_index = _coerce_non_negative_int(section.get("sub_turn_index"))
    content = _to_text(content_value).strip()
    if not content:
        return None
    if sub_turn_index is None:
        sub_turn_index = fallback_sub_turn_index
    return {
        "sub_turn_index": sub_turn_index,
        "content": content,
        "metadata": {
            "ind": 1,
            "sub_turn_index": sub_turn_index,
            "backfilled": True,
            "backfill_source": "observation_tokens",
            "backfill_strategy": "legacy_detail_interleave_v1",
        },
    }


def _map_tool_entries(tool_calls: Sequence[ToolCall]) -> List[Dict[str, Any]]:
    sorted_calls = sorted(
        list(tool_calls or []),
        key=lambda call: (
            int(getattr(call, "turn_index", 0) or 0),
            int(getattr(call, "id", 0) or 0),
        ),
    )
    entries: List[Dict[str, Any]] = []
    for index, tool_call in enumerate(sorted_calls):
        sub_turn_index = _coerce_non_negative_int(getattr(tool_call, "turn_index", None))
        if sub_turn_index is None:
            sub_turn_index = index
        entries.append(
            {
                "sub_turn_index": sub_turn_index,
                "tool_call_id": str(getattr(tool_call, "tool_call_id", "") or "") or None,
                "content": _to_text(getattr(tool_call, "tool_result", None)),
                "metadata": {
                    "ind": 1,
                    "status": "success",
                    "sub_turn_index": sub_turn_index,
                    "chat_message_id": getattr(tool_call, "chat_message_id", None),
                    "tool_call_id": getattr(tool_call, "tool_call_id", None),
                    "tool_name": getattr(tool_call, "tool_name", None),
                    "tool_arguments": getattr(tool_call, "tool_arguments", None),
                    "parent_tool_call_id": getattr(tool_call, "parent_tool_call_id", None),
                    "backfilled": True,
                    "backfill_source": "tool_calls",
                    "backfill_strategy": "legacy_detail_interleave_v1",
                },
            }
        )
    return entries


def _map_observation_entries(observation_tokens: Optional[str]) -> List[Dict[str, Any]]:
    if not isinstance(observation_tokens, str) or not observation_tokens.strip():
        return []
    entries: List[Dict[str, Any]] = []
    for index, section in enumerate(
        parse_observation_sections(
            observation_tokens,
            non_list_strategy="dict_or_raw",
        )
    ):
        normalized = _normalize_observation_section(
            section=section,
            fallback_sub_turn_index=index,
        )
        if normalized is None:
            continue
        entries.append(normalized)
    return entries


def _min_non_negative_sub_turn_index(entries: Sequence[Dict[str, Any]]) -> Optional[int]:
    candidates = [
        _coerce_non_negative_int(entry.get("sub_turn_index"))
        for entry in entries
    ]
    normalized = [candidate for candidate in candidates if candidate is not None]
    if not normalized:
        return None
    return min(normalized)


def _normalized_family_sub_turn(*, raw_sub_turn: Any, family_min_sub_turn: Optional[int]) -> Optional[int]:
    normalized = _coerce_non_negative_int(raw_sub_turn)
    if normalized is None:
        return None
    if family_min_sub_turn is None:
        return normalized
    adjusted = normalized - family_min_sub_turn
    return adjusted if adjusted >= 0 else normalized


def _phase_sort_index(sub_turn_index: Any, *, fallback_position: int) -> int:
    parsed = _coerce_non_negative_int(sub_turn_index)
    if parsed is not None:
        return parsed
    return _PHASE_FALLBACK_BASE + max(0, int(fallback_position))


def _build_ordered_events(message: ChatMessage) -> List[Dict[str, Any]]:
    tool_entries = _map_tool_entries(getattr(message, "tool_calls", []) or [])
    observation_entries = _map_observation_entries(getattr(message, "observation_tokens", None))
    if not tool_entries and not observation_entries:
        return []

    ordered_entries: List[tuple[int, int, int, str, Dict[str, Any]]] = []
    tool_min_sub_turn = _min_non_negative_sub_turn_index(tool_entries)
    observation_min_sub_turn = _min_non_negative_sub_turn_index(observation_entries)

    for position, entry in enumerate(tool_entries):
        normalized_sub_turn = _normalized_family_sub_turn(
            raw_sub_turn=entry.get("sub_turn_index"),
            family_min_sub_turn=tool_min_sub_turn,
        )
        sort_index = _phase_sort_index(normalized_sub_turn, fallback_position=position)
        ordered_entries.append((sort_index, position, 0, "tool", entry))

    for position, entry in enumerate(observation_entries):
        normalized_sub_turn = _normalized_family_sub_turn(
            raw_sub_turn=entry.get("sub_turn_index"),
            family_min_sub_turn=observation_min_sub_turn,
        )
        sort_index = _phase_sort_index(normalized_sub_turn, fallback_position=position)
        ordered_entries.append((sort_index, position, 1, "observation", entry))

    ordered_entries.sort(key=lambda value: (value[0], value[1], value[2]))

    events: List[Dict[str, Any]] = []
    for phase_sequence, (_, _, _, kind, entry) in enumerate(ordered_entries):
        events.append(
            {
                "phase_sequence": phase_sequence,
                "kind": kind,
                "sub_turn_index": _coerce_non_negative_int(entry.get("sub_turn_index")),
                "tool_call_id": entry.get("tool_call_id"),
                "content": _to_text(entry.get("content")),
                "event_metadata": dict(entry.get("metadata") or {}),
            }
        )
    return events


def backfill_chat_turn_events_for_task(db: Session, task_id: int, *, dry_run: bool) -> BackfillStats:
    """Backfill one task's assistant messages with canonical chat_turn_events rows."""
    stats = BackfillStats(task_id=task_id)
    messages = (
        db.query(ChatMessage)
        .options(selectinload(ChatMessage.tool_calls))
        .filter(
            ChatMessage.task_id == task_id,
            func.lower(ChatMessage.message_type).in_(_ASSISTANT_MESSAGE_TYPES),
        )
        .order_by(
            ChatMessage.conversation_id.asc(),
            func.coalesce(ChatMessage.turn_number, ChatMessage.id).asc(),
            ChatMessage.id.asc(),
        )
        .all()
    )
    if not messages:
        return stats

    existing_message_ids = {
        int(message_id)
        for (message_id,) in db.query(ChatTurnEvent.chat_message_id)
        .filter(ChatTurnEvent.task_id == task_id)
        .distinct()
        .all()
        if message_id is not None
    }

    for message in messages:
        stats.scanned_messages += 1
        message_id = int(getattr(message, "id", 0) or 0)
        if message_id <= 0:
            stats.skipped_empty_messages += 1
            continue
        if message_id in existing_message_ids:
            stats.skipped_canonical_messages += 1
            continue

        events = _build_ordered_events(message)
        if not events:
            stats.skipped_empty_messages += 1
            continue

        turn_number = int(getattr(message, "turn_number", 0) or message_id)
        conversation_id = str(getattr(message, "conversation_id", "") or "")
        for event in events:
            db.add(
                ChatTurnEvent(
                    task_id=task_id,
                    tenant_id=int(getattr(message, "tenant_id", 1) or 1),
                    conversation_id=conversation_id,
                    chat_message_id=message_id,
                    turn_number=turn_number,
                    phase_sequence=int(event["phase_sequence"]),
                    kind=str(event["kind"]),
                    sub_turn_index=event.get("sub_turn_index"),
                    tool_call_id=event.get("tool_call_id"),
                    content=str(event.get("content", "") or ""),
                    event_metadata=event.get("event_metadata"),
                )
            )
        stats.migrated_messages += 1
        stats.migrated_rows += len(events)

    if dry_run:
        db.rollback()
    else:
        db.commit()
    return stats


def _discover_task_ids(db: Session, requested_task_id: Optional[int]) -> List[int]:
    if requested_task_id is not None:
        return [requested_task_id]
    rows = (
        db.query(ChatMessage.task_id)
        .filter(func.lower(ChatMessage.message_type).in_(_ASSISTANT_MESSAGE_TYPES))
        .distinct()
        .order_by(ChatMessage.task_id.asc())
        .all()
    )
    return [int(task_id) for (task_id,) in rows if task_id is not None]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill canonical chat_turn_events from legacy ToolCall and "
            "observation_tokens payloads."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute backfill counts without writing database changes.",
    )
    parser.add_argument(
        "--task-id",
        type=int,
        default=None,
        metavar="ID",
        help="Backfill only one task id (default: process all tasks).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    db = SessionLocal()
    try:
        task_ids = _discover_task_ids(db, args.task_id)
        if not task_ids:
            logger.info("No assistant chat messages found; nothing to backfill.")
            return 0

        total_scanned = 0
        total_skipped_canonical = 0
        total_skipped_empty = 0
        total_migrated_messages = 0
        total_migrated_rows = 0

        for task_id in task_ids:
            logger.info("Backfilling chat_turn_events for task %s...", task_id)
            stats = backfill_chat_turn_events_for_task(db, task_id, dry_run=args.dry_run)
            total_scanned += stats.scanned_messages
            total_skipped_canonical += stats.skipped_canonical_messages
            total_skipped_empty += stats.skipped_empty_messages
            total_migrated_messages += stats.migrated_messages
            total_migrated_rows += stats.migrated_rows
            logger.info(
                (
                    "Task %s results: scanned=%s skipped_canonical=%s "
                    "skipped_empty=%s migrated_messages=%s migrated_rows=%s"
                ),
                stats.task_id,
                stats.scanned_messages,
                stats.skipped_canonical_messages,
                stats.skipped_empty_messages,
                stats.migrated_messages,
                stats.migrated_rows,
            )

        if args.dry_run:
            logger.info(
                (
                    "DRY RUN complete: scanned=%s skipped_canonical=%s "
                    "skipped_empty=%s migrated_messages=%s migrated_rows=%s"
                ),
                total_scanned,
                total_skipped_canonical,
                total_skipped_empty,
                total_migrated_messages,
                total_migrated_rows,
            )
        else:
            logger.info(
                (
                    "Backfill complete: scanned=%s skipped_canonical=%s "
                    "skipped_empty=%s migrated_messages=%s migrated_rows=%s"
                ),
                total_scanned,
                total_skipped_canonical,
                total_skipped_empty,
                total_migrated_messages,
                total_migrated_rows,
            )
        return 0
    except Exception:
        db.rollback()
        logger.exception("Backfill chat_turn_events failed.")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
