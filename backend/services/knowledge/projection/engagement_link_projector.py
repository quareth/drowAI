"""Engagement link projector for many-to-many engagement-entity associations.

Populates the engagement lens tables so that canonical entities can be
filtered by the engagement that discovered them, without tying identity to
the engagement lifecycle.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ....models import (
    EngagementAssetLink,
    EngagementFindingLink,
    EngagementServiceLink,
    EngagementWebPathLink,
)


class EngagementLinkProjector:
    """Upsert engagement-entity link rows with first/last seen timestamps."""

    def upsert_asset_link(
        self,
        *,
        db: Session,
        engagement_id: int,
        asset_id: str,
        observed_at: datetime,
        tenant_id: int,
    ) -> None:
        self._upsert_link(
            db, EngagementAssetLink, "asset_id", tenant_id, engagement_id, asset_id, observed_at
        )

    def upsert_service_link(
        self,
        *,
        db: Session,
        engagement_id: int,
        service_id: str,
        observed_at: datetime,
        tenant_id: int,
    ) -> None:
        self._upsert_link(
            db, EngagementServiceLink, "service_id", tenant_id, engagement_id, service_id, observed_at
        )

    def upsert_finding_link(
        self,
        *,
        db: Session,
        engagement_id: int,
        finding_id: str,
        observed_at: datetime,
        tenant_id: int,
    ) -> None:
        self._upsert_link(
            db, EngagementFindingLink, "finding_id", tenant_id, engagement_id, finding_id, observed_at
        )

    def upsert_web_path_link(
        self,
        *,
        db: Session,
        engagement_id: int,
        web_path_id: str,
        observed_at: datetime,
        tenant_id: int,
    ) -> None:
        self._upsert_link(
            db, EngagementWebPathLink, "web_path_id", tenant_id, engagement_id, web_path_id, observed_at
        )

    @staticmethod
    def _upsert_link(
        db: Session,
        model_class: type,
        entity_id_column: str,
        tenant_id: int,
        engagement_id: int,
        entity_id: str,
        observed_at: datetime,
    ) -> None:
        observed_at = EngagementLinkProjector._normalize_datetime(observed_at)
        existing = db.execute(
            select(model_class).where(
                model_class.engagement_id == int(engagement_id),
                getattr(model_class, entity_id_column) == str(entity_id),
            )
        ).scalar_one_or_none()

        if existing is not None:
            first_seen = EngagementLinkProjector._normalize_datetime(existing.first_seen_in_engagement)
            last_seen = EngagementLinkProjector._normalize_datetime(existing.last_seen_in_engagement)
            if observed_at < first_seen:
                existing.first_seen_in_engagement = observed_at
            if observed_at > last_seen:
                existing.last_seen_in_engagement = observed_at
            if getattr(existing, "tenant_id", None) != int(tenant_id):
                existing.tenant_id = int(tenant_id)
            return

        row = model_class(
            tenant_id=int(tenant_id),
            engagement_id=int(engagement_id),
            first_seen_in_engagement=observed_at,
            last_seen_in_engagement=observed_at,
        )
        setattr(row, entity_id_column, str(entity_id))
        db.add(row)
        db.flush()

    @staticmethod
    def _normalize_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)
