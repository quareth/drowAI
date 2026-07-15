"""Project task memo attempts into reporting input display state.

This module compares persisted task closure memo watermarks with current
source watermarks. It performs no routing, LLM calls, generation, or workspace
reads.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from typing import Any

from sqlalchemy.orm import Session

from backend.models.reporting import TaskClosureMemo
from backend.repositories.reporting.task_closure_memo_repository import (
    TaskClosureMemoRepository,
)
from backend.services.reporting.contracts import (
    INPUT_STATE_FAILED,
    INPUT_STATE_NOT_PREPARED,
    INPUT_STATE_PREPARING,
    INPUT_STATE_READY,
    INPUT_STATE_STALE,
    MEMO_STATUS_FAILED,
    MEMO_STATUS_PREPARING,
    InputState,
    MemoStatus,
    validate_memo_status,
)


@dataclass(frozen=True, slots=True)
class MemoInputStateProjection:
    """Projected memo input state for one task inventory row."""

    input_state: InputState
    current_memo: TaskClosureMemo | None
    latest_attempt: TaskClosureMemo | None
    latest_attempt_status: MemoStatus | None
    current_source_watermark: dict[str, Any]
    current_memo_stale: bool


class ReportingStateService:
    """Compute memo currentness state from durable memo rows and watermarks."""

    def __init__(
        self,
        db: Session,
        *,
        repository: TaskClosureMemoRepository | None = None,
    ) -> None:
        self._repository = repository or TaskClosureMemoRepository(db)

    def project_memo_input_state(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
        current_source_watermark: Mapping[str, Any],
    ) -> MemoInputStateProjection:
        """Return the display state for one task's memo input."""

        current_ready_memo = self._repository.get_current_ready_memo(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        )
        latest_attempt = self._repository.get_latest_memo_attempt(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        )
        latest_attempt_status = _memo_status(latest_attempt)
        normalized_current_watermark = _normalize_mapping(current_source_watermark)

        if current_ready_memo is not None:
            current_memo_stale = not watermarks_match(
                current_ready_memo.source_watermark,
                normalized_current_watermark,
            )
            return MemoInputStateProjection(
                input_state=INPUT_STATE_STALE if current_memo_stale else INPUT_STATE_READY,
                current_memo=current_ready_memo,
                latest_attempt=latest_attempt,
                latest_attempt_status=latest_attempt_status,
                current_source_watermark=normalized_current_watermark,
                current_memo_stale=current_memo_stale,
            )

        if latest_attempt is None:
            input_state: InputState = INPUT_STATE_NOT_PREPARED
        elif latest_attempt_status == MEMO_STATUS_PREPARING:
            input_state = INPUT_STATE_PREPARING
        elif latest_attempt_status == MEMO_STATUS_FAILED:
            input_state = INPUT_STATE_FAILED
        else:
            input_state = INPUT_STATE_NOT_PREPARED

        return MemoInputStateProjection(
            input_state=input_state,
            current_memo=None,
            latest_attempt=latest_attempt,
            latest_attempt_status=latest_attempt_status,
            current_source_watermark=normalized_current_watermark,
            current_memo_stale=False,
        )


def watermarks_match(
    stored_source_watermark: Mapping[str, Any] | None,
    current_source_watermark: Mapping[str, Any] | None,
) -> bool:
    """Return whether two JSON-like source watermarks are canonically equal."""

    return _canonical_json(stored_source_watermark or {}) == _canonical_json(
        current_source_watermark or {}
    )


def _memo_status(memo: TaskClosureMemo | None) -> MemoStatus | None:
    if memo is None:
        return None
    return validate_memo_status(str(memo.status))


def _normalize_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _normalize_json_value(value)
    if isinstance(normalized, dict):
        return normalized
    return {}


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        _normalize_json_value(value),
        sort_keys=True,
        separators=(",", ":"),
    )


def _normalize_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_normalize_json_value(item) for item in value]
    return value


__all__ = [
    "MemoInputStateProjection",
    "ReportingStateService",
    "watermarks_match",
]
