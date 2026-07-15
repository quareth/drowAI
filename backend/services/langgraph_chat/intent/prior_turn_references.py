"""Materialize classifier prior-turn hints against canonical chat rows.

This module keeps classifier-produced prior-turn references as resolver
hints. It never treats classifier anchor text as canonical transcript
content; resolved output always copies text from ``ChatMessage`` rows.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Mapping, Optional

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    empty_prior_turn_references_context,
    update_prior_turn_references,
)
from agent.graph.context.transcript import select_recent_transcript_window
from backend.services.langgraph_chat.facade_helpers import coerce_turn_sequence

if TYPE_CHECKING:
    from backend.services.chat.conversation_history_reader import ConversationHistoryReader
    from backend.services.langgraph_chat.contracts import LangGraphRuntimeConfig


METADATA_KEY_PRIOR_TURN_REFERENCES = "prior_turn_references"
logger = logging.getLogger("backend.services.langgraph_chat.facade")

_USER_MESSAGE_TYPES = {"user", "user_input", "user_message"}
_ASSISTANT_MESSAGE_TYPES = {"assistant", "assistant_message"}
_SYSTEM_MESSAGE_TYPES = {"system"}
_TOOL_MESSAGE_TYPES = {"tool"}
_ROLE_ORDER = {"system": 0, "user": 1, "assistant": 2, "tool": 3}


def _message_role(row: Any) -> Optional[str]:
    """Map persisted message_type values to prompt-facing speaker roles."""
    message_type = str(getattr(row, "message_type", "") or "").strip().lower()
    if message_type in _USER_MESSAGE_TYPES:
        return "user"
    if message_type in _ASSISTANT_MESSAGE_TYPES:
        return "assistant"
    if message_type in _SYSTEM_MESSAGE_TYPES:
        return "system"
    if message_type in _TOOL_MESSAGE_TYPES:
        return "tool"
    return None


def _message_text(row: Any) -> str:
    """Return canonical message text from a persisted row."""
    return str(getattr(row, "message", "") or "")


def _normalize_text(value: Any) -> str:
    """Normalize text for deterministic anchor matching."""
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _hint_confidence(hint: Mapping[str, Any]) -> Optional[float]:
    """Return a valid classifier confidence value when present."""
    confidence = hint.get("confidence")
    if isinstance(confidence, bool):
        return None
    if isinstance(confidence, (int, float)) and 0.0 <= float(confidence) <= 1.0:
        return float(confidence)
    return None


def _prior_turn_hint_confidence(value: Any) -> Optional[float]:
    """Normalize a prior-turn hint confidence value."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and 0.0 <= float(value) <= 1.0:
        return float(value)
    return None


def _unresolved_prior_turn_reference_context(
    reference: Dict[str, Any],
) -> Dict[str, Any]:
    """Return a stable unresolved prior-turn reference payload."""
    unresolved_hints = []
    raw_hints = reference.get("hints")
    if isinstance(raw_hints, list):
        for hint in raw_hints:
            if isinstance(hint, dict):
                unresolved_hints.append(
                    {
                        "status": "unresolved",
                        "reference_kind": str(hint.get("reference_kind") or "unknown"),
                        "turn_number": hint.get("turn_number"),
                        "speaker": hint.get("speaker"),
                        "anchor_text": hint.get("anchor_text"),
                        "reason": hint.get("reason"),
                        "confidence": _prior_turn_hint_confidence(
                            hint.get("confidence")
                        ),
                    }
                )
            else:
                unresolved_hints.append(
                    {"status": "unresolved", "reference_kind": "unknown"}
                )
    return {
        **empty_prior_turn_references_context(),
        "operation": reference.get("operation") or "reference_resolution",
        "status": "unresolved",
        "unresolved_hints": unresolved_hints,
    }


