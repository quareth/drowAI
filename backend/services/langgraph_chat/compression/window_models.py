"""
Typed contracts for chat-scoped context window decisions and snapshots.

This module defines stable data models used by application-layer services to
represent context occupancy and ceiling evaluation outcomes keyed by
``(task_id, conversation_id)``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional

RecommendedNextAction = Literal["none", "compress"]
ContextWindowSnapshotKind = Literal["measured", "bootstrap_estimate"]


@dataclass(slots=True, frozen=True)
class ContextWindowSnapshot:
    """Current context-window occupancy for one task conversation."""

    task_id: int
    conversation_id: str
    max_tokens: int
    used_tokens: int
    remaining_tokens: int
    ratio: float
    ceiling_reached: bool
    recommended_next_action: RecommendedNextAction = "none"
    compression_candidate: bool = False
    turn_sequence: Optional[int] = None
    revision: int = -1
    snapshot_kind: ContextWindowSnapshotKind = "bootstrap_estimate"


def measured_snapshot_revision(turn_sequence: int) -> int:
    """Use the canonical non-negative turn sequence as snapshot revision."""

    if isinstance(turn_sequence, bool) or not isinstance(turn_sequence, int):
        raise ValueError("turn_sequence must be a non-negative integer")
    if turn_sequence < 0:
        raise ValueError("turn_sequence must be a non-negative integer")
    return turn_sequence


def parse_persisted_measured_snapshot(
    payload: Any,
    *,
    task_id: int,
    fallback_conversation_id: str,
) -> Optional[ContextWindowSnapshot]:
    """Parse only canonical measured snapshots from workflow JSON metadata."""

    source = _canonical_measured_snapshot_source(
        payload,
        expected_conversation_id=fallback_conversation_id,
    )
    if source is None:
        return None

    return ContextWindowSnapshot(
        task_id=task_id,
        conversation_id=source["conversation_id"],
        max_tokens=source["max_tokens"],
        used_tokens=source["used_tokens"],
        remaining_tokens=source["remaining_tokens"],
        ratio=source["ratio"],
        ceiling_reached=source["ceiling_reached"],
        recommended_next_action=source["recommended_next_action"],
        compression_candidate=source["compression_candidate"],
        turn_sequence=source["turn_sequence"],
        revision=source["revision"],
        snapshot_kind="measured",
    )


def canonical_measured_snapshot_revision(
    payload: Any,
    *,
    expected_conversation_id: Optional[str] = None,
) -> Optional[int]:
    """Return the revision only when a complete measured snapshot is canonical."""

    source = _canonical_measured_snapshot_source(
        payload,
        expected_conversation_id=expected_conversation_id,
    )
    return source["revision"] if source is not None else None


def _canonical_measured_snapshot_source(
    payload: Any,
    *,
    expected_conversation_id: Optional[str],
) -> Optional[dict[str, Any]]:
    """Validate and normalize the persisted measured-snapshot boundary."""

    if not isinstance(payload, Mapping):
        return None
    source = payload.get("context_window")
    if not isinstance(source, Mapping):
        source = payload
    if source.get("snapshot_kind") != "measured":
        return None

    turn_sequence = source.get("turn_sequence")
    revision = source.get("revision")
    if (
        isinstance(turn_sequence, bool)
        or not isinstance(turn_sequence, int)
        or turn_sequence < 0
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision != turn_sequence
    ):
        return None

    integer_fields: dict[str, int] = {}
    for field_name in ("max_tokens", "used_tokens", "remaining_tokens"):
        value = source.get(field_name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        integer_fields[field_name] = value
    if integer_fields["max_tokens"] <= 0:
        return None
    expected_remaining = max(
        0,
        integer_fields["max_tokens"] - integer_fields["used_tokens"],
    )
    if integer_fields["remaining_tokens"] != expected_remaining:
        return None

    ratio = source.get("ratio")
    if (
        isinstance(ratio, bool)
        or not isinstance(ratio, (int, float))
        or not math.isfinite(ratio)
        or ratio < 0
        or ratio > 1
        or not math.isclose(
            float(ratio),
            min(
                1.0,
                integer_fields["used_tokens"] / integer_fields["max_tokens"],
            ),
            rel_tol=1e-9,
            abs_tol=1e-12,
        )
    ):
        return None

    ceiling_reached = source.get("ceiling_reached")
    if not isinstance(ceiling_reached, bool):
        return None
    conversation_id = source.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        return None
    conversation_id = conversation_id.strip()
    if (
        expected_conversation_id is not None
        and conversation_id != expected_conversation_id
    ):
        return None
    recommended_next_action = source.get("recommended_next_action")
    if recommended_next_action not in {"none", "compress"}:
        return None
    compression_candidate = source.get("compression_candidate")
    if not isinstance(compression_candidate, bool):
        return None

    return {
        **integer_fields,
        "ratio": float(ratio),
        "ceiling_reached": ceiling_reached,
        "conversation_id": conversation_id,
        "recommended_next_action": recommended_next_action,
        "compression_candidate": compression_candidate,
        "turn_sequence": turn_sequence,
        "revision": revision,
    }


@dataclass(slots=True, frozen=True)
class ContextWindowDecision:
    """Deterministic policy decision for a projected context usage."""

    snapshot: ContextWindowSnapshot
    ceiling_reached: bool
    recommended_next_action: RecommendedNextAction = "none"
    compression_candidate: bool = False
