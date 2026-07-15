"""Tests for object-backed durable evidence storage materialization."""

from __future__ import annotations

from pathlib import Path

from backend.services.data_plane.local_object_store import LocalObjectStore
from backend.services.knowledge.evidence_storage_service import EvidenceStorageService


def test_materialize_object_reference_copies_source_object_to_evidence_scope(tmp_path: Path) -> None:
    source_key = "tenants/1/tasks/9/executions/3/artifacts/7/report.bin"
    source_payload = b"\xAA\xBB\xCC"
    object_store = LocalObjectStore(root_path=tmp_path / "object-store")
    object_store.put_bytes(source_key, source_payload, content_type="application/octet-stream")
    service = EvidenceStorageService(object_store=object_store)

    result = service.materialize_object_reference(
        tenant_id=1,
        engagement_id=5,
        evidence_id="evidence-1",
        artifact_id="artifact-7",
        source_object_key=source_key,
        source_relative_path="reports/final-report.bin",
        mime_type="application/octet-stream",
    )

    assert result is not None
    assert result.object_key.startswith("tenants/1/engagements/5/evidence/evidence-1/")
    assert result.byte_size == 3
    assert result.content_sha256 == "fa22dfe1da9013b3c1145040acae9089e0c08bc1c1a0719614f4b73add6f6ef5"
    assert object_store.read_bytes(result.object_key) == source_payload


def test_materialize_object_reference_returns_none_when_source_key_missing(tmp_path: Path) -> None:
    service = EvidenceStorageService(
        object_store=LocalObjectStore(root_path=tmp_path / "object-store")
    )
    result = service.materialize_object_reference(
        tenant_id=1,
        engagement_id=2,
        evidence_id="evidence-2",
        artifact_id="artifact-2",
        source_object_key="tenants/1/tasks/1/missing.bin",
        source_relative_path="missing.bin",
        mime_type="application/octet-stream",
    )
    assert result is None


def test_materialize_object_reference_uses_target_tenant_scope_from_server_row(tmp_path: Path) -> None:
    source_key = "tenants/99/tasks/9/executions/3/artifacts/7/report.bin"
    object_store = LocalObjectStore(root_path=tmp_path / "object-store")
    object_store.put_bytes(source_key, b"payload", content_type="application/octet-stream")
    service = EvidenceStorageService(object_store=object_store)

    result = service.materialize_object_reference(
        tenant_id=7,
        engagement_id=12,
        evidence_id="evidence-tenant-check",
        artifact_id="artifact-99",
        source_object_key=source_key,
        source_relative_path="reports/final-report.bin",
        mime_type="application/octet-stream",
    )

    assert result is not None
    assert result.object_key.startswith("tenants/7/engagements/12/evidence/evidence-tenant-check/")
