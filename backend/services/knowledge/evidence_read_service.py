""" durable evidence read contract for engagement-owned archives.

Scope:
- Provide bounded reads over `knowledge_evidence_archives` for engagement APIs.

Responsibilities:
- Enforce engagement scope and durable-root path boundaries.
- Apply deterministic bounded read modes (`auto`, `head`, `tail`, `match`, `full`).
- Return typed read results without leaking host filesystem paths.

Boundary:
- This service reads existing durable archive rows only.
- Archive policy/write behavior is owned by `knowledge_archive_service.py`."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session

from backend.config.workspace_config import WorkspaceConfig
from backend.models.core import Engagement
from backend.models.knowledge import KnowledgeEvidenceArchive
from backend.services.data_plane.object_store import ObjectStore
from backend.services.data_plane.registry import get_object_store
from .archive_service import KnowledgeArchiveService

KnowledgeEvidenceReadMode = Literal["auto", "head", "tail", "match", "full"]
KnowledgeEvidenceReadStatus = Literal["ready", "not_found", "not_available"]
KnowledgeEvidenceReadSource = Literal["inline_excerpt", "object_ref", "archived_file", "none"]

_DEFAULT_MAX_CHARS = 4000
_MAX_READ_CHARS = 20000
_MAX_FILE_SCAN_CHARS = 200000
_TEXTUAL_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-ndjson",
        "application/x-yaml",
        "application/yaml",
    }
)


@dataclass(frozen=True, slots=True)
class KnowledgeEvidenceReadRequest:
    """Bounded durable evidence read request contract."""

    mode: KnowledgeEvidenceReadMode = "auto"
    query: str | None = None
    max_chars: int = _DEFAULT_MAX_CHARS

    def normalized_max_chars(self) -> int:
        """Return service-safe read budget."""
        return max(1, min(int(self.max_chars), _MAX_READ_CHARS))


@dataclass(frozen=True, slots=True)
class KnowledgeEvidenceReadResult:
    """Durable evidence read response contract."""

    status: KnowledgeEvidenceReadStatus
    evidence_archive_id: str
    storage_mode: str
    content: str | None
    mode_used: KnowledgeEvidenceReadMode
    truncated: bool
    source: KnowledgeEvidenceReadSource


class KnowledgeEvidenceReadService:
    """Engagement-scoped bounded reader for durable evidence archives."""

    def __init__(self, db: Session, *, object_store: ObjectStore | None = None) -> None:
        self.db = db
        self._object_store = object_store or get_object_store()

    def read_evidence(
        self,
        *,
        tenant_id: int | None = None,
        user_id: int | None = None,
        engagement_id: int,
        evidence_id: str,
        request: KnowledgeEvidenceReadRequest,
    ) -> KnowledgeEvidenceReadResult:
        """Read one durable evidence row in bounded mode without leaking host paths."""
        scoped_engagement_id = self._require_engagement_scope(engagement_id)
        scoped_tenant_id = self._resolve_tenant_scope(
            tenant_id=tenant_id,
            engagement_id=scoped_engagement_id,
        )
        if scoped_tenant_id is None:
            return KnowledgeEvidenceReadResult(
                status="not_found",
                evidence_archive_id=str(evidence_id),
                storage_mode="unknown",
                content=None,
                mode_used=self._resolve_auto_mode(request=request, text=""),
                truncated=False,
                source="none",
            )
        if user_id is not None and not self._engagement_belongs_to_user(
            engagement_id=scoped_engagement_id,
            tenant_id=scoped_tenant_id,
            user_id=int(user_id),
        ):
            return KnowledgeEvidenceReadResult(
                status="not_found",
                evidence_archive_id=str(evidence_id),
                storage_mode="unknown",
                content=None,
                mode_used=self._resolve_auto_mode(request=request, text=""),
                truncated=False,
                source="none",
            )
        query = self.db.query(KnowledgeEvidenceArchive).filter(
            KnowledgeEvidenceArchive.engagement_id == scoped_engagement_id,
            KnowledgeEvidenceArchive.id == evidence_id,
            KnowledgeEvidenceArchive.tenant_id == scoped_tenant_id,
        )
        if user_id is not None:
            query = query.filter(KnowledgeEvidenceArchive.user_id == int(user_id))

        row = query.one_or_none()
        if row is None:
            return KnowledgeEvidenceReadResult(
                status="not_found",
                evidence_archive_id=str(evidence_id),
                storage_mode="unknown",
                content=None,
                mode_used=self._resolve_auto_mode(request=request, text=""),
                truncated=False,
                source="none",
            )

        storage_mode = KnowledgeArchiveService.normalize_storage_mode(str(row.storage_mode or ""))

        if row.inline_excerpt is not None:
            content, truncated, mode_used = self._apply_mode_with_source_bound(
                text=str(row.inline_excerpt),
                request=request,
                source_truncated=False,
            )
            return KnowledgeEvidenceReadResult(
                status="ready",
                evidence_archive_id=str(row.id),
                storage_mode=storage_mode,
                content=content,
                mode_used=mode_used,
                truncated=truncated,
                source="inline_excerpt",
            )

        object_text, object_truncated = self._read_object_backed_text(row=row)
        if object_text is not None:
            content, bounded_truncated, mode_used = self._apply_mode_with_source_bound(
                text=object_text,
                request=request,
                source_truncated=object_truncated,
            )
            return KnowledgeEvidenceReadResult(
                status="ready",
                evidence_archive_id=str(row.id),
                storage_mode=storage_mode,
                content=content,
                mode_used=mode_used,
                truncated=bounded_truncated,
                source="object_ref",
            )

        file_path = self._resolve_archived_file_path(
            engagement_id=scoped_engagement_id,
            archived_file_ref=str(row.archived_file_ref or ""),
        )
        text, file_truncated = self._read_text_file(file_path, mime_type=str(row.mime_type or ""))
        if text is not None:
            content, bounded_truncated, mode_used = self._apply_mode_with_source_bound(
                text=text,
                request=request,
                source_truncated=file_truncated,
            )
            return KnowledgeEvidenceReadResult(
                status="ready",
                evidence_archive_id=str(row.id),
                storage_mode=storage_mode,
                content=content,
                mode_used=mode_used,
                truncated=bounded_truncated,
                source="archived_file",
            )

        if storage_mode == "metadata_only":
            return KnowledgeEvidenceReadResult(
                status="not_available",
                evidence_archive_id=str(row.id),
                storage_mode=storage_mode,
                content=None,
                mode_used=self._resolve_auto_mode(request=request, text=""),
                truncated=False,
                source="none",
            )

        return KnowledgeEvidenceReadResult(
            status="not_available",
            evidence_archive_id=str(row.id),
            storage_mode=storage_mode,
            content=None,
            mode_used=self._resolve_auto_mode(request=request, text=""),
            truncated=False,
            source="none",
        )

    @staticmethod
    def _require_engagement_scope(engagement_id: int) -> int:
        try:
            parsed = int(engagement_id)
        except (TypeError, ValueError):
            raise ValueError("Valid engagement_id is required for durable evidence access") from None
        if parsed <= 0:
            raise ValueError("Valid engagement_id is required for durable evidence access")
        return parsed

    @staticmethod
    def _require_tenant_scope(tenant_id: int) -> int:
        try:
            parsed = int(tenant_id)
        except (TypeError, ValueError):
            raise ValueError("Valid tenant_id is required for durable evidence access") from None
        if parsed <= 0:
            raise ValueError("Valid tenant_id is required for durable evidence access")
        return parsed

    def _resolve_tenant_scope(self, *, tenant_id: int | None, engagement_id: int) -> int | None:
        if tenant_id is not None:
            return self._require_tenant_scope(tenant_id)
        resolved_tenant_id = (
            self.db.query(Engagement.tenant_id)
            .filter(Engagement.id == int(engagement_id))
            .scalar()
        )
        if resolved_tenant_id is None:
            return None
        try:
            return self._require_tenant_scope(int(resolved_tenant_id))
        except ValueError:
            return None

    def _engagement_belongs_to_user(
        self,
        *,
        engagement_id: int,
        tenant_id: int,
        user_id: int,
    ) -> bool:
        return (
            self.db.query(Engagement.id)
            .filter(
                Engagement.id == int(engagement_id),
                Engagement.tenant_id == int(tenant_id),
                Engagement.user_id == int(user_id),
            )
            .scalar()
            is not None
        )

    @staticmethod
    def _resolve_auto_mode(
        *,
        request: KnowledgeEvidenceReadRequest,
        text: str,
    ) -> KnowledgeEvidenceReadMode:
        _ = text
        if request.mode != "auto":
            return request.mode
        if (request.query or "").strip():
            return "match"
        return "head"

    @classmethod
    def _apply_mode(
        cls,
        *,
        text: str,
        request: KnowledgeEvidenceReadRequest,
    ) -> tuple[str, bool, KnowledgeEvidenceReadMode]:
        budget = request.normalized_max_chars()
        if not text:
            return "", False, cls._resolve_auto_mode(request=request, text=text)

        mode = cls._resolve_auto_mode(request=request, text=text)
        if mode == "head":
            sliced = text[:budget]
            return sliced, len(sliced) < len(text), "head"

        if mode == "tail":
            sliced = text[-budget:]
            return sliced, len(sliced) < len(text), "tail"

        if mode == "match":
            query = (request.query or "").strip()
            if not query:
                sliced = text[:budget]
                return sliced, len(sliced) < len(text), "head"
            lower_text = text.lower()
            hit = lower_text.find(query.lower())
            if hit < 0:
                sliced = text[:budget]
                return sliced, len(sliced) < len(text), "head"
            half = max(1, budget // 2)
            start = max(0, hit - half)
            end = min(len(text), start + budget)
            return text[start:end], (start > 0 or end < len(text)), "match"

        # Full is explicit but still bounded.
        sliced = text[:budget]
        return sliced, len(sliced) < len(text), "full"

    @classmethod
    def _apply_mode_with_source_bound(
        cls,
        *,
        text: str,
        request: KnowledgeEvidenceReadRequest,
        source_truncated: bool,
    ) -> tuple[str, bool, KnowledgeEvidenceReadMode]:
        content, bounded_truncated, mode_used = cls._apply_mode(text=text, request=request)
        return content, bool(bounded_truncated or source_truncated), mode_used

    def _read_object_backed_text(self, *, row: KnowledgeEvidenceArchive) -> tuple[str | None, bool]:
        object_key = str(row.object_key or "").strip()
        if not object_key:
            return None, False
        if not self._mime_type_is_textual(str(row.mime_type or "")):
            return None, False
        try:
            raw = self._object_store.read_bytes(object_key, max_bytes=_MAX_FILE_SCAN_CHARS + 1)
        except Exception:
            return None, False
        truncated = len(raw) > _MAX_FILE_SCAN_CHARS
        bounded = raw[:_MAX_FILE_SCAN_CHARS]
        try:
            return bounded.decode("utf-8"), truncated
        except UnicodeDecodeError:
            return None, False

    @staticmethod
    def _resolve_archived_file_path(*, engagement_id: int, archived_file_ref: str) -> Path | None:
        normalized_ref = str(archived_file_ref or "").strip()
        if not normalized_ref:
            return None
        if normalized_ref.startswith("pending://"):
            return None

        root = WorkspaceConfig.get_engagement_durable_root_path(engagement_id).resolve()
        ref_path = Path(normalized_ref)
        try:
            candidate = ref_path.resolve() if ref_path.is_absolute() else (root / ref_path).resolve()
        except Exception:
            return None

        try:
            candidate.relative_to(root)
        except ValueError:
            return None

        if not candidate.exists() or not candidate.is_file():
            return None
        return candidate

    @staticmethod
    def _read_text_file(file_path: Path | None, *, mime_type: str) -> tuple[str | None, bool]:
        if file_path is None:
            return None, False
        if not KnowledgeEvidenceReadService._mime_type_is_textual(mime_type):
            return None, False
        try:
            with file_path.open("rb") as handle:
                raw = handle.read(_MAX_FILE_SCAN_CHARS + 1)
        except Exception:
            return None, False

        truncated = len(raw) > _MAX_FILE_SCAN_CHARS
        bounded = raw[:_MAX_FILE_SCAN_CHARS]
        try:
            return bounded.decode("utf-8"), truncated
        except UnicodeDecodeError:
            return None, False

    @staticmethod
    def _mime_type_is_textual(mime_type: str) -> bool:
        normalized = str(mime_type or "").strip().lower()
        if not normalized:
            return True
        if normalized.startswith("text/"):
            return True
        return normalized in _TEXTUAL_MIME_TYPES


__all__ = [
    "KnowledgeEvidenceReadMode",
    "KnowledgeEvidenceReadRequest",
    "KnowledgeEvidenceReadResult",
    "KnowledgeEvidenceReadService",
]
