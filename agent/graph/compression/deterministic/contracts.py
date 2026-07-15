"""Pure contracts for deterministic tool-output compression adapters.

This module defines side-effect-free adapter input and result shapes only. It
must not import DB, runtime provider, Docker, runner, filesystem, or LLM
dependencies.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol


Completeness = Literal["complete", "partial", "none"]
LossinessRisk = Literal["low", "medium", "high"]
StructuredSignal = Mapping[str, Any]

_VALID_COMPLETENESS: frozenset[str] = frozenset(("complete", "partial", "none"))
_VALID_LOSSINESS_RISK: frozenset[str] = frozenset(("low", "medium", "high"))


@dataclass(frozen=True, slots=True)
class CompressionInput:
    """Input available to pure deterministic compression adapters."""

    tool_name: str
    raw_result: Mapping[str, Any]
    artifact_path: str | None = None
    execution_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.raw_result, Mapping):
            raise TypeError("raw_result must be a mapping")


@dataclass(frozen=True, slots=True)
class DeterministicCompressionResult:
    """Partial compact fields produced without LLM calls or runtime side effects.

    Completeness semantics:
    - ``complete``: deterministic fields are sufficient for later deterministic-first use.
    - ``partial``: deterministic fields are useful but need fallback processing.
    - ``none``: no deterministic compact fields are available.
    """

    summary: str | None = None
    key_findings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    structured_signals: tuple[StructuredSignal, ...] = ()
    decision_evidence: tuple[str, ...] = ()
    lossiness_risk: LossinessRisk = "medium"
    completeness: Completeness = "none"
    fallback_reason: str | None = None

    def __post_init__(self) -> None:
        if self.completeness not in _VALID_COMPLETENESS:
            raise ValueError(
                "completeness must be one of: complete, partial, none"
            )
        if self.lossiness_risk not in _VALID_LOSSINESS_RISK:
            raise ValueError("lossiness_risk must be one of: low, medium, high")

        object.__setattr__(
            self,
            "key_findings",
            _normalize_text_tuple(self.key_findings),
        )
        object.__setattr__(self, "errors", _normalize_text_tuple(self.errors))
        object.__setattr__(
            self,
            "structured_signals",
            _normalize_mapping_tuple(self.structured_signals),
        )
        object.__setattr__(
            self,
            "decision_evidence",
            _normalize_text_tuple(self.decision_evidence),
        )

    @classmethod
    def none(
        cls,
        *,
        fallback_reason: str | None = None,
    ) -> "DeterministicCompressionResult":
        """Return an explicit no-result value for missing or skipped adapters."""

        return cls(completeness="none", fallback_reason=fallback_reason)


class DeterministicCompressionAdapter(Protocol):
    """Callable contract implemented by pure deterministic adapters."""

    def __call__(
        self,
        input_data: CompressionInput,
    ) -> DeterministicCompressionResult:
        """Return deterministic compact fields for one tool result."""


def _normalize_text_tuple(values: Any) -> tuple[str, ...]:
    """Normalize optional scalar/list/tuple text values into an immutable tuple."""

    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        candidates: Iterable[Any] = (values,)
    else:
        try:
            candidates = iter(values)
        except TypeError:
            candidates = (values,)

    return tuple(str(value) for value in candidates if value is not None)


def _normalize_mapping_tuple(values: Any) -> tuple[StructuredSignal, ...]:
    """Normalize optional mapping/list/tuple signal values into copied mappings."""

    if values is None:
        return ()
    if isinstance(values, Mapping):
        candidates: Iterable[Any] = (values,)
    elif isinstance(values, (str, bytes)):
        candidates = ()
    else:
        try:
            candidates = iter(values)
        except TypeError:
            candidates = ()

    return tuple(dict(value) for value in candidates if isinstance(value, Mapping))
