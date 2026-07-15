"""Current-turn phase-memory helper for post-tool reasoning continuity.

Purpose
-------
Owns the single contract, ordering, append, and compact rendering for the
*current-turn phase ledger* used by post-tool reasoning (PTR). Runtime code
stamps ``turn_sequence`` and ``phase_sequence`` on every record; the LLM
never generates these identity fields.

Responsibility boundary
-----------------------
This module is deliberately small and seam-local:

- defines the :class:`IterationMemoryRecord` TypedDict shape,
- reserves monotonic per-turn phase sequence values,
- appends ordered records into working-memory ledger storage,
- renders a compact, deterministic prompt section from those records.

It does **not** own:

- PTR orchestration (see ``agent/graph/nodes/post_tool_reasoning/``),
- tool runtime state projection (see
  ``agent/graph/subgraphs/tool_execution_runtime/result_state_projection.py``),
- prose history formatting (see ``history_formatter.py``),
- cross-turn persistence — records prior to the active turn are pruned
  at turn boundary inside ``MemoryManager.reduce_phase_ledger_append``;
  cross-turn persistence is intentionally not supported.

State keys
----------
The ledger lives in ``metadata["working_memory"]`` under three fields:

- ``current_turn_phases`` - ordered ``list[IterationMemoryRecord]``.
- ``current_turn_phase_counter`` - integer counter for the next
  ``phase_sequence`` to hand out within the active turn.
- ``current_turn_phase_turn`` - last turn the counter was scoped to;
  used to reset the counter on turn boundary.

``TraceState.observations`` is intentionally *not* widened by this helper:
prose observations remain ``List[str]`` and the ledger lives in working memory.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Mapping, Optional, TypedDict

from agent.graph.memory.memory_manager import MemoryManager


_WORKING_MEMORY_KEY = "working_memory"
_LEDGER_FIELD = "current_turn_phases"
_COUNTER_FIELD = "current_turn_phase_counter"
_TURN_FIELD = "current_turn_phase_turn"
PHASE_MEMORY_SECTION_HEADING = "## Prior Current-Turn Phase Memory"
LATEST_PHASE_MEMORY_SECTION_HEADING = "## Latest Current-Turn Phase"


PhaseMemorySource = Literal["tool", "ptr", "think_more", "reflect"]


class IterationMemoryRecord(TypedDict, total=False):
    """One ordered current-turn phase memory entry.

    ``turn_sequence``, ``phase_sequence``, and ``source`` are runtime-owned
    identity fields and must be stamped by runtime code (never by the LLM).
    ``sections`` stores the ordered prompt-section snapshot for that phase as
    compact ``{"heading": ..., "body": ...}`` entries without the leading
    markdown ``##`` prefix. The stored snapshot is the prompt-facing continuity
    payload; legacy semantic summary fields are no longer part of the record
    contract.
    """

    turn_sequence: int
    phase_sequence: int
    source: PhaseMemorySource
    sections: List[Dict[str, str]]


def _working_memory_view(metadata: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return read-only working-memory mapping view from metadata."""
    working_memory = metadata.get(_WORKING_MEMORY_KEY)
    if isinstance(working_memory, Mapping):
        return working_memory
    return {}


def _coerce_counter(value: Any) -> int:
    """Return a non-negative phase counter with 0 fallback."""
    if isinstance(value, int) and value >= 0:
        return value
    return 0


def _coerce_turn_scope(value: Any) -> Optional[int]:
    """Return an integer turn scope or ``None`` when unavailable."""
    if isinstance(value, int):
        return value
    return None


