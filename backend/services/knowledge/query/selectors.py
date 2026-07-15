"""SQLAlchemy selectors for knowledge query reads.

This module owns retrieval for tenant/user-scoped and engagement-scoped
knowledge reads. It returns model rows and counts; it does not shape API
payloads.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import String, and_, cast, func, or_
from sqlalchemy.orm import Session, joinedload

from ....models import (
    Engagement,
    EngagementAssetLink,
    EngagementFindingLink,
    EngagementServiceLink,
    EngagementWebPathLink,
    KnowledgeAsset,
    KnowledgeEvidenceArchive,
    KnowledgeFinding,
    KnowledgeRelationship,
    KnowledgeService,
    KnowledgeWebPath,
)
from ..adapters.web_common import build_web_origin_key
from .contracts import WEB_SURFACE_NOISY_HIDE_THRESHOLD


class KnowledgeQuerySelectors:
    """Read selectors for durable knowledge models."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def list_engagements(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        query_text: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Engagement], int]:
        query = self.db.query(Engagement)
        if tenant_id is not None:
            query = query.filter(Engagement.tenant_id == int(tenant_id))
        query = query.filter(Engagement.user_id == int(user_id))
        normalized_status = str(status or "active").strip().lower()
        if normalized_status and normalized_status != "all":
            query = query.filter(func.lower(func.coalesce(Engagement.status, "active")) == normalized_status)
        if query_text:
            like_query = f"%{query_text.lower()}%"
            query = query.filter(
                or_(
                    func.lower(func.coalesce(Engagement.name, "")).like(like_query),
                    func.lower(func.coalesce(Engagement.description, "")).like(like_query),
                )
            )
        total = int(query.order_by(None).count())
        rows = (
            query.order_by(Engagement.updated_at.desc(), Engagement.id.desc())
            .offset(int(offset))
            .limit(int(limit))
            .all()
        )
        return rows, total

    def get_engagement(
        self,
        *,
        engagement_id: int,
        tenant_id: int | None = None,
        user_id: int | None = None,
    ) -> Engagement | None:
        query = self.db.query(Engagement).filter(Engagement.id == int(engagement_id))
        if tenant_id is not None:
            query = query.filter(Engagement.tenant_id == int(tenant_id))
        if user_id is not None:
            query = query.filter(Engagement.user_id == int(user_id))
        return query.one_or_none()

    def is_owned_engagement(self, *, engagement_id: int, user_id: int) -> bool:
        return self.is_engagement_in_scope(engagement_id=engagement_id, user_id=user_id)

    def is_engagement_in_scope(
        self,
        *,
        engagement_id: int,
        user_id: int | None = None,
        tenant_id: int | None = None,
    ) -> bool:
        query = self.db.query(Engagement.id).filter(Engagement.id == int(engagement_id))
        if tenant_id is not None:
            query = query.filter(Engagement.tenant_id == int(tenant_id))
        if user_id is not None:
            query = query.filter(Engagement.user_id == int(user_id))
        return query.first() is not None

    def list_findings(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        severity: str | None,
        status: str | None,
        asset: str | None,
        source: str | None,
        query_text: str | None,
    ) -> list[KnowledgeFinding]:
        query = self.db.query(KnowledgeFinding).options(
            joinedload(KnowledgeFinding.asset),
            joinedload(KnowledgeFinding.service),
        )
        if tenant_id is not None:
            query = query.filter(KnowledgeFinding.tenant_id == int(tenant_id))
        query = query.filter(KnowledgeFinding.user_id == int(user_id))
        if severity:
            query = query.filter(func.lower(func.coalesce(KnowledgeFinding.severity, "")) == severity)
        if status:
            query = query.filter(func.lower(func.coalesce(KnowledgeFinding.status, "")) == status)
        if asset:
            asset_filter = asset.lower()
            query = query.filter(func.lower(func.coalesce(KnowledgeFinding.subject_key, "")).like(f"%{asset_filter}%"))
        if source:
            source_filter = source.lower()
            query = query.filter(
                func.lower(func.coalesce(KnowledgeFinding.finding_type, "")).like(f"%{source_filter}%")
            )
        if query_text:
            search = f"%{query_text.lower()}%"
            query = query.filter(
                or_(
                    func.lower(func.coalesce(KnowledgeFinding.title, "")).like(search),
                    func.lower(func.coalesce(KnowledgeFinding.subject_key, "")).like(search),
                    func.lower(func.coalesce(KnowledgeFinding.finding_key, "")).like(search),
                )
            )
        return query.all()

    def list_findings_for_engagement(
        self,
        *,
        user_id: int,
        tenant_id: int,
        engagement_id: int,
        severity: str | None,
        status: str | None,
        asset: str | None,
        source: str | None,
        query_text: str | None,
    ) -> list[KnowledgeFinding]:
        query = (
            self.db.query(KnowledgeFinding)
            .options(joinedload(KnowledgeFinding.asset), joinedload(KnowledgeFinding.service))
            .outerjoin(
                EngagementFindingLink,
                and_(
                    EngagementFindingLink.finding_id == KnowledgeFinding.id,
                    EngagementFindingLink.engagement_id == int(engagement_id),
                ),
            )
            .filter(
                KnowledgeFinding.tenant_id == int(tenant_id),
                KnowledgeFinding.user_id == int(user_id),
                or_(
                    EngagementFindingLink.id.isnot(None),
                    KnowledgeFinding.engagement_id == int(engagement_id),
                ),
            )
        )
        if severity:
            query = query.filter(func.lower(func.coalesce(KnowledgeFinding.severity, "")) == severity)
        if status:
            query = query.filter(func.lower(func.coalesce(KnowledgeFinding.status, "")) == status)
        if asset:
            asset_filter = asset.lower()
            query = query.filter(func.lower(func.coalesce(KnowledgeFinding.subject_key, "")).like(f"%{asset_filter}%"))
        if source:
            source_filter = source.lower()
            query = query.filter(
                func.lower(func.coalesce(KnowledgeFinding.finding_type, "")).like(f"%{source_filter}%")
            )
        if query_text:
            search = f"%{query_text.lower()}%"
            query = query.filter(
                or_(
                    func.lower(func.coalesce(KnowledgeFinding.title, "")).like(search),
                    func.lower(func.coalesce(KnowledgeFinding.subject_key, "")).like(search),
                    func.lower(func.coalesce(KnowledgeFinding.finding_key, "")).like(search),
                )
            )
        return query.all()

    def get_finding_with_links(
        self,
        *,
        user_id: int,
        finding_id: str,
        tenant_id: int | None = None,
    ) -> KnowledgeFinding | None:
        query = (
            self.db.query(KnowledgeFinding)
            .options(joinedload(KnowledgeFinding.asset), joinedload(KnowledgeFinding.service))
            .filter(KnowledgeFinding.id == str(finding_id))
        )
        if tenant_id is not None:
            query = query.filter(KnowledgeFinding.tenant_id == int(tenant_id))
        query = query.filter(KnowledgeFinding.user_id == int(user_id))
        return query.one_or_none()

    def get_finding_with_links_for_engagement(
        self,
        *,
        user_id: int,
        tenant_id: int,
        engagement_id: int,
        finding_id: str,
    ) -> KnowledgeFinding | None:
        return (
            self.db.query(KnowledgeFinding)
            .options(joinedload(KnowledgeFinding.asset), joinedload(KnowledgeFinding.service))
            .outerjoin(
                EngagementFindingLink,
                and_(
                    EngagementFindingLink.finding_id == KnowledgeFinding.id,
                    EngagementFindingLink.engagement_id == int(engagement_id),
                ),
            )
            .filter(
                KnowledgeFinding.tenant_id == int(tenant_id),
                KnowledgeFinding.user_id == int(user_id),
                or_(
                    EngagementFindingLink.id.isnot(None),
                    KnowledgeFinding.engagement_id == int(engagement_id),
                ),
                KnowledgeFinding.id == str(finding_id),
            )
            .one_or_none()
        )

    def evidence_by_archive_ids(
        self,
        *,
        user_id: int,
        tenant_id: int | None,
        engagement_id: int | None,
        evidence_ids: set[str],
    ) -> dict[str, KnowledgeEvidenceArchive]:
        """Return user-scoped evidence rows keyed by archive id."""
        normalized_ids = {str(item).strip() for item in evidence_ids if str(item).strip()}
        if not normalized_ids:
            return {}
        query = self.db.query(KnowledgeEvidenceArchive).filter(
            KnowledgeEvidenceArchive.user_id == int(user_id),
            cast(KnowledgeEvidenceArchive.id, String).in_(normalized_ids),
        )
        if tenant_id is not None:
            query = query.filter(KnowledgeEvidenceArchive.tenant_id == int(tenant_id))
        if engagement_id is not None:
            query = query.filter(KnowledgeEvidenceArchive.engagement_id == int(engagement_id))
        rows = query.all()
        return {str(row.id): row for row in rows}

    def list_assets(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        asset_type: str | None,
        query_text: str | None,
    ) -> list[KnowledgeAsset]:
        query = self.db.query(KnowledgeAsset)
        if tenant_id is not None:
            query = query.filter(KnowledgeAsset.tenant_id == int(tenant_id))
        query = query.filter(KnowledgeAsset.user_id == int(user_id))
        if asset_type:
            query = query.filter(func.lower(func.coalesce(KnowledgeAsset.asset_type, "")) == asset_type.lower())
        if query_text:
            search = f"%{query_text.lower()}%"
            query = query.filter(
                or_(
                    func.lower(func.coalesce(KnowledgeAsset.display_name, "")).like(search),
                    func.lower(func.coalesce(KnowledgeAsset.asset_key, "")).like(search),
                    func.lower(func.coalesce(KnowledgeAsset.ip_address, "")).like(search),
                    func.lower(func.coalesce(KnowledgeAsset.hostname, "")).like(search),
                )
            )
        return query.all()

    def list_assets_for_engagement(
        self,
        *,
        user_id: int,
        tenant_id: int,
        engagement_id: int,
        asset_type: str | None,
        query_text: str | None,
    ) -> list[KnowledgeAsset]:
        query = (
            self.db.query(KnowledgeAsset)
            .outerjoin(
                EngagementAssetLink,
                and_(
                    EngagementAssetLink.asset_id == KnowledgeAsset.id,
                    EngagementAssetLink.engagement_id == int(engagement_id),
                ),
            )
            .filter(
                KnowledgeAsset.tenant_id == int(tenant_id),
                KnowledgeAsset.user_id == int(user_id),
                or_(
                    EngagementAssetLink.id.isnot(None),
                    KnowledgeAsset.engagement_id == int(engagement_id),
                ),
            )
        )
        if asset_type:
            query = query.filter(func.lower(func.coalesce(KnowledgeAsset.asset_type, "")) == asset_type.lower())
        if query_text:
            search = f"%{query_text.lower()}%"
            query = query.filter(
                or_(
                    func.lower(func.coalesce(KnowledgeAsset.display_name, "")).like(search),
                    func.lower(func.coalesce(KnowledgeAsset.asset_key, "")).like(search),
                    func.lower(func.coalesce(KnowledgeAsset.ip_address, "")).like(search),
                    func.lower(func.coalesce(KnowledgeAsset.hostname, "")).like(search),
                )
            )
        return query.all()

    def get_asset_with_links(
        self,
        *,
        user_id: int,
        asset_id: str,
        tenant_id: int | None = None,
    ) -> KnowledgeAsset | None:
        query = (
            self.db.query(KnowledgeAsset)
            .options(joinedload(KnowledgeAsset.services), joinedload(KnowledgeAsset.findings))
            .filter(cast(KnowledgeAsset.id, String) == str(asset_id))
        )
        if tenant_id is not None:
            query = query.filter(KnowledgeAsset.tenant_id == int(tenant_id))
        query = query.filter(KnowledgeAsset.user_id == int(user_id))
        return query.one_or_none()

    def get_asset_with_links_for_engagement(
        self,
        *,
        user_id: int,
        tenant_id: int,
        engagement_id: int,
        asset_id: str,
    ) -> KnowledgeAsset | None:
        return (
            self.db.query(KnowledgeAsset)
            .options(joinedload(KnowledgeAsset.services), joinedload(KnowledgeAsset.findings))
            .outerjoin(
                EngagementAssetLink,
                and_(
                    EngagementAssetLink.asset_id == KnowledgeAsset.id,
                    EngagementAssetLink.engagement_id == int(engagement_id),
                ),
            )
            .filter(
                KnowledgeAsset.tenant_id == int(tenant_id),
                KnowledgeAsset.user_id == int(user_id),
                or_(
                    EngagementAssetLink.id.isnot(None),
                    KnowledgeAsset.engagement_id == int(engagement_id),
                ),
                cast(KnowledgeAsset.id, String) == str(asset_id),
            )
            .one_or_none()
        )

    def list_services_page(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[KnowledgeService], int]:
        query = self.db.query(KnowledgeService)
        if tenant_id is not None:
            query = query.filter(KnowledgeService.tenant_id == int(tenant_id))
        query = query.filter(KnowledgeService.user_id == int(user_id))
        total = int(query.order_by(None).count())
        rows = (
            query.order_by(KnowledgeService.last_seen_at.desc(), KnowledgeService.id.desc())
            .offset(int(offset))
            .limit(int(limit))
            .all()
        )
        return rows, total

    def list_services_page_for_engagement(
        self,
        *,
        user_id: int,
        tenant_id: int,
        engagement_id: int,
        limit: int,
        offset: int,
    ) -> tuple[list[KnowledgeService], int]:
        query = (
            self.db.query(KnowledgeService)
            .outerjoin(
                EngagementServiceLink,
                and_(
                    EngagementServiceLink.service_id == KnowledgeService.id,
                    EngagementServiceLink.engagement_id == int(engagement_id),
                ),
            )
            .filter(
                KnowledgeService.tenant_id == int(tenant_id),
                KnowledgeService.user_id == int(user_id),
                or_(
                    EngagementServiceLink.id.isnot(None),
                    KnowledgeService.engagement_id == int(engagement_id),
                ),
            )
        )
        total = int(query.order_by(None).count())
        rows = (
            query.order_by(KnowledgeService.last_seen_at.desc(), KnowledgeService.id.desc())
            .offset(int(offset))
            .limit(int(limit))
            .all()
        )
        return rows, total

    def list_evidence(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
    ) -> list[KnowledgeEvidenceArchive]:
        query = self.db.query(KnowledgeEvidenceArchive)
        if tenant_id is not None:
            query = query.filter(KnowledgeEvidenceArchive.tenant_id == int(tenant_id))
        query = query.filter(KnowledgeEvidenceArchive.user_id == int(user_id))
        return query.all()

    def list_evidence_for_engagement(
        self,
        *,
        user_id: int,
        tenant_id: int,
        engagement_id: int,
    ) -> list[KnowledgeEvidenceArchive]:
        return (
            self.db.query(KnowledgeEvidenceArchive)
            .filter(
                KnowledgeEvidenceArchive.tenant_id == int(tenant_id),
                KnowledgeEvidenceArchive.user_id == int(user_id),
                KnowledgeEvidenceArchive.engagement_id == int(engagement_id),
            )
            .all()
        )

    def list_findings_for_user(self, *, user_id: int) -> list[KnowledgeFinding]:
        return self.list_findings_for_tenant_or_user(user_id=user_id, tenant_id=None)

    def list_findings_for_tenant_or_user(
        self,
        *,
        user_id: int,
        tenant_id: int | None,
    ) -> list[KnowledgeFinding]:
        query = self.db.query(KnowledgeFinding)
        if tenant_id is not None:
            query = query.filter(KnowledgeFinding.tenant_id == int(tenant_id))
        query = query.filter(KnowledgeFinding.user_id == int(user_id))
        return query.all()

    def list_findings_for_engagement_scope(
        self,
        *,
        user_id: int,
        tenant_id: int,
        engagement_id: int,
    ) -> list[KnowledgeFinding]:
        return self.list_findings_for_engagement(
            user_id=user_id,
            tenant_id=tenant_id,
            engagement_id=engagement_id,
            severity=None,
            status=None,
            asset=None,
            source=None,
            query_text=None,
        )

    def list_assets_for_user(self, *, user_id: int) -> list[KnowledgeAsset]:
        return self.list_assets_for_tenant_or_user(user_id=user_id, tenant_id=None)

    def list_assets_for_tenant_or_user(
        self,
        *,
        user_id: int,
        tenant_id: int | None,
    ) -> list[KnowledgeAsset]:
        query = self.db.query(KnowledgeAsset)
        if tenant_id is not None:
            query = query.filter(KnowledgeAsset.tenant_id == int(tenant_id))
        query = query.filter(KnowledgeAsset.user_id == int(user_id))
        return query.all()

    def list_assets_for_engagement_scope(
        self,
        *,
        user_id: int,
        tenant_id: int,
        engagement_id: int,
    ) -> list[KnowledgeAsset]:
        return self.list_assets_for_engagement(
            user_id=user_id,
            tenant_id=tenant_id,
            engagement_id=engagement_id,
            asset_type=None,
            query_text=None,
        )

    def list_graph_components(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
    ) -> tuple[list[KnowledgeAsset], list[KnowledgeService], list[KnowledgeFinding], list[KnowledgeRelationship]]:
        if tenant_id is not None:
            tid = int(tenant_id)
            uid = int(user_id)
            assets = self.db.query(KnowledgeAsset).filter(KnowledgeAsset.tenant_id == tid, KnowledgeAsset.user_id == uid).all()
            services = self.db.query(KnowledgeService).filter(KnowledgeService.tenant_id == tid, KnowledgeService.user_id == uid).all()
            findings = self.db.query(KnowledgeFinding).filter(KnowledgeFinding.tenant_id == tid, KnowledgeFinding.user_id == uid).all()
            relationships = self.db.query(KnowledgeRelationship).filter(KnowledgeRelationship.tenant_id == tid, KnowledgeRelationship.user_id == uid).all()
            return assets, services, findings, relationships

        uid = int(user_id)
        assets = self.db.query(KnowledgeAsset).filter(KnowledgeAsset.user_id == uid).all()
        services = self.db.query(KnowledgeService).filter(KnowledgeService.user_id == uid).all()
        findings = self.db.query(KnowledgeFinding).filter(KnowledgeFinding.user_id == uid).all()
        relationships = self.db.query(KnowledgeRelationship).filter(KnowledgeRelationship.user_id == uid).all()
        return assets, services, findings, relationships

    def list_graph_components_for_engagement(
        self,
        *,
        user_id: int,
        tenant_id: int,
        engagement_id: int,
    ) -> tuple[list[KnowledgeAsset], list[KnowledgeService], list[KnowledgeFinding], list[KnowledgeRelationship]]:
        assets = self.list_assets_for_engagement_scope(
            user_id=user_id,
            tenant_id=tenant_id,
            engagement_id=engagement_id,
        )
        services = self.list_services_for_engagement_scope(
            user_id=user_id,
            tenant_id=tenant_id,
            engagement_id=engagement_id,
        )
        findings = self.list_findings_for_engagement_scope(
            user_id=user_id,
            tenant_id=tenant_id,
            engagement_id=engagement_id,
        )
        relationships = self.list_relationships_for_engagement_scope(
            user_id=user_id,
            tenant_id=tenant_id,
            engagement_id=engagement_id,
            subject_keys={
                str(asset.asset_key)
                for asset in assets
                if str(asset.asset_key or "").strip()
            }
            | {
                str(service.service_key)
                for service in services
                if str(service.service_key or "").strip()
            }
            | {
                str(finding.finding_key)
                for finding in findings
                if str(finding.finding_key or "").strip()
            },
        )
        return assets, services, findings, relationships

    def counts_for_summary(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
    ) -> tuple[int, int, int]:
        if tenant_id is not None:
            tid = int(tenant_id)
            uid = int(user_id)
            service_count = int(self.db.query(KnowledgeService).filter(KnowledgeService.tenant_id == tid, KnowledgeService.user_id == uid).count())
            evidence_count = int(
                self.db.query(KnowledgeEvidenceArchive)
                .filter(KnowledgeEvidenceArchive.tenant_id == tid, KnowledgeEvidenceArchive.user_id == uid)
                .count()
            )
            relationship_count = int(
                self.db.query(KnowledgeRelationship)
                .filter(KnowledgeRelationship.tenant_id == tid, KnowledgeRelationship.user_id == uid)
                .count()
            )
            return service_count, evidence_count, relationship_count

        uid = int(user_id)
        service_count = int(self.db.query(KnowledgeService).filter(KnowledgeService.user_id == uid).count())
        evidence_count = int(self.db.query(KnowledgeEvidenceArchive).filter(KnowledgeEvidenceArchive.user_id == uid).count())
        relationship_count = int(self.db.query(KnowledgeRelationship).filter(KnowledgeRelationship.user_id == uid).count())
        return service_count, evidence_count, relationship_count

    def counts_for_engagement_summary(
        self,
        *,
        user_id: int,
        tenant_id: int,
        engagement_id: int,
    ) -> tuple[int, int, int]:
        service_count = len(
            self.list_services_for_engagement_scope(
                user_id=user_id,
                tenant_id=tenant_id,
                engagement_id=engagement_id,
            )
        )
        evidence_count = len(
            self.list_evidence_for_engagement(
                user_id=user_id,
                tenant_id=tenant_id,
                engagement_id=engagement_id,
            )
        )
        relationship_count = len(
            self.list_relationships_for_engagement_scope(
                user_id=user_id,
                tenant_id=tenant_id,
                engagement_id=engagement_id,
                subject_keys=set(),
            )
        )
        return service_count, evidence_count, relationship_count

    def findings_by_asset(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
    ) -> dict[str, list[KnowledgeFinding]]:
        rows = self.list_findings_for_tenant_or_user(user_id=user_id, tenant_id=tenant_id)
        grouped: dict[str, list[KnowledgeFinding]] = defaultdict(list)
        for finding in rows:
            if finding.asset_id is None:
                continue
            grouped[str(finding.asset_id)].append(finding)
        return dict(grouped)

    def findings_by_asset_for_engagement(
        self,
        *,
        user_id: int,
        tenant_id: int,
        engagement_id: int,
    ) -> dict[str, list[KnowledgeFinding]]:
        rows = self.list_findings_for_engagement_scope(
            user_id=user_id,
            tenant_id=tenant_id,
            engagement_id=engagement_id,
        )
        grouped: dict[str, list[KnowledgeFinding]] = defaultdict(list)
        for finding in rows:
            if finding.asset_id is None:
                continue
            grouped[str(finding.asset_id)].append(finding)
        return dict(grouped)

    def service_counts_by_asset(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
    ) -> dict[str, int]:
        query = self.db.query(KnowledgeService.asset_id, func.count(KnowledgeService.id)).filter(
            KnowledgeService.asset_id.isnot(None)
        )
        if tenant_id is not None:
            query = query.filter(KnowledgeService.tenant_id == int(tenant_id))
        query = query.filter(KnowledgeService.user_id == int(user_id))
        rows = query.group_by(KnowledgeService.asset_id).all()
        return {str(asset_id): int(count) for asset_id, count in rows if asset_id is not None}

    def service_counts_by_asset_for_engagement(
        self,
        *,
        user_id: int,
        tenant_id: int,
        engagement_id: int,
    ) -> dict[str, int]:
        rows = (
            self.db.query(KnowledgeService.asset_id, func.count(KnowledgeService.id))
            .outerjoin(
                EngagementServiceLink,
                and_(
                    EngagementServiceLink.service_id == KnowledgeService.id,
                    EngagementServiceLink.engagement_id == int(engagement_id),
                ),
            )
            .filter(
                KnowledgeService.tenant_id == int(tenant_id),
                KnowledgeService.user_id == int(user_id),
                or_(
                    EngagementServiceLink.id.isnot(None),
                    KnowledgeService.engagement_id == int(engagement_id),
                ),
                KnowledgeService.asset_id.isnot(None),
            )
            .group_by(KnowledgeService.asset_id)
            .all()
        )
        return {str(asset_id): int(count) for asset_id, count in rows if asset_id is not None}

    def list_services_for_engagement_scope(
        self,
        *,
        user_id: int,
        tenant_id: int,
        engagement_id: int,
    ) -> list[KnowledgeService]:
        rows, _total = self.list_services_page_for_engagement(
            user_id=user_id,
            tenant_id=tenant_id,
            engagement_id=engagement_id,
            limit=10_000,
            offset=0,
        )
        return rows

    def list_relationships_for_engagement_scope(
        self,
        *,
        user_id: int,
        tenant_id: int,
        engagement_id: int,
        subject_keys: set[str],
    ) -> list[KnowledgeRelationship]:
        query = self.db.query(KnowledgeRelationship).filter(
            KnowledgeRelationship.tenant_id == int(tenant_id),
            KnowledgeRelationship.user_id == int(user_id),
        )
        if subject_keys:
            query = query.filter(
                or_(
                    KnowledgeRelationship.engagement_id == int(engagement_id),
                    and_(
                        KnowledgeRelationship.source_subject_key.in_(subject_keys),
                        KnowledgeRelationship.target_subject_key.in_(subject_keys),
                    ),
                )
            )
        else:
            query = query.filter(KnowledgeRelationship.engagement_id == int(engagement_id))
        return query.all()

    def resolve_service_for_engagement(
        self,
        *,
        user_id: int,
        tenant_id: int,
        engagement_id: int,
        service_key: str,
    ) -> KnowledgeService | None:
        normalized_service_key = str(service_key or "").strip()
        if not normalized_service_key:
            return None
        return (
            self.db.query(KnowledgeService)
            .join(
                EngagementServiceLink,
                EngagementServiceLink.service_id == KnowledgeService.id,
            )
            .filter(
                KnowledgeService.service_key == normalized_service_key,
                EngagementServiceLink.engagement_id == int(engagement_id),
                KnowledgeService.tenant_id == int(tenant_id),
                KnowledgeService.user_id == int(user_id),
            )
            .one_or_none()
        )

    def list_web_surface_paths_for_service(
        self,
        *,
        user_id: int,
        tenant_id: int,
        engagement_id: int,
        service_id: str,
        origin_key: str | None,
        include_noisy: bool,
        limit: int,
        offset: int,
    ) -> tuple[list[KnowledgeWebPath], int, int]:
        base_query = (
            self.db.query(KnowledgeWebPath)
            .join(
                EngagementWebPathLink,
                EngagementWebPathLink.web_path_id == KnowledgeWebPath.id,
            )
            .filter(
                KnowledgeWebPath.service_id == str(service_id),
                EngagementWebPathLink.engagement_id == int(engagement_id),
                KnowledgeWebPath.tenant_id == int(tenant_id),
                KnowledgeWebPath.user_id == int(user_id),
            )
        )
        normalized_origin_key = self._normalize_origin_key_filter(origin_key)
        if origin_key is not None and normalized_origin_key is None:
            return [], 0, 0
        if normalized_origin_key:
            base_query = base_query.filter(KnowledgeWebPath.origin_key == normalized_origin_key)

        hidden_noisy = 0
        visible_query = base_query
        if not include_noisy:
            hidden_noisy = int(
                base_query.filter(
                    KnowledgeWebPath.noise_score >= float(WEB_SURFACE_NOISY_HIDE_THRESHOLD)
                ).count()
            )
            visible_query = visible_query.filter(
                KnowledgeWebPath.noise_score < float(WEB_SURFACE_NOISY_HIDE_THRESHOLD)
            )

        total = int(visible_query.order_by(None).count())
        rows = (
            visible_query.order_by(
                KnowledgeWebPath.noise_score.asc(),
                KnowledgeWebPath.last_seen_at.desc(),
                KnowledgeWebPath.canonical_url.asc(),
            )
            .offset(int(offset))
            .limit(int(limit))
            .all()
        )
        return rows, total, hidden_noisy

    def list_web_surface_origins_for_service(
        self,
        *,
        user_id: int,
        tenant_id: int,
        engagement_id: int,
        service_id: str,
    ) -> list[KnowledgeWebPath]:
        return (
            self.db.query(KnowledgeWebPath)
            .join(
                EngagementWebPathLink,
                EngagementWebPathLink.web_path_id == KnowledgeWebPath.id,
            )
            .filter(
                KnowledgeWebPath.service_id == str(service_id),
                EngagementWebPathLink.engagement_id == int(engagement_id),
                KnowledgeWebPath.tenant_id == int(tenant_id),
                KnowledgeWebPath.user_id == int(user_id),
            )
            .order_by(KnowledgeWebPath.origin_key.asc(), KnowledgeWebPath.canonical_url.asc())
            .all()
        )

    @staticmethod
    def _normalize_origin_key_filter(origin_key: str | None) -> str | None:
        normalized = str(origin_key or "").strip()
        if not normalized:
            return None
        resolved = build_web_origin_key(normalized)
        if not resolved:
            return None
        return resolved
