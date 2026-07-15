"""Security and failure-path tests for runner control channel behavior.

This module verifies deterministic runner-control security error codes and
confirms unauthorized or malformed traffic does not mutate runtime-job state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.runner_control import (
    ExecutionSite,
    Runner,
    RunnerConnection,
    RunnerControlMessage,
    RunnerCredential,
    RuntimeJob,
)
from backend.models.tenant import Tenant
from backend.services.runner_control.channel.auth import (
    RunnerChannelAuthContext,
    RunnerChannelAuthError,
    RunnerChannelAuthService,
)
from backend.services.runner_control.channel.types import RunnerChannelHandleResult
from backend.services.runner_control.channel_manager import RunnerChannelManager
from backend.services.runner_control.credentials import RunnerCredentialService
from backend.services.runner_control.protocol import (
    RunnerChannelIdentity,
    RunnerProtocolValidationError,
    RunnerProtocolValidator,
)
from backend.services.runner_control.registry_service import RunnerRegistryService
from runtime_shared.runner_protocol import RunnerErrorPayload, parse_runner_envelope


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            Engagement.__table__,
            Task.__table__,
            ExecutionSite.__table__,
            Runner.__table__,
            RunnerCredential.__table__,
            RuntimeJob.__table__,
            RunnerConnection.__table__,
            RunnerControlMessage.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return factory()


def _seed_runner_context(db: Session) -> tuple[Tenant, Runner]:
    tenant = Tenant(slug="tenant-one", name="Tenant One")
    user = User(username="owner", password="hashed")
    db.add_all([tenant, user])
    db.flush()

    engagement = Engagement(
        user_id=user.id,
        tenant_id=tenant.id,
        name="Primary Engagement",
        status="active",
    )
    db.add(engagement)
    db.flush()

    task = Task(
        user_id=user.id,
        tenant_id=tenant.id,
        engagement_id=engagement.id,
        name="Seed Task",
        status="created",
    )
    db.add(task)
    db.flush()

    site = ExecutionSite(
        tenant_id=tenant.id,
        name="Primary Site",
        slug="primary-site",
        status="active",
    )
    db.add(site)
    db.flush()

    runner = Runner(
        tenant_id=tenant.id,
        execution_site_id=site.id,
        name="runner-alpha",
        status="active",
        last_seen_at=datetime.now(tz=UTC),
    )
    db.add(runner)
    db.commit()
    return tenant, runner


def _runner_envelope_json(
    *,
    message_id: str,
    tenant_id: int,
    runner_id: uuid.UUID,
    message_type: str,
    payload: dict[str, object],
    schema_version: str = "runner_control.v1",
    task_id: int | None = None,
) -> str:
    return json.dumps(
        {
            "message_id": message_id,
            "type": message_type,
            "schema_version": schema_version,
            "tenant_id": str(tenant_id),
            "runner_id": str(runner_id),
            "correlation_id": "corr-security",
            "runtime_job_id": None,
            "task_id": task_id,
            "created_at": "2026-05-23T12:00:00+00:00",
            "payload": payload,
        }
    )


def _error_code(result: RunnerChannelHandleResult) -> str:
    assert len(result.response_envelopes) == 1
    payload = result.response_envelopes[0].payload
    assert isinstance(payload, RunnerErrorPayload)
    return payload.error_code


def _issue_credential_id(db: Session, *, tenant_id: int, runner_id: uuid.UUID) -> uuid.UUID:
    issued = RunnerCredentialService(db).issue_runner_credential(tenant_id=tenant_id, runner_id=runner_id)
    db.commit()
    return issued.credential_id


def test_invalid_runner_credential_returns_stable_error_and_does_not_open_connection() -> None:
    db = _build_session()
    tenant, runner = _seed_runner_context(db)
    credential_service = RunnerCredentialService(db)
    credential_service.issue_runner_credential(tenant_id=tenant.id, runner_id=runner.id)
    db.commit()

    auth_service = RunnerChannelAuthService(db)
    with pytest.raises(RunnerChannelAuthError) as error:
        auth_service.authenticate(
            tenant_id_header=str(tenant.id),
            runner_id_header=str(runner.id),
            runner_secret_header="rsec_fake_invalid_secret",
        )

    assert error.value.error_code == "RUNNER_AUTH_INVALID"
    assert db.execute(select(RunnerConnection)).scalars().all() == []


def test_revoked_runner_credential_returns_stable_error_and_does_not_open_connection() -> None:
    db = _build_session()
    tenant, runner = _seed_runner_context(db)
    now = datetime(2026, 5, 23, 15, 0, tzinfo=UTC)
    credential_service = RunnerCredentialService(db, now_provider=lambda: now)
    issued = credential_service.issue_runner_credential(tenant_id=tenant.id, runner_id=runner.id)
    credential_row = db.execute(
        select(RunnerCredential).where(RunnerCredential.id == issued.credential_id)
    ).scalar_one_or_none()
    assert credential_row is not None
    credential_service.revoke_runner_credential(credential_row)
    db.commit()

    auth_service = RunnerChannelAuthService(db, credential_service=credential_service)
    with pytest.raises(RunnerChannelAuthError) as error:
        auth_service.authenticate(
            tenant_id_header=str(tenant.id),
            runner_id_header=str(runner.id),
            runner_secret_header=issued.plaintext_secret,
        )

    assert error.value.error_code == "RUNNER_AUTH_REVOKED"
    assert db.execute(select(RunnerConnection)).scalars().all() == []


@pytest.mark.parametrize(
    ("message_type", "message_payload", "message_id"),
    (
        (
            "runner.heartbeat",
            {
                "capacity": {
                    "active_tasks": 1,
                    "max_active_tasks": 3,
                    "available_tasks": 2,
                    "max_parallel_commands_per_task": 4,
                    "docker_available": True,
                    "runtime_image": "drowai-runtime-local:latest",
                    "runtime_image_available": True,
                    "version": "2.0.0",
                    "capabilities": ["docker"],
                    "labels": {"region": "us-east"},
                }
            },
            "revoked-heartbeat-1",
        ),
        (
            "runner.ack",
            {"acked_message_id": "outbound-live-1", "status": "accepted", "error_code": None},
            "revoked-ack-1",
        ),
    ),
)
def test_revoked_live_runner_session_rejects_operational_messages_and_cannot_restore_active_state(
    message_type: str,
    message_payload: dict[str, object],
    message_id: str,
) -> None:
    db = _build_session()
    tenant, runner = _seed_runner_context(db)
    now = datetime(2026, 5, 23, 16, 0, tzinfo=UTC)
    credential_service = RunnerCredentialService(db, now_provider=lambda: now)
    issued = credential_service.issue_runner_credential(tenant_id=tenant.id, runner_id=runner.id)
    db.commit()

    manager = RunnerChannelManager(db)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=issued.credential_id,
            allowed_protocol_versions=("runner_control.v1",),
        )
    )
    manager.handle_inbound_json(
        session,
        _runner_envelope_json(
            message_id="hello-before-revoke",
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "2.0.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )
    db.commit()

    revoked_count = RunnerRegistryService(db, credential_service=credential_service).revoke_runner_credentials(
        tenant_id=tenant.id,
        runner_id=runner.id,
    )
    db.commit()
    assert revoked_count == 1

    result = manager.handle_inbound_json(
        session,
        _runner_envelope_json(
            message_id=message_id,
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type=message_type,
            payload=message_payload,
        ),
    )
    db.commit()

    assert _error_code(result) == "RUNNER_AUTH_REVOKED"
    assert result.should_close is True
    assert result.close_code == 1008

    refreshed_runner = db.execute(select(Runner).where(Runner.id == runner.id)).scalar_one()
    assert refreshed_runner.status == "revoked"

    inbound_rows = db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "inbound",
            RunnerControlMessage.message_id == message_id,
        )
    ).scalars().all()
    assert inbound_rows == []

    active_connections = db.execute(
        select(RunnerConnection).where(
            RunnerConnection.tenant_id == tenant.id,
            RunnerConnection.runner_id == runner.id,
            RunnerConnection.status == "active",
        )
    ).scalars().all()
    assert active_connections == []


@pytest.mark.parametrize(
    ("message_field", "message_value", "expected_error_code"),
    (
        ("tenant_id", "999999", "RUNNER_IDENTITY_MISMATCH"),
        ("runner_id", str(uuid.uuid4()), "RUNNER_IDENTITY_MISMATCH"),
        ("schema_version", "runner_control.v0", "RUNNER_PROTOCOL_UNSUPPORTED"),
    ),
)
def test_identity_and_protocol_failures_return_stable_error_codes_without_inbound_side_effects(
    message_field: str,
    message_value: str,
    expected_error_code: str,
) -> None:
    db = _build_session()
    tenant, runner = _seed_runner_context(db)
    manager = RunnerChannelManager(db)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1",),
        )
    )

    envelope = {
        "message_id": "identity-failure",
        "type": "runner.hello",
        "schema_version": "runner_control.v1",
        "tenant_id": str(tenant.id),
        "runner_id": str(runner.id),
        "correlation_id": "corr-identity",
        "runtime_job_id": None,
        "task_id": None,
        "created_at": "2026-05-23T12:00:00+00:00",
        "payload": {"version": "1.0.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
    }
    envelope[message_field] = message_value

    result = manager.handle_inbound_json(session, json.dumps(envelope))
    db.commit()

    assert _error_code(result) == expected_error_code
    assert result.should_close is True

    inbound_rows = db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "inbound",
            RunnerControlMessage.message_id == "identity-failure",
        )
    ).scalars().all()
    assert inbound_rows == []


def test_unsupported_message_type_returns_stable_error_code_after_hello_handshake() -> None:
    db = _build_session()
    tenant, runner = _seed_runner_context(db)
    manager = RunnerChannelManager(db)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1",),
        )
    )

    hello = _runner_envelope_json(
        message_id="hello-1",
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_type="runner.hello",
        payload={"version": "1.0.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
    )
    manager.handle_inbound_json(session, hello)

    unsupported = _runner_envelope_json(
        message_id="unsupported-1",
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_type="runner.not-real",
        payload={},
    )
    result = manager.handle_inbound_json(session, unsupported)
    db.commit()

    assert _error_code(result) == "RUNNER_MESSAGE_TYPE_UNKNOWN"


def test_malformed_runner_payload_returns_stable_error_and_does_not_mutate_runtime_job_state() -> None:
    db = _build_session()
    tenant, runner = _seed_runner_context(db)
    now = datetime.now(tz=UTC)

    runtime_job = RuntimeJob(
        tenant_id=tenant.id,
        runner_id=runner.id,
        execution_site_id=runner.execution_site_id,
        task_id=None,
        job_type="task.start",
        status="dispatching",
        idempotency_key="malformed-does-not-mutate",
        lease_expires_at=now + timedelta(minutes=5),
    )
    db.add(runtime_job)
    db.commit()

    manager = RunnerChannelManager(db)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1",),
        )
    )

    result = manager.handle_inbound_json(session, "{not-json")
    db.commit()

    assert _error_code(result) == "RUNNER_PROTOCOL_INVALID"
    refreshed_job = db.get(RuntimeJob, runtime_job.id)
    assert refreshed_job is not None
    assert refreshed_job.status == "dispatching"
    assert refreshed_job.error_code is None


def test_protocol_validator_rejects_unassigned_task_scoped_message_with_stable_error_code() -> None:
    envelope = parse_runner_envelope(
        {
            "message_id": "task-unassigned-1",
            "type": "runner.heartbeat",
            "schema_version": "runner_control.v1",
            "tenant_id": "tenant-one",
            "runner_id": "runner-one",
            "correlation_id": "corr-task-unassigned",
            "runtime_job_id": None,
            "task_id": 42,
            "created_at": "2026-05-23T12:00:00+00:00",
            "payload": {
                "capacity": {
                    "active_tasks": 1,
                    "max_active_tasks": 3,
                    "available_tasks": 2,
                    "max_parallel_commands_per_task": 4,
                    "docker_available": True,
                    "runtime_image": "drowai-runtime-local:latest",
                    "runtime_image_available": True,
                    "version": "2.0.0",
                    "capabilities": ["docker"],
                    "labels": {"site": "hq"},
                }
            },
        }
    )

    validator = RunnerProtocolValidator(
        task_assignment_checker=lambda _tenant_id, _runner_id, _task_id: False,
    )
    identity = RunnerChannelIdentity(
        tenant_id="tenant-one",
        runner_id="runner-one",
        runner_status="active",
        credential_status="active",
    )

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=identity, envelope=envelope)

    assert error.value.error_code == "RUNNER_ASSIGNMENT_NOT_FOUND"
