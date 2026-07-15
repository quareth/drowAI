"""Central severity policy for durable knowledge findings.

This module owns severity vocabulary, normalization, ranking, bucket creation,
and deterministic MVP severity resolution from structured observation facts.
It does not inspect titles, raw artifacts, UI state, or database rows.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

SEVERITY_POLICY_VERSION = "knowledge-severity.v1"
FINDING_SEVERITY_LEVELS: tuple[str, ...] = ("critical", "high", "medium", "low", "info")
ALLOWED_FINDING_SEVERITIES = frozenset(FINDING_SEVERITY_LEVELS)

_SEVERITY_SORT_SCORE: dict[str, int] = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
    "info": 0,
}

_CVSS_KEYS: tuple[str, ...] = ("cvss_score", "cvss", "base_score")
_FINDING_SUBTYPE_DEFAULTS: dict[str, tuple[str, str]] = {
    "credential_compromise_confirmed": ("high", "finding_subtype:credential_compromise_confirmed"),
    "secret_exposure_detected": ("medium", "finding_subtype:secret_exposure_detected"),
}


@dataclass(frozen=True, slots=True)
class SeverityResolution:
    """Resolved finding severity plus explainability metadata."""

    severity: str
    source: str
    signal: str
    policy_version: str = SEVERITY_POLICY_VERSION

    def to_metadata(self) -> dict[str, str]:
        """Serialize resolution details for durable finding metadata."""
        return {
            "policy_version": self.policy_version,
            "source": self.source,
            "signal": self.signal,
            "severity": self.severity,
        }


def normalize_severity(value: Any) -> str | None:
    """Return a canonical severity when value is part of the policy vocabulary."""
    normalized = str(value or "").strip().lower()
    return normalized if normalized in ALLOWED_FINDING_SEVERITIES else None


def severity_sort_score(value: Any) -> int:
    """Return deterministic severity sort score matching existing query behavior."""
    return _SEVERITY_SORT_SCORE.get(str(value or "").strip().lower(), -1)


def severity_bucket_template() -> dict[str, int]:
    """Return a fresh summary bucket map in canonical display order."""
    return {severity: 0 for severity in FINDING_SEVERITY_LEVELS}


def severity_from_cvss(value: Any) -> str | None:
    """Map a CVSS-like numeric score into the canonical severity vocabulary."""
    if value is None or isinstance(value, bool):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score) or score < 0:
        return None
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    if score > 0:
        return "low"
    return "info"


def resolve_finding_severity(
    *,
    observation_type: str,
    assertion_level: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> SeverityResolution | None:
    """Resolve severity from structured finding observation facts only."""
    del assertion_level  # Reserved for future policy inputs; not needed by MVP rules.
    payload_map = dict(payload or {})

    explicit = normalize_severity(payload_map.get("severity"))
    if explicit is not None:
        return SeverityResolution(
            severity=explicit,
            source="explicit",
            signal="payload.severity",
        )

    for key in _CVSS_KEYS:
        mapped = severity_from_cvss(payload_map.get(key))
        if mapped is not None:
            return SeverityResolution(
                severity=mapped,
                source="numeric_score",
                signal=f"payload.{key}",
            )

    subtype = str(payload_map.get("finding_subtype") or "").strip().lower()
    if subtype in _FINDING_SUBTYPE_DEFAULTS:
        severity, signal = _FINDING_SUBTYPE_DEFAULTS[subtype]
        return SeverityResolution(
            severity=severity,
            source="policy_default",
            signal=signal,
        )

    normalized_observation_type = str(observation_type or "").strip().lower()
    if normalized_observation_type == "finding.exploit_succeeded":
        return SeverityResolution(
            severity="high",
            source="policy_default",
            signal="observation_type:finding.exploit_succeeded",
        )

    return None