class PriorTurnReferenceMaterializer:
    """Resolve prior-turn reference hints against canonical chat messages."""

    def materialize(
        self,
        *,
        prior_turn_reference: Mapping[str, Any] | None,
        chat_messages: Iterable[Any],
        prompt_messages: Iterable[Mapping[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        """Build materialized prior-turn context from canonical chat rows."""
        reference = (
            prior_turn_reference if isinstance(prior_turn_reference, Mapping) else {}
        )
        if reference.get("required") is not True:
            return empty_prior_turn_references_context()

        rows = self._indexed_rows(chat_messages, prompt_messages=prompt_messages)
        operation = (
            str(reference.get("operation") or "reference_resolution").strip()
            or "reference_resolution"
        )
        hints = reference.get("hints")
        if not isinstance(hints, list):
            hints = []

        materialized_by_id: Dict[int, Dict[str, Any]] = {}
        unresolved_hints: List[Dict[str, Any]] = []

        for hint in hints:
            if not isinstance(hint, Mapping):
                unresolved_hints.append(
                    {"status": "unresolved", "reference_kind": "unknown"}
                )
                continue

            match = self._resolve_hint(hint, rows)
            if match is None:
                unresolved_hints.append(
                    self._unresolved_hint(hint, status="unresolved")
                )
                continue
            if isinstance(match, list):
                unresolved_hints.append(self._unresolved_hint(hint, status="ambiguous"))
                continue

            for item in self._expand_to_turn_context(match, rows, hint):
                row_id = item["message_id"]
                if row_id not in materialized_by_id:
                    materialized_by_id[row_id] = item

        materialized_turns = sorted(
            materialized_by_id.values(),
            key=lambda item: (item["order_index"], item["message_id"]),
        )
        for item in materialized_turns:
            item.pop("order_index", None)

        if not materialized_turns:
            status = "unresolved"
        elif unresolved_hints:
            status = "partial"
        else:
            status = "ok"

        return {
            "operation": operation,
            "status": status,
            "materialized_turns": materialized_turns,
            "unresolved_hints": unresolved_hints,
        }

    def materialize_for_runtime_config(
        self,
        runtime_config: "LangGraphRuntimeConfig",
        *,
        session_factory: Callable[[], Any],
        history_reader_factory: Callable[[Any], "ConversationHistoryReader"],
    ) -> None:
        """Resolve classifier prior-turn hints and write context into metadata.

        Args:
            runtime_config: Turn runtime configuration to update.
            session_factory: Factory for a database session.
            history_reader_factory: Factory that builds a conversation history reader.
        """
        metadata = runtime_config.metadata
        reference = metadata.get("intent_prior_turn_reference")
        if not isinstance(reference, dict) or reference.get("required") is not True:
            metadata[METADATA_KEY_PRIOR_TURN_REFERENCES] = (
                empty_prior_turn_references_context()
            )
            bundle = metadata.get(METADATA_CONTEXT_BUNDLE_KEY)
            if isinstance(bundle, dict):
                update_prior_turn_references(
                    bundle, metadata[METADATA_KEY_PRIOR_TURN_REFERENCES]
                )
            return

        conversation_id = runtime_config.chat_inputs.conversation_id
        if not conversation_id:
            metadata[METADATA_KEY_PRIOR_TURN_REFERENCES] = (
                _unresolved_prior_turn_reference_context(reference)
            )
            bundle = metadata.get(METADATA_CONTEXT_BUNDLE_KEY)
            if isinstance(bundle, dict):
                update_prior_turn_references(
                    bundle, metadata[METADATA_KEY_PRIOR_TURN_REFERENCES]
                )
            return

        db = session_factory()
        try:
            history_reader = history_reader_factory(db)
            chat_messages = history_reader.get_conversation_history(
                task_id=runtime_config.chat_inputs.task_id,
                conversation_id=conversation_id,
            )
            current_turn_number = coerce_turn_sequence(
                metadata.get("turn_number", metadata.get("turn_sequence"))
            )
            if current_turn_number is not None:
                chat_messages = [
                    row
                    for row in chat_messages
                    if getattr(row, "turn_number", None) != current_turn_number
                ]
            metadata[METADATA_KEY_PRIOR_TURN_REFERENCES] = self.materialize(
                prior_turn_reference=reference,
                chat_messages=chat_messages,
                prompt_messages=runtime_config.chat_inputs.history,
            )
            bundle = metadata.get(METADATA_CONTEXT_BUNDLE_KEY)
            if isinstance(bundle, dict):
                update_prior_turn_references(
                    bundle, metadata[METADATA_KEY_PRIOR_TURN_REFERENCES]
                )
        except Exception:
            logger.warning(
                "[FACADE] Failed to materialize prior-turn references for task %s",
                runtime_config.chat_inputs.task_id,
                exc_info=True,
            )
            metadata[METADATA_KEY_PRIOR_TURN_REFERENCES] = (
                _unresolved_prior_turn_reference_context(reference)
            )
            bundle = metadata.get(METADATA_CONTEXT_BUNDLE_KEY)
            if isinstance(bundle, dict):
                update_prior_turn_references(
                    bundle, metadata[METADATA_KEY_PRIOR_TURN_REFERENCES]
                )
        finally:
            close = getattr(db, "close", None)
            if callable(close):
                close()

    def _indexed_rows(
        self,
        chat_messages: Iterable[Any],
        *,
        prompt_messages: Iterable[Mapping[str, Any]] | None = None,
    ) -> List[Dict[str, Any]]:
        """Build canonical row indexes plus classifier-prompt turn numbers."""
        rows: List[Dict[str, Any]] = []
        row_prompt_messages: List[Dict[str, Any]] = []

        for order_index, row in enumerate(chat_messages):
            role = _message_role(row)
            if role is None:
                continue
            text = _message_text(row)
            if not text.strip():
                continue

            entry = {
                "row": row,
                "message_id": int(getattr(row, "id")),
                "turn_number": getattr(row, "turn_number", None),
                "speaker": role,
                "text": text,
                "normalized_text": _normalize_text(text),
                "order_index": order_index,
                "rendered_turn_number": None,
            }
            rows.append(entry)
            row_prompt_messages.append(
                {
                    "role": role,
                    "content": text,
                    "_message_id": entry["message_id"],
                }
            )

        if prompt_messages is not None:
            self._apply_rendered_turns_from_prompt_history(rows, prompt_messages)
            return rows

        window = select_recent_transcript_window(row_prompt_messages)
        rendered_by_id: Dict[int, int] = {}
        turn_index = int(window.get("dropped_older_turn_count") or 0)
        for message in window.get("turns") or []:
            if str(message.get("role") or "").strip().lower() == "user":
                turn_index += 1
            message_id = message.get("_message_id")
            if isinstance(message_id, int):
                rendered_by_id[message_id] = turn_index

        for entry in rows:
            entry["rendered_turn_number"] = rendered_by_id.get(entry["message_id"])
        return rows

    def _apply_rendered_turns_from_prompt_history(
        self,
        rows: List[Dict[str, Any]],
        prompt_messages: Iterable[Mapping[str, Any]],
    ) -> None:
        """Map classifier-visible turn numbers back onto canonical rows.

        The classifier sees the already-shaped OpenAI history that built the
        ``ConversationContextBundle``. That history may have been collapsed by
        a compression summary, so recomputing rendered turn numbers from raw
        ``ChatMessage`` rows can point at the wrong row. This method derives
        rendered turn numbers from the exact prompt history and reverse-aligns
        those visible messages against canonical rows by role/content.
        """
        visible_messages: List[Dict[str, Any]] = []
        for message in prompt_messages:
            role = str(message.get("role") or "").strip().lower()
            if not role:
                role = "unknown"
            content = str(message.get("content") or "")
            visible_messages.append({"role": role, "content": content})

        window = select_recent_transcript_window(visible_messages)
        rendered_sequence: List[tuple[str, str, int]] = []
        turn_index = int(window.get("dropped_older_turn_count") or 0)
        for message in window.get("turns") or []:
            role = str(message.get("role") or "").strip().lower()
            if role == "user":
                turn_index += 1
            content = _normalize_text(message.get("content"))
            if role in _ROLE_ORDER and content:
                rendered_sequence.append((role, content, turn_index))

        if not rendered_sequence:
            return

        sequence_index = len(rendered_sequence) - 1
        for row in reversed(rows):
            if sequence_index < 0:
                break
            target_role, target_text, rendered_turn = rendered_sequence[sequence_index]
            if row["speaker"] == target_role and row["normalized_text"] == target_text:
                row["rendered_turn_number"] = rendered_turn
                sequence_index -= 1

    def _resolve_hint(
        self,
        hint: Mapping[str, Any],
        rows: List[Dict[str, Any]],
    ) -> Dict[str, Any] | List[Dict[str, Any]] | None:
        """Resolve one hint, returning ``list`` only for ambiguous matches."""
        reference_kind = str(hint.get("reference_kind") or "unknown").strip().lower()
        if reference_kind == "rendered_turn":
            return self._resolve_rendered_turn_hint(hint, rows)
        if reference_kind == "relative_turn":
            return self._resolve_relative_turn_hint(hint, rows)
        if reference_kind == "anchor_text":
            return self._resolve_anchor_hint(hint, rows)
        return None

    def _resolve_rendered_turn_hint(
        self,
        hint: Mapping[str, Any],
        rows: List[Dict[str, Any]],
    ) -> Dict[str, Any] | List[Dict[str, Any]] | None:
        """Resolve a hint that names a rendered ``<turn n=...>`` block."""
        turn_number = hint.get("turn_number")
        if not isinstance(turn_number, int) or isinstance(turn_number, bool):
            return self._resolve_anchor_hint(hint, rows)

        candidates = [
            row for row in rows if row.get("rendered_turn_number") == turn_number
        ]
        speaker = self._hint_speaker(hint)
        if speaker is not None:
            candidates = [row for row in candidates if row["speaker"] == speaker]
        if not candidates:
            return self._resolve_anchor_hint(hint, rows)
        return self._select_candidate(candidates, hint, matched_by="rendered_turn")

    def _resolve_relative_turn_hint(
        self,
        hint: Mapping[str, Any],
        rows: List[Dict[str, Any]],
    ) -> Dict[str, Any] | List[Dict[str, Any]] | None:
        """Resolve relative phrases such as last assistant or first user."""
        anchor_match = self._resolve_anchor_hint(hint, rows)
        if anchor_match is not None:
            return anchor_match

        descriptor = _normalize_text(
            " ".join(
                str(part or "")
                for part in (hint.get("anchor_text"), hint.get("reason"))
            )
        )
        speaker = self._hint_speaker(hint)
        if speaker is None:
            if "assistant" in descriptor:
                speaker = "assistant"
            elif "user" in descriptor:
                speaker = "user"

        if speaker is None:
            return None

        speaker_rows = [row for row in rows if row["speaker"] == speaker]
        if not speaker_rows:
            return None
        if "first" in descriptor:
            return self._materialized_turn(
                speaker_rows[0], hint, matched_by="relative_turn"
            )
        if "previous" in descriptor or "last" in descriptor or "prior" in descriptor:
            return self._materialized_turn(
                speaker_rows[-1], hint, matched_by="relative_turn"
            )
        return None

    def _resolve_anchor_hint(
        self,
        hint: Mapping[str, Any],
        rows: List[Dict[str, Any]],
    ) -> Dict[str, Any] | List[Dict[str, Any]] | None:
        """Resolve a hint by exact normalized anchor substring matching."""
        anchor = _normalize_text(hint.get("anchor_text"))
        if not anchor:
            return None
        candidates = [row for row in rows if anchor in row["normalized_text"]]
        speaker = self._hint_speaker(hint)
        if speaker is not None:
            candidates = [row for row in candidates if row["speaker"] == speaker]
        return self._select_candidate(candidates, hint, matched_by="anchor_text")

    def _select_candidate(
        self,
        candidates: List[Dict[str, Any]],
        hint: Mapping[str, Any],
        *,
        matched_by: str,
    ) -> Dict[str, Any] | List[Dict[str, Any]] | None:
        """Return one materialized row, ambiguous candidates, or ``None``."""
        if not candidates:
            return None
        if len(candidates) == 1:
            return self._materialized_turn(candidates[0], hint, matched_by=matched_by)
        anchor = _normalize_text(hint.get("anchor_text"))
        if anchor:
            exact = [row for row in candidates if row["normalized_text"] == anchor]
            if len(exact) == 1:
                return self._materialized_turn(exact[0], hint, matched_by=matched_by)
        return candidates

    def _materialized_turn(
        self,
        row: Mapping[str, Any],
        hint: Mapping[str, Any],
        *,
        matched_by: str,
    ) -> Dict[str, Any]:
        """Build one canonical materialized turn payload."""
        return {
            "turn_number": row.get("turn_number"),
            "rendered_turn_number": row.get("rendered_turn_number"),
            "speaker": row["speaker"],
            "message_id": row["message_id"],
            "text": row["text"],
            "matched_by": matched_by,
            "classifier_confidence": _hint_confidence(hint),
            "order_index": row["order_index"],
        }

    def _expand_to_turn_context(
        self,
        match: Mapping[str, Any],
        rows: List[Dict[str, Any]],
        hint: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        """Expand a resolved row to the full user/assistant turn context."""
        turn_number = match.get("turn_number")
        matched_by = str(match.get("matched_by") or "prior_turn_reference")
        candidates: List[Dict[str, Any]] = []
        if turn_number is not None:
            candidates = [
                row
                for row in rows
                if row.get("turn_number") == turn_number
                and row.get("speaker") in {"user", "assistant"}
            ]
        else:
            rendered_turn_number = match.get("rendered_turn_number")
            if rendered_turn_number is not None:
                candidates = [
                    row
                    for row in rows
                    if row.get("rendered_turn_number") == rendered_turn_number
                    and row.get("speaker") in {"user", "assistant"}
                ]

        if not candidates:
            return [dict(match)]

        return [
            self._materialized_turn(row, hint, matched_by=matched_by)
            for row in sorted(
                candidates,
                key=lambda item: (item["order_index"], item["message_id"]),
            )
        ]

    def _unresolved_hint(
        self, hint: Mapping[str, Any], *, status: str
    ) -> Dict[str, Any]:
        """Preserve resolver metadata for hints that could not materialize."""
        return {
            "status": status,
            "reference_kind": str(hint.get("reference_kind") or "unknown"),
            "turn_number": hint.get("turn_number"),
            "speaker": hint.get("speaker"),
            "anchor_text": hint.get("anchor_text"),
            "reason": hint.get("reason"),
            "confidence": _hint_confidence(hint),
        }

    def _hint_speaker(self, hint: Mapping[str, Any]) -> Optional[str]:
        """Return a normalized hint speaker when specific enough."""
        speaker = str(hint.get("speaker") or "").strip().lower()
        if speaker in _ROLE_ORDER and speaker != "unknown":
            return speaker
        return None


__all__ = [
    "METADATA_KEY_PRIOR_TURN_REFERENCES",
    "PriorTurnReferenceMaterializer",
    "_prior_turn_hint_confidence",
    "_unresolved_prior_turn_reference_context",
]
