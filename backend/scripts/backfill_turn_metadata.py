#!/usr/bin/env python3
"""Backfill turn metadata for existing AgentLog events.

Populates turn_id and turn_number for events that were created before the
turn-based model. Turn boundaries are detected by:
- Group by conversation_id (or 'legacy' when missing)
- New turn on user_message (leading non-user events are skipped with warning)
- End turn on assistant_final
- Split on sequence gap > 10 from previous event
- First event per turn must be user_message (validated post-update)

Usage:
  python -m backend.scripts.backfill_turn_metadata [--dry-run] [--task-id TASK_ID]

  --dry-run: Log what would be updated without committing.
  --task-id: Backfill only the given task (default: all tasks with unbackfilled events).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

# Ensure backend is importable when run as script
if __name__ == "__main__" and __package__ is None:
    import os
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if root not in sys.path:
        sys.path.insert(0, root)

from backend.database import SessionLocal
from backend.models.chat import AgentLog

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Sequence gap above which we start a new turn
SEQUENCE_GAP_THRESHOLD = 10


def _conv_id(event: AgentLog) -> str:
    """Resolve conversation id from column or log_metadata; 'legacy' when missing."""
    cid = getattr(event, "conversation_id", None)
    if cid:
        return cid
    meta = event.log_metadata or {}
    return meta.get("conversation_id") or meta.get("conversationId") or "legacy"


def _is_assistant_final(event: AgentLog) -> bool:
    """True if event is assistant_final (type or metadata subtype)."""
    if event.type == "assistant_final":
        return True
    meta = event.log_metadata or {}
    return meta.get("subtype") == "assistant_final"


def _save_turn(
    events: List[AgentLog],
    task_id: int,
    conv_id: str,
    turn_num: int,
    dry_run: bool,
) -> None:
    """Assign turn_id and turn_number to events; commit is caller's responsibility."""
    turn_id = f"task-{task_id}-conv-{conv_id}-turn-{turn_num}"
    for e in events:
        e.turn_id = turn_id
        e.turn_number = turn_num
    logger.debug(
        "Task %s conv %s turn %s: %s events (seq %s..%s)",
        task_id,
        conv_id,
        turn_num,
        len(events),
        events[0].sequence if events else None,
        events[-1].sequence if events else None,
    )


def _validate_turn(db: Any, task_id: int, dry_run: bool) -> None:
    """Assert first event per turn is user_message and sequences have no gap > 10; log violations."""
    rows = (
        db.query(AgentLog.turn_id, AgentLog.sequence, AgentLog.type)
        .filter(AgentLog.task_id == task_id)
        .order_by(AgentLog.turn_id, AgentLog.sequence.asc())
        .all()
    )
    by_turn: Dict[str, List[Tuple[Optional[int], str]]] = defaultdict(list)
    for turn_id, seq, typ in rows:
        by_turn[turn_id].append((seq, typ))
    violations: List[str] = []
    for turn_id, seq_type_list in by_turn.items():
        if not seq_type_list:
            continue
        sorted_list = sorted(seq_type_list, key=lambda x: (x[0] is None, x[0] or 0))
        first_seq, first_type = sorted_list[0]
        if first_type != "user_message":
            violations.append(
                f"Turn {turn_id}: first event type is '{first_type}', expected 'user_message'"
            )
        prev_seq = None
        for seq, _ in sorted_list:
            if prev_seq is not None and seq is not None and (seq - prev_seq) > SEQUENCE_GAP_THRESHOLD:
                violations.append(
                    f"Turn {turn_id}: sequence gap {seq - prev_seq} > {SEQUENCE_GAP_THRESHOLD} (seq {prev_seq} -> {seq})"
                )
            prev_seq = seq
    if violations:
        for v in violations:
            logger.warning("[Validation] %s", v)
    elif not dry_run:
        logger.debug(
            "Task %s: validation passed (first event user_message, gaps <= %s)",
            task_id,
            SEQUENCE_GAP_THRESHOLD,
        )


