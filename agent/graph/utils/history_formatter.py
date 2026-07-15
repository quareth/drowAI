"""Shared helpers for within-turn iteration history and content sanitization.

Scope (post Phase 5/6 cutover):

- Within-turn iteration history rendering for post-tool reasoning
  (``build_iteration_history`` / ``build_iteration_history_from_state``).
  These are primarily driven by the current-turn phase ledger owned by
  :mod:`agent.graph.utils.iteration_memory`. The formatter is
  **rendering-only**: it never mutates metadata, never stamps identity
  fields, and never pulls transcript ``<turn ...>`` blocks into PTR
  continuity output.
- Hygiene helpers (``truncate_content``, ``sanitize_history_content``)
  used to strip raw tool blobs before they end up in any rendered
  history string.

Cross-turn user/assistant continuity is owned by the shared
``ConversationContextBundle`` (``agent/graph/context/projections.py``);
no prompt consumer in this module formats transcript continuity.

Duplicate-section avoidance
---------------------------
The PTR prompt builder (``core/prompts/builders/post_tool.py``) renders
the phase ledger directly into its own dedicated
``## Prior Current-Turn Phase Memory`` section. To avoid producing a
redundant ``## Conversation History`` section carrying the same
content, these helpers return the empty-context marker whenever that
ledger section should remain the sole continuity surface.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from . import iteration_memory as _iteration_memory

if TYPE_CHECKING:
    from ..state import InteractiveState

logger = logging.getLogger(__name__)

# =============================================================================
# Constants
# =============================================================================

MAX_HISTORY_ENTRIES = 120
MAX_HISTORY_CONTENT_CHARS = 8000
EMPTY_ITERATION_HISTORY_MARKER = (
    "No prior context available. This is the first reasoning iteration."
)
_RAW_TOOL_HISTORY_MARKERS = (
    "<?xml",
    "<!doctype",
    "<nmaprun",
    "scanner=\"nmap\"",
    "nmap scan initiated",
)


# =============================================================================
# Core Helper Functions
# =============================================================================


def truncate_content(content: str, max_chars: int = MAX_HISTORY_CONTENT_CHARS) -> str:
    """Truncate content to max characters with ellipsis indicator.
    
    Args:
        content: The string to truncate.
        max_chars: Maximum allowed characters.
        
    Returns:
        Truncated string with ellipsis if it exceeded max_chars.
    """
    content = content.strip()
    if len(content) <= max_chars:
        return content
    return content[: max_chars - 1].rstrip() + "…"


def sanitize_history_content(
    content: str,
    *,
    compact_summary: Optional[str] = None,
) -> str:
    """Normalize history content to avoid leaking raw tool output into prompts."""
    text = str(content or "").strip()
    if not text:
        return ""

    lowered = text.lower()
    marker_hit = any(marker in lowered for marker in _RAW_TOOL_HISTORY_MARKERS)
    multiline_blob = text.count("\n") >= 12 and len(text) >= 400
    oversized_observation = lowered.startswith("observation:") and len(text) >= 350
    looks_raw = marker_hit or multiline_blob or oversized_observation
    if not looks_raw:
        return text

    replacement = (
        str(compact_summary or "").strip()
        or "Tool output was condensed. Refer to the compact tool summary."
    )
    if lowered.startswith("observation:"):
        return f"Observation: {replacement}"
    return replacement


# =============================================================================
# Within-Turn History (Observations + Reasoning) - For Post-Tool Reasoning
# =============================================================================
#
# Cross-turn user/assistant continuity is owned by the shared
# ``ConversationContextBundle`` (``agent/graph/context/projections.py``).
# The legacy ``format_conversation_history`` helper was removed in
# Phase 6 after the Phase 5 authority cutover eliminated its last
# prompt consumer.


def _ledger_has_records(
    metadata: Optional[Dict[str, Any]],
    *,
    turn_sequence: Optional[int],
) -> bool:
    """Return True when the current-turn phase ledger has at least one record.

    Used to scope current-turn ledger presence checks via the shared helper.
    This function never mutates metadata.
    """
    if not isinstance(metadata, dict):
        return False
    ledger = _iteration_memory.get_ledger(metadata)
    if not ledger:
        return False
    if turn_sequence is None:
        return any(
            _iteration_memory.has_renderable_sections(dict(record))
            for record in ledger
        )
    for record in ledger:
        if (
            record.get("turn_sequence") == turn_sequence
            and _iteration_memory.has_renderable_sections(dict(record))
        ):
            return True
    return False


def build_iteration_history(
    trace_observations: Optional[List[str]],
    trace_reasoning: Optional[List[str]],
    compact_summary: Optional[str] = None,
    max_entries: int = MAX_HISTORY_ENTRIES,
    *,
    metadata: Optional[Dict[str, Any]] = None,
    turn_sequence: Optional[int] = None,
) -> str:
    """Build formatted history for within-turn reasoning iterations.

    The current-turn phase ledger (owned by
    :mod:`agent.graph.utils.iteration_memory`) is the only PTR
    continuity source after the Phase 5 cutover. This helper returns the
    empty-context marker so prompt builders omit their redundant prose
    history section and rely on the dedicated
    ``## Prior Current-Turn Phase Memory`` section.

    This is **within-turn** context only. Cross-turn user/assistant
    continuity is owned by the shared ``ConversationContextBundle``
    (``agent/graph/context/projections.py``). Transcript ``<turn ...>``
    blocks are never pulled in here.

    Args:
        trace_observations: List of observation strings from trace.observations.
            These are the articulated observations from previous tool executions.
        trace_reasoning: List of reasoning entries from trace.reasoning.
            These are internal reasoning notes and decision records.
        compact_summary: Unused compatibility argument retained for callers.
        max_entries: Maximum number of entries to include (prevents context bloat).
        metadata: Optional reference to ``facts.metadata`` for ledger probing.
            Keyword-only. When provided and the ledger is present for the
            active turn, the dedicated phase-memory section remains the
            sole continuity source.
        turn_sequence: Optional canonical runtime-owned turn ordinal used to
            scope the ledger presence check. Keyword-only.

    Returns:
        Empty-context marker. The dedicated phase-memory section is the
        continuity authority.
    """
    _ = trace_observations, trace_reasoning, compact_summary, max_entries
    if _ledger_has_records(metadata, turn_sequence=turn_sequence):
        return EMPTY_ITERATION_HISTORY_MARKER
    return EMPTY_ITERATION_HISTORY_MARKER


def build_iteration_history_from_state(
    interactive: "InteractiveState",
    max_entries: int = MAX_HISTORY_ENTRIES,
) -> str:
    """Build iteration history from InteractiveState.

    Convenience function that extracts history sources from state and
    delegates to :func:`build_iteration_history`. The metadata reference
    and the last observed turn_sequence tracked by the iteration-memory
    helper are passed through so the ledger-first branch can suppress
    duplicated prose output when the authoritative phase ledger is
    already populated.

    Args:
        interactive: The current InteractiveState.
        max_entries: Maximum number of entries to include.

    Returns:
        Formatted iteration history string.
    """
    metadata = interactive.facts.safe_metadata
    trace_observations = interactive.trace.observations or []
    trace_reasoning = interactive.trace.reasoning or []

    # Scope the ledger presence check to the last turn the iteration
    # memory counter was scoped to. When absent, fall back to "any
    # record present" via turn_sequence=None inside
    # :func:`_ledger_has_records`.
    turn_sequence: Optional[int] = None
    stamped_turn = _iteration_memory.get_current_turn_scope(metadata)
    if isinstance(stamped_turn, int):
        turn_sequence = stamped_turn

    return build_iteration_history(
        trace_observations=trace_observations,
        trace_reasoning=trace_reasoning,
        compact_summary=None,
        max_entries=max_entries,
        metadata=metadata if isinstance(metadata, dict) else None,
        turn_sequence=turn_sequence,
    )


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # Constants
    "MAX_HISTORY_ENTRIES",
    "MAX_HISTORY_CONTENT_CHARS",
    "EMPTY_ITERATION_HISTORY_MARKER",
    # Core functions
    "truncate_content",
    "sanitize_history_content",
    # Within-turn history (observations + reasoning)
    "build_iteration_history",
    "build_iteration_history_from_state",
]
