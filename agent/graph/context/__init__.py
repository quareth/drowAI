"""Hot-path conversation context package for LangGraph.

This package owns the canonical, prompt-authoritative hot-path memory
contract used by every LangGraph role (classifier, category selector,
planner, articulation). A single ``ConversationContextBundle`` is
assembled once per turn and projected deterministically per role — this
package is the *only* place new prompt-authoritative memory contracts
should live.

Modules:

- ``contracts``     -- typed bundle and section contracts.
- ``transcript``    -- recent-turn window selection policy.
- ``builder``       -- single bundle assembly authority.
- ``projections``   -- shared per-role projections and cache-friendly
                       prompt-section serializer.
- ``runtime_state`` -- single authority for mapping canonical working
                       memory into the bundle's ``runtime_state`` /
                       ``evidence_refs`` slots (keeps the bundle in
                       sync with working-memory mutations without
                       re-doing projection logic).

Only the contract types are re-exported here; callers import the
builder, transcript, projection, and runtime-state helpers directly
from their respective submodules to keep this package's public
surface small.
"""

from agent.graph.context.contracts import (
    ConversationContextBundle,
    EvidenceRef,
    RuntimeStateSnapshot,
    TranscriptWindow,
)

__all__ = [
    "ConversationContextBundle",
    "EvidenceRef",
    "RuntimeStateSnapshot",
    "TranscriptWindow",
]
