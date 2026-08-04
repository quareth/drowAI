"""Pure helpers for assembling compact tool-output envelope fields.

This module owns behavior-preserving merge, artifact reference, and compact
error projection helpers. It must not call LLMs, execute tools, read files, or
reach into runtime-provider or backend services.
"""

from __future__ import annotations

from typing import Any, Iterable, List, Mapping, Optional

from core.prompts.constants import COMPACT_ERROR_ENTRY_MAX_CHARS

from ..schema import ArtifactReference
from .common import (
    _metadata_compact_decision_evidence,
    as_int,
    compact_evidence_line,
    dedupe_string_list,
    sanitize_artifact_refs,
)


def merge_decision_evidence(
    *,
    raw_result: Mapping[str, Any],
    processed_evidence: Iterable[Any],
    limit: int = 5,
) -> List[str]:
    """Prefer tool-authored and exact locator evidence, then processed evidence."""

    deterministic = [
        *_metadata_compact_decision_evidence(raw_result),
        *_extract_locator_evidence_from_metadata(raw_result, limit=limit),
    ]
    return dedupe_string_list([*deterministic, *list(processed_evidence)], limit=limit)


def _extract_locator_evidence_from_metadata(
    raw_result: Mapping[str, Any],
    *,
    limit: int = 5,
) -> List[str]:
    """Build locator evidence from structured filesystem metadata."""

    runtime_metadata = raw_result.get("metadata")
    if not isinstance(runtime_metadata, Mapping):
        return []

    evidence: List[str] = []
    seen: set[str] = set()

    def add(raw: Any) -> None:
        if len(evidence) >= limit:
            return
        compact = compact_evidence_line(raw)
        if compact and compact not in seen:
            seen.add(compact)
            evidence.append(compact)

    fs_search = runtime_metadata.get("fs_search_text")
    if isinstance(fs_search, Mapping):
        matches = fs_search.get("matches")
        if isinstance(matches, list):
            for item in matches:
                if not isinstance(item, Mapping):
                    continue
                path = str(item.get("path") or "").strip()
                line = as_int(item.get("line"))
                snippet = str(item.get("snippet") or "").strip()
                if not path or line is None or not snippet:
                    continue
                add(f"{path}:{line}:{snippet}")

    fs_read = runtime_metadata.get("fs_read")
    if isinstance(fs_read, Mapping):
        line_evidence = fs_read.get("line_evidence")
        if isinstance(line_evidence, list):
            for item in line_evidence:
                add(item)

    return evidence


def extract_artifact_refs(
    *,
    artifact_path: Optional[str],
    raw_result: Mapping[str, Any],
    execution_id: Optional[str],
) -> List[ArtifactReference]:
    """Project current artifact inputs into compact artifact references."""

    candidates: List[Mapping[str, Any]] = []

    def _append_candidate(
        path: Optional[str],
        *,
        artifact_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        artifact_kind: Optional[str] = None,
        label: Optional[str] = None,
        relative_path: Optional[str] = None,
    ) -> None:
        if not path:
            return
        normalized = str(path).strip()
        if not normalized:
            return
        candidates.append(
            {
                "path": normalized,
                "artifact_id": artifact_id,
                "execution_id": execution_id,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "artifact_kind": artifact_kind,
                "label": label,
                "relative_path": relative_path,
            }
        )

    _append_candidate(artifact_path)

    raw_artifacts = raw_result.get("artifacts")
    if isinstance(raw_artifacts, list):
        for item in raw_artifacts:
            if isinstance(item, str):
                _append_candidate(item)
            elif isinstance(item, Mapping):
                artifact_id = (
                    str(item["artifact_id"])
                    if item.get("artifact_id") is not None
                    else None
                )
                _append_candidate(
                    str(item.get("path") or item.get("artifact_path") or ""),
                    artifact_id=artifact_id,
                    tool_call_id=(
                        str(item["tool_call_id"])
                        if item.get("tool_call_id") is not None
                        else None
                    ),
                    tool_name=(
                        str(item["tool_name"]) if item.get("tool_name") is not None else None
                    ),
                    artifact_kind=(
                        str(item["artifact_kind"]) if item.get("artifact_kind") is not None else None
                    ),
                    label=(
                        str(item["label"]) if item.get("label") is not None else None
                    ),
                    relative_path=(
                        str(item["relative_path"]) if item.get("relative_path") is not None else None
                    ),
                )

    return [
        ArtifactReference(
            path=str(ref["path"]),
            artifact_id=ref.get("artifact_id"),
            execution_id=ref.get("execution_id"),
            tool_call_id=ref.get("tool_call_id"),
            tool_name=ref.get("tool_name"),
            artifact_kind=ref.get("artifact_kind"),
            label=ref.get("label"),
            relative_path=ref.get("relative_path"),
        )
        for ref in sanitize_artifact_refs(candidates)
    ]


def derive_compact_errors(
    *,
    processed: Any,
    summary: str,
    success: bool,
) -> List[str]:
    """Return compact, bounded failure causes without copying raw stderr blobs."""

    if success:
        return []

    candidates: List[str] = []

    processed_summary = (
        str(getattr(processed, "summary", "") or "").strip()
        if processed
        else ""
    )
    if processed_summary:
        candidates.append(processed_summary)

    processed_findings = dedupe_string_list(
        getattr(processed, "key_findings", []) if processed else [],
        limit=3,
    )
    candidates.extend(processed_findings)

    compact_summary = str(summary or "").strip()
    if compact_summary:
        candidates.append(compact_summary)

    def _is_traceback_scaffold(text: str) -> bool:
        lowered = text.lower()
        return (
            lowered.startswith("traceback")
            or lowered.startswith("file ")
            or lowered.startswith("command failed: traceback")
            or lowered.startswith("detail:")
            or lowered.startswith("hint:")
            or "most recent call last" in lowered
        )

    for candidate in candidates:
        text = str(candidate).strip().replace("\n", " ")
        if text and not _is_traceback_scaffold(text):
            return [text[:COMPACT_ERROR_ENTRY_MAX_CHARS]]

    for candidate in candidates:
        text = str(candidate).strip().replace("\n", " ")
        if text:
            return [text[:COMPACT_ERROR_ENTRY_MAX_CHARS]]
    return []
