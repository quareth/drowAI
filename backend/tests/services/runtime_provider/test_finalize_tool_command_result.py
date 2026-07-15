"""Tests for boundary-safe finalize_tool_command_result provider behavior."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models.core import Task, User
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.models.runner_control import ExecutionSite, Runner, RuntimeJob
from backend.models.tenant import Tenant
from backend.services.runtime_provider.cloud_runner_provider import CloudRunnerRuntimeProvider
from backend.services.runtime_provider.contracts import (
    RuntimeActorType,
    RuntimeOperationRequest,
    RuntimeOperationStatus,
    RuntimePlacementMode,
)
from runtime_shared.runner_protocol import RUNNER_TOOL_RESULT_COMPLETED_STATUS


def _build_session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _seed_tool_command_job(db: Session) -> tuple[Tenant, Runner, Task, RuntimeJob, str]:
    tenant = Tenant(slug=f"tenant-{uuid.uuid4().hex[:8]}", name="Tenant")
    db.add(tenant)
    db.flush()

    user = User(
        username=f"user-{uuid.uuid4().hex[:8]}",
        password="test-password",
        email=f"user-{uuid.uuid4().hex[:8]}@example.com",
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
        capabilities_json=["tool_command.v1"],
        last_seen_at=datetime.now(tz=UTC),
    )
    db.add(runner)
    db.flush()

    task = Task(
        user_id=user.id,
        tenant_id=tenant.id,
        name="Finalize Test Task",
        runtime_placement_mode="runner",
        runner_id=str(runner.id).lower(),
    )
    db.add(task)
    db.flush()
    workspace_id = f"task-{task.id}"
    task.workspace_id = workspace_id

    command_id = "cmd-fping-1"
    tool_job = RuntimeJob(
        tenant_id=tenant.id,
        runner_id=runner.id,
        execution_site_id=site.id,
        task_id=task.id,
        job_type="tool.command",
        status="running",
        idempotency_key=f"tool-command-{uuid.uuid4()}",
        payload_json={
            "command_id": command_id,
            "workspace_id": workspace_id,
            "tool": "information_gathering.network_discovery.fping",
            "command": "fping -a -q 192.168.1.0/24",
            "task_runtime_job_id": str(uuid.uuid4()),
        },
        result_json={
            "source": "runner_event",
            "status": RUNNER_TOOL_RESULT_COMPLETED_STATUS,
            "success": False,
            "exit_code": 1,
            "stdout": "alive hosts",
            "stderr": "",
            "artifacts": [],
            "metadata": {
                "process_success": False,
                "process_exit_code": 1,
            },
        },
    )
    db.add(tool_job)
    db.commit()
    return tenant, runner, task, tool_job, workspace_id


def _finalize_request(
    *,
    tenant: Tenant,
    runner: Runner,
    task: Task,
    tool_job: RuntimeJob,
    workspace_id: str,
    command_id: str = "cmd-fping-1",
    task_id_override: int | None = None,
    runner_id_override: str | None = None,
    workspace_id_override: str | None = None,
    command_id_override: str | None = None,
) -> RuntimeOperationRequest:
    return RuntimeOperationRequest(
        tenant_id=tenant.id,
        task_id=task_id_override if task_id_override is not None else task.id,
        user_id=None,
        actor_type=RuntimeActorType.USER,
        actor_id="tester",
        runtime_placement_mode=RuntimePlacementMode.RUNNER,
        workspace_id=workspace_id_override or workspace_id,
        runner_id=runner_id_override or str(runner.id),
        execution_site_id=str(runner.execution_site_id),
        operation="finalize_tool_command_result",
        payload={
            "tool_command_runtime_job_id": str(tool_job.id),
            "task_runtime_job_id": tool_job.payload_json["task_runtime_job_id"],
            "command_id": command_id_override or command_id,
            "workspace_id": workspace_id_override or workspace_id,
            "tool": "information_gathering.network_discovery.fping",
            "canonical_status": "succeeded",
            "canonical_success": True,
            "canonical_exit_code": 1,
            "stdout": "alive hosts",
            "stderr": "",
            "artifacts": ["artifacts/fping_123.txt"],
            "process_success": False,
            "process_exit_code": 1,
        },
        metadata={"wait_for_result": True},
    )


def test_finalize_tool_command_result_terminalizes_completed_job_once() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant, runner, task, tool_job, workspace_id = _seed_tool_command_job(db)

    provider = CloudRunnerRuntimeProvider(session_factory=factory)
    request = _finalize_request(
        tenant=tenant,
        runner=runner,
        task=task,
        tool_job=tool_job,
        workspace_id=workspace_id,
    )

    result = asyncio.run(provider.finalize_tool_command_result(request))

    assert result.ok is True
    assert result.status is RuntimeOperationStatus.SUCCEEDED
    assert result.metadata["runtime_job_status"] == "succeeded"

    with factory() as db:
        refreshed = db.execute(select(RuntimeJob).where(RuntimeJob.id == tool_job.id)).scalar_one()
        assert refreshed.status == "succeeded"
        assert refreshed.result_json["status"] == "succeeded"
        assert refreshed.result_json["success"] is True
        assert refreshed.result_json["exit_code"] == 1
        assert refreshed.result_json["metadata"]["process_exit_code"] == 1

        execution = db.execute(
            select(ToolExecution).where(ToolExecution.runtime_job_id == tool_job.id)
        ).scalar_one()
        assert execution.status == "succeeded"
        assert execution.exit_code == 1

        command_artifact = (
            db.execute(
                select(ExecutionArtifact).where(
                    ExecutionArtifact.execution_id == execution.id,
                    ExecutionArtifact.artifact_kind == "command",
                )
            )
            .scalars()
            .one()
        )
        assert command_artifact.content_text == "fping -a -q 192.168.1.0/24"


def test_finalize_tool_command_result_rejects_identity_mismatch() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant, runner, task, tool_job, workspace_id = _seed_tool_command_job(db)

    provider = CloudRunnerRuntimeProvider(session_factory=factory)
    bad_request = _finalize_request(
        tenant=tenant,
        runner=runner,
        task=task,
        tool_job=tool_job,
        workspace_id=workspace_id,
        command_id_override="wrong-command",
    )

    result = asyncio.run(provider.finalize_tool_command_result(bad_request))

    assert result.ok is False
    assert result.error_code == "RUNNER_TOOL_COMMAND_ID_MISMATCH"

    with factory() as db:
        refreshed = db.execute(select(RuntimeJob).where(RuntimeJob.id == tool_job.id)).scalar_one()
        assert refreshed.status == "running"


def test_finalize_tool_command_result_rejects_workspace_mismatch() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant, runner, task, tool_job, workspace_id = _seed_tool_command_job(db)

    provider = CloudRunnerRuntimeProvider(session_factory=factory)
    bad_request = _finalize_request(
        tenant=tenant,
        runner=runner,
        task=task,
        tool_job=tool_job,
        workspace_id=workspace_id,
        workspace_id_override=f"{workspace_id}-other",
    )

    result = asyncio.run(provider.finalize_tool_command_result(bad_request))

    assert result.ok is False
    assert result.status is RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNNER_WORKSPACE_MISMATCH"

    with factory() as db:
        refreshed = db.execute(select(RuntimeJob).where(RuntimeJob.id == tool_job.id)).scalar_one()
        assert refreshed.status == "running"


def test_finalize_tool_command_result_rejects_non_tool_command_runtime_job() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant, runner, task, tool_job, workspace_id = _seed_tool_command_job(db)
        tool_job.job_type = "task.start"
        db.commit()

    provider = CloudRunnerRuntimeProvider(session_factory=factory)
    bad_request = _finalize_request(
        tenant=tenant,
        runner=runner,
        task=task,
        tool_job=tool_job,
        workspace_id=workspace_id,
    )

    result = asyncio.run(provider.finalize_tool_command_result(bad_request))

    assert result.ok is False
    assert result.status is RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNTIME_JOB_BINDING_INVALID"

    with factory() as db:
        refreshed = db.execute(select(RuntimeJob).where(RuntimeJob.id == tool_job.id)).scalar_one()
        assert refreshed.status == "running"


def test_finalize_tool_command_result_rejects_conflicting_terminal_status() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant, runner, task, tool_job, workspace_id = _seed_tool_command_job(db)
        tool_job.status = "failed"
        db.commit()

    provider = CloudRunnerRuntimeProvider(session_factory=factory)
    request = _finalize_request(
        tenant=tenant,
        runner=runner,
        task=task,
        tool_job=tool_job,
        workspace_id=workspace_id,
    )

    result = asyncio.run(provider.finalize_tool_command_result(request))

    assert result.ok is False
    assert result.status is RuntimeOperationStatus.REJECTED
    assert result.error_code == "RUNTIME_JOB_TRANSITION_STALE"

    with factory() as db:
        refreshed = db.execute(select(RuntimeJob).where(RuntimeJob.id == tool_job.id)).scalar_one()
        assert refreshed.status == "failed"


def test_finalize_tool_command_result_rejects_wrong_task_id_sync() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant, runner, task, tool_job, workspace_id = _seed_tool_command_job(db)

    provider = CloudRunnerRuntimeProvider(session_factory=factory)
    bad_request = _finalize_request(
        tenant=tenant,
        runner=runner,
        task=task,
        tool_job=tool_job,
        workspace_id=workspace_id,
        task_id_override=task.id + 999,
    )

    result = asyncio.run(provider.finalize_tool_command_result(bad_request))
    assert result.ok is False
    assert result.error_code == "RUNTIME_JOB_NOT_FOUND"
