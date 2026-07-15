"""Result interpretation and deterministic completion-preparation helpers.

This module owns behavior-preserving result shaping extracted from
``turn_execution_service.py``:
- final content extraction/validation
- turn identity fallback resolution
- completion metadata and stream sequence shaping
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from backend.services.chat.turn_identity_resolver import (
    resolve_turn_identity_from_reserved_message_best_effort,
)
from backend.services.chat.event_builders import attach_conversation_ids


class TurnExecutionResultService:
    """Service for deterministic result interpretation across turn flows."""

    @staticmethod
    def extract_final_content(
        *,
        result: Any,
        failure_message: str,
    ) -> str:
        """Read and validate final assistant text from facade result."""
        final_content = (getattr(result, "final_text", "") or "").strip()
        if not final_content:
            raise RuntimeError(failure_message)
        return final_content

    @staticmethod
    def resolve_turn_identity_from_result(
        *,
        task_id: int,
        metadata: Optional[Dict[str, Any]],
        reserved_message_id: Optional[int],
        fallback_turn_id: Optional[str],
        fallback_turn_sequence: Optional[int],
    ) -> Tuple[Optional[str], Optional[int]]:
        """Derive turn identity from metadata, then reserved-message fallback."""
        resolved_turn_id = fallback_turn_id if isinstance(fallback_turn_id, str) and fallback_turn_id.strip() else None
        resolved_turn_sequence = fallback_turn_sequence if isinstance(fallback_turn_sequence, int) else None
        if isinstance(metadata, dict):
            result_turn_id = metadata.get("id")
            if isinstance(result_turn_id, str) and result_turn_id.strip():
                resolved_turn_id = result_turn_id.strip()
            result_turn_sequence = metadata.get("turn_sequence")
            if isinstance(result_turn_sequence, int):
                resolved_turn_sequence = result_turn_sequence
        if (resolved_turn_id is None or resolved_turn_sequence is None) and reserved_message_id is not None:
            lookup_turn_id, lookup_turn_sequence = resolve_turn_identity_from_reserved_message_best_effort(
                task_id=task_id,
                reserved_message_id=reserved_message_id,
            )
            if resolved_turn_id is None:
                resolved_turn_id = lookup_turn_id
            if resolved_turn_sequence is None:
                resolved_turn_sequence = lookup_turn_sequence
        return resolved_turn_id, resolved_turn_sequence

    @staticmethod
    def build_start_completion_metadata(
        *,
        result_metadata: Any,
        conversation_id: str,
        anchor_sequence: Optional[int],
        turn_sequence: Optional[int],
    ) -> Tuple[Dict[str, Any], Optional[int], Optional[int]]:
        """Shape start-flow completion metadata and resolve stream sequence."""
        completion_metadata = result_metadata or attach_conversation_ids(
            {"role": "assistant", "streaming": False},
            conversation_id,
        )
        if anchor_sequence is not None:
            completion_metadata["sequence"] = anchor_sequence
        completion_metadata.setdefault("turn_sequence", turn_sequence)
        stream_sequence = (
            completion_metadata.get("turn_sequence")
            if isinstance(completion_metadata.get("turn_sequence"), int)
            else completion_metadata.get("sequence")
        )
        resolved_stream_sequence = stream_sequence if isinstance(stream_sequence, int) else None
        boundary_turn_sequence = (
            resolved_stream_sequence if resolved_stream_sequence is not None else turn_sequence
        )
        return completion_metadata, resolved_stream_sequence, boundary_turn_sequence

    @staticmethod
    def build_completion_metadata(
        *,
        result_metadata: Dict[str, Any],
        conversation_id: str,
        turn_id: Optional[str],
        turn_sequence: Optional[int],
    ) -> Tuple[Dict[str, Any], Optional[int]]:
        """Shape resume/retry completion metadata and resolve stream sequence."""
        completion_metadata = attach_conversation_ids(
            result_metadata,
            conversation_id,
        )
        completion_metadata.setdefault("role", "assistant")
        completion_metadata.setdefault("streaming", False)
        if isinstance(turn_id, str):
            completion_metadata.setdefault("id", turn_id)
        completion_metadata.setdefault("turn_sequence", turn_sequence)
        stream_sequence = completion_metadata.get("turn_sequence")
        return completion_metadata, stream_sequence if isinstance(stream_sequence, int) else None
