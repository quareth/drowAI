"""Backend wrapper for building LangGraph tool catalogs with metrics."""

from __future__ import annotations

from typing import Any, Dict, Optional

from backend.services.metrics.utils import safe_gauge, safe_inc

from agent.graph.utils.tool_catalog import (
    ToolCatalogEntry,
    ToolCatalogResult,
    build_tool_catalog as _build_tool_catalog,
)


def build_tool_catalog(
    *,
    capability: Optional[str],
    metadata: Optional[Dict[str, Any]] = None,
    limit: Optional[int] = None,
    config: Optional[Any] = None,
) -> ToolCatalogResult:
    """Build a tool catalog and emit backend metrics."""

    result = _build_tool_catalog(
        capability=capability,
        metadata=metadata,
        limit=limit,
        config=config,
    )
    safe_inc("langgraph_tool_catalog_requests")
    safe_gauge("langgraph_tool_catalog_candidates", len(result.candidates))
    return result


__all__ = ["ToolCatalogEntry", "ToolCatalogResult", "build_tool_catalog"]
