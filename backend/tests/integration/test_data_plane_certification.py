"""Data-plane certification scenarios across runner ingest and artifact reads.

Scope:
- Exercises runner manifest -> upload-complete -> tool-result promotion flow.
- Verifies artifact catalog, file-browser preview/download, and artifact.read behavior.
- Verifies duplicate message handling and tenant fail-closed artifact reads.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import uuid

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from backend.config.data_plane import DataPlaneConfig
from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.knowledge import KnowledgeEvidenceArchive, KnowledgeIngestionRun, KnowledgeObservation
from backend.models.provenance import ArtifactManifest, ExecutionArtifact, ToolExecution
from backend.models.runner_control import ExecutionSite, Runner, RuntimeJob
from backend.models.tenant import Tenant
from backend.services.artifact.memory_service import (
    ArtifactMemoryService,
    ArtifactReadRequest,
    ArtifactSearchFilters,
)
from backend.services.data_plane.artifact_file_browser_service import ArtifactFileBrowserService
from backend.services.data_plane.artifact_manifest_service import ArtifactManifestService
from backend.services.data_plane.artifact_read_service import ArtifactReadService
from backend.services.data_plane.artifact_upload_service import ArtifactUploadService
from backend.services.data_plane.local_object_store import LocalObjectStore
from backend.services.knowledge.evidence_read_service import KnowledgeEvidenceReadRequest, KnowledgeEvidenceReadService
from backend.services.knowledge.replay_service import KnowledgeReplayService
from backend.services.runner_control.runtime_event_service import RuntimeEventService
from runtime_shared.runner_protocol import (
    RUNNER_PROTOCOL_DATA_PLANE_VERSION,
    RunnerArtifactManifestItem,
    RunnerArtifactManifestPayload,
    RunnerArtifactUploadCompleteItem,
    RunnerArtifactUploadCompletePayload,
    RunnerEnvelope,
    RunnerMessageType,
    RunnerToolResultPayload,
)


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed_runner_context(db: Session) -> tuple[Tenant, User, Runner, Task, RuntimeJob, RuntimeJob, str]:
    suffix = uuid.uuid4().hex[:10]
    tenant = Tenant(slug=f"tenant-{suffix}", name="Tenant")
    db.add(tenant)
    db.flush()

    user = User(
        username=f"data-plane-cert-user-{suffix}",
        password="test-password",
        email=f"data-plane-cert-{suffix}@example.test",
    )
    db.add(user)
    db.flush()
    engagement = Engagement(
        user_id=user.id,
        tenant_id=tenant.id,
        name=f"Data Plane Certification Engagement {suffix}",
        status="active",
    )
    db.add(engagement)
    db.flush()

    site = ExecutionSite(
        tenant_id=tenant.id,
        name="Primary Site",
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
    )
    db.add(runner)
    db.flush()

    task = Task(
        user_id=user.id,
        tenant_id=tenant.id,
        engagement_id=engagement.id,
        name=f"Data Plane Certification Task {suffix}",
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
            "tool": "information_gathering.network_discovery.fping",
            "args": {"target": "10.0.0.5"},
            "tool_call_id": "tool-call-42",
            "agent_path": "runner.tool_command",
        },
    )
    db.add(tool_runtime_job)
    db.commit()

    return tenant, user, runner, task, task_runtime_job, tool_runtime_job, workspace_id


def _build_data_plane(root: Path) -> tuple[DataPlaneConfig, LocalObjectStore]:
    config = DataPlaneConfig(
        object_store_backend="local",
        local_object_store_root=root,
        object_store_bucket=None,
        object_store_prefix="data-plane-cert",
        signed_upload_ttl_seconds=900,
        signed_download_ttl_seconds=900,
        max_artifact_size_bytes=64 * 1024 * 1024,
        max_manifest_items=256,
        max_zip_download_size_bytes=64 * 1024 * 1024,
    )
    return config, LocalObjectStore(root_path=root)


def _manifest_envelope(
    *,
    tenant_id: int,
    runner_id: uuid.UUID,
    task_id: int,
    runtime_job_id: uuid.UUID,
    task_runtime_job_id: uuid.UUID,
    workspace_id: str,
    message_id: str,
) -> RunnerEnvelope:
    return RunnerEnvelope(
        message_id=message_id,
        message_type=RunnerMessageType.ARTIFACT_MANIFEST,
        schema_version=RUNNER_PROTOCOL_DATA_PLANE_VERSION,
        tenant_id=str(tenant_id),
        runner_id=str(runner_id),
        correlation_id=f"corr-{message_id}",
        runtime_job_id=str(runtime_job_id),
        task_id=task_id,
        created_at=datetime.now(tz=UTC).isoformat(),
        payload=RunnerArtifactManifestPayload(
            task_runtime_job_id=str(task_runtime_job_id),
            command_id="cmd-42",
            workspace_id=workspace_id,
            tool_call_id="tool-call-42",
            tool_batch_id="tool-batch-42",
            artifacts=(
                RunnerArtifactManifestItem(
                    artifact_client_id="artifact-text-1",
                    relative_path="artifacts/cmd-42/report.txt",
                    artifact_kind="tool_file",
                    size_bytes=23,
                    content_sha256="283b00b85dd14baef29e36b8576b592e87980fc8fc6fed840f91fc5632e2e1de",
                    content_type="text/plain",
                    is_text=True,
                    created_at="2026-05-25T12:00:00+00:00",
                    metadata={"source": "certification"},
                ),
                RunnerArtifactManifestItem(
                    artifact_client_id="artifact-bin-1",
                    relative_path="artifacts/cmd-42/screenshot.png",
                    artifact_kind="tool_file",
                    size_bytes=8,
                    content_sha256="4c4b6a3be1314ab86138bef4314dde022e600960d8689a2c8f8631802d20dab6",
                    content_type="image/png",
                    is_text=False,
                    created_at="2026-05-25T12:00:01+00:00",
                    metadata={"source": "certification"},
                ),
            ),
        ),
        raw_message_type=RunnerMessageType.ARTIFACT_MANIFEST.value,
    )


def _tool_result_payload(*, workspace_id: str) -> RunnerToolResultPayload:
    return RunnerToolResultPayload(
        operation_id="tool-op-42",
        command_id="cmd-42",
        tool="information_gathering.network_discovery.fping",
        status="succeeded",
        success=True,
        exit_code=0,
        stdout="command completed",
        stderr="",
        artifacts=("artifacts/cmd-42/report.txt", "artifacts/cmd-42/screenshot.png"),
        error_code=None,
        error_message=None,
        result={"semantic_schema_version": "data_plane.v1"},
        metadata={
            "workspace_id": workspace_id,
            "tool_call_id": "tool-call-42",
            "semantic_observations": [
                {
                    "observation_type": "network.host_discovered",
                    "subject_type": "host.ip",
                    "subject_key": "host.ip:10.0.0.5",
                    "payload": {"source": "fping"},
                }
            ],
            "semantic_evidence": [{"evidence_kind": "host_probe", "artifact_client_id": "artifact-text-1"}],
            "semantic_schema_version": "data_plane.v1",
            "capability_family": "network_discovery",
        },
    )


def _promote_artifacts(
    *,
    db: Session,
    tenant: Tenant,
    runner: Runner,
    task: Task,
    task_runtime_job: RuntimeJob,
    tool_runtime_job: RuntimeJob,
    workspace_id: str,
    object_store: LocalObjectStore,
    config: DataPlaneConfig,
) -> None:
    manifest_service = ArtifactManifestService(db, object_store=object_store, data_plane_config=config)
    upload_service = ArtifactUploadService(db, object_store=object_store)

    manifest = _manifest_envelope(
        tenant_id=tenant.id,
        runner_id=runner.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        task_runtime_job_id=task_runtime_job.id,
        workspace_id=workspace_id,
        message_id="msg-manifest-cert-1",
    )
    manifest_result = manifest_service.handle_inbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        envelope=manifest,
    )

    upload_request = manifest_result.response_envelopes[0]
    uploads = upload_request.payload.uploads

    RuntimeEventService(db).apply_runtime_event(
        tenant_id=tenant.id,
        runner_id=runner.id,
        envelope=RunnerEnvelope(
            message_id="msg-tool-result-cert-1",
            message_type=RunnerMessageType.TOOL_RESULT,
            schema_version=RUNNER_PROTOCOL_DATA_PLANE_VERSION,
            tenant_id=str(tenant.id),
            runner_id=str(runner.id),
            correlation_id="corr-tool-result-cert-1",
            runtime_job_id=str(tool_runtime_job.id),
            task_id=task.id,
            created_at=datetime.now(tz=UTC).isoformat(),
            payload=_tool_result_payload(workspace_id=workspace_id),
            raw_message_type=RunnerMessageType.TOOL_RESULT.value,
        ),
    )

    payload_by_client_id = {
        "artifact-text-1": b"service=nginx\nport=443\n",
        "artifact-bin-1": b"\x89PNG\r\n\x1a\n",
    }
    completed_items: list[RunnerArtifactUploadCompleteItem] = []
    for upload in uploads:
        payload = payload_by_client_id[upload.artifact_client_id]
        object_store.put_bytes(upload.object_key, payload, content_type=upload.content_type)
        completed_items.append(
            RunnerArtifactUploadCompleteItem(
                artifact_id=upload.artifact_id,
                artifact_client_id=upload.artifact_client_id,
                object_key=upload.object_key,
                size_bytes=upload.size_bytes,
                content_sha256=upload.content_sha256,
                uploaded_at="2026-05-25T12:00:10+00:00",
            )
        )

    upload_complete = RunnerEnvelope(
        message_id="msg-upload-complete-cert-1",
        message_type=RunnerMessageType.ARTIFACT_UPLOAD_COMPLETE,
        schema_version=RUNNER_PROTOCOL_DATA_PLANE_VERSION,
        tenant_id=str(tenant.id),
        runner_id=str(runner.id),
        correlation_id="corr-upload-complete-cert-1",
        runtime_job_id=str(tool_runtime_job.id),
        task_id=task.id,
        created_at=datetime.now(tz=UTC).isoformat(),
        payload=RunnerArtifactUploadCompletePayload(
            task_runtime_job_id=str(task_runtime_job.id),
            command_id="cmd-42",
            workspace_id=workspace_id,
            tool_call_id="tool-call-42",
            tool_batch_id="tool-batch-42",
            uploads=tuple(completed_items),
        ),
        raw_message_type=RunnerMessageType.ARTIFACT_UPLOAD_COMPLETE.value,
    )
    upload_service.handle_inbound_message(
        tenant_id=tenant.id,
        runner_id=runner.id,
        task_id=task.id,
        runtime_job_id=tool_runtime_job.id,
        envelope=upload_complete,
    )
    db.commit()


def test_data_plane_text_and_binary_artifacts_are_available_from_data_plane_after_runtime_cleanup(tmp_path: Path) -> None:
    db = _build_session()
    try:
        tenant, user, runner, task, task_runtime_job, tool_runtime_job, workspace_id = _seed_runner_context(db)
        config, object_store = _build_data_plane(tmp_path / "object-store")
        _promote_artifacts(
            db=db,
            tenant=tenant,
            runner=runner,
            task=task,
            task_runtime_job=task_runtime_job,
            tool_runtime_job=tool_runtime_job,
            workspace_id=workspace_id,
            object_store=object_store,
            config=config,
        )
        execution = db.execute(
            select(ToolExecution).where(
                ToolExecution.task_id == task.id,
                ToolExecution.command_id == "cmd-42",
            )
        ).scalar_one()
        runs = db.execute(
            select(KnowledgeIngestionRun).where(
                KnowledgeIngestionRun.source_execution_id == execution.id
            )
        ).scalars().all()
        observations = db.execute(
            select(KnowledgeObservation).where(
                KnowledgeObservation.source_execution_id == execution.id
            )
        ).scalars().all()
        evidence_rows = db.execute(
            select(KnowledgeEvidenceArchive).where(
                KnowledgeEvidenceArchive.source_execution_id == execution.id
            )
        ).scalars().all()

        assert len(runs) == 1
        assert runs[0].status == "succeeded"
        snapshot = dict((runs[0].run_metadata or {}).get("semantic_input_snapshot") or {})
        assert snapshot.get("semantic_schema_version") == "data_plane.v1"
        assert snapshot.get("capability_family") == "network_discovery"
        assert snapshot.get("semantic_observations")
        assert observations
        assert any(item.observation_type == "network.host_discovered" for item in observations)
        assert evidence_rows
        assert any(row.storage_mode == "object_ref" for row in evidence_rows)

        evidence_reader = KnowledgeEvidenceReadService(db, object_store=object_store)
        evidence_read = evidence_reader.read_evidence(
            tenant_id=tenant.id,
            engagement_id=int(task.engagement_id or 0),
            evidence_id=str(evidence_rows[0].id),
            request=KnowledgeEvidenceReadRequest(mode="full", max_chars=4000),
        )
        assert evidence_read.status == "ready"
        assert (evidence_read.content or "") != ""

        memory = ArtifactMemoryService(
            db,
            object_read_service=ArtifactReadService(db, object_store=object_store),
        )
        browser = ArtifactFileBrowserService(
            db,
            object_store=object_store,
            object_read_service=ArtifactReadService(db, object_store=object_store),
            data_plane_config=config,
        )

        catalog = memory.search_task_artifacts(task_id=task.id, filters=ArtifactSearchFilters())
        by_path = {item.relative_path: item for item in catalog.artifacts if item.relative_path}
        assert "artifacts/cmd-42/report.txt" in by_path
        assert "artifacts/cmd-42/screenshot.png" in by_path

        tree = browser.get_directory_tree(tenant_id=tenant.id, task_id=task.id, path="/artifacts/cmd-42")
        child_names = {child["name"] for child in tree["children"]}
        assert {"report.txt", "screenshot.png"}.issubset(child_names)

        preview = browser.get_file_content(tenant_id=tenant.id, task_id=task.id, path="/artifacts/cmd-42/report.txt")
        assert "service=nginx" in preview["content"]

        report_read = memory.read_task_artifact(
            task_id=task.id,
            artifact_id=by_path["artifacts/cmd-42/report.txt"].artifact_id,
            request=ArtifactReadRequest(mode="full", max_chars=4000),
            user_id=user.id,
        )
        assert report_read.status == "ready"
        assert report_read.source == "object_store"
        assert "service=nginx" in (report_read.content or "")

        binary_read = memory.read_task_artifact(
            task_id=task.id,
            artifact_id=by_path["artifacts/cmd-42/screenshot.png"].artifact_id,
            request=ArtifactReadRequest(mode="full"),
            user_id=user.id,
        )
        assert binary_read.status == "not_available"

        download_path = browser.resolve_download_path(
            tenant_id=tenant.id,
            task_id=task.id,
            path="/artifacts/cmd-42/screenshot.png",
        )
        try:
            assert download_path.read_bytes() == b"\x89PNG\r\n\x1a\n"
        finally:
            download_path.unlink(missing_ok=True)

        db.execute(
            delete(RuntimeJob).where(
                RuntimeJob.id.in_((task_runtime_job.id, tool_runtime_job.id))
            )
        )
        db.commit()

        post_cleanup_read = memory.read_task_artifact(
            task_id=task.id,
            artifact_id=by_path["artifacts/cmd-42/report.txt"].artifact_id,
            request=ArtifactReadRequest(mode="full"),
            user_id=user.id,
        )
        assert post_cleanup_read.status == "ready"
        assert "service=nginx" in (post_cleanup_read.content or "")

        post_cleanup_evidence = evidence_reader.read_evidence(
            tenant_id=tenant.id,
            engagement_id=int(task.engagement_id or 0),
            evidence_id=str(evidence_rows[0].id),
            request=KnowledgeEvidenceReadRequest(mode="full", max_chars=4000),
        )
        assert post_cleanup_evidence.status == "ready"

        replay = KnowledgeReplayService(db).replay_execution(
            task_id=task.id,
            source_execution_id=str(execution.id),
            extractor_family="runtime.ingestion",
        )
        replay_runs = db.execute(
            select(KnowledgeIngestionRun).where(
                KnowledgeIngestionRun.source_execution_id == execution.id
            )
        ).scalars().all()
        assert replay["ok"] is True
        assert replay["replay_source_type"] == "runtime"
        assert len(replay_runs) >= 2
    finally:
        db.close()


def test_data_plane_upload_complete_reconciles_existing_evidence_rows_to_object_ref(tmp_path: Path) -> None:
    db = _build_session()
    try:
        tenant, _user, runner, task, task_runtime_job, tool_runtime_job, workspace_id = _seed_runner_context(db)
        config, object_store = _build_data_plane(tmp_path / "object-store")
        manifest_service = ArtifactManifestService(db, object_store=object_store, data_plane_config=config)
        upload_service = ArtifactUploadService(db, object_store=object_store)

        manifest = _manifest_envelope(
            tenant_id=tenant.id,
            runner_id=runner.id,
            task_id=task.id,
            runtime_job_id=tool_runtime_job.id,
            task_runtime_job_id=task_runtime_job.id,
            workspace_id=workspace_id,
            message_id="msg-manifest-cert-reconcile-1",
        )
        manifest_result = manifest_service.handle_inbound_message(
            tenant_id=tenant.id,
            runner_id=runner.id,
            task_id=task.id,
            runtime_job_id=tool_runtime_job.id,
            envelope=manifest,
        )

        RuntimeEventService(db).apply_runtime_event(
            tenant_id=tenant.id,
            runner_id=runner.id,
            envelope=RunnerEnvelope(
                message_id="msg-tool-result-cert-reconcile-1",
                message_type=RunnerMessageType.TOOL_RESULT,
                schema_version=RUNNER_PROTOCOL_DATA_PLANE_VERSION,
                tenant_id=str(tenant.id),
                runner_id=str(runner.id),
                correlation_id="corr-tool-result-cert-reconcile-1",
                runtime_job_id=str(tool_runtime_job.id),
                task_id=task.id,
                created_at=datetime.now(tz=UTC).isoformat(),
                payload=_tool_result_payload(workspace_id=workspace_id),
                raw_message_type=RunnerMessageType.TOOL_RESULT.value,
            ),
        )

        text_artifact = db.execute(
            select(ExecutionArtifact).where(
                ExecutionArtifact.task_id == task.id,
                ExecutionArtifact.command_id == "cmd-42",
                ExecutionArtifact.relative_path == "artifacts/cmd-42/report.txt",
            )
        ).scalar_one()
        pre_upload_evidence = db.execute(
            select(KnowledgeEvidenceArchive).where(
                KnowledgeEvidenceArchive.source_artifact_id == text_artifact.id
            )
        ).scalars().all()
        assert pre_upload_evidence
        assert all(row.storage_mode != "object_ref" for row in pre_upload_evidence)

        upload_request = manifest_result.response_envelopes[0]
        payload_by_client_id = {
            "artifact-text-1": b"service=nginx\nport=443\n",
            "artifact-bin-1": b"\x89PNG\r\n\x1a\n",
        }
        completed_items: list[RunnerArtifactUploadCompleteItem] = []
        for upload in upload_request.payload.uploads:
            payload = payload_by_client_id[upload.artifact_client_id]
            object_store.put_bytes(upload.object_key, payload, content_type=upload.content_type)
            completed_items.append(
                RunnerArtifactUploadCompleteItem(
                    artifact_id=upload.artifact_id,
                    artifact_client_id=upload.artifact_client_id,
                    object_key=upload.object_key,
                    size_bytes=upload.size_bytes,
                    content_sha256=upload.content_sha256,
                    uploaded_at="2026-05-25T12:00:10+00:00",
                )
            )

        upload_service.handle_inbound_message(
            tenant_id=tenant.id,
            runner_id=runner.id,
            task_id=task.id,
            runtime_job_id=tool_runtime_job.id,
            envelope=RunnerEnvelope(
                message_id="msg-upload-complete-cert-reconcile-1",
                message_type=RunnerMessageType.ARTIFACT_UPLOAD_COMPLETE,
                schema_version=RUNNER_PROTOCOL_DATA_PLANE_VERSION,
                tenant_id=str(tenant.id),
                runner_id=str(runner.id),
                correlation_id="corr-upload-complete-cert-reconcile-1",
                runtime_job_id=str(tool_runtime_job.id),
                task_id=task.id,
                created_at=datetime.now(tz=UTC).isoformat(),
                payload=RunnerArtifactUploadCompletePayload(
                    task_runtime_job_id=str(task_runtime_job.id),
                    command_id="cmd-42",
                    workspace_id=workspace_id,
                    tool_call_id="tool-call-42",
                    tool_batch_id="tool-batch-42",
                    uploads=tuple(completed_items),
                ),
                raw_message_type=RunnerMessageType.ARTIFACT_UPLOAD_COMPLETE.value,
            ),
        )
        db.commit()

        post_upload_evidence = db.execute(
            select(KnowledgeEvidenceArchive).where(
                KnowledgeEvidenceArchive.source_artifact_id == text_artifact.id
            )
        ).scalars().all()
        assert post_upload_evidence
        assert any(row.storage_mode == "object_ref" for row in post_upload_evidence)
        assert any(str(row.object_key or "").strip() for row in post_upload_evidence if row.storage_mode == "object_ref")
    finally:
        db.close()


def test_data_plane_duplicate_manifest_upload_tool_result_are_idempotent_and_cross_tenant_reads_fail_closed(
    tmp_path: Path,
) -> None:
    db = _build_session()
    try:
        tenant, _user, runner, task, task_runtime_job, tool_runtime_job, workspace_id = _seed_runner_context(db)
        config, object_store = _build_data_plane(tmp_path / "object-store")

        _promote_artifacts(
            db=db,
            tenant=tenant,
            runner=runner,
            task=task,
            task_runtime_job=task_runtime_job,
            tool_runtime_job=tool_runtime_job,
            workspace_id=workspace_id,
            object_store=object_store,
            config=config,
        )

        manifests_before = db.execute(select(ArtifactManifest)).scalars().all()
        artifacts_before = db.execute(select(ExecutionArtifact)).scalars().all()
        executions_before = db.execute(select(ToolExecution)).scalars().all()

        _promote_artifacts(
            db=db,
            tenant=tenant,
            runner=runner,
            task=task,
            task_runtime_job=task_runtime_job,
            tool_runtime_job=tool_runtime_job,
            workspace_id=workspace_id,
            object_store=object_store,
            config=config,
        )

        manifests_after = db.execute(select(ArtifactManifest)).scalars().all()
        artifacts_after = db.execute(select(ExecutionArtifact)).scalars().all()
        executions_after = db.execute(select(ToolExecution)).scalars().all()

        assert len(manifests_before) == len(manifests_after) == 1
        assert len(executions_before) == len(executions_after) == 1
        assert len(artifacts_before) == len(artifacts_after)

        text_artifact = next(
            item for item in artifacts_after if item.relative_path == "artifacts/cmd-42/report.txt"
        )
        evidence_row = db.execute(
            select(KnowledgeEvidenceArchive).where(
                KnowledgeEvidenceArchive.source_artifact_id == text_artifact.id
            )
        ).scalars().first()
        assert evidence_row is not None
        read_service = ArtifactReadService(db, object_store=object_store)
        denied = read_service.read_artifact_text(
            task_id=task.id,
            tenant_id=tenant.id + 1,
            artifact_id=str(text_artifact.id),
        )
        allowed = read_service.read_artifact_text(
            task_id=task.id,
            tenant_id=tenant.id,
            artifact_id=str(text_artifact.id),
        )
        evidence_reader = KnowledgeEvidenceReadService(db, object_store=object_store)
        denied_evidence = evidence_reader.read_evidence(
            tenant_id=tenant.id + 1,
            engagement_id=int(task.engagement_id or 0),
            evidence_id=str(evidence_row.id),
            request=KnowledgeEvidenceReadRequest(mode="full", max_chars=4000),
        )

        assert denied.status == "not_found"
        assert denied_evidence.status == "not_found"
        assert allowed.status == "ready"
        assert "service=nginx" in (allowed.content or "")
    finally:
        db.close()
