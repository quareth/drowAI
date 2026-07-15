"""Pydantic schemas and typed errors for chunked artifact retrieval.

Implements minimal data models to support ingestion, indexing, and retrieval
without committing to a specific backend. Focus is on determinism and clean I/O
boundaries as described in Agents.md.
"""

from __future__ import annotations

from typing import Any, Dict, List
from pydantic import BaseModel, Field


class IngestionError(Exception):
    pass


class IndexingError(Exception):
    pass


class RetrievalError(Exception):
    pass


class Chunk(BaseModel):
    """A semantic chunk of an artifact with citation offsets.

    Offsets are byte offsets within the original artifact file.
    IDs should be stable for determinism (e.g., hash of path+offsets).
    """

    id: str
    run_id: str
    artifact_path: str
    offset_start: int = Field(ge=0)
    offset_end: int = Field(ge=0)
    text: str
    meta: Dict[str, Any] = Field(default_factory=dict)
    digest: str = ""
    token_count: int = 0


class ContextPack(BaseModel):
    """Budgeted retrieval pack to merge into reasoning context."""

    run_digest: str = ""
    entity_digests: List[Dict[str, Any]] = Field(default_factory=list)
    chunks: List[Chunk] = Field(default_factory=list)
    citations: List[Dict[str, Any]] = Field(default_factory=list)

