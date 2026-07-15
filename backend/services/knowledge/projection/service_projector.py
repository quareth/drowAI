"""Service projector for deterministic tenant/user-scoped knowledge_services upserts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from ....models import KnowledgeAsset, KnowledgeService
from ..identity_service import IdentityMergeDecision
from runtime_shared.semantic.service_identity import parse_service_socket_key


@dataclass(frozen=True)
class ServiceUpsertResult:
    """Result envelope for one service upsert call."""

    row: KnowledgeService
    inserted: bool


class ServiceProjector:
    """Upsert durable service rows from identity merge decisions."""

    def upsert(
        self,
        *,
        db: Session,
        user_id: int,
        decision: IdentityMergeDecision,
        merged_state: Mapping[str, Any],
        asset_key_to_id: Mapping[str, str],
        engagement_id: int | None = None,
        tenant_id: int,
    ) -> ServiceUpsertResult:
        existing = self._resolve_existing_row(
            db=db,
            tenant_id=tenant_id,
            user_id=user_id,
            service_key=decision.identity_key,
        )
        inserted = existing is None
        row = existing or KnowledgeService(
            tenant_id=tenant_id,
            user_id=int(user_id),
            engagement_id=engagement_id,
            service_key=decision.identity_key,
            first_seen_at=decision.first_seen_at,
            last_seen_at=decision.last_seen_at,
        )

        parsed = self._parse_service_key(decision.identity_key)
        metadata = dict(merged_state.get("metadata") or {})
        state = dict(metadata.get("state") or {})
        asset_id = self._resolve_asset_id(
            db=db,
            tenant_id=tenant_id,
            user_id=user_id,
            parsed=parsed,
            asset_key_to_id=asset_key_to_id,
        )

        row.asset_id = asset_id
        row.tenant_id = int(tenant_id)
        row.protocol = parsed.get("protocol")
        row.port = parsed.get("port")
        row.service_name = str(state.get("service_name") or row.service_name or "").strip() or None
        row.product = str(state.get("product") or row.product or "").strip() or None
        row.version = str(state.get("version") or row.version or "").strip() or None
        row.status = str(state.get("status") or row.status or "").strip() or None
        row.first_seen_at = merged_state.get("first_seen_at")
        row.last_seen_at = merged_state.get("last_seen_at")
        row.service_metadata = metadata

        if inserted:
            db.add(row)
        db.flush()
        return ServiceUpsertResult(row=row, inserted=inserted)

    @staticmethod
    def _resolve_existing_row(
        *,
        db: Session,
        tenant_id: int,
        user_id: int,
        service_key: str,
    ) -> KnowledgeService | None:
        return db.execute(
            select(KnowledgeService).where(
                KnowledgeService.tenant_id == int(tenant_id),
                KnowledgeService.user_id == int(user_id),
                KnowledgeService.service_key == str(service_key),
            )
        ).scalar_one_or_none()

    @staticmethod
    def _parse_service_key(service_key: str) -> dict[str, Any]:
        parsed = parse_service_socket_key(service_key)
        if parsed is None:
            return {"ip": None, "protocol": None, "port": None}
        return {
            "ip": parsed.ip,
            "protocol": parsed.protocol,
            "port": parsed.port,
        }

    @staticmethod
    def _resolve_asset_id(
        *,
        db: Session,
        tenant_id: int,
        user_id: int,
        parsed: Mapping[str, Any],
        asset_key_to_id: Mapping[str, str],
    ) -> str | None:
        ip = str(parsed.get("ip") or "").strip().lower()
        if not ip:
            return None
        host_asset_key = f"host.ip:{ip}"
        cached = asset_key_to_id.get(host_asset_key)
        if cached:
            return str(cached)
        asset = db.execute(
            select(KnowledgeAsset.id).where(
                KnowledgeAsset.asset_key == host_asset_key,
                KnowledgeAsset.tenant_id == int(tenant_id),
                KnowledgeAsset.user_id == int(user_id),
            )
        ).scalar_one_or_none()
        return str(asset) if asset is not None else None
