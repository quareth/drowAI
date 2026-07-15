"""Closed vocabulary and bounds for semantic evidence entries.

Every rich tool maps execution-local facts into these shared, tool-agnostic
types. Top-level `name` and `value` are canonical summary fields on every
entry. Optional `detail` fields are auxiliary-only and constrained by a closed
per-type schema.
"""

from __future__ import annotations

from enum import Enum
from typing import Mapping, TypedDict


class SemanticEvidenceType(str, Enum):
    """Closed semantic evidence type vocabulary."""

    TARGET_TEMPLATE = "target_template"
    EXECUTION_PARAMETER = "execution_parameter"
    MATCHER_OR_FILTER = "matcher_or_filter"
    BASELINE = "baseline"
    RESULT_SUMMARY = "result_summary"
    VARIANT = "variant"
    DIAGNOSTIC = "diagnostic"


SemanticEvidenceScalar = str | int | float | bool | None


class SemanticEvidenceEntry(TypedDict, total=False):
    """Normalized semantic evidence entry contract."""

    type: str
    name: str
    value: SemanticEvidenceScalar
    detail: Mapping[str, SemanticEvidenceScalar]
    source: str


EVIDENCE_PER_TYPE_LIMIT: Mapping[SemanticEvidenceType, int] = {
    SemanticEvidenceType.TARGET_TEMPLATE: 2,
    SemanticEvidenceType.EXECUTION_PARAMETER: 6,
    SemanticEvidenceType.MATCHER_OR_FILTER: 5,
    SemanticEvidenceType.BASELINE: 3,
    SemanticEvidenceType.RESULT_SUMMARY: 3,
    SemanticEvidenceType.VARIANT: 1,
    SemanticEvidenceType.DIAGNOSTIC: 4,
}

EVIDENCE_DETAIL_SCHEMA: Mapping[SemanticEvidenceType, frozenset[str]] = {
    SemanticEvidenceType.TARGET_TEMPLATE: frozenset(
        {"placeholder", "scheme", "host", "port"}
    ),
    SemanticEvidenceType.EXECUTION_PARAMETER: frozenset({"unit"}),
    SemanticEvidenceType.MATCHER_OR_FILTER: frozenset(
        {"kind", "source", "negated"}
    ),
    SemanticEvidenceType.BASELINE: frozenset({"source", "strategy", "unit", "note"}),
    SemanticEvidenceType.RESULT_SUMMARY: frozenset(
        {"before_filter_count", "after_filter_count", "unit"}
    ),
    SemanticEvidenceType.VARIANT: frozenset(),
    SemanticEvidenceType.DIAGNOSTIC: frozenset({"severity", "note"}),
}

SEMANTIC_EVIDENCE_NAME_MAX_LEN = 64
SEMANTIC_EVIDENCE_VALUE_MAX_LEN = 256
SEMANTIC_EVIDENCE_DETAIL_MAX_KEYS = 8
SEMANTIC_EVIDENCE_DETAIL_VALUE_MAX_LEN = 256

_SEMANTIC_EVIDENCE_GLOBAL_LIMIT = 25


_enum_members = frozenset(SemanticEvidenceType)
_schema_members = frozenset(EVIDENCE_DETAIL_SCHEMA.keys())
if _enum_members != _schema_members:
    missing = sorted(member.value for member in _enum_members - _schema_members)
    extra = sorted(member.value for member in _schema_members - _enum_members)
    raise ValueError(
        "EVIDENCE_DETAIL_SCHEMA must cover exactly SemanticEvidenceType members "
        f"(missing={missing}, extra={extra})"
    )

for evidence_type, allowed_detail_keys in EVIDENCE_DETAIL_SCHEMA.items():
    forbidden = {"name", "value"} & allowed_detail_keys
    if forbidden:
        raise ValueError(
            "EVIDENCE_DETAIL_SCHEMA must keep `name`/`value` top-level only "
            f"(type={evidence_type.value}, forbidden={sorted(forbidden)})"
        )

_max_detail_keys = max((len(keys) for keys in EVIDENCE_DETAIL_SCHEMA.values()), default=0)
if SEMANTIC_EVIDENCE_DETAIL_MAX_KEYS < _max_detail_keys:
    raise ValueError(
        "SEMANTIC_EVIDENCE_DETAIL_MAX_KEYS must be >= largest allowed detail "
        f"set ({_max_detail_keys})"
    )

if sum(EVIDENCE_PER_TYPE_LIMIT.values()) > _SEMANTIC_EVIDENCE_GLOBAL_LIMIT:
    raise ValueError(
        "Sum of EVIDENCE_PER_TYPE_LIMIT must be <= semantic evidence global "
        f"limit ({_SEMANTIC_EVIDENCE_GLOBAL_LIMIT})"
    )


def get_evidence_per_type_limit(evidence_type: SemanticEvidenceType) -> int:
    """Return configured per-type evidence cap for a vocabulary member."""
    return EVIDENCE_PER_TYPE_LIMIT[evidence_type]


def get_evidence_detail_schema(evidence_type: SemanticEvidenceType) -> frozenset[str]:
    """Return a copy of allowed detail keys for a vocabulary member."""
    return frozenset(EVIDENCE_DETAIL_SCHEMA[evidence_type])


def get_semantic_evidence_global_limit() -> int:
    """Return configured global evidence cap."""
    return _SEMANTIC_EVIDENCE_GLOBAL_LIMIT


__all__ = (
    "SemanticEvidenceEntry",
    "SemanticEvidenceScalar",
    "SemanticEvidenceType",
    "EVIDENCE_DETAIL_SCHEMA",
    "EVIDENCE_PER_TYPE_LIMIT",
    "get_evidence_per_type_limit",
    "get_evidence_detail_schema",
    "get_semantic_evidence_global_limit",
    "SEMANTIC_EVIDENCE_DETAIL_MAX_KEYS",
    "SEMANTIC_EVIDENCE_DETAIL_VALUE_MAX_LEN",
    "SEMANTIC_EVIDENCE_NAME_MAX_LEN",
    "SEMANTIC_EVIDENCE_VALUE_MAX_LEN",
)
