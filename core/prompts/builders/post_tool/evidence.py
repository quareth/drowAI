"""Batch-aware compact evidence reader for the PTR prompt builders.

Phase 6 Task 6.3: ``read_compact_evidence`` returns the batch view when
``metadata["last_tool_result_compact_batch"]`` is present and falls back
to the legacy single-call ``metadata["last_tool_result_compact"]``
otherwise. PTR builders read the helper exclusively so partial failures
in a multi-call batch are not hidden by the compatibility field.

The returned :class:`EvidenceView` is intentionally a small, flat shape
(neither pydantic nor dataclass-with-slots) so prompt builders can
serialize it directly and tests can assert on its keys without coupling
to runtime types.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence

_RUNTIME_COMPACT_EVIDENCE_LIMIT = 128
_RUNTIME_COMPACT_EVIDENCE_BY_BATCH_ID: OrderedDict[
    str, Dict[str, Mapping[str, Any]]
] = OrderedDict()


@dataclass(frozen=True)
class EvidenceView:
    """Compact evidence projected for the PTR prompt builders.

    Fields:

    - ``source``: ``"batch"`` when the batch-shaped metadata was used,
      ``"single"`` otherwise.
    - ``status``: aggregate batch status (``completed`` / ``completed_with_errors``
      / ``failed`` / ``denied`` / ``cancelled``) for batch source, or the
      single-call status string for single source.
    - ``success``: aggregate success flag.
    - ``rows``: per-call rows (always populated even for single-call).
    - ``failed_rows`` / ``successful_rows``: convenience filters PTR
      builders use to surface failures distinctly from successes.
    - ``deferred_followups``: batch-only field (empty for single source).
    - ``raw``: the underlying compact metadata dict for callers that need
      it verbatim.
    """

    source: str
    status: str
    success: bool
    rows: Sequence[Dict[str, Any]]
    failed_rows: Sequence[Dict[str, Any]] = field(default_factory=tuple)
    successful_rows: Sequence[Dict[str, Any]] = field(default_factory=tuple)
    deferred_followups: Sequence[str] = field(default_factory=tuple)
    raw: Mapping[str, Any] = field(default_factory=dict)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _row_from_single(compact: Mapping[str, Any]) -> Dict[str, Any]:
    """Project a legacy single-tool compact dict into the batch row shape."""
    tool_id = (
        compact.get("tool")
        or compact.get("tool_id")
        or compact.get("name")
        or "unknown_tool"
    )
    summary = compact.get("summary") or ""
    success = bool(compact.get("success", True)) if "success" in compact else True
    row: Dict[str, Any] = {
        "tool_call_id": compact.get("tool_call_id", ""),
        "tool_id": str(tool_id),
        "intent": str(compact.get("intent", "") or ""),
        "status": "success" if success else "failed",
        "success": success,
        "compact_tool_result": dict(compact),
    }
    if not success:
        row["failure_category"] = (
            compact.get("failure_category") or "tool_error"
        )
    if summary:
        row["summary"] = str(summary)
    return row


def register_runtime_compact_evidence(
    batch_metadata: Mapping[str, Any],
    *,
    single_compact: Optional[Mapping[str, Any]] = None,
) -> None:
    """Store same-process raw compact evidence for the immediate PTR turn."""
    batch_id = str(batch_metadata.get("tool_batch_id") or "").strip()
    if not batch_id:
        return

    _RUNTIME_COMPACT_EVIDENCE_BY_BATCH_ID[batch_id] = {
        "batch": dict(batch_metadata),
        "single": dict(single_compact or {}),
    }
    _RUNTIME_COMPACT_EVIDENCE_BY_BATCH_ID.move_to_end(batch_id)
    while (
        len(_RUNTIME_COMPACT_EVIDENCE_BY_BATCH_ID)
        > _RUNTIME_COMPACT_EVIDENCE_LIMIT
    ):
        _RUNTIME_COMPACT_EVIDENCE_BY_BATCH_ID.popitem(last=False)


def _runtime_compact_evidence(metadata: Mapping[str, Any]) -> Mapping[str, Any]:
    batch_meta = metadata.get("last_tool_result_compact_batch")
    batch_id = ""
    if isinstance(batch_meta, Mapping):
        batch_id = str(batch_meta.get("tool_batch_id") or "").strip()
    if not batch_id:
        batch_id = str(metadata.get("tool_batch_id") or "").strip()
    if not batch_id:
        return {}

    cached = _RUNTIME_COMPACT_EVIDENCE_BY_BATCH_ID.get(batch_id)
    if cached is None:
        return {}
    _RUNTIME_COMPACT_EVIDENCE_BY_BATCH_ID.move_to_end(batch_id)
    return cached


def read_compact_evidence(
    metadata: Mapping[str, Any],
    *,
    prefer_runtime: bool = False,
) -> Optional[EvidenceView]:
    """Return the preferred compact-evidence view for PTR.

    Returns ``None`` only when neither metadata key is populated.
    """
    if not isinstance(metadata, Mapping):
        return None

    runtime_evidence = _runtime_compact_evidence(metadata) if prefer_runtime else {}
    batch_meta = runtime_evidence.get("batch") or metadata.get(
        "last_tool_result_compact_batch"
    )
    if isinstance(batch_meta, Mapping) and batch_meta.get("results") is not None:
        results = list(batch_meta.get("results") or [])
        rows = [dict(row) for row in results if isinstance(row, Mapping)]
        successes = [row for row in rows if row.get("success")]
        failures = [row for row in rows if not row.get("success")]
        deferred = batch_meta.get("deferred_followups") or []
        return EvidenceView(
            source="batch",
            status=str(batch_meta.get("status") or "unknown"),
            success=bool(batch_meta.get("success", False)),
            rows=tuple(rows),
            failed_rows=tuple(failures),
            successful_rows=tuple(successes),
            deferred_followups=tuple(deferred) if isinstance(deferred, list) else (),
            raw=dict(batch_meta),
        )

    single_meta = runtime_evidence.get("single") or metadata.get(
        "last_tool_result_compact"
    )
    if isinstance(single_meta, Mapping) and single_meta:
        row = _row_from_single(single_meta)
        rows = (row,)
        return EvidenceView(
            source="single",
            status=row["status"],
            success=row["success"],
            rows=rows,
            failed_rows=() if row["success"] else (row,),
            successful_rows=(row,) if row["success"] else (),
            deferred_followups=(),
            raw=dict(single_meta),
        )

    return None


__all__ = [
    "EvidenceView",
    "read_compact_evidence",
    "register_runtime_compact_evidence",
]
