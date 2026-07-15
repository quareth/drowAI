"""Knowledge query service facade.

Delegates query implementation to focused internals under
`backend/services/knowledge/query/`."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .query import (
    AssetSort,
    AssetsFilters,
    DEFAULT_LIMIT,
    EngagementListFilters,
    EvidenceFilters,
    EvidenceSort,
    FindingsFilters,
    FindingSort,
    KnowledgeQueryEngine,
    MAX_LIMIT,
    PaginatedResult,
    PaginationParams,
    WebSurfacePathsFilters,
    normalize_optional_bool,
)


class KnowledgeQueryService:
    """Read/query service facade over durable knowledge models."""

    def __init__(self, db: Session) -> None:
        self._engine = KnowledgeQueryEngine(db)

    def list_engagements(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        filters: EngagementListFilters | None = None,
    ) -> dict[str, object]:
        return self._engine.list_engagements(
            user_id=user_id,
            tenant_id=tenant_id,
            filters=filters,
        )

    def get_engagement(
        self,
        *,
        engagement_id: int,
        tenant_id: int | None = None,
        user_id: int | None = None,
    ) -> dict[str, object] | None:
        return self._engine.get_engagement(
            engagement_id=engagement_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )

    def get_summary(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        engagement_id: int | None = None,
    ) -> dict[str, object]:
        return self._engine.get_summary(
            user_id=user_id,
            tenant_id=tenant_id,
            engagement_id=engagement_id,
        )

    def list_findings(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        engagement_id: int | None = None,
        filters: FindingsFilters | None = None,
    ) -> dict[str, object]:
        return self._engine.list_findings(
            user_id=user_id,
            tenant_id=tenant_id,
            engagement_id=engagement_id,
            filters=filters,
        )

    def get_finding(
        self,
        *,
        user_id: int,
        finding_id: str,
        tenant_id: int | None = None,
        engagement_id: int | None = None,
    ) -> dict[str, object] | None:
        return self._engine.get_finding(
            user_id=user_id,
            tenant_id=tenant_id,
            engagement_id=engagement_id,
            finding_id=finding_id,
        )

    def list_assets(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        engagement_id: int | None = None,
        filters: AssetsFilters | None = None,
    ) -> dict[str, object]:
        return self._engine.list_assets(
            user_id=user_id,
            tenant_id=tenant_id,
            engagement_id=engagement_id,
            filters=filters,
        )

    def get_asset(
        self,
        *,
        user_id: int,
        asset_id: str,
        tenant_id: int | None = None,
        engagement_id: int | None = None,
    ) -> dict[str, object] | None:
        return self._engine.get_asset(
            user_id=user_id,
            tenant_id=tenant_id,
            engagement_id=engagement_id,
            asset_id=asset_id,
        )

    def list_services(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        engagement_id: int | None = None,
        limit: int | str | None = DEFAULT_LIMIT,
        offset: int | str | None = 0,
    ) -> dict[str, object]:
        return self._engine.list_services(
            user_id=user_id,
            tenant_id=tenant_id,
            engagement_id=engagement_id,
            limit=limit,
            offset=offset,
        )

    def list_evidence(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        engagement_id: int | None = None,
        filters: EvidenceFilters | None = None,
    ) -> dict[str, object]:
        return self._engine.list_evidence(
            user_id=user_id,
            tenant_id=tenant_id,
            engagement_id=engagement_id,
            filters=filters,
        )

    def get_graph_snapshot(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        engagement_id: int | None = None,
    ) -> dict[str, object]:
        return self._engine.get_graph_snapshot(
            user_id=user_id,
            tenant_id=tenant_id,
            engagement_id=engagement_id,
        )

    def list_service_web_surface_origins(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        engagement_id: int,
        service_key: str,
        include_noisy: bool = False,
    ) -> dict[str, object]:
        return self._engine.list_service_web_surface_origins(
            user_id=user_id,
            tenant_id=tenant_id,
            engagement_id=engagement_id,
            service_key=service_key,
            include_noisy=include_noisy,
        )

    def list_service_web_surface_paths(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        engagement_id: int,
        filters: WebSurfacePathsFilters | None = None,
    ) -> dict[str, object]:
        return self._engine.list_service_web_surface_paths(
            user_id=user_id,
            tenant_id=tenant_id,
            engagement_id=engagement_id,
            filters=filters,
        )


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "FindingSort",
    "AssetSort",
    "EvidenceSort",
    "PaginationParams",
    "PaginatedResult",
    "EngagementListFilters",
    "FindingsFilters",
    "AssetsFilters",
    "EvidenceFilters",
    "WebSurfacePathsFilters",
    "KnowledgeQueryService",
    "normalize_optional_bool",
]
