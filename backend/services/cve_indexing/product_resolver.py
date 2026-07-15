"""Product candidate resolution for CVE lookup matching.

Scope:
- Resolves candidate CVE projection rows for one product query.
- Uses exact product, CPE token, and product token strategies with deduping.

Boundary:
- Contains no version applicability logic and no lookup scoring/ranking.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Literal

from sqlalchemy import Text, and_, cast
from sqlalchemy.orm import Session

from backend.models.cve import CveAffectedProduct
from backend.services.cve_indexing.cpe import extract_cpe_identity_tokens

_NORMALIZED_NAME_MAX_LEN = 255
_TOKEN_RE = re.compile(r"[a-z0-9]+")
MatchQuality = Literal["exact", "cpe", "token"]
_QUALITY_RANK: dict[MatchQuality, int] = {"exact": 0, "cpe": 1, "token": 2}


@dataclass(slots=True, frozen=True)
class ResolvedProjectionCandidate:
    """Projection row with product match quality metadata."""

    row: CveAffectedProduct
    match_quality: MatchQuality


class ProductResolver:
    """Load projection candidates using multiple identity signals."""

    def __init__(self, db: Session | None = None) -> None:
        self._db = db

    def resolve(self, *, product: str) -> tuple[ResolvedProjectionCandidate, ...]:
        if self._db is None:
            return ()
        product_norm = _normalize_name(product)
        if product_norm is None:
            return ()
        tokens = _tokenize(product_norm)
        exact_rows = self._query_exact(product_norm=product_norm)
        cpe_rows = self._query_cpe_tokens(tokens=tokens)
        token_rows = self._query_product_tokens(tokens=tokens)
        return self._dedupe(
            exact_rows=exact_rows,
            cpe_rows=cpe_rows,
            token_rows=token_rows,
        )

    def _query_exact(self, *, product_norm: str) -> tuple[CveAffectedProduct, ...]:
        rows = (
            self._db.query(CveAffectedProduct)
            .filter(CveAffectedProduct.product_norm == product_norm)
            .all()
        )
        return tuple(rows)

    def _query_cpe_tokens(self, *, tokens: tuple[str, ...]) -> tuple[CveAffectedProduct, ...]:
        if not tokens:
            return ()
        conditions = [cast(CveAffectedProduct.cpes_json, Text).ilike(f"%:{token}%") for token in tokens]
        rows = (
            self._db.query(CveAffectedProduct)
            .filter(CveAffectedProduct.cpes_json.isnot(None))
            .filter(and_(*conditions))
            .all()
        )
        selected: list[CveAffectedProduct] = []
        query_tokens = set(tokens)
        for row in rows:
            cpe_tokens = set(extract_cpe_identity_tokens(_to_string_list(row.cpes_json)))
            if _all_query_tokens_covered(query_tokens, cpe_tokens):
                selected.append(row)
        return tuple(selected)

    def _query_product_tokens(self, *, tokens: tuple[str, ...]) -> tuple[CveAffectedProduct, ...]:
        if not tokens:
            return ()
        significant_tokens = tuple(token for token in tokens if len(token) >= 3) or tokens
        conditions = [CveAffectedProduct.product_norm.ilike(f"%{token}%") for token in significant_tokens]
        rows = (
            self._db.query(CveAffectedProduct)
            .filter(CveAffectedProduct.product_norm.isnot(None))
            .filter(and_(*conditions))
            .all()
        )
        return tuple(rows)

    def _dedupe(
        self,
        *,
        exact_rows: tuple[CveAffectedProduct, ...],
        cpe_rows: tuple[CveAffectedProduct, ...],
        token_rows: tuple[CveAffectedProduct, ...],
    ) -> tuple[ResolvedProjectionCandidate, ...]:
        merged: dict[str, ResolvedProjectionCandidate] = {}
        for quality, rows in (
            ("exact", exact_rows),
            ("cpe", cpe_rows),
            ("token", token_rows),
        ):
            for row in rows:
                key = _row_key(row)
                existing = merged.get(key)
                if existing is not None and _QUALITY_RANK[existing.match_quality] <= _QUALITY_RANK[quality]:
                    continue
                merged[key] = ResolvedProjectionCandidate(row=row, match_quality=quality)

        ordered = sorted(
            merged.values(),
            key=lambda item: (_QUALITY_RANK[item.match_quality], str(getattr(item.row, "cve_id", ""))),
        )
        return tuple(ordered)


def _all_query_tokens_covered(query_tokens: set[str], cpe_tokens: set[str]) -> bool:
    """Every query token must substring-match at least one CPE identity token."""
    for qt in query_tokens:
        if not any(qt in ct or ct in qt for ct in cpe_tokens):
            return False
    return True


def _row_key(row: CveAffectedProduct) -> str:
    payload = {
        "cve_id": str(getattr(row, "cve_id", "") or ""),
        "product_norm": str(getattr(row, "product_norm", "") or ""),
        "vendor_norm": str(getattr(row, "vendor_norm", "") or ""),
        "versions_json": getattr(row, "versions_json", None),
        "cpes_json": getattr(row, "cpes_json", None),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _to_string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    rows: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            rows.append(item.strip())
    return rows or None


def _normalize_name(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip().lower()
    if not cleaned:
        return None
    normalized = " ".join(cleaned.split())
    if len(normalized) > _NORMALIZED_NAME_MAX_LEN:
        normalized = normalized[:_NORMALIZED_NAME_MAX_LEN]
    return normalized


def _tokenize(value: str) -> tuple[str, ...]:
    tokens = _TOKEN_RE.findall(value)
    deduped: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return tuple(deduped)


__all__ = ["MatchQuality", "ProductResolver", "ResolvedProjectionCandidate"]
