""" knowledge query orchestration engine.

This module composes contracts, SQL selectors, and payload mappers into the
public `KnowledgeQueryService` behavior while preserving response contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ....models import KnowledgeEvidenceArchive
from ..evidence_refs import normalize_canonical_evidence_refs
from .contracts import (
    AssetsFilters,
    DEFAULT_LIMIT,
    EngagementListFilters,
    EvidenceFilters,
    FindingsFilters,
    PaginatedResult,
    PaginationParams,
    WebSurfacePathsFilters,
    WEB_SURFACE_NOISY_HIDE_THRESHOLD,
)
from .mappers import (
    asset_sort_key,
    datetime_sort_rank,
    evidence_sort_key,
    extract_source_tool,
    fallback_graph_node,
    finding_affected_asset_count,
    finding_is_candidate,
    finding_is_exploited,
    finding_is_open,
    finding_sort_key,
    serialize_asset_list_item,
    serialize_asset_summary,
    serialize_datetime,
    serialize_engagement,
    serialize_evidence,
    serialize_finding_base,
    serialize_finding_list_item,
    serialize_service_summary,
    serialize_web_surface_origin_summary,
    serialize_web_surface_path_item,
)
from .selectors import KnowledgeQuerySelectors
from ..severity_policy import severity_bucket_template


class KnowledgeQueryEngine:
    """Query engine implementing service-level read behavior."""

    def __init__(self, db: Session, *, selectors: KnowledgeQuerySelectors | None = None) -> None:
        self.db = db
        self.selectors = selectors or KnowledgeQuerySelectors(db)

    def _is_engagement_in_scope(
        self,
        *,
        engagement_id: int,
        user_id: int | None,
        tenant_id: int | None,
    ) -> bool:
        return self.selectors.is_engagement_in_scope(
            engagement_id=int(engagement_id),
            user_id=int(user_id) if user_id is not None else None,
            tenant_id=int(tenant_id) if tenant_id is not None else None,
        )

    @staticmethod
    def _empty_page(*, limit: int, offset: int) -> dict[str, object]:
        return PaginatedResult.from_items(items=[], total=0, limit=limit, offset=offset).to_dict()

    @staticmethod
    def _empty_summary() -> dict[str, object]:
        return {
            "open_findings_total": 0,
            "open_findings_by_severity": severity_bucket_template(),
            "asset_counts": {"total": 0, "vulnerable": 0, "exploited": 0},
            "service_count": 0,
            "evidence_count": 0,
            "relationship_count": 0,
            "last_observed_at": None,
            "open_statuses": ["confirmed", "exploited", "in_progress", "open", "triaged"],
        }

    @staticmethod
    def _evidence_source_tool(row: KnowledgeEvidenceArchive) -> str | None:
        """Return source tool from durable evidence lineage or metadata."""
        metadata = dict(row.archive_metadata or {})
        lineage = dict(row.lineage_snapshot or {})
        return extract_source_tool(lineage=lineage, metadata=metadata)

    def _attach_missing_finding_sources(
        self,
        *,
        items: list[dict[str, Any]],
        user_id: int,
        tenant_id: int | None,
        engagement_id: int | None,
    ) -> None:
        """Promote source tool from linked evidence for findings missing it."""
        missing_source_items = [item for item in items if not item.get("source_tool")]
        if not missing_source_items:
            return

        evidence_ids: set[str] = set()
        for item in missing_source_items:
            for ref in item.get("evidence_refs") or []:
                if not isinstance(ref, dict):
                    continue
                evidence_id = str(ref.get("evidence_archive_id") or "").strip()
                if evidence_id:
                    evidence_ids.add(evidence_id)
        evidence_by_id = self.selectors.evidence_by_archive_ids(
            user_id=int(user_id),
            tenant_id=int(tenant_id) if tenant_id is not None else None,
            engagement_id=int(engagement_id) if engagement_id is not None else None,
            evidence_ids=evidence_ids,
        )
        if not evidence_by_id:
            return

        for item in missing_source_items:
            for ref in item.get("evidence_refs") or []:
                if not isinstance(ref, dict):
                    continue
                evidence_id = str(ref.get("evidence_archive_id") or "").strip()
                evidence_row = evidence_by_id.get(evidence_id)
                if evidence_row is None:
                    continue
                source_tool = self._evidence_source_tool(evidence_row)
                if source_tool:
                    item["source_tool"] = source_tool
                    break

    def list_engagements(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        filters: EngagementListFilters | None = None,
    ) -> dict[str, object]:
        normalized = (filters or EngagementListFilters()).normalized()
        rows, total = self.selectors.list_engagements(
            user_id=int(user_id),
            tenant_id=int(tenant_id) if tenant_id is not None else None,
            query_text=normalized.query,
            status=normalized.status,
            limit=int(normalized.limit),
            offset=int(normalized.offset),
        )
        items = [serialize_engagement(row) for row in rows]
        return PaginatedResult.from_items(
            items=items,
            total=total,
            limit=int(normalized.limit),
            offset=int(normalized.offset),
        ).to_dict()

    def get_engagement(
        self,
        *,
        engagement_id: int,
        tenant_id: int | None = None,
        user_id: int | None = None,
    ) -> dict[str, object] | None:
        if not self._is_engagement_in_scope(
            engagement_id=engagement_id,
            user_id=user_id,
            tenant_id=tenant_id,
        ):
            return None
        row = self.selectors.get_engagement(
            engagement_id=int(engagement_id),
            tenant_id=int(tenant_id) if tenant_id is not None else None,
            user_id=int(user_id) if user_id is not None else None,
        )
        if row is None:
            return None
        return serialize_engagement(row)

    def get_summary(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        engagement_id: int | None = None,
    ) -> dict[str, object]:
        if tenant_id is not None and engagement_id is not None:
            finding_rows = self.selectors.list_findings_for_engagement_scope(
                user_id=int(user_id),
                tenant_id=int(tenant_id),
                engagement_id=int(engagement_id),
            )
            asset_rows = self.selectors.list_assets_for_engagement_scope(
                user_id=int(user_id),
                tenant_id=int(tenant_id),
                engagement_id=int(engagement_id),
            )
            service_count, evidence_count, relationship_count = self.selectors.counts_for_engagement_summary(
                user_id=int(user_id),
                tenant_id=int(tenant_id),
                engagement_id=int(engagement_id),
            )
        elif tenant_id is not None:
            finding_rows = self.selectors.list_findings_for_tenant_or_user(
                user_id=int(user_id),
                tenant_id=int(tenant_id),
            )
            asset_rows = self.selectors.list_assets_for_tenant_or_user(
                user_id=int(user_id),
                tenant_id=int(tenant_id),
            )
            service_count, evidence_count, relationship_count = self.selectors.counts_for_summary(
                user_id=int(user_id),
                tenant_id=int(tenant_id),
            )
        else:
            finding_rows = self.selectors.list_findings_for_user(user_id=int(user_id))
            asset_rows = self.selectors.list_assets_for_user(user_id=int(user_id))
            service_count, evidence_count, relationship_count = self.selectors.counts_for_summary(
                user_id=int(user_id)
            )

        open_statuses = {"open", "confirmed", "exploited", "triaged", "in_progress"}
        severity_buckets = severity_bucket_template()
        open_findings_total = 0
        vulnerable_asset_ids: set[str] = set()
        exploited_asset_ids: set[str] = set()
        last_observed: datetime | None = None

        for finding in finding_rows:
            if finding_is_candidate(finding=finding):
                continue
            severity = str(finding.severity or "").strip().lower()
            if severity in severity_buckets and finding_is_open(finding=finding):
                severity_buckets[severity] += 1

            if finding_is_open(finding=finding):
                open_findings_total += 1
                if finding.asset_id is not None:
                    vulnerable_asset_ids.add(str(finding.asset_id))
            if finding_is_exploited(finding=finding) and finding.asset_id is not None:
                exploited_asset_ids.add(str(finding.asset_id))

            if finding.last_seen_at is not None and (
                last_observed is None or finding.last_seen_at > last_observed
            ):
                last_observed = finding.last_seen_at

        for asset in asset_rows:
            if asset.last_seen_at is not None and (
                last_observed is None or asset.last_seen_at > last_observed
            ):
                last_observed = asset.last_seen_at

        return {
            "open_findings_total": open_findings_total,
            "open_findings_by_severity": severity_buckets,
            "asset_counts": {
                "total": len(asset_rows),
                "vulnerable": len(vulnerable_asset_ids),
                "exploited": len(exploited_asset_ids),
            },
            "service_count": service_count,
            "evidence_count": evidence_count,
            "relationship_count": relationship_count,
            "last_observed_at": serialize_datetime(last_observed),
            "open_statuses": sorted(open_statuses),
        }

    def list_findings(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        engagement_id: int | None = None,
        filters: FindingsFilters | None = None,
    ) -> dict[str, object]:
        normalized = (filters or FindingsFilters()).normalized()
        if tenant_id is not None and engagement_id is not None:
            rows = self.selectors.list_findings_for_engagement(
                user_id=int(user_id),
                tenant_id=int(tenant_id),
                engagement_id=int(engagement_id),
                severity=normalized.severity,
                status=normalized.status,
                asset=normalized.asset,
                source=normalized.source,
                query_text=normalized.query,
            )
        elif tenant_id is not None:
            rows = self.selectors.list_findings(
                user_id=int(user_id),
                tenant_id=int(tenant_id),
                severity=normalized.severity,
                status=normalized.status,
                asset=normalized.asset,
                source=normalized.source,
                query_text=normalized.query,
            )
        else:
            rows = self.selectors.list_findings(
                user_id=int(user_id),
                severity=normalized.severity,
                status=normalized.status,
                asset=normalized.asset,
                source=normalized.source,
                query_text=normalized.query,
            )
        if not bool(normalized.include_candidates):
            rows = [row for row in rows if not finding_is_candidate(finding=row)]
        if normalized.exploited is not None:
            rows = [row for row in rows if finding_is_exploited(finding=row) is normalized.exploited]

        rows = sorted(rows, key=finding_sort_key(str(normalized.sort)))
        total = len(rows)
        paged = rows[int(normalized.offset) : int(normalized.offset) + int(normalized.limit)]
        items = [serialize_finding_list_item(row) for row in paged]
        self._attach_missing_finding_sources(
            items=items,
            user_id=int(user_id),
            tenant_id=int(tenant_id) if tenant_id is not None else None,
            engagement_id=int(engagement_id) if engagement_id is not None else None,
        )
        return PaginatedResult.from_items(
            items=items,
            total=total,
            limit=int(normalized.limit),
            offset=int(normalized.offset),
        ).to_dict()

    def get_finding(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        engagement_id: int | None = None,
        finding_id: str,
    ) -> dict[str, object] | None:
        if tenant_id is not None and engagement_id is not None:
            row = self.selectors.get_finding_with_links_for_engagement(
                user_id=int(user_id),
                tenant_id=int(tenant_id),
                engagement_id=int(engagement_id),
                finding_id=str(finding_id),
            )
        else:
            row = self.selectors.get_finding_with_links(
                user_id=int(user_id),
                finding_id=str(finding_id),
                tenant_id=int(tenant_id) if tenant_id is not None else None,
            )
        if row is None:
            return None

        evidence_summary = dict(row.evidence_summary or {})
        metadata = dict(row.finding_metadata or {})
        metadata_state = metadata.get("state")
        metadata_refs = metadata.get("evidence_refs")
        if not isinstance(metadata_refs, list) and isinstance(metadata_state, dict):
            metadata_refs = metadata_state.get("evidence_refs")

        evidence_refs = normalize_canonical_evidence_refs(
            list(evidence_summary.get("evidence_refs") or []) + list(metadata_refs or []),
            strict=False,
        )
        evidence_summary["evidence_refs"] = evidence_refs

        item = {
            **serialize_finding_base(row),
            "affected_asset_count": finding_affected_asset_count(row),
            "evidence_count": len(evidence_refs),
            "asset": serialize_asset_summary(row.asset),
            "service": serialize_service_summary(row.service),
            "evidence_refs": evidence_refs,
            "evidence_summary": evidence_summary,
            "metadata": metadata,
        }
        self._attach_missing_finding_sources(
            items=[item],
            user_id=int(user_id),
            tenant_id=int(tenant_id) if tenant_id is not None else None,
            engagement_id=int(engagement_id) if engagement_id is not None else None,
        )
        return item

    def list_assets(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        engagement_id: int | None = None,
        filters: AssetsFilters | None = None,
    ) -> dict[str, object]:
        normalized = (filters or AssetsFilters()).normalized()
        if tenant_id is not None and engagement_id is not None:
            rows = self.selectors.list_assets_for_engagement(
                user_id=int(user_id),
                tenant_id=int(tenant_id),
                engagement_id=int(engagement_id),
                asset_type=normalized.type,
                query_text=normalized.query,
            )
            findings_by_asset = self.selectors.findings_by_asset_for_engagement(
                user_id=int(user_id),
                tenant_id=int(tenant_id),
                engagement_id=int(engagement_id),
            )
            service_counts_by_asset = self.selectors.service_counts_by_asset_for_engagement(
                user_id=int(user_id),
                tenant_id=int(tenant_id),
                engagement_id=int(engagement_id),
            )
        elif tenant_id is not None:
            rows = self.selectors.list_assets(
                user_id=int(user_id),
                tenant_id=int(tenant_id),
                asset_type=normalized.type,
                query_text=normalized.query,
            )
            findings_by_asset = self.selectors.findings_by_asset(
                user_id=int(user_id),
                tenant_id=int(tenant_id),
            )
            service_counts_by_asset = self.selectors.service_counts_by_asset(
                user_id=int(user_id),
                tenant_id=int(tenant_id),
            )
        else:
            rows = self.selectors.list_assets(
                user_id=int(user_id),
                asset_type=normalized.type,
                query_text=normalized.query,
            )
            findings_by_asset = self.selectors.findings_by_asset(user_id=int(user_id))
            service_counts_by_asset = self.selectors.service_counts_by_asset(user_id=int(user_id))

        filtered_rows = []
        for row in rows:
            finding_rows = [
                item for item in findings_by_asset.get(str(row.id), []) if not finding_is_candidate(finding=item)
            ]
            vulnerable = any(finding_is_open(finding=finding) for finding in finding_rows)
            exploited = any(finding_is_exploited(finding=finding) for finding in finding_rows)
            if normalized.vulnerable is not None and vulnerable is not normalized.vulnerable:
                continue
            if normalized.exploited is not None and exploited is not normalized.exploited:
                continue
            filtered_rows.append(row)

        filtered_rows = sorted(filtered_rows, key=asset_sort_key(str(normalized.sort)))
        total = len(filtered_rows)
        paged = filtered_rows[int(normalized.offset) : int(normalized.offset) + int(normalized.limit)]
        items = [
            serialize_asset_list_item(
                asset=row,
                finding_rows=[
                    item
                    for item in findings_by_asset.get(str(row.id), [])
                    if not finding_is_candidate(finding=item)
                ],
                service_count=int(service_counts_by_asset.get(str(row.id), 0)),
            )
            for row in paged
        ]
        return PaginatedResult.from_items(
            items=items,
            total=total,
            limit=int(normalized.limit),
            offset=int(normalized.offset),
        ).to_dict()

    def get_asset(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        engagement_id: int | None = None,
        asset_id: str,
    ) -> dict[str, object] | None:
        if tenant_id is not None and engagement_id is not None:
            row = self.selectors.get_asset_with_links_for_engagement(
                user_id=int(user_id),
                tenant_id=int(tenant_id),
                engagement_id=int(engagement_id),
                asset_id=str(asset_id),
            )
        else:
            row = self.selectors.get_asset_with_links(
                user_id=int(user_id),
                asset_id=str(asset_id),
                tenant_id=int(tenant_id) if tenant_id is not None else None,
            )
        if row is None:
            return None

        linked_services = sorted(
            [serialize_service_summary(item) for item in list(row.services or []) if item is not None],
            key=lambda item: ((item.get("port") is None), int(item.get("port") or 0), str(item.get("id") or "")),
        )
        linked_findings = sorted(
            [
                serialize_finding_list_item(item)
                for item in list(row.findings or [])
                if item is not None and not finding_is_candidate(finding=item)
            ],
            key=lambda item: (str(item.get("last_seen_at") or ""), str(item.get("id") or "")),
            reverse=True,
        )
        return {
            "id": str(row.id),
            "asset_key": row.asset_key,
            "asset_type": row.asset_type,
            "display_name": row.display_name,
            "ip_address": row.ip_address,
            "hostname": row.hostname,
            "status": row.status,
            "first_seen_at": serialize_datetime(row.first_seen_at),
            "last_seen_at": serialize_datetime(row.last_seen_at),
            "max_confidence": row.max_confidence,
            "metadata": dict(row.asset_metadata or {}),
            "services": linked_services,
            "findings": linked_findings,
            "service_count": len(linked_services),
            "finding_count": len(linked_findings),
            "is_vulnerable": any(
                finding_is_open(finding=item)
                for item in list(row.findings or [])
                if item is not None and not finding_is_candidate(finding=item)
            ),
            "is_exploited": any(
                finding_is_exploited(finding=item)
                for item in list(row.findings or [])
                if item is not None and not finding_is_candidate(finding=item)
            ),
        }

    def list_services(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        engagement_id: int | None = None,
        limit: int | str | None = DEFAULT_LIMIT,
        offset: int | str | None = 0,
    ) -> dict[str, object]:
        pagination = PaginationParams(limit=limit, offset=offset).normalized()
        if tenant_id is not None and engagement_id is not None:
            rows, total = self.selectors.list_services_page_for_engagement(
                user_id=int(user_id),
                tenant_id=int(tenant_id),
                engagement_id=int(engagement_id),
                limit=int(pagination.limit),
                offset=int(pagination.offset),
            )
        else:
            rows, total = self.selectors.list_services_page(
                user_id=int(user_id),
                tenant_id=int(tenant_id) if tenant_id is not None else None,
                limit=int(pagination.limit),
                offset=int(pagination.offset),
            )
        items = [
            {
                "id": str(row.id),
                "service_key": row.service_key,
                "asset_id": str(row.asset_id) if row.asset_id is not None else None,
                "protocol": row.protocol,
                "port": row.port,
                "service_name": row.service_name,
                "product": row.product,
                "version": row.version,
                "status": row.status,
                "first_seen_at": serialize_datetime(row.first_seen_at),
                "last_seen_at": serialize_datetime(row.last_seen_at),
                "metadata": dict(row.service_metadata or {}),
            }
            for row in rows
        ]
        return PaginatedResult.from_items(
            items=items,
            total=total,
            limit=int(pagination.limit),
            offset=int(pagination.offset),
        ).to_dict()

    def list_evidence(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        engagement_id: int | None = None,
        filters: EvidenceFilters | None = None,
    ) -> dict[str, object]:
        normalized = (filters or EvidenceFilters()).normalized()
        if tenant_id is not None and engagement_id is not None:
            rows = self.selectors.list_evidence_for_engagement(
                user_id=int(user_id),
                tenant_id=int(tenant_id),
                engagement_id=int(engagement_id),
            )
        else:
            rows = self.selectors.list_evidence(
                user_id=int(user_id),
                tenant_id=int(tenant_id) if tenant_id is not None else None,
            )

        canonical_rows_by_execution: dict[str, KnowledgeEvidenceArchive] = {}
        grouped_by_execution: dict[str, list[KnowledgeEvidenceArchive]] = {}
        for row in rows:
            execution_id = str(row.source_execution_id or "").strip()
            if not execution_id:
                continue
            grouped_by_execution.setdefault(execution_id, []).append(row)
        for execution_id, execution_rows in grouped_by_execution.items():
            canonical_rows_by_execution[execution_id] = self._select_canonical_evidence_row(
                rows=execution_rows
            )

        filtered_rows: list[KnowledgeEvidenceArchive] = []
        for row in canonical_rows_by_execution.values():
            metadata = dict(row.archive_metadata or {})
            lineage = dict(row.lineage_snapshot or {})
            source_tool = str(extract_source_tool(lineage=lineage, metadata=metadata) or "").strip()
            evidence_type = str(
                metadata.get("evidence_type")
                or metadata.get("type")
                or lineage.get("artifact_kind")
                or ""
            ).strip()
            query_blob = " ".join(
                item
                for item in [
                    str(row.id),
                    str(row.storage_mode or ""),
                    str(row.inline_excerpt or ""),
                    source_tool,
                    evidence_type,
                ]
                if item
            ).lower()
            if normalized.source_tool and source_tool.lower() != normalized.source_tool.lower():
                continue
            if normalized.type and evidence_type.lower() != normalized.type.lower():
                continue
            if normalized.query and normalized.query.lower() not in query_blob:
                continue
            filtered_rows.append(row)

        filtered_rows = sorted(filtered_rows, key=evidence_sort_key(str(normalized.sort)))
        total = len(filtered_rows)
        paged = filtered_rows[int(normalized.offset) : int(normalized.offset) + int(normalized.limit)]
        items = [
            self._serialize_canonical_evidence_with_execution_group(
                row=row,
                grouped_by_execution=grouped_by_execution,
            )
            for row in paged
        ]
        return PaginatedResult.from_items(
            items=items,
            total=total,
            limit=int(normalized.limit),
            offset=int(normalized.offset),
        ).to_dict()

    def list_service_web_surface_origins(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        engagement_id: int,
        service_key: str,
        include_noisy: bool = False,
    ) -> dict[str, object]:
        if tenant_id is None:
            return {
                "service_key": str(service_key or "").strip(),
                "items": [],
            }
        service_row = self.selectors.resolve_service_for_engagement(
            user_id=int(user_id),
            tenant_id=int(tenant_id),
            engagement_id=int(engagement_id),
            service_key=service_key,
        )
        if service_row is None:
            return {
                "service_key": str(service_key or "").strip(),
                "items": [],
            }

        web_path_rows = self.selectors.list_web_surface_origins_for_service(
            user_id=int(user_id),
            tenant_id=int(tenant_id),
            engagement_id=int(engagement_id),
            service_id=str(service_row.id),
        )
        grouped: dict[str, list] = {}
        for row in web_path_rows:
            grouped.setdefault(str(row.origin_key or ""), []).append(row)

        summaries: list[dict[str, object]] = []
        for origin_key in sorted(grouped):
            rows = grouped[origin_key]
            total_paths = len(rows)
            visible_paths = len(
                [
                    row
                    for row in rows
                    if include_noisy
                    or float(row.noise_score or 0.0) < float(WEB_SURFACE_NOISY_HIDE_THRESHOLD)
                ]
            )
            hidden_noisy = total_paths - visible_paths
            calibrated_warnings = len([row for row in rows if bool(row.calibrated_baseline)])
            producers = sorted(
                {
                    str(source).strip()
                    for row in rows
                    for source in dict(row.producer_summary or {}).keys()
                    if str(source).strip()
                }
            )
            first_seen_at = min((row.first_seen_at for row in rows if row.first_seen_at is not None), default=None)
            last_seen_at = max((row.last_seen_at for row in rows if row.last_seen_at is not None), default=None)
            summaries.append(
                serialize_web_surface_origin_summary(
                    origin_key=origin_key,
                    total_paths=total_paths,
                    visible_paths=visible_paths,
                    hidden_noisy=hidden_noisy,
                    calibrated_warnings=calibrated_warnings,
                    producers=producers,
                    first_seen_at=first_seen_at,
                    last_seen_at=last_seen_at,
                )
            )

        summaries = sorted(
            summaries,
            key=lambda item: (
                str(item.get("last_seen_at") or ""),
                str(item.get("origin_key") or ""),
            ),
            reverse=True,
        )
        return {
            "service_key": str(service_row.service_key or ""),
            "items": summaries,
        }

    def list_service_web_surface_paths(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        engagement_id: int,
        filters: WebSurfacePathsFilters | None = None,
    ) -> dict[str, object]:
        normalized = (filters or WebSurfacePathsFilters()).normalized()
        if tenant_id is None:
            return {
                "service_key": normalized.service_key,
                "origin_key": normalized.origin_key,
                "items": [],
                "total": 0,
                "limit": int(normalized.limit),
                "offset": int(normalized.offset),
                "hidden_noisy": 0,
            }
        if not normalized.service_key:
            return {
                "service_key": None,
                "origin_key": normalized.origin_key,
                "items": [],
                "total": 0,
                "limit": int(normalized.limit),
                "offset": int(normalized.offset),
                "hidden_noisy": 0,
            }

        service_row = self.selectors.resolve_service_for_engagement(
            user_id=int(user_id),
            tenant_id=int(tenant_id),
            engagement_id=int(engagement_id),
            service_key=str(normalized.service_key),
        )
        if service_row is None:
            return {
                "service_key": str(normalized.service_key),
                "origin_key": normalized.origin_key,
                "items": [],
                "total": 0,
                "limit": int(normalized.limit),
                "offset": int(normalized.offset),
                "hidden_noisy": 0,
            }

        rows, total, hidden_noisy = self.selectors.list_web_surface_paths_for_service(
            user_id=int(user_id),
            tenant_id=int(tenant_id),
            engagement_id=int(engagement_id),
            service_id=str(service_row.id),
            origin_key=normalized.origin_key,
            include_noisy=bool(normalized.include_noisy),
            limit=int(normalized.limit),
            offset=int(normalized.offset),
        )
        items = [serialize_web_surface_path_item(row) for row in rows]
        return {
            "service_key": str(service_row.service_key or ""),
            "origin_key": normalized.origin_key,
            "items": items,
            "total": int(total),
            "limit": int(normalized.limit),
            "offset": int(normalized.offset),
            "hidden_noisy": int(hidden_noisy),
        }

    @staticmethod
    def _select_canonical_evidence_row(
        *,
        rows: list[KnowledgeEvidenceArchive],
    ) -> KnowledgeEvidenceArchive:
        """Return one canonical evidence row for a single source execution."""
        if len(rows) == 1:
            return rows[0]

        def _earliest_row(candidates: list[KnowledgeEvidenceArchive]) -> KnowledgeEvidenceArchive:
            return min(
                candidates,
                key=lambda item: (
                    datetime_sort_rank(item.created_at),
                    str(item.id),
                ),
            )

        stdout_rows = [row for row in rows if KnowledgeQueryEngine._artifact_kind(row) == "stdout"]
        if stdout_rows:
            return _earliest_row(stdout_rows)

        command_rows = [row for row in rows if KnowledgeQueryEngine._artifact_kind(row) == "command"]
        if command_rows:
            return _earliest_row(command_rows)

        non_mirror_rows = [
            row
            for row in rows
            if not KnowledgeQueryEngine._is_langgraph_stdout_mirror_tool_file(row)
        ]
        if non_mirror_rows:
            return _earliest_row(non_mirror_rows)

        return _earliest_row(rows)

    @staticmethod
    def _serialize_canonical_evidence_with_execution_group(
        *,
        row: KnowledgeEvidenceArchive,
        grouped_by_execution: dict[str, list[KnowledgeEvidenceArchive]],
    ) -> dict[str, object]:
        """Serialize canonical evidence row and include expandable execution group members."""
        payload = serialize_evidence(row)
        execution_id = str(payload.get("source_execution_id") or "").strip()
        members = list(grouped_by_execution.get(execution_id, []))
        has_stdout_member = any(
            KnowledgeQueryEngine._artifact_kind(member) == "stdout"
            for member in members
        )
        if has_stdout_member:
            members = [
                member
                for member in members
                if not KnowledgeQueryEngine._is_langgraph_stdout_mirror_tool_file(member)
            ]

        sorted_members = sorted(
            members,
            key=lambda item: (
                datetime_sort_rank(item.created_at),
                str(item.id),
            ),
        )
        member_payloads = []
        for member in sorted_members:
            serialized = serialize_evidence(member)
            member_payloads.append(
                {
                    "id": serialized.get("id"),
                    "evidence_type": serialized.get("evidence_type"),
                    "source_tool": serialized.get("source_tool"),
                    "created_at": serialized.get("created_at"),
                    "storage_mode": serialized.get("storage_mode"),
                    "is_canonical": str(serialized.get("id") or "") == str(payload.get("id") or ""),
                }
            )

        metadata = dict(payload.get("metadata") or {})
        metadata["execution_group"] = {
            "execution_id": execution_id,
            "canonical_evidence_id": str(payload.get("id") or ""),
            "member_count": len(member_payloads),
            "members": member_payloads,
        }
        payload["metadata"] = metadata
        return payload

    @staticmethod
    def _artifact_kind(row: KnowledgeEvidenceArchive) -> str:
        metadata = dict(row.archive_metadata or {})
        lineage = dict(row.lineage_snapshot or {})
        return str(
            lineage.get("artifact_kind")
            or metadata.get("evidence_type")
            or metadata.get("type")
            or ""
        ).strip().lower()

    @staticmethod
    def _is_langgraph_stdout_mirror_tool_file(row: KnowledgeEvidenceArchive) -> bool:
        lineage = dict(row.lineage_snapshot or {})
        relative_path = str(lineage.get("relative_path") or "").strip().lower().replace("\\", "/")
        return KnowledgeQueryEngine._artifact_kind(row) == "tool_file" and relative_path.endswith("_tool.txt")

    @staticmethod
    def _synthesize_fk_edges(
        *,
        assets: list,
        services: list,
        findings: list,
        relationships: list,
    ) -> list[dict[str, object]]:
        """Derive graph edges from FK ownership links (service.asset_id, finding.asset_id/service_id).

        Deduplicates against explicit KnowledgeRelationship rows so the caller
        never gets double edges for the same source/type/target triplet.
        """
        asset_id_to_key: dict[str, str] = {str(a.id): a.asset_key for a in assets}
        service_id_to_key: dict[str, str] = {str(s.id): s.service_key for s in services}

        existing_triplets: set[tuple[str, str, str]] = {
            (
                str(r.source_subject_key or "").strip(),
                str(r.relationship_type or "").strip(),
                str(r.target_subject_key or "").strip(),
            )
            for r in relationships
        }

        synthetic: list[dict[str, object]] = []

        for svc in services:
            if not svc.asset_id:
                continue
            asset_key = asset_id_to_key.get(str(svc.asset_id))
            if not asset_key:
                continue
            if (asset_key, "exposes", svc.service_key) in existing_triplets:
                continue
            synthetic.append({
                "id": f"synth:exposes:{asset_key}:{svc.service_key}",
                "source": asset_key,
                "target": svc.service_key,
                "relationship_type": "exposes",
                "confidence": None,
                "first_seen_at": serialize_datetime(svc.first_seen_at),
                "last_seen_at": serialize_datetime(svc.last_seen_at),
                "metadata": {"synthetic": True, "source": "fk_projection"},
            })

        for finding in findings:
            if finding.asset_id:
                asset_key = asset_id_to_key.get(str(finding.asset_id))
                if asset_key and (asset_key, "has_finding", finding.finding_key) not in existing_triplets:
                    synthetic.append({
                        "id": f"synth:has_finding:{asset_key}:{finding.finding_key}",
                        "source": asset_key,
                        "target": finding.finding_key,
                        "relationship_type": "has_finding",
                        "confidence": finding.confidence,
                        "first_seen_at": serialize_datetime(finding.first_seen_at),
                        "last_seen_at": serialize_datetime(finding.last_seen_at),
                        "metadata": {"synthetic": True, "source": "fk_projection"},
                    })
            if finding.service_id:
                service_key = service_id_to_key.get(str(finding.service_id))
                if service_key and (service_key, "has_finding", finding.finding_key) not in existing_triplets:
                    synthetic.append({
                        "id": f"synth:has_finding:{service_key}:{finding.finding_key}",
                        "source": service_key,
                        "target": finding.finding_key,
                        "relationship_type": "has_finding",
                        "confidence": finding.confidence,
                        "first_seen_at": serialize_datetime(finding.first_seen_at),
                        "last_seen_at": serialize_datetime(finding.last_seen_at),
                        "metadata": {"synthetic": True, "source": "fk_projection"},
                    })

        return synthetic

    def get_graph_snapshot(
        self,
        *,
        user_id: int,
        tenant_id: int | None = None,
        engagement_id: int | None = None,
    ) -> dict[str, object]:
        if tenant_id is not None and engagement_id is not None:
            assets, services, findings, relationships = self.selectors.list_graph_components_for_engagement(
                user_id=int(user_id),
                tenant_id=int(tenant_id),
                engagement_id=int(engagement_id),
            )
        else:
            assets, services, findings, relationships = self.selectors.list_graph_components(
                user_id=int(user_id),
                tenant_id=int(tenant_id) if tenant_id is not None else None,
            )

        asset_id_to_key: dict[str, str] = {str(a.id): a.asset_key for a in assets}

        node_map: dict[str, dict[str, object]] = {}
        for asset in assets:
            node_map[asset.asset_key] = {
                "id": asset.asset_key,
                "subject_key": asset.asset_key,
                "node_type": "asset",
                "label": asset.display_name or asset.hostname or asset.ip_address or asset.asset_key,
                "metadata": dict(asset.asset_metadata or {}),
            }
        for service in services:
            meta = dict(service.service_metadata or {})
            meta["service_key"] = service.service_key
            if service.service_name:
                meta["service_name"] = service.service_name
            if service.protocol:
                meta["transport_protocol"] = service.protocol
            if service.port is not None:
                meta["port"] = service.port
            if service.asset_id:
                linked_asset_key = asset_id_to_key.get(str(service.asset_id))
                if linked_asset_key:
                    meta["asset_key"] = linked_asset_key
            node_map[service.service_key] = {
                "id": service.service_key,
                "subject_key": service.service_key,
                "node_type": "service",
                "label": service.service_name or service.service_key,
                "metadata": meta,
            }
        for finding in findings:
            node_map[finding.finding_key] = {
                "id": finding.finding_key,
                "subject_key": finding.finding_key,
                "node_type": "finding",
                "label": finding.title or finding.finding_key,
                "metadata": dict(finding.finding_metadata or {}),
            }

        edges: list[dict[str, object]] = []
        for row in relationships:
            source_key = str(row.source_subject_key or "").strip()
            target_key = str(row.target_subject_key or "").strip()
            if source_key and source_key not in node_map:
                node_map[source_key] = fallback_graph_node(source_key)
            if target_key and target_key not in node_map:
                node_map[target_key] = fallback_graph_node(target_key)
            edges.append(
                {
                    "id": str(row.id),
                    "source": source_key,
                    "target": target_key,
                    "relationship_type": row.relationship_type,
                    "confidence": row.confidence,
                    "first_seen_at": serialize_datetime(row.first_seen_at),
                    "last_seen_at": serialize_datetime(row.last_seen_at),
                    "metadata": dict(row.relationship_metadata or {}),
                }
            )

        edges.extend(self._synthesize_fk_edges(
            assets=assets,
            services=services,
            findings=findings,
            relationships=relationships,
        ))

        nodes = [node_map[key] for key in sorted(node_map.keys())]
        edges = sorted(
            edges,
            key=lambda edge: (
                str(edge.get("source") or ""),
                str(edge.get("relationship_type") or ""),
                str(edge.get("target") or ""),
                str(edge.get("id") or ""),
            ),
        )
        return {
            "nodes": nodes,
            "edges": edges,
        }
