"""Project durable canonical web-path rows from raw web observations.

This projector enforces `(tenant_id, user_id, canonical_url)` identity for upserts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from ....models import KnowledgeAsset, KnowledgeService, KnowledgeWebPath
from runtime_shared.semantic.service_identity import build_service_socket_key
from ..adapters.web_common import build_web_origin_key
from ..contracts import ObservationCreate
from ..evidence_refs import normalize_canonical_evidence_refs

"""Noise weights tuned at projection-time.

Calibrated paths are more likely to be noisy, while corroboration and
interesting statuses (3xx/401/403) reduce noise.
"""
NOISE_WEIGHT_CALIBRATED = 0.6
NOISE_WEIGHT_ISOLATED = 0.2
NOISE_WEIGHT_CORROBORATED = -0.4
NOISE_WEIGHT_INTERESTING_STATUS = -0.2

_MAX_EVIDENCE_REFS = 20
_MAX_TRACKED_RUN_IDS_PER_PRODUCER = 5


@dataclass(frozen=True)
class WebPathProjectionCounts:
    """Aggregate counts for one projector pass."""

    upsert_count: int = 0
    insert_count: int = 0


@dataclass(frozen=True)
class _ProjectedObservation:
    canonical_url: str
    origin_key: str
    path: str
    source: str
    status_code: int | None
    response_size: int | None
    calibrated: bool
    evidence_refs: tuple[dict[str, Any], ...]
    ingestion_run_id: str
    observed_at: datetime


class WebPathProjector:
    """Project canonical web paths from `web.path_discovered` observations."""

    def upsert_from_observations(
        self,
        *,
        db: Session,
        user_id: int,
        engagement_id: int | None,
        observations: Sequence[ObservationCreate],
        asset_key_to_id: Mapping[str, str],
        service_key_to_id: Mapping[str, str],
        tenant_id: int,
    ) -> WebPathProjectionCounts:
        _ = engagement_id
        grouped: dict[str, list[_ProjectedObservation]] = {}
        for observation in observations:
            projected = self._to_projected_observation(observation)
            if projected is None:
                continue
            grouped.setdefault(projected.canonical_url, []).append(projected)

        upsert_count = 0
        insert_count = 0
        for canonical_url in sorted(grouped):
            parsed_group = sorted(
                grouped[canonical_url],
                key=lambda item: (self._normalize_datetime(item.observed_at), item.source),
            )
            inserted = self._upsert_group(
                db=db,
                user_id=int(user_id),
                tenant_id=tenant_id,
                group=parsed_group,
                asset_key_to_id=asset_key_to_id,
                service_key_to_id=service_key_to_id,
            )
            upsert_count += 1
            if inserted:
                insert_count += 1
        return WebPathProjectionCounts(
            upsert_count=upsert_count,
            insert_count=insert_count,
        )

    def _upsert_group(
        self,
        *,
        db: Session,
        user_id: int,
        tenant_id: int,
        group: Sequence[_ProjectedObservation],
        asset_key_to_id: Mapping[str, str],
        service_key_to_id: Mapping[str, str],
    ) -> bool:
        representative = group[0]
        existing = self._resolve_existing_row(
            db=db,
            tenant_id=tenant_id,
            user_id=user_id,
            canonical_url=representative.canonical_url,
        )
        inserted = existing is None
        row = existing or KnowledgeWebPath(
            tenant_id=tenant_id,
            user_id=int(user_id),
            canonical_url=representative.canonical_url,
            origin_key=representative.origin_key,
            path=representative.path,
            first_seen_at=self._normalize_datetime(representative.observed_at),
            last_seen_at=self._normalize_datetime(representative.observed_at),
            producer_summary={},
            evidence_refs=[],
            calibrated_baseline=False,
            noise_score=0.0,
        )

        if inserted:
            db.add(row)

        row.tenant_id = int(tenant_id)
        row.origin_key = representative.origin_key
        row.path = representative.path
        row.first_seen_at = min(
            self._normalize_datetime(row.first_seen_at),
            min(self._normalize_datetime(item.observed_at) for item in group),
        )
        row.last_seen_at = max(
            self._normalize_datetime(row.last_seen_at),
            max(self._normalize_datetime(item.observed_at) for item in group),
        )

        row.producer_summary = self._merge_producer_summary(
            existing_summary=row.producer_summary,
            projected=group,
        )
        row.evidence_refs = self._merge_evidence_refs(
            existing_refs=row.evidence_refs,
            projected=group,
        )
        row.last_status_code = self._resolve_last_status_code(
            current_value=row.last_status_code,
            projected=group,
        )
        row.last_response_size = self._resolve_last_response_size(
            current_value=row.last_response_size,
            projected=group,
        )
        row.calibrated_baseline = bool(row.calibrated_baseline) or any(item.calibrated for item in group)

        resolved_service_id = self._resolve_service_id(
            db=db,
            tenant_id=tenant_id,
            user_id=user_id,
            origin_key=representative.origin_key,
            service_key_to_id=service_key_to_id,
        )
        row.service_id = resolved_service_id or row.service_id

        resolved_asset_id = self._resolve_asset_id(
            db=db,
            tenant_id=tenant_id,
            user_id=user_id,
            origin_key=representative.origin_key,
            asset_key_to_id=asset_key_to_id,
            service_id=row.service_id,
        )
        row.asset_id = resolved_asset_id or row.asset_id
        row.noise_score = self._compute_noise_score(
            calibrated_baseline=bool(row.calibrated_baseline),
            producer_summary=row.producer_summary,
            status_code=row.last_status_code,
        )
        db.flush()
        return inserted

    @staticmethod
    def _resolve_existing_row(
        *,
        db: Session,
        tenant_id: int,
        user_id: int,
        canonical_url: str,
    ) -> KnowledgeWebPath | None:
        return db.execute(
            select(KnowledgeWebPath).where(
                KnowledgeWebPath.tenant_id == int(tenant_id),
                KnowledgeWebPath.user_id == int(user_id),
                KnowledgeWebPath.canonical_url == str(canonical_url),
            )
        ).scalar_one_or_none()

    @staticmethod
    def _to_projected_observation(observation: ObservationCreate) -> _ProjectedObservation | None:
        if str(observation.observation_type or "").strip().lower() != "web.path_discovered":
            return None
        if str(observation.subject_type or "").strip().lower() != "web.path":
            return None
        subject_key = str(observation.subject_key or "").strip().lower()
        if not subject_key.startswith("web.path:"):
            return None

        canonical_url = subject_key.removeprefix("web.path:")
        parsed_url = urlsplit(canonical_url)
        if not parsed_url.scheme or not parsed_url.netloc:
            return None
        origin_key = build_web_origin_key(canonical_url)
        if not origin_key:
            return None

        payload = dict(observation.payload or {})
        source = str(payload.get("source") or "unknown").strip().lower() or "unknown"
        path = parsed_url.path or "/"
        status_code = WebPathProjector._coerce_int(payload.get("status_code"))
        response_size = WebPathProjector._coerce_int(payload.get("response_size"))
        calibrated = bool(payload.get("calibrated"))
        evidence_refs = WebPathProjector._normalize_evidence_refs(
            payload.get("evidence_refs"),
        )
        observed_at = WebPathProjector._normalize_datetime(observation.observed_at)
        return _ProjectedObservation(
            canonical_url=canonical_url,
            origin_key=origin_key,
            path=path,
            source=source,
            status_code=status_code,
            response_size=response_size,
            calibrated=calibrated,
            evidence_refs=evidence_refs,
            ingestion_run_id=str(observation.ingestion_run_id or "").strip(),
            observed_at=observed_at,
        )

    @staticmethod
    def _normalize_evidence_refs(value: Any) -> tuple[dict[str, Any], ...]:
        return tuple(normalize_canonical_evidence_refs(value, strict=False))

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return None

    @staticmethod
    def _merge_producer_summary(
        *,
        existing_summary: Any,
        projected: Sequence[_ProjectedObservation],
    ) -> dict[str, dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        if isinstance(existing_summary, Mapping):
            for source, summary in existing_summary.items():
                source_key = str(source or "").strip().lower()
                if not source_key or not isinstance(summary, Mapping):
                    continue
                seen_count = WebPathProjector._coerce_int(summary.get("seen_count")) or 0
                last_seen_at = WebPathProjector._normalize_timestamp_text(summary.get("last_seen_at"))
                run_ids_raw = summary.get("run_ids")
                run_ids = (
                    [str(item).strip() for item in run_ids_raw if str(item).strip()]
                    if isinstance(run_ids_raw, list)
                    else []
                )
                merged[source_key] = {
                    "seen_count": seen_count,
                    "last_seen_at": last_seen_at,
                    "run_ids": run_ids[-_MAX_TRACKED_RUN_IDS_PER_PRODUCER:],
                }

        for item in projected:
            entry = merged.setdefault(
                item.source,
                {"seen_count": 0, "last_seen_at": None, "run_ids": []},
            )
            entry["seen_count"] = int(entry.get("seen_count") or 0) + 1
            last_seen_at = WebPathProjector._normalize_timestamp_text(entry.get("last_seen_at"))
            candidate_seen_at = item.observed_at.isoformat()
            if not last_seen_at or candidate_seen_at > last_seen_at:
                entry["last_seen_at"] = candidate_seen_at
            run_ids = entry.get("run_ids")
            if not isinstance(run_ids, list):
                run_ids = []
            if item.ingestion_run_id and item.ingestion_run_id not in run_ids:
                run_ids.append(item.ingestion_run_id)
            entry["run_ids"] = run_ids[-_MAX_TRACKED_RUN_IDS_PER_PRODUCER:]

        return {
            source: {
                "seen_count": int(summary["seen_count"]),
                "last_seen_at": summary["last_seen_at"],
                "run_ids": list(summary["run_ids"]),
            }
            for source, summary in sorted(merged.items())
        }

    @staticmethod
    def _merge_evidence_refs(
        *,
        existing_refs: Any,
        projected: Sequence[_ProjectedObservation],
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for item in existing_refs if isinstance(existing_refs, list) else []:
            merged.extend(normalize_canonical_evidence_refs([item], strict=False))

        for projected_item in projected:
            if not projected_item.evidence_refs:
                continue
            for ref in projected_item.evidence_refs:
                if ref not in merged:
                    merged.append(dict(ref))

        return normalize_canonical_evidence_refs(merged[-_MAX_EVIDENCE_REFS:], strict=False)

    @staticmethod
    def _resolve_last_status_code(
        *,
        current_value: int | None,
        projected: Sequence[_ProjectedObservation],
    ) -> int | None:
        best: tuple[int, datetime] | None = None
        for item in projected:
            if item.status_code is None:
                continue
            marker = (int(item.status_code), item.observed_at)
            if best is None or marker[1] > best[1]:
                best = marker
        if best is not None:
            return best[0]
        return current_value

    @staticmethod
    def _resolve_last_response_size(
        *,
        current_value: int | None,
        projected: Sequence[_ProjectedObservation],
    ) -> int | None:
        best: tuple[int, datetime] | None = None
        for item in projected:
            if item.response_size is None:
                continue
            marker = (int(item.response_size), item.observed_at)
            if best is None or marker[1] > best[1]:
                best = marker
        if best is not None:
            return best[0]
        return current_value

    def _resolve_service_id(
        self,
        *,
        db: Session,
        tenant_id: int,
        user_id: int,
        origin_key: str,
        service_key_to_id: Mapping[str, str],
    ) -> str | None:
        candidates = self._service_key_candidates_from_origin(origin_key)
        for candidate in candidates:
            cached = service_key_to_id.get(candidate)
            if cached:
                return str(cached)
        if not candidates:
            return None
        service_row = db.execute(
            select(KnowledgeService.id).where(
                KnowledgeService.tenant_id == int(tenant_id),
                KnowledgeService.user_id == int(user_id),
                KnowledgeService.service_key.in_(candidates),
            )
        ).first()
        if service_row is None:
            return None
        return str(service_row[0])

    def _resolve_asset_id(
        self,
        *,
        db: Session,
        tenant_id: int,
        user_id: int,
        origin_key: str,
        asset_key_to_id: Mapping[str, str],
        service_id: str | None,
    ) -> str | None:
        if service_id:
            service_asset_id = db.execute(
                select(KnowledgeService.asset_id).where(
                    KnowledgeService.tenant_id == int(tenant_id),
                    KnowledgeService.user_id == int(user_id),
                    KnowledgeService.id == str(service_id),
                )
            ).scalar_one_or_none()
            if service_asset_id is not None:
                return str(service_asset_id)

        for candidate in self._asset_key_candidates_from_origin(origin_key):
            cached = asset_key_to_id.get(candidate)
            if cached:
                return str(cached)
            asset_id = db.execute(
                select(KnowledgeAsset.id).where(
                    KnowledgeAsset.tenant_id == int(tenant_id),
                    KnowledgeAsset.user_id == int(user_id),
                    KnowledgeAsset.asset_key == candidate,
                )
            ).scalar_one_or_none()
            if asset_id is not None:
                return str(asset_id)
        return None

    @staticmethod
    def _service_key_candidates_from_origin(origin_key: str) -> tuple[str, ...]:
        parsed = urlsplit(origin_key)
        host = str(parsed.hostname or "").strip().lower()
        if not host:
            return ()
        port = parsed.port
        if port is None:
            if parsed.scheme == "https":
                port = 443
            elif parsed.scheme == "http":
                port = 80
        if port is None:
            return ()
        candidates: list[str] = []
        for protocol in ("tcp", "udp"):
            try:
                candidates.append(build_service_socket_key(ip=host, protocol=protocol, port=port))
            except ValueError:
                continue
        return tuple(candidates)

    @staticmethod
    def _asset_key_candidates_from_origin(origin_key: str) -> tuple[str, ...]:
        host = str(urlsplit(origin_key).hostname or "").strip().lower()
        if not host:
            return ()
        if host.replace(".", "").isdigit():
            return (f"host.ip:{host}",)
        return (f"host.dns:{host}", f"host.ip:{host}")

    @staticmethod
    def _compute_noise_score(
        *,
        calibrated_baseline: bool,
        producer_summary: Mapping[str, Any] | None,
        status_code: int | None,
    ) -> float:
        summary = producer_summary if isinstance(producer_summary, Mapping) else {}
        producer_count = len([item for item in summary.values() if isinstance(item, Mapping)])

        run_ids: set[str] = set()
        for item in summary.values():
            if not isinstance(item, Mapping):
                continue
            ids = item.get("run_ids")
            if not isinstance(ids, list):
                continue
            for run_id in ids:
                run_text = str(run_id or "").strip()
                if run_text:
                    run_ids.add(run_text)
        only_one_run = len(run_ids) <= 1 and bool(run_ids)
        only_one_producer = producer_count == 1

        score = 0.0
        if calibrated_baseline:
            score += NOISE_WEIGHT_CALIBRATED
        if only_one_producer and only_one_run:
            score += NOISE_WEIGHT_ISOLATED
        if producer_count >= 2:
            score += NOISE_WEIGHT_CORROBORATED
        if WebPathProjector._is_interesting_status(status_code):
            score += NOISE_WEIGHT_INTERESTING_STATUS
        return max(0.0, min(1.0, score))

    @staticmethod
    def _is_interesting_status(status_code: int | None) -> bool:
        if status_code is None:
            return False
        if 300 <= int(status_code) < 400:
            return True
        return int(status_code) in {401, 403}

    @staticmethod
    def _normalize_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    @staticmethod
    def _normalize_timestamp_text(value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None
