"""Runner websocket-channel inbound message router.

Purpose: parse authenticated runner websocket payloads, revalidate active
session authorization, enforce hello-first ordering, and route message families
to their focused channel collaborators. Scope boundary: this module owns
inbound channel routing and channel ledger decision replacement only; it
delegates family-specific side effects to ACK, heartbeat, artifact, and runtime
ingest collaborators.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.runner_control import Runner, RunnerControlMessage
from backend.services.runner_control.audit import RunnerControlAuditService
from backend.services.runner_control.channel.ack import (
    _apply_runner_ack,
    _build_runner_ack_idempotency_key,
    _build_runner_ack_observation,
)
from backend.services.runner_control.channel.artifact_ingest import RunnerArtifactEventIngest
from backend.services.runner_control.channel.auth import _validate_session_authorization
from backend.services.runner_control.channel.errors import (
    _audit_message_accepted,
    _audit_message_rejected,
    _audit_protocol_violation,
    _audit_unparsed_message_rejected,
    _build_error_envelope,
    _error_code_from_error_envelope,
)
from backend.services.runner_control.channel.heartbeat import (
    _apply_runner_heartbeat,
    _apply_runner_hello,
)
from backend.services.runner_control.channel.runtime_ingest import RunnerRuntimeEventIngest
from backend.services.runner_control.channel.types import RunnerChannelHandleResult, RunnerChannelSession
from backend.services.runner_control.coordination import RunnerCoordinationStore
from backend.services.runner_control.credentials import RunnerCredentialService
from backend.services.runner_control.message_ingest import (
    InboundMessageLedgerOutcome,
    record_inbound_message_decision,
)
from backend.services.runner_control.metrics import RunnerControlMetrics
from backend.services.runner_control.protocol import (
    _REMOTE_RUNTIME_RUNNER_EVENT_TYPES,
    _RUNNER_ARTIFACT_EVENT_TYPES,
    _RUNNER_RUNTIME_EVENT_TYPES,
)
from runtime_shared.runner_protocol import (
    RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION,
    RunnerEnvelope,
    RunnerMessageType,
    RunnerProtocolUnsupportedSchemaError,
    RunnerProtocolValidationError,
    parse_runner_envelope_json,
)


class RunnerInboundRouter:
    """Route parsed runner channel envelopes to channel-family collaborators."""

    def __init__(
        self,
        db: Session,
        *,
        coordination_store: RunnerCoordinationStore,
        credential_service: RunnerCredentialService,
        audit: RunnerControlAuditService,
        metrics: RunnerControlMetrics,
        artifact_ingest: RunnerArtifactEventIngest,
        runtime_ingest: RunnerRuntimeEventIngest,
        touch_connection: Callable[[RunnerChannelSession], None],
        require_runner: Callable[..., Runner],
    ) -> None:
        self._db = db
        self._coordination = coordination_store
        self._credential_service = credential_service
        self._audit = audit
        self._metrics = metrics
        self._artifact_ingest = artifact_ingest
        self._runtime_ingest = runtime_ingest
        self._touch_connection = touch_connection
        self._require_runner = require_runner

    def handle_inbound_json(self, session: RunnerChannelSession, payload_json: str) -> RunnerChannelHandleResult:
        """Validate and handle one runner message payload."""
        try:
            envelope = parse_runner_envelope_json(payload_json)
        except RunnerProtocolUnsupportedSchemaError:
            self._metrics.record_protocol_validation_failure()
            _audit_unparsed_message_rejected(
                audit=self._audit,
                session=session,
                error_code="RUNNER_PROTOCOL_UNSUPPORTED",
            )
            _audit_protocol_violation(
                audit=self._audit,
                session=session,
                error_code="RUNNER_PROTOCOL_UNSUPPORTED",
                payload_size_bytes=len(payload_json.encode("utf-8")),
            )
            return RunnerChannelHandleResult(
                response_envelopes=(
                    _build_error_envelope(
                        session=session,
                        error_code="RUNNER_PROTOCOL_UNSUPPORTED",
                        message="Unsupported schema version.",
                        correlation_id=None,
                    ),
                ),
                should_close=True,
                close_code=1008,
                close_reason="Unsupported runner protocol schema version.",
            )
        except RunnerProtocolValidationError:
            self._metrics.record_protocol_validation_failure()
            _audit_unparsed_message_rejected(
                audit=self._audit,
                session=session,
                error_code="RUNNER_PROTOCOL_INVALID",
            )
            _audit_protocol_violation(
                audit=self._audit,
                session=session,
                error_code="RUNNER_PROTOCOL_INVALID",
                payload_size_bytes=len(payload_json.encode("utf-8")),
            )
            return RunnerChannelHandleResult(
                response_envelopes=(
                    _build_error_envelope(
                        session=session,
                        error_code="RUNNER_PROTOCOL_INVALID",
                        message="Runner envelope validation failed.",
                        correlation_id=None,
                    ),
                ),
            )

        authorization_error = _validate_session_authorization(
            db=self._db,
            credential_service=self._credential_service,
            session=session,
            correlation_id=envelope.correlation_id,
        )
        if authorization_error is not None:
            authorization_error_code = _error_code_from_error_envelope(authorization_error)
            self._metrics.record_unauthorized_message()
            self._metrics.record_protocol_validation_failure()
            _audit_message_rejected(
                audit=self._audit,
                session=session,
                envelope=envelope,
                error_code=authorization_error_code,
            )
            _audit_protocol_violation(
                audit=self._audit,
                session=session,
                envelope=envelope,
                error_code=authorization_error_code,
            )
            return RunnerChannelHandleResult(
                response_envelopes=(authorization_error,),
                should_close=True,
                close_code=1008,
                close_reason="Runner channel authorization failed.",
            )

        identity_error = self._validate_envelope_identity(session=session, envelope=envelope)
        if identity_error is not None:
            identity_error_code = _error_code_from_error_envelope(identity_error)
            self._metrics.record_unauthorized_message()
            self._metrics.record_protocol_validation_failure()
            _audit_message_rejected(
                audit=self._audit,
                session=session,
                envelope=envelope,
                error_code=identity_error_code,
            )
            _audit_protocol_violation(
                audit=self._audit,
                session=session,
                envelope=envelope,
                error_code=identity_error_code,
            )
            return RunnerChannelHandleResult(
                response_envelopes=(identity_error,),
                should_close=True,
                close_code=1008,
                close_reason="Runner envelope identity mismatch.",
            )

        if not session.hello_received and envelope.message_type is not RunnerMessageType.RUNNER_HELLO:
            _audit_message_rejected(
                audit=self._audit,
                session=session,
                envelope=envelope,
                error_code="RUNNER_HELLO_REQUIRED",
            )
            self._metrics.record_protocol_validation_failure()
            _audit_protocol_violation(
                audit=self._audit,
                session=session,
                envelope=envelope,
                error_code="RUNNER_HELLO_REQUIRED",
            )
            return RunnerChannelHandleResult(
                response_envelopes=(
                    _build_error_envelope(
                        session=session,
                        error_code="RUNNER_HELLO_REQUIRED",
                        message="First runner channel message must be runner.hello.",
                        correlation_id=envelope.correlation_id,
                    ),
                ),
                should_close=True,
                close_code=1008,
                close_reason="runner.hello required before operational messages.",
            )

        if envelope.message_type is RunnerMessageType.RUNNER_HELLO:
            decision = record_inbound_message_decision(
                coordination_store=self._coordination,
                tenant_id=session.tenant_id,
                runner_id=session.runner_id,
                envelope=envelope,
                status="accepted",
            )
            _audit_message_accepted(
                audit=self._audit,
                session=session,
                envelope=envelope,
                duplicate=decision.record.duplicate,
            )
            session.hello_received = True
            self._touch_connection(session)
            if decision.should_apply_side_effects:
                _apply_runner_hello(
                    coordination_store=self._coordination,
                    metrics=self._metrics,
                    session=session,
                    envelope=envelope,
                    runner=self._require_runner(
                        tenant_id=session.tenant_id,
                        runner_id=session.runner_id,
                    ),
                )
            self._db.flush()
            return RunnerChannelHandleResult(response_envelopes=())

        if envelope.message_type is RunnerMessageType.RUNNER_HEARTBEAT:
            decision = record_inbound_message_decision(
                coordination_store=self._coordination,
                tenant_id=session.tenant_id,
                runner_id=session.runner_id,
                envelope=envelope,
                status="accepted",
            )
            _audit_message_accepted(
                audit=self._audit,
                session=session,
                envelope=envelope,
                duplicate=decision.record.duplicate,
            )
            self._touch_connection(session)
            if decision.should_apply_side_effects:
                _apply_runner_heartbeat(
                    db=self._db,
                    coordination_store=self._coordination,
                    audit=self._audit,
                    metrics=self._metrics,
                    session=session,
                    envelope=envelope,
                    runner=self._require_runner(
                        tenant_id=session.tenant_id,
                        runner_id=session.runner_id,
                    ),
                )
            self._db.flush()
            return RunnerChannelHandleResult(response_envelopes=())

        if envelope.message_type in {RunnerMessageType.RUNNER_ACK, RunnerMessageType.ERROR}:
            ack_observation = None
            ack_idempotency_key = None
            if envelope.message_type is RunnerMessageType.RUNNER_ACK:
                ack_idempotency_key = _build_runner_ack_idempotency_key(envelope=envelope)
                ack_observation = _build_runner_ack_observation(envelope=envelope)
            decision = record_inbound_message_decision(
                coordination_store=self._coordination,
                tenant_id=session.tenant_id,
                runner_id=session.runner_id,
                envelope=envelope,
                status="accepted",
                idempotency_key=ack_idempotency_key,
            )
            _audit_message_accepted(
                audit=self._audit,
                session=session,
                envelope=envelope,
                duplicate=decision.record.duplicate,
            )
            self._touch_connection(session)
            if decision.should_apply_side_effects and envelope.message_type is RunnerMessageType.RUNNER_ACK:
                _apply_runner_ack(
                    db=self._db,
                    coordination_store=self._coordination,
                    session=session,
                    envelope=envelope,
                )
            self._db.flush()
            return RunnerChannelHandleResult(
                response_envelopes=(),
                ack_observation=ack_observation,
            )

        if envelope.message_type in {
            RunnerMessageType.TASK_START,
            RunnerMessageType.TASK_STOP,
            RunnerMessageType.TASK_PAUSE,
            RunnerMessageType.TASK_RESUME,
            RunnerMessageType.TOOL_COMMAND,
            RunnerMessageType.TERMINAL_OPEN,
            RunnerMessageType.TERMINAL_INPUT,
            RunnerMessageType.TERMINAL_RESIZE,
            RunnerMessageType.TERMINAL_CLOSE,
            RunnerMessageType.RUNNER_ASSIGNMENT_PROBE,
            RunnerMessageType.RUNNER_CONFIG_UPDATE,
        }:
            _audit_message_rejected(
                audit=self._audit,
                session=session,
                envelope=envelope,
                error_code="RUNNER_CONTROL_NOT_IMPLEMENTED",
            )
            return RunnerChannelHandleResult(
                response_envelopes=(
                    _build_error_envelope(
                        session=session,
                        error_code="RUNNER_CONTROL_NOT_IMPLEMENTED",
                        message=f"Message type `{envelope.type}` is not implemented in runner_control.",
                        correlation_id=envelope.correlation_id,
                    ),
                ),
            )

        if envelope.message_type in _RUNNER_ARTIFACT_EVENT_TYPES:
            return self._artifact_ingest.handle_artifact_event(session=session, envelope=envelope)

        if envelope.message_type in _RUNNER_RUNTIME_EVENT_TYPES:
            return self._runtime_ingest.handle_runtime_event(session=session, envelope=envelope)

        _audit_message_rejected(
            audit=self._audit,
            session=session,
            envelope=envelope,
            error_code="RUNNER_MESSAGE_TYPE_UNKNOWN",
        )
        self._metrics.record_protocol_validation_failure()
        _audit_protocol_violation(
            audit=self._audit,
            session=session,
            envelope=envelope,
            error_code="RUNNER_MESSAGE_TYPE_UNKNOWN",
        )
        return RunnerChannelHandleResult(
            response_envelopes=(
                _build_error_envelope(
                    session=session,
                    error_code="RUNNER_MESSAGE_TYPE_UNKNOWN",
                    message=f"Unsupported runner message type `{envelope.type}`.",
                    correlation_id=envelope.correlation_id,
                ),
            ),
        )

    def _validate_envelope_identity(
        self,
        *,
        session: RunnerChannelSession,
        envelope: RunnerEnvelope,
    ) -> RunnerEnvelope | None:
        if envelope.message_type in _REMOTE_RUNTIME_RUNNER_EVENT_TYPES and envelope.schema_version != RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION:
            return _build_error_envelope(
                session=session,
                error_code="RUNNER_PROTOCOL_UNSUPPORTED",
                message=f"Unsupported schema version `{envelope.schema_version}`.",
                correlation_id=envelope.correlation_id,
            )

        if envelope.schema_version not in set(session.allowed_protocol_versions):
            return _build_error_envelope(
                session=session,
                error_code="RUNNER_PROTOCOL_UNSUPPORTED",
                message=f"Unsupported schema version `{envelope.schema_version}`.",
                correlation_id=envelope.correlation_id,
            )

        if envelope.tenant_id.strip() != str(session.tenant_id) or envelope.runner_id.strip() != str(session.runner_id):
            return _build_error_envelope(
                session=session,
                error_code="RUNNER_IDENTITY_MISMATCH",
                message="Envelope tenant_id/runner_id does not match authenticated identity.",
                correlation_id=envelope.correlation_id,
            )
        return None


def replace_inbound_accept_with_rejected(
    *,
    db: Session,
    coordination_store: RunnerCoordinationStore,
    session: RunnerChannelSession,
    decision: InboundMessageLedgerOutcome,
    envelope: RunnerEnvelope,
    error_code: str = "RUNNER_PROTOCOL_INVALID",
    error_message: str | None = None,
) -> None:
    if not decision.record.duplicate:
        accepted_row = db.execute(
            select(RunnerControlMessage).where(
                RunnerControlMessage.id == decision.record.id,
                RunnerControlMessage.tenant_id == session.tenant_id,
                RunnerControlMessage.runner_id == session.runner_id,
                RunnerControlMessage.direction == "inbound",
            )
        ).scalar_one_or_none()
        if accepted_row is not None:
            db.delete(accepted_row)
            db.flush()

    record_inbound_message_decision(
        coordination_store=coordination_store,
        tenant_id=session.tenant_id,
        runner_id=session.runner_id,
        envelope=envelope,
        status="rejected",
        error_code=error_code,
        error_message=error_message,
    )
