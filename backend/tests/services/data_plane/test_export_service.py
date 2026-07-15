"""Tests for Data Plane tenant-scoped data-plane task exports.

Scope:
- Verifies tenant/task export boundaries and lineage preservation.
- Confirms signed upload URLs are stripped from export payloads.
- Validates bounded inline object-content export mode.
"""

from __future__ import annotations

from base64 import b64decode
from datetime import UTC, datetime
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.knowledge import (
    EngagementAssetLink,
    EngagementFindingLink,
    EngagementServiceLink,
    EngagementWebPathLink,
    KnowledgeAsset,
    KnowledgeEntityProvenance,
    KnowledgeEvidenceArchive,
    KnowledgeFinding,
    KnowledgeIngestionRun,
    KnowledgeObservation,
    KnowledgeRelationship,
    KnowledgeService,
    KnowledgeWebPath,
)
from backend.models.provenance import ArtifactManifest, ExecutionArtifact, ToolExecution
from backend.models.streaming import StreamEvent
from backend.models.tenant import Tenant
from backend.services.data_plane.export_service import DataPlaneExportService
from backend.services.data_plane.local_object_store import LocalObjectStore


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed_task_bundle(
    db: Session,
    *,
    tenant_suffix: str,
    artifact_bytes: bytes,
    evidence_bytes: bytes,
    include_signed_urls: bool,
) -> tuple[int, int]:
    tenant = Tenant(slug=f"tenant-{tenant_suffix}", name=f"Tenant {tenant_suffix}")
    db.add(tenant)
    db.flush()

    user = User(
        username=f"user-{tenant_suffix}",
        password="password",
        email=f"{tenant_suffix}@example.com",
    )
    db.add(user)
    db.flush()

    engagement = Engagement(
        user_id=user.id,
        tenant_id=tenant.id,
        name=f"Engagement {tenant_suffix}",
    )
    db.add(engagement)
    db.flush()

    task = Task(
        user_id=user.id,
        tenant_id=tenant.id,
        engagement_id=engagement.id,
        name=f"Task {tenant_suffix}",
    )
    db.add(task)
    db.flush()

    execution = ToolExecution(
        tenant_id=tenant.id,
        task_id=task.id,
        tool_name="shell.exec",
        tool_arguments={"command": "cat artifacts/out.txt"},
        agent_path="runner.tool_command",
        status="succeeded",
        started_at=datetime.now(tz=UTC),
        command_id=f"cmd-{tenant_suffix}",
        workspace_id=f"task-{task.id}",
    )
    db.add(execution)
    db.flush()

    manifest_json = {
        "artifacts": [{"relative_path": "artifacts/out.txt"}],
    }
    manifest_metadata = {"status": "accepted"}
    if include_signed_urls:
        manifest_json["upload"] = {"signed_url": "https://object-store.invalid/upload?sig=123"}
        manifest_metadata["signed_upload_url"] = "https://object-store.invalid/upload?sig=456"

    manifest = ArtifactManifest(
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=None,
        runner_id=None,
        command_id=f"cmd-{tenant_suffix}",
        workspace_id=f"task-{task.id}",
        message_id=f"msg-{tenant_suffix}",
        status="accepted",
        manifest_json=manifest_json,
        manifest_metadata=manifest_metadata,
    )
    db.add(manifest)
    db.flush()

    artifact_key = f"tenants/{tenant.id}/tasks/{task.id}/executions/{execution.id}/artifacts/output.txt"
    evidence_key = f"tenants/{tenant.id}/tasks/{task.id}/evidence/{execution.id}.json"
    artifact_metadata = {"source": "runner"}
    if include_signed_urls:
        artifact_metadata["signed_download_url"] = "https://object-store.invalid/download?sig=999"

    artifact = ExecutionArtifact(
        execution_id=execution.id,
        manifest_id=manifest.id,
        tenant_id=tenant.id,
        task_id=task.id,
        runtime_job_id=None,
        runner_id=None,
        command_id=f"cmd-{tenant_suffix}",
        artifact_kind="tool_file",
        relative_path="artifacts/out.txt",
        object_key=artifact_key,
        upload_status="ready",
        content_sha256="a" * 64,
        byte_size=len(artifact_bytes),
        mime_type="text/plain",
        is_text=True,
        artifact_metadata=artifact_metadata,
    )
    db.add(artifact)
    db.flush()

    ingestion_run = KnowledgeIngestionRun(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
        source_execution_id=execution.id,
        extractor_family="data_plane",
        extractor_version="1",
        status="completed",
    )
    db.add(ingestion_run)
    db.flush()

    observation = KnowledgeObservation(
        tenant_id=tenant.id,
        user_id=user.id,
        ingestion_run_id=ingestion_run.id,
        engagement_id=engagement.id,
        task_id=task.id,
        source_execution_id=execution.id,
        observation_type="network.open_port",
        subject_type="service",
        subject_key=f"service-{tenant_suffix}",
        assertion_level="observed",
        dedupe_key=f"dedupe-{tenant_suffix}",
        payload={"port": 443},
        observed_at=datetime.now(tz=UTC),
    )
    db.add(observation)
    db.flush()

    evidence_archive = KnowledgeEvidenceArchive(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
        source_execution_id=execution.id,
        source_artifact_id=artifact.id,
        storage_mode="object_ref",
        object_key=evidence_key,
        content_sha256="b" * 64,
        byte_size=len(evidence_bytes),
        mime_type="application/json",
        lineage_snapshot={
            "execution_id": str(execution.id),
            "artifact_id": str(artifact.id),
        },
    )
    db.add(evidence_archive)
    db.flush()

    asset = KnowledgeAsset(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        asset_key=f"asset-{tenant_suffix}",
        asset_type="host.ip",
        first_seen_at=datetime.now(tz=UTC),
        last_seen_at=datetime.now(tz=UTC),
    )
    db.add(asset)
    db.flush()

    service = KnowledgeService(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        service_key=f"service-{tenant_suffix}",
        asset_id=asset.id,
        first_seen_at=datetime.now(tz=UTC),
        last_seen_at=datetime.now(tz=UTC),
    )
    db.add(service)
    db.flush()

    finding = KnowledgeFinding(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        finding_key=f"finding-{tenant_suffix}",
        finding_type="vulnerability",
        subject_type="service",
        subject_key=f"service-{tenant_suffix}",
        service_id=service.id,
        first_seen_at=datetime.now(tz=UTC),
        last_seen_at=datetime.now(tz=UTC),
    )
    db.add(finding)
    db.flush()

    relationship = KnowledgeRelationship(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        relationship_key=f"relationship-{tenant_suffix}",
        source_subject_key=f"asset-{tenant_suffix}",
        relationship_type="hosts",
        target_subject_key=f"service-{tenant_suffix}",
        first_seen_at=datetime.now(tz=UTC),
        last_seen_at=datetime.now(tz=UTC),
    )
    db.add(relationship)
    db.flush()

    web_path = KnowledgeWebPath(
        tenant_id=tenant.id,
        user_id=user.id,
        asset_id=asset.id,
        service_id=service.id,
        canonical_url=f"https://{tenant_suffix}.example.com/path",
        origin_key=f"origin-{tenant_suffix}",
        path="/path",
        first_seen_at=datetime.now(tz=UTC),
        last_seen_at=datetime.now(tz=UTC),
        producer_summary={"source": "gobuster"},
        evidence_refs=[{"evidence_archive_id": str(artifact.id)}],
    )
    db.add(web_path)
    db.flush()

    db.add(
        EngagementAssetLink(
            tenant_id=tenant.id,
            engagement_id=engagement.id,
            asset_id=asset.id,
            first_seen_in_engagement=datetime.now(tz=UTC),
            last_seen_in_engagement=datetime.now(tz=UTC),
        )
    )
    db.add(
        EngagementServiceLink(
            tenant_id=tenant.id,
            engagement_id=engagement.id,
            service_id=service.id,
            first_seen_in_engagement=datetime.now(tz=UTC),
            last_seen_in_engagement=datetime.now(tz=UTC),
        )
    )
    db.add(
        EngagementFindingLink(
            tenant_id=tenant.id,
            engagement_id=engagement.id,
            finding_id=finding.id,
            first_seen_in_engagement=datetime.now(tz=UTC),
            last_seen_in_engagement=datetime.now(tz=UTC),
        )
    )
    db.add(
        EngagementWebPathLink(
            tenant_id=tenant.id,
            engagement_id=engagement.id,
            web_path_id=web_path.id,
            first_seen_in_engagement=datetime.now(tz=UTC),
            last_seen_in_engagement=datetime.now(tz=UTC),
        )
    )
    db.add(
        KnowledgeEntityProvenance(
            tenant_id=tenant.id,
            user_id=user.id,
            entity_type="asset",
            entity_id=asset.id,
            engagement_id=engagement.id,
            task_id=task.id,
            execution_id=execution.id,
            tool_name="shell.exec",
            ingestion_run_id=ingestion_run.id,
            observed_at=datetime.now(tz=UTC),
            evidence_archive_id=evidence_archive.id,
        )
    )
    db.add(
        StreamEvent(
            task_id=task.id,
            tenant_id=tenant.id,
            sequence=1,
            event_type="runner_runtime_event",
            payload={"type": "artifact.upload.complete", "task_id": task.id},
        )
    )
    db.commit()
    return int(tenant.id), int(task.id)


