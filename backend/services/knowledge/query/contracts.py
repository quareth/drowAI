""" knowledge query contracts and normalization helpers.

This module owns router-facing typed filter contracts and deterministic
normalization for pagination, booleans, and sort inputs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, Literal, Sequence, TypeVar

from ..severity_policy import ALLOWED_FINDING_SEVERITIES

_T = TypeVar("_T")

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
WEB_SURFACE_NOISY_HIDE_THRESHOLD = 0.5

FindingSort = Literal["last_seen_desc", "last_seen_asc", "severity_desc", "severity_asc"]
AssetSort = Literal["last_seen_desc", "last_seen_asc", "asset_type_asc", "asset_type_desc"]
EvidenceSort = Literal["observed_desc", "observed_asc", "source_tool_asc", "source_tool_desc"]

ALLOWED_FINDING_STATUSES = frozenset(
    {
        "candidate",
        "open",
        "triaged",
        "in_progress",
        "resolved",
        "false_positive",
        "accepted_risk",
        "confirmed",
        "exploited",
    }
)

TRUE_VALUES = frozenset({"1", "true", "t", "yes", "y", "on"})
FALSE_VALUES = frozenset({"0", "false", "f", "no", "n", "off"})
ALLOWED_ENGAGEMENT_STATUSES = frozenset({"active", "archived", "all"})


@dataclass(frozen=True, slots=True)
class PaginationParams:
    """Shared pagination parameters for list endpoints."""

    limit: int | str | None = DEFAULT_LIMIT
    offset: int | str | None = 0

    def normalized(self) -> "PaginationParams":
        """Clamp pagination values into a safe deterministic range."""
        safe_limit = _coerce_int(self.limit, default=DEFAULT_LIMIT)
        safe_offset = _coerce_int(self.offset, default=0)
        safe_limit = max(1, min(safe_limit, MAX_LIMIT))
        safe_offset = max(0, safe_offset)
        return PaginationParams(limit=safe_limit, offset=safe_offset)


@dataclass(frozen=True, slots=True)
class PaginatedResult(Generic[_T]):
    """Shared list response shape for engagement-scoped list routes."""

    items: tuple[_T, ...] = field(default_factory=tuple)
    total: int = 0
    limit: int = DEFAULT_LIMIT
    offset: int = 0

    @classmethod
    def from_items(
        cls,
        *,
        items: Sequence[_T],
        total: int,
        limit: int,
        offset: int,
    ) -> "PaginatedResult[_T]":
        """Construct a pagination contract that always uses one stable shape."""
        normalized = PaginationParams(limit=limit, offset=offset).normalized()
        return cls(
            items=tuple(items),
            total=max(0, int(total)),
            limit=int(normalized.limit),
            offset=int(normalized.offset),
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize to the common `items/total/limit/offset` router payload shape."""
        return {
            "items": list(self.items),
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
        }


@dataclass(frozen=True, slots=True)
class EngagementListFilters:
    """Normalized filters for engagement listing."""

    query: str | None = None
    status: str | None = "active"
    limit: int | str | None = DEFAULT_LIMIT
    offset: int | str | None = 0

    def normalized(self) -> "EngagementListFilters":
        """Return normalized engagement list filters."""
        pagination = PaginationParams(limit=self.limit, offset=self.offset).normalized()
        return EngagementListFilters(
            query=_normalize_text(self.query),
            status=_normalize_choice(self.status or "active", ALLOWED_ENGAGEMENT_STATUSES) or "active",
            limit=int(pagination.limit),
            offset=int(pagination.offset),
        )


@dataclass(frozen=True, slots=True)
class FindingsFilters:
    """Normalized findings query filters for one engagement."""

    severity: str | None = None
    status: str | None = None
    exploited: bool | str | None = None
    asset: str | None = None
    source: str | None = None
    query: str | None = None
    include_candidates: bool | str | None = False
    sort: FindingSort | str = "last_seen_desc"
    limit: int | str | None = DEFAULT_LIMIT
    offset: int | str | None = 0

    def normalized(self) -> "FindingsFilters":
        """Return deterministic findings filters for query execution."""
        pagination = PaginationParams(limit=self.limit, offset=self.offset).normalized()
        return FindingsFilters(
            severity=_normalize_choice(self.severity, ALLOWED_FINDING_SEVERITIES),
            status=_normalize_choice(self.status, ALLOWED_FINDING_STATUSES),
            exploited=normalize_optional_bool(self.exploited),
            asset=_normalize_text(self.asset),
            source=_normalize_text(self.source),
            query=_normalize_text(self.query),
            include_candidates=normalize_optional_bool(self.include_candidates) is True,
            sort=_normalize_sort(
                self.sort,
                allowed_values=("last_seen_desc", "last_seen_asc", "severity_desc", "severity_asc"),
                default="last_seen_desc",
            ),
            limit=int(pagination.limit),
            offset=int(pagination.offset),
        )


