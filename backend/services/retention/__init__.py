"""Retention entrypoint used by backend startup maintenance loop.

Scope:
- Preserve existing callsite compatibility (`cleanup_agent_logs`).
- Delegate scheduled maintenance to the central retention orchestrator.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from backend.services.retention.contracts import (
    RETENTION_RUN_MODE_APPLY,
    RETENTION_SCOPE_ALL_TENANTS,
    RetentionRunRequest,
    RetentionRunResult,
)
from backend.services.retention.orchestrator import (
    EXISTING_RETENTION_CLASSES,
    EXISTING_RETENTION_EXECUTOR_ORDER,
    RetentionOrchestrator,
    build_existing_retention_executors,
)
from backend.services.tenant.rls import privileged_rls_bypass

logger = logging.getLogger(__name__)


def cleanup_agent_logs(db: Session) -> int:
    try:
        # Retention runs as a trusted background maintenance job.
        with privileged_rls_bypass(db, scope="maintenance", actor_type="system"):
            result = RetentionOrchestrator(
                db,
                executors=build_existing_retention_executors(db),
                executor_order=EXISTING_RETENTION_EXECUTOR_ORDER,
            ).run(
                RetentionRunRequest(
                    mode=RETENTION_RUN_MODE_APPLY,
                    scope=RETENTION_SCOPE_ALL_TENANTS,
                    retention_classes=EXISTING_RETENTION_CLASSES,
                )
            )
            if not result.succeeded:
                _log_failed_cleanup_result(result)
                db.rollback()
                return 0
            return _sum_applied_destructive_counts(result)
    except Exception:
        logger.error("retention cleanup failed before safe result was produced; returning 0")
        db.rollback()
        return 0


def _sum_applied_destructive_counts(result: RetentionRunResult) -> int:
    return sum(int(item.counts.applied_count) for item in result.results)


def _log_failed_cleanup_result(result: RetentionRunResult) -> None:
    failed_count = sum(1 for item in result.results if not item.succeeded)
    error_codes = sorted(
        {
            str(item.error_code)
            for item in result.results
            if not item.succeeded and item.error_code is not None
        }
    )
    logger.error(
        "retention cleanup failed for %s executor result(s); error_codes=%s; returning 0",
        failed_count,
        ",".join(error_codes) if error_codes else "unknown",
    )


__all__ = ["cleanup_agent_logs"]
