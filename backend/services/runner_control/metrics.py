"""Runner-control metrics helpers for Runner Control observability signals.

Scope:
- Emits bounded counter/gauge metrics for runner presence, heartbeat health,
  outbound delivery lifecycle, protocol failures, and assignment outcomes.
- Provides DB-backed queue-depth/presence snapshots without exposing ORM details
  to callers.

Boundaries:
- Uses shared metrics safe wrappers; no external metrics backend dependency.
- Enforces bounded reason labels to avoid unbounded user-controlled cardinality.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.runner_control import Runner, RuntimeJob
from backend.services.metrics.utils import safe_gauge, safe_inc

_ONLINE_STATUSES = frozenset({"active", "online"})
_QUEUE_DEPTH_STATUSES = frozenset({"queued", "assigned", "dispatching", "dispatched"})
_ALLOWED_REASON_RE = re.compile(r"^[A-Z0-9_]{1,64}$")


class RunnerControlMetrics:
    """Emit runner-control metrics with bounded names and optional DB snapshots."""

    def __init__(self, db: Session | None = None) -> None:
        self._db = db

    def record_runner_presence_snapshot(self, *, tenant_id: int) -> None:
        """Emit tenant-scoped online/offline runner gauges from DB state."""

        if self._db is None:
            return
        rows = self._db.execute(
            select(Runner.status, func.count(Runner.id))
            .where(Runner.tenant_id == tenant_id)
            .group_by(Runner.status)
        ).all()

        online_count = 0
        offline_count = 0
        for status, count in rows:
            normalized = str(status or "").strip().lower()
            if normalized in _ONLINE_STATUSES:
                online_count += int(count or 0)
            elif normalized == "offline":
                offline_count += int(count or 0)

        safe_gauge("runner_control.runners.online_count", float(max(0, online_count)))
        safe_gauge("runner_control.runners.offline_count", float(max(0, offline_count)))

    def observe_heartbeat_health(
        self,
        *,
        latency_seconds: float | None,
        staleness_seconds: float | None,
    ) -> None:
        """Emit heartbeat latency and staleness gauges when available."""

        if latency_seconds is not None:
            safe_gauge("runner_control.heartbeat.latency_seconds", max(0.0, float(latency_seconds)))
        if staleness_seconds is not None:
            safe_gauge("runner_control.heartbeat.staleness_seconds", max(0.0, float(staleness_seconds)))

    def record_reconnect(self) -> None:
        safe_inc("runner_control.runners.reconnect_count")

    def record_runtime_job_queue_depth(self, *, tenant_id: int) -> None:
        """Emit tenant-scoped queued runtime-job depth gauge."""

        if self._db is None:
            return
        queue_depth = self._db.execute(
            select(func.count(RuntimeJob.id)).where(
                RuntimeJob.tenant_id == tenant_id,
                RuntimeJob.status.in_(tuple(_QUEUE_DEPTH_STATUSES)),
            )
        ).scalar_one()
        safe_gauge("runner_control.runtime_jobs.queue_depth", float(max(0, int(queue_depth or 0))))

    def record_outbound_queued(self) -> None:
        safe_inc("runner_control.outbound.queued_count")

    def record_outbound_delivered(self, *, count: int = 1) -> None:
        safe_inc("runner_control.outbound.delivered_count", max(0, int(count)))

    def record_outbound_acked(self, *, count: int = 1) -> None:
        safe_inc("runner_control.outbound.acked_count", max(0, int(count)))

    def record_outbound_failed(self, *, count: int = 1) -> None:
        safe_inc("runner_control.outbound.failed_count", max(0, int(count)))

    def record_unauthorized_message(self) -> None:
        safe_inc("runner_control.messages.unauthorized_count")

    def record_protocol_validation_failure(self) -> None:
        safe_inc("runner_control.protocol.validation_failure_count")

    def record_assignment_success(self) -> None:
        safe_inc("runner_control.assignment.success_count")

    def record_assignment_failure(self, *, reason_codes: Sequence[str]) -> None:
        safe_inc("runner_control.assignment.failure_count")
        for reason in reason_codes:
            safe_inc(f"runner_control.assignment.failure_reason.{_sanitize_reason_label(reason)}")


def heartbeat_latency_seconds(*, created_at: str, now: datetime | None = None) -> float | None:
    """Return heartbeat transport latency seconds, or None when timestamp is invalid."""

    try:
        created_dt = datetime.fromisoformat(str(created_at))
    except ValueError:
        return None
    created_utc = _ensure_utc(created_dt)
    now_utc = _ensure_utc(now or datetime.now(tz=UTC))
    return max(0.0, (now_utc - created_utc).total_seconds())


def heartbeat_staleness_seconds(*, last_seen_at: datetime | None, now: datetime | None = None) -> float | None:
    """Return heartbeat staleness seconds from runner last-seen timestamp."""

    if last_seen_at is None:
        return None
    now_utc = _ensure_utc(now or datetime.now(tz=UTC))
    return max(0.0, (now_utc - _ensure_utc(last_seen_at)).total_seconds())


def _sanitize_reason_label(reason: str) -> str:
    normalized = str(reason or "").strip().upper()
    if _ALLOWED_REASON_RE.fullmatch(normalized):
        return normalized
    return "UNKNOWN"


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
