"""Tests for runner-control coordination store lease, queue, and idempotency behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models.runner_control import ExecutionSite, Runner, RunnerConnection, RunnerControlMessage
from backend.models.tenant import Tenant
from backend.services.runner_control.db_coordination import DBRunnerCoordinationStore


def _build_session_factory(database_url: str = "sqlite+pysqlite:///:memory:") -> sessionmaker[Session]:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            ExecutionSite.__table__,
            Runner.__table__,
            RunnerConnection.__table__,
            RunnerControlMessage.__table__,
        ],
    )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _build_session() -> Session:
    return _build_session_factory()()


def _build_shared_sessions(tmp_path: Path) -> tuple[Session, Session]:
    database_path = tmp_path / "coordination-store.db"
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
        status="registered",
    )
    db.add(runner)
    db.commit()
    return tenant, runner


def test_db_coordination_claim_refresh_release_is_idempotent() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    store = DBRunnerCoordinationStore(db, pod_id="pod-a")

    now = datetime.now(tz=UTC)
    lease = store.claim_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-a",
        connection_id="conn-1",
        lease_expires_at=now + timedelta(seconds=90),
        last_seen_at=now,
    )
    assert lease.status == "active"

    refreshed = store.refresh_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        connection_id="conn-1",
        lease_expires_at=now + timedelta(seconds=180),
        last_seen_at=now + timedelta(seconds=10),
    )
    assert refreshed is not None
    assert refreshed.lease_expires_at > lease.lease_expires_at

    assert store.release_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        connection_id="conn-1",
        released_at=now + timedelta(seconds=11),
    )
    assert store.release_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        connection_id="conn-1",
        released_at=now + timedelta(seconds=12),
    )

    db.commit()
    rows = db.execute(
        select(RunnerConnection).where(
            RunnerConnection.tenant_id == tenant.id,
            RunnerConnection.runner_id == runner.id,
            RunnerConnection.connection_id == "conn-1",
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "disconnected"


def test_db_coordination_expire_stale_leases_marks_runner_offline() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    store = DBRunnerCoordinationStore(db, pod_id="pod-a")

    now = datetime.now(tz=UTC)
    store.claim_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-a",
        connection_id="conn-a",
        lease_expires_at=now - timedelta(seconds=10),
        last_seen_at=now - timedelta(seconds=20),
    )
    store.claim_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-b",
        connection_id="conn-b",
        lease_expires_at=now - timedelta(seconds=5),
        last_seen_at=now - timedelta(seconds=15),
    )

    result = store.expire_stale_leases(now=now)
    assert result.expired_connection_count == 2
    assert result.offline_runner_count == 1

    repeat = store.expire_stale_leases(now=now + timedelta(seconds=1))
    assert repeat.expired_connection_count == 0
    assert repeat.offline_runner_count == 0

    db.commit()
    refreshed_runner = db.execute(select(Runner).where(Runner.id == runner.id)).scalar_one()
    assert refreshed_runner.status == "offline"


def test_db_coordination_enqueue_and_claim_outbound_messages_idempotently() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    store = DBRunnerCoordinationStore(db, pod_id="pod-a")

    first = store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="msg-1",
        message_type="task.start",
        payload_json={"task": 1},
        idempotency_key="task-1-dispatch",
        runtime_job_id=uuid.uuid4(),
        task_id=1,
        correlation_id="corr-1",
    )
    duplicate = store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="msg-2",
        message_type="task.start",
        payload_json={"task": 1},
        idempotency_key="task-1-dispatch",
        runtime_job_id=uuid.uuid4(),
        task_id=1,
        correlation_id="corr-2",
    )
    assert duplicate.id == first.id
    assert duplicate.message_id == "msg-1"

    now = datetime.now(tz=UTC)
    store.claim_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-a",
        connection_id="conn-claim",
        lease_expires_at=now + timedelta(seconds=90),
        last_seen_at=now,
    )

    claimed = store.claim_queued_outbound_messages(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-a",
        connection_id="conn-claim",
        max_messages=10,
    )
    assert len(claimed) == 1
    assert claimed[0].status == "dispatching"

    assert store.mark_outbound_message_delivered(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="msg-1",
    )
    assert store.mark_outbound_message_acked(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="msg-1",
    )

    db.commit()
    row = db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "outbound",
            RunnerControlMessage.message_id == "msg-1",
        )
    ).scalar_one()
    assert row.status == "acked"


def test_db_coordination_record_inbound_message_idempotency_replays_duplicate() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    store = DBRunnerCoordinationStore(db, pod_id="pod-a")

    first = store.record_inbound_message_idempotency(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="inbound-1",
        message_type="runner.heartbeat",
        idempotency_key="tenant:runner:inbound-1",
        status="accepted",
        payload_json={"heartbeat": True},
        runtime_job_id=None,
        task_id=None,
        correlation_id="corr-1",
    )
    assert first.duplicate is False
    assert first.status == "accepted"

    duplicate = store.record_inbound_message_idempotency(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="inbound-1",
        message_type="runner.heartbeat",
        idempotency_key="tenant:runner:inbound-1",
        status="rejected",
        payload_json={"heartbeat": False},
        runtime_job_id=None,
        task_id=None,
        correlation_id="corr-2",
    )
    assert duplicate.duplicate is True
    assert duplicate.id == first.id
    assert duplicate.status == "accepted"


def test_db_coordination_record_inbound_message_idempotency_replays_duplicate_business_key() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    store = DBRunnerCoordinationStore(db, pod_id="pod-a")

    first = store.record_inbound_message_idempotency(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="inbound-business-1",
        message_type="runner.heartbeat",
        idempotency_key="runner:heartbeat:slot-1",
        status="accepted",
        payload_json={"heartbeat": True},
        runtime_job_id=None,
        task_id=None,
        correlation_id="corr-1",
    )
    duplicate = store.record_inbound_message_idempotency(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="inbound-business-2",
        message_type="runner.heartbeat",
        idempotency_key="runner:heartbeat:slot-1",
        status="rejected",
        payload_json={"heartbeat": False},
        runtime_job_id=None,
        task_id=None,
        correlation_id="corr-2",
    )

    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert duplicate.id == first.id
    assert duplicate.status == "accepted"

    db.commit()
    inbound_rows = db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "inbound",
            RunnerControlMessage.idempotency_key == "runner:heartbeat:slot-1",
        )
    ).scalars().all()
    assert len(inbound_rows) == 1


def test_db_coordination_record_inbound_message_idempotency_records_stale_rejection() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    store = DBRunnerCoordinationStore(db, pod_id="pod-a")

    first = store.record_inbound_message_idempotency(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="inbound-stale-1",
        message_type="runtime.failed",
        idempotency_key="runtime_job:job-1:transition:failed",
        status="rejected",
        payload_json={"reason": "stale_transition"},
        runtime_job_id=None,
        task_id=42,
        correlation_id="corr-stale-1",
    )
    duplicate = store.record_inbound_message_idempotency(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="inbound-stale-2",
        message_type="runtime.failed",
        idempotency_key="runtime_job:job-1:transition:failed",
        status="accepted",
        payload_json={"reason": "stale_transition"},
        runtime_job_id=None,
        task_id=42,
        correlation_id="corr-stale-2",
    )

    assert first.duplicate is False
    assert first.status == "rejected"
    assert duplicate.duplicate is True
    assert duplicate.id == first.id
    assert duplicate.status == "rejected"


def test_db_coordination_record_inbound_business_key_idempotent_across_sessions(tmp_path: Path) -> None:
    db_a, db_b = _build_shared_sessions(tmp_path)
    tenant, runner = _seed_runner(db_a)
    store_a = DBRunnerCoordinationStore(db_a, pod_id="pod-a")
    store_b = DBRunnerCoordinationStore(db_b, pod_id="pod-b")

    first = store_a.record_inbound_message_idempotency(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="inbound-shared-key-1",
        message_type="runtime.running",
        idempotency_key="runtime_job:job-77:transition:running",
        status="accepted",
        payload_json={"transition": "running"},
        runtime_job_id=None,
        task_id=77,
        correlation_id="corr-77-a",
    )
    db_a.commit()

    duplicate = store_b.record_inbound_message_idempotency(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="inbound-shared-key-2",
        message_type="runtime.running",
        idempotency_key="runtime_job:job-77:transition:running",
        status="rejected",
        payload_json={"transition": "running"},
        runtime_job_id=None,
        task_id=77,
        correlation_id="corr-77-b",
    )
    db_b.commit()

    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert duplicate.id == first.id
    assert duplicate.status == "accepted"

    inbound_rows = db_a.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "inbound",
            RunnerControlMessage.idempotency_key == "runtime_job:job-77:transition:running",
        )
    ).scalars().all()
    assert len(inbound_rows) == 1


def test_db_coordination_claim_outbound_messages_single_winner_across_sessions(tmp_path: Path) -> None:
    db_a, db_b = _build_shared_sessions(tmp_path)
    tenant, runner = _seed_runner(db_a)
    store_a = DBRunnerCoordinationStore(db_a, pod_id="pod-a")
    store_b = DBRunnerCoordinationStore(db_b, pod_id="pod-b")

    store_a.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="msg-queue-1",
        message_type="task.start",
        payload_json={"task": 101},
        idempotency_key="task-101",
        runtime_job_id=None,
        task_id=101,
        correlation_id="corr-101",
    )
    now = datetime.now(tz=UTC)
    store_a.claim_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-a",
        connection_id="conn-a",
        lease_expires_at=now + timedelta(seconds=90),
        last_seen_at=now,
    )
    db_a.commit()

    first_claim = store_a.claim_queued_outbound_messages(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-a",
        connection_id="conn-a",
        max_messages=1,
    )
    db_a.commit()
    second_claim = store_b.claim_queued_outbound_messages(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-b",
        connection_id="conn-b",
        max_messages=1,
    )
    db_b.commit()

    assert len(first_claim) == 1
    assert first_claim[0].message_id == "msg-queue-1"
    assert first_claim[0].status == "dispatching"
    assert second_claim == ()


def test_db_coordination_claim_outbound_messages_requires_active_matching_lease() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    store = DBRunnerCoordinationStore(db, pod_id="pod-a")
    now = datetime.now(tz=UTC)

    store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="msg-requires-lease",
        message_type="task.start",
        payload_json={"task": 55},
        idempotency_key="task-55",
        runtime_job_id=None,
        task_id=55,
        correlation_id="corr-55",
    )

    denied_without_lease = store.claim_queued_outbound_messages(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-a",
        connection_id="conn-a",
        max_messages=1,
    )
    assert denied_without_lease == ()

    store.claim_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-a",
        connection_id="conn-a",
        lease_expires_at=now + timedelta(seconds=90),
        last_seen_at=now,
    )

    denied_wrong_pod = store.claim_queued_outbound_messages(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-b",
        connection_id="conn-a",
        max_messages=1,
    )
    assert denied_wrong_pod == ()

    denied_wrong_connection = store.claim_queued_outbound_messages(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-a",
        connection_id="conn-b",
        max_messages=1,
    )
    assert denied_wrong_connection == ()

    claimed = store.claim_queued_outbound_messages(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-a",
        connection_id="conn-a",
        max_messages=1,
    )
    assert len(claimed) == 1
    assert claimed[0].message_id == "msg-requires-lease"


def test_db_coordination_enqueue_outbound_message_idempotent_across_sessions(tmp_path: Path) -> None:
    db_a, db_b = _build_shared_sessions(tmp_path)
    tenant, runner = _seed_runner(db_a)
    store_a = DBRunnerCoordinationStore(db_a, pod_id="pod-a")
    store_b = DBRunnerCoordinationStore(db_b, pod_id="pod-b")

    first = store_a.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="msg-idempotency-1",
        message_type="task.start",
        payload_json={"task": 77},
        idempotency_key="task-77-dispatch",
        runtime_job_id=None,
        task_id=77,
        correlation_id="corr-77-a",
    )
    db_a.commit()

    duplicate = store_b.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="msg-idempotency-2",
        message_type="task.start",
        payload_json={"task": 77},
        idempotency_key="task-77-dispatch",
        runtime_job_id=None,
        task_id=77,
        correlation_id="corr-77-b",
    )
    db_b.commit()

    assert duplicate.id == first.id
    assert duplicate.message_id == "msg-idempotency-1"

    outbound_rows = db_a.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "outbound",
            RunnerControlMessage.idempotency_key == "task-77-dispatch",
        )
    ).scalars().all()
    assert len(outbound_rows) == 1


def test_db_coordination_expire_stale_leases_converges_across_sessions(tmp_path: Path) -> None:
    db_a, db_b = _build_shared_sessions(tmp_path)
    tenant, runner = _seed_runner(db_a)
    store_a = DBRunnerCoordinationStore(db_a, pod_id="pod-a")
    store_b = DBRunnerCoordinationStore(db_b, pod_id="pod-b")

    now = datetime.now(tz=UTC)
    store_a.claim_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-a",
        connection_id="expired-conn",
        lease_expires_at=now - timedelta(seconds=5),
        last_seen_at=now - timedelta(seconds=10),
    )
    db_a.commit()

    first = store_a.expire_stale_leases(now=now)
    db_a.commit()
    second = store_b.expire_stale_leases(now=now)
    db_b.commit()

    assert first.expired_connection_count == 1
    assert first.offline_runner_count == 1
    assert second.expired_connection_count == 0
    assert second.offline_runner_count == 0
