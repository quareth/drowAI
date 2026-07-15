#!/usr/bin/env python3
"""Backfill `cve_affected_products` rows from existing `cve_records`.

Scope:
- Rebuilds deterministic affected-product projection rows from already indexed CVE JSON.
- Supports batched, cursor-based operation for resumable MVP execution.

Boundary:
- Performs no source-feed download or sync scheduling.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

# Ensure backend imports work when executing script directly.
if __name__ == "__main__" and __package__ is None:
    import os

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if root not in sys.path:
        sys.path.insert(0, root)

from backend.database import SessionLocal
from backend.models.cve import CveRecord
from backend.services.cve_indexing.projection_readiness import CveProjectionReadinessService
from backend.services.cve_indexing.primitives import utc_now
from backend.services.cve_indexing.sync_record_projection import compute_projection_state


def run_backfill(
    *,
    db: Any,
    batch_size: int = 250,
    cursor_after_id: int | None = None,
) -> dict[str, Any]:
    """Backfill one deterministic batch of affected-product projection rows."""
    if int(batch_size) <= 0:
        raise ValueError("batch_size must be a positive integer")

    query = db.query(CveRecord).order_by(CveRecord.id.asc())
    if cursor_after_id is not None:
        query = query.filter(CveRecord.id > int(cursor_after_id))
    records = list(query.limit(int(batch_size)).all())

    processed_count = 0
    updated_count = 0
    unchanged_count = 0
    next_cursor_id: int | None = None

    for record in records:
        processed_count += 1
        next_cursor_id = int(record.id)
        current_signature = _projection_signature(
            affected_rows=list(getattr(record, "affected_products", []) or []),
            projection_status=getattr(record, "projection_status", None),
            projection_affected_count=getattr(record, "projection_affected_count", 0),
            projection_error_code=getattr(record, "projection_error_code", None),
        )
        computed = compute_projection_state(
            cve_id=str(record.cve_id),
            cve_json=dict(record.cve_json or {}),
        )
        projected_signature = _projection_signature(
            affected_rows=list(computed.affected_rows),
            projection_status=computed.projection_status,
            projection_affected_count=computed.affected_count,
            projection_error_code=computed.projection_error_code,
        )
        if current_signature == projected_signature:
            unchanged_count += 1
            continue

        record.affected_products = list(computed.affected_rows)
        record.projection_status = computed.projection_status
        record.projection_affected_count = int(computed.affected_count)
        record.projection_error_code = computed.projection_error_code
        record.projection_last_projected_at = utc_now()
        updated_count += 1
        db.add(record)

    projection_state = _projection_ready_state(db=db)
    has_more = False
    if records and len(records) >= int(batch_size) and next_cursor_id is not None:
        has_more = (
            db.query(CveRecord)
            .filter(CveRecord.id > int(next_cursor_id))
            .order_by(CveRecord.id.asc())
            .first()
            is not None
        )

    return {
        "ok": True,
        "processed_count": int(processed_count),
        "updated_count": int(updated_count),
        "unchanged_count": int(unchanged_count),
        "batch_size": int(batch_size),
        "cursor_after_id": int(cursor_after_id) if cursor_after_id is not None else None,
        "next_cursor_id": int(next_cursor_id) if next_cursor_id is not None else None,
        "has_more": bool(has_more),
        **projection_state,
    }


def _projection_signature(
    *,
    affected_rows: list[Any],
    projection_status: Any,
    projection_affected_count: Any,
    projection_error_code: Any,
) -> tuple[str, ...]:
    signatures: list[str] = []
    for row in affected_rows:
        payload = {
            "cve_id": str(getattr(row, "cve_id", "") or ""),
            "vendor_raw": getattr(row, "vendor_raw", None),
            "vendor_norm": getattr(row, "vendor_norm", None),
            "product_raw": getattr(row, "product_raw", None),
            "product_norm": getattr(row, "product_norm", None),
            "default_status": getattr(row, "default_status", None),
            "versions_json": getattr(row, "versions_json", None),
            "cpes_json": getattr(row, "cpes_json", None),
        }
        signatures.append(
            json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        )
    signatures.append(
        json.dumps(
            {
                "projection_status": projection_status,
                "projection_affected_count": int(projection_affected_count or 0),
                "projection_error_code": projection_error_code,
            },
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )
    return tuple(sorted(signatures))


def _projection_ready_state(*, db: Any) -> dict[str, Any]:
    readiness = CveProjectionReadinessService(db).evaluate()
    status_counts = dict(readiness.status_counts)
    projected_count = int(status_counts.get("projected", 0))
    non_projectable_count = int(status_counts.get("non_projectable", 0))
    pending_count = int(status_counts.get("pending", 0))
    error_count = int(status_counts.get("projection_error", 0))
    blocking_count = int(pending_count + error_count)

    return {
        "projection_ready": bool(readiness.ready),
        "record_count": int(readiness.record_count),
        "affected_product_count": int(readiness.affected_product_count),
        "projected_cve_count": int(projected_count),
        "non_projectable_cve_count": int(non_projectable_count),
        "pending_projection_count": int(pending_count),
        "projection_error_count": int(error_count),
        "missing_projection_records": int(blocking_count),
        "status_counts": status_counts,
        "blocking_status_counts": dict(readiness.blocking_status_counts),
        "blocking_reasons": list(readiness.blocking_reasons),
        "readiness_reason": readiness.reason,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill cve_affected_products from existing cve_records.")
    parser.add_argument("--batch-size", type=int, default=250, help="Max CVE records processed per run.")
    parser.add_argument(
        "--cursor-after-id",
        type=int,
        default=None,
        help="Resume cursor: process records with id strictly greater than this value.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Rollback DB mutations after execution.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON result.")
    parser.add_argument(
        "--strict-ready",
        action="store_true",
        help="Return non-zero when projection remains not-ready after this run.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    db = SessionLocal()
    try:
        result = run_backfill(
            db=db,
            batch_size=int(args.batch_size),
            cursor_after_id=int(args.cursor_after_id) if args.cursor_after_id is not None else None,
        )
        if args.dry_run:
            db.rollback()
        else:
            db.commit()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        if not bool(result.get("ok")):
            return 2
        if bool(args.strict_ready) and not bool(result.get("projection_ready")):
            return 3
        return 0
    except Exception as exc:
        db.rollback()
        payload = {
            "ok": False,
            "error": f"{exc.__class__.__name__}: {str(exc)}",
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
