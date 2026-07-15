"""Singleton-state persistence helper for CVE indexing services.

Scope:
- Loads or creates the single `CveIndexState` row used by CVE workflows.
- Optionally acquires row-level lock when callers need lease-safe mutations.

Boundary:
- Contains no scheduler decisions or sync run execution logic.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.models.cve import CveIndexState


def get_or_create_cve_index_state(db: Session, *, lock: bool = False) -> CveIndexState:
    """Return singleton CVE index state row, creating it when absent."""
    query = db.query(CveIndexState).order_by(CveIndexState.id.asc())
    if lock and hasattr(query, "with_for_update"):
        query = query.with_for_update()

    state = query.first()
    if state is not None:
        return state

    state = CveIndexState(last_sync_status="idle", rebuild_required=False)
    db.add(state)
    db.commit()
    db.refresh(state)
    return state


__all__ = ["get_or_create_cve_index_state"]
