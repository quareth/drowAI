"""Tests for Runner Control runtime-job service tenant safety and transition validation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.runner_control import ExecutionSite, Runner, RuntimeJob
from backend.models.tenant import Tenant
from backend.services.runner_control.runtime_job_service import (
    RuntimeJobCreateRequest,
    RuntimeJobService,
    RuntimeJobServiceError,
)


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
            RuntimeJob.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return factory()


def _seed_tenant_context(db: Session) -> tuple[Tenant, Tenant, User]:
    tenant_one = Tenant(slug="tenant-one", name="Tenant One")
    tenant_two = Tenant(slug="tenant-two", name="Tenant Two")
    user = User(username="owner", password="hashed")
    db.add_all([tenant_one, tenant_two, user])
    db.flush()
    return tenant_one, tenant_two, user


def _seed_task(db: Session, *, tenant: Tenant, user: User, suffix: str) -> Task:
    engagement = Engagement(
        user_id=user.id,
        tenant_id=tenant.id,
        name=f"Engagement {suffix}",
        status="active",
    )
    db.add(engagement)
    db.flush()

    task = Task(
        user_id=user.id,
        tenant_id=tenant.id,
        engagement_id=engagement.id,
        name=f"Task {suffix}",
        status="created",
    )
    db.add(task)
    db.flush()
    return task


def _seed_runner(db: Session, *, tenant: Tenant, suffix: str) -> Runner:
    site = ExecutionSite(
        tenant_id=tenant.id,
        name=f"Site {suffix}",
        slug=f"site-{suffix}",
        status="active",
    )
    db.add(site)
    db.flush()

    runner = Runner(
        tenant_id=tenant.id,
        execution_site_id=site.id,
        name=f"runner-{suffix}",
        status="active",
        last_seen_at=datetime.now(tz=UTC),
    )
    db.add(runner)
    db.flush()
    return runner


def test_create_runtime_job_is_tenant_bound_and_rejects_duplicate_idempotency_key() -> None:
    db = _build_session()
    tenant_one, _tenant_two, user = _seed_tenant_context(db)
    task_one = _seed_task(db, tenant=tenant_one, user=user, suffix="one")
    audit_events: list[dict[str, object]] = []
    service = RuntimeJobService(db, audit_emitter=audit_events.append)

    created = service.create_runtime_job(
        RuntimeJobCreateRequest(
            tenant_id=tenant_one.id,
            task_id=task_one.id,
            job_type="task.start",
            idempotency_key="idem-1",
            payload_json={"runtime": "probe"},
        )
    )
    db.commit()

    assert created.tenant_id == tenant_one.id
    assert created.task_id == task_one.id
    assert created.status == "queued"
    assert [event["event_type"] for event in audit_events] == ["runtime_job.created"]
    assert audit_events[0]["runtime_job_id"] == str(created.id)

    with pytest.raises(RuntimeJobServiceError) as duplicate_error:
        service.create_runtime_job(
            RuntimeJobCreateRequest(
                tenant_id=tenant_one.id,
                task_id=task_one.id,
                job_type="task.start",
                idempotency_key="idem-1",
                payload_json={"runtime": "duplicate"},
            )
        )
    assert duplicate_error.value.error_code == "RUNTIME_JOB_IDEMPOTENCY_CONFLICT"


def test_assign_runtime_job_rejects_runner_or_task_tenant_mismatch() -> None:
    db = _build_session()
    tenant_one, tenant_two, user = _seed_tenant_context(db)
    task_one = _seed_task(db, tenant=tenant_one, user=user, suffix="one")
    task_two = _seed_task(db, tenant=tenant_two, user=user, suffix="two")
    runner_one = _seed_runner(db, tenant=tenant_one, suffix="one")
    runner_two = _seed_runner(db, tenant=tenant_two, suffix="two")
    db.commit()

    audit_events: list[dict[str, object]] = []
    service = RuntimeJobService(db, audit_emitter=audit_events.append)

    job = service.create_runtime_job(
        RuntimeJobCreateRequest(
            tenant_id=tenant_one.id,
            task_id=task_one.id,
            job_type="task.start",
            idempotency_key="assign-ok",
        )
    )

    assigned = service.assign_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        runner_id=runner_one.id,
        lease_expires_at=datetime.now(tz=UTC) + timedelta(minutes=5),
    )
    db.commit()
    assert assigned.runner_id == runner_one.id
    assert assigned.status == "assigned"
    event_types = [event["event_type"] for event in audit_events]
    assert event_types[:3] == ["runtime_job.created", "runtime_job.assigned", "runner.assignment_created"]

    cross_tenant_runner_job = service.create_runtime_job(
        RuntimeJobCreateRequest(
            tenant_id=tenant_one.id,
            task_id=task_one.id,
            job_type="task.start",
            idempotency_key="assign-cross-runner",
        )
    )

    with pytest.raises(RuntimeJobServiceError) as runner_mismatch_error:
        service.assign_runtime_job(
            tenant_id=tenant_one.id,
            runtime_job_id=cross_tenant_runner_job.id,
            runner_id=runner_two.id,
        )
    assert runner_mismatch_error.value.error_code == "RUNNER_TENANT_MISMATCH"

    mismatched_job = RuntimeJob(
        tenant_id=tenant_one.id,
        task_id=task_two.id,
        runner_id=None,
        execution_site_id=None,
        job_type="task.start",
        status="queued",
        idempotency_key=f"task-mismatch-{uuid.uuid4()}",
    )
    db.add(mismatched_job)
    db.commit()

    with pytest.raises(RuntimeJobServiceError) as task_mismatch_error:
        service.assign_runtime_job(
            tenant_id=tenant_one.id,
            runtime_job_id=mismatched_job.id,
            runner_id=runner_one.id,
        )
    assert task_mismatch_error.value.error_code == "RUNTIME_JOB_TASK_TENANT_MISMATCH"


def test_assign_runtime_job_is_idempotent_for_same_runner_and_rejects_reassignment() -> None:
    db = _build_session()
    tenant_one, _tenant_two, user = _seed_tenant_context(db)
    task_one = _seed_task(db, tenant=tenant_one, user=user, suffix="one")
    runner_one = _seed_runner(db, tenant=tenant_one, suffix="one")
    runner_two = _seed_runner(db, tenant=tenant_one, suffix="two")
    db.commit()

    service = RuntimeJobService(db)
    job = service.create_runtime_job(
        RuntimeJobCreateRequest(
            tenant_id=tenant_one.id,
            task_id=task_one.id,
            job_type="task.start",
            idempotency_key="assign-idempotent",
        )
    )

    first_assignment = service.assign_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        runner_id=runner_one.id,
    )
    second_assignment = service.assign_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        runner_id=runner_one.id,
    )
    assert first_assignment.runner_id == runner_one.id
    assert second_assignment.runner_id == runner_one.id
    assert second_assignment.status == "assigned"

    with pytest.raises(RuntimeJobServiceError) as reassignment_error:
        service.assign_runtime_job(
            tenant_id=tenant_one.id,
            runtime_job_id=job.id,
            runner_id=runner_two.id,
        )
    assert reassignment_error.value.error_code == "RUNTIME_JOB_ASSIGNMENT_CONFLICT"


def test_transition_runtime_job_rejects_invalid_and_stale_changes_and_persists_metadata() -> None:
    db = _build_session()
    tenant_one, _tenant_two, user = _seed_tenant_context(db)
    task_one = _seed_task(db, tenant=tenant_one, user=user, suffix="one")
    runner_one = _seed_runner(db, tenant=tenant_one, suffix="one")

    service = RuntimeJobService(db)
    job = service.create_runtime_job(
        RuntimeJobCreateRequest(
            tenant_id=tenant_one.id,
            task_id=task_one.id,
            job_type="task.start",
            idempotency_key="transition-1",
        )
    )
    service.assign_runtime_job(tenant_id=tenant_one.id, runtime_job_id=job.id, runner_id=runner_one.id)

    with pytest.raises(RuntimeJobServiceError) as stale_error:
        service.transition_runtime_job(
            tenant_id=tenant_one.id,
            runtime_job_id=job.id,
            next_status="queued",
        )
    assert stale_error.value.error_code == "RUNTIME_JOB_TRANSITION_STALE"

    with pytest.raises(RuntimeJobServiceError) as invalid_error:
        service.transition_runtime_job(
            tenant_id=tenant_one.id,
            runtime_job_id=job.id,
            next_status="acknowledged",
        )
    assert invalid_error.value.error_code == "RUNTIME_JOB_TRANSITION_INVALID"

    dispatching = service.transition_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        next_status="dispatching",
        payload_json={"dispatch": "start"},
    )
    assert dispatching.status == "dispatching"

    dispatched = service.transition_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        next_status="dispatched",
        result_json={"message_id": "msg-1"},
    )
    assert dispatched.status == "dispatched"

    acknowledged = service.transition_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        next_status="acknowledged",
        result_json={"ack": "ok"},
        error_code="RUNNER_ACK",
        error_message="Delivered and acknowledged.",
    )
    assert acknowledged.status == "acknowledged"
    accepted = service.transition_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        next_status="accepted",
    )
    assert accepted.status == "accepted"

    running = service.transition_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        next_status="running",
    )
    assert running.status == "running"

    succeeded = service.transition_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        next_status="succeeded",
        result_json={"runtime": "started"},
    )
    db.commit()

    assert succeeded.result_json == {"runtime": "started"}
    assert succeeded.error_code == "RUNNER_ACK"
    assert succeeded.error_message == "Delivered and acknowledged."


def test_transition_runtime_job_duplicate_ack_replay_is_idempotent_before_runtime_terminal() -> None:
    db = _build_session()
    tenant_one, _tenant_two, user = _seed_tenant_context(db)
    task_one = _seed_task(db, tenant=tenant_one, user=user, suffix="duplicate-ack")
    runner_one = _seed_runner(db, tenant=tenant_one, suffix="duplicate-ack")

    service = RuntimeJobService(db)
    job = service.create_runtime_job(
        RuntimeJobCreateRequest(
            tenant_id=tenant_one.id,
            task_id=task_one.id,
            job_type="task.start",
            idempotency_key="duplicate-ack-transition",
        )
    )
    service.assign_runtime_job(tenant_id=tenant_one.id, runtime_job_id=job.id, runner_id=runner_one.id)
    service.transition_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        next_status="dispatching",
    )
    service.transition_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        next_status="dispatched",
    )

    first_ack = service.transition_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        next_status="acknowledged",
        result_json={"ack": "accepted"},
    )
    replay_ack = service.transition_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        next_status="acknowledged",
        result_json={"ack": "accepted-replay"},
    )
    db.commit()

    assert first_ack.status == "acknowledged"
    assert replay_ack.status == "acknowledged"
    assert replay_ack.result_json == {"ack": "accepted-replay"}


def test_transition_runtime_job_duplicate_runtime_started_cannot_reopen_succeeded_task_start_job() -> None:
    db = _build_session()
    tenant_one, _tenant_two, user = _seed_tenant_context(db)
    task_one = _seed_task(db, tenant=tenant_one, user=user, suffix="one")
    runner_one = _seed_runner(db, tenant=tenant_one, suffix="one")

    service = RuntimeJobService(db)
    job = service.create_runtime_job(
        RuntimeJobCreateRequest(
            tenant_id=tenant_one.id,
            task_id=task_one.id,
            job_type="task.start",
            idempotency_key="terminal-replay",
        )
    )
    service.assign_runtime_job(tenant_id=tenant_one.id, runtime_job_id=job.id, runner_id=runner_one.id)
    service.transition_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        next_status="dispatching",
    )
    service.transition_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        next_status="dispatched",
    )

    terminal_lease = datetime.now(tz=UTC) + timedelta(minutes=2)
    service.transition_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        next_status="acknowledged",
        result_json={"ack": "ok"},
    )
    service.transition_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        next_status="accepted",
    )
    service.transition_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        next_status="running",
    )
    succeeded = service.transition_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        next_status="succeeded",
        payload_json={"final_payload": "kept"},
        result_json={"final_result": "kept"},
        error_code="ACK_OK",
        error_message="Final metadata",
        lease_expires_at=terminal_lease,
    )
    db.commit()

    with pytest.raises(RuntimeJobServiceError) as stale_error:
        service.transition_runtime_job(
            tenant_id=tenant_one.id,
            runtime_job_id=job.id,
            next_status="running",
            payload_json={"final_payload": "mutated"},
            result_json={"final_result": "mutated"},
            error_code="ACK_MUTATED",
            error_message="Should not overwrite terminal metadata.",
            lease_expires_at=terminal_lease + timedelta(minutes=3),
        )
    assert stale_error.value.error_code == "RUNTIME_JOB_TRANSITION_STALE"

    with pytest.raises(RuntimeJobServiceError) as failed_after_success_error:
        service.transition_runtime_job(
            tenant_id=tenant_one.id,
            runtime_job_id=job.id,
            next_status="failed",
            error_code="RUNTIME_FAILED",
            error_message="Should remain terminal succeeded.",
        )
    assert failed_after_success_error.value.error_code == "RUNTIME_JOB_TRANSITION_STALE"

    persisted = db.get(RuntimeJob, succeeded.id)
    assert persisted is not None
    assert persisted.status == "succeeded"
    assert persisted.payload_json == {"final_payload": "kept"}
    assert persisted.result_json == {"final_result": "kept"}
    assert persisted.error_code == "ACK_OK"
    assert persisted.error_message == "Final metadata"
    assert persisted.lease_expires_at == terminal_lease.replace(tzinfo=None)


def test_transition_runtime_job_out_of_order_stopped_then_started_is_stale() -> None:
    db = _build_session()
    tenant_one, _tenant_two, user = _seed_tenant_context(db)
    task_one = _seed_task(db, tenant=tenant_one, user=user, suffix="stopped-started")
    runner_one = _seed_runner(db, tenant=tenant_one, suffix="stopped-started")

    service = RuntimeJobService(db)
    job = service.create_runtime_job(
        RuntimeJobCreateRequest(
            tenant_id=tenant_one.id,
            task_id=task_one.id,
            job_type="task.stop",
            idempotency_key="stopped-then-started",
        )
    )
    service.assign_runtime_job(tenant_id=tenant_one.id, runtime_job_id=job.id, runner_id=runner_one.id)
    service.transition_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        next_status="dispatching",
    )
    service.transition_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        next_status="dispatched",
    )
    service.transition_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        next_status="acknowledged",
        result_json={"ack": "accepted"},
    )
    service.transition_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        next_status="accepted",
    )
    terminal = service.transition_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=job.id,
        next_status="succeeded",
        payload_json={"event": "runtime.stopped"},
        result_json={"lifecycle_outcome": "stopped"},
        error_code="STOPPED",
        error_message="Runtime stopped successfully.",
    )
    db.commit()

    with pytest.raises(RuntimeJobServiceError) as stale_error:
        service.transition_runtime_job(
            tenant_id=tenant_one.id,
            runtime_job_id=job.id,
            next_status="running",
            payload_json={"event": "runtime.started"},
            result_json={"lifecycle_outcome": "running"},
            error_code="STARTED",
            error_message="Stale runtime.started must not reopen terminal stop.",
        )
    assert stale_error.value.error_code == "RUNTIME_JOB_TRANSITION_STALE"

    persisted = db.get(RuntimeJob, terminal.id)
    assert persisted is not None
    assert persisted.status == "succeeded"
    assert persisted.payload_json == {"event": "runtime.stopped"}
    assert persisted.result_json == {"lifecycle_outcome": "stopped"}
    assert persisted.error_code == "STOPPED"
    assert persisted.error_message == "Runtime stopped successfully."


def test_runtime_job_database_default_status_is_assignable_in_runner_control_lifecycle() -> None:
    db = _build_session()
    tenant_one, _tenant_two, user = _seed_tenant_context(db)
    task_one = _seed_task(db, tenant=tenant_one, user=user, suffix="one")
    runner_one = _seed_runner(db, tenant=tenant_one, suffix="one")
    db.commit()

    runtime_job_id = str(uuid.uuid4())
    db.execute(
        text(
            """
            INSERT INTO runtime_jobs (
                id,
                tenant_id,
                task_id,
                runner_id,
                execution_site_id,
                job_type,
                idempotency_key
            ) VALUES (
                :id,
                :tenant_id,
                :task_id,
                NULL,
                NULL,
                :job_type,
                :idempotency_key
            )
            """
        ),
        {
            "id": runtime_job_id,
            "tenant_id": tenant_one.id,
            "task_id": task_one.id,
            "job_type": "task.start",
            "idempotency_key": f"db-default-{runtime_job_id}",
        },
    )
    db.commit()

    service = RuntimeJobService(db)

    assigned = service.assign_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=uuid.UUID(runtime_job_id),
        runner_id=runner_one.id,
    )
    assert assigned.status == "assigned"

    dispatching = service.transition_runtime_job(
        tenant_id=tenant_one.id,
        runtime_job_id=uuid.UUID(runtime_job_id),
        next_status="dispatching",
    )
    assert dispatching.status == "dispatching"


def test_runtime_job_payload_policy_masks_unredacted_sensitive_values() -> None:
    db = _build_session()
    tenant_one, _tenant_two, user = _seed_tenant_context(db)
    task_one = _seed_task(db, tenant=tenant_one, user=user, suffix="one")
    service = RuntimeJobService(db)

    masked = service.create_runtime_job(
        RuntimeJobCreateRequest(
            tenant_id=tenant_one.id,
            task_id=task_one.id,
            job_type="task.start",
            idempotency_key="payload-secret",
            payload_json={"api_key": "sk-live-raw"},
        )
    )

    accepted = service.create_runtime_job(
        RuntimeJobCreateRequest(
            tenant_id=tenant_one.id,
            task_id=task_one.id,
            job_type="task.start",
            idempotency_key="payload-redacted",
            payload_json={"api_key": "<KEY_SET>", "token": "enc:v1:ciphertext"},
        )
    )
    db.commit()

    assert masked.payload_json == {"api_key": "<DURABLE_SECRET_MASK:secret>"}
    assert accepted.payload_json == {"api_key": "<KEY_SET>", "token": "enc:v1:ciphertext"}
