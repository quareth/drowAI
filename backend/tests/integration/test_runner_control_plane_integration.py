"""Runner-control failure-matrix integration coverage.

This module exercises multi-component runner-control flows for duplicate
idempotency, stale lease reconciliation, offline assignment handling, and
cross-pod outbound delivery behavior.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
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
from backend.services.runner_control.assignment_service import RunnerAssignmentRequest, RunnerAssignmentService
from backend.services.runner_control.channel.auth import RunnerChannelAuthContext
from backend.services.runner_control.channel_manager import RunnerChannelManager
from backend.services.runner_control.db_coordination import DBRunnerCoordinationStore
from backend.services.runner_control.dispatcher import (
    DispatchAttemptResult,
    RunnerOutboundDispatcher,
    RunnerOutboundTransport,
)
from backend.services.runner_control.registry_service import RunnerRegistryService
from backend.services.runner_control.credentials import RunnerCredentialService


class _AckingTransport(RunnerOutboundTransport):
    async def send(self, envelope, *, timeout_seconds: float) -> DispatchAttemptResult:
        del envelope, timeout_seconds
        return DispatchAttemptResult(delivered=True, acked=True)


class _DeliveredWithoutAckTransport(RunnerOutboundTransport):
    async def send(self, envelope, *, timeout_seconds: float) -> DispatchAttemptResult:
        del envelope, timeout_seconds
        return DispatchAttemptResult(
            delivered=True,
            acked=False,
            timed_out=False,
            error_code="RUNNER_ACK_PENDING",
            error_message="Awaiting asynchronous runner ack.",
            retryable=True,
        )


def _build_session_factory(database_url: str = "sqlite+pysqlite:///:memory:") -> sessionmaker[Session]:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)
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
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _build_session() -> Session:
    return _build_session_factory()()


def _build_shared_sessions(tmp_path: Path, filename: str) -> tuple[Session, Session]:
    database_path = tmp_path / filename
    factory = _build_session_factory(f"sqlite+pysqlite:///{database_path}")
    return factory(), factory()


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
        status="active",
        last_seen_at=datetime.now(tz=UTC),
    )
    db.add(runner)
    db.commit()
    return tenant, runner


def _issue_credential_id(db: Session, *, tenant_id: int, runner_id: uuid.UUID) -> uuid.UUID:
    issued = RunnerCredentialService(db).issue_runner_credential(tenant_id=tenant_id, runner_id=runner_id)
    db.commit()
    return issued.credential_id


def _runner_envelope_json(
    *,
    message_id: str,
    tenant_id: int,
    runner_id: uuid.UUID,
    message_type: str,
    payload: dict[str, object],
) -> str:
    return json.dumps(
        {
            "message_id": message_id,
            "type": message_type,
            "schema_version": "runner_control.v1",
            "tenant_id": str(tenant_id),
            "runner_id": str(runner_id),
            "correlation_id": "corr-runner-control",
            "runtime_job_id": None,
            "task_id": None,
            "created_at": "2026-05-23T12:00:00+00:00",
            "payload": payload,
        }
    )


def test_duplicate_message_id_does_not_duplicate_side_effects() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)

    manager = RunnerChannelManager(db)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1",),
        )
    )

    manager.handle_inbound_json(
        session,
        _runner_envelope_json(
            message_id="hello-1",
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.0.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )

    manager.handle_inbound_json(
        session,
        _runner_envelope_json(
            message_id="heartbeat-1",
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
                    "version": "1.0.0",
                    "capabilities": ["docker"],
                    "labels": {"region": "us-east"},
                }
            },
        ),
    )

    manager.handle_inbound_json(
        session,
        _runner_envelope_json(
            message_id="heartbeat-1",
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
                    "capabilities": ["docker", "kali"],
                    "labels": {"region": "eu-west"},
                }
            },
        ),
    )
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
        "version": "1.0.0",
        "capabilities": ["docker"],
        "labels": {"region": "us-east"},
        "active_runtime_jobs": [],
    }

    inbound_rows = db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "inbound",
            RunnerControlMessage.message_id == "heartbeat-1",
        )
    ).scalars().all()
    assert len(inbound_rows) == 1


def test_offline_runner_assignment_returns_stable_reason_and_does_not_create_runtime_jobs() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    db.add(
        RunnerCredential(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_fingerprint="offline-assignment-fingerprint",
            secret_hash="sha256$offline-assignment-secret",
            status="active",
            expires_at=datetime.now(tz=UTC) + timedelta(days=30),
        )
    )
    runner.last_seen_at = datetime.now(tz=UTC)
    db.commit()

    result = RunnerAssignmentService(db).select_runner(RunnerAssignmentRequest(tenant_id=tenant.id))

    assert result.selection is None
    assert "RUNNER_STALE_OR_OFFLINE" in result.reason_codes
    assert db.execute(select(RuntimeJob)).scalars().all() == []


def test_stale_runtime_job_lease_and_connection_lease_produce_stable_reconciliation_outcomes() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    now = datetime.now(tz=UTC)

    store = DBRunnerCoordinationStore(db, pod_id="pod-a")
    store.claim_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-a",
        connection_id="stale-connection",
        lease_expires_at=now - timedelta(seconds=30),
        last_seen_at=now - timedelta(seconds=45),
    )

    runtime_job = RuntimeJob(
        tenant_id=tenant.id,
        runner_id=runner.id,
        execution_site_id=runner.execution_site_id,
        task_id=None,
        job_type="task.start",
        status="dispatching",
        idempotency_key="lease-expired-job",
        lease_expires_at=now - timedelta(seconds=10),
    )
    db.add(runtime_job)
    db.commit()

    result = RunnerRegistryService(db).reconcile_stale_presence(now=now)
    db.commit()

    assert result.lease_expiry.expired_connection_count == 1
    assert result.lease_expiry.offline_runner_count == 1
    assert len(result.lease_expiry.offline_transitions) == 1
    assert result.lease_expiry.offline_transitions[0].reason == "stale_connection_lease_expired"
    assert result.lost_runtime_job_count == 1

    refreshed_job = db.get(RuntimeJob, runtime_job.id)
    assert refreshed_job is not None
    assert refreshed_job.status == "lost"
    assert refreshed_job.error_code == "RUNNER_LEASE_EXPIRED"


def test_cross_pod_delivery_simulation_marks_outbound_message_acked(tmp_path: Path) -> None:
    enqueue_db, dispatch_db = _build_shared_sessions(tmp_path, "runner-control-cross-pod.db")
    tenant, runner = _seed_runner(enqueue_db)

    enqueue_store = DBRunnerCoordinationStore(enqueue_db, pod_id="pod-a")
    enqueue_store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="cross-pod-1",
        message_type="task.start",
        payload_json={"task": 101},
        idempotency_key="cross-pod-idem-1",
        runtime_job_id=None,
        task_id=101,
        correlation_id="corr-cross-pod-1",
    )
    enqueue_db.commit()

    dispatch_store = DBRunnerCoordinationStore(dispatch_db, pod_id="pod-b")
    now = datetime.now(tz=UTC)
    dispatch_store.claim_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-b",
        connection_id="conn-b",
        lease_expires_at=now + timedelta(seconds=60),
        last_seen_at=now,
    )
    dispatch_db.commit()

    dispatcher = RunnerOutboundDispatcher(
        dispatch_db,
        coordination_store=dispatch_store,
        pod_id="pod-b",
    )
    result = asyncio.run(
        dispatcher.dispatch_for_connection(
            tenant_id=tenant.id,
            runner_id=runner.id,
            connection_id="conn-b",
            transport=_AckingTransport(),
        )
    )
    dispatch_db.commit()

    assert result.claimed_count == 1
    assert result.delivered_count == 1
    assert result.acked_count == 1

    message_row = enqueue_db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "outbound",
            RunnerControlMessage.message_id == "cross-pod-1",
        )
    ).scalar_one()
    assert message_row.status == "acked"
    assert message_row.delivery_attempt_count == 1


def test_probe_dispatch_and_runner_ack_transition_runtime_job_to_acknowledged() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    now = datetime.now(tz=UTC)

    runtime_job = RuntimeJob(
        tenant_id=tenant.id,
        runner_id=runner.id,
        execution_site_id=runner.execution_site_id,
        task_id=None,
        job_type="runner_control.runtime.assignment_probe",
        status="assigned",
        idempotency_key=f"ack-transition-{uuid.uuid4()}",
        lease_expires_at=now + timedelta(minutes=5),
    )
    db.add(runtime_job)
    db.flush()

    store = DBRunnerCoordinationStore(db, pod_id="pod-runner-control")
    store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="probe-msg-1",
        message_type="runner.assignment.probe",
        payload_json={"runtime_job_id": str(runtime_job.id), "operation": "provision_task_runtime"},
        idempotency_key=f"probe:{runtime_job.id}",
        runtime_job_id=runtime_job.id,
        task_id=None,
        correlation_id="corr-probe-1",
    )
    store.claim_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-runner-control",
        connection_id="conn-runner-control",
        lease_expires_at=now + timedelta(seconds=90),
        last_seen_at=now,
    )
    db.commit()

    dispatcher = RunnerOutboundDispatcher(
        db,
        coordination_store=store,
        pod_id="pod-runner-control",
    )
    dispatch_result = asyncio.run(
        dispatcher.dispatch_for_connection(
            tenant_id=tenant.id,
            runner_id=runner.id,
            connection_id="conn-runner-control",
            transport=_DeliveredWithoutAckTransport(),
        )
    )
    assert dispatch_result.delivered_count == 1
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
    manager.handle_inbound_json(
        session,
        _runner_envelope_json(
            message_id="hello-runtime-ack",
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.0.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )
    manager.handle_inbound_json(
        session,
        _runner_envelope_json(
            message_id="ack-runtime-ack",
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.ack",
            payload={
                "acked_message_id": "probe-msg-1",
                "status": "accepted",
                "error_code": None,
            },
        ),
    )
    db.commit()

    refreshed_job = db.get(RuntimeJob, runtime_job.id)
    assert refreshed_job is not None
    assert refreshed_job.status == "acknowledged"
    outbound_message = db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "outbound",
            RunnerControlMessage.message_id == "probe-msg-1",
        )
    ).scalar_one()
    assert outbound_message.status == "acked"


def test_tool_command_rejected_ack_transitions_runtime_job_to_failed_without_tool_result() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    now = datetime.now(tz=UTC)

    runtime_job = RuntimeJob(
        tenant_id=tenant.id,
        runner_id=runner.id,
        execution_site_id=runner.execution_site_id,
        task_id=91,
        job_type="tool.command",
        status="assigned",
        idempotency_key=f"tool-command-reject-{uuid.uuid4()}",
        lease_expires_at=now + timedelta(minutes=5),
    )
    db.add(runtime_job)
    db.flush()

    store = DBRunnerCoordinationStore(db, pod_id="pod-tooling-plane")
    store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="tool-command-msg-1",
        message_type="tool.command",
        payload_json={
            "runtime_job_id": str(runtime_job.id),
            "operation_id": "tool-op-1",
            "task_runtime_job_id": "task-runtime-91",
        },
        idempotency_key=f"tool-command:{runtime_job.id}",
        runtime_job_id=runtime_job.id,
        task_id=91,
        correlation_id="corr-tool-command-1",
    )
    store.claim_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-tooling-plane",
        connection_id="conn-tooling-plane",
        lease_expires_at=now + timedelta(seconds=90),
        last_seen_at=now,
    )
    db.commit()

    dispatcher = RunnerOutboundDispatcher(
        db,
        coordination_store=store,
        pod_id="pod-tooling-plane",
    )
    dispatch_result = asyncio.run(
        dispatcher.dispatch_for_connection(
            tenant_id=tenant.id,
            runner_id=runner.id,
            connection_id="conn-tooling-plane",
            transport=_DeliveredWithoutAckTransport(),
        )
    )
    assert dispatch_result.delivered_count == 1
    db.commit()

    manager = RunnerChannelManager(db)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "tooling_plane.v1"),
        )
    )
    manager.handle_inbound_json(
        session,
        _runner_envelope_json(
            message_id="hello-tool-ack",
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.0.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )
    manager.handle_inbound_json(
        session,
        _runner_envelope_json(
            message_id="ack-tool-command-rejected",
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.ack",
            payload={
                "acked_message_id": "tool-command-msg-1",
                "status": "rejected",
                "error_code": "RUNNER_TOOL_BINDING_INVALID",
            },
        ),
    )
    db.commit()

    refreshed_job = db.get(RuntimeJob, runtime_job.id)
    assert refreshed_job is not None
    assert refreshed_job.status == "failed"
    assert refreshed_job.result_json == {"source": "runner_ack", "ack_status": "rejected"}
    assert refreshed_job.error_code == "RUNNER_TOOL_BINDING_INVALID"
    outbound_message = db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "outbound",
            RunnerControlMessage.message_id == "tool-command-msg-1",
        )
    ).scalar_one()
    assert outbound_message.status == "failed"
    inbound_tool_results = db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "inbound",
            RunnerControlMessage.type == "tool.result",
        )
    ).scalars().all()
    assert inbound_tool_results == []
