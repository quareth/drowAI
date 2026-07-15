"""Tests for Data Plane artifact upload completion verification and readiness transitions.

Scope:
- Validates `artifact.upload.complete` identity, object verification, and
  artifact/manifest status transitions.
- Confirms browser-facing stream events include only safe artifact status
  metadata without signed URL/header fields.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.provenance import ArtifactManifest, ExecutionArtifact, ToolExecution
from backend.models.runner_control import ExecutionSite, Runner, RuntimeJob
from backend.models.tenant import Tenant
from backend.services.data_plane.artifact_upload_service import (
    ArtifactUploadService,
    ArtifactUploadServiceError,
)
from backend.services.data_plane.object_store import ObjectHead
from runtime_shared.runner_protocol import (
    RUNNER_PROTOCOL_DATA_PLANE_VERSION,
    RunnerArtifactUploadCompleteItem,
    RunnerArtifactUploadCompletePayload,
    RunnerEnvelope,
    RunnerMessageType,
)


class _StubHeadObjectStore:
    def __init__(self, *, head_result: ObjectHead | None) -> None:
        self._head_result = head_result

    def put_bytes(self, object_key: str, data: bytes, *, content_type: str | None = None, metadata=None):
        raise NotImplementedError

    def read_bytes(self, object_key: str, *, max_bytes: int | None = None) -> bytes:
        raise NotImplementedError

    def delete_object(self, object_key: str) -> bool:
        raise NotImplementedError

    def create_signed_upload(self, object_key: str, *, content_type: str | None = None, metadata=None):
        raise NotImplementedError

    def create_signed_download(self, object_key: str, *, response_filename: str | None = None):
        raise NotImplementedError

    def head_object(self, object_key: str) -> ObjectHead | None:
        return self._head_result


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


def _seed_upload_context(db: Session) -> tuple[Tenant, Runner, Task, RuntimeJob, RuntimeJob, ToolExecution, ArtifactManifest]:
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
    db.flush()

    workspace_id = f"task-{task.id}"
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
            "command_id": "cmd-42",
            "task_runtime_job_id": str(task_runtime_job.id),
        },
    )
    db.add(tool_runtime_job)
    db.flush()

    execution = ToolExecution(
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        runner_id=runner.id,
        execution_site_id=runner.execution_site_id,
        command_id="cmd-42",
        workspace_id=workspace_id,
        tool_name="shell.exec",
        tool_arguments={"command": "cat out.txt"},
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
        message_id="msg-manifest-1",
        idempotency_key="tenant:runner:msg-manifest-1",
        status="accepted",
    )
    db.add(manifest)
    db.commit()
    return tenant, runner, task, task_runtime_job, tool_runtime_job, execution, manifest


def _upload_envelope(
    *,
    tenant: Tenant,
    runner: Runner,
    task: Task,
    tool_runtime_job: RuntimeJob,
    task_runtime_job: RuntimeJob,
    artifact: ExecutionArtifact,
    object_key: str,
    content_sha256: str,
    size_bytes: int,
    command_id: str = "cmd-42",
    workspace_id: str | None = None,
) -> RunnerEnvelope:
    resolved_workspace_id = workspace_id if workspace_id is not None else f"task-{task.id}"
    return RunnerEnvelope(
        message_id=f"msg-upload-{uuid.uuid4().hex}",
        message_type=RunnerMessageType.ARTIFACT_UPLOAD_COMPLETE,
        schema_version=RUNNER_PROTOCOL_DATA_PLANE_VERSION,
        tenant_id=str(tenant.id),
        runner_id=str(runner.id),
        correlation_id="corr-upload-1",
        runtime_job_id=str(tool_runtime_job.id),
        task_id=task.id,
        created_at="2026-05-25T12:00:00+00:00",
        payload=RunnerArtifactUploadCompletePayload(
            task_runtime_job_id=str(task_runtime_job.id),
            command_id=command_id,
            workspace_id=resolved_workspace_id,
            tool_call_id="tool-call-1",
            tool_batch_id="tool-batch-1",
            uploads=(
                RunnerArtifactUploadCompleteItem(
                    artifact_id=str(artifact.id),
                    artifact_client_id="artifact-client-1",
                    object_key=object_key,
                    size_bytes=size_bytes,
                    content_sha256=content_sha256,
                    uploaded_at="2026-05-25T12:05:00+00:00",
                ),
            ),
        ),
        raw_message_type=RunnerMessageType.ARTIFACT_UPLOAD_COMPLETE.value,
    )


def test_upload_complete_marks_ready_and_manifest_ready(tmp_path: Path) -> None:
    db = _build_session()
    tenant, runner, task, task_runtime_job, tool_runtime_job, execution, manifest = _seed_upload_context(db)

    content = b"hello-world"
    from backend.services.data_plane.local_object_store import LocalObjectStore

    object_store = LocalObjectStore(root_path=tmp_path / "objects")
    object_key = f"tenants/{tenant.id}/tasks/{task.id}/executions/{execution.id}/artifacts/one/stdout.txt"
    object_store.put_bytes(object_key, content, content_type="text/plain")

    artifact = ExecutionArtifact(
        execution_id=execution.id,
        manifest_id=manifest.id,
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        runner_id=runner.id,
        command_id="cmd-42",
        artifact_kind="stdout",
        relative_path="artifacts/stdout.txt",
        object_key=object_key,
        upload_status="upload_pending",
        content_sha256="afa27b44d43b02a9fea41d13b4b4d04210d1009b0c36d6e41009f8fc54a0f4f5",
        byte_size=len(content),
        mime_type="text/plain",
        artifact_metadata={"artifact_client_id": "artifact-client-1"},
    )
    db.add(artifact)
    db.commit()

    service = ArtifactUploadService(db, object_store=object_store)
    envelope = _upload_envelope(
        tenant=tenant,
        runner=runner,
        task=task,
        tool_runtime_job=tool_runtime_job,
        task_runtime_job=task_runtime_job,
        artifact=artifact,
        object_key=object_key,
        content_sha256=artifact.content_sha256,
        size_bytes=len(content),
    )

    result = service.handle_inbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        envelope=envelope,
    )

    assert result.response_envelopes == ()
    db.refresh(artifact)
    db.refresh(manifest)
    assert artifact.upload_status == "ready"
    assert artifact.uploaded_at is not None
    assert manifest.status == "ready"


def test_upload_complete_fails_artifact_on_size_mismatch(tmp_path: Path) -> None:
    db = _build_session()
    tenant, runner, task, task_runtime_job, tool_runtime_job, execution, manifest = _seed_upload_context(db)

    from backend.services.data_plane.local_object_store import LocalObjectStore

    content = b"abc"
    object_key = f"tenants/{tenant.id}/tasks/{task.id}/executions/{execution.id}/artifacts/one/stdout.txt"
    object_store = LocalObjectStore(root_path=tmp_path / "objects")
    object_store.put_bytes(object_key, content, content_type="text/plain")

    artifact = ExecutionArtifact(
        execution_id=execution.id,
        manifest_id=manifest.id,
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        runner_id=runner.id,
        command_id="cmd-42",
        artifact_kind="stdout",
        relative_path="artifacts/stdout.txt",
        object_key=object_key,
        upload_status="upload_pending",
        content_sha256="ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        byte_size=99,
        mime_type="text/plain",
        artifact_metadata={"artifact_client_id": "artifact-client-1"},
    )
    db.add(artifact)
    db.commit()

    service = ArtifactUploadService(db, object_store=object_store)
    envelope = _upload_envelope(
        tenant=tenant,
        runner=runner,
        task=task,
        tool_runtime_job=tool_runtime_job,
        task_runtime_job=task_runtime_job,
        artifact=artifact,
        object_key=object_key,
        content_sha256=artifact.content_sha256,
        size_bytes=99,
    )
    service.handle_inbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        envelope=envelope,
    )

    db.refresh(artifact)
    db.refresh(manifest)
    assert artifact.upload_status == "upload_failed"
    metadata = artifact.artifact_metadata or {}
    upload_error = metadata.get("upload_error") if isinstance(metadata, dict) else None
    assert isinstance(upload_error, dict)
    assert upload_error.get("error_code") == "RUNNER_ARTIFACT_UPLOAD_SIZE_MISMATCH"
    assert manifest.status == "failed"


def test_upload_complete_fails_artifact_on_hash_mismatch() -> None:
    db = _build_session()
    tenant, runner, task, task_runtime_job, tool_runtime_job, execution, manifest = _seed_upload_context(db)

    object_key = f"tenants/{tenant.id}/tasks/{task.id}/executions/{execution.id}/artifacts/one/stdout.txt"
    artifact = ExecutionArtifact(
        execution_id=execution.id,
        manifest_id=manifest.id,
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        runner_id=runner.id,
        command_id="cmd-42",
        artifact_kind="stdout",
        relative_path="artifacts/stdout.txt",
        object_key=object_key,
        upload_status="upload_pending",
        content_sha256="a" * 64,
        byte_size=12,
        mime_type="text/plain",
        artifact_metadata={"artifact_client_id": "artifact-client-1"},
    )
    db.add(artifact)
    db.commit()

    object_store = _StubHeadObjectStore(
        head_result=ObjectHead(
            object_key=object_key,
            byte_size=12,
            content_type="text/plain",
            content_sha256="b" * 64,
        )
    )
    service = ArtifactUploadService(db, object_store=object_store)
    envelope = _upload_envelope(
        tenant=tenant,
        runner=runner,
        task=task,
        tool_runtime_job=tool_runtime_job,
        task_runtime_job=task_runtime_job,
        artifact=artifact,
        object_key=object_key,
        content_sha256="a" * 64,
        size_bytes=12,
    )
    service.handle_inbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        envelope=envelope,
    )

    db.refresh(artifact)
    db.refresh(manifest)
    assert artifact.upload_status == "upload_failed"
    metadata = artifact.artifact_metadata or {}
    upload_error = metadata.get("upload_error") if isinstance(metadata, dict) else None
    assert isinstance(upload_error, dict)
    assert upload_error.get("error_code") == "RUNNER_ARTIFACT_UPLOAD_HASH_MISMATCH"
    assert manifest.status == "failed"


def test_upload_complete_fails_closed_when_object_missing() -> None:
    db = _build_session()
    tenant, runner, task, task_runtime_job, tool_runtime_job, execution, manifest = _seed_upload_context(db)

    object_key = f"tenants/{tenant.id}/tasks/{task.id}/executions/{execution.id}/artifacts/one/stdout.txt"
    artifact = ExecutionArtifact(
        execution_id=execution.id,
        manifest_id=manifest.id,
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        runner_id=runner.id,
        command_id="cmd-42",
        artifact_kind="stdout",
        relative_path="artifacts/stdout.txt",
        object_key=object_key,
        upload_status="upload_pending",
        content_sha256="a" * 64,
        byte_size=10,
        mime_type="text/plain",
        artifact_metadata={"artifact_client_id": "artifact-client-1"},
    )
    db.add(artifact)
    db.commit()

    service = ArtifactUploadService(db, object_store=_StubHeadObjectStore(head_result=None))
    envelope = _upload_envelope(
        tenant=tenant,
        runner=runner,
        task=task,
        tool_runtime_job=tool_runtime_job,
        task_runtime_job=task_runtime_job,
        artifact=artifact,
        object_key=object_key,
        content_sha256="a" * 64,
        size_bytes=10,
    )
    service.handle_inbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        envelope=envelope,
    )

    db.refresh(artifact)
    db.refresh(manifest)
    assert artifact.upload_status == "upload_failed"
    metadata = artifact.artifact_metadata or {}
    upload_error = metadata.get("upload_error") if isinstance(metadata, dict) else None
    assert isinstance(upload_error, dict)
    assert upload_error.get("error_code") == "RUNNER_ARTIFACT_OBJECT_MISSING"
    assert manifest.status == "failed"


@pytest.mark.parametrize(
    ("context_field", "context_value_factory"),
    (
        ("tenant_id", lambda tenant, _runner, _task, tool_runtime_job: tenant.id + 999),
        ("task_id", lambda _tenant, _runner, task, _tool_runtime_job: task.id + 999),
        ("runtime_job_id", lambda _tenant, _runner, _task, _tool_runtime_job: uuid.uuid4()),
    ),
)
def test_upload_complete_rejects_cross_scope_context_bindings(
    context_field: str,
    context_value_factory,
) -> None:
    db = _build_session()
    tenant, runner, task, task_runtime_job, tool_runtime_job, execution, manifest = _seed_upload_context(db)

    object_key = f"tenants/{tenant.id}/tasks/{task.id}/executions/{execution.id}/artifacts/one/stdout.txt"
    artifact = ExecutionArtifact(
        execution_id=execution.id,
        manifest_id=manifest.id,
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        runner_id=runner.id,
        command_id="cmd-42",
        artifact_kind="stdout",
        relative_path="artifacts/stdout.txt",
        object_key=object_key,
        upload_status="upload_pending",
        content_sha256="a" * 64,
        byte_size=12,
        mime_type="text/plain",
        artifact_metadata={"artifact_client_id": "artifact-client-1"},
    )
    db.add(artifact)
    db.commit()

    service = ArtifactUploadService(
        db,
        object_store=_StubHeadObjectStore(
            head_result=ObjectHead(
                object_key=object_key,
                byte_size=12,
                content_type="text/plain",
                content_sha256="a" * 64,
            )
        ),
    )
    envelope = _upload_envelope(
        tenant=tenant,
        runner=runner,
        task=task,
        tool_runtime_job=tool_runtime_job,
        task_runtime_job=task_runtime_job,
        artifact=artifact,
        object_key=object_key,
        content_sha256="a" * 64,
        size_bytes=12,
    )
    context = {
        "tenant_id": tenant.id,
        "runner_id": runner.id,
        "task_id": task.id,
        "runtime_job_id": tool_runtime_job.id,
    }
    context[context_field] = context_value_factory(tenant, runner, task, tool_runtime_job)

    with pytest.raises(ArtifactUploadServiceError) as error:
        service.handle_inbound_message(
            tenant_id=context["tenant_id"],
            runner_id=context["runner_id"],
            task_id=context["task_id"],
            runtime_job_id=context["runtime_job_id"],
            envelope=envelope,
        )

    assert error.value.error_code == "RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED"


def test_upload_complete_rejects_command_workspace_and_object_key_mismatches() -> None:
    db = _build_session()
    tenant, runner, task, task_runtime_job, tool_runtime_job, execution, manifest = _seed_upload_context(db)

    object_key = f"tenants/{tenant.id}/tasks/{task.id}/executions/{execution.id}/artifacts/one/stdout.txt"
    artifact = ExecutionArtifact(
        execution_id=execution.id,
        manifest_id=manifest.id,
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        runner_id=runner.id,
        command_id="cmd-42",
        artifact_kind="stdout",
        relative_path="artifacts/stdout.txt",
        object_key=object_key,
        upload_status="upload_pending",
        content_sha256="a" * 64,
        byte_size=12,
        mime_type="text/plain",
        artifact_metadata={"artifact_client_id": "artifact-client-1"},
    )
    db.add(artifact)
    db.commit()

    service = ArtifactUploadService(
        db,
        object_store=_StubHeadObjectStore(
            head_result=ObjectHead(
                object_key=object_key,
                byte_size=12,
                content_type="text/plain",
                content_sha256="a" * 64,
            )
        ),
    )
    mismatched_cases = (
        _upload_envelope(
            tenant=tenant,
            runner=runner,
            task=task,
            tool_runtime_job=tool_runtime_job,
            task_runtime_job=task_runtime_job,
            artifact=artifact,
            object_key=object_key,
            content_sha256="a" * 64,
            size_bytes=12,
            command_id="cmd-foreign",
        ),
        _upload_envelope(
            tenant=tenant,
            runner=runner,
            task=task,
            tool_runtime_job=tool_runtime_job,
            task_runtime_job=task_runtime_job,
            artifact=artifact,
            object_key=object_key,
            content_sha256="a" * 64,
            size_bytes=12,
            workspace_id=f"task-{task.id}-foreign",
        ),
        _upload_envelope(
            tenant=tenant,
            runner=runner,
            task=task,
            tool_runtime_job=tool_runtime_job,
            task_runtime_job=task_runtime_job,
            artifact=artifact,
            object_key=f"{object_key}.foreign",
            content_sha256="a" * 64,
            size_bytes=12,
        ),
    )

    for envelope in mismatched_cases:
        with pytest.raises(ArtifactUploadServiceError) as error:
            service.handle_inbound_message(
                tenant_id=tenant.id,
                runner_id=runner.id,
                task_id=task.id,
                runtime_job_id=tool_runtime_job.id,
                envelope=envelope,
            )
        assert error.value.error_code == "RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED"


@pytest.mark.asyncio
async def test_upload_complete_publishes_safe_browser_status_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _build_session()
    tenant, runner, task, task_runtime_job, tool_runtime_job, execution, manifest = _seed_upload_context(db)

    from backend.services.data_plane.local_object_store import LocalObjectStore

    content = b"hello-world"
    object_store = LocalObjectStore(root_path=tmp_path / "objects")
    object_key = f"tenants/{tenant.id}/tasks/{task.id}/executions/{execution.id}/artifacts/one/stdout.txt"
    object_store.put_bytes(object_key, content, content_type="text/plain")

    artifact = ExecutionArtifact(
        execution_id=execution.id,
        manifest_id=manifest.id,
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        runner_id=runner.id,
        command_id="cmd-42",
        artifact_kind="stdout",
        relative_path="artifacts/stdout.txt",
        object_key=object_key,
        upload_status="upload_pending",
        content_sha256="afa27b44d43b02a9fea41d13b4b4d04210d1009b0c36d6e41009f8fc54a0f4f5",
        byte_size=len(content),
        mime_type="text/plain",
        artifact_metadata={"artifact_client_id": "artifact-client-1"},
    )
    db.add(artifact)
    db.commit()

    events: list[tuple[int, dict]] = []

    class _StubHub:
        async def publish(self, task_id: int, event: dict) -> None:
            events.append((task_id, event))

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _StubHub(),
    )

    service = ArtifactUploadService(db, object_store=object_store)
    envelope = _upload_envelope(
        tenant=tenant,
        runner=runner,
        task=task,
        tool_runtime_job=tool_runtime_job,
        task_runtime_job=task_runtime_job,
        artifact=artifact,
        object_key=object_key,
        content_sha256=artifact.content_sha256,
        size_bytes=len(content),
    )
    service.handle_inbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        envelope=envelope,
    )
    await asyncio.sleep(0)

    assert events
    event_task_id, event = events[0]
    assert event_task_id == task.id
    assert event["type"] == "status"
    assert event["content"] == "artifact_upload_status"
    metadata = event.get("metadata")
    assert isinstance(metadata, dict)
    assert "upload_url" not in metadata
    assert "upload_headers" not in metadata
    statuses = metadata.get("artifact_statuses")
    assert isinstance(statuses, list)
    assert statuses[0].get("artifact_id") == str(artifact.id)
