"""Tests for outbound runner-control dispatcher delivery, timeout, and offline policy."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models.runner_control import ExecutionSite, Runner, RunnerConnection, RunnerControlMessage, RuntimeJob
from backend.models.tenant import Tenant
from backend.services.runner_control import db_coordination as db_coordination_module
from backend.services.runner_control.db_coordination import DBRunnerCoordinationStore
from backend.services.runner_control.dispatcher import (
    DispatchAttemptResult,
    RunnerOutboundDispatcher,
    RunnerOutboundTransport,
)
from backend.services.runner_control.runtime_job_service import RuntimeJobCreateRequest, RuntimeJobService
from runtime_shared.runner_protocol import (
    RUNNER_PROTOCOL_RUNNER_CONTROL_VERSION,
    RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION,
    RUNNER_PROTOCOL_TOOLING_PLANE_VERSION,
)


class _MetricsStub:
    def __init__(self) -> None:
        self.delivered = 0
        self.acked = 0
        self.failed = 0

    def record_outbound_delivered(self, *, count: int = 1) -> None:
        self.delivered += int(count)

    def record_outbound_acked(self, *, count: int = 1) -> None:
        self.acked += int(count)

    def record_outbound_failed(self, *, count: int = 1) -> None:
        self.failed += int(count)


class _FakeTransport(RunnerOutboundTransport):
    def __init__(self, outcomes: list[DispatchAttemptResult]) -> None:
        self._outcomes = outcomes
        self.timeouts: list[float] = []
        self.envelopes = []

    async def send(self, envelope, *, timeout_seconds: float) -> DispatchAttemptResult:
        self.envelopes.append(envelope)
        self.timeouts.append(timeout_seconds)
        if not self._outcomes:
            return DispatchAttemptResult(
                delivered=False,
                acked=False,
                error_code="RUNNER_DELIVERY_FAILED",
                error_message="No configured transport outcome.",
                retryable=True,
            )
        return self._outcomes.pop(0)


class _CommitAwareTransport(RunnerOutboundTransport):
    def __init__(self, commit_events: list[str]) -> None:
        self._commit_events = commit_events
        self.commits_seen_before_send = 0

    async def send(self, envelope, *, timeout_seconds: float) -> DispatchAttemptResult:
        del envelope, timeout_seconds
        self.commits_seen_before_send = len(self._commit_events)
        return DispatchAttemptResult(delivered=True, acked=True)


class _RaisingTransport(RunnerOutboundTransport):
    def __init__(self, *, message: str) -> None:
        self._message = message

    async def send(self, envelope, *, timeout_seconds: float) -> DispatchAttemptResult:
        del envelope, timeout_seconds
        raise RuntimeError(self._message)


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
            RuntimeJob.__table__,
        ],
    )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _build_shared_sessions(tmp_path: Path) -> tuple[Session, Session]:
    database_path = tmp_path / "dispatcher.db"
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


def _tool_command_payload(secret: str, *, command_id: str = "cmd-secret-dispatch") -> dict:
    return {
        "operation_id": f"send_tool_command:{command_id}",
        "workspace_id": "task-202",
        "task_runtime_job_id": "runtime-task-202",
        "runtime_image": "drowai-runtime-local:latest",
        "tool": "shell.exec",
        "command": f"curl -H 'Authorization: Bearer {secret}' http://example.test",
        "cwd": "/workspace",
        "env": {"API_TOKEN": secret},
        "command_id": command_id,
        "timeout_seconds": 10.0,
        "timeout_policy": {},
        "route_policy": {},
        "delivery_policy": {"max_attempts": 1},
        "params": {"password": secret},
    }


def test_dispatcher_delivers_cross_pod_enqueued_message_and_records_ack(tmp_path: Path, caplog) -> None:
    enqueue_db, dispatch_db = _build_shared_sessions(tmp_path)
    tenant, runner = _seed_runner(enqueue_db)

    enqueue_store = DBRunnerCoordinationStore(enqueue_db, pod_id="pod-a")
    secret = "dispatch-ack-secret-101"
    runtime_job_id = uuid.uuid4()
    enqueue_db.add(
        RuntimeJob(
            id=runtime_job_id,
            tenant_id=tenant.id,
            task_id=101,
            runner_id=runner.id,
            execution_site_id=runner.execution_site_id,
            job_type="runner_control.runtime.assignment_probe",
            status="assigned",
            idempotency_key=f"dispatch-ack-{runtime_job_id}",
        )
    )
    enqueue_store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="outbound-1",
        message_type="task.start",
        payload_json={"task": 101, "secret": secret},
        idempotency_key="task-101",
        runtime_job_id=runtime_job_id,
        task_id=101,
        correlation_id="corr-101",
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

    metrics = _MetricsStub()
    dispatcher = RunnerOutboundDispatcher(
        dispatch_db,
        coordination_store=dispatch_store,
        pod_id="pod-b",
        metrics=metrics,
    )
    transport = _FakeTransport([DispatchAttemptResult(delivered=True, acked=True)])
    caplog.set_level("INFO")
    result = asyncio.run(
        dispatcher.dispatch_for_connection(
            tenant_id=tenant.id,
            runner_id=runner.id,
            connection_id="conn-b",
            transport=transport,
        )
    )
    dispatch_db.commit()

    assert result.claimed_count == 1
    assert result.delivered_count == 1
    assert result.acked_count == 1
    assert result.retried_count == 0
    assert result.failed_count == 0
    assert metrics.delivered == 1
    assert metrics.acked == 1
    assert metrics.failed == 0

    row = enqueue_db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "outbound",
            RunnerControlMessage.message_id == "outbound-1",
        )
    ).scalar_one()
    assert row.status == "acked"
    assert row.delivery_attempt_count == 1
    runtime_job = enqueue_db.execute(select(RuntimeJob).where(RuntimeJob.id == runtime_job_id)).scalar_one()
    assert runtime_job.status == "acknowledged"
    assert "runner_control.dispatch_message_delivered" in caplog.text
    assert "runner_control.dispatch_message_acked" in caplog.text
    assert f"tenant_id={tenant.id}" in caplog.text
    assert f"runner_id={runner.id}" in caplog.text
    assert f"runtime_job_id={runtime_job_id}" in caplog.text
    assert "task_id=101" in caplog.text
    assert "message_id=outbound-1" in caplog.text
    assert "correlation_id=corr-101" in caplog.text
    assert secret not in caplog.text


def test_tool_command_dispatch_uses_raw_payload_while_durable_rows_are_masked(tmp_path: Path) -> None:
    enqueue_db, dispatch_db = _build_shared_sessions(tmp_path)
    tenant, runner = _seed_runner(enqueue_db)
    secret = "phase1-runner-password-202"
    payload = _tool_command_payload(secret)

    runtime_job_service = RuntimeJobService(enqueue_db, audit_emitter=lambda event: None)
    runtime_job = runtime_job_service.create_runtime_job(
        RuntimeJobCreateRequest(
            tenant_id=tenant.id,
            job_type="tool.command",
            idempotency_key="tool-command-raw-dispatch",
            payload_json=payload,
            correlation_id="corr-tool-command-raw",
        )
    )
    runtime_job_service.assign_runtime_job(
        tenant_id=tenant.id,
        runtime_job_id=runtime_job.id,
        runner_id=runner.id,
    )

    enqueue_store = DBRunnerCoordinationStore(enqueue_db, pod_id="pod-a")
    enqueue_store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="tool-command-raw-dispatch",
        message_type="tool.command",
        payload_json=payload,
        idempotency_key="tool-command-raw-dispatch",
        runtime_job_id=runtime_job.id,
        task_id=202,
        correlation_id="corr-tool-command-raw",
    )
    enqueue_db.commit()

    durable_message = enqueue_db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.message_id == "tool-command-raw-dispatch",
        )
    ).scalar_one()
    durable_runtime_job = enqueue_db.execute(select(RuntimeJob).where(RuntimeJob.id == runtime_job.id)).scalar_one()
    serialized_message = str(durable_message.payload_json)
    serialized_job = str(durable_runtime_job.payload_json)
    assert secret not in serialized_message
    assert secret not in serialized_job
    assert "<DURABLE_SECRET_MASK:" in serialized_message
    assert "<DURABLE_SECRET_MASK:" in serialized_job

    dispatch_store = DBRunnerCoordinationStore(dispatch_db, pod_id="pod-b")
    now = datetime.now(tz=UTC)
    dispatch_store.claim_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-b",
        connection_id="conn-tool-command-raw",
        lease_expires_at=now + timedelta(seconds=60),
        last_seen_at=now,
    )
    dispatch_db.commit()

    dispatcher = RunnerOutboundDispatcher(dispatch_db, coordination_store=dispatch_store, pod_id="pod-b")
    transport = _FakeTransport([DispatchAttemptResult(delivered=True, acked=True)])
    result = asyncio.run(
        dispatcher.dispatch_for_connection(
            tenant_id=tenant.id,
            runner_id=runner.id,
            connection_id="conn-tool-command-raw",
            transport=transport,
        )
    )
    dispatch_db.commit()

    assert result.claimed_count == 1
    assert result.delivered_count == 1
    assert result.acked_count == 1
    assert len(transport.envelopes) == 1
    envelope_payload = transport.envelopes[0].payload
    assert envelope_payload["command"].endswith(f"Bearer {secret}' http://example.test")
    assert envelope_payload["env"]["API_TOKEN"] == secret
    assert envelope_payload["params"]["password"] == secret


def test_tool_command_dispatch_fails_closed_when_raw_payload_cache_missing(tmp_path: Path) -> None:
    enqueue_db, dispatch_db = _build_shared_sessions(tmp_path)
    tenant, runner = _seed_runner(enqueue_db)
    secret = "phase1-runner-cache-miss-password-202"
    payload = _tool_command_payload(secret, command_id="cmd-cache-miss")

    runtime_job_service = RuntimeJobService(enqueue_db, audit_emitter=lambda event: None)
    runtime_job = runtime_job_service.create_runtime_job(
        RuntimeJobCreateRequest(
            tenant_id=tenant.id,
            job_type="tool.command",
            idempotency_key="tool-command-cache-miss",
            payload_json=payload,
            correlation_id="corr-tool-command-cache-miss",
        )
    )
    runtime_job_service.assign_runtime_job(
        tenant_id=tenant.id,
        runtime_job_id=runtime_job.id,
        runner_id=runner.id,
    )

    enqueue_store = DBRunnerCoordinationStore(enqueue_db, pod_id="pod-a")
    enqueue_store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="tool-command-cache-miss",
        message_type="tool.command",
        payload_json=payload,
        idempotency_key="tool-command-cache-miss",
        runtime_job_id=runtime_job.id,
        task_id=202,
        correlation_id="corr-tool-command-cache-miss",
    )
    enqueue_db.commit()
    db_coordination_module._RAW_OUTBOUND_PAYLOADS.clear()

    dispatch_store = DBRunnerCoordinationStore(dispatch_db, pod_id="pod-b")
    now = datetime.now(tz=UTC)
    dispatch_store.claim_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-b",
        connection_id="conn-tool-command-cache-miss",
        lease_expires_at=now + timedelta(seconds=60),
        last_seen_at=now,
    )
    dispatch_db.commit()

    dispatcher = RunnerOutboundDispatcher(dispatch_db, coordination_store=dispatch_store, pod_id="pod-b")
    transport = _FakeTransport([DispatchAttemptResult(delivered=True, acked=True)])
    result = asyncio.run(
        dispatcher.dispatch_for_connection(
            tenant_id=tenant.id,
            runner_id=runner.id,
            connection_id="conn-tool-command-cache-miss",
            transport=transport,
        )
    )
    dispatch_db.commit()
    enqueue_db.expire_all()

    assert result.claimed_count == 1
    assert result.delivered_count == 0
    assert result.acked_count == 0
    assert result.failed_count == 1
    assert transport.envelopes == []

    durable_message = enqueue_db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.message_id == "tool-command-cache-miss",
        )
    ).scalar_one()
    durable_runtime_job = enqueue_db.execute(select(RuntimeJob).where(RuntimeJob.id == runtime_job.id)).scalar_one()
    assert durable_message.status == "failed"
    assert durable_message.error_code == "RUNNER_RAW_DISPATCH_PAYLOAD_UNAVAILABLE"
    assert durable_runtime_job.status == "failed"
    assert durable_runtime_job.error_code == "RUNNER_RAW_DISPATCH_PAYLOAD_UNAVAILABLE"
    serialized_message = str(durable_message.payload_json)
    serialized_job = str(durable_runtime_job.payload_json)
    assert secret not in serialized_message
    assert secret not in serialized_job
    assert "<DURABLE_SECRET_MASK:" in serialized_message
    assert "<DURABLE_SECRET_MASK:" in serialized_job


def test_dispatcher_uses_payload_task_id_for_stale_retire_cleanup(tmp_path: Path) -> None:
    enqueue_db, dispatch_db = _build_shared_sessions(tmp_path)
    tenant, runner = _seed_runner(enqueue_db)

    enqueue_store = DBRunnerCoordinationStore(enqueue_db, pod_id="pod-a")
    runtime_job_id = uuid.uuid4()
    enqueue_db.add(
        RuntimeJob(
            id=runtime_job_id,
            tenant_id=tenant.id,
            task_id=None,
            runner_id=runner.id,
            execution_site_id=runner.execution_site_id,
            job_type="task.retire",
            status="assigned",
            idempotency_key=f"stale-retire-{runtime_job_id}",
        )
    )
    enqueue_store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="stale-retire-1",
        message_type="task.retire",
        payload_json={
            "operation_id": "op-stale-retire",
            "workspace_id": "task-404",
            "runtime_image": "drowai-runtime-local:latest",
            "operation": "task.retire",
            "task_id": 404,
            "params": {"runtime_job_id": "runner-local-job-404"},
        },
        idempotency_key="stale-retire-1",
        runtime_job_id=runtime_job_id,
        task_id=None,
        correlation_id="corr-stale-retire",
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
    transport = _FakeTransport([DispatchAttemptResult(delivered=True, acked=True)])
    dispatcher = RunnerOutboundDispatcher(dispatch_db, coordination_store=dispatch_store, pod_id="pod-b")

    asyncio.run(
        dispatcher.dispatch_for_connection(
            tenant_id=tenant.id,
            runner_id=runner.id,
            connection_id="conn-b",
            transport=transport,
        )
    )

    assert len(transport.envelopes) == 1
    assert transport.envelopes[0].type == "task.retire"
    assert transport.envelopes[0].task_id == 404
    assert transport.envelopes[0].runtime_job_id == str(runtime_job_id)


def test_dispatcher_commits_claimed_runtime_message_before_transport_send(tmp_path: Path) -> None:
    enqueue_db, dispatch_db = _build_shared_sessions(tmp_path)
    tenant, runner = _seed_runner(enqueue_db)

    enqueue_store = DBRunnerCoordinationStore(enqueue_db, pod_id="pod-a")
    runtime_job_id = uuid.uuid4()
    enqueue_db.add(
        RuntimeJob(
            id=runtime_job_id,
            tenant_id=tenant.id,
            task_id=909,
            runner_id=runner.id,
            execution_site_id=runner.execution_site_id,
            job_type="terminal.open",
            status="assigned",
            idempotency_key=f"terminal-open-{runtime_job_id}",
        )
    )
    enqueue_store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="terminal-open-commit-before-send",
        message_type="terminal.open",
        payload_json={"operation": "terminal.open", "params": {"cols": 120, "rows": 40}},
        idempotency_key="terminal-open-commit-before-send",
        runtime_job_id=runtime_job_id,
        task_id=909,
        correlation_id="corr-terminal-open-commit-before-send",
    )
    enqueue_db.commit()

    dispatch_store = DBRunnerCoordinationStore(dispatch_db, pod_id="pod-b")
    now = datetime.now(tz=UTC)
    dispatch_store.claim_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-b",
        connection_id="conn-terminal-open",
        lease_expires_at=now + timedelta(seconds=60),
        last_seen_at=now,
    )
    dispatch_db.commit()

    commit_events: list[str] = []
    original_commit = dispatch_db.commit

    def commit_and_record() -> None:
        commit_events.append("commit")
        original_commit()

    dispatch_db.commit = commit_and_record  # type: ignore[method-assign]
    dispatcher = RunnerOutboundDispatcher(dispatch_db, coordination_store=dispatch_store, pod_id="pod-b")
    transport = _CommitAwareTransport(commit_events)

    result = asyncio.run(
        dispatcher.dispatch_for_connection(
            tenant_id=tenant.id,
            runner_id=runner.id,
            connection_id="conn-terminal-open",
            transport=transport,
        )
    )
    dispatch_db.commit()

    assert result.claimed_count == 1
    assert result.acked_count == 1
    assert transport.commits_seen_before_send >= 2

    row = enqueue_db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "outbound",
            RunnerControlMessage.message_id == "terminal-open-commit-before-send",
        )
    ).scalar_one()
    assert row.status == "acked"
    runtime_job = enqueue_db.execute(select(RuntimeJob).where(RuntimeJob.id == runtime_job_id)).scalar_one()
    assert runtime_job.status == "acknowledged"


def test_dispatcher_timeout_retries_then_fails_when_attempt_limit_is_reached(tmp_path: Path, caplog) -> None:
    enqueue_db, dispatch_db = _build_shared_sessions(tmp_path)
    tenant, runner = _seed_runner(enqueue_db)

    enqueue_store = DBRunnerCoordinationStore(enqueue_db, pod_id="pod-a")
    enqueue_store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="timeout-1",
        message_type="task.start",
        payload_json={"delivery_policy": {"max_attempts": 2, "timeout_seconds": 3}},
        idempotency_key="timeout-key-1",
        runtime_job_id=None,
        task_id=11,
        correlation_id="corr-timeout-1",
    )
    enqueue_db.commit()

    dispatch_store = DBRunnerCoordinationStore(dispatch_db, pod_id="pod-b")
    now = datetime.now(tz=UTC)
    dispatch_store.claim_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-b",
        connection_id="conn-timeout",
        lease_expires_at=now + timedelta(seconds=60),
        last_seen_at=now,
    )
    dispatch_db.commit()

    dispatcher = RunnerOutboundDispatcher(dispatch_db, coordination_store=dispatch_store, pod_id="pod-b")
    caplog.set_level("INFO")
    timeout_outcome = DispatchAttemptResult(
        delivered=False,
        acked=False,
        timed_out=True,
        error_code="RUNNER_ACK_TIMEOUT",
        error_message="Runner did not acknowledge.",
        retryable=True,
    )
    transport = _FakeTransport([timeout_outcome, timeout_outcome])

    first = asyncio.run(
        dispatcher.dispatch_for_connection(
            tenant_id=tenant.id,
            runner_id=runner.id,
            connection_id="conn-timeout",
            transport=transport,
        )
    )
    dispatch_db.commit()
    assert first.retried_count == 1
    assert first.failed_count == 0
    assert first.timed_out_count == 1

    second = asyncio.run(
        dispatcher.dispatch_for_connection(
            tenant_id=tenant.id,
            runner_id=runner.id,
            connection_id="conn-timeout",
            transport=transport,
        )
    )
    dispatch_db.commit()
    assert second.retried_count == 0
    assert second.failed_count == 1
    assert second.timed_out_count == 1

    row = enqueue_db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "outbound",
            RunnerControlMessage.message_id == "timeout-1",
        )
    ).scalar_one()
    assert row.status == "failed"
    assert row.delivery_attempt_count == 2
    assert row.error_code == "RUNNER_ACK_TIMEOUT"
    assert row.error_message == "Runner did not acknowledge."
    assert "runner_control.dispatch_message_timeout" in caplog.text
    assert "runner_control.dispatch_message_retry" in caplog.text
    assert "runner_control.dispatch_message_failed" in caplog.text
    assert "error_code=RUNNER_ACK_TIMEOUT" in caplog.text
    assert "message_id=timeout-1" in caplog.text
    assert "correlation_id=corr-timeout-1" in caplog.text


def test_dispatcher_offline_policy_controls_retry_vs_failed(tmp_path: Path) -> None:
    enqueue_db, dispatch_db = _build_shared_sessions(tmp_path)
    tenant, runner = _seed_runner(enqueue_db)

    enqueue_store = DBRunnerCoordinationStore(enqueue_db, pod_id="pod-a")
    enqueue_store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="offline-retry",
        message_type="task.start",
        payload_json={"delivery_policy": {"offline": "queue", "max_attempts": 1}},
        idempotency_key="offline-policy-retry",
        runtime_job_id=None,
        task_id=None,
        correlation_id=None,
    )
    enqueue_store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="offline-fail",
        message_type="task.start",
        payload_json={"delivery_policy": {"offline": "fail", "max_attempts": 3}},
        idempotency_key="offline-policy-fail",
        runtime_job_id=None,
        task_id=None,
        correlation_id=None,
    )
    enqueue_db.commit()

    dispatch_store = DBRunnerCoordinationStore(dispatch_db, pod_id="pod-b")
    now = datetime.now(tz=UTC)
    dispatch_store.claim_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-b",
        connection_id="conn-offline",
        lease_expires_at=now + timedelta(seconds=60),
        last_seen_at=now,
    )
    dispatch_db.commit()

    offline_outcome = DispatchAttemptResult(
        delivered=False,
        acked=False,
        timed_out=False,
        error_code="RUNNER_OFFLINE",
        error_message="Runner connection unavailable.",
        retryable=True,
    )
    dispatcher = RunnerOutboundDispatcher(dispatch_db, coordination_store=dispatch_store, pod_id="pod-b")
    transport = _FakeTransport([offline_outcome, offline_outcome])
    result = asyncio.run(
        dispatcher.dispatch_for_connection(
            tenant_id=tenant.id,
            runner_id=runner.id,
            connection_id="conn-offline",
            transport=transport,
            max_messages=10,
        )
    )
    dispatch_db.commit()

    assert result.retried_count == 1
    assert result.failed_count == 1

    rows = enqueue_db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "outbound",
        )
    ).scalars().all()
    statuses = {row.message_id: row.status for row in rows}
    assert statuses["offline-retry"] == "retry"
    assert statuses["offline-fail"] == "failed"


def test_dispatcher_timeout_after_delivery_counts_each_send_once(tmp_path: Path) -> None:
    enqueue_db, dispatch_db = _build_shared_sessions(tmp_path)
    tenant, runner = _seed_runner(enqueue_db)

    enqueue_store = DBRunnerCoordinationStore(enqueue_db, pod_id="pod-a")
    enqueue_store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="timeout-delivered-1",
        message_type="task.start",
        payload_json={"delivery_policy": {"max_attempts": 2, "timeout_seconds": 3}},
        idempotency_key="timeout-delivered-key-1",
        runtime_job_id=None,
        task_id=501,
        correlation_id="corr-timeout-delivered-1",
    )
    enqueue_db.commit()

    dispatch_store = DBRunnerCoordinationStore(dispatch_db, pod_id="pod-b")
    now = datetime.now(tz=UTC)
    dispatch_store.claim_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-b",
        connection_id="conn-timeout-delivered",
        lease_expires_at=now + timedelta(seconds=60),
        last_seen_at=now,
    )
    dispatch_db.commit()

    dispatcher = RunnerOutboundDispatcher(dispatch_db, coordination_store=dispatch_store, pod_id="pod-b")
    timeout_after_delivery = DispatchAttemptResult(
        delivered=True,
        acked=False,
        timed_out=True,
        error_code="RUNNER_ACK_TIMEOUT",
        error_message="Runner did not acknowledge.",
        retryable=True,
    )
    transport = _FakeTransport([timeout_after_delivery, timeout_after_delivery])

    first = asyncio.run(
        dispatcher.dispatch_for_connection(
            tenant_id=tenant.id,
            runner_id=runner.id,
            connection_id="conn-timeout-delivered",
            transport=transport,
        )
    )
    dispatch_db.commit()
    assert first.delivered_count == 1
    assert first.retried_count == 1
    assert first.failed_count == 0
    assert first.timed_out_count == 1

    second = asyncio.run(
        dispatcher.dispatch_for_connection(
            tenant_id=tenant.id,
            runner_id=runner.id,
            connection_id="conn-timeout-delivered",
            transport=transport,
        )
    )
    dispatch_db.commit()
    assert second.delivered_count == 1
    assert second.retried_count == 0
    assert second.failed_count == 1
    assert second.timed_out_count == 1

    row = enqueue_db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "outbound",
            RunnerControlMessage.message_id == "timeout-delivered-1",
        )
    ).scalar_one()
    assert row.status == "failed"
    assert row.delivery_attempt_count == 2
    assert row.error_code == "RUNNER_ACK_TIMEOUT"
    assert row.error_message == "Runner did not acknowledge."


def test_dispatcher_transport_exception_logs_are_secret_safe(tmp_path: Path, caplog) -> None:
    enqueue_db, dispatch_db = _build_shared_sessions(tmp_path)
    tenant, runner = _seed_runner(enqueue_db)

    enqueue_store = DBRunnerCoordinationStore(enqueue_db, pod_id="pod-a")
    enqueue_store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="transport-exception-1",
        message_type="task.start",
        payload_json={"task": 808},
        idempotency_key="transport-exception-key-1",
        runtime_job_id=None,
        task_id=808,
        correlation_id="corr-transport-exception-1",
    )
    enqueue_db.commit()

    dispatch_store = DBRunnerCoordinationStore(dispatch_db, pod_id="pod-b")
    now = datetime.now(tz=UTC)
    dispatch_store.claim_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-b",
        connection_id="conn-transport-exception",
        lease_expires_at=now + timedelta(seconds=60),
        last_seen_at=now,
    )
    dispatch_db.commit()

    dispatcher = RunnerOutboundDispatcher(dispatch_db, coordination_store=dispatch_store, pod_id="pod-b")
    secret = "runner-secret-123"
    caplog.set_level("INFO")
    result = asyncio.run(
        dispatcher.dispatch_for_connection(
            tenant_id=tenant.id,
            runner_id=runner.id,
            connection_id="conn-transport-exception",
            transport=_RaisingTransport(message=f"transport exploded secret={secret}"),
        )
    )
    dispatch_db.commit()

    assert result.retried_count == 1
    assert result.failed_count == 0
    assert secret not in caplog.text
    assert "RUNNER_DELIVERY_EXCEPTION" in caplog.text
    assert "tenant_id" in caplog.text
    assert "runner_id" in caplog.text
    assert "message_id=transport-exception-1" in caplog.text
    assert "correlation_id=corr-transport-exception-1" in caplog.text


def test_dispatcher_uses_expected_schema_versions_for_outbound_requests(tmp_path: Path) -> None:
    enqueue_db, dispatch_db = _build_shared_sessions(tmp_path)
    tenant, runner = _seed_runner(enqueue_db)

    enqueue_store = DBRunnerCoordinationStore(enqueue_db, pod_id="pod-a")
    enqueue_store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="remote-runtime-task-start",
        message_type="task.start",
        payload_json={"operation": "task.start", "params": {"target": "127.0.0.1"}},
        idempotency_key="remote-runtime-schema-task-start",
        runtime_job_id=None,
        task_id=34,
        correlation_id="corr-remote-runtime-task-start",
    )
    enqueue_store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="remote-runtime-task-stop",
        message_type="task.stop",
        payload_json={"operation": "task.stop", "params": {"lifecycle_intent": "cancel"}},
        idempotency_key="remote-runtime-schema-task-stop",
        runtime_job_id=None,
        task_id=34,
        correlation_id="corr-remote-runtime-task-stop",
    )
    enqueue_store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="remote-runtime-runtime-status",
        message_type="runtime.status",
        payload_json={"operation": "runtime.status", "params": {"detail": "full"}},
        idempotency_key="remote-runtime-schema-runtime-status",
        runtime_job_id=None,
        task_id=34,
        correlation_id="corr-remote-runtime-runtime-status",
    )
    enqueue_store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="runner-control-assignment-probe",
        message_type="runner.assignment.probe",
        payload_json={"operation": "runner.assignment.probe"},
        idempotency_key="runner-control-schema-assignment-probe",
        runtime_job_id=None,
        task_id=34,
        correlation_id="corr-runner-control-assignment-probe",
    )
    enqueue_store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="tooling-plane-tool-command",
        message_type="tool.command",
        payload_json={
            "operation_id": "send_tool_command:tooling-plane",
            "workspace_id": "task-34",
            "task_runtime_job_id": "runtime-task-34",
            "runtime_image": "drowai-runtime-local:latest",
            "tool": "shell.exec",
            "args": {"command": "id"},
            "command_id": "cmd-tooling-plane",
            "timeout_seconds": 10.0,
            "params": {},
        },
        idempotency_key="tooling-plane-schema-tool-command",
        runtime_job_id=None,
        task_id=34,
        correlation_id="corr-tooling-plane-tool-command",
    )
    enqueue_db.commit()

    dispatch_store = DBRunnerCoordinationStore(dispatch_db, pod_id="pod-b")
    now = datetime.now(tz=UTC)
    dispatch_store.claim_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-b",
        connection_id="conn-wave-schema",
        lease_expires_at=now + timedelta(seconds=60),
        last_seen_at=now,
    )
    dispatch_db.commit()

    dispatcher = RunnerOutboundDispatcher(dispatch_db, coordination_store=dispatch_store, pod_id="pod-b")
    transport = _FakeTransport([DispatchAttemptResult(delivered=True, acked=True) for _ in range(5)])

    result = asyncio.run(
        dispatcher.dispatch_for_connection(
            tenant_id=tenant.id,
            runner_id=runner.id,
            connection_id="conn-wave-schema",
            transport=transport,
            max_messages=10,
        )
    )
    dispatch_db.commit()

    assert result.claimed_count == 5
    assert result.acked_count == 5

    versions_by_type = {
        envelope.type: envelope.schema_version
        for envelope in transport.envelopes
    }
    assert versions_by_type["task.start"] == RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION
    assert versions_by_type["task.stop"] == RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION
    assert versions_by_type["runtime.status"] == RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION
    assert versions_by_type["runner.assignment.probe"] == RUNNER_PROTOCOL_RUNNER_CONTROL_VERSION
    assert versions_by_type["tool.command"] == RUNNER_PROTOCOL_TOOLING_PLANE_VERSION


def test_dispatcher_remote_runtime_envelope_runtime_job_id_stays_control_runtime_job_id(tmp_path: Path) -> None:
    enqueue_db, dispatch_db = _build_shared_sessions(tmp_path)
    tenant, runner = _seed_runner(enqueue_db)
    control_runtime_job_id = uuid.uuid4()

    enqueue_db.add(
        RuntimeJob(
            id=control_runtime_job_id,
            tenant_id=tenant.id,
            task_id=34,
            runner_id=runner.id,
            execution_site_id=runner.execution_site_id,
            job_type="runtime.status",
            status="assigned",
            idempotency_key=f"remote-runtime-runtime-status-{control_runtime_job_id}",
        )
    )
    enqueue_db.commit()

    enqueue_store = DBRunnerCoordinationStore(enqueue_db, pod_id="pod-a")
    enqueue_store.enqueue_outbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id="remote-runtime-runtime-status-runtime-job-routing",
        message_type="runtime.status",
        payload_json={
            "operation": "runtime.status",
            "runtime_job_id": "runner-runtime-job-34",
            "params": {"detail": "full"},
        },
        idempotency_key="remote-runtime-runtime-status-routing",
        runtime_job_id=control_runtime_job_id,
        task_id=34,
        correlation_id="corr-remote-runtime-runtime-status-runtime-job-routing",
    )
    enqueue_db.commit()

    dispatch_store = DBRunnerCoordinationStore(dispatch_db, pod_id="pod-b")
    now = datetime.now(tz=UTC)
    dispatch_store.claim_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-b",
        connection_id="conn-remote-runtime-runtime-job-routing",
        lease_expires_at=now + timedelta(seconds=60),
        last_seen_at=now,
    )
    dispatch_db.commit()

    dispatcher = RunnerOutboundDispatcher(dispatch_db, coordination_store=dispatch_store, pod_id="pod-b")
    transport = _FakeTransport([DispatchAttemptResult(delivered=True, acked=True)])

    result = asyncio.run(
        dispatcher.dispatch_for_connection(
            tenant_id=tenant.id,
            runner_id=runner.id,
            connection_id="conn-remote-runtime-runtime-job-routing",
            transport=transport,
        )
    )
    dispatch_db.commit()

    assert result.claimed_count == 1
    assert len(transport.envelopes) == 1
    assert transport.envelopes[0].type == "runtime.status"
    assert transport.envelopes[0].runtime_job_id == str(control_runtime_job_id)