def backfill_turn_metadata(
    db: Any,
    task_id: int,
    dry_run: bool = False,
) -> Tuple[int, int]:
    """Backfill turn_id and turn_number for events of one task that have turn_id IS NULL.

    Groups by conversation_id (or 'legacy'). Boundaries: new turn on user_message
    (skip leading non-user), end on assistant_final, split on sequence gap > 10.
    Commits per conversation group. Validates first event per turn is user_message.

    Returns:
        (events_updated_count, turns_created_count)
    """
    events = (
        db.query(AgentLog)
        .filter(
            AgentLog.task_id == task_id,
            AgentLog.turn_id.is_(None),
        )
        .order_by(AgentLog.sequence.asc())
        .all()
    )

    if not events:
        return 0, 0

    groups: Dict[str, List[AgentLog]] = defaultdict(list)
    for e in events:
        groups[_conv_id(e)].append(e)

    total_updated = 0
    total_turns = 0

    for conv_id, group in groups.items():
        sorted_group = sorted(group, key=lambda e: (e.sequence is None, e.sequence or 0))
        turn_num = 1
        current_turn: List[AgentLog] = []
        prev_seq: Optional[int] = None
        group_updated = 0

        for event in sorted_group:
            # Skip leading non-user_message (guardrail)
            if not current_turn and event.type != "user_message":
                logger.warning(
                    "Task %s conv %s: skipping leading non-user_message event id=%s type=%s seq=%s",
                    task_id,
                    conv_id,
                    event.id,
                    event.type,
                    event.sequence,
                )
                continue

            gap = (event.sequence - prev_seq) if (prev_seq is not None and event.sequence is not None) else 0
            if gap > SEQUENCE_GAP_THRESHOLD and current_turn:
                _save_turn(current_turn, task_id, conv_id, turn_num, dry_run)
                group_updated += len(current_turn)
                total_updated += len(current_turn)
                total_turns += 1
                turn_num += 1
                current_turn = []
                prev_seq = None

            if event.type == "user_message" and current_turn:
                _save_turn(current_turn, task_id, conv_id, turn_num, dry_run)
                group_updated += len(current_turn)
                total_updated += len(current_turn)
                total_turns += 1
                turn_num += 1
                current_turn = []

            if _is_assistant_final(event):
                current_turn.append(event)
                _save_turn(current_turn, task_id, conv_id, turn_num, dry_run)
                group_updated += len(current_turn)
                total_updated += len(current_turn)
                total_turns += 1
                turn_num += 1
                current_turn = []
                prev_seq = event.sequence
                continue

            current_turn.append(event)
            prev_seq = event.sequence

        if current_turn:
            _save_turn(current_turn, task_id, conv_id, turn_num, dry_run)
            group_updated += len(current_turn)
            total_updated += len(current_turn)
            total_turns += 1

        # Commit per conversation group
        if group_updated and not dry_run:
            db.commit()

    if total_updated and not dry_run:
        _validate_turn(db, task_id, dry_run)

    return total_updated, total_turns


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill turn_id and turn_number for existing AgentLog events (group by conversation_id, boundaries: user_message/assistant_final/gap>10).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log updates without committing.",
    )
    parser.add_argument(
        "--task-id",
        type=int,
        default=None,
        metavar="ID",
        help="Backfill only this task (default: all tasks with unbackfilled events).",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.task_id is not None:
            task_ids = [(args.task_id,)]
            count = (
                db.query(AgentLog)
                .filter(
                    AgentLog.task_id == args.task_id,
                    AgentLog.turn_id.is_(None),
                )
                .count()
            )
            if count == 0:
                logger.info("Task %s has no events with turn_id IS NULL; nothing to backfill.", args.task_id)
                return 0
        else:
            task_ids = (
                db.query(AgentLog.task_id)
                .filter(AgentLog.turn_id.is_(None))
                .distinct()
                .all()
            )

        if not task_ids:
            logger.info("No events with turn_id IS NULL; nothing to backfill.")
            return 0

        total_events = 0
        for (task_id,) in task_ids:
            logger.info("Backfilling task %s...", task_id)
            updated, turns = backfill_turn_metadata(db, task_id, dry_run=args.dry_run)
            total_events += updated
            logger.info("Task %s: updated %s events in %s turn(s)", task_id, updated, turns)

        if args.dry_run:
            logger.info("DRY RUN: would have updated %s events total (no commit).", total_events)
        else:
            logger.info("Backfill complete. Updated %s events total.", total_events)

        return 0
    except Exception as e:
        logger.exception("Backfill failed: %s", e)
        db.rollback()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
