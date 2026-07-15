"""Tests tooling plane runner `tool.result` ingestion and runtime-job transitions."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

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
from runtime_shared.durable_secret_masking.masker import MASK_PREFIX
from runtime_shared.runner_protocol import RunnerErrorPayload


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
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
            RunnerControlMessage.__table__,
            RuntimeJob.__table__,
        ],
    )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


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
    db.flush()
    return tenant, runner


def _seed_task(db: Session, *, tenant: Tenant, runner: Runner) -> Task:
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


def _issue_credential_id(db: Session, *, tenant_id: int, runner_id: uuid.UUID) -> uuid.UUID:
    issued = RunnerCredentialService(db).issue_runner_credential(tenant_id=tenant_id, runner_id=runner_id)
    db.flush()
    return issued.credential_id


def _seed_task_start_job(
    db: Session,
    *,
    tenant: Tenant,
    runner: Runner,
    task: Task,
    workspace_id: str,
) -> RuntimeJob:
    job = RuntimeJob(
        tenant_id=tenant.id,
        runner_id=runner.id,
        execution_site_id=runner.execution_site_id,
        task_id=task.id,
        job_type="task.start",
        status="dispatched",
        idempotency_key=f"task-start-{uuid.uuid4()}",
        payload_json={"workspace_id": workspace_id},
    )
    db.add(job)
    db.flush()
    return job


def _seed_runtime_job(
    db: Session,
    *,
    tenant: Tenant,
    runner: Runner,
    task: Task,
    job_type: str,
) -> RuntimeJob:
    job = RuntimeJob(
        tenant_id=tenant.id,
        runner_id=runner.id,
        execution_site_id=runner.execution_site_id,
        task_id=task.id,
        job_type=job_type,
        status="dispatched",
        idempotency_key=f"{job_type}-{uuid.uuid4()}",
    )
    db.add(job)
    db.flush()
    return job


def _seed_tool_command_job(
    db: Session,
    *,
    tenant: Tenant,
    runner: Runner,
    task: Task,
    command_id: str,
    task_runtime_job_id: str,
    workspace_id: str,
) -> RuntimeJob:
    job = RuntimeJob(
        tenant_id=tenant.id,
        runner_id=runner.id,
        execution_site_id=runner.execution_site_id,
        task_id=task.id,
        job_type="tool.command",
        status="dispatched",
        idempotency_key=f"tool-command-{uuid.uuid4()}",
        payload_json={
            "command_id": command_id,
            "task_runtime_job_id": task_runtime_job_id,
            "workspace_id": workspace_id,
            "metadata": {
                "command_id": command_id,
                "task_runtime_job_id": task_runtime_job_id,
                "workspace_id": workspace_id,
            },
        },
    )
    db.add(job)
    db.flush()
    return job


def _envelope_json(
    *,
    tenant_id: int,
    runner_id: uuid.UUID,
    message_id: str,
    runtime_job_id: str,
    task_id: int | None,
    payload: dict[str, object],
    message_type: str = "tool.result",
    schema_version: str = "tooling_plane.v1",
) -> str:
    return json.dumps(
        {
            "message_id": message_id,
            "type": message_type,
            "schema_version": schema_version,
            "tenant_id": str(tenant_id),
            "runner_id": str(runner_id),
            "correlation_id": "corr-tool-result-1",
            "runtime_job_id": runtime_job_id,
            "task_id": task_id,
            "created_at": "2026-05-24T14:00:00+00:00",
            "payload": payload,
        }
    )


def _tool_result_payload(
    *,
    command_id: str,
    status: str,
    success: bool,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
    metadata: dict[str, object],
    result: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "operation_id": "tool-op-1",
        "command_id": command_id,
        "tool": "shell.exec",
        "status": status,
        "success": success,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "artifacts": ["artifacts/cmd-42/stdout.txt"],
        "error_code": None if success else "TOOL_FAILED",
        "error_message": None if success else "Tool execution failed.",
        "result": result if result is not None else {"duration_seconds": 0.1},
        "metadata": metadata,
    }


def _open_runner_session(
    db: Session,
    *,
    tenant: Tenant,
    runner: Runner,
    allowed_protocol_versions: tuple[str, ...] = ("runner_control.v1", "tooling_plane.v1"),
) -> tuple[RunnerChannelManager, object]:
    manager = RunnerChannelManager(db)
    credential_id = _issue_credential_id(db, tenant_id=tenant.id, runner_id=runner.id)
    session = manager.open_session(
        RunnerChannelAuthContext(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_id=credential_id,
            allowed_protocol_versions=allowed_protocol_versions,
        )
    )
    manager.handle_inbound_json(
        session,
        json.dumps(
            {
                "message_id": "hello-1",
                "type": "runner.hello",
                "schema_version": "runner_control.v1",
                "tenant_id": str(tenant.id),
                "runner_id": str(runner.id),
                "correlation_id": None,
                "runtime_job_id": None,
                "task_id": None,
                "created_at": "2026-05-24T14:00:00+00:00",
                "payload": {"version": "1.9.0", "capabilities": ["docker", "tool_command.v1"], "labels": {"site": "hq"}},
            }
        ),
    )
    return manager, session


def test_tool_result_transitions_runtime_job_to_succeeded_and_persists_result_shape() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_task(db, tenant=tenant, runner=runner)
    workspace_id = f"task-{task.id}"
    task_start_job = _seed_task_start_job(db, tenant=tenant, runner=runner, task=task, workspace_id=workspace_id)
    tool_command_job = _seed_tool_command_job(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        command_id="cmd-42",
        task_runtime_job_id=str(task_start_job.id),
        workspace_id=workspace_id,
    )
    manager, session = _open_runner_session(db, tenant=tenant, runner=runner)

    result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_id="tool-result-1",
            runtime_job_id=str(tool_command_job.id),
            task_id=task.id,
            payload=_tool_result_payload(
                command_id="cmd-42",
                status="succeeded",
                success=True,
                exit_code=0,
                stdout="ok",
                metadata={
                    "task_runtime_job_id": str(task_start_job.id),
                    "workspace_id": workspace_id,
                },
            ),
        ),
    )
    db.commit()

    assert result.response_envelopes == ()
    refreshed = db.execute(select(RuntimeJob).where(RuntimeJob.id == tool_command_job.id)).scalar_one()
    assert refreshed.status == "succeeded"
    assert isinstance(refreshed.result_json, dict)
    assert refreshed.result_json["command_id"] == "cmd-42"
    assert refreshed.result_json["tool"] == "shell.exec"
    assert refreshed.result_json["status"] == "succeeded"
    assert refreshed.result_json["success"] is True
    assert refreshed.result_json["stdout"] == "ok"
    assert refreshed.result_json["metadata"]["workspace_id"] == workspace_id


def test_tool_result_with_pending_artifact_manifest_does_not_terminalize_runtime_job() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_task(db, tenant=tenant, runner=runner)
    workspace_id = f"task-{task.id}"
    task_start_job = _seed_task_start_job(db, tenant=tenant, runner=runner, task=task, workspace_id=workspace_id)
    tool_command_job = _seed_tool_command_job(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        command_id="cmd-42",
        task_runtime_job_id=str(task_start_job.id),
        workspace_id=workspace_id,
    )
    manager, session = _open_runner_session(db, tenant=tenant, runner=runner)

    result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_id="tool-result-artifact-pending-1",
            runtime_job_id=str(tool_command_job.id),
            task_id=task.id,
            payload=_tool_result_payload(
                command_id="cmd-42",
                status="succeeded",
                success=True,
                exit_code=0,
                stdout="ok",
                metadata={
                    "task_runtime_job_id": str(task_start_job.id),
                    "workspace_id": workspace_id,
                    "artifact_manifest": {
                        "status": "ready_for_upload_request",
                        "declared_count": 1,
                        "accepted_count": 1,
                    },
                },
            ),
        ),
    )
    db.commit()

    assert result.response_envelopes == ()
    refreshed = db.execute(select(RuntimeJob).where(RuntimeJob.id == tool_command_job.id)).scalar_one()
    assert refreshed.status == "dispatched"
    assert refreshed.result_json["metadata"]["artifact_manifest"]["status"] == "ready_for_upload_request"


def test_tool_result_with_promoted_artifact_upload_terminalizes_runtime_job() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_task(db, tenant=tenant, runner=runner)
    workspace_id = f"task-{task.id}"
    task_start_job = _seed_task_start_job(db, tenant=tenant, runner=runner, task=task, workspace_id=workspace_id)
    tool_command_job = _seed_tool_command_job(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        command_id="cmd-42",
        task_runtime_job_id=str(task_start_job.id),
        workspace_id=workspace_id,
    )
    manager, session = _open_runner_session(db, tenant=tenant, runner=runner)

    result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_id="tool-result-artifact-promoted-1",
            runtime_job_id=str(tool_command_job.id),
            task_id=task.id,
            payload=_tool_result_payload(
                command_id="cmd-42",
                status="succeeded",
                success=True,
                exit_code=0,
                stdout="ok",
                metadata={
                    "task_runtime_job_id": str(task_start_job.id),
                    "workspace_id": workspace_id,
                    "artifact_upload": {"status": "promoted", "completed_count": 1},
                    "artifact_promotion_status": "ready",
                },
            ),
        ),
    )
    db.commit()

    assert result.response_envelopes == ()
    refreshed = db.execute(select(RuntimeJob).where(RuntimeJob.id == tool_command_job.id)).scalar_one()
    assert refreshed.status == "succeeded"
    assert refreshed.result_json["metadata"]["artifact_upload"]["status"] == "promoted"


def test_tool_result_with_unuploadable_declared_artifact_fails_runtime_job() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_task(db, tenant=tenant, runner=runner)
    workspace_id = f"task-{task.id}"
    task_start_job = _seed_task_start_job(db, tenant=tenant, runner=runner, task=task, workspace_id=workspace_id)
    tool_command_job = _seed_tool_command_job(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        command_id="cmd-42",
        task_runtime_job_id=str(task_start_job.id),
        workspace_id=workspace_id,
    )
    manager, session = _open_runner_session(db, tenant=tenant, runner=runner)

    result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_id="tool-result-artifact-failed-1",
            runtime_job_id=str(tool_command_job.id),
            task_id=task.id,
            payload=_tool_result_payload(
                command_id="cmd-42",
                status="succeeded",
                success=True,
                exit_code=0,
                stdout="ok",
                metadata={
                    "task_runtime_job_id": str(task_start_job.id),
                    "workspace_id": workspace_id,
                    "artifact_manifest": {
                        "status": "no_uploadable_artifacts",
                        "declared_count": 1,
                        "accepted_count": 0,
                    },
                },
            ),
        ),
    )
    db.commit()

    assert result.response_envelopes == ()
    refreshed = db.execute(select(RuntimeJob).where(RuntimeJob.id == tool_command_job.id)).scalar_one()
    assert refreshed.status == "failed"
    assert refreshed.error_code == "RUNNER_ARTIFACT_PROMOTION_REQUIRED"


def test_tool_result_promotion_failure_does_not_reject_or_fail_runtime_job() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_task(db, tenant=tenant, runner=runner)
    workspace_id = f"task-{task.id}"
    task_start_job = _seed_task_start_job(db, tenant=tenant, runner=runner, task=task, workspace_id=workspace_id)
    tool_command_job = _seed_tool_command_job(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        command_id="cmd-42",
        task_runtime_job_id=str(task_start_job.id),
        workspace_id=workspace_id,
    )
    manager, session = _open_runner_session(db, tenant=tenant, runner=runner)

    result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_id="tool-result-promotion-failure-1",
            runtime_job_id=str(tool_command_job.id),
            task_id=task.id,
            payload=_tool_result_payload(
                command_id="cmd-42",
                status="succeeded",
                success=True,
                exit_code=0,
                stdout="ok",
                metadata={
                    "task_runtime_job_id": str(task_start_job.id),
                    "workspace_id": workspace_id,
                },
            ),
        ),
    )
    db.commit()

    assert result.response_envelopes == ()
    refreshed = db.execute(select(RuntimeJob).where(RuntimeJob.id == tool_command_job.id)).scalar_one()
    assert refreshed.status == "succeeded"
    assert isinstance(refreshed.result_json, dict)
    promotion = refreshed.result_json.get("artifact_promotion")
    assert isinstance(promotion, dict)
    assert promotion.get("status") == "failed"
    assert promotion.get("error_code") == "RUNNER_RESULT_PROMOTION_FAILED"


def test_tool_result_promotion_failure_masks_error_message_before_persistence(monkeypatch) -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_task(db, tenant=tenant, runner=runner)
    workspace_id = f"task-{task.id}"
    task_start_job = _seed_task_start_job(db, tenant=tenant, runner=runner, task=task, workspace_id=workspace_id)
    tool_command_job = _seed_tool_command_job(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        command_id="cmd-42",
        task_runtime_job_id=str(task_start_job.id),
        workspace_id=workspace_id,
    )
    manager, session = _open_runner_session(db, tenant=tenant, runner=runner)
    raw_secret = "PocSecret-DurableMasking-Sentinel-4a0e12"

    def _raise_promotion_failure(*args, **kwargs):
        raise RuntimeError(f"upload promotion failed with token={raw_secret}")

    monkeypatch.setattr(
        "backend.services.runner_control.runtime_event_service.RunnerResultIngestService.ingest_tool_result",
        _raise_promotion_failure,
    )

    result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_id="tool-result-promotion-failure-redacted-1",
            runtime_job_id=str(tool_command_job.id),
            task_id=task.id,
            payload=_tool_result_payload(
                command_id="cmd-42",
                status="succeeded",
                success=True,
                exit_code=0,
                stdout="ok",
                metadata={
                    "task_runtime_job_id": str(task_start_job.id),
                    "workspace_id": workspace_id,
                },
            ),
        ),
    )
    db.commit()

    assert result.response_envelopes == ()
    refreshed = db.execute(select(RuntimeJob).where(RuntimeJob.id == tool_command_job.id)).scalar_one()
    assert isinstance(refreshed.result_json, dict)
    promotion = refreshed.result_json.get("artifact_promotion")
    assert isinstance(promotion, dict)
    assert raw_secret not in str(promotion)
    assert MASK_PREFIX in promotion["error_message"]


def test_runtime_result_masks_result_map_before_runtime_job_persistence() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_task(db, tenant=tenant, runner=runner)
    runtime_job = _seed_runtime_job(db, tenant=tenant, runner=runner, task=task, job_type="runtime.status")
    manager, session = _open_runner_session(
        db,
        tenant=tenant,
        runner=runner,
        allowed_protocol_versions=("runner_control.v1", "tooling_plane.v1", "remote_runtime.v1"),
    )
    raw_secret = "PocSecret-DurableMasking-Sentinel-8b1c37"

    result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_id="runtime-status-redacted-1",
            runtime_job_id=str(runtime_job.id),
            task_id=task.id,
            message_type="runtime.status",
            schema_version="remote_runtime.v1",
            payload={
                "operation_id": "runtime-status-op-1",
                "status": "succeeded",
                "error_code": None,
                "error_message": None,
                "result": {
                    "message": f"runtime health includes Authorization: Bearer {raw_secret}",
                    "state": "ready",
                },
            },
        ),
    )
    db.commit()

    assert result.response_envelopes == ()
    refreshed = db.execute(select(RuntimeJob).where(RuntimeJob.id == runtime_job.id)).scalar_one()
    assert refreshed.status == "succeeded"
    assert isinstance(refreshed.result_json, dict)
    assert raw_secret not in str(refreshed.result_json)
    assert refreshed.result_json["result"]["message"].startswith("runtime health includes Authorization: Bearer ")
    assert MASK_PREFIX in refreshed.result_json["result"]["message"]


def test_tool_result_masks_sensitive_values_before_persistence() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_task(db, tenant=tenant, runner=runner)
    workspace_id = f"task-{task.id}"
    task_start_job = _seed_task_start_job(db, tenant=tenant, runner=runner, task=task, workspace_id=workspace_id)
    tool_command_job = _seed_tool_command_job(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        command_id="cmd-42",
        task_runtime_job_id=str(task_start_job.id),
        workspace_id=workspace_id,
    )
    manager, session = _open_runner_session(db, tenant=tenant, runner=runner)

    secret_token = "tok_123456789"
    secret_cookie = "session=abcdef"
    result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_id="tool-result-redacted-1",
            runtime_job_id=str(tool_command_job.id),
            task_id=task.id,
            payload=_tool_result_payload(
                command_id="cmd-42",
                status="succeeded",
                success=True,
                exit_code=0,
                stdout=f"Authorization: Bearer {secret_token}\nCookie: {secret_cookie}",
                stderr=f"api_key={secret_token}",
                result={
                    "duration_seconds": 0.1,
                    "session_token": secret_token,
                },
                metadata={
                    "task_runtime_job_id": str(task_start_job.id),
                    "workspace_id": workspace_id,
                    "session_cookie": secret_cookie,
                },
            ),
        ),
    )
    db.commit()

    assert result.response_envelopes == ()
    refreshed_job = db.execute(select(RuntimeJob).where(RuntimeJob.id == tool_command_job.id)).scalar_one()
    assert refreshed_job.status == "succeeded"
    assert isinstance(refreshed_job.result_json, dict)
    assert secret_token not in refreshed_job.result_json["stdout"]
    assert secret_cookie not in refreshed_job.result_json["stdout"]
    assert secret_token not in refreshed_job.result_json["stderr"]
    assert refreshed_job.result_json["result"]["session_token"].startswith(
        MASK_PREFIX
    )
    assert refreshed_job.result_json["metadata"]["session_cookie"].startswith(
        MASK_PREFIX
    )

    inbound_message = db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "inbound",
            RunnerControlMessage.message_id == "tool-result-redacted-1",
        )
    ).scalar_one()
    assert isinstance(inbound_message.payload_json, dict)
    assert secret_token not in inbound_message.payload_json["stdout"]
    assert secret_cookie not in inbound_message.payload_json["stdout"]
    assert secret_token not in inbound_message.payload_json["stderr"]
    assert inbound_message.payload_json["result"]["session_token"].startswith(
        MASK_PREFIX
    )
    assert inbound_message.payload_json["metadata"]["session_cookie"].startswith(
        MASK_PREFIX
    )


def test_tool_result_masks_tshark_secret_exposure_proofs_before_persistence() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_task(db, tenant=tenant, runner=runner)
    workspace_id = f"task-{task.id}"
    task_start_job = _seed_task_start_job(db, tenant=tenant, runner=runner, task=task, workspace_id=workspace_id)
    tool_command_job = _seed_tool_command_job(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        command_id="cmd-42",
        task_runtime_job_id=str(task_start_job.id),
        workspace_id=workspace_id,
    )
    manager, session = _open_runner_session(db, tenant=tenant, runner=runner)
    raw_secret = "PocSecret-DurableMasking-Sentinel-9f4c2a"

    result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_id="tool-result-tshark-proof-1",
            runtime_job_id=str(tool_command_job.id),
            task_id=task.id,
            payload=_tool_result_payload(
                command_id="cmd-42",
                status="succeeded",
                success=True,
                exit_code=0,
                stdout="ok",
                metadata={
                    "task_runtime_job_id": str(task_start_job.id),
                    "workspace_id": workspace_id,
                    "secret_exposure": [
                        {
                            "field": "ftp.request.command_parameter",
                            "kind": "protocol_auth_argument",
                            "proof_mode": "proof_excerpt",
                            "proof_excerpt": raw_secret,
                        }
                    ],
                },
            ),
        ),
    )
    db.commit()

    assert result.response_envelopes == ()
    refreshed_job = db.execute(select(RuntimeJob).where(RuntimeJob.id == tool_command_job.id)).scalar_one()
    assert isinstance(refreshed_job.result_json, dict)
    assert raw_secret not in str(refreshed_job.result_json)
    assert refreshed_job.result_json["metadata"]["secret_exposure"][0]["proof_excerpt"].startswith(
        MASK_PREFIX
    )

    inbound_message = db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "inbound",
            RunnerControlMessage.message_id == "tool-result-tshark-proof-1",
        )
    ).scalar_one()
    assert isinstance(inbound_message.payload_json, dict)
    assert raw_secret not in str(inbound_message.payload_json)
    assert inbound_message.payload_json["metadata"]["secret_exposure"][0]["proof_excerpt"].startswith(
        MASK_PREFIX
    )


def test_tool_result_transitions_runtime_job_to_failed_from_failed_payload() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_task(db, tenant=tenant, runner=runner)
    workspace_id = f"task-{task.id}"
    task_start_job = _seed_task_start_job(db, tenant=tenant, runner=runner, task=task, workspace_id=workspace_id)
    tool_command_job = _seed_tool_command_job(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        command_id="cmd-42",
        task_runtime_job_id=str(task_start_job.id),
        workspace_id=workspace_id,
    )
    manager, session = _open_runner_session(db, tenant=tenant, runner=runner)

    result = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_id="tool-result-failed-1",
            runtime_job_id=str(tool_command_job.id),
            task_id=task.id,
            payload=_tool_result_payload(
                command_id="cmd-42",
                status="failed",
                success=False,
                exit_code=1,
                stderr="boom",
                metadata={
                    "task_runtime_job_id": str(task_start_job.id),
                    "workspace_id": workspace_id,
                },
            ),
        ),
    )
    db.commit()

    assert result.response_envelopes == ()
    refreshed = db.execute(select(RuntimeJob).where(RuntimeJob.id == tool_command_job.id)).scalar_one()
    assert refreshed.status == "failed"
    assert refreshed.error_code == "TOOL_FAILED"
    assert refreshed.result_json["status"] == "failed"
    assert refreshed.result_json["success"] is False


def test_tool_result_rejects_when_envelope_uses_task_start_runtime_job_id() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_task(db, tenant=tenant, runner=runner)
    workspace_id = f"task-{task.id}"
    task_start_job = _seed_task_start_job(db, tenant=tenant, runner=runner, task=task, workspace_id=workspace_id)
    _seed_tool_command_job(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        command_id="cmd-42",
        task_runtime_job_id=str(task_start_job.id),
        workspace_id=workspace_id,
    )
    audit_events: list[dict[str, object]] = []
    manager = RunnerChannelManager(db, audit_emitter=audit_events.append)
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
        json.dumps(
            {
                "message_id": "hello-1",
                "type": "runner.hello",
                "schema_version": "runner_control.v1",
                "tenant_id": str(tenant.id),
                "runner_id": str(runner.id),
                "correlation_id": None,
                "runtime_job_id": None,
                "task_id": None,
                "created_at": "2026-05-24T14:00:00+00:00",
                "payload": {"version": "1.9.0", "capabilities": ["docker", "tool_command.v1"], "labels": {"site": "hq"}},
            }
        ),
    )

    rejected = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_id="tool-result-invalid-1",
            runtime_job_id=str(task_start_job.id),
            task_id=task.id,
            payload=_tool_result_payload(
                command_id="cmd-42",
                status="succeeded",
                success=True,
                exit_code=0,
                stdout="ok",
                metadata={
                    "task_runtime_job_id": str(task_start_job.id),
                    "workspace_id": workspace_id,
                },
            ),
        ),
    )
    db.commit()

    assert len(rejected.response_envelopes) == 1
    error_envelope = rejected.response_envelopes[0]
    assert isinstance(error_envelope.payload, RunnerErrorPayload)
    assert error_envelope.payload.error_code == "RUNTIME_JOB_NOT_ASSIGNED"

    protocol_events = [event for event in audit_events if event.get("event_type") == "runner.protocol_violation"]
    assert protocol_events
    assert protocol_events[-1]["metadata"]["message_type"] == "tool.result"


def test_tool_result_rejects_when_envelope_omits_task_id() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_task(db, tenant=tenant, runner=runner)
    workspace_id = f"task-{task.id}"
    task_start_job = _seed_task_start_job(db, tenant=tenant, runner=runner, task=task, workspace_id=workspace_id)
    tool_command_job = _seed_tool_command_job(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        command_id="cmd-42",
        task_runtime_job_id=str(task_start_job.id),
        workspace_id=workspace_id,
    )
    audit_events: list[dict[str, object]] = []
    manager = RunnerChannelManager(db, audit_emitter=audit_events.append)
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
        json.dumps(
            {
                "message_id": "hello-1",
                "type": "runner.hello",
                "schema_version": "runner_control.v1",
                "tenant_id": str(tenant.id),
                "runner_id": str(runner.id),
                "correlation_id": None,
                "runtime_job_id": None,
                "task_id": None,
                "created_at": "2026-05-24T14:00:00+00:00",
                "payload": {"version": "1.9.0", "capabilities": ["docker", "tool_command.v1"], "labels": {"site": "hq"}},
            }
        ),
    )

    rejected = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_id="tool-result-missing-task-id",
            runtime_job_id=str(tool_command_job.id),
            task_id=None,
            payload=_tool_result_payload(
                command_id="cmd-42",
                status="succeeded",
                success=True,
                exit_code=0,
                stdout="ok",
                metadata={
                    "task_runtime_job_id": str(task_start_job.id),
                    "workspace_id": workspace_id,
                },
            ),
        ),
    )
    db.commit()

    assert len(rejected.response_envelopes) == 1
    error_envelope = rejected.response_envelopes[0]
    assert isinstance(error_envelope.payload, RunnerErrorPayload)
    assert error_envelope.payload.error_code == "RUNNER_TOOL_TASK_MISMATCH"

    refreshed = db.execute(select(RuntimeJob).where(RuntimeJob.id == tool_command_job.id)).scalar_one()
    assert refreshed.status == "dispatched"
    assert refreshed.result_json is None

    protocol_events = [event for event in audit_events if event.get("event_type") == "runner.protocol_violation"]
    assert protocol_events
    assert protocol_events[-1]["metadata"]["message_type"] == "tool.result"
    assert protocol_events[-1]["metadata"]["error_code"] == "RUNNER_TOOL_TASK_MISMATCH"


def test_tool_result_rejects_when_metadata_mapping_is_oversized() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_task(db, tenant=tenant, runner=runner)
    workspace_id = f"task-{task.id}"
    task_start_job = _seed_task_start_job(db, tenant=tenant, runner=runner, task=task, workspace_id=workspace_id)
    tool_command_job = _seed_tool_command_job(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        command_id="cmd-42",
        task_runtime_job_id=str(task_start_job.id),
        workspace_id=workspace_id,
    )
    manager, session = _open_runner_session(db, tenant=tenant, runner=runner)

    rejected = manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_id="tool-result-oversized-metadata",
            runtime_job_id=str(tool_command_job.id),
            task_id=task.id,
            payload=_tool_result_payload(
                command_id="cmd-42",
                status="succeeded",
                success=True,
                exit_code=0,
                stdout="ok",
                metadata={
                    "task_runtime_job_id": str(task_start_job.id),
                    "workspace_id": workspace_id,
                    "blob": "x" * (70 * 1024),
                },
            ),
        ),
    )
    db.commit()

    assert len(rejected.response_envelopes) == 1
    error_envelope = rejected.response_envelopes[0]
    assert isinstance(error_envelope.payload, RunnerErrorPayload)
    assert error_envelope.payload.error_code == "RUNNER_TOOL_RESULT_PAYLOAD_TOO_LARGE"

    refreshed = db.execute(select(RuntimeJob).where(RuntimeJob.id == tool_command_job.id)).scalar_one()
    assert refreshed.status == "dispatched"
    assert refreshed.result_json is None


def test_tool_result_duplicate_is_deduped_by_transition_idempotency_key() -> None:
    db = _build_session()
    tenant, runner = _seed_runner(db)
    task = _seed_task(db, tenant=tenant, runner=runner)
    workspace_id = f"task-{task.id}"
    task_start_job = _seed_task_start_job(db, tenant=tenant, runner=runner, task=task, workspace_id=workspace_id)
    tool_command_job = _seed_tool_command_job(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        command_id="cmd-42",
        task_runtime_job_id=str(task_start_job.id),
        workspace_id=workspace_id,
    )
    manager, session = _open_runner_session(db, tenant=tenant, runner=runner)

    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_id="tool-result-dup-1",
            runtime_job_id=str(tool_command_job.id),
            task_id=task.id,
            payload=_tool_result_payload(
                command_id="cmd-42",
                status="succeeded",
                success=True,
                exit_code=0,
                stdout="first",
                metadata={
                    "task_runtime_job_id": str(task_start_job.id),
                    "workspace_id": workspace_id,
                },
            ),
        ),
    )
    manager.handle_inbound_json(
        session,
        _envelope_json(
            tenant_id=tenant.id,
            runner_id=runner.id,
            message_id="tool-result-dup-2",
            runtime_job_id=str(tool_command_job.id),
            task_id=task.id,
            payload=_tool_result_payload(
                command_id="cmd-42",
                status="succeeded",
                success=True,
                exit_code=0,
                stdout="second",
                metadata={
                    "task_runtime_job_id": str(task_start_job.id),
                    "workspace_id": workspace_id,
                },
            ),
        ),
    )
    db.commit()

    refreshed = db.execute(select(RuntimeJob).where(RuntimeJob.id == tool_command_job.id)).scalar_one()
    assert refreshed.status == "succeeded"
    assert refreshed.result_json["stdout"] == "first"

    inbound_rows = db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == tenant.id,
            RunnerControlMessage.runner_id == runner.id,
            RunnerControlMessage.direction == "inbound",
            RunnerControlMessage.type == "tool.result",
        )
    ).scalars().all()
    assert len(inbound_rows) == 1
