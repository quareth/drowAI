"""Runner-control coordination store interface and lightweight in-memory adapter.

Scope:
- Defines the persistence port used by runner-channel and dispatcher services for
  presence leases, runner online/offline state, outbound queueing, and inbound
  idempotency tracking.

Boundaries:
- Exposes storage-agnostic contracts only; no SQLAlchemy or router imports.
- Includes an in-memory implementation strictly for unit tests or explicit local
  development workflows.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any
from uuid import UUID, uuid4

from backend.services.runner_control.metrics import RunnerControlMetrics

_metrics = RunnerControlMetrics()


@dataclass(frozen=True, slots=True)
class RunnerConnectionLease:
    """Connection lease snapshot returned by coordination-store operations."""

    tenant_id: int
    runner_id: UUID
    pod_id: str
    connection_id: str
    status: str
    lease_expires_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class QueuedOutboundMessage:
    """Outbound runner-control message metadata claimed for delivery."""

    id: UUID
    tenant_id: int
    runner_id: UUID
    message_id: str
    message_type: str
    status: str
    payload_json: dict[str, Any] | None
    payload_is_raw: bool
    idempotency_key: str | None
    runtime_job_id: UUID | None
    task_id: int | None
    correlation_id: str | None
    delivery_attempt_count: int


@dataclass(frozen=True, slots=True)
class InboundIdempotencyRecord:
    """Result of recording one inbound message idempotency decision."""

    id: UUID
    tenant_id: int
    runner_id: UUID
    message_id: str
    status: str
    error_code: str | None
    error_message: str | None
    duplicate: bool


@dataclass(frozen=True, slots=True)
class RunnerOfflineTransition:
    """Offline transition metadata produced by stale-lease reconciliation."""

    tenant_id: int
    runner_id: UUID
    last_seen_at: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class LeaseExpiryResult:
    """Summary produced when stale runner connection leases are expired."""

    expired_connection_count: int
    offline_runner_count: int
    offline_transitions: tuple[RunnerOfflineTransition, ...] = ()


class RunnerCoordinationStore(ABC):
    """Port for shared runner-control coordination across backend pods."""

    @abstractmethod
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
        """Create or upsert a runner connection lease for a live channel."""

    @abstractmethod
    def refresh_connection_lease(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        connection_id: str,
        lease_expires_at: datetime,
        last_seen_at: datetime,
    ) -> RunnerConnectionLease | None:
        """Refresh an existing lease idempotently for a heartbeat/touch event."""

    @abstractmethod
    def release_connection_lease(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        connection_id: str,
        released_at: datetime,
    ) -> bool:
        """Mark a connection lease disconnected when a channel closes."""

    @abstractmethod
    def mark_runner_online(self, *, tenant_id: int, runner_id: UUID, last_seen_at: datetime) -> bool:
        """Mark a runner as online/active with a refreshed last-seen timestamp."""

    @abstractmethod
    def mark_runner_offline(self, *, tenant_id: int, runner_id: UUID, last_seen_at: datetime | None = None) -> bool:
        """Mark a runner as offline if no active lease is present."""

    @abstractmethod
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
        """Queue one outbound control message idempotently for later dispatch."""

    @abstractmethod
    def claim_queued_outbound_messages(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        pod_id: str,
        connection_id: str,
        max_messages: int,
    ) -> tuple[QueuedOutboundMessage, ...]:
        """Claim queued outbound messages for a connected runner."""

    @abstractmethod
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
        """Record or replay one inbound message idempotency decision."""

    @abstractmethod
    def mark_outbound_message_delivered(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        message_id: str,
    ) -> bool:
        """Mark an outbound message delivered to the runner transport."""

    @abstractmethod
    def mark_outbound_message_acked(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        message_id: str,
    ) -> bool:
        """Mark an outbound message acknowledged by the runner."""

    @abstractmethod
    def mark_outbound_message_retry(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        message_id: str,
        error_code: str | None,
        error_message: str | None,
    ) -> bool:
        """Mark an outbound message for retry and record last error metadata."""

    @abstractmethod
    def mark_outbound_message_failed(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        message_id: str,
        error_code: str | None,
        error_message: str | None,
    ) -> bool:
        """Mark an outbound message failed with stable error metadata."""

    @abstractmethod
    def expire_stale_leases(self, *, now: datetime) -> LeaseExpiryResult:
        """Expire stale active leases and mark runners offline when needed."""


class InMemoryRunnerCoordinationStore(RunnerCoordinationStore):
    """In-memory coordination store for unit tests or explicit local dev only."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._leases: dict[tuple[int, UUID, str], RunnerConnectionLease] = {}
        self._runner_status: dict[tuple[int, UUID], tuple[str, datetime | None]] = {}
        self._outbound_messages: dict[tuple[int, UUID, str], QueuedOutboundMessage] = {}
        self._outbound_messages_by_idempotency: dict[tuple[int, UUID, str], QueuedOutboundMessage] = {}
        self._inbound_messages: dict[tuple[int, UUID, str], InboundIdempotencyRecord] = {}

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
        with self._lock:
            key = (tenant_id, runner_id, connection_id)
            lease = RunnerConnectionLease(
                tenant_id=tenant_id,
                runner_id=runner_id,
                pod_id=pod_id,
                connection_id=connection_id,
                status="active",
                lease_expires_at=_ensure_utc(lease_expires_at),
                last_seen_at=_ensure_utc(last_seen_at),
            )
            self._leases[key] = lease
            return lease

    def refresh_connection_lease(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        connection_id: str,
        lease_expires_at: datetime,
        last_seen_at: datetime,
    ) -> RunnerConnectionLease | None:
        with self._lock:
            key = (tenant_id, runner_id, connection_id)
            current = self._leases.get(key)
            if current is None:
                return None
            refreshed = RunnerConnectionLease(
                tenant_id=current.tenant_id,
                runner_id=current.runner_id,
                pod_id=current.pod_id,
                connection_id=current.connection_id,
                status="active",
                lease_expires_at=_ensure_utc(lease_expires_at),
                last_seen_at=_ensure_utc(last_seen_at),
            )
            self._leases[key] = refreshed
            return refreshed

    def release_connection_lease(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        connection_id: str,
        released_at: datetime,
    ) -> bool:
        with self._lock:
            key = (tenant_id, runner_id, connection_id)
            current = self._leases.get(key)
            if current is None:
                return False
            released_at_utc = _ensure_utc(released_at)
            self._leases[key] = RunnerConnectionLease(
                tenant_id=current.tenant_id,
                runner_id=current.runner_id,
                pod_id=current.pod_id,
                connection_id=current.connection_id,
                status="disconnected",
                lease_expires_at=current.lease_expires_at,
                last_seen_at=released_at_utc,
            )
            has_active_lease = any(
                lease.tenant_id == tenant_id
                and lease.runner_id == runner_id
                and lease.status == "active"
                and lease.lease_expires_at > released_at_utc
                for lease in self._leases.values()
            )
            if not has_active_lease:
                self._runner_status[(tenant_id, runner_id)] = ("offline", released_at_utc)
            return True

    def mark_runner_online(self, *, tenant_id: int, runner_id: UUID, last_seen_at: datetime) -> bool:
        with self._lock:
            self._runner_status[(tenant_id, runner_id)] = ("active", _ensure_utc(last_seen_at))
            return True

    def mark_runner_offline(self, *, tenant_id: int, runner_id: UUID, last_seen_at: datetime | None = None) -> bool:
        with self._lock:
            self._runner_status[(tenant_id, runner_id)] = (
                "offline",
                _ensure_utc(last_seen_at) if last_seen_at is not None else None,
            )
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
        with self._lock:
            key = (tenant_id, runner_id, message_id)
            existing = self._outbound_messages.get(key)
            if existing is not None:
                return existing
            normalized_idempotency = _normalize_optional(idempotency_key)
            if normalized_idempotency is not None:
                by_idempotency = self._outbound_messages_by_idempotency.get(
                    (tenant_id, runner_id, normalized_idempotency)
                )
                if by_idempotency is not None:
                    return by_idempotency

            queued = QueuedOutboundMessage(
                id=uuid4(),
                tenant_id=tenant_id,
                runner_id=runner_id,
                message_id=message_id,
                message_type=message_type,
                status="queued",
                payload_json=dict(payload_json) if payload_json is not None else None,
                payload_is_raw=True,
                idempotency_key=normalized_idempotency,
                runtime_job_id=runtime_job_id,
                task_id=task_id,
                correlation_id=_normalize_optional(correlation_id),
                delivery_attempt_count=0,
            )
            self._outbound_messages[key] = queued
            if normalized_idempotency is not None:
                self._outbound_messages_by_idempotency[(tenant_id, runner_id, normalized_idempotency)] = queued
            _metrics.record_outbound_queued()
            return queued

    def claim_queued_outbound_messages(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        pod_id: str,
        connection_id: str,
        max_messages: int,
    ) -> tuple[QueuedOutboundMessage, ...]:
        with self._lock:
            lease = self._leases.get((tenant_id, runner_id, connection_id))
            now = datetime.now(tz=UTC)
            if (
                lease is None
                or lease.status != "active"
                or lease.pod_id != str(pod_id).strip()
                or lease.lease_expires_at <= now
            ):
                return ()
            claimed: list[QueuedOutboundMessage] = []
            for key, message in list(self._outbound_messages.items()):
                if key[0] != tenant_id or key[1] != runner_id:
                    continue
                if message.status not in {"queued", "pending", "retry"}:
                    continue
                dispatching = QueuedOutboundMessage(
                    id=message.id,
                    tenant_id=message.tenant_id,
                    runner_id=message.runner_id,
                    message_id=message.message_id,
                    message_type=message.message_type,
                    status="dispatching",
                    payload_json=message.payload_json,
                    payload_is_raw=message.payload_is_raw,
                    idempotency_key=message.idempotency_key,
                    runtime_job_id=message.runtime_job_id,
                    task_id=message.task_id,
                    correlation_id=message.correlation_id,
                    delivery_attempt_count=message.delivery_attempt_count,
                )
                self._outbound_messages[key] = dispatching
                claimed.append(dispatching)
                if len(claimed) >= max(1, int(max_messages)):
                    break
            return tuple(claimed)

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
        del message_type, idempotency_key, payload_json, runtime_job_id, task_id, correlation_id
        with self._lock:
            key = (tenant_id, runner_id, message_id)
            existing = self._inbound_messages.get(key)
            if existing is not None:
                return InboundIdempotencyRecord(
                    id=existing.id,
                    tenant_id=existing.tenant_id,
                    runner_id=existing.runner_id,
                    message_id=existing.message_id,
                    status=existing.status,
                    error_code=existing.error_code,
                    error_message=existing.error_message,
                    duplicate=True,
                )

            created = InboundIdempotencyRecord(
                id=uuid4(),
                tenant_id=tenant_id,
                runner_id=runner_id,
                message_id=message_id,
                status=str(status).strip() or "accepted",
                error_code=_normalize_optional(error_code),
                error_message=_normalize_optional(error_message),
                duplicate=False,
            )
            self._inbound_messages[key] = created
            return created

    def mark_outbound_message_delivered(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        message_id: str,
    ) -> bool:
        return self._mark_outbound_status(
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
        return self._mark_outbound_status(
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
        return self._mark_outbound_status(
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
        return self._mark_outbound_status(
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
        with self._lock:
            expired_count = 0
            runner_ids_to_check: set[tuple[int, UUID]] = set()
            for key, lease in list(self._leases.items()):
                if lease.status != "active":
                    continue
                if lease.lease_expires_at > now_utc:
                    continue
                self._leases[key] = RunnerConnectionLease(
                    tenant_id=lease.tenant_id,
                    runner_id=lease.runner_id,
                    pod_id=lease.pod_id,
                    connection_id=lease.connection_id,
                    status="disconnected",
                    lease_expires_at=lease.lease_expires_at,
                    last_seen_at=lease.last_seen_at,
                )
                expired_count += 1
                runner_ids_to_check.add((lease.tenant_id, lease.runner_id))

            offline_count = 0
            offline_transitions: list[RunnerOfflineTransition] = []
            for runner_key in runner_ids_to_check:
                has_active = any(
                    lease.tenant_id == runner_key[0]
                    and lease.runner_id == runner_key[1]
                    and lease.status == "active"
                    and lease.lease_expires_at > now_utc
                    for lease in self._leases.values()
                )
                if has_active:
                    continue
                self._runner_status[runner_key] = ("offline", now_utc)
                offline_count += 1
                offline_transitions.append(
                    RunnerOfflineTransition(
                        tenant_id=runner_key[0],
                        runner_id=runner_key[1],
                        last_seen_at=now_utc,
                        reason="stale_connection_lease_expired",
                    )
                )

            return LeaseExpiryResult(
                expired_connection_count=expired_count,
                offline_runner_count=offline_count,
                offline_transitions=tuple(offline_transitions),
            )

    def _mark_outbound_status(
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
        with self._lock:
            key = (tenant_id, runner_id, message_id)
            current = self._outbound_messages.get(key)
            if current is None:
                return False
            should_increment_attempt = increment_attempt and str(current.status or "").strip().lower() != "delivered"
            next_attempt_count = current.delivery_attempt_count + (1 if should_increment_attempt else 0)
            self._outbound_messages[key] = QueuedOutboundMessage(
                id=current.id,
                tenant_id=current.tenant_id,
                runner_id=current.runner_id,
                message_id=current.message_id,
                message_type=current.message_type,
                status=status,
                payload_json=current.payload_json,
                payload_is_raw=current.payload_is_raw,
                idempotency_key=current.idempotency_key,
                runtime_job_id=current.runtime_job_id,
                task_id=current.task_id,
                correlation_id=current.correlation_id,
                delivery_attempt_count=next_attempt_count,
            )
            return True


def _normalize_optional(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
