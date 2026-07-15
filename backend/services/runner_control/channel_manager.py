"""Runner channel facade for runner control-plane sockets.

This module wires authenticated runner channel collaborators and exposes the
public channel manager API used by websocket routers.
"""

from __future__ import annotations

from datetime import timedelta
from functools import partial
import os

from sqlalchemy.orm import Session

from backend.services.data_plane.artifact_manifest_service import ArtifactManifestService
from backend.services.data_plane.artifact_upload_service import ArtifactUploadService
from backend.services.runner_control.audit import RunnerControlAuditEmitter, RunnerControlAuditService
from backend.services.runner_control.channel.artifact_ingest import RunnerArtifactEventIngest
from backend.services.runner_control.channel.auth import RunnerChannelAuthContext
from backend.services.runner_control.channel.inbound import RunnerInboundRouter, replace_inbound_accept_with_rejected
from backend.services.runner_control.channel.lifecycle import RunnerChannelLifecycle
from backend.services.runner_control.channel.runtime_ingest import RunnerRuntimeEventIngest
from backend.services.runner_control.channel.types import RunnerChannelHandleResult, RunnerChannelSession
from backend.services.runner_control.coordination import RunnerCoordinationStore
from backend.services.runner_control.credentials import RunnerCredentialService
from backend.services.runner_control.db_coordination import DBRunnerCoordinationStore
from backend.services.runner_control.dispatcher import DispatcherRunResult, RunnerOutboundDispatcher, RunnerOutboundTransport
from backend.services.runner_control.metrics import RunnerControlMetrics
from backend.services.runner_control.registry_service import RunnerRegistryService


class RunnerChannelManager:
    """Manage authenticated runner channel session lifecycle and heartbeat leases."""

    def __init__(
        self,
        db: Session,
        *,
        lease_ttl_seconds: int = 90,
        coordination_store: RunnerCoordinationStore | None = None,
        audit_emitter: RunnerControlAuditEmitter | None = None,
        metrics: RunnerControlMetrics | None = None,
        artifact_manifest_service: ArtifactManifestService | None = None,
        artifact_upload_service: ArtifactUploadService | None = None,
    ) -> None:
        self._db = db
        self._lease_ttl = timedelta(seconds=max(30, int(lease_ttl_seconds)))
        self._pod_id = str(os.getenv("HOSTNAME") or "local-pod").strip() or "local-pod"
        self._coordination = coordination_store or DBRunnerCoordinationStore(db, pod_id=self._pod_id)
        self._credential_service = RunnerCredentialService(db)
        self._registry = RunnerRegistryService(db, coordination_store=self._coordination)
        self._audit = RunnerControlAuditService(emitter=audit_emitter)
        self._metrics = metrics or RunnerControlMetrics(db)
        self._lifecycle = RunnerChannelLifecycle(
            db,
            lease_ttl=self._lease_ttl,
            pod_id=self._pod_id,
            coordination_store=self._coordination,
            registry=self._registry,
            audit=self._audit,
            metrics=self._metrics,
        )
        replace_inbound_accept_with_rejected_callback = partial(
            replace_inbound_accept_with_rejected,
            db=db,
            coordination_store=self._coordination,
        )
        self._runtime_ingest = RunnerRuntimeEventIngest(
            db,
            coordination_store=self._coordination,
            audit=self._audit,
            metrics=self._metrics,
            replace_inbound_accept_with_rejected=replace_inbound_accept_with_rejected_callback,
            touch_connection=self._lifecycle._touch_connection,
        )
        self._artifact_ingest = RunnerArtifactEventIngest(
            db,
            coordination_store=self._coordination,
            audit=self._audit,
            metrics=self._metrics,
            artifact_manifest_service=artifact_manifest_service or ArtifactManifestService(db),
            artifact_upload_service=artifact_upload_service or ArtifactUploadService(db),
            validate_runtime_event_binding=self._runtime_ingest._validate_runtime_event_binding,
            replace_inbound_accept_with_rejected=replace_inbound_accept_with_rejected_callback,
            touch_connection=self._lifecycle._touch_connection,
        )
        self._inbound = RunnerInboundRouter(
            db,
            coordination_store=self._coordination,
            credential_service=self._credential_service,
            audit=self._audit,
            metrics=self._metrics,
            artifact_ingest=self._artifact_ingest,
            runtime_ingest=self._runtime_ingest,
            touch_connection=self._lifecycle._touch_connection,
            require_runner=self._lifecycle._require_runner,
        )

    def open_session(
        self,
        auth: RunnerChannelAuthContext,
        *,
        remote_ip_address: str | None = None,
    ) -> RunnerChannelSession:
        """Open a new runner channel session and persist initial connection lease."""
        return self._lifecycle.open_session(auth, remote_ip_address=remote_ip_address)

    def handle_inbound_json(
        self,
        session: RunnerChannelSession,
        payload_json: str,
    ) -> RunnerChannelHandleResult:
        """Validate and handle one runner message payload."""
        return self._inbound.handle_inbound_json(session, payload_json)

    def close_session(self, session: RunnerChannelSession) -> None:
        """Mark a runner connection row as disconnected."""
        self._lifecycle.close_session(session)

    async def dispatch_outbound_messages(
        self,
        session: RunnerChannelSession,
        *,
        transport: RunnerOutboundTransport,
        dispatcher: RunnerOutboundDispatcher | None = None,
        max_messages: int = 25,
    ) -> DispatcherRunResult:
        """Dispatch queued outbound messages for the active runner session."""
        resolved_dispatcher = dispatcher or RunnerOutboundDispatcher(
            self._db,
            coordination_store=self._coordination,
            pod_id=self._pod_id,
        )
        return await resolved_dispatcher.dispatch_for_connection(
            tenant_id=session.tenant_id,
            runner_id=session.runner_id,
            connection_id=session.connection_id,
            transport=transport,
            max_messages=max_messages,
        )
