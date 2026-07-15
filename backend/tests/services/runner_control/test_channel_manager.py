"""Tests for runner channel manager heartbeat ingest and lease-based presence state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, TaskHistory, User
from backend.models.provenance import ArtifactManifest, ExecutionArtifact, ToolExecution
from backend.models.runner_control import (
    ExecutionSite,
    Runner,
    RunnerConnection,
    RunnerControlMessage,
    RunnerCredential,
    RuntimeJob,
)
from backend.models.tenant import Tenant
from backend.services.data_plane.artifact_manifest_service import (
    ArtifactManifestHandleResult,
    ArtifactManifestService,
    ArtifactManifestServiceError,
)
from backend.services.data_plane.artifact_upload_service import ArtifactUploadService
from backend.services.data_plane.local_object_store import LocalObjectStore
from backend.services.runner_control.terminal_frame_buffer import get_runner_terminal_frame_buffer
import backend.services.runner_control.metrics as runner_metrics
from backend.services.runner_control.channel.auth import RunnerChannelAuthContext
from backend.services.runner_control.channel_manager import RunnerChannelManager
from backend.services.runner_control.credentials import RunnerCredentialService
from backend.services.terminal.manager import terminal_session_manager
from backend.services.terminal.models import TerminalSession
from runtime_shared.runner_protocol import RunnerErrorPayload


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            Engagement.__table__,
            Task.__table__,
            TaskHistory.__table__,
            ExecutionSite.__table__,
            Runner.__table__,
            RunnerCredential.__table__,
            RunnerConnection.__table__,
            RunnerControlMessage.__table__,
            RuntimeJob.__table__,
            ToolExecution.__table__,
            ArtifactManifest.__table__,
            ExecutionArtifact.__table__,
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


def _seed_runner_assigned_task(db: Session, *, tenant: Tenant, runner: Runner) -> Task:
    unique_suffix = uuid.uuid4().hex
    user = User(
        username=f"runner-user-{unique_suffix}",
        password="test-password",
        email=f"runner-user-{unique_suffix}@example.com",
    )
    db.add(user)
    db.flush()

    task = Task(
        user_id=user.id,
        tenant_id=tenant.id,
        name=f"Runner Task {unique_suffix}",
        runtime_placement_mode="runner",
        runner_id=str(runner.id).lower(),
    )
    db.add(task)
    db.flush()
    return task


def _seed_runtime_job(
    db: Session,
    *,
    tenant: Tenant,
    runner: Runner,
    task: Task,
    job_type: str,
) -> RuntimeJob:
    runtime_job = RuntimeJob(
        tenant_id=tenant.id,
        runner_id=runner.id,
        execution_site_id=runner.execution_site_id,
        task_id=task.id,
        job_type=job_type,
        status="dispatched",
        idempotency_key=f"{job_type}-{uuid.uuid4()}",
    )
    db.add(runtime_job)
    db.flush()
    return runtime_job


def _seed_runtime_job_with_payload(
    db: Session,
    *,
    tenant: Tenant,
    runner: Runner,
    task: Task,
    job_type: str,
    payload_json: dict[str, object],
) -> RuntimeJob:
    runtime_job = _seed_runtime_job(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        job_type=job_type,
    )
    runtime_job.payload_json = dict(payload_json)
    db.flush()
    return runtime_job


def _envelope_json(
    *,
    tenant_id: int,
    runner_id: uuid.UUID,
    message_type: str,
    payload: dict,
    schema_version: str = "runner_control.v1",
    runtime_job_id: str | None = None,
    task_id: int | None = None,
) -> str:
    return json.dumps(
        {
            "message_id": str(uuid.uuid4()),
            "type": message_type,
            "schema_version": schema_version,
            "tenant_id": str(tenant_id),
            "runner_id": str(runner_id),
            "correlation_id": None,
            "runtime_job_id": runtime_job_id,
            "task_id": task_id,
            "created_at": "2026-05-23T12:00:00+00:00",
            "payload": payload,
        }
    )


def _artifact_manifest_envelope_json(
    *,
    tenant_id: int,
    runner_id: uuid.UUID,
    runtime_job_id: str,
    task_id: int,
    message_id: str,
    command_id: str,
    task_runtime_job_id: str,
    workspace_id: str,
    artifact_relative_path: str = "artifacts/cmd-42/stdout.txt",
    metadata: dict[str, object] | None = None,
    schema_version: str = "data_plane.v1",
) -> str:
    return json.dumps(
        {
            "message_id": message_id,
            "type": "artifact.manifest",
            "schema_version": schema_version,
            "tenant_id": str(tenant_id),
            "runner_id": str(runner_id),
            "correlation_id": None,
            "runtime_job_id": runtime_job_id,
            "task_id": task_id,
            "created_at": "2026-05-25T12:00:00+00:00",
            "payload": {
                "task_runtime_job_id": task_runtime_job_id,
                "command_id": command_id,
                "workspace_id": workspace_id,
                "tool_call_id": "tool-call-1",
                "tool_batch_id": "tool-batch-1",
                "artifacts": [
                    {
                        "artifact_client_id": "artifact-client-1",
                        "relative_path": artifact_relative_path,
                        "artifact_kind": "stdout",
                        "size_bytes": 10,
                        "content_sha256": "a" * 64,
                        "content_type": "text/plain",
                        "is_text": True,
                        "created_at": "2026-05-25T12:00:00+00:00",
                        "metadata": metadata or {"source": "test"},
                    }
                ],
            },
        }
    )


def _artifact_upload_complete_envelope_json(
    *,
    tenant_id: int,
    runner_id: uuid.UUID,
    runtime_job_id: str,
    task_id: int,
    message_id: str,
    command_id: str,
    task_runtime_job_id: str,
    workspace_id: str,
    artifact_id: str,
    artifact_client_id: str,
    object_key: str,
    content_sha256: str = "a" * 64,
    size_bytes: int = 10,
    schema_version: str = "data_plane.v1",
) -> str:
    return json.dumps(
        {
            "message_id": message_id,
            "type": "artifact.upload.complete",
            "schema_version": schema_version,
            "tenant_id": str(tenant_id),
            "runner_id": str(runner_id),
            "correlation_id": None,
            "runtime_job_id": runtime_job_id,
            "task_id": task_id,
            "created_at": "2026-05-25T12:00:10+00:00",
            "payload": {
                "task_runtime_job_id": task_runtime_job_id,
                "command_id": command_id,
                "workspace_id": workspace_id,
                "tool_call_id": "tool-call-1",
                "tool_batch_id": "tool-batch-1",
                "uploads": [
                    {
                        "artifact_id": artifact_id,
                        "artifact_client_id": artifact_client_id,
                        "object_key": object_key,
                        "size_bytes": size_bytes,
                        "content_sha256": content_sha256,
                        "uploaded_at": "2026-05-25T12:00:09+00:00",
                    }
                ],
            },
        }
    )


class _RecordingArtifactManifestService(ArtifactManifestService):
    def __init__(self) -> None:  # type: ignore[super-init-not-called]
        self.calls: list[dict[str, object]] = []

    def handle_inbound_message(
        self,
        *,
        tenant_id: int,
        runner_id: uuid.UUID,
        task_id: int,
        runtime_job_id: uuid.UUID,
        envelope,
    ) -> ArtifactManifestHandleResult:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "runner_id": runner_id,
                "task_id": task_id,
                "runtime_job_id": runtime_job_id,
                "message_type": envelope.type,
            }
        )
        return ArtifactManifestHandleResult()


class _FailingArtifactManifestService(ArtifactManifestService):
    def __init__(self, *, error_code: str, message: str) -> None:  # type: ignore[super-init-not-called]
        self._error_code = error_code
        self._message = message
        self.calls = 0

    def handle_inbound_message(
        self,
        *,
        tenant_id: int,
        runner_id: uuid.UUID,
        task_id: int,
        runtime_job_id: uuid.UUID,
        envelope,
    ) -> ArtifactManifestHandleResult:
        del tenant_id, runner_id, task_id, runtime_job_id, envelope
        self.calls += 1
        raise ArtifactManifestServiceError(error_code=self._error_code, message=self._message)


def test_runner_channel_manager_heartbeat_persists_latest_capacity_snapshot() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    audit_events: list[dict[str, object]] = []
    manager = RunnerChannelManager(db, audit_emitter=audit_events.append)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1",),
        ),
        remote_ip_address="203.0.113.24",
    )

    connection = db.execute(
        select(RunnerConnection).where(RunnerConnection.connection_id == session.connection_id)
    ).scalar_one()
    assert connection.remote_ip_address == "203.0.113.24"

    hello = _envelope_json(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_type="runner.hello",
        payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
    )
    manager.handle_inbound_json(session, hello)

    heartbeat = _envelope_json(
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
    manager.handle_inbound_json(session, heartbeat)
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
    event_types = [event["event_type"] for event in audit_events]
    assert "runner.connected" in event_types
    assert "runner.heartbeat" in event_types
    assert event_types.count("runner.message.accepted") >= 2


def test_runner_channel_manager_heartbeat_enqueues_retire_for_missing_task_runtime() -> None:
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
    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )

    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.heartbeat",
            payload={
                "capacity": {
                    "active_tasks": 1,
                    "max_active_tasks": 2,
                    "available_tasks": 1,
                    "max_parallel_commands_per_task": 4,
                    "docker_available": True,
                    "runtime_image": "drowai-runtime-local:latest",
                    "runtime_image_available": True,
                    "version": "1.9.0",
                    "capabilities": ["docker"],
                    "labels": {"site": "hq"},
                    "active_runtime_jobs": [
                        {
                            "runtime_job_id": "local-runtime-job-404",
                            "task_id": "404",
                            "workspace_id": "task-404",
                            "status": "running",
                        }
                    ],
                }
            },
        ),
    )
    db.commit()

    retire_job = db.execute(select(RuntimeJob).where(RuntimeJob.job_type == "task.retire")).scalar_one()
    assert retire_job.task_id is None
    assert retire_job.runner_id == runner.id
    outbound = db.execute(
        select(RunnerControlMessage).where(RunnerControlMessage.type == "task.retire")
    ).scalar_one()
    assert outbound.task_id is None
    assert outbound.payload_json["task_id"] == 404
    assert outbound.payload_json["params"]["runtime_job_id"] == "local-runtime-job-404"
    assert outbound.payload_json["params"]["stale_reason"] == "backend_task_missing"


def test_runner_channel_manager_heartbeat_keeps_valid_active_runtime() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_runner_assigned_task(db, tenant=tenant, runner=runner)
    task.status = "running"
    db.flush()
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
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )

    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.heartbeat",
            payload={
                "capacity": {
                    "active_tasks": 1,
                    "max_active_tasks": 2,
                    "available_tasks": 1,
                    "max_parallel_commands_per_task": 4,
                    "docker_available": True,
                    "runtime_image": "drowai-runtime-local:latest",
                    "runtime_image_available": True,
                    "version": "1.9.0",
                    "capabilities": ["docker"],
                    "labels": {"site": "hq"},
                    "active_runtime_jobs": [
                        {
                            "runtime_job_id": "local-runtime-job-valid",
                            "task_id": str(task.id),
                            "workspace_id": f"task-{task.id}",
                            "status": "running",
                        }
                    ],
                }
            },
        ),
    )
    db.commit()

    retire_jobs = db.execute(select(RuntimeJob).where(RuntimeJob.job_type == "task.retire")).scalars().all()
    assert retire_jobs == []


def test_runner_channel_manager_hello_refreshes_online_offline_gauges(monkeypatch) -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    gauges: dict[str, float] = {}

    def _fake_safe_gauge(name: str, value: float) -> None:
        gauges[name] = float(value)

    monkeypatch.setattr(runner_metrics, "safe_gauge", _fake_safe_gauge)

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
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )
    db.commit()

    assert gauges["runner_control.runners.online_count"] == 1.0
    assert gauges["runner_control.runners.offline_count"] == 0.0


def test_runner_channel_manager_heartbeat_refreshes_online_offline_gauges(monkeypatch) -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    gauges: dict[str, float] = {}

    def _fake_safe_gauge(name: str, value: float) -> None:
        gauges[name] = float(value)

    monkeypatch.setattr(runner_metrics, "safe_gauge", _fake_safe_gauge)

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
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )
    db.commit()

    refreshed_runner = db.execute(select(Runner).where(Runner.id == runner.id)).scalar_one()
    refreshed_runner.status = "offline"
    db.commit()

    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.heartbeat",
            payload={"capacity": {"active_tasks": 0, "max_active_tasks": 1, "available_tasks": 1}},
        ),
    )
    db.commit()

    assert gauges["runner_control.runners.online_count"] == 1.0
    assert gauges["runner_control.runners.offline_count"] == 0.0


def test_runner_channel_manager_marks_runner_offline_when_all_leases_expire() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    manager = RunnerChannelManager(db, lease_ttl_seconds=30)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    auth = RunnerChannelAuthContext(
        tenant_id=tenant.id,
        runner_id=runner.id,
        credential_id=credential_id,
        allowed_protocol_versions=("runner_control.v1",),
    )

    first_session = manager.open_session(auth)
    hello = _envelope_json(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_type="runner.hello",
        payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
    )
    manager.handle_inbound_json(first_session, hello)
    db.commit()

    stale_time = datetime.now(tz=UTC) - timedelta(seconds=120)
    for connection in db.execute(
        select(RunnerConnection).where(
            RunnerConnection.tenant_id == tenant.id,
            RunnerConnection.runner_id == runner.id,
        )
    ).scalars().all():
        connection.lease_expires_at = stale_time
        connection.status = "active"
    db.commit()

    manager.open_session(auth)
    db.commit()

    refreshed_runner = db.execute(select(Runner).where(Runner.id == runner.id)).scalar_one()
    assert refreshed_runner.status == "offline"


def test_runner_channel_manager_close_session_marks_runner_offline_when_last_lease_released() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    audit_events: list[dict[str, object]] = []
    manager = RunnerChannelManager(db, lease_ttl_seconds=30, audit_emitter=audit_events.append)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    auth = RunnerChannelAuthContext(
        tenant_id=tenant.id,
        runner_id=runner.id,
        credential_id=credential_id,
        allowed_protocol_versions=("runner_control.v1",),
    )

    session = manager.open_session(auth)
    manager.close_session(session)
    db.commit()

    refreshed_runner = db.execute(select(Runner).where(Runner.id == runner.id)).scalar_one()
    assert refreshed_runner.status == "offline"
    assert refreshed_runner.last_seen_at is not None
    event_types = [event["event_type"] for event in audit_events]
    assert "runner.disconnected" in event_types
    assert "runner.offline" in event_types


def test_runner_channel_manager_close_session_tolerates_deleted_runner() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_runner_assigned_task(db, tenant=tenant, runner=runner)
    manager = RunnerChannelManager(db, lease_ttl_seconds=30)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1",),
        )
    )

    frame_buffer = get_runner_terminal_frame_buffer()
    frame_buffer.reset()
    frame_buffer.append_frame(
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id="deleted-runner-runtime-job",
        session_id="deleted-runner-session",
        sequence=1,
        stream="stdout",
        data="pending runner output",
    )

    terminal_session = TerminalSession(
        session_id="deleted-runner-terminal-session",
        task_id=task.id,
        user_id=task.user_id,
        container_name=f"task-{task.id}",
        connection_type="docker_exec",
        exec_id="deleted-runner-session",
        runtime_job_id="deleted-runner-runtime-job",
    )
    terminal_session_manager.sessions[terminal_session.session_id] = terminal_session

    db.delete(runner)
    db.commit()

    manager.close_session(session)
    db.commit()

    assert db.execute(select(Runner).where(Runner.id == runner.id)).scalar_one_or_none() is None
    assert terminal_session_manager.get_session("deleted-runner-terminal-session") is None
    frames = frame_buffer.read_frames(
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id="deleted-runner-runtime-job",
        session_id="deleted-runner-session",
        after_sequence=-1,
    )
    assert frames["frames"] == []

    frame_buffer.reset()


def test_runner_channel_manager_close_session_cleans_runner_terminal_sessions_and_buffers() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_runner_assigned_task(db, tenant=tenant, runner=runner)
    manager = RunnerChannelManager(db, lease_ttl_seconds=30)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1",),
        )
    )

    frame_buffer = get_runner_terminal_frame_buffer()
    frame_buffer.reset()
    frame_buffer.append_frame(
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id="runner-runtime-job-1",
        session_id="runner-session-1",
        sequence=1,
        stream="stdout",
        data="hello from runner",
    )

    terminal_session = TerminalSession(
        session_id="runner-terminal-session",
        task_id=task.id,
        user_id=task.user_id,
        container_name=f"task-{task.id}",
        connection_type="docker_exec",
        exec_id="runner-session-1",
        runtime_job_id="runner-runtime-job-1",
    )
    terminal_session_manager.sessions[terminal_session.session_id] = terminal_session

    manager.close_session(session)
    db.commit()

    assert terminal_session_manager.get_session("runner-terminal-session") is None
    frames = frame_buffer.read_frames(
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id="runner-runtime-job-1",
        session_id="runner-session-1",
        after_sequence=-1,
    )
    assert frames["frames"] == []

    frame_buffer.reset()


def test_runner_channel_manager_open_session_reconciles_expired_runtime_job(monkeypatch) -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    gauges: dict[str, float] = {}

    def _fake_safe_gauge(name: str, value: float) -> None:
        gauges[name] = float(value)

    monkeypatch.setattr(runner_metrics, "safe_gauge", _fake_safe_gauge)
    manager = RunnerChannelManager(db, lease_ttl_seconds=30)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)

    expired_job = RuntimeJob(
        tenant_id=tenant.id,
        runner_id=runner.id,
        execution_site_id=runner.execution_site_id,
        task_id=None,
        job_type="task.start",
        status="dispatched",
        idempotency_key=f"runtime-job-{uuid.uuid4()}",
        lease_expires_at=datetime.now(tz=UTC) - timedelta(seconds=30),
    )
    db.add(expired_job)
    db.commit()

    manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1",),
        )
    )
    db.commit()

    refreshed_job = db.execute(select(RuntimeJob).where(RuntimeJob.id == expired_job.id)).scalar_one()
    assert refreshed_job.status == "lost"
    assert refreshed_job.error_code == "RUNNER_LEASE_EXPIRED"
    assert gauges["runner_control.runtime_jobs.queue_depth"] == 0.0


def test_runner_channel_manager_protocol_violation_audit_redacts_secret_like_payload() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    audit_events: list[dict[str, object]] = []
    manager = RunnerChannelManager(db, audit_emitter=audit_events.append)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1",),
        )
    )

    result = manager.handle_inbound_json(
        session,
        '{"bad": "json", "runner_secret": "rsec_this_should_not_be_logged"',
    )
    assert result.response_envelopes

    protocol_events = [event for event in audit_events if event.get("event_type") == "runner.protocol_violation"]
    assert len(protocol_events) == 1
    serialized = str(protocol_events[0])
    assert "rsec_this_should_not_be_logged" not in serialized


def test_runner_channel_manager_unsupported_schema_audit_matches_protocol_error() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    audit_events: list[dict[str, object]] = []
    manager = RunnerChannelManager(db, audit_emitter=audit_events.append)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1",),
        )
    )

    result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            schema_version="runner_control.v0",
            payload={
                "version": "1.9.0",
                "capabilities": ["docker"],
                "labels": {"runner_secret": "rsec_this_should_not_be_logged"},
            },
        ),
    )

    assert result.should_close is True
    assert result.response_envelopes
    error_envelope = result.response_envelopes[0]
    assert isinstance(error_envelope.payload, RunnerErrorPayload)
    assert error_envelope.payload.error_code == "RUNNER_PROTOCOL_UNSUPPORTED"

    rejected_events = [event for event in audit_events if event.get("event_type") == "runner.message.rejected"]
    assert len(rejected_events) == 1
    assert rejected_events[0]["metadata"]["error_code"] == "RUNNER_PROTOCOL_UNSUPPORTED"

    protocol_events = [event for event in audit_events if event.get("event_type") == "runner.protocol_violation"]
    assert len(protocol_events) == 1
    assert protocol_events[0]["metadata"]["error_code"] == "RUNNER_PROTOCOL_UNSUPPORTED"
    assert "rsec_this_should_not_be_logged" not in str(protocol_events[0])


def test_runner_channel_manager_accepts_remote_runtime_runtime_and_terminal_events() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_runner_assigned_task(db, tenant=tenant, runner=runner)
    task.status = "starting"
    db.flush()
    runtime_started_job = _seed_runtime_job(db, tenant=tenant, runner=runner, task=task, job_type="task.start")
    terminal_open_job = _seed_runtime_job(db, tenant=tenant, runner=runner, task=task, job_type="terminal.open")
    audit_events: list[dict[str, object]] = []
    manager = RunnerChannelManager(db, audit_emitter=audit_events.append)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "remote_runtime.v1"),
        )
    )

    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )

    runtime_started_result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runtime.started",
            schema_version="remote_runtime.v1",
            runtime_job_id=str(runtime_started_job.id),
            task_id=task.id,
            payload={
                "operation_id": "op-1",
                "status": "succeeded",
                "error_code": None,
                "error_message": None,
                "result": {"runtime_job_id": str(runtime_started_job.id), "task_id": task.id, "workspace_id": "task-42"},
            },
        ),
    )
    terminal_result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="terminal.result",
            schema_version="remote_runtime.v1",
            runtime_job_id=str(terminal_open_job.id),
            task_id=task.id,
            payload={
                "operation_id": "op-2",
                "terminal_operation": "open",
                "session_id": f"task-{task.id}-terminal",
                "status": "succeeded",
                "sequence": 1,
                "error_code": None,
                "error_message": None,
                "result": {"runtime_job_id": str(terminal_open_job.id), "task_id": task.id, "workspace_id": "task-42"},
            },
        ),
    )
    db.commit()

    assert runtime_started_result.response_envelopes == ()
    assert terminal_result.response_envelopes == ()
    assert runtime_started_result.should_close is False
    assert terminal_result.should_close is False

    accepted_types = [
        event["metadata"]["message_type"]
        for event in audit_events
        if event.get("event_type") == "runner.message.accepted"
    ]
    assert "runtime.started" in accepted_types
    assert "terminal.result" in accepted_types

    applied_types = [
        event["metadata"]["message_type"]
        for event in audit_events
        if event.get("event_type") == "runner.runtime_event.applied"
    ]
    assert "runtime.started" in applied_types
    assert "terminal.result" in applied_types

    refreshed_runtime_started_job = db.execute(
        select(RuntimeJob).where(RuntimeJob.id == runtime_started_job.id)
    ).scalar_one()
    refreshed_terminal_open_job = db.execute(
        select(RuntimeJob).where(RuntimeJob.id == terminal_open_job.id)
    ).scalar_one()
    refreshed_task = db.execute(select(Task).where(Task.id == task.id)).scalar_one()
    assert refreshed_runtime_started_job.status == "succeeded"
    assert refreshed_terminal_open_job.status == "succeeded"
    assert refreshed_task.status == "running"

    inbound_rows = db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "inbound",
            RunnerControlMessage.type.in_(["runtime.started", "terminal.result"]),
        )
    ).scalars().all()
    assert len(inbound_rows) == 2


def test_runner_channel_manager_rejects_terminal_frame_for_unbound_session_id() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_runner_assigned_task(db, tenant=tenant, runner=runner)
    runtime_start_job = _seed_runtime_job(db, tenant=tenant, runner=runner, task=task, job_type="task.start")
    frame_buffer = get_runner_terminal_frame_buffer()
    frame_buffer.reset()

    manager = RunnerChannelManager(db)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "remote_runtime.v1"),
        )
    )

    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )

    result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="terminal.frame",
            schema_version="remote_runtime.v1",
            runtime_job_id=str(runtime_start_job.id),
            task_id=task.id,
            payload={
                "session_id": "unbound-session",
                "sequence": 1,
                "stream": "stdout",
                "data": "whoami\n",
            },
        ),
    )
    db.commit()

    assert len(result.response_envelopes) == 1
    error_envelope = result.response_envelopes[0]
    assert isinstance(error_envelope.payload, RunnerErrorPayload)
    assert error_envelope.payload.error_code == "RUNNER_TERMINAL_SESSION_UNBOUND"

    buffered = frame_buffer.read_frames(
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=str(runtime_start_job.id),
        session_id="unbound-session",
        after_sequence=-1,
    )
    assert buffered["frames"] == []
    frame_buffer.reset()


def test_runner_channel_manager_accepts_terminal_frame_for_bound_session_id() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_runner_assigned_task(db, tenant=tenant, runner=runner)
    runtime_start_job = _seed_runtime_job(db, tenant=tenant, runner=runner, task=task, job_type="task.start")
    terminal_open_job = _seed_runtime_job(db, tenant=tenant, runner=runner, task=task, job_type="terminal.open")
    frame_buffer = get_runner_terminal_frame_buffer()
    frame_buffer.reset()

    manager = RunnerChannelManager(db)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "remote_runtime.v1"),
        )
    )

    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )
    open_result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="terminal.result",
            schema_version="remote_runtime.v1",
            runtime_job_id=str(terminal_open_job.id),
            task_id=task.id,
            payload={
                "operation_id": "open-1",
                "terminal_operation": "open",
                "session_id": "bound-session",
                "status": "succeeded",
                "sequence": 0,
                "error_code": None,
                "error_message": None,
                "result": {
                    "runtime_job_id": str(runtime_start_job.id),
                },
            },
        ),
    )
    frame_result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="terminal.frame",
            schema_version="remote_runtime.v1",
            runtime_job_id=str(runtime_start_job.id),
            task_id=task.id,
            payload={
                "session_id": "bound-session",
                "sequence": 1,
                "stream": "stdout",
                "data": "id\n",
            },
        ),
    )
    db.commit()

    assert open_result.response_envelopes == ()
    assert frame_result.response_envelopes == ()
    buffered = frame_buffer.read_frames(
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=str(runtime_start_job.id),
        session_id="bound-session",
        after_sequence=-1,
    )
    assert buffered["data"] == "id\n"
    frame_buffer.reset()


def test_runner_channel_manager_rejects_terminal_result_when_session_id_mismatches_request() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_runner_assigned_task(db, tenant=tenant, runner=runner)
    terminal_input_job = _seed_runtime_job(db, tenant=tenant, runner=runner, task=task, job_type="terminal.input")
    terminal_input_job.payload_json = {"params": {"session_id": "expected-session"}}
    db.flush()

    manager = RunnerChannelManager(db)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "remote_runtime.v1"),
        )
    )

    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )
    result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="terminal.result",
            schema_version="remote_runtime.v1",
            runtime_job_id=str(terminal_input_job.id),
            task_id=task.id,
            payload={
                "operation_id": "input-1",
                "terminal_operation": "input",
                "session_id": "unexpected-session",
                "status": "succeeded",
                "sequence": 2,
                "error_code": None,
                "error_message": None,
                "result": {},
            },
        ),
    )
    db.commit()

    assert len(result.response_envelopes) == 1
    error_envelope = result.response_envelopes[0]
    assert isinstance(error_envelope.payload, RunnerErrorPayload)
    assert error_envelope.payload.error_code == "RUNNER_EVENT_OPERATION_MISMATCH"

    refreshed_runtime_job = db.execute(
        select(RuntimeJob).where(RuntimeJob.id == terminal_input_job.id)
    ).scalar_one()
    assert refreshed_runtime_job.status == "dispatched"


def test_runner_channel_manager_terminal_close_purges_all_session_frame_buckets() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_runner_assigned_task(db, tenant=tenant, runner=runner)
    terminal_open_job = _seed_runtime_job(db, tenant=tenant, runner=runner, task=task, job_type="terminal.open")
    terminal_input_job = _seed_runtime_job(db, tenant=tenant, runner=runner, task=task, job_type="terminal.input")
    terminal_close_job = _seed_runtime_job(db, tenant=tenant, runner=runner, task=task, job_type="terminal.close")

    frame_buffer = get_runner_terminal_frame_buffer()
    frame_buffer.reset()
    assert frame_buffer.append_frame(
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=str(terminal_open_job.id),
        session_id=f"task-{task.id}-terminal",
        sequence=1,
        stream="stdout",
        data="open-frame",
    )
    assert frame_buffer.append_frame(
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=str(terminal_input_job.id),
        session_id=f"task-{task.id}-terminal",
        sequence=2,
        stream="stdout",
        data="input-frame",
    )

    manager = RunnerChannelManager(db)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "remote_runtime.v1"),
        )
    )

    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )
    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="terminal.result",
            schema_version="remote_runtime.v1",
            runtime_job_id=str(terminal_close_job.id),
            task_id=task.id,
            payload={
                "operation_id": "op-terminal-close",
                "terminal_operation": "close",
                "session_id": f"task-{task.id}-terminal",
                "status": "succeeded",
                "sequence": 2,
                "error_code": None,
                "error_message": None,
                "result": {
                    "runtime_job_id": str(terminal_close_job.id),
                    "task_id": task.id,
                    "workspace_id": f"task-{task.id}",
                },
            },
        ),
    )
    db.commit()

    open_frames = frame_buffer.read_frames(
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=str(terminal_open_job.id),
        session_id=f"task-{task.id}-terminal",
        after_sequence=-1,
    )
    input_frames = frame_buffer.read_frames(
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=str(terminal_input_job.id),
        session_id=f"task-{task.id}-terminal",
        after_sequence=-1,
    )
    assert open_frames["frames"] == []
    assert input_frames["frames"] == []
    assert frame_buffer.append_frame(
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=str(terminal_open_job.id),
        session_id=f"task-{task.id}-terminal",
        sequence=0,
        stream="stdout",
        data="reopened-frame",
    )
    frame_buffer.reset()


def test_runner_channel_manager_rejects_request_shaped_remote_runtime_dual_event_payloads() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_runner_assigned_task(db, tenant=tenant, runner=runner)
    runtime_status_job = _seed_runtime_job(db, tenant=tenant, runner=runner, task=task, job_type="runtime.status")
    runtime_logs_job = _seed_runtime_job(db, tenant=tenant, runner=runner, task=task, job_type="runtime.logs")
    runtime_vpn_status_job = _seed_runtime_job(db, tenant=tenant, runner=runner, task=task, job_type="runtime.vpn.status")
    manager = RunnerChannelManager(db)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "remote_runtime.v1"),
        )
    )

    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )

    for runtime_job_id, message_type in (
        (runtime_status_job.id, "runtime.status"),
        (runtime_logs_job.id, "runtime.logs"),
        (runtime_vpn_status_job.id, "runtime.vpn.status"),
    ):
        result = manager.handle_inbound_json(
            session,
            _envelope_json(
                tenant_id=tenant.id,
                runner_id=runner.id,
                message_type=message_type,
                schema_version="remote_runtime.v1",
                runtime_job_id=str(runtime_job_id),
                task_id=task.id,
                payload={
                    "operation_id": f"{message_type}-op",
                    "workspace_id": f"task-{task.id}",
                    "runtime_image": "drowai-runtime-local:latest",
                    "operation": message_type,
                    "params": {"probe": True},
                },
            ),
        )
        assert result.should_close is False
        assert result.response_envelopes
        error_envelope = result.response_envelopes[0]
        assert isinstance(error_envelope.payload, RunnerErrorPayload)
        assert error_envelope.payload.error_code == "RUNNER_PROTOCOL_INVALID"


def test_runner_channel_manager_accepts_result_shaped_remote_runtime_dual_event_payloads() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_runner_assigned_task(db, tenant=tenant, runner=runner)
    runtime_status_job = _seed_runtime_job(db, tenant=tenant, runner=runner, task=task, job_type="runtime.status")
    runtime_logs_job = _seed_runtime_job(db, tenant=tenant, runner=runner, task=task, job_type="runtime.logs")
    runtime_vpn_status_job = _seed_runtime_job(db, tenant=tenant, runner=runner, task=task, job_type="runtime.vpn.status")
    audit_events: list[dict[str, object]] = []
    manager = RunnerChannelManager(db, audit_emitter=audit_events.append)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "remote_runtime.v1"),
        )
    )

    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )

    for runtime_job_id, message_type in (
        (runtime_status_job.id, "runtime.status"),
        (runtime_logs_job.id, "runtime.logs"),
        (runtime_vpn_status_job.id, "runtime.vpn.status"),
    ):
        result = manager.handle_inbound_json(
            session,
            _envelope_json(
                tenant_id=tenant.id,
                runner_id=runner.id,
                message_type=message_type,
                schema_version="remote_runtime.v1",
                runtime_job_id=str(runtime_job_id),
                task_id=task.id,
                payload={
                    "operation_id": f"{message_type}-op",
                    "status": "succeeded",
                    "error_code": None,
                    "error_message": None,
                    "result": {"runtime_job_id": str(runtime_job_id), "task_id": task.id},
                },
            ),
        )
        assert result.response_envelopes == ()
        assert result.should_close is False

    applied_types = [
        event["metadata"]["message_type"]
        for event in audit_events
        if event.get("event_type") == "runner.runtime_event.applied"
    ]
    assert "runtime.status" in applied_types
    assert "runtime.logs" in applied_types
    assert "runtime.vpn.status" in applied_types

    refreshed_runtime_status_job = db.execute(
        select(RuntimeJob).where(RuntimeJob.id == runtime_status_job.id)
    ).scalar_one()
    refreshed_runtime_logs_job = db.execute(
        select(RuntimeJob).where(RuntimeJob.id == runtime_logs_job.id)
    ).scalar_one()
    refreshed_runtime_vpn_status_job = db.execute(
        select(RuntimeJob).where(RuntimeJob.id == runtime_vpn_status_job.id)
    ).scalar_one()
    assert refreshed_runtime_status_job.status == "succeeded"
    assert refreshed_runtime_logs_job.status == "succeeded"
    assert refreshed_runtime_vpn_status_job.status == "succeeded"


def test_runner_channel_manager_rejects_remote_runtime_event_with_operation_family_mismatch() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_runner_assigned_task(db, tenant=tenant, runner=runner)
    runtime_status_job = _seed_runtime_job(db, tenant=tenant, runner=runner, task=task, job_type="runtime.status")
    manager = RunnerChannelManager(db)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "remote_runtime.v1"),
        )
    )
    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )

    result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runtime.startup_progress",
            schema_version="remote_runtime.v1",
            runtime_job_id=str(runtime_status_job.id),
            task_id=task.id,
            payload={
                "operation_id": "op-1",
                "status": "succeeded",
                "error_code": None,
                "error_message": None,
                "result": {"runtime_job_id": str(runtime_status_job.id), "task_id": task.id},
            },
        ),
    )

    assert result.should_close is False
    assert result.response_envelopes
    error_envelope = result.response_envelopes[0]
    assert isinstance(error_envelope.payload, RunnerErrorPayload)
    assert error_envelope.payload.error_code == "RUNNER_EVENT_OPERATION_MISMATCH"


def test_runner_channel_manager_rejects_remote_runtime_event_with_unassigned_runtime_job_id() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_runner_assigned_task(db, tenant=tenant, runner=runner)
    audit_events: list[dict[str, object]] = []
    manager = RunnerChannelManager(db, audit_emitter=audit_events.append)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "remote_runtime.v1"),
        )
    )

    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )

    result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runtime.started",
            schema_version="remote_runtime.v1",
            runtime_job_id=str(uuid.uuid4()),
            task_id=task.id,
            payload={
                "operation_id": "op-1",
                "status": "succeeded",
                "error_code": None,
                "error_message": None,
                "result": {"runtime_job_id": "unassigned", "task_id": task.id, "workspace_id": "task-42"},
            },
        ),
    )

    assert result.should_close is False
    assert result.response_envelopes
    error_envelope = result.response_envelopes[0]
    assert isinstance(error_envelope.payload, RunnerErrorPayload)
    assert error_envelope.payload.error_code == "RUNTIME_JOB_NOT_ASSIGNED"

    rejected_events = [event for event in audit_events if event.get("event_type") == "runner.message.rejected"]
    assert rejected_events
    assert rejected_events[-1]["metadata"]["error_code"] == "RUNTIME_JOB_NOT_ASSIGNED"


def test_runner_channel_manager_rejects_remote_runtime_event_with_mismatched_runtime_job_task_id() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_runner_assigned_task(db, tenant=tenant, runner=runner)
    runtime_job = _seed_runtime_job(db, tenant=tenant, runner=runner, task=task, job_type="task.start")
    manager = RunnerChannelManager(db)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "remote_runtime.v1"),
        )
    )

    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )

    result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runtime.started",
            schema_version="remote_runtime.v1",
            runtime_job_id=str(runtime_job.id),
            task_id=task.id + 1,
            payload={
                "operation_id": "op-1",
                "status": "succeeded",
                "error_code": None,
                "error_message": None,
                "result": {"runtime_job_id": str(runtime_job.id), "task_id": task.id + 1, "workspace_id": "task-42"},
            },
        ),
    )

    assert result.should_close is False
    assert result.response_envelopes
    error_envelope = result.response_envelopes[0]
    assert isinstance(error_envelope.payload, RunnerErrorPayload)
    assert error_envelope.payload.error_code == "RUNTIME_JOB_NOT_ASSIGNED"


def test_runner_channel_manager_rejects_remote_runtime_task_only_event_without_assignment() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    manager = RunnerChannelManager(db)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "remote_runtime.v1"),
        )
    )

    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )

    result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runtime.status",
            schema_version="remote_runtime.v1",
            runtime_job_id=None,
            task_id=4242,
            payload={
                "operation_id": "op-status-1",
                "status": "succeeded",
                "error_code": None,
                "error_message": None,
                "result": {"task_id": 4242, "state": "running"},
            },
        ),
    )

    assert result.should_close is False
    assert result.response_envelopes
    error_envelope = result.response_envelopes[0]
    assert isinstance(error_envelope.payload, RunnerErrorPayload)
    assert error_envelope.payload.error_code == "RUNNER_ASSIGNMENT_NOT_FOUND"


def test_runner_channel_manager_rejects_remote_runtime_lifecycle_event_without_runtime_job_id() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_runner_assigned_task(db, tenant=tenant, runner=runner)
    task.status = "starting"
    db.flush()
    manager = RunnerChannelManager(db)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "remote_runtime.v1"),
        )
    )

    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )

    result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runtime.started",
            schema_version="remote_runtime.v1",
            runtime_job_id=None,
            task_id=task.id,
            payload={
                "operation_id": "op-start-no-job",
                "status": "succeeded",
                "error_code": None,
                "error_message": None,
                "result": {"task_id": task.id},
            },
        ),
    )

    assert result.should_close is False
    assert result.response_envelopes
    error_envelope = result.response_envelopes[0]
    assert isinstance(error_envelope.payload, RunnerErrorPayload)
    assert error_envelope.payload.error_code == "RUNTIME_JOB_NOT_ASSIGNED"


def test_runner_channel_manager_rejected_remote_runtime_event_does_not_block_valid_transition_replay() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_runner_assigned_task(db, tenant=tenant, runner=runner)
    task.status = "starting"
    db.flush()
    runtime_start_job = _seed_runtime_job(db, tenant=tenant, runner=runner, task=task, job_type="task.start")
    manager = RunnerChannelManager(db)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "remote_runtime.v1"),
        )
    )

    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )

    first_result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runtime.startup_progress",
            schema_version="remote_runtime.v1",
            runtime_job_id=str(runtime_start_job.id),
            task_id=task.id,
            payload={
                "operation_id": "op-mismatch",
                "status": "succeeded",
                "error_code": None,
                "error_message": None,
                "result": {"runtime_job_id": str(runtime_start_job.id), "task_id": task.id},
            },
        ),
    )
    second_result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runtime.started",
            schema_version="remote_runtime.v1",
            runtime_job_id=str(runtime_start_job.id),
            task_id=task.id,
            payload={
                "operation_id": "op-correct",
                "status": "succeeded",
                "error_code": None,
                "error_message": None,
                "result": {"runtime_job_id": str(runtime_start_job.id), "task_id": task.id},
            },
        ),
    )
    db.commit()

    assert first_result.response_envelopes
    assert second_result.response_envelopes == ()

    refreshed_runtime_job = db.execute(select(RuntimeJob).where(RuntimeJob.id == runtime_start_job.id)).scalar_one()
    refreshed_task = db.execute(select(Task).where(Task.id == task.id)).scalar_one()
    assert refreshed_runtime_job.status == "succeeded"
    assert refreshed_task.status == "running"

    inbound_rows = db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "inbound",
            RunnerControlMessage.type.in_(["runtime.startup_progress", "runtime.started"]),
        )
    ).scalars().all()
    assert len(inbound_rows) == 2
    assert any(row.type == "runtime.startup_progress" and row.status == "rejected" for row in inbound_rows)
    assert any(
        row.type == "runtime.started"
        and row.status == "accepted"
        and row.idempotency_key == f"runtime_job:{runtime_start_job.id}:transition:succeeded"
        for row in inbound_rows
    )


def test_runner_channel_manager_runtime_stopped_cancelled_uses_cancellation_projection() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_runner_assigned_task(db, tenant=tenant, runner=runner)
    task.status = "stopping"
    db.flush()
    runtime_stop_job = _seed_runtime_job(db, tenant=tenant, runner=runner, task=task, job_type="task.stop")
    manager = RunnerChannelManager(db)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "remote_runtime.v1"),
        )
    )
    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )

    result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runtime.stopped",
            schema_version="remote_runtime.v1",
            runtime_job_id=str(runtime_stop_job.id),
            task_id=task.id,
            payload={
                "operation_id": "op-stop-cancel",
                "status": "succeeded",
                "error_code": None,
                "error_message": None,
                "result": {"runtime_job_id": str(runtime_stop_job.id), "task_id": task.id, "lifecycle_outcome": "cancelled"},
            },
        ),
    )
    db.commit()

    assert result.response_envelopes == ()
    refreshed_runtime_job = db.execute(select(RuntimeJob).where(RuntimeJob.id == runtime_stop_job.id)).scalar_one()
    refreshed_task = db.execute(select(Task).where(Task.id == task.id)).scalar_one()
    latest_history = db.execute(
        select(TaskHistory).where(TaskHistory.task_id == task.id).order_by(TaskHistory.timestamp.desc())
    ).scalar_one()
    assert refreshed_runtime_job.status == "cancelled"
    assert refreshed_task.status == "stopped"
    assert latest_history.transition_reason == "Runner lifecycle cancellation completed"


def test_runner_channel_manager_runtime_retired_triggers_retirement_cleanup(
    monkeypatch,
) -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_runner_assigned_task(db, tenant=tenant, runner=runner)
    task.status = "stopping"
    db.flush()
    runtime_retire_job = _seed_runtime_job(db, tenant=tenant, runner=runner, task=task, job_type="task.retire")
    manager = RunnerChannelManager(db)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "remote_runtime.v1"),
        )
    )
    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )

    cleaned_task_ids: list[int] = []

    async def _fake_cleanup_runtime_stream_state(*, task_id: int) -> None:
        cleaned_task_ids.append(task_id)

    monkeypatch.setattr(
        "backend.services.task.retirement_service.TaskRetirementService.cleanup_runtime_stream_state",
        _fake_cleanup_runtime_stream_state,
    )

    result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runtime.retired",
            schema_version="remote_runtime.v1",
            runtime_job_id=str(runtime_retire_job.id),
            task_id=task.id,
            payload={
                "operation_id": "op-retire",
                "status": "succeeded",
                "error_code": None,
                "error_message": None,
                "result": {"runtime_job_id": str(runtime_retire_job.id), "task_id": task.id, "lifecycle_outcome": "retired"},
            },
        ),
    )
    db.commit()

    assert result.response_envelopes == ()
    refreshed_runtime_job = db.execute(select(RuntimeJob).where(RuntimeJob.id == runtime_retire_job.id)).scalar_one()
    refreshed_task = db.execute(select(Task).where(Task.id == task.id)).scalar_one()
    assert refreshed_runtime_job.status == "succeeded"
    assert refreshed_task.status == "stopped"
    assert cleaned_task_ids == [task.id]


def test_runner_channel_manager_rejects_remote_runtime_event_labeled_with_runner_control_schema() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    manager = RunnerChannelManager(db)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "remote_runtime.v1"),
        )
    )

    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )

    result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runtime.started",
            schema_version="runner_control.v1",
            payload={
                "operation_id": "op-1",
                "status": "succeeded",
                "error_code": None,
                "error_message": None,
                "result": {"runtime_job_id": "job-1", "task_id": 42, "workspace_id": "task-42"},
            },
        ),
    )

    assert result.should_close is True
    assert result.response_envelopes
    error_envelope = result.response_envelopes[0]
    assert isinstance(error_envelope.payload, RunnerErrorPayload)
    assert error_envelope.payload.error_code == "RUNNER_PROTOCOL_UNSUPPORTED"


def test_runner_channel_manager_rejects_remote_runtime_event_with_unsupported_schema_version() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    manager = RunnerChannelManager(db)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "remote_runtime.v1"),
        )
    )

    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )

    result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runtime.started",
            schema_version="remote_runtime.v0",
            payload={
                "operation_id": "op-1",
                "status": "succeeded",
                "error_code": None,
                "error_message": None,
                "result": {"runtime_job_id": "job-1", "task_id": 42, "workspace_id": "task-42"},
            },
        ),
    )

    assert result.should_close is True
    assert result.response_envelopes
    error_envelope = result.response_envelopes[0]
    assert isinstance(error_envelope.payload, RunnerErrorPayload)
    assert error_envelope.payload.error_code == "RUNNER_PROTOCOL_UNSUPPORTED"


def test_runner_channel_manager_artifact_manifest_delegates_side_effects_and_preserves_idempotency() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_runner_assigned_task(db, tenant=tenant, runner=runner)
    workspace_id = f"task-{task.id}"
    task_runtime_job = _seed_runtime_job_with_payload(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        job_type="task.start",
        payload_json={"workspace_id": workspace_id},
    )
    tool_runtime_job = _seed_runtime_job_with_payload(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        job_type="tool.command",
        payload_json={
            "workspace_id": workspace_id,
            "command_id": "cmd-42",
            "task_runtime_job_id": str(task_runtime_job.id),
        },
    )
    artifact_service = _RecordingArtifactManifestService()
    manager = RunnerChannelManager(db, artifact_manifest_service=artifact_service)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "data_plane.v1"),
        )
    )
    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )

    message_id = "msg-artifact-manifest-idempotent"
    payload_json = _artifact_manifest_envelope_json(
        tenant_id=tenant.id,
        runner_id=runner.id,
        runtime_job_id=str(tool_runtime_job.id),
        task_id=task.id,
        message_id=message_id,
        command_id="cmd-42",
        task_runtime_job_id=str(task_runtime_job.id),
        workspace_id=workspace_id,
        metadata={"signed_url": "https://example.local/upload?signature=secret"},
    )
    first_result = manager.handle_inbound_json(session, payload_json)
    second_result = manager.handle_inbound_json(session, payload_json)

    assert first_result.response_envelopes == ()
    assert second_result.response_envelopes == ()
    assert len(artifact_service.calls) == 1

    inbound_rows = db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "inbound",
            RunnerControlMessage.message_id == message_id,
            RunnerControlMessage.type == "artifact.manifest",
        )
    ).scalars().all()
    assert len(inbound_rows) == 1
    assert inbound_rows[0].status == "accepted"
    payload = inbound_rows[0].payload_json or {}
    artifacts = payload.get("artifacts") if isinstance(payload, dict) else None
    first_artifact = artifacts[0] if isinstance(artifacts, list) and artifacts else {}
    metadata = first_artifact.get("metadata") if isinstance(first_artifact, dict) else {}
    assert metadata.get("signed_url") == "<redacted>"


def test_runner_channel_manager_artifact_manifest_rejects_workspace_mismatch_and_records_rejected_decision() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_runner_assigned_task(db, tenant=tenant, runner=runner)
    task_runtime_job = _seed_runtime_job_with_payload(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        job_type="task.start",
        payload_json={"workspace_id": f"task-{task.id}"},
    )
    tool_runtime_job = _seed_runtime_job_with_payload(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        job_type="tool.command",
        payload_json={
            "workspace_id": f"task-{task.id}",
            "command_id": "cmd-42",
            "task_runtime_job_id": str(task_runtime_job.id),
        },
    )
    artifact_service = _RecordingArtifactManifestService()
    manager = RunnerChannelManager(db, artifact_manifest_service=artifact_service)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "data_plane.v1"),
        )
    )
    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )

    message_id = "msg-artifact-manifest-ws-mismatch"
    result = manager.handle_inbound_json(
        session,
        _artifact_manifest_envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            runtime_job_id=str(tool_runtime_job.id),
            task_id=task.id,
            message_id=message_id,
            command_id="cmd-42",
            task_runtime_job_id=str(task_runtime_job.id),
            workspace_id=f"task-{task.id + 1}",
        ),
    )

    assert result.should_close is False
    assert result.response_envelopes
    error_envelope = result.response_envelopes[0]
    assert isinstance(error_envelope.payload, RunnerErrorPayload)
    assert error_envelope.payload.error_code == "RUNNER_WORKSPACE_MISMATCH"
    assert artifact_service.calls == []

    rejected_row = db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "inbound",
            RunnerControlMessage.message_id == message_id,
            RunnerControlMessage.type == "artifact.manifest",
        )
    ).scalar_one()
    assert rejected_row.status == "rejected"
    assert rejected_row.error_code == "RUNNER_WORKSPACE_MISMATCH"


def test_runner_channel_manager_artifact_upload_complete_accepts_matching_manifest_artifact_identity(tmp_path) -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_runner_assigned_task(db, tenant=tenant, runner=runner)
    workspace_id = f"task-{task.id}"
    task_runtime_job = _seed_runtime_job_with_payload(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        job_type="task.start",
        payload_json={"workspace_id": workspace_id},
    )
    tool_runtime_job = _seed_runtime_job_with_payload(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        job_type="tool.command",
        payload_json={
            "workspace_id": workspace_id,
            "command_id": "cmd-42",
            "task_runtime_job_id": str(task_runtime_job.id),
        },
    )

    execution = ToolExecution(
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        runner_id=runner.id,
        execution_site_id=runner.execution_site_id,
        command_id="cmd-42",
        workspace_id=workspace_id,
        tool_name="shell.exec",
        tool_arguments={"command": "echo ok"},
        agent_path="runner.tool_command",
        status="pending",
        started_at=datetime.now(tz=UTC),
    )
    db.add(execution)
    db.flush()

    manifest = ArtifactManifest(
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        runner_id=runner.id,
        command_id="cmd-42",
        workspace_id=workspace_id,
        message_id="msg-manifest-accepted",
        idempotency_key="tenant:runner:msg-manifest-accepted",
        status="accepted",
    )
    db.add(manifest)
    db.flush()

    artifact = ExecutionArtifact(
        execution_id=execution.id,
        manifest_id=manifest.id,
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        runner_id=runner.id,
        command_id="cmd-42",
        artifact_kind="stdout",
        relative_path="artifacts/cmd-42/stdout.txt",
        object_key="tenant-1/task-1/cmd-42/stdout.txt",
        storage_backend="local",
        upload_status="uploading",
        content_sha256="a" * 64,
        byte_size=10,
        mime_type="text/plain",
        artifact_metadata={"artifact_client_id": "artifact-client-1"},
    )
    db.add(artifact)
    db.commit()

    object_store = LocalObjectStore(root_path=tmp_path / "objects")
    object_store.put_bytes(
        "tenant-1/task-1/cmd-42/stdout.txt",
        b"0123456789",
        content_type="text/plain",
    )
    upload_service = ArtifactUploadService(db, object_store=object_store)
    manager = RunnerChannelManager(db, artifact_upload_service=upload_service)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "data_plane.v1"),
        )
    )
    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )

    result = manager.handle_inbound_json(
        session,
        _artifact_upload_complete_envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            runtime_job_id=str(tool_runtime_job.id),
            task_id=task.id,
            message_id="msg-upload-complete-accepted",
            command_id="cmd-42",
            task_runtime_job_id=str(task_runtime_job.id),
            workspace_id=workspace_id,
            artifact_id=str(artifact.id),
            artifact_client_id="artifact-client-1",
            object_key="tenant-1/task-1/cmd-42/stdout.txt",
        ),
    )

    assert result.response_envelopes == ()
    db.refresh(artifact)
    db.refresh(manifest)
    assert artifact.upload_status == "ready"
    assert manifest.status == "ready"


def test_runner_channel_manager_artifact_upload_complete_rejects_unaccepted_identity() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_runner_assigned_task(db, tenant=tenant, runner=runner)
    workspace_id = f"task-{task.id}"
    task_runtime_job = _seed_runtime_job_with_payload(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        job_type="task.start",
        payload_json={"workspace_id": workspace_id},
    )
    tool_runtime_job = _seed_runtime_job_with_payload(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        job_type="tool.command",
        payload_json={
            "workspace_id": workspace_id,
            "command_id": "cmd-42",
            "task_runtime_job_id": str(task_runtime_job.id),
        },
    )

    execution = ToolExecution(
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        runner_id=runner.id,
        execution_site_id=runner.execution_site_id,
        command_id="cmd-42",
        workspace_id=workspace_id,
        tool_name="shell.exec",
        tool_arguments={"command": "echo ok"},
        agent_path="runner.tool_command",
        status="pending",
        started_at=datetime.now(tz=UTC),
    )
    db.add(execution)
    db.flush()

    manifest = ArtifactManifest(
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        runner_id=runner.id,
        command_id="cmd-42",
        workspace_id=workspace_id,
        message_id="msg-manifest-accepted",
        status="accepted",
    )
    db.add(manifest)
    db.flush()

    artifact = ExecutionArtifact(
        execution_id=execution.id,
        manifest_id=manifest.id,
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        runner_id=runner.id,
        command_id="cmd-42",
        artifact_kind="stdout",
        relative_path="artifacts/cmd-42/stdout.txt",
        object_key="tenant-1/task-1/cmd-42/stdout.txt",
        storage_backend="local",
        upload_status="uploading",
        content_sha256="a" * 64,
        byte_size=10,
        mime_type="text/plain",
        artifact_metadata={"artifact_client_id": "artifact-client-1"},
    )
    db.add(artifact)
    db.commit()

    manager = RunnerChannelManager(db)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "data_plane.v1"),
        )
    )
    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )

    result = manager.handle_inbound_json(
        session,
        _artifact_upload_complete_envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            runtime_job_id=str(tool_runtime_job.id),
            task_id=task.id,
            message_id="msg-upload-complete-rejected",
            command_id="cmd-42",
            task_runtime_job_id=str(task_runtime_job.id),
            workspace_id=workspace_id,
            artifact_id=str(artifact.id),
            artifact_client_id="artifact-client-1",
            object_key="tenant-1/task-1/cmd-42/WRONG.txt",
        ),
    )

    assert result.response_envelopes
    error_envelope = result.response_envelopes[0]
    assert isinstance(error_envelope.payload, RunnerErrorPayload)
    assert error_envelope.payload.error_code == "RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED"


def test_runner_channel_manager_artifact_manifest_duplicate_replays_prior_rejection_without_reapplying_side_effects() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_runner_assigned_task(db, tenant=tenant, runner=runner)
    workspace_id = f"task-{task.id}"
    task_runtime_job = _seed_runtime_job_with_payload(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        job_type="task.start",
        payload_json={"workspace_id": workspace_id},
    )
    tool_runtime_job = _seed_runtime_job_with_payload(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        job_type="tool.command",
        payload_json={
            "workspace_id": workspace_id,
            "command_id": "cmd-42",
            "task_runtime_job_id": str(task_runtime_job.id),
        },
    )
    failing_service = _FailingArtifactManifestService(
        error_code="RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED",
        message="artifact identity rejected",
    )
    manager = RunnerChannelManager(db, artifact_manifest_service=failing_service)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=("runner_control.v1", "data_plane.v1"),
        )
    )
    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_type="runner.hello",
            payload={"version": "1.9.0", "capabilities": ["docker"], "labels": {"site": "hq"}},
        ),
    )

    payload_json = _artifact_manifest_envelope_json(
        tenant_id=tenant.id,
        runner_id=runner.id,
        runtime_job_id=str(tool_runtime_job.id),
        task_id=task.id,
        message_id="msg-artifact-manifest-replay-reject",
        command_id="cmd-42",
        task_runtime_job_id=str(task_runtime_job.id),
        workspace_id=workspace_id,
    )
    first_result = manager.handle_inbound_json(session, payload_json)
    second_result = manager.handle_inbound_json(session, payload_json)

    assert failing_service.calls == 1
    assert first_result.response_envelopes
    assert second_result.response_envelopes
    first_error = first_result.response_envelopes[0]
    second_error = second_result.response_envelopes[0]
    assert isinstance(first_error.payload, RunnerErrorPayload)
    assert isinstance(second_error.payload, RunnerErrorPayload)
    assert first_error.payload.error_code == "RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED"
    assert second_error.payload.error_code == "RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED"
