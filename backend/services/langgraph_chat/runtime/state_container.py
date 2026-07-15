"""ChatStateContainer: state accumulation during streaming.

Accumulates answer deltas, reasoning deltas, observation deltas, and completed tool calls during
graph execution so handlers can persist via ChatMessageService at turn completion.

: State Container & Handler Integration."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


# Tool call payload for add_tool_call / get_tool_calls (ChatMessageService-compatible)
ToolCallInfo = Dict[str, Any]
ObservationInfo = Dict[str, Any]
ReasoningInfo = Dict[str, Any]


@dataclass
class _PendingObservation:
    """In-flight observation chunks for the active section."""

    chunks: List[str]
    sub_turn_index: Optional[int] = None


@dataclass
class _StoredObservation:
    """Finalized observation section ready for persistence."""

    content: str
    phase_sequence: int
    sub_turn_index: Optional[int] = None


@dataclass
class _PendingReasoning:
    """In-flight reasoning chunks for the active reasoning section."""

    chunks: List[str]
    phase_sequence: int
    reasoning_section_id: str
    section_name: Optional[str] = None
    sub_turn_index: Optional[int] = None
    started_at: Optional[float] = None


@dataclass
class _StoredReasoningSection:
    """Finalized reasoning section ready for persistence."""

    content: str
    phase_sequence: int
    reasoning_section_id: str
    section_name: Optional[str] = None
    sub_turn_index: Optional[int] = None
    started_at: Optional[float] = None
    ended_at: Optional[float] = None


def _coerce_timestamp(value: Any) -> Optional[float]:
    """Normalize an optional Unix-seconds timestamp."""
    if isinstance(value, (int, float)):
        numeric = float(value)
        if math.isfinite(numeric) and numeric >= 0:
            return numeric
    return None


class ChatStateContainer:
    """Accumulates streaming state for a single turn (answer, reasoning, observations, tool calls).

    Thread-safe container used by the streaming adapter to accumulate state
    during graph execution. Handlers pass the container to the executor and
    then use get_* methods to update the reserved ChatMessage via ChatMessageService.

    Interface (from Approach §3.2):
    - append_answer(delta) - Accumulate answer deltas
    - append_reasoning(delta) - Accumulate reasoning deltas
    - start_observation() / append_observation(delta) / end_observation() - Accumulate observation sections
    - record_tool_call_start(tool_call_id, parameters) - Cache parameters from tool_start
    - add_tool_call(tool_call) - Add completed tool call (fills parameters, assigns turn_index)
    - get_answer_tokens() - Get accumulated answer
    - get_reasoning_tokens() - Get accumulated reasoning
    - get_observation_tokens() - Get accumulated observations in encounter order
    - get_tool_calls() - Get all tool calls
    """

    def __init__(self, reserved_message_id: Optional[int] = None) -> None:
        self._answer: List[str] = []
        self._reasoning: List[str] = []
        self._reasoning_sections: List[_StoredReasoningSection] = []
        self._current_reasoning: Optional[_PendingReasoning] = None
        self._observations: List[_StoredObservation] = []
        self._current_observation: Optional[_PendingObservation] = None
        self._tool_calls: List[ToolCallInfo] = []
        self._tool_call_params: Dict[str, Dict[str, Any]] = {}
        self._tool_call_counter = 0
        self._phase_sequence_counter = 0
        self._used_phase_sequences: set[int] = set()
        self.reserved_message_id = reserved_message_id
        self._lock = threading.Lock()

    def _claim_phase_sequence_locked(self, proposed: Any = None) -> int:
        """Claim a unique per-turn phase sequence (lock must be held)."""
        phase_sequence: Optional[int] = None
        if isinstance(proposed, int) and proposed >= 0 and proposed not in self._used_phase_sequences:
            phase_sequence = proposed
        else:
            while self._phase_sequence_counter in self._used_phase_sequences:
                self._phase_sequence_counter += 1
            phase_sequence = self._phase_sequence_counter
        self._used_phase_sequences.add(phase_sequence)
        if phase_sequence >= self._phase_sequence_counter:
            self._phase_sequence_counter = phase_sequence + 1
        return phase_sequence

    @staticmethod
    def _build_reasoning_section_id(
        *,
        identity_scope: Optional[str],
        phase_sequence: int,
    ) -> str:
        """Build a stable reasoning section identity within a turn."""
        scope = (identity_scope or "turn").strip() if isinstance(identity_scope, str) else "turn"
        return f"{scope}:reasoning:{phase_sequence}"

    def _current_reasoning_identity_locked(self) -> Optional[ReasoningInfo]:
        """Return active reasoning section identity metadata (lock must be held)."""
        if self._current_reasoning is None:
            return None
        return {
            "phase_sequence": self._current_reasoning.phase_sequence,
            "reasoning_section_id": self._current_reasoning.reasoning_section_id,
            "section_name": self._current_reasoning.section_name,
            "sub_turn_index": self._current_reasoning.sub_turn_index,
        }

    def append_answer(self, delta: str) -> None:
        """Append a chunk of answer text."""
        if not delta:
            return
        with self._lock:
            self._answer.append(delta)

    def append_reasoning(self, delta: str) -> None:
        """Append a chunk of reasoning text.

        Appends to the legacy flat buffer for backward compatibility.
        If a structured reasoning section is active, also appends there.
        """
        if not delta:
            return
        with self._lock:
            self._reasoning.append(delta)
            if self._current_reasoning is not None:
                self._current_reasoning.chunks.append(delta)

    def start_reasoning(
        self,
        *,
        section_name: Optional[str] = None,
        sub_turn_index: Optional[int] = None,
        timestamp: Optional[float] = None,
        identity_scope: Optional[str] = None,
        phase_sequence: Any = None,
        reasoning_section_id: Optional[str] = None,
    ) -> ReasoningInfo:
        """Open a new structured reasoning section.

        If a previous reasoning section is still open it is finalized first.
        """
        with self._lock:
            started_at = _coerce_timestamp(timestamp)
            if started_at is None:
                started_at = time.time()
            if self._current_reasoning is not None:
                self._finalize_current_reasoning_locked(ended_at=started_at)
            claimed_phase_sequence = self._claim_phase_sequence_locked(phase_sequence)
            self._current_reasoning = _PendingReasoning(
                chunks=[],
                phase_sequence=claimed_phase_sequence,
                reasoning_section_id=(
                    reasoning_section_id
                    if isinstance(reasoning_section_id, str) and reasoning_section_id.strip()
                    else self._build_reasoning_section_id(
                        identity_scope=identity_scope,
                        phase_sequence=claimed_phase_sequence,
                    )
                ),
                section_name=section_name,
                sub_turn_index=sub_turn_index,
                started_at=started_at,
            )
            return self._current_reasoning_identity_locked() or {}

    def _finalize_current_reasoning_locked(self, *, ended_at: Optional[float] = None) -> None:
        """Finalize the active reasoning section (lock must be held)."""
        if self._current_reasoning is None:
            return
        reasoning_text = "".join(self._current_reasoning.chunks).strip()
        if reasoning_text:
            resolved_ended_at = _coerce_timestamp(ended_at)
            if resolved_ended_at is None:
                resolved_ended_at = time.time()
            self._reasoning_sections.append(
                _StoredReasoningSection(
                    content=reasoning_text,
                    phase_sequence=self._current_reasoning.phase_sequence,
                    reasoning_section_id=self._current_reasoning.reasoning_section_id,
                    section_name=self._current_reasoning.section_name,
                    sub_turn_index=self._current_reasoning.sub_turn_index,
                    started_at=self._current_reasoning.started_at,
                    ended_at=resolved_ended_at,
                )
            )
        self._current_reasoning = None

    def end_reasoning(
        self,
        sub_turn_index: Optional[int] = None,
        *,
        timestamp: Optional[float] = None,
    ) -> Optional[ReasoningInfo]:
        """Finalize the current reasoning section."""
        with self._lock:
            if self._current_reasoning is not None and self._current_reasoning.sub_turn_index is None:
                self._current_reasoning.sub_turn_index = sub_turn_index
            identity = self._current_reasoning_identity_locked()
            self._finalize_current_reasoning_locked(ended_at=timestamp)
            return identity

    def get_current_reasoning_identity(self) -> Optional[ReasoningInfo]:
        """Return active reasoning section identity metadata."""
        with self._lock:
            return self._current_reasoning_identity_locked()

    def get_reasoning_sections(self) -> List[ReasoningInfo]:
        """Return finalized reasoning sections in encounter order.

        Each section is a dict with 'content', 'phase_sequence', and optional
        'section_name', 'sub_turn_index', 'started_at', and 'ended_at' keys,
        matching the ObservationInfo pattern used by get_observation_tokens()
        while preserving refresh-safe reasoning timing metadata.
        """
        with self._lock:
            self._finalize_current_reasoning_locked()
            sections: List[ReasoningInfo] = []
            for stored in self._reasoning_sections:
                section: ReasoningInfo = {
                    "content": stored.content,
                    "phase_sequence": stored.phase_sequence,
                    "reasoning_section_id": stored.reasoning_section_id,
                }
                if stored.section_name is not None:
                    section["section_name"] = stored.section_name
                if stored.sub_turn_index is not None:
                    section["sub_turn_index"] = stored.sub_turn_index
                if stored.started_at is not None:
                    section["started_at"] = stored.started_at
                if stored.ended_at is not None:
                    section["ended_at"] = stored.ended_at
                sections.append(section)
            return sections

    def _finalize_current_observation_locked(self) -> None:
        """Finalize the active observation (lock must be held)."""
        if self._current_observation is None:
            return
        observation_text = "".join(self._current_observation.chunks).strip()
        if observation_text:
            self._observations.append(
                _StoredObservation(
                    content=observation_text,
                    phase_sequence=self._claim_phase_sequence_locked(),
                    sub_turn_index=self._current_observation.sub_turn_index,
                )
            )
        self._current_observation = None

    def _replace_last_observation_locked(
        self,
        text: str,
        sub_turn_index: Optional[int],
    ) -> None:
        """Replace the latest stored observation when a trailing snapshot arrives."""
        if not text:
            return
        if self._observations:
            last = self._observations[-1]
            if sub_turn_index is None or last.sub_turn_index == sub_turn_index:
                last.content = text
                if sub_turn_index is not None:
                    last.sub_turn_index = sub_turn_index
                return
        self._observations.append(
            _StoredObservation(
                content=text,
                phase_sequence=self._claim_phase_sequence_locked(),
                sub_turn_index=sub_turn_index,
            )
        )

    def start_observation(self, sub_turn_index: Optional[int] = None) -> None:
        """Start a new observation accumulation section."""
        with self._lock:
            if self._current_observation is not None:
                self._finalize_current_observation_locked()
            self._current_observation = _PendingObservation(
                chunks=[],
                sub_turn_index=sub_turn_index,
            )

    def append_observation(
        self,
        delta: str,
        *,
        snapshot: bool = False,
        sub_turn_index: Optional[int] = None,
    ) -> None:
        """Append a chunk of observation text.

        Snapshot events carry the final articulated observation content and should
        replace any previously accumulated chunks for the current observation.
        """
        if not delta:
            return
        with self._lock:
            if snapshot:
                snapshot_text = delta.strip()
                if not snapshot_text:
                    return
                if self._current_observation is not None:
                    self._current_observation.chunks = [snapshot_text]
                    if sub_turn_index is not None:
                        self._current_observation.sub_turn_index = sub_turn_index
                    return
                self._replace_last_observation_locked(snapshot_text, sub_turn_index)
                return
            if self._current_observation is None:
                self._current_observation = _PendingObservation(
                    chunks=[],
                    sub_turn_index=sub_turn_index,
                )
            elif self._current_observation.sub_turn_index is None and sub_turn_index is not None:
                self._current_observation.sub_turn_index = sub_turn_index
            self._current_observation.chunks.append(delta)

    def end_observation(self, sub_turn_index: Optional[int] = None) -> None:
        """Finalize the current observation section."""
        with self._lock:
            if self._current_observation is not None and self._current_observation.sub_turn_index is None:
                self._current_observation.sub_turn_index = sub_turn_index
            self._finalize_current_observation_locked()

    def add_tool_call(self, tool_call: ToolCallInfo) -> ToolCallInfo:
        """Add a completed tool call (from tool_end event)."""
        if not tool_call:
            return {}
        with self._lock:
            stored = dict(tool_call)
            tool_call_id = stored.get("tool_call_id")
            if tool_call_id:
                cached_params = self._tool_call_params.get(str(tool_call_id))
                if cached_params and not stored.get("tool_arguments"):
                    stored["tool_arguments"] = dict(cached_params)
            if stored.get("turn_index") is None:
                stored["turn_index"] = self._tool_call_counter
            stored["phase_sequence"] = self._claim_phase_sequence_locked(
                stored.get("phase_sequence")
            )
            self._tool_call_counter += 1
            self._tool_calls.append(stored)
            return stored

    def record_tool_call_start(self, tool_call_id: Optional[str], parameters: Any) -> None:
        """Record tool parameters when tool_start is emitted."""
        if not tool_call_id:
            return
        if not isinstance(parameters, dict):
            return
        with self._lock:
            self._tool_call_params[str(tool_call_id)] = dict(parameters)

    def get_tool_call_parameters(self, tool_call_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """Return cached tool parameters for a tool_call_id."""
        if not tool_call_id:
            return None
        with self._lock:
            params = self._tool_call_params.get(str(tool_call_id))
            return dict(params) if params else None

    def get_answer_tokens(self) -> str:
        """Return full accumulated answer text."""
        with self._lock:
            return "".join(self._answer)

    def get_reasoning_tokens(self) -> str:
        """Return full accumulated reasoning text.

        Compatibility method: if structured reasoning sections exist, joins
        their content in encounter order. Otherwise falls back to the legacy
        flat delta buffer so callers that predate structured sections still
        work during the migration window.
        """
        with self._lock:
            self._finalize_current_reasoning_locked()
            if self._reasoning_sections:
                return "\n\n".join(s.content for s in self._reasoning_sections)
            return "".join(self._reasoning)

    def get_observation_tokens(self) -> List[ObservationInfo]:
        """Return observation sections in encounter order."""
        with self._lock:
            self._finalize_current_observation_locked()
            tokens: List[ObservationInfo] = []
            for section in self._observations:
                token: ObservationInfo = {
                    "content": section.content,
                    "phase_sequence": section.phase_sequence,
                }
                if section.sub_turn_index is not None:
                    token["sub_turn_index"] = section.sub_turn_index
                tokens.append(token)
            return tokens

    def get_tool_calls(self) -> List[ToolCallInfo]:
        """Return a copy of all tool calls added so far."""
        with self._lock:
            return list(self._tool_calls)


__all__ = ["ChatStateContainer", "ToolCallInfo", "ObservationInfo", "ReasoningInfo"]
