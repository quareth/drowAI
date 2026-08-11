"""Batch-aware compact evidence reader for caller reasoning prompts.

Phase 6 Task 6.3: ``read_compact_evidence`` returns the batch view when
``metadata["last_tool_result_compact_batch"]`` is present and falls back
to the legacy single-call ``metadata["last_tool_result_compact"]``
otherwise. Reasoning callers read these helpers exclusively so partial
failures in a multi-call batch are not hidden by the compatibility field.

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


def aggregate_compact_evidence_rows(
    *,
    tool_batch_id: str,
    rows: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    """Build canonical aggregate batch metadata from selected evidence rows."""

    return _aggregate_batch_metadata(tool_batch_id, rows)


def _aggregate_batch_metadata(
    tool_batch_id: str,
    rows: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Return one compact batch envelope with deterministic aggregate status."""

    copied_rows = [dict(row) for row in rows]
    successful = [row for row in copied_rows if row.get("success")]
    failed = [row for row in copied_rows if not row.get("success")]
    if failed and successful:
        status = "completed_with_errors"
    elif failed:
        status = "failed"
    else:
        status = "completed"
    deferred: list[str] = []
    for row in copied_rows:
        raw_followups = row.get("deferred_followups")
        if not isinstance(raw_followups, list):
            continue
        for followup in raw_followups:
            normalized = str(followup or "").strip()
            if normalized and normalized not in deferred:
                deferred.append(normalized)
    return {
        "tool_batch_id": str(tool_batch_id),
        "execution_session_aggregate": True,
        "status": status,
        "success": not failed,
        "results": copied_rows,
        "deferred_followups": deferred,
    }


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


def select_compact_evidence_for_reasoning(
    metadata: Mapping[str, Any],
) -> tuple[Optional[EvidenceView], bool]:
    """Select same-turn evidence without restoring transient batch rows.

    Durable evidence is the authority for row membership and ordering. When
    same-process runtime evidence is available, matching rows supply their raw
    compact payloads so immediate reasoning keeps the existing unmasked view.
    Runtime-only evidence remains available to the current turn but is marked
    non-durable so callers can suppress graph and memory persistence. Completed
    interactive execution-session aggregates retain their bounded ordered rows;
    the terminal row remains available as the primary completion summary without
    deleting the interaction history needed to reason about session continuity.

    Returns:
        A pair of ``(evidence, is_durable)``. ``is_durable`` is true only when
        the selected row set is backed by durable metadata.
    """
    durable = read_compact_evidence(metadata)
    runtime = read_compact_evidence(metadata, prefer_runtime=True)
    if durable is None:
        return runtime, False
    if runtime is None:
        return durable, True

    if runtime.raw.get("execution_session_aggregate") is True:
        durable_call_ids = {
            str(row.get("tool_call_id") or "").strip()
            for row in durable.rows
            if str(row.get("tool_call_id") or "").strip()
        }
        runtime_call_ids = {
            str(row.get("tool_call_id") or "").strip()
            for row in runtime.rows
            if str(row.get("tool_call_id") or "").strip()
        }
        if runtime_call_ids != durable_call_ids:
            return runtime, False

    runtime_by_call_id = {
        str(row.get("tool_call_id")): row
        for row in runtime.rows
        if str(row.get("tool_call_id") or "").strip()
    }
    selected_rows: list[Dict[str, Any]] = []
    for durable_row in durable.rows:
        call_id = str(durable_row.get("tool_call_id") or "").strip()
        runtime_row = runtime_by_call_id.get(call_id) if call_id else None
        if (
            runtime_row is None
            and durable.source == "single"
            and runtime.source == "single"
            and len(durable.rows) == 1
            and len(runtime.rows) == 1
        ):
            runtime_row = runtime.rows[0]
        selected_rows.append(dict(runtime_row or durable_row))

    successes = [row for row in selected_rows if row.get("success")]
    failures = [row for row in selected_rows if not row.get("success")]
    selected_raw = dict(durable.raw)
    if durable.source == "batch":
        selected_raw["results"] = [dict(row) for row in selected_rows]
    elif selected_rows:
        compact = selected_rows[0].get("compact_tool_result")
        if isinstance(compact, Mapping):
            selected_raw = dict(compact)

    return (
        EvidenceView(
            source=durable.source,
            status=durable.status,
            success=durable.success,
            rows=tuple(selected_rows),
            failed_rows=tuple(failures),
            successful_rows=tuple(successes),
            deferred_followups=durable.deferred_followups,
            raw=selected_raw,
        ),
        True,
    )


def preferred_compact_evidence_row(
    evidence: EvidenceView,
) -> Mapping[str, Any] | None:
    """Select the terminal session row or the ordinary batch primary row."""

    if not evidence.rows:
        return None
    if evidence.raw.get("execution_session_aggregate") is True:
        return evidence.rows[-1]
    return evidence.rows[0]


def compact_tool_result_for_reasoning(
    evidence: EvidenceView | None,
) -> Mapping[str, Any]:
    """Return the caller-ready compact payload from canonical evidence."""

    if evidence is None:
        return {}
    selected_row = preferred_compact_evidence_row(evidence)
    compact = selected_row.get("compact_tool_result") if selected_row else None
    return compact if isinstance(compact, Mapping) else {}


__all__ = [
    "EvidenceView",
    "aggregate_compact_evidence_rows",
    "compact_tool_result_for_reasoning",
    "preferred_compact_evidence_row",
    "read_compact_evidence",
    "register_runtime_compact_evidence",
    "select_compact_evidence_for_reasoning",
]
