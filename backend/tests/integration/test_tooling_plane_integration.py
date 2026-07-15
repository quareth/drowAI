"""Tooling-plane integration coverage across provider, channel, and lane boundaries.

This module validates fail-closed and mixed-lane behavior with real backend
runner-control components wired against a temporary SQLite database.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from agent.graph.subgraphs.tool_execution_runtime.lane_dispatch import resolve_tool_lane_dispatch
from backend.database import Base
from backend.models.core import Task, User
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
from backend.services.runner_control.dispatcher import (
    DispatchAttemptResult,
    RunnerOutboundDispatcher,
    RunnerOutboundTransport,
)
from backend.services.runtime_provider.cloud_runner_provider import CloudRunnerRuntimeProvider
from backend.services.runtime_provider.contracts import (
    RuntimeActorType,
    RuntimeOperationRequest,
    RuntimeOperationStatus,
    RuntimePlacementMode,
)


class _OfflineTransport(RunnerOutboundTransport):
    async def send(self, envelope, *, timeout_seconds: float) -> DispatchAttemptResult:
        del envelope, timeout_seconds
        return DispatchAttemptResult(
            delivered=False,
            acked=False,
            timed_out=False,
            error_code="RUNNER_OFFLINE",
            error_message="Runner is offline.",
            retryable=True,
        )


def _build_session_factory(database_path: Path) -> sessionmaker[Session]:
    engine = create_engine(
        f"sqlite+pysqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            Task.__table__,
            ExecutionSite.__table__,
            Runner.__table__,
            RunnerCredential.__table__,
            RunnerConnection.__table__,
            RuntimeJob.__table__,
            RunnerControlMessage.__table__,
        ],
    )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _seed_runner_context(db: Session) -> tuple[Tenant, Runner, Task, RuntimeJob, str]:
    tenant = Tenant(slug=f"tenant-{uuid.uuid4().hex[:8]}", name="Tenant")
    db.add(tenant)
    db.flush()

    user = User(
        username=f"tooling-plane-user-{uuid.uuid4().hex[:8]}",
        password="test-password",
        email=f"tooling-plane-{uuid.uuid4().hex[:8]}@example.com",
    )
    db.add(user)
    db.flush()

    site = ExecutionSite(
        tenant_id=tenant.id,
        name="Primary Site",
        slug=f"site-{uuid.uuid4().hex[:8]}",
        status="active",
    )
    db.add(site)
    db.flush()

    runner = Runner(
        tenant_id=tenant.id,
        execution_site_id=site.id,
        name=f"runner-{uuid.uuid4().hex[:8]}",
        status="active",
        capabilities_json=["docker", "tool_command.v1", "tooling_plane.commands.v1"],
        last_seen_at=datetime.now(tz=UTC),
    )
    db.add(runner)
    db.flush()

    task = Task(
        user_id=user.id,
        tenant_id=tenant.id,
        name="Tooling Plane Task",
        runtime_placement_mode="runner",
        runner_id=str(runner.id).lower(),
    )
    db.add(task)
    db.flush()

    workspace_id = f"task-{task.id}"
    task.workspace_id = workspace_id

    task_start_job = RuntimeJob(
        tenant_id=tenant.id,
        runner_id=runner.id,
        execution_site_id=site.id,
        task_id=task.id,
        job_type="task.start",
        status="dispatched",
        idempotency_key=f"task-start-{uuid.uuid4()}",
        payload_json={"workspace_id": workspace_id},
    )
    db.add(task_start_job)
    db.commit()
    return tenant, runner, task, task_start_job, workspace_id


def _open_runner_session(db: Session, *, tenant_id: int, runner_id: uuid.UUID) -> tuple[RunnerChannelManager, object]:
    manager = RunnerChannelManager(db)
    issued = RunnerCredentialService(db).issue_runner_credential(tenant_id=tenant_id, runner_id=runner_id)
    db.commit()

    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant_id,
            runner_id=runner_id,
            credential_id=issued.credential_id,
            allowed_protocol_versions=("runner_control.v1", "tooling_plane.v1"),
        )
    )
    manager.handle_inbound_json(
        session,
        json.dumps(
            {
                "message_id": "hello-tooling-plane",
                "type": "runner.hello",
                "schema_version": "runner_control.v1",
                "tenant_id": str(tenant_id),
                "runner_id": str(runner_id),
                "correlation_id": None,
                "runtime_job_id": None,
                "task_id": None,
                "created_at": "2026-05-24T18:00:00+00:00",
                "payload": {
                    "version": "1.9.0",
                    "capabilities": ["docker", "tool_command.v1", "tooling_plane.commands.v1"],
                    "labels": {"site": "hq"},
                },
            }
        ),
    )
    db.commit()
    return manager, session


def _tool_result_envelope(
    *,
    tenant_id: int,
    runner_id: uuid.UUID,
    message_id: str,
    runtime_job_id: uuid.UUID,
    task_id: int,
    command_id: str,
    status: str,
    success: bool,
    exit_code: int,
    task_runtime_job_id: uuid.UUID,
    workspace_id: str,
    stdout: str = "",
    stderr: str = "",
) -> str:
    return json.dumps(
        {
            "message_id": message_id,
            "type": "tool.result",
            "schema_version": "tooling_plane.v1",
            "tenant_id": str(tenant_id),
            "runner_id": str(runner_id),
            "correlation_id": f"corr-{message_id}",
            "runtime_job_id": str(runtime_job_id),
            "task_id": task_id,
            "created_at": "2026-05-24T18:01:00+00:00",
            "payload": {
                "operation_id": f"op-{command_id}",
                "command_id": command_id,
                "tool": "shell.exec",
                "status": status,
                "success": success,
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "artifacts": [f"artifacts/{command_id}/stdout.txt"],
                "error_code": None if success else "TOOL_FAILED",
                "error_message": None if success else "Tool failed.",
                "result": {"duration_seconds": 0.1},
                "metadata": {
                    "task_runtime_job_id": str(task_runtime_job_id),
                    "workspace_id": workspace_id,
                },
            },
        }
    )


def test_tooling_plane_provider_enqueues_tool_command_and_times_out_without_result(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "1")

    factory = _build_session_factory(tmp_path / "tooling-plane-provider-timeout.db")
    with factory() as db:
        tenant, runner, task, _task_start_job, workspace_id = _seed_runner_context(db)
        tenant_id = tenant.id
        runner_uuid = runner.id
        task_id = task.id

    provider = CloudRunnerRuntimeProvider(session_factory=factory)
    request = RuntimeOperationRequest(
        tenant_id=tenant_id,
        task_id=task_id,
        actor_type=RuntimeActorType.SYSTEM,
        actor_id="scheduler",
        runtime_placement_mode=RuntimePlacementMode.RUNNER,
        workspace_id=workspace_id,
        operation="send_tool_command",
        runner_id=str(runner_uuid),
        payload={
            "tool": "shell.exec",
            "command_id": "cmd-timeout-integration",
            "command": "id",
            "timeout_seconds": 5.0,
            "timeout_policy": {"deadline_seconds": 0.01, "grace_seconds": 0.01},
        },
        metadata={
            "lane_dispatch": {
                "lane": "container_scoped",
                "authority": "container_runner_transport",
            },
            "wait_for_result": True,
            "ack_wait_timeout_seconds": 0.0,
            "wait_timeout_seconds": 0.0,
            "wait_poll_seconds": 0.01,
        },
    )

    result = asyncio.run(provider.send_tool_command(request))

    assert result.status == RuntimeOperationStatus.FAILED
    assert result.error_code == "TOOL_RESULT_TIMEOUT"
    with factory() as verify_db:
        command_jobs = verify_db.execute(
            select(RuntimeJob).where(
                RuntimeJob.tenant_id == tenant_id,
                RuntimeJob.task_id == task_id,
                RuntimeJob.job_type == "tool.command",
            )
        ).scalars().all()
        assert len(command_jobs) == 1
        assert command_jobs[0].status == "failed"

        outbound_messages = verify_db.execute(
            select(RunnerControlMessage).where(
                RunnerControlMessage.tenant_id == tenant_id,
                RunnerControlMessage.runner_id == runner_uuid,
                RunnerControlMessage.direction == "outbound",
                RunnerControlMessage.type == "tool.command",
            )
        ).scalars().all()
        assert len(outbound_messages) == 1


def test_tooling_plane_tool_result_ingestion_covers_success_failure_and_duplicate(tmp_path: Path) -> None:
    factory = _build_session_factory(tmp_path / "tooling-plane-tool-result-ingest.db")
    with factory() as db:
        tenant, runner, task, task_start_job, workspace_id = _seed_runner_context(db)

        success_job = RuntimeJob(
            tenant_id=tenant.id,
            runner_id=runner.id,
            execution_site_id=runner.execution_site_id,
            task_id=task.id,
            job_type="tool.command",
            status="dispatched",
            idempotency_key=f"tool-success-{uuid.uuid4()}",
            payload_json={
                "command_id": "cmd-success",
                "task_runtime_job_id": str(task_start_job.id),
                "workspace_id": workspace_id,
                "metadata": {
                    "command_id": "cmd-success",
                    "task_runtime_job_id": str(task_start_job.id),
                    "workspace_id": workspace_id,
                },
            },
        )
        failed_job = RuntimeJob(
            tenant_id=tenant.id,
            runner_id=runner.id,
            execution_site_id=runner.execution_site_id,
            task_id=task.id,
            job_type="tool.command",
            status="dispatched",
            idempotency_key=f"tool-failed-{uuid.uuid4()}",
            payload_json={
                "command_id": "cmd-failed",
                "task_runtime_job_id": str(task_start_job.id),
                "workspace_id": workspace_id,
                "metadata": {
                    "command_id": "cmd-failed",
                    "task_runtime_job_id": str(task_start_job.id),
                    "workspace_id": workspace_id,
                },
            },
        )
        db.add(success_job)
        db.add(failed_job)
        db.commit()

        manager, session = _open_runner_session(db, tenant_id=tenant.id, runner_id=runner.id)

        manager.handle_inbound_json(
            session,
            _tool_result_envelope(
                tenant_id=tenant.id,
                runner_id=runner.id,
                message_id="tool-result-success-1",
                runtime_job_id=success_job.id,
                task_id=task.id,
                command_id="cmd-success",
                status="succeeded",
                success=True,
                exit_code=0,
                task_runtime_job_id=task_start_job.id,
                workspace_id=workspace_id,
                stdout="first",
            ),
        )
        manager.handle_inbound_json(
            session,
            _tool_result_envelope(
                tenant_id=tenant.id,
                runner_id=runner.id,
                message_id="tool-result-success-duplicate",
                runtime_job_id=success_job.id,
                task_id=task.id,
                command_id="cmd-success",
                status="succeeded",
                success=True,
                exit_code=0,
                task_runtime_job_id=task_start_job.id,
                workspace_id=workspace_id,
                stdout="second",
            ),
        )
        manager.handle_inbound_json(
            session,
            _tool_result_envelope(
                tenant_id=tenant.id,
                runner_id=runner.id,
                message_id="tool-result-failed-1",
                runtime_job_id=failed_job.id,
                task_id=task.id,
                command_id="cmd-failed",
                status="failed",
                success=False,
                exit_code=1,
                task_runtime_job_id=task_start_job.id,
                workspace_id=workspace_id,
                stderr="boom",
            ),
        )
        db.commit()

        refreshed_success = db.get(RuntimeJob, success_job.id)
        refreshed_failed = db.get(RuntimeJob, failed_job.id)
        assert refreshed_success is not None
        assert refreshed_failed is not None
        assert refreshed_success.status == "succeeded"
        assert refreshed_success.result_json["stdout"] == "first"
        assert refreshed_failed.status == "failed"
        assert refreshed_failed.error_code == "TOOL_FAILED"

        inbound_results = db.execute(
            select(RunnerControlMessage).where(
                RunnerControlMessage.tenant_id == tenant.id,
                RunnerControlMessage.runner_id == runner.id,
                RunnerControlMessage.direction == "inbound",
                RunnerControlMessage.type == "tool.result",
            )
        ).scalars().all()
        assert len(inbound_results) == 2


def test_tooling_plane_dispatcher_marks_tool_command_failed_when_runner_offline_and_policy_is_fail(tmp_path: Path) -> None:
    factory = _build_session_factory(tmp_path / "tooling-plane-offline-dispatch.db")
    with factory() as db:
        tenant, runner, task, _task_start_job, _workspace_id = _seed_runner_context(db)
        now = datetime.now(tz=UTC)

        runtime_job = RuntimeJob(
            tenant_id=tenant.id,
            runner_id=runner.id,
            execution_site_id=runner.execution_site_id,
            task_id=task.id,
            job_type="tool.command",
            status="assigned",
            idempotency_key=f"offline-tool-command-{uuid.uuid4()}",
            payload_json={"command_id": "cmd-offline"},
        )
        db.add(runtime_job)
        db.flush()

        store = DBRunnerCoordinationStore(db, pod_id="pod-tooling-plane-offline")
        store.enqueue_outbound_message(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_id="tool-command-offline-1",
            message_type="tool.command",
            payload_json={
                "command_id": "cmd-offline",
                "tool": "shell.exec",
                "args": {"command": "id"},
                "delivery_policy": {"offline": "fail", "max_attempts": 1, "timeout_seconds": 1.0},
            },
            idempotency_key=f"tooling_plane:tool.command:{runtime_job.id}",
            runtime_job_id=runtime_job.id,
            task_id=task.id,
            correlation_id="corr-offline-1",
        )
        store.claim_connection_lease(
            tenant_id=tenant.id,
            runner_id=runner.id,
            pod_id="pod-tooling-plane-offline",
            connection_id="conn-offline",
            lease_expires_at=now + timedelta(seconds=120),
            last_seen_at=now,
        )
        db.commit()

        dispatcher = RunnerOutboundDispatcher(
            db,
            coordination_store=store,
            pod_id="pod-tooling-plane-offline",
        )
        result = asyncio.run(
            dispatcher.dispatch_for_connection(
                tenant_id=tenant.id,
                runner_id=runner.id,
                connection_id="conn-offline",
                transport=_OfflineTransport(),
            )
        )
        db.commit()

        assert result.claimed_count == 1
        assert result.failed_count == 1

        refreshed = db.get(RuntimeJob, runtime_job.id)
        assert refreshed is not None
        assert refreshed.status == "failed"
        assert refreshed.error_code == "RUNNER_OFFLINE"


def test_tooling_plane_mixed_lane_routing_keeps_runner_and_management_boundaries() -> None:
    shell = resolve_tool_lane_dispatch(tool_id="shell.exec", runtime_placement_mode="runner")
    filesystem = resolve_tool_lane_dispatch(tool_id="filesystem.read_file", runtime_placement_mode="runner")
    knowledge = resolve_tool_lane_dispatch(tool_id="knowledge.cve_lookup", runtime_placement_mode="runner")
    artifact_read = resolve_tool_lane_dispatch(tool_id="artifact.read", runtime_placement_mode="runner")
    artifact_search = resolve_tool_lane_dispatch(tool_id="artifact.search", runtime_placement_mode="runner")

    assert shell.lane == "container_scoped"
    assert shell.authority == "container_runner_transport"
    assert filesystem.lane == "container_scoped"
    assert filesystem.authority == "container_runner_transport"

    assert knowledge.lane == "backend_scoped"
    assert knowledge.authority == "backend_direct"

    assert artifact_read.lane == "artifact_scoped"
    assert artifact_read.authority == "artifact_direct"
    assert artifact_search.lane == "artifact_scoped"
    assert artifact_search.authority == "artifact_direct"

    mixed_authorities = [
        shell.authority,
        knowledge.authority,
        artifact_search.authority,
    ]
    assert mixed_authorities == [
        "container_runner_transport",
        "backend_direct",
        "artifact_direct",
    ]