@dataclass(frozen=True, slots=True)
class AssetsFilters:
    """Normalized assets query filters for one engagement."""

    type: str | None = None
    vulnerable: bool | str | None = None
    exploited: bool | str | None = None
    query: str | None = None
    sort: AssetSort | str = "last_seen_desc"
    limit: int | str | None = DEFAULT_LIMIT
    offset: int | str | None = 0

    def normalized(self) -> "AssetsFilters":
        """Return deterministic assets filters for query execution."""
        pagination = PaginationParams(limit=self.limit, offset=self.offset).normalized()
        return AssetsFilters(
            type=_normalize_text(self.type),
            vulnerable=normalize_optional_bool(self.vulnerable),
            exploited=normalize_optional_bool(self.exploited),
            query=_normalize_text(self.query),
            sort=_normalize_sort(
                self.sort,
                allowed_values=("last_seen_desc", "last_seen_asc", "asset_type_asc", "asset_type_desc"),
                default="last_seen_desc",
            ),
            limit=int(pagination.limit),
            offset=int(pagination.offset),
        )


@dataclass(frozen=True, slots=True)
class EvidenceFilters:
    """Normalized evidence query filters for one engagement."""

    source_tool: str | None = None
    type: str | None = None
    query: str | None = None
    sort: EvidenceSort | str = "observed_desc"
    limit: int | str | None = DEFAULT_LIMIT
    offset: int | str | None = 0

    def normalized(self) -> "EvidenceFilters":
        """Return deterministic evidence filters for query execution."""
        pagination = PaginationParams(limit=self.limit, offset=self.offset).normalized()
        return EvidenceFilters(
            source_tool=_normalize_text(self.source_tool),
            type=_normalize_text(self.type),
            query=_normalize_text(self.query),
            sort=_normalize_sort(
                self.sort,
                allowed_values=("observed_desc", "observed_asc", "source_tool_asc", "source_tool_desc"),
                default="observed_desc",
            ),
            limit=int(pagination.limit),
            offset=int(pagination.offset),
        )


@dataclass(frozen=True, slots=True)
class WebSurfacePathsFilters:
    """Normalized filters for service-scoped web-surface path reads."""

    service_key: str | None = None
    origin_key: str | None = None
    include_noisy: bool | str | None = False
    limit: int | str | None = MAX_LIMIT
    offset: int | str | None = 0

    def normalized(self) -> "WebSurfacePathsFilters":
        """Return deterministic web-surface path filters for query execution."""
        safe_limit = _coerce_int(self.limit, default=MAX_LIMIT)
        safe_offset = _coerce_int(self.offset, default=0)
        return WebSurfacePathsFilters(
            service_key=_normalize_text(self.service_key),
            origin_key=_normalize_text(self.origin_key),
            include_noisy=normalize_optional_bool(self.include_noisy) is True,
            limit=max(1, min(safe_limit, MAX_LIMIT)),
            offset=max(0, safe_offset),
        )


def _coerce_int(value: int | str | None, *, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _normalize_choice(value: str | None, allowed: frozenset[str]) -> str | None:
    normalized = _normalize_text(value)
    if not normalized:
        return None
    lowered = normalized.lower()
    if lowered not in allowed:
        return None
    return lowered


def normalize_optional_bool(value: bool | str | None) -> bool | None:
    """Coerce loosely-typed query input into a strict tri-state bool.

    Returns ``True``/``False`` only for known canonical truthy/falsy strings
    (or native ``bool``). Unknown strings collapse to ``None`` so callers can
    apply a safe default instead of trusting Python's "non-empty string is
    truthy" semantics.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    return None


def _normalize_sort(value: str, *, allowed_values: tuple[str, ...], default: str) -> str:
    normalized = _normalize_text(value)
    if not normalized:
        return default
    lowered = normalized.lower()
    if lowered not in allowed_values:
        return default
    return lowered


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "WEB_SURFACE_NOISY_HIDE_THRESHOLD",
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
    "normalize_optional_bool",
]
