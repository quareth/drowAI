"""Runner websocket-channel session lifecycle orchestration.

Purpose: open, refresh, reconcile, and close authenticated runner channel
sessions. Scope boundary: this module owns channel lifecycle side effects only;
it delegates terminal disconnect cleanup to terminal_cleanup.py and does not own
inbound routing, ACK handling, heartbeat parsing, artifact ingest, or runtime
event ingest.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import uuid as uuid_lib
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.runner_control import Runner, RunnerConnection
from backend.core.network_utils import normalize_ip_address
from backend.services.runner_control.audit import RunnerControlAuditService
from backend.services.runner_control.channel.auth import RunnerChannelAuthContext, RunnerChannelAuthError
from backend.services.runner_control.channel.terminal_cleanup import _cleanup_runner_terminal_state
from backend.services.runner_control.channel.types import RunnerChannelSession
from backend.services.runner_control.coordination import RunnerCoordinationStore
from backend.services.runner_control.metrics import RunnerControlMetrics
from backend.services.runner_control.registry_service import RunnerRegistryService

logger = logging.getLogger("backend.services.runner_control.channel_manager")


class RunnerChannelLifecycle:
    """Apply open, close, presence, and lease side effects for runner sessions."""

    def __init__(
        self,
        db: Session,
        *,
        lease_ttl: timedelta,
        pod_id: str,
        coordination_store: RunnerCoordinationStore,
        registry: RunnerRegistryService,
        audit: RunnerControlAuditService,
        metrics: RunnerControlMetrics,
    ) -> None:
        self._db = db
        self._lease_ttl = lease_ttl
        self._pod_id = pod_id
        self._coordination = coordination_store
        self._registry = registry
        self._audit = audit
        self._metrics = metrics

    def open_session(
        self,
        auth: RunnerChannelAuthContext,
        *,
        remote_ip_address: str | None = None,
    ) -> RunnerChannelSession:
        """Open a new runner channel session and persist initial connection lease."""
        runner = self._require_runner(tenant_id=auth.tenant_id, runner_id=auth.runner_id)
        existing_connection_count = self._db.execute(
            select(func.count(RunnerConnection.id)).where(
                RunnerConnection.tenant_id == auth.tenant_id,
                RunnerConnection.runner_id == auth.runner_id,
            )
        ).scalar_one()
        now = _utcnow()
        connection_id = str(uuid_lib.uuid4())
        lease_expires_at = now + self._lease_ttl
        self._reconcile_presence(now=now)
        self._coordination.claim_connection_lease(
            tenant_id=auth.tenant_id,
            runner_id=auth.runner_id,
            pod_id=self._pod_id,
            connection_id=connection_id,
            lease_expires_at=lease_expires_at,
            last_seen_at=now,
        )
        normalized_remote_ip = normalize_ip_address(remote_ip_address)
        if normalized_remote_ip:
            connection = self._db.execute(
                select(RunnerConnection).where(
                    RunnerConnection.tenant_id == auth.tenant_id,
                    RunnerConnection.runner_id == auth.runner_id,
                    RunnerConnection.connection_id == connection_id,
                )
            ).scalar_one_or_none()
            if connection is not None:
                connection.remote_ip_address = normalized_remote_ip
        self._db.flush()
        self._audit.emit(
            event_type="runner.connected",
            tenant_id=auth.tenant_id,
            runner_id=auth.runner_id,
            metadata={
                "connection_id": connection_id,
                "pod_id": self._pod_id,
                "runner_status": str(runner.status or "").strip() or None,
            },
        )
        if int(existing_connection_count or 0) > 0:
            self._metrics.record_reconnect()
        self._metrics.record_runner_presence_snapshot(tenant_id=auth.tenant_id)
        logger.info(
            "runner_control.session_opened tenant_id=%s runner_id=%s runtime_job_id=%s task_id=%s message_id=%s correlation_id=%s connection_id=%s pod_id=%s",
            auth.tenant_id,
            auth.runner_id,
            None,
            None,
            None,
            None,
            connection_id,
            self._pod_id,
        )

        return RunnerChannelSession(
            tenant_id=auth.tenant_id,
            runner_id=auth.runner_id,
            credential_id=auth.credential_id,
            connection_id=connection_id,
            allowed_protocol_versions=auth.allowed_protocol_versions,
        )

    def close_session(self, session: RunnerChannelSession) -> None:
        """Mark a runner connection row as disconnected."""
        now = _utcnow()
        self._coordination.release_connection_lease(
            tenant_id=session.tenant_id,
            runner_id=session.runner_id,
            connection_id=session.connection_id,
            released_at=now,
        )
        self._reconcile_presence(now=now)
        runner = self._db.execute(
            select(Runner).where(
                Runner.tenant_id == session.tenant_id,
                Runner.id == session.runner_id,
            )
        ).scalar_one_or_none()
        _cleanup_runner_terminal_state(
            db=self._db,
            tenant_id=session.tenant_id,
            runner_id=session.runner_id,
        )
        if runner is None:
            self._metrics.record_runner_presence_snapshot(tenant_id=session.tenant_id)
            logger.info(
                "runner_control.session_close_skipped_deleted_runner tenant_id=%s runner_id=%s connection_id=%s",
                session.tenant_id,
                session.runner_id,
                session.connection_id,
            )
            self._db.flush()
            return
        self._audit.emit(
            event_type="runner.disconnected",
            tenant_id=session.tenant_id,
            runner_id=session.runner_id,
            metadata={
                "connection_id": session.connection_id,
                "runner_status": str(runner.status or "").strip() or None,
            },
        )
        if str(runner.status or "").strip().lower() == "offline":
            self._audit.emit(
                event_type="runner.offline",
                tenant_id=session.tenant_id,
                runner_id=session.runner_id,
                metadata={
                    "reason": "channel_closed_no_active_lease",
                    "connection_id": session.connection_id,
                },
            )
        self._metrics.record_runner_presence_snapshot(tenant_id=session.tenant_id)
        logger.info(
            "runner_control.session_closed tenant_id=%s runner_id=%s runtime_job_id=%s task_id=%s message_id=%s correlation_id=%s connection_id=%s",
            session.tenant_id,
            session.runner_id,
            None,
            None,
            None,
            None,
            session.connection_id,
        )
        self._db.flush()

    def _reconcile_presence(self, *, now: datetime) -> None:
        result = self._registry.reconcile_stale_presence(now=now)
        touched_tenants = {transition.tenant_id for transition in result.lease_expiry.offline_transitions}
        for tenant_id in touched_tenants:
            self._metrics.record_runner_presence_snapshot(tenant_id=tenant_id)
        for tenant_id in result.runtime_job_transition_tenants:
            self._metrics.record_runtime_job_queue_depth(tenant_id=tenant_id)
        logger.info(
            "runner_control.presence_reconciliation expired_connections=%s offline_runners=%s lost_runtime_jobs=%s expired_runtime_jobs=%s",
            result.lease_expiry.expired_connection_count,
            result.lease_expiry.offline_runner_count,
            result.lost_runtime_job_count,
            result.expired_runtime_job_count,
        )

    def _touch_connection(self, session: RunnerChannelSession) -> None:
        now = _utcnow()
        self._coordination.refresh_connection_lease(
            tenant_id=session.tenant_id,
            runner_id=session.runner_id,
            connection_id=session.connection_id,
            lease_expires_at=now + self._lease_ttl,
            last_seen_at=now,
        )

    def _require_runner(self, *, tenant_id: int, runner_id: UUID) -> Runner:
        runner = self._db.execute(
            select(Runner).where(
                Runner.tenant_id == tenant_id,
                Runner.id == runner_id,
            )
        ).scalar_one_or_none()
        if runner is None:
            raise RunnerChannelAuthError(
                error_code="RUNNER_AUTH_INVALID",
                message="Runner channel authentication failed.",
            )
        return runner


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
