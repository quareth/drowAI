"""Object-backed evidence materialization for durable knowledge archives.

Scope:
- Copy object-backed execution artifacts into engagement-scoped evidence keys.

Responsibilities:
- Read source artifact bytes from the shared object store.
- Write bytes to deterministic tenant/engagement evidence object keys.
- Return normalized object reference metadata for archive-row persistence.

Boundary:
- This service owns object-store copy mechanics only.
- Archive policy decisions remain in `archive_service.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from backend.services.data_plane.object_key_builder import build_evidence_object_key
from backend.services.data_plane.object_store import ObjectStore
from backend.services.data_plane.registry import get_object_store


@dataclass(frozen=True, slots=True)
class EvidenceObjectMaterialization:
    """Object-reference metadata produced by evidence materialization."""

    object_key: str
    content_sha256: str
    byte_size: int
    mime_type: str | None


class EvidenceStorageService:
    """Object-store copy helper for durable evidence archives."""

    def __init__(self, *, object_store: ObjectStore | None = None) -> None:
        self._object_store = object_store or get_object_store()

    def materialize_object_reference(
        self,
        *,
        tenant_id: int,
        engagement_id: int,
        evidence_id: str,
        artifact_id: str,
        source_object_key: str,
        source_relative_path: str | None,
        mime_type: str | None,
    ) -> EvidenceObjectMaterialization | None:
        """Copy source object bytes into evidence namespace and return metadata."""
        normalized_source_key = str(source_object_key or "").strip()
        if not normalized_source_key:
            return None

        try:
            source_bytes = self._object_store.read_bytes(normalized_source_key)
        except Exception:
            return None

        safe_filename = self._resolve_filename(source_relative_path, artifact_id)
        target_key = build_evidence_object_key(
            tenant_id=tenant_id,
            engagement_id=engagement_id,
            evidence_id=evidence_id,
            filename=safe_filename,
        )
        object_head = self._object_store.put_bytes(
            target_key,
            source_bytes,
            content_type=mime_type,
            metadata={
                "archive_source": "execution_artifact",
                "source_artifact_id": str(artifact_id),
            },
        )
        content_hash = str(object_head.content_sha256 or "").strip() or sha256(source_bytes).hexdigest()
        return EvidenceObjectMaterialization(
            object_key=object_head.object_key,
            content_sha256=content_hash,
            byte_size=int(object_head.byte_size),
            mime_type=mime_type,
        )

    @staticmethod
    def _resolve_filename(source_relative_path: str | None, artifact_id: str) -> str:
        raw = str(source_relative_path or "").strip()
        if raw:
            candidate = Path(raw.replace("\\", "/")).name.strip()
            if candidate:
                return candidate
        return f"{artifact_id}.bin"


def is_runner_backed_artifact(*, upload_status: str | None, object_key: str | None) -> bool:
    """Return whether an artifact row is in the Data Plane runner data plane."""
    normalized_upload_status = str(upload_status or "").strip().lower()
    normalized_object_key = str(object_key or "").strip()
    if normalized_object_key:
        return True
    return normalized_upload_status in {
        "manifest_pending",
        "upload_pending",
        "ready",
        "upload_failed",
        "failed",
    }


def runner_archive_mode_allowed(mode: str) -> bool:
    """Validate storage mode for new runner-backed evidence writes."""
    return str(mode).strip().lower() in {"object_ref", "inline_excerpt", "metadata_only"}


def sanitize_storage_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Return metadata copy without mutable caller references."""
    return dict(metadata or {})


__all__ = [
    "EvidenceObjectMaterialization",
    "EvidenceStorageService",
    "is_runner_backed_artifact",
    "runner_archive_mode_allowed",
    "sanitize_storage_metadata",
]
