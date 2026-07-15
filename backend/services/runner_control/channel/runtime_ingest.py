"""Runner websocket-channel runtime-event ingest wrapper.

Purpose: orchestrate authenticated runtime-event channel acceptance, binding
validation, idempotency replay keys, and RuntimeEventService error-envelope
conversion. Scope boundary: this module delegates accepted runtime-event side
effects to RuntimeEventService and does not own generic inbound routing,
artifact ingest, websocket I/O, or runtime-event side-effect internals.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from sqlalchemy.orm import Session

from backend.services.runner_control.audit import RunnerControlAuditService
from backend.services.runner_control.channel.binding_queries import (
    _is_task_assigned_to_runner,
    _lookup_runtime_job_binding,
)
from backend.services.runner_control.channel.errors import (
    _audit_message_accepted,
    _audit_message_rejected,
    _audit_protocol_violation,
    _build_error_envelope,
    _error_code_from_error_envelope,
)
from backend.services.runner_control.channel.types import RunnerChannelHandleResult, RunnerChannelSession
from backend.services.runner_control.coordination import RunnerCoordinationStore
from backend.services.runner_control.message_ingest import (
    InboundMessageLedgerOutcome,
    _runtime_job_transition_status_for_envelope,
    build_runtime_job_transition_idempotency_key,
    record_inbound_message_decision,
)
from backend.services.runner_control.metrics import RunnerControlMetrics
from backend.services.runner_control.protocol import (
    RunnerChannelIdentity,
    RunnerProtocolValidationError as RunnerChannelProtocolValidationError,
    RunnerProtocolValidator,
    remote_runtime_event_result_payload_is_valid,
)
from backend.services.runner_control.runtime_event_service import RuntimeEventService, RuntimeEventServiceError
from runtime_shared.runner_protocol import RunnerEnvelope


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


class RunnerRuntimeEventIngest:
    """Handle runtime-event messages on the runner channel."""

    def __init__(
        self,
        db: Session,
        *,
        coordination_store: RunnerCoordinationStore,
        audit: RunnerControlAuditService,
        metrics: RunnerControlMetrics,
        replace_inbound_accept_with_rejected: _InboundAcceptRejecter,
        touch_connection: Callable[[RunnerChannelSession], None],
    ) -> None:
        self._db = db
        self._coordination = coordination_store
        self._audit = audit
        self._metrics = metrics
        self._replace_inbound_accept_with_rejected = replace_inbound_accept_with_rejected
        self._touch_connection = touch_connection

    def handle_runtime_event(
        self,
        *,
        session: RunnerChannelSession,
        envelope: RunnerEnvelope,
    ) -> RunnerChannelHandleResult:
        runtime_event_binding_error = self._validate_runtime_event_binding(session=session, envelope=envelope)
        if runtime_event_binding_error is not None:
            binding_error_code = _error_code_from_error_envelope(runtime_event_binding_error)
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

        transition_status = _runtime_job_transition_status_for_envelope(envelope=envelope)
        transition_idempotency_key = (
            build_runtime_job_transition_idempotency_key(
                envelope=envelope,
                transition_status=transition_status,
            )
            if transition_status is not None
            else None
        )
        decision = record_inbound_message_decision(
            coordination_store=self._coordination,
            tenant_id=session.tenant_id,
            runner_id=session.runner_id,
            envelope=envelope,
            status="accepted",
            idempotency_key=transition_idempotency_key,
        )
        self._touch_connection(session)
        if decision.should_apply_side_effects:
            try:
                RuntimeEventService(self._db, audit_service=self._audit).apply_runtime_event(
                    tenant_id=session.tenant_id,
                    runner_id=session.runner_id,
                    envelope=envelope,
                )
            except RuntimeEventServiceError as exc:
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
        _audit_message_accepted(
            audit=self._audit,
            session=session,
            envelope=envelope,
            duplicate=decision.record.duplicate,
        )
        self._db.flush()
        return RunnerChannelHandleResult(response_envelopes=())

    def _validate_runtime_event_binding(
        self,
        *,
        session: RunnerChannelSession,
        envelope: RunnerEnvelope,
    ) -> RunnerEnvelope | None:
        validator = RunnerProtocolValidator(
            supported_schema_versions=set(session.allowed_protocol_versions),
            runtime_job_lookup=lambda runtime_job_id: _lookup_runtime_job_binding(
                self._db,
                runtime_job_id,
            ),
            task_assignment_checker=lambda tenant_id, runner_id, task_id: _is_task_assigned_to_runner(
                self._db,
                tenant_id,
                runner_id,
                task_id,
            ),
        )
        try:
            validator.validate_inbound_message(
                identity=RunnerChannelIdentity(
                    tenant_id=str(session.tenant_id),
                    runner_id=str(session.runner_id),
                    runner_status="active",
                    credential_status="active",
                ),
                envelope=envelope,
            )
        except RunnerChannelProtocolValidationError as exc:
            return _build_error_envelope(
                session=session,
                error_code=exc.error_code,
                message=str(exc),
                correlation_id=envelope.correlation_id,
            )

        if not remote_runtime_event_result_payload_is_valid(envelope):
            return _build_error_envelope(
                session=session,
                error_code="RUNNER_PROTOCOL_INVALID",
                message=f"Inbound runtime event `{envelope.type}` requires a result-shaped payload.",
                correlation_id=envelope.correlation_id,
            )
        return None
