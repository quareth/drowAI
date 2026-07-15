"""Read-only deterministic product-version CVE lookup service.

Scope:
- Matches one concrete `product + version` against projected CVE affected rows.
- Returns ranked, explainable candidates with conservative applicability semantics.

Boundary:
- Does not write findings or mutate CVE data.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from sqlalchemy.orm import Session

from backend.models.cve import CveAffectedProduct, CveRecord
from backend.services.metrics.utils import safe_inc
from backend.services.cve_indexing.cvss import extract_cvss_score
from backend.services.cve_indexing.match_contracts import CveLookupMatch, CveLookupRequest, CveLookupResponse
from backend.services.cve_indexing.product_resolver import MatchQuality, ProductResolver, ResolvedProjectionCandidate
from backend.services.cve_indexing.version_eval import evaluate_version_applicability

_MATCH_QUALITY_LABEL: dict[str, str] = {
    "exact": "product exact match",
    "cpe": "product CPE match",
    "token": "product token match",
}

ProjectionCandidateLoader = Callable[..., tuple[ResolvedProjectionCandidate | CveAffectedProduct, ...]]
RecordLoader = Callable[..., dict[str, CveRecord]]


class CveMatchService:
    """Evaluate applicability and rank CVE candidates for product-version requests."""

    def __init__(
        self,
        db: Session | None = None,
        *,
        projection_candidate_loader: ProjectionCandidateLoader | None = None,
        record_loader: RecordLoader | None = None,
    ) -> None:
        self._db = db
        self._projection_candidate_loader = projection_candidate_loader or self._load_projection_candidates_from_db
        self._record_loader = record_loader or self._load_records_by_cve_id_from_db

    def lookup(self, request: CveLookupRequest) -> CveLookupResponse:
        """Return ranked, explainable CVE lookup candidates for one product-version request."""
        safe_inc("cve.lookup.requests_total")

        candidates = self._normalize_candidates(self._projection_candidate_loader(product=request.product))
        if not candidates:
            return CveLookupResponse(matches=(), message="no_cve_match_candidates")

        cve_ids = {str(item.row.cve_id).strip() for item in candidates if str(item.row.cve_id).strip()}
        records_by_cve_id = self._record_loader(cve_ids=cve_ids)

        matches: list[tuple[tuple[int, int, int, float, str], CveLookupMatch]] = []
        for candidate in candidates:
            row = candidate.row
            evaluation = self._evaluate_projection_row(
                version=request.version, row=row, match_quality=candidate.match_quality,
            )
            if evaluation is None:
                continue
            match, rank_key = self._build_lookup_match(
                row=row,
                records_by_cve_id=records_by_cve_id,
                applicability_status=evaluation["status"],
                rationale=evaluation["rationale"],
                match_quality=candidate.match_quality,
            )
            if match is not None:
                matches.append((rank_key, match))

        ordered = tuple(item[1] for item in sorted(matches, key=lambda pair: pair[0])[: int(request.max_results)])
        self._emit_match_metrics(ordered)
        return CveLookupResponse(
            matches=ordered,
            message="ok" if ordered else "no_cve_matches_after_applicability",
        )

    @staticmethod
    def _emit_match_metrics(matches: tuple[CveLookupMatch, ...]) -> None:
        safe_inc("cve.lookup.matches_returned_total", len(matches))
        applicable_count = sum(1 for item in matches if item.version_applicable is True)
        possible_count = sum(1 for item in matches if item.version_applicable is None)
        safe_inc("cve.lookup.applicable_total", applicable_count)
        safe_inc("cve.lookup.possible_total", possible_count)

    def _load_projection_candidates_from_db(
        self,
        *,
        product: str,
    ) -> tuple[ResolvedProjectionCandidate, ...]:
        if self._db is None:
            return ()
        return ProductResolver(self._db).resolve(product=product)

    def _load_records_by_cve_id_from_db(self, *, cve_ids: Iterable[str]) -> dict[str, CveRecord]:
        normalized_ids = tuple(str(item).strip() for item in cve_ids if str(item).strip())
        if self._db is None or not normalized_ids:
            return {}
        rows = self._db.query(CveRecord).filter(CveRecord.cve_id.in_(normalized_ids)).all()
        return {str(row.cve_id).strip(): row for row in rows if str(row.cve_id).strip()}

    def _evaluate_projection_row(
        self,
        *,
        version: str,
        row: CveAffectedProduct,
        match_quality: MatchQuality = "exact",
    ) -> dict[str, str] | None:
        applicability = evaluate_version_applicability(
            fingerprint_version=version,
            versions_json=self._to_versions(row.versions_json),
            default_status=self._clean_text(row.default_status),
        )
        if applicability.status == "no_match":
            return None

        rationale = self._to_rationale(
            applicability_status=applicability.status,
            applicability_explanation=applicability.explanation,
            requested_version=version,
            versions_json=self._to_versions(row.versions_json),
            match_quality=match_quality,
        )
        return {"status": applicability.status, "rationale": rationale}

    def _build_lookup_match(
        self,
        *,
        row: CveAffectedProduct,
        records_by_cve_id: dict[str, CveRecord],
        applicability_status: str,
        rationale: str,
        match_quality: MatchQuality,
    ) -> tuple[CveLookupMatch | None, tuple[int, int, int, float, str]]:
        cve_id = self._clean_text(row.cve_id)
        if cve_id is None:
            return None, (9, 9, 9, 0.0, "zzzz")

        record = records_by_cve_id.get(cve_id)
        summary = self._clean_text(getattr(record, "title", None)) or self._clean_text(
            getattr(record, "description", None)
        )
        score = self._conservative_score(
            applicability_status=applicability_status,
            rationale=rationale,
            match_quality=match_quality,
        )
        severity_rank = self._severity_rank(getattr(record, "severity", None))
        cvss_score = self._record_cvss_score(record)
        match = CveLookupMatch(
            cve_id=cve_id,
            rationale=rationale,
            summary=summary,
            version_applicable=True if applicability_status == "applicable" else None,
            matched_fields=("product_norm", f"product_{match_quality}_match"),
            score=score,
        )
        rank_key = (
            self._applicability_rank(applicability_status),
            self._match_quality_rank(match_quality),
            self._version_basis_rank(rationale),
            -(float(severity_rank) + float(cvss_score) / 10.0),
            cve_id,
        )
        return match, rank_key

    @staticmethod
    def _version_basis_rank(rationale: str) -> int:
        normalized = str(rationale or "").strip().lower()
        if "version exact match" in normalized:
            return 0
        if "version range match" in normalized:
            return 1
        if "version affected" in normalized:
            return 2
        if "version missing" in normalized:
            return 3
        if "version evidence unavailable" in normalized:
            return 4
        if "unsupported" in normalized:
            return 5
        return 6

    @staticmethod
    def _to_versions(value: Any) -> list[dict[str, Any]] | None:
        if not isinstance(value, list):
            return None
        rows: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                rows.append(dict(item))
        return rows or None

    @staticmethod
    def _to_rationale(
        *,
        applicability_status: str,
        applicability_explanation: str,
        requested_version: str,
        versions_json: list[dict[str, Any]] | None,
        match_quality: MatchQuality = "exact",
    ) -> str:
        product_label = _MATCH_QUALITY_LABEL.get(match_quality, "product match")
        normalized_requested_version = str(requested_version or "").strip()
        if applicability_status == "applicable":
            if normalized_requested_version and versions_json:
                for entry in versions_json:
                    if not isinstance(entry, dict):
                        continue
                    entry_version = str(entry.get("version", "")).strip()
                    if not entry_version:
                        continue
                    if entry_version != normalized_requested_version:
                        continue
                    if entry.get("lessThan") is None and entry.get("lessThanOrEqual") is None:
                        return f"{product_label}; version exact match"
                    return f"{product_label}; version range match"
            return f"{product_label}; version affected"

        explanation = str(applicability_explanation).strip().lower()
        if "version missing" in explanation:
            return f"{product_label}; version missing"
        if "unsupported" in explanation:
            return f"{product_label}; version rule unsupported by MVP evaluator"
        if "evidence unavailable" in explanation:
            return f"{product_label}; version evidence unavailable"
        return f"{product_label}; version evaluation conservative"

    @staticmethod
    def _conservative_score(*, applicability_status: str, rationale: str, match_quality: MatchQuality) -> float:
        if applicability_status == "applicable":
            if match_quality == "exact":
                base = 0.95
            elif match_quality == "cpe":
                base = 0.92
            else:
                base = 0.9
        else:
            if "missing" in rationale:
                quality_base = 0.7
            elif "unsupported" in rationale:
                quality_base = 0.66
            else:
                quality_base = 0.62
            if match_quality == "exact":
                base = quality_base
            elif match_quality == "cpe":
                base = quality_base - 0.04
            else:
                base = quality_base - 0.08
        return max(0.0, min(1.0, round(base, 4)))

    @staticmethod
    def _severity_rank(value: Any) -> int:
        normalized = str(value or "").strip().lower()
        if normalized == "critical":
            return 4
        if normalized == "high":
            return 3
        if normalized == "medium":
            return 2
        if normalized == "low":
            return 1
        return 0

    @staticmethod
    def _record_cvss_score(record: CveRecord | None) -> float:
        if record is None:
            return 0.0
        score = extract_cvss_score(getattr(record, "metrics", None))
        if score is None:
            return 0.0
        return float(score)

    @staticmethod
    def _applicability_rank(status: str) -> int:
        if status == "applicable":
            return 0
        if status == "possible":
            return 1
        return 2

    @staticmethod
    def _match_quality_rank(match_quality: MatchQuality) -> int:
        if match_quality == "exact":
            return 0
        if match_quality == "cpe":
            return 1
        return 2

    @staticmethod
    def _clean_text(value: Any) -> str | None:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    @staticmethod
    def _normalize_candidates(
        candidates: tuple[ResolvedProjectionCandidate | CveAffectedProduct, ...],
    ) -> tuple[ResolvedProjectionCandidate, ...]:
        rows: list[ResolvedProjectionCandidate] = []
        for item in tuple(candidates):
            if isinstance(item, ResolvedProjectionCandidate):
                rows.append(item)
                continue
            if isinstance(item, CveAffectedProduct):
                rows.append(ResolvedProjectionCandidate(row=item, match_quality="exact"))
        return tuple(rows)


__all__ = ["CveMatchService"]
