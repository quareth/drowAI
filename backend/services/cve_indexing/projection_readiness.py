"""Typed projection-readiness contracts and evaluator for CVE lookup gates.

Scope:
- Defines durable projection status constants.
- Evaluates lookup readiness from persisted CVE projection state.
- Produces a typed readiness contract shared by runtime and backfill paths.

Boundary:
- Does not perform projection writes or CVE sync orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.models.cve import CveAffectedProduct, CveRecord


ProjectionStatus = Literal["pending", "projected", "non_projectable", "projection_error"]
LookupReadinessReason = Literal[
    "ok",
    "lookup_index_empty",
    "lookup_projection_incomplete",
    "lookup_projection_errors",
]

PROJECTION_STATUS_PENDING: ProjectionStatus = "pending"
PROJECTION_STATUS_PROJECTED: ProjectionStatus = "projected"
PROJECTION_STATUS_NON_PROJECTABLE: ProjectionStatus = "non_projectable"
PROJECTION_STATUS_ERROR: ProjectionStatus = "projection_error"

TERMINAL_PROJECTION_STATUSES: frozenset[str] = frozenset(
    {PROJECTION_STATUS_PROJECTED, PROJECTION_STATUS_NON_PROJECTABLE}
)
BLOCKING_PROJECTION_STATUSES: frozenset[str] = frozenset(
    {PROJECTION_STATUS_PENDING, PROJECTION_STATUS_ERROR}
)


@dataclass(slots=True, frozen=True)
class CveProjectionReadiness:
    """Typed projection-readiness contract shared by lookup consumers."""

    ready: bool
    reason: LookupReadinessReason
    message: str
    record_count: int
    affected_product_count: int
    status_counts: dict[str, int]
    blocking_status_counts: dict[str, int]
    blocking_reasons: tuple[str, ...]

    def to_lookup_availability(self, *, allow_partial: bool = False) -> dict[str, Any]:
        """Render lookup availability payload for runtime/tool metadata."""
        available = bool(self.ready)
        reason = str(self.reason)
        message = str(self.message)

        if allow_partial and reason in {"lookup_projection_incomplete", "lookup_projection_errors"}:
            available = True
            reason = "lookup_partial_index"
            message = (
                "knowledge.cve_lookup is running with partial CVE projection coverage; "
                "results may be incomplete."
            )

        return {
            "available": bool(available),
            "reason": reason,
            "message": message,
            "projection_ready": bool(self.ready),
            "record_count": int(self.record_count),
            "affected_product_count": int(self.affected_product_count),
            "status_counts": dict(self.status_counts),
            "blocking_status_counts": dict(self.blocking_status_counts),
            "blocking_reasons": list(self.blocking_reasons),
        }

    def to_lookup_coverage(self) -> dict[str, Any]:
        """Render best-effort lookup coverage details for tool output payloads."""
        status_counts = dict(self.status_counts)
        pending_count = int(status_counts.get(PROJECTION_STATUS_PENDING, 0))
        error_count = int(status_counts.get(PROJECTION_STATUS_ERROR, 0))
        projected_count = int(status_counts.get(PROJECTION_STATUS_PROJECTED, 0))
        is_partial = bool(pending_count > 0 or error_count > 0)
        warning = (
            "CVE projection is partial; lookup results may be incomplete."
            if is_partial
            else ""
        )
        return {
            "is_partial": is_partial,
            "pending_count": pending_count,
            "error_count": error_count,
            "projected_count": projected_count,
            "record_count": int(self.record_count),
            "warning": warning,
        }


class CveProjectionReadinessService:
    """Read-model readiness evaluator for lookup availability decisions."""

    def __init__(self, db: Session):
        self._db = db

    def evaluate(self) -> CveProjectionReadiness:
        try:
            self._db.flush()
        except Exception:
            pass
        record_count = int(self._db.query(func.count(CveRecord.id)).scalar() or 0)
        affected_product_count = int(self._db.query(CveAffectedProduct).count())

        if record_count <= 0:
            return CveProjectionReadiness(
                ready=False,
                reason="lookup_index_empty",
                message="knowledge.cve_lookup is unavailable because the CVE index is empty.",
                record_count=0,
                affected_product_count=0,
                status_counts={},
                blocking_status_counts={},
                blocking_reasons=(),
            )

        status_count_rows = (
            self._db.query(CveRecord.projection_status, func.count(CveRecord.id))
            .group_by(CveRecord.projection_status)
            .all()
        )
        status_counts = self._build_status_counts(status_count_rows)
        blocking_status_counts = {
            key: int(value)
            for key, value in status_counts.items()
            if key in BLOCKING_PROJECTION_STATUSES and int(value) > 0
        }
        blocking_reasons = tuple(sorted(blocking_status_counts.keys()))

        if int(blocking_status_counts.get(PROJECTION_STATUS_ERROR, 0)) > 0:
            return CveProjectionReadiness(
                ready=False,
                reason="lookup_projection_errors",
                message=(
                    "knowledge.cve_lookup is unavailable because projection errors are present. "
                    "Run projection backfill and inspect error statuses."
                ),
                record_count=int(record_count),
                affected_product_count=int(affected_product_count),
                status_counts=status_counts,
                blocking_status_counts=blocking_status_counts,
                blocking_reasons=blocking_reasons,
            )

        if int(blocking_status_counts.get(PROJECTION_STATUS_PENDING, 0)) > 0:
            return CveProjectionReadiness(
                ready=False,
                reason="lookup_projection_incomplete",
                message=(
                    "knowledge.cve_lookup is unavailable because affected-product projection "
                    "is incomplete. Run projection backfill before lookup."
                ),
                record_count=int(record_count),
                affected_product_count=int(affected_product_count),
                status_counts=status_counts,
                blocking_status_counts=blocking_status_counts,
                blocking_reasons=blocking_reasons,
            )

        return CveProjectionReadiness(
            ready=True,
            reason="ok",
            message="ok",
            record_count=int(record_count),
            affected_product_count=int(affected_product_count),
            status_counts=status_counts,
            blocking_status_counts={},
            blocking_reasons=(),
        )

    @staticmethod
    def normalize_status(value: Any) -> ProjectionStatus:
        raw = str(value or "").strip().lower()
        if raw == PROJECTION_STATUS_PROJECTED:
            return PROJECTION_STATUS_PROJECTED
        if raw == PROJECTION_STATUS_NON_PROJECTABLE:
            return PROJECTION_STATUS_NON_PROJECTABLE
        if raw == PROJECTION_STATUS_ERROR:
            return PROJECTION_STATUS_ERROR
        return PROJECTION_STATUS_PENDING

    def _build_status_counts(self, rows: list[tuple[Any, Any]]) -> dict[str, int]:
        counts = {
            PROJECTION_STATUS_PENDING: 0,
            PROJECTION_STATUS_PROJECTED: 0,
            PROJECTION_STATUS_NON_PROJECTABLE: 0,
            PROJECTION_STATUS_ERROR: 0,
        }
        for row in rows:
            try:
                raw_status, raw_count = row
            except Exception:
                raw_status = getattr(row, "projection_status", None)
                raw_count = 1
            status = self.normalize_status(raw_status)
            try:
                count = int(raw_count)
            except Exception:
                count = 0
            if count > 0:
                counts[status] = int(counts.get(status, 0)) + count
        return {key: int(value) for key, value in counts.items() if int(value) > 0}


__all__ = [
    "BLOCKING_PROJECTION_STATUSES",
    "CveProjectionReadiness",
    "CveProjectionReadinessService",
    "PROJECTION_STATUS_ERROR",
    "PROJECTION_STATUS_NON_PROJECTABLE",
    "PROJECTION_STATUS_PENDING",
    "PROJECTION_STATUS_PROJECTED",
    "ProjectionStatus",
    "TERMINAL_PROJECTION_STATUSES",
]
