"""Shared semantic transport package boundary.

This package is the single home for backend-free semantic transport helpers
used by agent runtime paths. Tool-specific parsing remains in tool modules.
"""

from __future__ import annotations

from .enrichment import (
    build_runtime_semantic_metadata,
    extract_runtime_semantic_inputs,
    extract_runtime_semantic_inputs_with_fallback,
)

__all__ = (
    "build_runtime_semantic_metadata",
    "extract_runtime_semantic_inputs",
    "extract_runtime_semantic_inputs_with_fallback",
)
