"""Runner websocket-channel artifact ingest wrapper.

Purpose: orchestrate authenticated artifact manifest/upload channel acceptance,
idempotency replay, and channel error-envelope conversion. Scope boundary: this
module delegates accepted artifact side effects to data-plane artifact services
and does not own generic inbound routing, runtime-event ingest, websocket I/O,
or artifact persistence internals.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from backend.services.data_plane.artifact_manifest_service import (
    ArtifactManifestService,
    ArtifactManifestServiceError,
)
from backend.services.data_plane.artifact_upload_service import (
    ArtifactUploadService,
    ArtifactUploadServiceError,
)
from backend.services.runner_control.audit import RunnerControlAuditService
from backend.services.runner_control.channel.errors import (
    _audit_message_accepted,
    _audit_message_rejected,
    _audit_protocol_violation,
    _build_error_envelope,
    _error_code_from_error_envelope,
)
from backend.services.runner_control.channel.types import RunnerChannelHandleResult, RunnerChannelSession
from backend.services.runner_control.coordination import RunnerCoordinationStore
from backend.services.runner_control.message_ingest import InboundMessageLedgerOutcome, record_inbound_message_decision
from backend.services.runner_control.metrics import RunnerControlMetrics
from runtime_shared.runner_protocol import RunnerEnvelope, RunnerErrorPayload, RunnerMessageType


class _BindingValidator(Protocol):
    def __call__(
        self,
        *,
        session: RunnerChannelSession,
        envelope: RunnerEnvelope,
    ) -> RunnerEnvelope | None: ...


class _InboundAcceptRejecter(Protocol):
    def __call__(
        self,
        *,
        session: RunnerChannelSession,
        decision: InboundMessageLedgerOutcome,
        envelope: RunnerEnvelope,
        error_code: str = "RUNNER_PROTOCOL_INVALID",
        error_message: str | None = None,
    ) -> None: ...


class RunnerArtifactEventIngest:
    """Handle artifact manifest/upload messages on the runner channel."""

    def __init__(
        self,
        db: Session,
        *,
        coordination_store: RunnerCoordinationStore,
        audit: RunnerControlAuditService,
        metrics: RunnerControlMetrics,
        artifact_manifest_service: ArtifactManifestService,
        artifact_upload_service: ArtifactUploadService,
        validate_runtime_event_binding: _BindingValidator,
        replace_inbound_accept_with_rejected: _InboundAcceptRejecter,
        touch_connection: Callable[[RunnerChannelSession], None],
    ) -> None:
        self._db = db
        self._coordination = coordination_store
        self._audit = audit
        self._metrics = metrics
        self._artifact_manifest_service = artifact_manifest_service
        self._artifact_upload_service = artifact_upload_service
        self._validate_runtime_event_binding = validate_runtime_event_binding
        self._replace_inbound_accept_with_rejected = replace_inbound_accept_with_rejected
        self._touch_connection = touch_connection

    def handle_artifact_event(
        self,
        *,
        session: RunnerChannelSession,
        envelope: RunnerEnvelope,
    ) -> RunnerChannelHandleResult:
        runtime_event_binding_error = self._validate_runtime_event_binding(session=session, envelope=envelope)
        if runtime_event_binding_error is not None:
            binding_error_code = _error_code_from_error_envelope(runtime_event_binding_error)
            binding_error_message = (
                runtime_event_binding_error.payload.message
                if isinstance(runtime_event_binding_error.payload, RunnerErrorPayload)
                else "Runner artifact message validation failed."
            )
            record_inbound_message_decision(
                coordination_store=self._coordination,
                tenant_id=session.tenant_id,
                runner_id=session.runner_id,
                envelope=envelope,
                status="rejected",
                error_code=binding_error_code,
                error_message=binding_error_message,
            )
            self._metrics.record_protocol_validation_failure()
            _audit_message_rejected(
                audit=self._audit,
                session=session,
                envelope=envelope,
                error_code=binding_error_code,
            )
            _audit_protocol_violation(
                audit=self._audit,
                session=session,
                envelope=envelope,
                error_code=binding_error_code,
            )
            return RunnerChannelHandleResult(response_envelopes=(runtime_event_binding_error,))

        decision = record_inbound_message_decision(
            coordination_store=self._coordination,
            tenant_id=session.tenant_id,
            runner_id=session.runner_id,
            envelope=envelope,
            status="accepted",
        )
        self._touch_connection(session)
        if decision.record.duplicate and decision.record.status != "accepted":
            replay_error_code = str(decision.record.error_code or "").strip() or "RUNNER_MESSAGE_REPLAY_REJECTED"
            replay_error_message = str(decision.record.error_message or "").strip() or (
                "Message replay rejected because an earlier attempt was rejected."
            )
            _audit_message_rejected(
                audit=self._audit,
                session=session,
                envelope=envelope,
                error_code=replay_error_code,
            )
            return RunnerChannelHandleResult(
                response_envelopes=(
                    _build_error_envelope(
                        session=session,
                        error_code=replay_error_code,
                        message=replay_error_message,
                        correlation_id=envelope.correlation_id,
                    ),
                ),
            )

        if decision.should_apply_side_effects:
            runtime_job_id = _parse_runtime_job_id_or_raise(envelope.runtime_job_id)
            task_id = _parse_task_id_or_raise(envelope.task_id)
            try:
                if envelope.message_type is RunnerMessageType.ARTIFACT_MANIFEST:
                    service_result = self._artifact_manifest_service.handle_inbound_message(
                        tenant_id=session.tenant_id,
                        runner_id=session.runner_id,
                        task_id=task_id,
                        runtime_job_id=runtime_job_id,
                        envelope=envelope,
                    )
                else:
                    service_result = self._artifact_upload_service.handle_inbound_message(
                        tenant_id=session.tenant_id,
                        runner_id=session.runner_id,
                        task_id=task_id,
                        runtime_job_id=runtime_job_id,
                        envelope=envelope,
                    )
            except (ArtifactManifestServiceError, ArtifactUploadServiceError) as exc:
                self._replace_inbound_accept_with_rejected(
                    session=session,
                    decision=decision,
                    envelope=envelope,
                    error_code=exc.error_code,
                    error_message=str(exc),
                )
                _audit_message_rejected(
                    audit=self._audit,
                    session=session,
                    envelope=envelope,
                    error_code=exc.error_code,
                )
                self._metrics.record_protocol_validation_failure()
                _audit_protocol_violation(
                    audit=self._audit,
                    session=session,
                    envelope=envelope,
                    error_code=exc.error_code,
                )
                return RunnerChannelHandleResult(
                    response_envelopes=(
                        _build_error_envelope(
                            session=session,
                            error_code=exc.error_code,
                            message=str(exc),
                            correlation_id=envelope.correlation_id,
                        ),
                    ),
                )
        else:
            service_result = None

        _audit_message_accepted(
            audit=self._audit,
            session=session,
            envelope=envelope,
            duplicate=decision.record.duplicate,
        )
        self._db.flush()
        return RunnerChannelHandleResult(
            response_envelopes=service_result.response_envelopes if service_result is not None else ()
        )


def _parse_runtime_job_id_or_raise(raw_runtime_job_id: str | None) -> UUID:
    normalized = str(raw_runtime_job_id or "").strip()
    if not normalized:
        raise ArtifactManifestServiceError(
            error_code="RUNTIME_JOB_NOT_ASSIGNED",
            message="artifact message requires runtime_job_id.",
        )
    try:
        return UUID(normalized)
    except ValueError as exc:
        raise ArtifactManifestServiceError(
            error_code="RUNTIME_JOB_NOT_ASSIGNED",
            message="artifact message runtime_job_id is malformed.",
        ) from exc


def _parse_task_id_or_raise(raw_task_id: int | None) -> int:
    if raw_task_id is None:
        raise ArtifactManifestServiceError(
            error_code="RUNNER_ARTIFACT_TASK_MISMATCH",
            message="artifact message requires task_id.",
        )
    return int(raw_task_id)
