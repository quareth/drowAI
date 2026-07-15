"""Typed contracts for deterministic product-version CVE lookup.

Scope:
- Defines request and response boundaries for product-version CVE matching.

Boundary:
- Contains no SQL, orchestration, or transport logic.
"""

from __future__ import annotations

from dataclasses import dataclass


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


@dataclass(slots=True, frozen=True)
class CveLookupRequest:
    """Input contract for one deterministic product-version lookup."""

    product: str
    version: str
    max_results: int = 5

    def __post_init__(self) -> None:
        cleaned_product = _clean_text(self.product)
        cleaned_version = _clean_text(self.version)
        object.__setattr__(self, "product", str(cleaned_product or "").lower())
        object.__setattr__(self, "version", cleaned_version)

        if cleaned_product is None:
            raise ValueError("product is required")
        if cleaned_version is None:
            raise ValueError("version is required")
        if int(self.max_results) <= 0:
            raise ValueError("max_results must be >= 1")


@dataclass(slots=True, frozen=True)
class CveLookupMatch:
    """One CVE match candidate returned by deterministic lookup."""

    cve_id: str
    rationale: str
    summary: str | None = None
    version_applicable: bool | None = None
    matched_fields: tuple[str, ...] = ()
    score: float = 0.0

    def __post_init__(self) -> None:
        cleaned_cve_id = _clean_text(self.cve_id)
        cleaned_rationale = _clean_text(self.rationale)
        cleaned_summary = _clean_text(self.summary)
        object.__setattr__(self, "cve_id", cleaned_cve_id)
        object.__setattr__(self, "rationale", cleaned_rationale)
        object.__setattr__(self, "summary", cleaned_summary)
        object.__setattr__(
            self,
            "matched_fields",
            tuple(str(item).strip() for item in self.matched_fields if str(item).strip()),
        )

        if cleaned_cve_id is None:
            raise ValueError("cve_id is required")
        if cleaned_rationale is None:
            raise ValueError("rationale is required")
        if not 0.0 <= float(self.score) <= 1.0:
            raise ValueError("score must be between 0.0 and 1.0")


@dataclass(slots=True, frozen=True)
class CveLookupResponse:
    """Typed lookup response envelope for product-version matcher boundaries."""

    matches: tuple[CveLookupMatch, ...] = ()
    message: str | None = None

    def __post_init__(self) -> None:
        cleaned_message = _clean_text(self.message)
        object.__setattr__(self, "message", cleaned_message)
        object.__setattr__(self, "matches", tuple(self.matches))


__all__ = ["CveLookupMatch", "CveLookupRequest", "CveLookupResponse"]
