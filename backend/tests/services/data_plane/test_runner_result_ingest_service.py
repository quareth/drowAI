"""Tests for Data Plane runner tool-result provenance ingest service.

Scope:
- Validates idempotent execution upsert behavior for repeated `tool.result`.
- Confirms semantic metadata persistence shape and manifest/artifact linking.
- Verifies bounded output artifact persistence with object-backed fallback for
  oversized stdout/stderr payloads.
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
from backend.services.artifact.provenance_query_service import ArtifactProvenanceQueryService
from backend.services.artifact.runner_result_ingest_service import (
    RunnerResultIngestService,
    _extract_display_command,
)
from backend.services.data_plane.artifact_manifest_service import ArtifactManifestService
from backend.services.data_plane.local_object_store import LocalObjectStore
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
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed_runner_context(db: Session) -> tuple[Tenant, Runner, Task, RuntimeJob, RuntimeJob]:
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
            "tool": "shell.exec",
            "args": {"command": "cat artifacts/report.txt"},
            "tool_call_id": "tool-call-1",
            "agent_path": "runner.tool_command",
        },
    )
    db.add(tool_runtime_job)
    db.commit()

    return tenant, runner, task, task_runtime_job, tool_runtime_job


def _build_data_plane_config(root: Path) -> DataPlaneConfig:
    return DataPlaneConfig(
        object_store_backend="local",
        local_object_store_root=root,
        object_store_bucket=None,
        object_store_prefix="data_plane",
        signed_upload_ttl_seconds=900,
        signed_download_ttl_seconds=900,
        max_artifact_size_bytes=64 * 1024 * 1024,
        max_manifest_items=256,
        max_zip_download_size_bytes=64 * 1024 * 1024,
    )


def _tool_result_payload(*, workspace_id: str, stdout: str, stderr: str) -> RunnerToolResultPayload:
    return RunnerToolResultPayload(
        operation_id="tool-op-1",
        command_id="cmd-42",
        tool="shell.exec",
        status="succeeded",
        success=True,
        exit_code=0,
        stdout=stdout,
        stderr=stderr,
        artifacts=("artifacts/report.txt",),
        error_code=None,
        error_message=None,
        result={"semantic_schema_version": "data_plane.v1"},
        metadata={
            "workspace_id": workspace_id,
            "tool_call_id": "tool-call-1",
            "semantic_observations": [{"type": "network.host", "value": "10.0.0.1"}],
            "semantic_evidence": [{"artifact_id": "artifact-client-1"}],
            "capability_family": "network",
            "tool_metadata": {"semantic_schema_version": "data_plane.v1"},
        },
    )


def test_extract_display_command_reads_tooling_plane_prepared_command() -> None:
    db = _build_session()
    _, _, _, _, runtime_job = _seed_runner_context(db)
    runtime_job.payload_json = {
        "workspace_id": "task-1",
        "command_id": "cmd-nmap",
        "tool": "information_gathering.network_discovery.nmap",
        "command": "nmap -T4 -p 443 -sV -oX - 127.0.0.1",
        "tool_call_id": "tool-call-nmap",
    }

    assert (
        _extract_display_command(runtime_job=runtime_job)
        == "nmap -T4 -p 443 -sV -oX - 127.0.0.1"
    )


def test_extract_display_command_prefers_runtime_job_command_over_tool_result_metadata() -> None:
    db = _build_session()
    _, _, _, _, runtime_job = _seed_runner_context(db)
    runtime_job.payload_json = {
        "command": "nmap -p 80 10.0.0.1",
        "tool": "information_gathering.network_discovery.nmap",
    }
    tool_result = _tool_result_payload(
        workspace_id="task-1",
        stdout="",
        stderr="",
    )
    tool_result.metadata["command_text"] = "echo stale"

    assert (
        _extract_display_command(runtime_job=runtime_job, tool_result=tool_result)
        == "nmap -p 80 10.0.0.1"
    )


def test_extract_display_command_falls_back_to_tool_result_metadata() -> None:
    db = _build_session()
    _, _, _, _, runtime_job = _seed_runner_context(db)
    runtime_job.payload_json = {
        "tool": "shell.exec",
        "command_id": "cmd-1",
    }
    tool_result = _tool_result_payload(
        workspace_id="task-1",
        stdout="",
        stderr="",
    )
    tool_result.metadata["command_text"] = "fping -a -q 192.168.1.0/24"

    assert (
        _extract_display_command(runtime_job=runtime_job, tool_result=tool_result)
        == "fping -a -q 192.168.1.0/24"
    )


def test_extract_display_command_derives_structured_tool_command(tmp_path: Path) -> None:
    db = _build_session()
    _, _, _, _, runtime_job = _seed_runner_context(db)
    runtime_job.payload_json = {
        "workspace_id": "task-1",
        "command_id": "cmd-nmap",
        "tool": "information_gathering.network_discovery.nmap",
        "args": {
            "target": "127.0.0.1",
            "ports": "443",
            "service_detection": True,
        },
    }

    assert _extract_display_command(runtime_job=runtime_job) == (
        "nmap -T4 -p 443 -sV -oX - 127.0.0.1"
    )


def test_ingest_tooling_plane_runtime_job_persists_command_for_raw_output_batch(tmp_path: Path) -> None:
    db = _build_session()
    tenant, _runner, task, _task_runtime_job, tool_runtime_job = _seed_runner_context(db)
    workspace_id = f"task-{task.id}"
    prepared_command = "nmap -T4 -p 443 -sV -oX - 127.0.0.1"
    tool_runtime_job.payload_json = {
        "workspace_id": workspace_id,
        "command_id": "cmd-nmap",
        "task_runtime_job_id": str(_task_runtime_job.id),
        "tool": "information_gathering.network_discovery.nmap",
        "command": prepared_command,
        "tool_call_id": "tool-call-nmap",
    }
    db.commit()

    config = _build_data_plane_config(tmp_path / "objects")
    object_store = LocalObjectStore(root_path=config.local_object_store_root)
    ingest_service = RunnerResultIngestService(
        db,
        object_store=object_store,
        data_plane_config=config,
    )
    query_service = ArtifactProvenanceQueryService(db)

    payload = _tool_result_payload(workspace_id=workspace_id, stdout="PORT STATE\n443/tcp open", stderr="")
    payload.metadata["command_text"] = prepared_command
    payload.metadata["tool_call_id"] = "tool-call-nmap"

    execution = ingest_service.ingest_tool_result(
        tenant_id=tenant.id,
        runtime_job=tool_runtime_job,
        payload=payload,
        runtime_job_status="succeeded",
    )
    execution.tool_call_id = "tool-call-nmap"
    db.commit()

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
    assert command_artifact.content_text == prepared_command

    batch = query_service.get_raw_output_batch(
        task_id=task.id,
        tool_call_ids=["tool-call-nmap"],
    )
    entry = batch["results"]["tool-call-nmap"]
    assert entry["status"] == "ready"
    assert entry["output_text"].startswith(f"$ {prepared_command}\n")
    assert "PORT STATE" in entry["output_text"]


def test_ingest_tool_result_is_idempotent_and_preserves_manifest_pending_rows(tmp_path: Path) -> None:
    db = _build_session()
    tenant, runner, task, _task_runtime_job, tool_runtime_job = _seed_runner_context(db)
    workspace_id = f"task-{task.id}"

    execution = ToolExecution(
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        runner_id=runner.id,
        execution_site_id=runner.execution_site_id,
        command_id="cmd-42",
        workspace_id=workspace_id,
        tool_call_id="tool-call-1",
        tool_name="shell.exec",
        tool_arguments={"command": "cat artifacts/report.txt"},
        agent_path="runner.tool_command",
        status="pending",
        started_at=datetime.now(tz=UTC),
        execution_metadata={"runner_manifest": {"skeletal": True}},
    )
    db.add(execution)
    db.flush()

    pending_stdout = ExecutionArtifact(
        execution_id=execution.id,
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        runner_id=runner.id,
        command_id="cmd-42",
        artifact_kind="stdout",
        relative_path="artifacts/cmd-42/stdout.txt",
        source_path="/workspace/artifacts/cmd-42/stdout.txt",
        object_key="tenants/t/tasks/u/executions/e/artifacts/a/stdout.txt",
        storage_backend="local",
        upload_status="upload_pending",
        content_sha256="a" * 64,
        byte_size=128,
        mime_type="text/plain",
        is_text=True,
        artifact_metadata={"artifact_client_id": "artifact-client-1"},
    )
    db.add(pending_stdout)
    db.commit()

    config = _build_data_plane_config(tmp_path / "objects")
    object_store = LocalObjectStore(root_path=config.local_object_store_root)
    service = RunnerResultIngestService(db, object_store=object_store, data_plane_config=config)

    payload = _tool_result_payload(workspace_id=workspace_id, stdout="scan ok", stderr="")
    first = service.ingest_tool_result(
        tenant_id=tenant.id,
        runtime_job=tool_runtime_job,
        payload=payload,
        runtime_job_status="succeeded",
    )
    second = service.ingest_tool_result(
        tenant_id=tenant.id,
        runtime_job=tool_runtime_job,
        payload=payload,
        runtime_job_status="succeeded",
    )
    db.commit()

    executions = db.execute(select(ToolExecution)).scalars().all()
    assert len(executions) == 1
    assert first.id == second.id == execution.id

    artifacts = db.execute(select(ExecutionArtifact).where(ExecutionArtifact.execution_id == execution.id)).scalars().all()
    assert len([item for item in artifacts if item.artifact_kind == "stdout"]) == 1
    assert len([item for item in artifacts if item.artifact_kind == "command"]) == 1

    stdout_artifact = next(item for item in artifacts if item.artifact_kind == "stdout")
    assert stdout_artifact.upload_status == "upload_pending"

    refreshed_execution = db.get(ToolExecution, execution.id)
    assert refreshed_execution is not None
    assert refreshed_execution.status == "succeeded"
    assert refreshed_execution.execution_metadata["runner_manifest"]["skeletal"] is True
    assert refreshed_execution.execution_metadata["semantic_observations"]
    assert refreshed_execution.execution_metadata["semantic_evidence"]
    assert refreshed_execution.execution_metadata["semantic_schema_version"] == "data_plane.v1"


def test_ingest_tool_result_masks_tshark_secret_exposure_semantic_metadata(tmp_path: Path) -> None:
    db = _build_session()
    tenant, _runner, task, _task_runtime_job, tool_runtime_job = _seed_runner_context(db)
    workspace_id = f"task-{task.id}"
    raw_secret = "PocSecret-DurableMasking-Sentinel-9f4c2a"

    config = _build_data_plane_config(tmp_path / "objects")
    object_store = LocalObjectStore(root_path=config.local_object_store_root)
    service = RunnerResultIngestService(db, object_store=object_store, data_plane_config=config)

    payload = _tool_result_payload(workspace_id=workspace_id, stdout="ok", stderr="")
    payload.metadata["tool_metadata"] = {
        "analysis_mode": "secret_exposure",
        "secret_exposure": [
            {
                "field": "ftp.request.command_parameter",
                "kind": "protocol_auth_argument",
                "proof_mode": "proof_excerpt",
                "proof_excerpt": raw_secret,
            }
        ],
    }

    execution = service.ingest_tool_result(
        tenant_id=tenant.id,
        runtime_job=tool_runtime_job,
        payload=payload,
        runtime_job_status="succeeded",
    )
    db.commit()

    refreshed_execution = db.get(ToolExecution, execution.id)
    assert refreshed_execution is not None
    metadata = refreshed_execution.execution_metadata
    assert raw_secret not in str(metadata)
    assert metadata["tool_metadata"]["secret_exposure"][0]["proof_excerpt"].startswith(
        "<DURABLE_SECRET_MASK:"
    )
    assert metadata["semantic_snapshot"]["tool_metadata"]["secret_exposure"][0]["proof_excerpt"].startswith(
        "<DURABLE_SECRET_MASK:"
    )


def test_ingest_updates_existing_graph_execution_without_replacement(tmp_path: Path) -> None:
    db = _build_session()
    tenant, runner, task, _task_runtime_job, tool_runtime_job = _seed_runner_context(db)
    workspace_id = f"task-{task.id}"

    existing = ToolExecution(
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=None,
        runner_id=None,
        execution_site_id=None,
        command_id=None,
        workspace_id=workspace_id,
        tool_call_id="tool-call-1",
        tool_name="shell.exec",
        tool_arguments={"command": "cat artifacts/report.txt"},
        agent_path="langgraph",
        status="started",
        started_at=datetime.now(tz=UTC),
        execution_metadata={"source": "graph"},
    )
    db.add(existing)
    db.commit()

    config = _build_data_plane_config(tmp_path / "objects")
    object_store = LocalObjectStore(root_path=config.local_object_store_root)
    service = RunnerResultIngestService(db, object_store=object_store, data_plane_config=config)

    updated = service.ingest_tool_result(
        tenant_id=tenant.id,
        runtime_job=tool_runtime_job,
        payload=_tool_result_payload(workspace_id=workspace_id, stdout="ok", stderr="warn"),
        runtime_job_status="succeeded",
    )
    db.commit()

    executions = db.execute(select(ToolExecution).where(ToolExecution.task_id == task.id)).scalars().all()
    assert len(executions) == 1
    assert updated.id == existing.id
    assert updated.runtime_job_id == tool_runtime_job.id
    assert updated.command_id == "cmd-42"
    assert updated.status == "succeeded"


def test_ingest_before_manifest_eventually_reconciles_artifact_links(tmp_path: Path) -> None:
    db = _build_session()
    tenant, runner, task, task_runtime_job, tool_runtime_job = _seed_runner_context(db)
    workspace_id = f"task-{task.id}"

    config = _build_data_plane_config(tmp_path / "objects")
    object_store = LocalObjectStore(root_path=config.local_object_store_root)
    ingest_service = RunnerResultIngestService(db, object_store=object_store, data_plane_config=config)
    manifest_service = ArtifactManifestService(db, object_store=object_store, data_plane_config=config)

    execution = ingest_service.ingest_tool_result(
        tenant_id=tenant.id,
        runtime_job=tool_runtime_job,
        payload=_tool_result_payload(workspace_id=workspace_id, stdout="pre-manifest", stderr=""),
        runtime_job_status="succeeded",
    )

    manifest_envelope = RunnerEnvelope(
        message_id="msg-manifest-after-result",
        message_type=RunnerMessageType.ARTIFACT_MANIFEST,
        schema_version=RUNNER_PROTOCOL_DATA_PLANE_VERSION,
        tenant_id=str(tenant.id),
        runner_id=str(runner.id),
        correlation_id="corr-manifest",
        runtime_job_id=str(tool_runtime_job.id),
        task_id=task.id,
        created_at=datetime.now(tz=UTC).isoformat(),
        payload=RunnerArtifactManifestPayload(
            task_runtime_job_id=str(task_runtime_job.id),
            command_id="cmd-42",
            workspace_id=workspace_id,
            tool_call_id="tool-call-1",
            tool_batch_id="tool-batch-1",
            artifacts=(
                RunnerArtifactManifestItem(
                    artifact_client_id="artifact-client-1",
                    relative_path="artifacts/cmd-42/stdout.txt",
                    artifact_kind="stdout",
                    size_bytes=16,
                    content_sha256="b" * 64,
                    content_type="text/plain",
                    is_text=True,
                    created_at="2026-05-25T12:00:00+00:00",
                    metadata={},
                ),
            ),
        ),
        raw_message_type=RunnerMessageType.ARTIFACT_MANIFEST.value,
    )
    manifest_service.handle_inbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        envelope=manifest_envelope,
    )
    db.commit()

    executions = db.execute(select(ToolExecution).where(ToolExecution.task_id == task.id)).scalars().all()
    assert len(executions) == 1
    assert executions[0].id == execution.id

    manifest_artifact = (
        db.execute(
            select(ExecutionArtifact).where(
                ExecutionArtifact.task_id == task.id,
                ExecutionArtifact.runtime_job_id == tool_runtime_job.id,
                ExecutionArtifact.command_id == "cmd-42",
                ExecutionArtifact.relative_path == "artifacts/cmd-42/stdout.txt",
            )
        )
        .scalars()
        .one()
    )
    assert manifest_artifact.execution_id == execution.id


def test_large_stdout_uses_object_backed_storage(tmp_path: Path) -> None:
    db = _build_session()
    tenant, _runner, task, _task_runtime_job, tool_runtime_job = _seed_runner_context(db)
    workspace_id = f"task-{task.id}"

    config = _build_data_plane_config(tmp_path / "objects")
    object_store = LocalObjectStore(root_path=config.local_object_store_root)
    service = RunnerResultIngestService(
        db,
        object_store=object_store,
        data_plane_config=config,
        inline_text_max_bytes=64,
    )

    stdout_payload = "X" * 1024
    execution = service.ingest_tool_result(
        tenant_id=tenant.id,
        runtime_job=tool_runtime_job,
        payload=_tool_result_payload(workspace_id=workspace_id, stdout=stdout_payload, stderr=""),
        runtime_job_status="succeeded",
    )
    db.commit()

    stdout_artifact = (
        db.execute(
            select(ExecutionArtifact).where(
                ExecutionArtifact.execution_id == execution.id,
                ExecutionArtifact.artifact_kind == "stdout",
            )
        )
        .scalars()
        .first()
    )
    assert stdout_artifact is not None
    assert stdout_artifact.content_text is None
    assert stdout_artifact.object_key is not None
    assert stdout_artifact.upload_status == "ready"

    stored_bytes = object_store.read_bytes(stdout_artifact.object_key)
    assert stored_bytes.decode("utf-8") == stdout_payload
