"""Runner websocket-channel heartbeat and hello side-effect helpers.

Purpose: apply accepted runner hello and heartbeat side effects for the
authenticated websocket channel. Scope boundary: this module owns runner
metadata updates, heartbeat capacity persistence, stale-runtime reconciliation,
heartbeat audit, presence snapshots, and heartbeat metrics only; it does not
own session open/close lifecycle behavior, inbound routing, ACK handling, or
outbound dispatch.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from backend.models.runner_control import Runner
from backend.services.runner_control.audit import RunnerControlAuditService
from backend.services.runner_control.channel.stale_runtime import _reconcile_stale_runner_runtime_jobs
from backend.services.runner_control.channel.types import RunnerChannelSession
from backend.services.runner_control.coordination import RunnerCoordinationStore
from backend.services.runner_control.metrics import (
    RunnerControlMetrics,
    heartbeat_latency_seconds,
    heartbeat_staleness_seconds,
)
from backend.services.runner_control.message_ingest import capacity_snapshot_from_envelope
from runtime_shared.runner_protocol import RunnerEnvelope

logger = logging.getLogger("backend.services.runner_control.channel_manager")


def _apply_runner_hello(
    *,
    coordination_store: RunnerCoordinationStore,
    metrics: RunnerControlMetrics,
    session: RunnerChannelSession,
    envelope: RunnerEnvelope,
    runner: Runner,
) -> None:
    payload = envelope.payload
    if hasattr(payload, "version"):
        runner.version = str(payload.version).strip()
    if hasattr(payload, "labels"):
        runner.labels_json = dict(payload.labels)
    if hasattr(payload, "capabilities"):
        runner.capabilities_json = list(payload.capabilities)
    coordination_store.mark_runner_online(
        tenant_id=session.tenant_id,
        runner_id=session.runner_id,
        last_seen_at=_utcnow(),
    )
    metrics.record_runner_presence_snapshot(tenant_id=session.tenant_id)


def _apply_runner_heartbeat(
    *,
    db: Session,
    coordination_store: RunnerCoordinationStore,
    audit: RunnerControlAuditService,
    metrics: RunnerControlMetrics,
    session: RunnerChannelSession,
    envelope: RunnerEnvelope,
    runner: Runner,
) -> None:
    latency = heartbeat_latency_seconds(created_at=envelope.created_at, now=_utcnow())
    staleness = heartbeat_staleness_seconds(last_seen_at=runner.last_seen_at, now=_utcnow())
    next_capacity = capacity_snapshot_from_envelope(envelope)
    if next_capacity:
        runner.capacity_json = next_capacity
        _reconcile_stale_runner_runtime_jobs(
            db=db,
            coordination_store=coordination_store,
            session=session,
            capacity=next_capacity,
        )
    audit.emit(
        event_type="runner.heartbeat",
        tenant_id=session.tenant_id,
        runner_id=session.runner_id,
        task_id=envelope.task_id,
        runtime_job_id=envelope.runtime_job_id,
        correlation_id=envelope.correlation_id,
        metadata={
            "message_type": envelope.type,
            "capacity_available_tasks": next_capacity.get("available_tasks"),
            "capacity_active_tasks": next_capacity.get("active_tasks"),
            "capacity_max_active_tasks": next_capacity.get("max_active_tasks"),
        },
    )
    coordination_store.mark_runner_online(
        tenant_id=session.tenant_id,
        runner_id=session.runner_id,
        last_seen_at=_utcnow(),
    )
    metrics.record_runner_presence_snapshot(tenant_id=session.tenant_id)
    metrics.observe_heartbeat_health(latency_seconds=latency, staleness_seconds=staleness)
    logger.info(
        "runner_control.heartbeat_received tenant_id=%s runner_id=%s runtime_job_id=%s task_id=%s message_id=%s correlation_id=%s",
        session.tenant_id,
        session.runner_id,
        envelope.runtime_job_id,
        envelope.task_id,
        envelope.message_id,
        envelope.correlation_id,
    )


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
