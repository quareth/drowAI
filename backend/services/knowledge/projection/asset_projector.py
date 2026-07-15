"""Asset projector for deterministic tenant/user-scoped knowledge_assets upserts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from ....models import KnowledgeAsset
from ..identity_service import IdentityMergeDecision


@dataclass(frozen=True)
class AssetUpsertResult:
    """Result envelope for one asset upsert call."""

    row: KnowledgeAsset
    inserted: bool


class AssetProjector:
    """Upsert durable asset rows from identity merge decisions."""

    def upsert(
        self,
        *,
        db: Session,
        user_id: int,
        decision: IdentityMergeDecision,
        merged_state: Mapping[str, Any],
        engagement_id: int | None = None,
        tenant_id: int,
    ) -> AssetUpsertResult:
        existing = self._resolve_existing_row(
            db=db,
            tenant_id=tenant_id,
            user_id=user_id,
            asset_key=decision.identity_key,
        )
        inserted = existing is None
        row = existing or KnowledgeAsset(
            tenant_id=tenant_id,
            user_id=int(user_id),
            engagement_id=engagement_id,
            asset_key=decision.identity_key,
            asset_type=self._asset_type_from_key(decision.identity_key),
            first_seen_at=decision.first_seen_at,
            last_seen_at=decision.last_seen_at,
        )

        ip_address, hostname, display_name = self._asset_fields_from_key(decision.identity_key)
        metadata = dict(merged_state.get("metadata") or {})
        state = dict(metadata.get("state") or {})

        row.asset_type = self._asset_type_from_key(decision.identity_key)
        row.tenant_id = int(tenant_id)
        row.ip_address = ip_address
        row.hostname = hostname
        row.display_name = display_name
        row.status = str(state.get("host_status") or row.status or "").strip() or None
        row.first_seen_at = merged_state.get("first_seen_at")
        row.last_seen_at = merged_state.get("last_seen_at")
        row.max_confidence = merged_state.get("confidence")
        row.asset_metadata = metadata

        if inserted:
            db.add(row)
        db.flush()
        return AssetUpsertResult(row=row, inserted=inserted)

    @staticmethod
    def _resolve_existing_row(
        *,
        db: Session,
        tenant_id: int,
        user_id: int,
        asset_key: str,
    ) -> KnowledgeAsset | None:
        return db.execute(
            select(KnowledgeAsset).where(
                KnowledgeAsset.tenant_id == int(tenant_id),
                KnowledgeAsset.user_id == int(user_id),
                KnowledgeAsset.asset_key == str(asset_key),
            )
        ).scalar_one_or_none()

    @staticmethod
    def _asset_type_from_key(asset_key: str) -> str:
        prefix = str(asset_key or "").split(":", 1)[0].strip().lower()
        return prefix or "unknown"

    @staticmethod
    def _asset_fields_from_key(asset_key: str) -> tuple[str | None, str | None, str | None]:
        key = str(asset_key or "").strip().lower()
        if key.startswith("host.ip:"):
            value = key.split(":", 1)[1]
            return value, None, value
        if key.startswith("host.dns:"):
            value = key.split(":", 1)[1]
            return None, value, value
        return None, None, None
