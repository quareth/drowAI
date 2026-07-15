"""Compute deterministic task-local source watermarks from durable rows.

This module reads reporting source tables through SQLAlchemy only. It does not
inspect task workspaces or perform memo/report generation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.models.chat import ChatMessage, ChatTurnEvent
from backend.models.core import Task
from backend.models.knowledge import (
    KnowledgeEntityProvenance,
    KnowledgeEvidenceArchive,
    KnowledgeObservation,
)
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.services.reporting.contracts import (
    GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY,
    GENERATION_METADATA_SOURCE_WATERMARK_SCHEMA_VERSION_KEY,
    validate_report_type,
)

_SOURCE_SCHEMA_VERSION = 1
_REPORT_SOURCE_SCHEMA_VERSION = 1
_REPORT_SOURCE_HASH_ALGORITHM = "sha256"


@dataclass(frozen=True, slots=True)
class ReportSourceMemoWatermarkInput:
    """Selected memo source snapshot used for report-level watermarks."""

    memo_id: str
    version: int
    source_watermark: Mapping[str, Any]


class SourceWatermarkService:
    """Build stable source markers for one tenant/user/engagement task."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def compute_for_task(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> dict[str, Any]:
        """Return a JSON-serializable watermark for durable task-local sources."""

        sources = {
            "chat_messages": self._chat_message_marker(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            ),
            "chat_turn_events": self._chat_turn_event_marker(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            ),
            "tool_executions": self._tool_execution_marker(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            ),
            "execution_artifacts": self._latest_timestamp_marker(
                ExecutionArtifact,
                ExecutionArtifact.created_at,
                "latest_created_at",
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            ),
            "knowledge_evidence_archives": self._latest_timestamp_marker(
                KnowledgeEvidenceArchive,
                KnowledgeEvidenceArchive.created_at,
                "latest_created_at",
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            ),
            "knowledge_observations": self._latest_timestamp_marker(
                KnowledgeObservation,
                KnowledgeObservation.observed_at,
                "latest_observed_at",
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            ),
            "knowledge_entity_provenance": self._latest_timestamp_marker(
                KnowledgeEntityProvenance,
                KnowledgeEntityProvenance.observed_at,
                "latest_observed_at",
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            ),
        }
        return {
            "schema_version": _SOURCE_SCHEMA_VERSION,
            "empty": not _contains_source_value(sources),
            "sources": sources,
        }

    def compute_for_report(
        self,
        *,
        report_type: str,
        selected_memos: Sequence[ReportSourceMemoWatermarkInput],
        include_candidate_findings: bool,
    ) -> dict[str, Any]:
        """Return a deterministic report-level watermark from selected memos."""

        return build_report_source_watermark(
            report_type=report_type,
            selected_memos=selected_memos,
            include_candidate_findings=include_candidate_findings,
        )

    def _chat_message_marker(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> dict[str, int | None]:
        row = self._scoped_query(
            ChatMessage,
            func.max(ChatMessage.id).label("latest_id"),
            func.max(ChatMessage.turn_number).label("latest_turn_number"),
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        ).one()
        return {
            "latest_id": _int_or_none(row.latest_id),
            "latest_turn_number": _int_or_none(row.latest_turn_number),
        }

    def _chat_turn_event_marker(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> dict[str, int | None]:
        row = (
            self._scoped_query(
                ChatTurnEvent,
                ChatTurnEvent.turn_number,
                ChatTurnEvent.phase_sequence,
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            )
            .order_by(
                ChatTurnEvent.turn_number.desc(),
                ChatTurnEvent.phase_sequence.desc(),
                ChatTurnEvent.id.desc(),
            )
            .first()
        )
        return {
            "latest_turn_number": _int_or_none(row.turn_number) if row is not None else None,
            "latest_phase_sequence": _int_or_none(row.phase_sequence) if row is not None else None,
        }

    def _tool_execution_marker(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> dict[str, str | None]:
        row = (
            self._scoped_query(
                ToolExecution,
                ToolExecution.id,
                ToolExecution.created_at,
                ToolExecution.finished_at,
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            )
            .order_by(
                func.coalesce(ToolExecution.finished_at, ToolExecution.created_at).desc(),
                ToolExecution.created_at.desc(),
                ToolExecution.id.desc(),
            )
            .first()
        )
        return {
            "latest_id": str(row.id) if row is not None and row.id is not None else None,
            "latest_created_at": _datetime_to_json(row.created_at) if row is not None else None,
            "latest_finished_at": _datetime_to_json(row.finished_at) if row is not None else None,
        }

    def _latest_timestamp_marker(
        self,
        model: Any,
        timestamp_column: Any,
        output_key: str,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> dict[str, str | None]:
        row = self._scoped_query(
            model,
            func.max(timestamp_column).label("latest_timestamp"),
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        ).one()
        return {output_key: _datetime_to_json(row.latest_timestamp)}

    def _scoped_query(
        self,
        model: Any,
        *entities: Any,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> Any:
        query = (
            self._db.query(*entities)
            .select_from(model)
            .join(Task, Task.id == model.task_id)
            .filter(
                model.tenant_id == int(tenant_id),
                model.task_id == int(task_id),
                Task.tenant_id == int(tenant_id),
                Task.user_id == int(user_id),
                Task.engagement_id == int(engagement_id),
            )
        )
        if hasattr(model, "user_id"):
            query = query.filter(model.user_id == int(user_id))
        if hasattr(model, "engagement_id"):
            query = query.filter(model.engagement_id == int(engagement_id))
        return query


def _datetime_to_json(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _int_or_none(value: Any) -> int | None:
    return int(value) if value is not None else None


def _contains_source_value(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_source_value(item) for item in value.values())
    return value is not None


def build_report_source_watermark(
    *,
    report_type: str,
    selected_memos: Sequence[ReportSourceMemoWatermarkInput],
    include_candidate_findings: bool,
) -> dict[str, Any]:
    """Return a stable JSON-ready report watermark and hash."""

    normalized_report_type = validate_report_type(report_type)
    memo_snapshots = _ordered_report_memo_snapshots(selected_memos)
    payload: dict[str, Any] = {
        "schema_version": _REPORT_SOURCE_SCHEMA_VERSION,
        "report_type": normalized_report_type,
        "candidate_policy": {
            "include_candidate_findings": bool(include_candidate_findings)
        },
        "selected_memos": memo_snapshots,
    }
    return {
        **payload,
        "hash_algorithm": _REPORT_SOURCE_HASH_ALGORITHM,
        "hash": _stable_json_hash(payload),
    }


def build_report_source_generation_metadata(
    source_watermark: Mapping[str, Any],
) -> dict[str, Any]:
    """Return final-report metadata keys derived from a report watermark."""

    return {
        GENERATION_METADATA_SOURCE_WATERMARK_SCHEMA_VERSION_KEY: source_watermark.get(
            "schema_version"
        ),
        GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY: source_watermark.get("hash"),
    }


def _ordered_report_memo_snapshots(
    selected_memos: Sequence[ReportSourceMemoWatermarkInput],
) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    seen_memo_ids: set[str] = set()
    for selected_memo in selected_memos:
        memo_id = str(selected_memo.memo_id).strip()
        if not memo_id:
            raise ValueError("selected memo ID is required")
        if memo_id in seen_memo_ids:
            raise ValueError("selected memo IDs must be unique")
        seen_memo_ids.add(memo_id)
        snapshots.append(
            {
                "memo_id": memo_id,
                "version": int(selected_memo.version),
                "source_watermark": _json_ready(selected_memo.source_watermark),
            }
        )
    return sorted(snapshots, key=lambda item: item["memo_id"])


def _stable_json_hash(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        _json_ready(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json_ready(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _json_ready(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list | tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, int | float | str | bool) or value is None:
        return value
    return str(value)


__all__ = [
    "ReportSourceMemoWatermarkInput",
    "SourceWatermarkService",
    "build_report_source_generation_metadata",
    "build_report_source_watermark",
]
