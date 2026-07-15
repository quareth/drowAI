"""Tests for runner-control message idempotency ledger and replay behavior."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models.runner_control import (
    ExecutionSite,
    Runner,
    RunnerConnection,
    RunnerControlMessage,
    RunnerCredential,
    RuntimeJob,
)
from backend.models.tenant import Tenant
from backend.services.runner_control.channel.auth import RunnerChannelAuthContext
from backend.services.runner_control.channel_manager import RunnerChannelManager
from backend.services.runner_control.credentials import RunnerCredentialService
from backend.services.runner_control.db_coordination import DBRunnerCoordinationStore
from backend.services.runner_control.message_ingest import (
    build_runtime_job_transition_idempotency_key,
    is_stale_runtime_job_transition,
)
from runtime_shared.runner_protocol import parse_runner_envelope


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
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


def _seed_runner(db: Session) -> tuple[Tenant, Runner]:
    tenant = Tenant(slug="tenant-one", name="Tenant One")
    db.add(tenant)
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
        status="registered",
    )
    db.add(runner)
    db.commit()
    return tenant, runner


def _issue_credential_id(db: Session, *, tenant_id: int, runner_id: uuid.UUID) -> uuid.UUID:
    issued = RunnerCredentialService(db).issue_runner_credential(tenant_id=tenant_id, runner_id=runner_id)
    db.commit()
    return issued.credential_id


def _envelope_json(
    *,
    message_id: str,
    tenant_id: int,
    runner_id: uuid.UUID,
    message_type: str,
    payload: dict,
) -> str:
    return json.dumps(
        {
            "message_id": message_id,
            "type": message_type,
            "schema_version": "runner_control.v1",
            "tenant_id": str(tenant_id),
            "runner_id": str(runner_id),
            "correlation_id": None,
            "runtime_job_id": None,
            "task_id": None,
            "created_at": "2026-05-23T12:00:00+00:00",
            "payload": payload,
        }
    )


def test_duplicate_heartbeat_message_replays_without_duplicate_side_effects() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
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

    hello = _envelope_json(
        message_id="hello-1",
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_type="runner.hello",
        payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
    )
    manager.handle_inbound_json(session, hello)

    first_heartbeat = _envelope_json(
        message_id="hb-1",
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_type="runner.heartbeat",
        payload={
            "capacity": {
                "active_tasks": 1,
                "max_active_tasks": 4,
                "available_tasks": 3,
                "max_parallel_commands_per_task": 6,
                "docker_available": True,
                "runtime_image": "drowai-runtime-local:latest",
                "runtime_image_available": True,
                "version": "1.9.0",
                "capabilities": ["docker", "kali"],
                "labels": {"region": "us-east", "tier": "gold"},
            }
        },
    )
    duplicate_heartbeat = _envelope_json(
        message_id="hb-1",
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_type="runner.heartbeat",
        payload={
            "capacity": {
                "active_tasks": 4,
                "max_active_tasks": 4,
                "available_tasks": 0,
                "max_parallel_commands_per_task": 8,
                "docker_available": True,
                "runtime_image": "different-image:latest",
                "runtime_image_available": True,
                "version": "2.0.0",
                "capabilities": ["docker"],
                "labels": {"region": "eu-west", "tier": "platinum"},
            }
        },
    )

    manager.handle_inbound_json(session, first_heartbeat)
    manager.handle_inbound_json(session, duplicate_heartbeat)
    db.commit()

    refreshed_runner = db.execute(select(Runner).where(Runner.id == runner.id)).scalar_one()
    assert refreshed_runner.capacity_json == {
        "active_tasks": 1,
        "max_active_tasks": 4,
        "available_tasks": 3,
        "max_parallel_commands_per_task": 6,
        "docker_available": True,
        "runtime_image": "drowai-runtime-local:latest",
        "runtime_image_available": True,
        "version": "1.9.0",
        "capabilities": ["docker", "kali"],
        "labels": {"region": "us-east", "tier": "gold"},
        "active_runtime_jobs": [],
    }

    inbound_rows = db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "inbound",
            RunnerControlMessage.message_id == "hb-1",
        )
    ).scalars().all()
    assert len(inbound_rows) == 1


def test_duplicate_ack_message_does_not_flip_previously_acked_delivery() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    manager = RunnerChannelManager(db)
    store = DBRunnerCoordinationStore(db, pod_id="pod-a")
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1",),
        )
    )

    hello = _envelope_json(
        message_id="hello-ack",
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_type="runner.hello",
        payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
    )
    manager.handle_inbound_json(session, hello)

    store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="outbound-1",
        message_type="task.start",
        payload_json={"task": 1},
        idempotency_key="task-1",
        runtime_job_id=None,
        task_id=1,
        correlation_id="corr-1",
    )

    first_ack = _envelope_json(
        message_id="ack-1",
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_type="runner.ack",
        payload={"acked_message_id": "outbound-1", "status": "accepted", "error_code": None},
    )
    duplicate_ack = _envelope_json(
        message_id="ack-1",
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_type="runner.ack",
        payload={"acked_message_id": "outbound-1", "status": "failed", "error_code": "runner_error"},
    )

    manager.handle_inbound_json(session, first_ack)
    manager.handle_inbound_json(session, duplicate_ack)
    db.commit()

    outbound_row = db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "outbound",
            RunnerControlMessage.message_id == "outbound-1",
        )
    ).scalar_one()
    assert outbound_row.status == "acked"

    inbound_acks = db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "inbound",
            RunnerControlMessage.message_id == "ack-1",
        )
    ).scalars().all()
    assert len(inbound_acks) == 1


def test_duplicate_ack_business_key_replay_with_new_message_id_preserves_acked_delivery() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    manager = RunnerChannelManager(db)
    store = DBRunnerCoordinationStore(db, pod_id="pod-a")
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1",),
        )
    )

    hello = _envelope_json(
        message_id="hello-ack-business-key",
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_type="runner.hello",
        payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
    )
    manager.handle_inbound_json(session, hello)

    store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="outbound-2",
        message_type="task.start",
        payload_json={"task": 2},
        idempotency_key="task-2",
        runtime_job_id=None,
        task_id=2,
        correlation_id="corr-2",
    )

    first_ack = _envelope_json(
        message_id="ack-business-1",
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_type="runner.ack",
        payload={"acked_message_id": "outbound-2", "status": "accepted", "error_code": None},
    )
    replayed_ack_with_new_message_id = _envelope_json(
        message_id="ack-business-2",
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_type="runner.ack",
        payload={"acked_message_id": "outbound-2", "status": "failed", "error_code": "runner_error"},
    )

    manager.handle_inbound_json(session, first_ack)
    manager.handle_inbound_json(session, replayed_ack_with_new_message_id)
    db.commit()

    outbound_row = db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "outbound",
            RunnerControlMessage.message_id == "outbound-2",
        )
    ).scalar_one()
    assert outbound_row.status == "acked"

    inbound_acks = db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "inbound",
            RunnerControlMessage.type == "runner.ack",
        )
    ).scalars().all()
    assert len(inbound_acks) == 1
    assert inbound_acks[0].message_id == "ack-business-1"


def test_runtime_job_transition_stale_detection_and_business_key_shape() -> None:
    envelope = parse_runner_envelope(
        {
            "message_id": "transition-1",
            "type": "runtime.failed",
            "schema_version": "remote_runtime.v1",
            "tenant_id": "1",
            "runner_id": "runner-1",
            "correlation_id": "corr-1",
            "runtime_job_id": str(uuid.uuid4()),
            "task_id": 10,
            "created_at": "2026-05-23T12:00:00+00:00",
            "payload": {
                "operation_id": "op-1",
                "status": "failed",
                "error_code": "RUNTIME_TIMEOUT",
                "error_message": "Runtime timed out.",
                "result": {"runtime_job_id": "job-1", "task_id": 10, "workspace_id": "task-10"},
            },
        }
    )

    assert is_stale_runtime_job_transition(current_status="dispatched", next_status="dispatching") is True
    assert is_stale_runtime_job_transition(current_status="failed", next_status="dispatched") is True
    assert is_stale_runtime_job_transition(current_status="dispatching", next_status="failed") is False
    assert is_stale_runtime_job_transition(current_status="acknowledged", next_status="dispatched") is True
    assert is_stale_runtime_job_transition(current_status="acknowledged", next_status="acknowledged") is False
    assert is_stale_runtime_job_transition(current_status="running", next_status="running") is False
    assert is_stale_runtime_job_transition(current_status="running", next_status="succeeded") is False
    assert is_stale_runtime_job_transition(current_status="succeeded", next_status="running") is True
    assert is_stale_runtime_job_transition(current_status="succeeded", next_status="failed") is True

    business_key = build_runtime_job_transition_idempotency_key(
        envelope=envelope,
        transition_status="failed",
    )
    assert business_key is not None
    assert business_key.endswith(":transition:failed")

    running_key = build_runtime_job_transition_idempotency_key(
        envelope=envelope,
        transition_status="running",
    )
    assert running_key is not None
    assert running_key.endswith(":transition:running")