def _assert_no_signed_url_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_lower = str(key).lower()
            assert key_lower not in {"signed_url", "signed_upload_url", "signed_download_url"}
            assert not ("signed" in key_lower and "url" in key_lower)
            _assert_no_signed_url_keys(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_signed_url_keys(item)


def test_export_is_tenant_scoped(tmp_path) -> None:
    db = _build_session()
    store = LocalObjectStore(root_path=tmp_path / "object-store")
    try:
        tenant_a, task_a = _seed_task_bundle(
            db,
            tenant_suffix=uuid.uuid4().hex[:8],
            artifact_bytes=b"artifact-a",
            evidence_bytes=b"evidence-a",
            include_signed_urls=False,
        )
        tenant_b, task_b = _seed_task_bundle(
            db,
            tenant_suffix=uuid.uuid4().hex[:8],
            artifact_bytes=b"artifact-b",
            evidence_bytes=b"evidence-b",
            include_signed_urls=False,
        )
        assert tenant_a != tenant_b
        assert task_a != task_b

        db.add(
            StreamEvent(
                task_id=task_a,
                tenant_id=tenant_b,
                sequence=2,
                event_type="runner_runtime_event",
                payload={"type": "cross_tenant.injected", "task_id": task_a},
            )
        )
        db.commit()

        service = DataPlaneExportService(db, object_store=store)
        export = service.export_task_bundle(tenant_id=tenant_a, task_id=task_a).to_dict()

        assert export["tenant_id"] == tenant_a
        assert export["task_id"] == task_a
        assert all(item["tenant_id"] == tenant_a for item in export["tool_executions"])
        assert all(item["tenant_id"] == tenant_a for item in export["execution_artifacts"])
        assert all(item["task_id"] == task_a for item in export["execution_artifacts"])
        assert all(item["task_id"] == task_a for item in export["stream_events"])
        assert all(item["tenant_id"] == tenant_a for item in export["stream_events"])
        assert not any(item["tenant_id"] == tenant_b for item in export["tool_executions"])
        assert not any(item["tenant_id"] == tenant_b for item in export["stream_events"])
    finally:
        db.close()


def test_export_excludes_unrelated_tenant_web_paths_outside_task_engagement(tmp_path) -> None:
    db = _build_session()
    store = LocalObjectStore(root_path=tmp_path / "object-store")
    tenant_suffix = uuid.uuid4().hex[:8]
    try:
        tenant_id, task_id = _seed_task_bundle(
            db,
            tenant_suffix=tenant_suffix,
            artifact_bytes=b"artifact-a",
            evidence_bytes=b"evidence-a",
            include_signed_urls=False,
        )

        task = (
            db.query(Task)
            .filter(Task.id == task_id, Task.tenant_id == tenant_id)
            .one()
        )
        user_id = int(task.user_id)

        unrelated_engagement = Engagement(
            user_id=user_id,
            tenant_id=tenant_id,
            name=f"Unrelated {tenant_suffix}",
        )
        db.add(unrelated_engagement)
        db.flush()

        unrelated_asset = KnowledgeAsset(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=unrelated_engagement.id,
            asset_key=f"asset-unrelated-{tenant_suffix}",
            asset_type="host.ip",
            first_seen_at=datetime.now(tz=UTC),
            last_seen_at=datetime.now(tz=UTC),
        )
        db.add(unrelated_asset)
        db.flush()

        unrelated_service = KnowledgeService(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=unrelated_engagement.id,
            service_key=f"service-unrelated-{tenant_suffix}",
            asset_id=unrelated_asset.id,
            first_seen_at=datetime.now(tz=UTC),
            last_seen_at=datetime.now(tz=UTC),
        )
        db.add(unrelated_service)
        db.flush()

        unrelated_web_path = KnowledgeWebPath(
            tenant_id=tenant_id,
            user_id=user_id,
            asset_id=unrelated_asset.id,
            service_id=unrelated_service.id,
            canonical_url=f"https://unrelated-{tenant_suffix}.example.com/out-of-scope",
            origin_key=f"origin-unrelated-{tenant_suffix}",
            path="/out-of-scope",
            first_seen_at=datetime.now(tz=UTC),
            last_seen_at=datetime.now(tz=UTC),
            producer_summary={"source": "crawler"},
            evidence_refs=[],
        )
        db.add(unrelated_web_path)
        db.flush()

        db.add(
            EngagementWebPathLink(
                tenant_id=tenant_id,
                engagement_id=unrelated_engagement.id,
                web_path_id=unrelated_web_path.id,
                first_seen_in_engagement=datetime.now(tz=UTC),
                last_seen_in_engagement=datetime.now(tz=UTC),
            )
        )
        db.commit()

        export = DataPlaneExportService(db, object_store=store).export_task_bundle(
            tenant_id=tenant_id,
            task_id=task_id,
        ).to_dict()

        web_paths = export["knowledge_read_models"]["knowledge_web_paths"]
        web_path_urls = {item["canonical_url"] for item in web_paths}
        assert web_path_urls == {f"https://{tenant_suffix}.example.com/path"}
        assert f"https://unrelated-{tenant_suffix}.example.com/out-of-scope" not in web_path_urls
    finally:
        db.close()


def test_export_preserves_artifact_and_evidence_lineage(tmp_path) -> None:
    db = _build_session()
    store = LocalObjectStore(root_path=tmp_path / "object-store")
    try:
        tenant_id, task_id = _seed_task_bundle(
            db,
            tenant_suffix=uuid.uuid4().hex[:8],
            artifact_bytes=b"artifact-lineage",
            evidence_bytes=b"evidence-lineage",
            include_signed_urls=False,
        )

        export = DataPlaneExportService(db, object_store=store).export_task_bundle(
            tenant_id=tenant_id,
            task_id=task_id,
        ).to_dict()

        execution_ids = {row["id"] for row in export["tool_executions"]}
        artifact_ids = {row["id"] for row in export["execution_artifacts"]}
        manifest_ids = {row["id"] for row in export["artifact_manifests"]}
        ingestion_ids = {row["id"] for row in export["knowledge_ingestion_runs"]}

        assert execution_ids
        assert artifact_ids
        assert manifest_ids
        assert ingestion_ids
        assert export["execution_artifacts"][0]["execution_id"] in execution_ids
        assert export["execution_artifacts"][0]["manifest_id"] in manifest_ids
        assert export["knowledge_evidence_archives"][0]["source_artifact_id"] in artifact_ids
        assert export["knowledge_evidence_archives"][0]["source_execution_id"] in execution_ids
        assert export["knowledge_observations"][0]["ingestion_run_id"] in ingestion_ids
        assert export["knowledge_observations"][0]["source_execution_id"] in execution_ids
    finally:
        db.close()


def test_export_strips_signed_upload_urls_from_payload(tmp_path) -> None:
    db = _build_session()
    store = LocalObjectStore(root_path=tmp_path / "object-store")
    try:
        tenant_id, task_id = _seed_task_bundle(
            db,
            tenant_suffix=uuid.uuid4().hex[:8],
            artifact_bytes=b"artifact-signed",
            evidence_bytes=b"evidence-signed",
            include_signed_urls=True,
        )

        export = DataPlaneExportService(db, object_store=store).export_task_bundle(
            tenant_id=tenant_id,
            task_id=task_id,
        ).to_dict()

        _assert_no_signed_url_keys(export)
    finally:
        db.close()


def test_export_includes_bounded_object_payload_mode(tmp_path) -> None:
    db = _build_session()
    store = LocalObjectStore(root_path=tmp_path / "object-store")
    artifact_bytes = b"artifact-content"
    evidence_bytes = b"evidence-content"
    try:
        tenant_id, task_id = _seed_task_bundle(
            db,
            tenant_suffix=uuid.uuid4().hex[:8],
            artifact_bytes=artifact_bytes,
            evidence_bytes=evidence_bytes,
            include_signed_urls=False,
        )
        artifact_key = export_artifact_key = (
            db.query(ExecutionArtifact)
            .filter(ExecutionArtifact.tenant_id == tenant_id, ExecutionArtifact.task_id == task_id)
            .one()
            .object_key
        )
        evidence_key = (
            db.query(KnowledgeEvidenceArchive)
            .filter(KnowledgeEvidenceArchive.tenant_id == tenant_id, KnowledgeEvidenceArchive.task_id == task_id)
            .one()
            .object_key
        )
        assert artifact_key
        assert evidence_key

        store.put_bytes(str(export_artifact_key), artifact_bytes, content_type="text/plain")
        store.put_bytes(str(evidence_key), evidence_bytes, content_type="application/json")

        export = DataPlaneExportService(db, object_store=store).export_task_bundle(
            tenant_id=tenant_id,
            task_id=task_id,
            include_object_payloads=True,
            max_object_bytes=4,
            max_total_object_bytes=12,
        ).to_dict()

        assert export["object_payload_mode"] == "bounded_inline"
        summary = export["object_payload_summary"]
        assert summary["included_count"] == 2
        assert summary["truncated_count"] == 2
        assert summary["total_returned_bytes"] == 8

        ready_payloads = [item for item in export["object_payloads"] if item.get("status") == "ready"]
        assert len(ready_payloads) == 2
        decoded = [b64decode(item["content_base64"]) for item in ready_payloads]
        assert b"arti" in decoded
        assert b"evid" in decoded
    finally:
        db.close()