def _working_memory_previous(metadata: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return raw previous working-memory mapping for reducer inputs."""
    previous = metadata.get(_WORKING_MEMORY_KEY)
    return previous if isinstance(previous, Mapping) else None


def _replace_working_memory(metadata: Dict[str, Any], memory: Mapping[str, Any]) -> None:
    """Write reduced working-memory payload back onto metadata."""
    metadata[_WORKING_MEMORY_KEY] = dict(memory)


def _sanitize_sections(payload: Mapping[str, Any]) -> List[Dict[str, str]]:
    """Return ordered renderable section snapshots from a caller payload."""
    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, list):
        return []

    sanitized: List[Dict[str, str]] = []
    for raw_section in raw_sections:
        if not isinstance(raw_section, Mapping):
            continue

        heading = raw_section.get("heading")
        body = raw_section.get("body")
        if not isinstance(heading, str) or not isinstance(body, str):
            continue

        heading_text = heading.strip()
        body_text = body.strip()
        if not heading_text or not body_text:
            continue

        sanitized.append({"heading": heading_text, "body": body_text})

    return sanitized


def get_ledger(metadata: Dict[str, Any]) -> List[IterationMemoryRecord]:
    """Return a shallow copy of the current-turn phase ledger.

    Callers that only need to read (e.g. the prompt builder or the history
    formatter) should use this helper so they do not accidentally mutate
    stored state.
    """
    ledger = _working_memory_view(metadata).get(_LEDGER_FIELD)
    if not isinstance(ledger, list):
        return []
    return list(ledger)


def get_current_turn_scope(metadata: Mapping[str, Any]) -> Optional[int]:
    """Return the turn sequence that currently scopes the ledger counter."""
    working_memory = _working_memory_view(metadata)
    return _coerce_turn_scope(working_memory.get(_TURN_FIELD))


def reserve_next_phase_sequence(
    metadata: Dict[str, Any],
    *,
    turn_sequence: int,
) -> int:
    """Return the next phase_sequence for the active turn and persist the counter.

    The counter is monotonic within a single ``turn_sequence`` and resets
    when a new turn is observed. The counter value is *reserved*, meaning
    the caller owns the returned value whether or not they go on to append
    a record with it. This mirrors how streaming assigns per-turn phase
    ordinals.

    Args:
        metadata: The graph metadata dict (usually ``facts.metadata``).
        turn_sequence: Canonical runtime-owned turn ordinal.

    Returns:
        The reserved ``phase_sequence`` integer (0-indexed within the turn).
    """
    scoped_turn = get_current_turn_scope(metadata)
    if scoped_turn != turn_sequence:
        next_phase = 0
    else:
        next_phase = _coerce_counter(_working_memory_view(metadata).get(_COUNTER_FIELD))

    working_memory = MemoryManager.reduce_phase_ledger_reset(
        _working_memory_previous(metadata),
        turn_sequence=turn_sequence,
        next_phase_counter=next_phase + 1,
    )
    _replace_working_memory(metadata, working_memory)
    return next_phase


def peek_next_phase_sequence(
    metadata: Dict[str, Any],
    *,
    turn_sequence: int,
) -> int:
    """Return the next phase_sequence *without* reserving it.

    Used by the PTR node to compute ``current_ptr_phase_sequence`` for the
    prompt (the phase the PTR step is *about to* create) while leaving the
    actual reservation to the recorder that ultimately appends.
    """
    scoped_turn = get_current_turn_scope(metadata)
    if scoped_turn != turn_sequence:
        return 0
    return _coerce_counter(_working_memory_view(metadata).get(_COUNTER_FIELD))


def latest_recorded_phase_sequence(
    metadata: Dict[str, Any],
    *,
    turn_sequence: int,
) -> Optional[int]:
    """Return the most recent recorded phase_sequence for the active turn.

    Returns ``None`` when the ledger is empty for the active turn.
    """
    ledger = get_ledger(metadata)
    if not ledger:
        return None
    latest: Optional[int] = None
    for record in ledger:
        if record.get("turn_sequence") != turn_sequence:
            continue
        phase = record.get("phase_sequence")
        if isinstance(phase, int):
            if latest is None or phase > latest:
                latest = phase
    return latest


def append(
    metadata: Dict[str, Any],
    *,
    turn_sequence: int,
    source: PhaseMemorySource,
    payload: Dict[str, Any],
    phase_sequence: Optional[int] = None,
) -> IterationMemoryRecord:
    """Append one ordered record to the current-turn phase ledger.

    Runtime identity (``turn_sequence``, ``phase_sequence``, ``source``) is
    stamped here. ``sections`` comes from ``payload`` and is filtered against
    the :class:`IterationMemoryRecord` contract so stray semantic keys do not
    leak into the prompt ledger.

    Args:
        metadata: Graph metadata dict.
        turn_sequence: Canonical runtime-owned turn ordinal.
        source: ``"tool"`` for deterministic tool phase records,
            ``"ptr"`` for structured PTR phase memory payloads,
            ``"think_more"`` for think-more phase records, and ``"reflect"``
            for reflection phase records.
        payload: Section snapshot payload supplied by the caller. Unknown
            keys are dropped; identity keys in the payload are ignored in
            favor of runtime-stamped values.
        phase_sequence: Optional pre-reserved phase. When omitted, a fresh
            phase is derived from the active per-turn counter and persisted
            together with the append.

    Returns:
        The stored record (same dict reference held in the ledger).
    """
    section_payload = _sanitize_sections(payload)
    if not section_payload:
        raise ValueError(
            "iteration_memory.append requires at least one renderable section snapshot"
        )

    if phase_sequence is None:
        # Use the shared reservation path so turn-boundary reset/increment
        # semantics stay centralized.
        phase_sequence = reserve_next_phase_sequence(
            metadata,
            turn_sequence=turn_sequence,
        )

    record: Dict[str, Any] = {
        "turn_sequence": int(turn_sequence),
        "phase_sequence": int(phase_sequence),
        "source": source,
        "sections": section_payload,
    }

    working_memory = MemoryManager.reduce_phase_ledger_append(
        previous=_working_memory_previous(metadata),
        record=record,
        turn_sequence=int(turn_sequence),
        phase_sequence=int(phase_sequence),
    )
    _replace_working_memory(metadata, working_memory)

    ledger = working_memory.get(_LEDGER_FIELD)
    if isinstance(ledger, list) and ledger:
        stored = ledger[-1]
        if isinstance(stored, dict):
            return stored  # type: ignore[return-value]
    return record  # type: ignore[return-value]


def has_renderable_sections(payload: Mapping[str, Any]) -> bool:
    """Return True when payload carries at least one renderable section snapshot."""
    return bool(_sanitize_sections(payload))


# =============================================================================
# Rendering
# =============================================================================


def render(
    metadata: Dict[str, Any],
    *,
    turn_sequence: Optional[int] = None,
) -> str:
    """Render the current-turn phase ledger as ordered phase-tagged blocks.

    Records are rendered in chronological (insertion) order using the stored
    section order. When ``turn_sequence`` is provided, only records tagged with
    that turn are included; otherwise every record in the ledger is rendered.
    Returns an empty string when no matching records exist so callers can
    cleanly omit the section.
    """
    ledger = get_ledger(metadata)
    if not ledger:
        return ""

    blocks: List[str] = []
    for record in ledger:
        if turn_sequence is not None and record.get("turn_sequence") != turn_sequence:
            continue

        sections = _sanitize_sections(record)
        if not sections:
            continue

        section_bodies = [
            f"## {section['heading']}\n{section['body']}" for section in sections
        ]
        blocks.append(
            "\n".join(
                [
                    (
                        f"<phase turn={record.get('turn_sequence')} "
                        f"phase={record.get('phase_sequence')} "
                        f"source={record.get('source')}>"
                    ),
                    "\n\n".join(section_bodies),
                    "</phase>",
                ]
            )
        )

    return "\n".join(blocks)


def render_phase_memory_section(
    metadata: Dict[str, Any],
    *,
    turn_sequence: Optional[int] = None,
) -> str:
    """Render the full phase-memory prompt section when records exist.

    This helper keeps the markdown heading and body composition in one place so
    prompt builders can reuse identical section text without duplicating string
    literals.
    """
    body = render(metadata, turn_sequence=turn_sequence)
    if not body:
        return ""
    return f"{PHASE_MEMORY_SECTION_HEADING}\n{body}"


def render_latest_phase_memory_section(
    metadata: Dict[str, Any],
    *,
    turn_sequence: Optional[int] = None,
) -> str:
    """Render only the latest current-turn phase-memory block.

    Category and tool selection need the freshest runtime steering signal, but
    not the full phase ledger. This helper preserves the exact ``<phase ...>``
    render format from :func:`render` while narrowing the body to the record
    with the greatest ``phase_sequence`` in the requested turn.
    """
    ledger = get_ledger(metadata)
    if not ledger:
        return ""

    latest_record: Optional[IterationMemoryRecord] = None
    latest_phase: Optional[int] = None
    for record in ledger:
        if turn_sequence is not None and record.get("turn_sequence") != turn_sequence:
            continue
        sections = _sanitize_sections(record)
        if not sections:
            continue
        phase = record.get("phase_sequence")
        if not isinstance(phase, int):
            continue
        if latest_phase is None or phase > latest_phase:
            latest_phase = phase
            latest_record = record

    if latest_record is None:
        return ""

    isolated_metadata = {
        _WORKING_MEMORY_KEY: {
            _LEDGER_FIELD: [dict(latest_record)],
        }
    }
    body = render(isolated_metadata, turn_sequence=turn_sequence)
    if not body:
        return ""
    return f"{LATEST_PHASE_MEMORY_SECTION_HEADING}\n{body}"


__all__ = [
    "LATEST_PHASE_MEMORY_SECTION_HEADING",
    "PHASE_MEMORY_SECTION_HEADING",
    "IterationMemoryRecord",
    "append",
    "get_current_turn_scope",
    "get_ledger",
    "has_renderable_sections",
    "latest_recorded_phase_sequence",
    "peek_next_phase_sequence",
    "render",
    "render_latest_phase_memory_section",
    "render_phase_memory_section",
    "reserve_next_phase_sequence",
]
