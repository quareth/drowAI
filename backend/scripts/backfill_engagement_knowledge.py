#!/usr/bin/env python3
"""Run historical backfill or candidate replay backfill.

This script executes engagement-scoped read-model rebuild/backfill using durable
observations as authority ( mode) or replays candidate extraction
across historical source executions in deterministic batches with cursor resume."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from typing import Any, Sequence

# Ensure backend imports when executed directly.
if __name__ == "__main__" and __package__ is None:
    import os

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if root not in sys.path:
        sys.path.insert(0, root)

from backend.database import SessionLocal
from backend.models.knowledge import KnowledgeIngestionRun
from backend.config.feature_flags import is_knowledge_candidate_extraction_enabled
from backend.services.knowledge.historical_backfill_service import (
    KnowledgeHistoricalBackfillService,
)
from backend.services.knowledge.replay_service import KnowledgeReplayService
from sqlalchemy import select

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplayTarget:
    """Replay target row for one source execution."""

    source_execution_id: str
    engagement_id: int
    task_id: int | None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run historical read-model backfill (default) or candidate replay "
            "candidate replay backfill in deterministic batches."
        )
    )
    parser.add_argument(
        "--mode",
        choices=("historical_projection", "candidate_replay"),
        default="historical_projection",
        help=(
            "Backfill mode. historical_projection keeps the existing completion-gate "
            "rebuild behavior; candidate_replay runs candidate replay batches."
        ),
    )
    parser.add_argument(
        "--engagement-id",
        action="append",
        type=int,
        dest="engagement_ids",
        help=(
            "Limit backfill to one engagement id. Repeat the flag to provide "
            "multiple engagement ids."
        ),
    )
    parser.add_argument(
        "--skip-idempotent-rerun-check",
        action="store_true",
        help="Skip the second-rerun idempotency verification (completion gate will not pass).",
    )
    parser.add_argument(
        "--task-id",
        action="append",
        type=int,
        dest="task_ids",
        help="Replay mode only: limit candidate replay backfill to specific task id(s).",
    )
    parser.add_argument(
        "--source-execution-id",
        action="append",
        dest="source_execution_ids",
        help=(
            "Replay mode only: limit candidate replay backfill to specific source "
            "execution id(s). Repeat for multiple values."
        ),
    )
    parser.add_argument(
        "--extractor-family",
        default="llm.candidate_extraction",
        help="Replay mode only: extractor family to execute.",
    )
    parser.add_argument(
        "--target-extractor-version",
        default="1.0",
        help="Replay mode only: explicit replay target extractor version.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Replay mode only: max replay items selected per batch.",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=None,
        help="Replay mode only: optional cap for selected replay items after cursor filtering.",
    )
    parser.add_argument(
        "--cursor-source-execution-id",
        default=None,
        help=(
            "Replay mode only: resume cursor; process source_execution_id values "
            "strictly greater than this cursor."
        ),
    )
    parser.add_argument(
        "--fail-on-existing-version",
        action="store_true",
        help=(
            "Replay mode only: treat already-used extractor family/version for the "
            "same execution as a failure instead of a skip."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Execute backfill logic and then rollback DB changes.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print only JSON summary output (no additional log lines).",
    )
    return parser


def _normalize_positive_ids(values: Sequence[int] | None) -> list[int] | None:
    if values is None:
        return None
    normalized = sorted({int(value) for value in values})
    for value in normalized:
        if value <= 0:
            raise ValueError("Filter ids must be positive integers")
    return normalized


def _normalize_source_execution_ids(values: Sequence[str] | None) -> list[str] | None:
    if values is None:
        return None
    normalized = sorted({str(item).strip() for item in values if str(item).strip()})
    return normalized or None


def _resolve_remote_runtime_replay_targets(
    *,
    db,
    engagement_ids: Sequence[int] | None,
    task_ids: Sequence[int] | None,
    source_execution_ids: Sequence[str] | None,
    cursor_source_execution_id: str | None,
    batch_size: int,
    max_items: int | None,
) -> tuple[list[ReplayTarget], str | None]:
    """Resolve deterministic replay targets from existing durable ingestion lineage."""
    if batch_size <= 0:
        raise ValueError("batch_size must be a positive integer")
    if max_items is not None and max_items <= 0:
        raise ValueError("max_items must be a positive integer when provided")

    engagement_set = set(int(item) for item in engagement_ids) if engagement_ids is not None else None
    task_set = set(int(item) for item in task_ids) if task_ids is not None else None
    source_set = set(source_execution_ids) if source_execution_ids is not None else None
    cursor = str(cursor_source_execution_id or "").strip() or None

    rows = db.execute(
        select(
            KnowledgeIngestionRun.source_execution_id,
            KnowledgeIngestionRun.engagement_id,
            KnowledgeIngestionRun.task_id,
            KnowledgeIngestionRun.created_at,
        ).order_by(
            KnowledgeIngestionRun.source_execution_id.asc(),
            KnowledgeIngestionRun.created_at.desc(),
            KnowledgeIngestionRun.id.desc(),
        )
    ).all()

    unique_latest_by_execution: dict[str, ReplayTarget] = {}
    for row in rows:
        source_execution_id = str(row[0])
        if source_execution_id in unique_latest_by_execution:
            continue
        engagement_id = int(row[1])
        task_id = int(row[2]) if row[2] is not None else None
        if engagement_set is not None and engagement_id not in engagement_set:
            continue
        if task_set is not None and (task_id is None or task_id not in task_set):
            continue
        if source_set is not None and source_execution_id not in source_set:
            continue
        if cursor is not None and source_execution_id <= cursor:
            continue
        unique_latest_by_execution[source_execution_id] = ReplayTarget(
            source_execution_id=source_execution_id,
            engagement_id=engagement_id,
            task_id=task_id,
        )

    ordered = sorted(
        unique_latest_by_execution.values(),
        key=lambda item: item.source_execution_id,
    )
    if max_items is not None:
        ordered = ordered[: int(max_items)]
    ordered = ordered[: int(batch_size)]
    next_cursor = ordered[-1].source_execution_id if ordered else cursor
    return ordered, next_cursor


def run_remote_runtime_candidate_replay_backfill(
    *,
    db,
    engagement_ids: Sequence[int] | None = None,
    task_ids: Sequence[int] | None = None,
    source_execution_ids: Sequence[str] | None = None,
    extractor_family: str = "llm.candidate_extraction",
    target_extractor_version: str = "1.0",
    batch_size: int = 100,
    max_items: int | None = None,
    cursor_source_execution_id: str | None = None,
    fail_on_existing_version: bool = False,
) -> dict[str, Any]:
    """Run one deterministic candidate replay backfill batch."""
    if not is_knowledge_candidate_extraction_enabled():
        raise ValueError(
            "Candidate replay is disabled because "
            "ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION is false"
        )
    targets, next_cursor = _resolve_remote_runtime_replay_targets(
        db=db,
        engagement_ids=engagement_ids,
        task_ids=task_ids,
        source_execution_ids=source_execution_ids,
        cursor_source_execution_id=cursor_source_execution_id,
        batch_size=batch_size,
        max_items=max_items,
    )
    replay_service = KnowledgeReplayService(db)
    succeeded: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for target in targets:
        already_used = replay_service.extractor_version_exists(
            source_execution_id=target.source_execution_id,
            extractor_family=str(extractor_family),
            extractor_version=str(target_extractor_version),
        )
        if already_used and not fail_on_existing_version:
            skipped.append(
                {
                    "source_execution_id": target.source_execution_id,
                    "engagement_id": target.engagement_id,
                    "task_id": target.task_id,
                    "reason": "extractor_version_already_used",
                }
            )
            continue

        try:
            replay = replay_service.replay_execution(
                task_id=target.task_id,
                source_execution_id=target.source_execution_id,
                extractor_family=str(extractor_family),
                target_extractor_version=str(target_extractor_version),
            )
            succeeded.append(
                {
                    "source_execution_id": target.source_execution_id,
                    "engagement_id": target.engagement_id,
                    "task_id": target.task_id,
                    "ingestion_run_id": replay.get("ingestion_run_id"),
                    "status": replay.get("status"),
                    "replay_source_type": replay.get("replay_source_type"),
                    "candidate_outcome_summary": replay.get("candidate_outcome_summary"),
                }
            )
        except Exception as exc:
            failed.append(
                {
                    "source_execution_id": target.source_execution_id,
                    "engagement_id": target.engagement_id,
                    "task_id": target.task_id,
                    "error_reason": f"{exc.__class__.__name__}: {str(exc)}",
                    "rerun_plan": {
                        "operation": "knowledge_replay_service.replay_execution",
                        "params": {
                            "task_id": target.task_id,
                            "source_execution_id": target.source_execution_id,
                            "extractor_family": str(extractor_family),
                            "target_extractor_version": str(target_extractor_version),
                        },
                    },
                }
            )

    return {
        "ok": len(failed) == 0,
        "mode": "candidate_replay",
        "extractor_family": str(extractor_family),
        "target_extractor_version": str(target_extractor_version),
        "input_cursor_source_execution_id": cursor_source_execution_id,
        "next_cursor_source_execution_id": next_cursor,
        "batch_size": int(batch_size),
        "max_items": max_items,
        "selected_target_count": len(targets),
        "succeeded_count": len(succeeded),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
    }


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if not args.json:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    db = SessionLocal()
    try:
        if args.mode == "historical_projection":
            service = KnowledgeHistoricalBackfillService(db)
            result = service.run_backfill(
                target_engagement_ids=args.engagement_ids,
                verify_idempotent_rerun=not bool(args.skip_idempotent_rerun_check),
            )
        else:
            if not is_knowledge_candidate_extraction_enabled():
                result = {
                    "ok": False,
                    "mode": "candidate_replay",
                    "error_reason": (
                        "Candidate replay is disabled because "
                        "ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION is false"
                    ),
                }
                output = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)
                print(output)
                return 2
            result = run_remote_runtime_candidate_replay_backfill(
                db=db,
                engagement_ids=_normalize_positive_ids(args.engagement_ids),
                task_ids=_normalize_positive_ids(args.task_ids),
                source_execution_ids=_normalize_source_execution_ids(args.source_execution_ids),
                extractor_family=str(args.extractor_family),
                target_extractor_version=str(args.target_extractor_version),
                batch_size=int(args.batch_size),
                max_items=int(args.max_items) if args.max_items is not None else None,
                cursor_source_execution_id=args.cursor_source_execution_id,
                fail_on_existing_version=bool(args.fail_on_existing_version),
            )

        if args.dry_run:
            db.rollback()
        else:
            db.commit()

        output = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)
        print(output)

        if not args.json:
            if args.mode == "historical_projection":
                logger.info(
                    (
                        "Backfill attempted=%s succeeded=%s failed=%s gate=%s "
                        "web_path_upserts=%s web_path_inserts=%s dry_run=%s"
                    ),
                    result.get("attempted_engagement_count"),
                    result.get("succeeded_engagement_count"),
                    result.get("failed_engagement_count"),
                    result.get("completion_gate_passed"),
                    result.get("web_path_upsert_count"),
                    result.get("web_path_insert_count"),
                    args.dry_run,
                )
            else:
                logger.info(
                    (
                        "Candidate replay batch selected=%s succeeded=%s "
                        "skipped=%s failed=%s next_cursor=%s dry_run=%s"
                    ),
                    result.get("selected_target_count"),
                    result.get("succeeded_count"),
                    result.get("skipped_count"),
                    result.get("failed_count"),
                    result.get("next_cursor_source_execution_id"),
                    args.dry_run,
                )

        if args.mode == "historical_projection":
            return 0 if bool(result.get("completion_gate_passed")) else 2
        return 0 if bool(result.get("ok")) else 2
    except Exception:
        db.rollback()
        logger.exception("Engagement knowledge backfill failed.")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
