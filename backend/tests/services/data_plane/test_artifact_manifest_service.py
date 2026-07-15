"""Tests for Data Plane runner artifact manifest ingest and upload-request creation.

Scope:
- Validates backend ingest behavior for `artifact.manifest` messages.
- Confirms skeletal execution creation, placeholder artifact persistence,
  tenant-bound manifest identity, and signed upload instruction responses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.config.data_plane import DataPlaneConfig
from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.provenance import ArtifactManifest, ExecutionArtifact, ToolExecution
from backend.models.runner_control import ExecutionSite, Runner, RuntimeJob
from backend.models.tenant import Tenant
from backend.services.data_plane.artifact_manifest_service import ArtifactManifestService
from backend.services.data_plane.local_object_store import LocalObjectStore
from backend.services.runner_control.runtime_event_service import RuntimeEventService
from runtime_shared.runner_protocol import (
    RUNNER_PROTOCOL_DATA_PLANE_VERSION,
    RunnerArtifactManifestItem,
    RunnerArtifactManifestPayload,
    RunnerEnvelope,
    RunnerMessageType,
    RunnerToolResultPayload,
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
            ToolExecution.__table__,
            ArtifactManifest.__table__,
            ExecutionArtifact.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return factory()


def _seed_runner_task_context(db: Session) -> tuple[Tenant, Runner, Task]:
    unique_suffix = uuid.uuid4().hex
    tenant = Tenant(slug=f"tenant-{unique_suffix}", name="Tenant")
    db.add(tenant)
    db.flush()

    user = User(
        username=f"user-{unique_suffix}",
        password="test-password",
        email=f"{unique_suffix}@example.com",
    )
    db.add(user)
    db.flush()

    site = ExecutionSite(
        tenant_id=tenant.id,
        name="Primary Site",
        slug=f"primary-{unique_suffix}",
        status="active",
    )
    db.add(site)
    db.flush()

    runner = Runner(
        tenant_id=tenant.id,
        execution_site_id=site.id,
        name=f"runner-{unique_suffix}",
        status="active",
    )
    db.add(runner)
    db.flush()

    task = Task(
        user_id=user.id,
        tenant_id=tenant.id,
        name=f"Runner Task {unique_suffix}",
        runtime_placement_mode="runner",
        runner_id=str(runner.id).lower(),
    )
    db.add(task)
    db.commit()

    return tenant, runner, task


def _seed_bound_runtime_jobs(
    db: Session,
    *,
    tenant: Tenant,
    runner: Runner,
    task: Task,
    command_id: str,
    workspace_id: str,
    tool_call_id: str = "tool-call-1",
) -> tuple[RuntimeJob, RuntimeJob]:
    task_runtime_job = RuntimeJob(
        tenant_id=tenant.id,
        runner_id=runner.id,
        execution_site_id=runner.execution_site_id,
        task_id=task.id,
        job_type="task.start",
        status="running",
        idempotency_key=f"task-start-{uuid.uuid4()}",
        payload_json={"workspace_id": workspace_id},
    )
    db.add(task_runtime_job)
    db.flush()

    tool_runtime_job = RuntimeJob(
        tenant_id=tenant.id,
        runner_id=runner.id,
        execution_site_id=runner.execution_site_id,
        task_id=task.id,
        job_type="tool.command",
        status="dispatched",
        idempotency_key=f"tool-command-{uuid.uuid4()}",
        payload_json={
            "workspace_id": workspace_id,
            "command_id": command_id,
            "task_runtime_job_id": str(task_runtime_job.id),
            "tool": "shell.exec",
            "args": {"command": "cat report.txt"},
            "tool_call_id": tool_call_id,
            "agent_path": "runner.tool_command",
        },
    )
    db.add(tool_runtime_job)
    db.commit()

    return task_runtime_job, tool_runtime_job


def _build_service(*, db: Session, root: Path, max_artifact_size_bytes: int = 1024) -> ArtifactManifestService:
    config = DataPlaneConfig(
        object_store_backend="local",
        local_object_store_root=root,
        object_store_bucket=None,
        object_store_prefix="data-plane-prefix",
        signed_upload_ttl_seconds=900,
        signed_download_ttl_seconds=900,
        max_artifact_size_bytes=max_artifact_size_bytes,
        max_manifest_items=256,
        max_zip_download_size_bytes=64 * 1024 * 1024,
    )
    object_store = LocalObjectStore(root_path=root)
    return ArtifactManifestService(
        db,
        object_store=object_store,
        data_plane_config=config,
    )


def _manifest_envelope(
    *,
    tenant: Tenant,
    runner: Runner,
    task: Task,
    tool_runtime_job: RuntimeJob,
    task_runtime_job: RuntimeJob,
    command_id: str,
    workspace_id: str,
    message_id: str,
    artifacts: tuple[RunnerArtifactManifestItem, ...],
) -> RunnerEnvelope:
    return RunnerEnvelope(
        message_id=message_id,
        message_type=RunnerMessageType.ARTIFACT_MANIFEST,
        schema_version=RUNNER_PROTOCOL_DATA_PLANE_VERSION,
        tenant_id=str(tenant.id),
        runner_id=str(runner.id),
        correlation_id="corr-manifest-1",
        runtime_job_id=str(tool_runtime_job.id),
        task_id=task.id,
        created_at=datetime.now(tz=UTC).isoformat(),
        payload=RunnerArtifactManifestPayload(
            task_runtime_job_id=str(task_runtime_job.id),
            command_id=command_id,
            workspace_id=workspace_id,
            tool_call_id="tool-call-1",
            tool_batch_id="tool-batch-1",
            artifacts=artifacts,
        ),
        raw_message_type=RunnerMessageType.ARTIFACT_MANIFEST.value,
    )


def test_handle_artifact_manifest_creates_skeletal_execution_manifest_rows_and_upload_instructions(tmp_path: Path) -> None:
    db = _build_session()
    tenant, runner, task = _seed_runner_task_context(db)
    workspace_id = f"task-{task.id}"
    command_id = "cmd-42"
    task_runtime_job, tool_runtime_job = _seed_bound_runtime_jobs(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        command_id=command_id,
        workspace_id=workspace_id,
    )

    service = _build_service(db=db, root=tmp_path / "objects")
    envelope = _manifest_envelope(
        tenant=tenant,
        runner=runner,
        task=task,
        tool_runtime_job=tool_runtime_job,
        task_runtime_job=task_runtime_job,
        command_id=command_id,
        workspace_id=workspace_id,
        message_id="msg-manifest-1",
        artifacts=(
            RunnerArtifactManifestItem(
                artifact_client_id="artifact-client-1",
                relative_path="artifacts/cmd-42/stdout.txt",
                artifact_kind="stdout",
                size_bytes=128,
                content_sha256="a" * 64,
                content_type="text/plain",
                is_text=True,
                created_at="2026-05-25T12:00:00+00:00",
                metadata={"source": "tool.result"},
            ),
        ),
    )

    result = service.handle_inbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        envelope=envelope,
    )

    assert len(result.response_envelopes) == 1
    upload_request = result.response_envelopes[0]
    assert upload_request.message_type is RunnerMessageType.ARTIFACT_UPLOAD_REQUEST
    assert upload_request.task_id == task.id
    payload = upload_request.payload
    assert len(payload.uploads) == 1

    upload_item = payload.uploads[0]
    parsed_artifact_id = uuid.UUID(upload_item.artifact_id)
    assert upload_item.object_key.startswith(
        f"data-plane-prefix/tenants/{tenant.id}/tasks/{task.id}/executions/"
    )
    assert f"artifacts/{parsed_artifact_id}/stdout.txt" in upload_item.object_key
    assert upload_item.object_key != "artifacts/cmd-42/stdout.txt"
    assert upload_item.upload_url.startswith("local-object://upload/")

    execution_rows = db.execute(select(ToolExecution)).scalars().all()
    assert len(execution_rows) == 1
    execution = execution_rows[0]
    assert execution.tenant_id == tenant.id
    assert execution.task_id == task.id
    assert execution.runtime_job_id == tool_runtime_job.id
    assert execution.runner_id == runner.id
    assert execution.command_id == command_id
    assert execution.workspace_id == workspace_id
    assert execution.status == "pending"
    assert execution.execution_metadata["runner_manifest"]["skeletal"] is True

    manifest_rows = db.execute(select(ArtifactManifest)).scalars().all()
    assert len(manifest_rows) == 1
    manifest = manifest_rows[0]
    assert manifest.tenant_id == tenant.id
    assert manifest.task_id == task.id
    assert manifest.runner_id == runner.id
    assert manifest.runtime_job_id == tool_runtime_job.id
    assert manifest.command_id == command_id
    assert manifest.workspace_id == workspace_id

    artifact_rows = db.execute(select(ExecutionArtifact)).scalars().all()
    assert len(artifact_rows) == 1
    artifact = artifact_rows[0]
    assert artifact.execution_id == execution.id
    assert artifact.manifest_id == manifest.id
    assert artifact.upload_status == "upload_pending"
    assert artifact.content_sha256 == "a" * 64
    assert artifact.byte_size == 128
    assert artifact.object_key == upload_item.object_key
    artifact_metadata = artifact.artifact_metadata or {}
    manifest_metadata = manifest.manifest_metadata or {}
    assert "upload_url" not in artifact_metadata
    assert "upload_headers" not in artifact_metadata
    assert "upload_url" not in manifest_metadata
    assert "upload_headers" not in manifest_metadata


def test_handle_artifact_manifest_is_idempotent_and_keeps_artifact_id_stable(tmp_path: Path) -> None:
    db = _build_session()
    tenant, runner, task = _seed_runner_task_context(db)
    workspace_id = f"task-{task.id}"
    command_id = "cmd-43"
    task_runtime_job, tool_runtime_job = _seed_bound_runtime_jobs(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        command_id=command_id,
        workspace_id=workspace_id,
    )

    service = _build_service(db=db, root=tmp_path / "objects")
    envelope = _manifest_envelope(
        tenant=tenant,
        runner=runner,
        task=task,
        tool_runtime_job=tool_runtime_job,
        task_runtime_job=task_runtime_job,
        command_id=command_id,
        workspace_id=workspace_id,
        message_id="msg-manifest-stable-id",
        artifacts=(
            RunnerArtifactManifestItem(
                artifact_client_id="artifact-client-1",
                relative_path="artifacts/cmd-43/stdout.txt",
                artifact_kind="stdout",
                size_bytes=256,
                content_sha256="b" * 64,
                content_type="text/plain",
                is_text=True,
                created_at="2026-05-25T12:00:00+00:00",
                metadata={},
            ),
        ),
    )

    first = service.handle_inbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        envelope=envelope,
    )
    second = service.handle_inbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        envelope=envelope,
    )

    first_artifact_id = first.response_envelopes[0].payload.uploads[0].artifact_id
    second_artifact_id = second.response_envelopes[0].payload.uploads[0].artifact_id
    assert first_artifact_id == second_artifact_id

    assert db.execute(select(ToolExecution)).scalars().all()
    assert len(db.execute(select(ToolExecution)).scalars().all()) == 1
    assert len(db.execute(select(ArtifactManifest)).scalars().all()) == 1
    assert len(db.execute(select(ExecutionArtifact)).scalars().all()) == 1


def test_handle_artifact_manifest_records_per_item_rejection_and_keeps_valid_sibling(tmp_path: Path) -> None:
    db = _build_session()
    tenant, runner, task = _seed_runner_task_context(db)
    workspace_id = f"task-{task.id}"
    command_id = "cmd-44"
    task_runtime_job, tool_runtime_job = _seed_bound_runtime_jobs(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        command_id=command_id,
        workspace_id=workspace_id,
    )

    service = _build_service(db=db, root=tmp_path / "objects", max_artifact_size_bytes=256)
    envelope = _manifest_envelope(
        tenant=tenant,
        runner=runner,
        task=task,
        tool_runtime_job=tool_runtime_job,
        task_runtime_job=task_runtime_job,
        command_id=command_id,
        workspace_id=workspace_id,
        message_id="msg-manifest-partial-reject",
        artifacts=(
            RunnerArtifactManifestItem(
                artifact_client_id="artifact-client-ok",
                relative_path="artifacts/cmd-44/stdout.txt",
                artifact_kind="stdout",
                size_bytes=128,
                content_sha256="c" * 64,
                content_type="text/plain",
                is_text=True,
                created_at=None,
                metadata={},
            ),
            RunnerArtifactManifestItem(
                artifact_client_id="artifact-client-big",
                relative_path="artifacts/cmd-44/big.bin",
                artifact_kind="result",
                size_bytes=1024,
                content_sha256="d" * 64,
                content_type="application/octet-stream",
                is_text=False,
                created_at=None,
                metadata={},
            ),
        ),
    )

    result = service.handle_inbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        envelope=envelope,
    )

    assert len(result.response_envelopes) == 1
    upload_payload = result.response_envelopes[0].payload
    assert len(upload_payload.uploads) == 1
    assert upload_payload.uploads[0].artifact_client_id == "artifact-client-ok"

    manifest = db.execute(select(ArtifactManifest)).scalar_one()
    metadata = manifest.manifest_metadata or {}
    assert metadata.get("accepted_item_count") == 1
    assert metadata.get("rejected_item_count") == 1
    rejected_items = metadata.get("rejected_items")
    assert isinstance(rejected_items, list)
    assert rejected_items[0]["artifact_client_id"] == "artifact-client-big"
    assert rejected_items[0]["error_code"] == "RUNNER_ARTIFACT_ITEM_TOO_LARGE"

    artifacts = db.execute(select(ExecutionArtifact)).scalars().all()
    assert len(artifacts) == 1
    assert artifacts[0].upload_status == "upload_pending"


def test_tool_result_updates_manifest_skeletal_execution_in_place(tmp_path: Path) -> None:
    db = _build_session()
    tenant, runner, task = _seed_runner_task_context(db)
    workspace_id = f"task-{task.id}"
    command_id = "cmd-45"
    task_runtime_job, tool_runtime_job = _seed_bound_runtime_jobs(
        db,
        tenant=tenant,
        runner=runner,
        task=task,
        command_id=command_id,
        workspace_id=workspace_id,
    )
    manifest_service = _build_service(db=db, root=tmp_path / "objects")

    manifest_envelope = _manifest_envelope(
        tenant=tenant,
        runner=runner,
        task=task,
        tool_runtime_job=tool_runtime_job,
        task_runtime_job=task_runtime_job,
        command_id=command_id,
        workspace_id=workspace_id,
        message_id="msg-manifest-skeletal-update",
        artifacts=(
            RunnerArtifactManifestItem(
                artifact_client_id="artifact-client-1",
                relative_path="artifacts/cmd-45/stdout.txt",
                artifact_kind="stdout",
                size_bytes=128,
                content_sha256="e" * 64,
                content_type="text/plain",
                is_text=True,
                created_at="2026-05-25T12:00:00+00:00",
                metadata={},
            ),
        ),
    )
    manifest_service.handle_inbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        envelope=manifest_envelope,
    )
    skeletal = db.execute(select(ToolExecution)).scalar_one()
    manifest_artifact = db.execute(select(ExecutionArtifact)).scalar_one()
    audit_events: list[dict[str, object]] = []

    RuntimeEventService(db, audit_emitter=audit_events.append).apply_runtime_event(
        tenant_id=tenant.id,
        runner_id=runner.id,
        envelope=RunnerEnvelope(
            message_id="msg-tool-result-skeletal-update",
            message_type=RunnerMessageType.TOOL_RESULT,
            schema_version="tooling_plane.v1",
            tenant_id=str(tenant.id),
            runner_id=str(runner.id),
            correlation_id="corr-tool-result-skeletal",
            runtime_job_id=str(tool_runtime_job.id),
            task_id=task.id,
            created_at=datetime.now(tz=UTC).isoformat(),
            payload=RunnerToolResultPayload(
                operation_id="tool-op-45",
                command_id=command_id,
                tool="shell.exec",
                status="succeeded",
                success=True,
                exit_code=0,
                stdout="ok",
                stderr="",
                artifacts=("artifacts/cmd-45/stdout.txt",),
                error_code=None,
                error_message=None,
                result={"semantic_schema_version": "data_plane.v1"},
                metadata={
                    "workspace_id": workspace_id,
                    "semantic_observations": [{"type": "network.host", "value": "10.0.0.1"}],
                    "semantic_evidence": [{"artifact_id": "artifact-client-1"}],
                },
            ),
            raw_message_type=RunnerMessageType.TOOL_RESULT.value,
        ),
    )

    executions = db.execute(select(ToolExecution)).scalars().all()
    assert len(executions) == 1
    updated = executions[0]
    assert updated.id == skeletal.id
    assert updated.status == "succeeded"
    assert updated.exit_code == 0
    assert updated.execution_metadata["runner_manifest"]["skeletal"] is True
    assert updated.execution_metadata["semantic_snapshot"]["semantic_schema_version"] == "data_plane.v1"
    assert updated.execution_metadata["semantic_snapshot"]["semantic_observations"]
    applied_events = [event for event in audit_events if event.get("event_type") == "runner.runtime_event.applied"]
    assert applied_events
    promoted_artifact_ids = applied_events[-1]["metadata"].get("promoted_artifact_ids")
    assert isinstance(promoted_artifact_ids, list)
    assert str(manifest_artifact.id) in promoted_artifact_ids
