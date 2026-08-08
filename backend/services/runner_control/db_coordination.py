"""SQLAlchemy-backed coordination store for runner-control multi-pod state.

Scope:
- Implements the runner-control coordination port using durable database tables
  for runner presence leases, idempotency ledger rows, and outbound queue state.

Boundaries:
- Keeps SQLAlchemy details inside this module behind coordination interfaces.
- Assumes caller controls outer request transactions while this module uses
  savepoints and conflict-aware writes for idempotent behavior.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models.runner_control import Runner, RunnerConnection, RunnerControlMessage
from backend.services.runner_control.coordination import (
    InboundIdempotencyRecord,
    LeaseExpiryResult,
    QueuedOutboundMessage,
    RunnerConnectionLease,
    RunnerCoordinationStore,
    RunnerOfflineTransition,
)
from backend.services.runner_control.metrics import RunnerControlMetrics
from runtime_shared.durable_secret_masking import mask_durable_secrets

_metrics = RunnerControlMetrics()
_RAW_OUTBOUND_PAYLOADS: dict[UUID, dict[str, Any]] = {}


class DBRunnerCoordinationStore(RunnerCoordinationStore):
    """Database-backed coordination store using conflict-safe, idempotent writes."""

    def __init__(self, db: Session, *, pod_id: str) -> None:
        self._db = db
        self._pod_id = str(pod_id).strip() or "local-pod"

    def claim_connection_lease(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        pod_id: str,
        connection_id: str,
        lease_expires_at: datetime,
        last_seen_at: datetime,
    ) -> RunnerConnectionLease:
        lease_expires_at = _ensure_utc(lease_expires_at)
        last_seen_at = _ensure_utc(last_seen_at)

        connection = self._find_connection(
            tenant_id=tenant_id,
            runner_id=runner_id,
            connection_id=connection_id,
        )
        if connection is None:
            connection = RunnerConnection(
                tenant_id=tenant_id,
                runner_id=runner_id,
                pod_id=str(pod_id).strip() or self._pod_id,
                connection_id=str(connection_id).strip(),
                status="active",
                lease_expires_at=lease_expires_at,
                last_seen_at=last_seen_at,
            )
            try:
                with self._db.begin_nested():
                    self._db.add(connection)
                    self._db.flush()
            except IntegrityError:
                connection = self._find_connection(
                    tenant_id=tenant_id,
                    runner_id=runner_id,
                    connection_id=connection_id,
                )
                if connection is None:
                    raise

        with self._db.begin_nested():
            connection.pod_id = str(pod_id).strip() or self._pod_id
            connection.status = "active"
            connection.lease_expires_at = lease_expires_at
            connection.last_seen_at = last_seen_at
            self._db.flush()

        return _to_connection_lease(connection)

    def refresh_connection_lease(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        connection_id: str,
        lease_expires_at: datetime,
        last_seen_at: datetime,
    ) -> RunnerConnectionLease | None:
        lease_expires_at = _ensure_utc(lease_expires_at)
        last_seen_at = _ensure_utc(last_seen_at)

        with self._db.begin_nested():
            connection = self._find_connection(
                tenant_id=tenant_id,
                runner_id=runner_id,
                connection_id=connection_id,
            )
            if connection is None:
                return None

            # Idempotent refresh always converges to active + latest lease bounds.
            connection.status = "active"
            connection.lease_expires_at = lease_expires_at
            connection.last_seen_at = last_seen_at
            self._db.flush()
            return _to_connection_lease(connection)

    def release_connection_lease(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        connection_id: str,
        released_at: datetime,
    ) -> bool:
        released_at = _ensure_utc(released_at)
        with self._db.begin_nested():
            connection = self._find_connection(
                tenant_id=tenant_id,
                runner_id=runner_id,
                connection_id=connection_id,
            )
            if connection is None:
                return False
            connection.status = "disconnected"
            connection.last_seen_at = released_at
            self._db.flush()
            active_lease_exists = exists(
                select(RunnerConnection.id).where(
                    RunnerConnection.tenant_id == tenant_id,
                    RunnerConnection.runner_id == runner_id,
                    RunnerConnection.status == "active",
                    RunnerConnection.lease_expires_at > released_at,
                )
            )
            self._db.execute(
                update(Runner)
                .where(
                    Runner.tenant_id == tenant_id,
                    Runner.id == runner_id,
                    Runner.status != "offline",
                    ~active_lease_exists,
                )
                .values(status="offline", last_seen_at=released_at)
            )
            self._db.flush()
            return True

    def mark_runner_online(self, *, tenant_id: int, runner_id: UUID, last_seen_at: datetime) -> bool:
        last_seen_at = _ensure_utc(last_seen_at)
        with self._db.begin_nested():
            runner = self._find_runner(tenant_id=tenant_id, runner_id=runner_id)
            if runner is None:
                return False
            runner.status = "active"
            runner.last_seen_at = last_seen_at
            self._db.flush()
            return True

    def mark_runner_offline(self, *, tenant_id: int, runner_id: UUID, last_seen_at: datetime | None = None) -> bool:
        with self._db.begin_nested():
            runner = self._find_runner(tenant_id=tenant_id, runner_id=runner_id)
            if runner is None:
                return False
            runner.status = "offline"
            if last_seen_at is not None:
                runner.last_seen_at = _ensure_utc(last_seen_at)
            self._db.flush()
            return True

    def enqueue_outbound_message(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        message_id: str,
        message_type: str,
        payload_json: dict[str, Any] | None,
        idempotency_key: str | None,
        runtime_job_id: UUID | None,
        task_id: int | None,
        correlation_id: str | None,
    ) -> QueuedOutboundMessage:
        normalized_message_id = str(message_id).strip()
        normalized_idempotency = _normalize_optional(idempotency_key)

        existing = self._find_existing_outbound(
            tenant_id=tenant_id,
            runner_id=runner_id,
            message_id=normalized_message_id,
            idempotency_key=normalized_idempotency,
        )
        if existing is not None:
            return _to_outbound_message(existing)

        raw_payload_json = dict(payload_json) if payload_json is not None else None
        durable_payload_json = _mask_payload(raw_payload_json, source="runner_outbound_message")

        message = RunnerControlMessage(
            tenant_id=tenant_id,
            runner_id=runner_id,
            runtime_job_id=runtime_job_id,
            task_id=task_id,
            message_id=normalized_message_id,
            direction="outbound",
            type=str(message_type).strip(),
            status="queued",
            idempotency_key=normalized_idempotency,
            correlation_id=_normalize_optional(correlation_id),
            payload_json=durable_payload_json,
        )
        try:
            with self._db.begin_nested():
                self._db.add(message)
                self._db.flush()
                if raw_payload_json is not None:
                    _RAW_OUTBOUND_PAYLOADS[message.id] = raw_payload_json
        except IntegrityError:
            existing = self._find_existing_outbound(
                tenant_id=tenant_id,
                runner_id=runner_id,
                message_id=normalized_message_id,
                idempotency_key=normalized_idempotency,
            )
            if existing is None:
                raise
            return _to_outbound_message(existing)
        _metrics.record_outbound_queued()
        return _to_outbound_message(message)

    def claim_queued_outbound_messages(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        pod_id: str,
        connection_id: str,
        max_messages: int,
    ) -> tuple[QueuedOutboundMessage, ...]:
        limit = max(1, int(max_messages))
        now_utc = datetime.now(tz=UTC)
        normalized_pod_id = str(pod_id).strip()
        normalized_connection_id = str(connection_id).strip()
        if not normalized_pod_id or not normalized_connection_id:
            return ()
        queued_statuses = ("queued", "pending", "retry")
        active_lease_exists = exists(
            select(RunnerConnection.id).where(
                RunnerConnection.tenant_id == tenant_id,
                RunnerConnection.runner_id == runner_id,
                RunnerConnection.pod_id == normalized_pod_id,
                RunnerConnection.connection_id == normalized_connection_id,
                RunnerConnection.status == "active",
                RunnerConnection.lease_expires_at > now_utc,
            )
        )

        claimable_ids = (
            select(RunnerControlMessage.id)
            .where(
                RunnerControlMessage.tenant_id == tenant_id,
                RunnerControlMessage.runner_id == runner_id,
                RunnerControlMessage.direction == "outbound",
                RunnerControlMessage.status.in_(queued_statuses),
            )
            .order_by(RunnerControlMessage.created_at.asc(), RunnerControlMessage.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )

        with self._db.begin_nested():
            claimed_ids = self._db.execute(
                update(RunnerControlMessage)
                .where(
                    RunnerControlMessage.id.in_(claimable_ids),
                    RunnerControlMessage.status.in_(queued_statuses),
                    active_lease_exists,
                )
                .values(status="dispatching")
                .returning(RunnerControlMessage.id)
            ).scalars().all()

        if not claimed_ids:
            return ()

        claimed_rows = self._db.execute(
            select(RunnerControlMessage)
            .where(
                RunnerControlMessage.id.in_(claimed_ids),
                RunnerControlMessage.tenant_id == tenant_id,
                RunnerControlMessage.runner_id == runner_id,
                RunnerControlMessage.direction == "outbound",
                RunnerControlMessage.status == "dispatching",
            )
            .order_by(RunnerControlMessage.created_at.asc(), RunnerControlMessage.id.asc())
        ).scalars().all()
        return tuple(_to_outbound_message(row) for row in claimed_rows)

    def record_inbound_message_idempotency(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        message_id: str,
        message_type: str,
        idempotency_key: str | None,
        status: str,
        payload_json: dict[str, Any] | None,
        runtime_job_id: UUID | None,
        task_id: int | None,
        correlation_id: str | None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> InboundIdempotencyRecord:
        normalized_message_id = str(message_id).strip()
        normalized_idempotency = _normalize_optional(idempotency_key)
        normalized_status = str(status).strip() or "accepted"

        existing = self._find_inbound(
            tenant_id=tenant_id,
            runner_id=runner_id,
            message_id=normalized_message_id,
            idempotency_key=normalized_idempotency,
        )
        if existing is not None:
            return _to_inbound_record(existing, duplicate=True)

        created = RunnerControlMessage(
            tenant_id=tenant_id,
            runner_id=runner_id,
            runtime_job_id=runtime_job_id,
            task_id=task_id,
            message_id=normalized_message_id,
            direction="inbound",
            type=str(message_type).strip(),
            status=normalized_status,
            idempotency_key=normalized_idempotency,
            correlation_id=_normalize_optional(correlation_id),
            payload_json=_mask_payload(
                dict(payload_json) if payload_json is not None else None,
                source="runner_inbound_message",
            ),
            error_code=_normalize_optional(error_code),
            error_message=_normalize_optional(
                str(mask_durable_secrets(error_message, source="runner_inbound_error_message"))
                if error_message is not None
                else None
            ),
        )
        try:
            with self._db.begin_nested():
                self._db.add(created)
                self._db.flush()
        except IntegrityError:
            existing = self._find_inbound(
                tenant_id=tenant_id,
                runner_id=runner_id,
                message_id=normalized_message_id,
                idempotency_key=normalized_idempotency,
            )
            if existing is None:
                raise
            return _to_inbound_record(existing, duplicate=True)
        return _to_inbound_record(created, duplicate=False)

    def mark_outbound_message_delivered(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        message_id: str,
    ) -> bool:
        return self._set_outbound_status(
            tenant_id=tenant_id,
            runner_id=runner_id,
            message_id=message_id,
            status="delivered",
            error_code=None,
            error_message=None,
            increment_attempt=True,
        )

    def mark_outbound_message_acked(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        message_id: str,
    ) -> bool:
        return self._set_outbound_status(
            tenant_id=tenant_id,
            runner_id=runner_id,
            message_id=message_id,
            status="acked",
            error_code=None,
            error_message=None,
            increment_attempt=False,
        )

    def mark_outbound_message_retry(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        message_id: str,
        error_code: str | None,
        error_message: str | None,
    ) -> bool:
        return self._set_outbound_status(
            tenant_id=tenant_id,
            runner_id=runner_id,
            message_id=message_id,
            status="retry",
            error_code=error_code,
            error_message=error_message,
            increment_attempt=True,
        )

    def mark_outbound_message_failed(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        message_id: str,
        error_code: str | None,
        error_message: str | None,
    ) -> bool:
        return self._set_outbound_status(
            tenant_id=tenant_id,
            runner_id=runner_id,
            message_id=message_id,
            status="failed",
            error_code=error_code,
            error_message=error_message,
            increment_attempt=True,
        )

    def expire_stale_leases(self, *, now: datetime) -> LeaseExpiryResult:
        now_utc = _ensure_utc(now)
        with self._db.begin_nested():
            expired_rows = self._db.execute(
                update(RunnerConnection)
                .where(
                    RunnerConnection.status == "active",
                    RunnerConnection.lease_expires_at <= now_utc,
                )
                .values(status="disconnected", last_seen_at=now_utc)
                .returning(RunnerConnection.tenant_id, RunnerConnection.runner_id)
            ).all()

        runner_keys = {
            (int(row.tenant_id), row.runner_id)
            for row in expired_rows
        }
        expired_count = len(expired_rows)

        offline_count = 0
        offline_transitions: list[RunnerOfflineTransition] = []
        for tenant_id, runner_id in runner_keys:
            active_lease_exists = exists(
                select(RunnerConnection.id).where(
                    RunnerConnection.tenant_id == tenant_id,
                    RunnerConnection.runner_id == runner_id,
                    RunnerConnection.status == "active",
                    RunnerConnection.lease_expires_at > now_utc,
                )
            )
            with self._db.begin_nested():
                result = self._db.execute(
                    update(Runner)
                    .where(
                        Runner.tenant_id == tenant_id,
                        Runner.id == runner_id,
                        Runner.status != "offline",
                        ~active_lease_exists,
                    )
                    .values(status="offline", last_seen_at=now_utc)
                )
                if result.rowcount and result.rowcount > 0:
                    offline_count += 1
                    offline_transitions.append(
                        RunnerOfflineTransition(
                            tenant_id=tenant_id,
                            runner_id=runner_id,
                            last_seen_at=now_utc,
                            reason="stale_connection_lease_expired",
                        )
                    )

        return LeaseExpiryResult(
            expired_connection_count=expired_count,
            offline_runner_count=offline_count,
            offline_transitions=tuple(offline_transitions),
        )

    def _set_outbound_status(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        message_id: str,
        status: str,
        error_code: str | None,
        error_message: str | None,
        increment_attempt: bool,
    ) -> bool:
        with self._db.begin_nested():
            message = self._find_outbound(
                tenant_id=tenant_id,
                runner_id=runner_id,
                message_id=str(message_id).strip(),
            )
            if message is None:
                return False
            previous_status = str(message.status or "").strip().lower()
            message.status = status
            message.error_code = _normalize_optional(error_code)
            message.error_message = _normalize_optional(error_message)
            # A single physical send should count once even if a later transition
            # (retry/failed) follows an intermediate delivered state.
            if increment_attempt and previous_status != "delivered":
                message.delivery_attempt_count = int(message.delivery_attempt_count or 0) + 1
            self._db.flush()
            return True

    def _find_runner(self, *, tenant_id: int, runner_id: UUID) -> Runner | None:
        return self._db.execute(
            select(Runner).where(
                Runner.tenant_id == tenant_id,
                Runner.id == runner_id,
            )
        ).scalar_one_or_none()

    def _find_connection(self, *, tenant_id: int, runner_id: UUID, connection_id: str) -> RunnerConnection | None:
        return self._db.execute(
            select(RunnerConnection).where(
                RunnerConnection.tenant_id == tenant_id,
                RunnerConnection.runner_id == runner_id,
                RunnerConnection.connection_id == str(connection_id).strip(),
            )
        ).scalar_one_or_none()

    def _find_existing_outbound(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        message_id: str,
        idempotency_key: str | None,
    ) -> RunnerControlMessage | None:
        by_message = self._find_outbound(
            tenant_id=tenant_id,
            runner_id=runner_id,
            message_id=message_id,
        )
        if by_message is not None:
            return by_message

        if idempotency_key is None:
            return None

        return self._db.execute(
            select(RunnerControlMessage).where(
                RunnerControlMessage.tenant_id == tenant_id,
                RunnerControlMessage.runner_id == runner_id,
                RunnerControlMessage.direction == "outbound",
                RunnerControlMessage.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()

    def _find_outbound(self, *, tenant_id: int, runner_id: UUID, message_id: str) -> RunnerControlMessage | None:
        return self._db.execute(
            select(RunnerControlMessage).where(
                RunnerControlMessage.tenant_id == tenant_id,
                RunnerControlMessage.runner_id == runner_id,
                RunnerControlMessage.direction == "outbound",
                RunnerControlMessage.message_id == message_id,
            )
        ).scalar_one_or_none()

    def _find_outbound_by_id(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        message_db_id: UUID,
    ) -> RunnerControlMessage | None:
        return self._db.execute(
            select(RunnerControlMessage).where(
                RunnerControlMessage.id == message_db_id,
                RunnerControlMessage.tenant_id == tenant_id,
                RunnerControlMessage.runner_id == runner_id,
                RunnerControlMessage.direction == "outbound",
            )
        ).scalar_one_or_none()

    def _find_inbound(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        message_id: str,
        idempotency_key: str | None,
    ) -> RunnerControlMessage | None:
        by_message_id = self._db.execute(
            select(RunnerControlMessage).where(
                RunnerControlMessage.tenant_id == tenant_id,
                RunnerControlMessage.runner_id == runner_id,
                RunnerControlMessage.direction == "inbound",
                RunnerControlMessage.message_id == message_id,
            )
        ).scalar_one_or_none()
        if by_message_id is not None:
            return by_message_id
        if idempotency_key is None:
            return None
        return self._db.execute(
            select(RunnerControlMessage).where(
                RunnerControlMessage.tenant_id == tenant_id,
                RunnerControlMessage.runner_id == runner_id,
                RunnerControlMessage.direction == "inbound",
                RunnerControlMessage.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()

    def _outbound_query(self, *, tenant_id: int, runner_id: UUID) -> Select[tuple[RunnerControlMessage]]:
        return (
            select(RunnerControlMessage)
            .where(
                RunnerControlMessage.tenant_id == tenant_id,
                RunnerControlMessage.runner_id == runner_id,
                RunnerControlMessage.direction == "outbound",
                RunnerControlMessage.status.in_(("queued", "pending", "retry")),
            )
            .order_by(RunnerControlMessage.created_at.asc(), RunnerControlMessage.id.asc())
        )


def _to_connection_lease(connection: RunnerConnection) -> RunnerConnectionLease:
    return RunnerConnectionLease(
        tenant_id=int(connection.tenant_id),
        runner_id=connection.runner_id,
        pod_id=str(connection.pod_id),
        connection_id=str(connection.connection_id),
        status=str(connection.status),
        lease_expires_at=_ensure_utc(connection.lease_expires_at),
        last_seen_at=_ensure_utc(connection.last_seen_at),
    )


def _to_outbound_message(message: RunnerControlMessage) -> QueuedOutboundMessage:
    raw_payload = _RAW_OUTBOUND_PAYLOADS.get(message.id)
    payload = raw_payload or message.payload_json
    payload_json = dict(payload) if isinstance(payload, dict) else None
    return QueuedOutboundMessage(
        id=message.id,
        tenant_id=int(message.tenant_id),
        runner_id=message.runner_id,
        message_id=str(message.message_id),
        message_type=str(message.type),
        status=str(message.status),
        payload_json=payload_json,
        payload_is_raw=raw_payload is not None,
        idempotency_key=_normalize_optional(message.idempotency_key),
        runtime_job_id=message.runtime_job_id,
        task_id=message.task_id,
        correlation_id=_normalize_optional(message.correlation_id),
        delivery_attempt_count=int(message.delivery_attempt_count or 0),
    )


def _mask_payload(value: dict[str, Any] | None, *, source: str) -> dict[str, Any] | None:
    if value is None:
        return None
    masked = mask_durable_secrets(value, source=source)
    return masked if isinstance(masked, dict) else {}


def _to_inbound_record(message: RunnerControlMessage, *, duplicate: bool) -> InboundIdempotencyRecord:
    return InboundIdempotencyRecord(
        id=message.id,
        tenant_id=int(message.tenant_id),
        runner_id=message.runner_id,
        message_id=str(message.message_id),
        status=str(message.status),
        error_code=_normalize_optional(message.error_code),
        error_message=_normalize_optional(message.error_message),
        duplicate=bool(duplicate),
    )


def _normalize_optional(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
