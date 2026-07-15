"""Outbound runner-control message dispatcher for queued cross-pod delivery.

Scope:
- Claims queued outbound control messages for an active runner connection and
  performs transport delivery with timeout and retry/failure policy handling.

Boundaries:
- Uses the coordination-store contract for all durable state transitions.
- Avoids logging raw payloads; logs only stable identifiers and error codes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import os
from typing import Protocol
from uuid import UUID

from backend.services.runner_control.coordination import QueuedOutboundMessage, RunnerCoordinationStore
from backend.services.runner_control.db_coordination import DBRunnerCoordinationStore
from backend.services.runner_control.metrics import RunnerControlMetrics
from backend.services.runner_control.runtime_job_service import RuntimeJobService, RuntimeJobServiceError
from runtime_shared.runner_protocol import (
    RUNNER_PROTOCOL_SCHEMA_VERSION,
    RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION,
    RUNNER_PROTOCOL_TOOLING_PLANE_VERSION,
    RunnerEnvelope,
    RunnerMessageType,
    requires_remote_runtime_schema_version,
    requires_tooling_plane_schema_version,
)

logger = logging.getLogger(__name__)

_REMOTE_RUNTIME_OUTBOUND_REQUEST_MESSAGE_TYPES = frozenset(
    {
        RunnerMessageType.TASK_START,
        RunnerMessageType.TASK_STOP,
        RunnerMessageType.TASK_PAUSE,
        RunnerMessageType.TASK_RESUME,
        RunnerMessageType.TASK_RETIRE,
        RunnerMessageType.RUNTIME_INPUT,
        RunnerMessageType.RUNTIME_STARTUP_PROGRESS,
        RunnerMessageType.RUNTIME_STATUS,
        RunnerMessageType.RUNTIME_LOGS,
        RunnerMessageType.RUNTIME_METRICS,
        RunnerMessageType.RUNTIME_INVENTORY,
        RunnerMessageType.RUNTIME_WORKSPACE_QUERY,
        RunnerMessageType.RUNTIME_WORKSPACE_READ,
        RunnerMessageType.RUNTIME_WORKSPACE_WRITE,
        RunnerMessageType.RUNTIME_WORKSPACE_CLEANUP,
        RunnerMessageType.RUNTIME_ENVIRONMENT_METADATA,
        RunnerMessageType.RUNTIME_VPN_STATUS,
        RunnerMessageType.RUNTIME_VPN_RETRY,
        RunnerMessageType.RUNTIME_VPN_CONFIG,
        RunnerMessageType.TERMINAL_OPEN,
        RunnerMessageType.TERMINAL_INPUT,
        RunnerMessageType.TERMINAL_RESIZE,
        RunnerMessageType.TERMINAL_CLOSE,
    }
)


@dataclass(frozen=True, slots=True)
class DispatchAttemptResult:
    """Transport result for one outbound message delivery attempt."""

    delivered: bool
    acked: bool
    timed_out: bool = False
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = True


@dataclass(frozen=True, slots=True)
class DispatcherRunResult:
    """Summary of one dispatcher poll and delivery run."""

    claimed_count: int
    delivered_count: int
    acked_count: int
    retried_count: int
    failed_count: int
    timed_out_count: int


class RunnerOutboundTransport(Protocol):
    """Transport callback used by dispatcher to deliver one outbound envelope."""

    async def send(self, envelope: RunnerEnvelope, *, timeout_seconds: float) -> DispatchAttemptResult:
        """Send one outbound envelope and return deterministic delivery outcome."""


@dataclass(frozen=True, slots=True)
class _DeliveryPolicy:
    max_attempts: int
    timeout_seconds: float
    offline_mode: str


class RunnerOutboundDispatcher:
    """Dispatch queued outbound messages for active runner channel connections."""

    def __init__(
        self,
        db,
        *,
        coordination_store: RunnerCoordinationStore | None = None,
        pod_id: str | None = None,
        default_max_attempts: int = 3,
        default_timeout_seconds: float = 10.0,
        metrics: RunnerControlMetrics | None = None,
    ) -> None:
        self._db = db
        self._pod_id = str(pod_id or os.getenv("HOSTNAME") or "local-pod").strip() or "local-pod"
        self._coordination = coordination_store or DBRunnerCoordinationStore(db, pod_id=self._pod_id)
        self._default_max_attempts = max(1, int(default_max_attempts))
        self._default_timeout_seconds = max(0.1, float(default_timeout_seconds))
        self._metrics = metrics or RunnerControlMetrics(db)

    async def dispatch_for_connection(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        connection_id: str,
        transport: RunnerOutboundTransport,
        max_messages: int = 25,
    ) -> DispatcherRunResult:
        """Claim and deliver queued messages for a connected runner session."""
        claimed = self._coordination.claim_queued_outbound_messages(
            tenant_id=tenant_id,
            runner_id=runner_id,
            pod_id=self._pod_id,
            connection_id=str(connection_id).strip(),
            max_messages=max(1, int(max_messages)),
        )
        self._commit_dispatch_progress()
        result = DispatcherRunResult(
            claimed_count=len(claimed),
            delivered_count=0,
            acked_count=0,
            retried_count=0,
            failed_count=0,
            timed_out_count=0,
        )
        for message in claimed:
            result = await self._dispatch_one(
                tenant_id=tenant_id,
                runner_id=runner_id,
                message=message,
                transport=transport,
                aggregate=result,
            )
        if result.delivered_count > 0:
            self._metrics.record_outbound_delivered(count=result.delivered_count)
        if result.acked_count > 0:
            self._metrics.record_outbound_acked(count=result.acked_count)
        if result.failed_count > 0:
            self._metrics.record_outbound_failed(count=result.failed_count)
        logger.info(
            "runner_control.dispatch_run tenant_id=%s runner_id=%s runtime_job_id=%s task_id=%s message_id=%s correlation_id=%s claimed=%s delivered=%s acked=%s retried=%s failed=%s timed_out=%s",
            tenant_id,
            runner_id,
            None,
            None,
            None,
            None,
            result.claimed_count,
            result.delivered_count,
            result.acked_count,
            result.retried_count,
            result.failed_count,
            result.timed_out_count,
        )
        self._db.flush()
        return result

    def _commit_dispatch_progress(self) -> None:
        """Release durable dispatch locks before awaiting runner websocket ACKs."""
        commit = getattr(self._db, "commit", None)
        if callable(commit):
            commit()

    async def _dispatch_one(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        message: QueuedOutboundMessage,
        transport: RunnerOutboundTransport,
        aggregate: DispatcherRunResult,
    ) -> DispatcherRunResult:
        policy = _delivery_policy_from_message(
            message=message,
            default_max_attempts=self._default_max_attempts,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        if _requires_raw_dispatch_payload(message) and not message.payload_is_raw:
            error_code = "RUNNER_RAW_DISPATCH_PAYLOAD_UNAVAILABLE"
            error_message = "Raw tool.command dispatch payload is unavailable."
            self._coordination.mark_outbound_message_failed(
                tenant_id=tenant_id,
                runner_id=runner_id,
                message_id=message.message_id,
                error_code=error_code,
                error_message=error_message,
            )
            logger.info(
                "runner_control.dispatch_message_failed outcome=%s error_code=%s tenant_id=%s runner_id=%s runtime_job_id=%s task_id=%s message_id=%s correlation_id=%s",
                "FAILED",
                error_code,
                tenant_id,
                runner_id,
                message.runtime_job_id,
                message.task_id,
                message.message_id,
                message.correlation_id,
            )
            self._transition_runtime_job_from_message(
                tenant_id=tenant_id,
                message=message,
                next_status="failed",
                error_code=error_code,
                error_message=error_message,
                result_json={"source": "dispatcher", "ack_status": "failed"},
            )
            return DispatcherRunResult(
                claimed_count=aggregate.claimed_count,
                delivered_count=aggregate.delivered_count,
                acked_count=aggregate.acked_count,
                retried_count=aggregate.retried_count,
                failed_count=aggregate.failed_count + 1,
                timed_out_count=aggregate.timed_out_count,
            )
        self._transition_runtime_job_from_message(
            tenant_id=tenant_id,
            message=message,
            next_status="dispatching",
        )
        self._commit_dispatch_progress()
        envelope = _build_outbound_envelope(message=message)
        try:
            attempt = await transport.send(envelope, timeout_seconds=policy.timeout_seconds)
        except TimeoutError:
            attempt = DispatchAttemptResult(
                delivered=False,
                acked=False,
                timed_out=True,
                error_code="RUNNER_ACK_TIMEOUT",
                error_message="Runner acknowledgment timeout.",
                retryable=True,
            )
        except Exception:
            logger.error(
                "runner_control.dispatcher.transport_error error_code=%s tenant_id=%s runner_id=%s runtime_job_id=%s task_id=%s message_id=%s correlation_id=%s",
                "RUNNER_DELIVERY_EXCEPTION",
                tenant_id,
                runner_id,
                message.runtime_job_id,
                message.task_id,
                message.message_id,
                message.correlation_id,
            )
            attempt = DispatchAttemptResult(
                delivered=False,
                acked=False,
                timed_out=False,
                error_code="RUNNER_DELIVERY_EXCEPTION",
                error_message="Outbound runner delivery raised an exception.",
                retryable=True,
            )

        delivered_count = aggregate.delivered_count
        acked_count = aggregate.acked_count
        retried_count = aggregate.retried_count
        failed_count = aggregate.failed_count
        timed_out_count = aggregate.timed_out_count

        if attempt.delivered:
            self._coordination.mark_outbound_message_delivered(
                tenant_id=tenant_id,
                runner_id=runner_id,
                message_id=message.message_id,
            )
            logger.info(
                "runner_control.dispatch_message_delivered outcome=%s error_code=%s tenant_id=%s runner_id=%s runtime_job_id=%s task_id=%s message_id=%s correlation_id=%s",
                "DELIVERED",
                None,
                tenant_id,
                runner_id,
                message.runtime_job_id,
                message.task_id,
                message.message_id,
                message.correlation_id,
            )
            self._transition_runtime_job_from_message(
                tenant_id=tenant_id,
                message=message,
                next_status="dispatched",
            )
            delivered_count += 1
            if attempt.acked:
                self._coordination.mark_outbound_message_acked(
                    tenant_id=tenant_id,
                    runner_id=runner_id,
                    message_id=message.message_id,
                )
                logger.info(
                    "runner_control.dispatch_message_acked outcome=%s error_code=%s tenant_id=%s runner_id=%s runtime_job_id=%s task_id=%s message_id=%s correlation_id=%s",
                    "ACKED",
                    None,
                    tenant_id,
                    runner_id,
                    message.runtime_job_id,
                    message.task_id,
                    message.message_id,
                    message.correlation_id,
                )
                self._transition_runtime_job_from_message(
                    tenant_id=tenant_id,
                    message=message,
                    next_status="acknowledged",
                    result_json={"source": "dispatcher", "ack_status": "accepted"},
                )
                acked_count += 1
                return DispatcherRunResult(
                    claimed_count=aggregate.claimed_count,
                    delivered_count=delivered_count,
                    acked_count=acked_count,
                    retried_count=retried_count,
                    failed_count=failed_count,
                    timed_out_count=timed_out_count,
                )

        if attempt.timed_out:
            logger.info(
                "runner_control.dispatch_message_timeout outcome=%s error_code=%s tenant_id=%s runner_id=%s runtime_job_id=%s task_id=%s message_id=%s correlation_id=%s",
                "TIMEOUT",
                _normalize_error_code(attempt.error_code, attempt.timed_out),
                tenant_id,
                runner_id,
                message.runtime_job_id,
                message.task_id,
                message.message_id,
                message.correlation_id,
            )
            timed_out_count += 1

        should_fail = _should_fail_message(
            message=message,
            attempt=attempt,
            policy=policy,
        )
        if should_fail:
            error_code = _normalize_error_code(attempt.error_code, attempt.timed_out)
            self._coordination.mark_outbound_message_failed(
                tenant_id=tenant_id,
                runner_id=runner_id,
                message_id=message.message_id,
                error_code=error_code,
                error_message=_normalize_error_message(attempt.error_message, attempt.timed_out),
            )
            logger.info(
                "runner_control.dispatch_message_failed outcome=%s error_code=%s tenant_id=%s runner_id=%s runtime_job_id=%s task_id=%s message_id=%s correlation_id=%s",
                "FAILED",
                error_code,
                tenant_id,
                runner_id,
                message.runtime_job_id,
                message.task_id,
                message.message_id,
                message.correlation_id,
            )
            self._transition_runtime_job_from_message(
                tenant_id=tenant_id,
                message=message,
                next_status="failed",
                error_code=error_code,
                error_message=_normalize_error_message(attempt.error_message, attempt.timed_out),
                result_json={"source": "dispatcher", "ack_status": "failed"},
            )
            failed_count += 1
        else:
            error_code = _normalize_error_code(attempt.error_code, attempt.timed_out)
            self._coordination.mark_outbound_message_retry(
                tenant_id=tenant_id,
                runner_id=runner_id,
                message_id=message.message_id,
                error_code=error_code,
                error_message=_normalize_error_message(attempt.error_message, attempt.timed_out),
            )
            logger.info(
                "runner_control.dispatch_message_retry outcome=%s error_code=%s tenant_id=%s runner_id=%s runtime_job_id=%s task_id=%s message_id=%s correlation_id=%s",
                "RETRY",
                error_code,
                tenant_id,
                runner_id,
                message.runtime_job_id,
                message.task_id,
                message.message_id,
                message.correlation_id,
            )
            retried_count += 1

        return DispatcherRunResult(
            claimed_count=aggregate.claimed_count,
            delivered_count=delivered_count,
            acked_count=acked_count,
            retried_count=retried_count,
            failed_count=failed_count,
            timed_out_count=timed_out_count,
        )

    def _transition_runtime_job_from_message(
        self,
        *,
        tenant_id: int,
        message: QueuedOutboundMessage,
        next_status: str,
        result_json: dict[str, str] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        runtime_job_id = message.runtime_job_id
        if runtime_job_id is None:
            return
        try:
            RuntimeJobService(self._db).transition_runtime_job(
                tenant_id=tenant_id,
                runtime_job_id=runtime_job_id,
                next_status=next_status,
                result_json=result_json,
                error_code=error_code,
                error_message=error_message,
            )
        except RuntimeJobServiceError as exc:
            # Retries/duplicate ack paths can legitimately replay stale transitions.
            if exc.error_code in {"RUNTIME_JOB_TRANSITION_STALE", "RUNTIME_JOB_TRANSITION_INVALID"}:
                return
            logger.warning(
                "runner_control.runtime_job_transition_failed tenant_id=%s runner_id=%s runtime_job_id=%s task_id=%s message_id=%s correlation_id=%s next_status=%s error_code=%s",
                tenant_id,
                message.runner_id,
                runtime_job_id,
                message.task_id,
                message.message_id,
                message.correlation_id,
                next_status,
                exc.error_code,
            )


def _delivery_policy_from_message(
    *,
    message: QueuedOutboundMessage,
    default_max_attempts: int,
    default_timeout_seconds: float,
) -> _DeliveryPolicy:
    payload = message.payload_json if isinstance(message.payload_json, dict) else {}
    raw_policy = payload.get("delivery_policy")
    policy = raw_policy if isinstance(raw_policy, dict) else {}

    raw_max_attempts = policy.get("max_attempts")
    if isinstance(raw_max_attempts, int) and raw_max_attempts > 0:
        max_attempts = raw_max_attempts
    else:
        max_attempts = default_max_attempts

    raw_timeout = policy.get("timeout_seconds")
    if isinstance(raw_timeout, (float, int)) and raw_timeout > 0:
        timeout_seconds = float(raw_timeout)
    else:
        timeout_seconds = default_timeout_seconds

    offline_mode = str(policy.get("offline", "queue")).strip().lower()
    if offline_mode not in {"queue", "fail"}:
        offline_mode = "queue"

    return _DeliveryPolicy(
        max_attempts=max_attempts,
        timeout_seconds=max(0.1, timeout_seconds),
        offline_mode=offline_mode,
    )


def _build_outbound_envelope(*, message: QueuedOutboundMessage) -> RunnerEnvelope:
    payload = message.payload_json if isinstance(message.payload_json, dict) else {}
    message_type = RunnerMessageType.from_wire(str(message.message_type))
    schema_version = RUNNER_PROTOCOL_SCHEMA_VERSION
    if requires_tooling_plane_schema_version(message_type):
        schema_version = RUNNER_PROTOCOL_TOOLING_PLANE_VERSION
    elif (
        message_type in _REMOTE_RUNTIME_OUTBOUND_REQUEST_MESSAGE_TYPES
        or requires_remote_runtime_schema_version(message_type, payload)
    ):
        schema_version = RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION
    task_id = message.task_id
    if task_id is None and message_type is RunnerMessageType.TASK_RETIRE:
        task_id = _coerce_optional_task_id(payload.get("task_id"))
    return RunnerEnvelope(
        message_id=str(message.message_id),
        message_type=message_type,
        schema_version=schema_version,
        tenant_id=str(message.tenant_id),
        runner_id=str(message.runner_id),
        correlation_id=message.correlation_id,
        runtime_job_id=str(message.runtime_job_id) if message.runtime_job_id is not None else None,
        task_id=task_id,
        created_at=datetime.now(tz=UTC).isoformat(),
        payload=payload,
        raw_message_type=str(message.message_type),
    )


def _requires_raw_dispatch_payload(message: QueuedOutboundMessage) -> bool:
    return RunnerMessageType.from_wire(str(message.message_type)) is RunnerMessageType.TOOL_COMMAND


def _coerce_optional_task_id(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _should_fail_message(
    *,
    message: QueuedOutboundMessage,
    attempt: DispatchAttemptResult,
    policy: _DeliveryPolicy,
) -> bool:
    if attempt.error_code == "RUNNER_OFFLINE":
        return policy.offline_mode == "fail"
    if not attempt.retryable:
        return True
    next_attempt = int(message.delivery_attempt_count) + 1
    return next_attempt >= policy.max_attempts


def _normalize_error_code(value: str | None, timed_out: bool) -> str | None:
    normalized = str(value or "").strip()
    if normalized:
        return normalized[:128]
    if timed_out:
        return "RUNNER_ACK_TIMEOUT"
    return "RUNNER_DELIVERY_FAILED"


def _normalize_error_message(value: str | None, timed_out: bool) -> str | None:
    normalized = str(value or "").strip()
    if normalized:
        return normalized[:512]
    if timed_out:
        return "Runner acknowledgment timeout."
    return "Outbound runner message delivery failed."
