"""Task 4.2: Tool metadata warm path tests.

Verifies catalog metadata cache correctness and that first post-approval
dispatch does not trigger full cold metadata scan on subsequent calls.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.tools.tool_registry import (
    available_tools,
    clear_catalog_metadata_cache,
    get_catalog_metadata_snapshot,
    get_tool_metadata,
    warm_catalog_metadata_snapshot,
)


def test_catalog_metadata_snapshot_returns_same_structure_as_per_tool() -> None:
    """Cached snapshot yields same metadata structure as get_tool_metadata per tool."""
    clear_catalog_metadata_cache()
    snapshot = get_catalog_metadata_snapshot()
    tool_ids = available_tools()
    assert len(snapshot) == len(tool_ids)
    # Sample tools that have BaseTool (e.g. shell.exec); skip non-tool modules
    known_tools = ["shell.exec", "shell.script", "information_gathering.network_discovery.nmap"]
    for tid in known_tools:
        if tid not in snapshot:
            continue
        meta = snapshot[tid]
        assert "name" in meta
        assert "description" in meta
        direct = get_tool_metadata(tid)
        assert meta["name"] == direct["name"]
        assert meta["description"] == direct["description"]
        assert "args_schema" in meta


def test_catalog_metadata_snapshot_caches_on_second_call() -> None:
    """Second call returns cached snapshot (no re-scan)."""
    clear_catalog_metadata_cache()
    snap1 = get_catalog_metadata_snapshot()
    snap2 = get_catalog_metadata_snapshot()
    assert snap1 is snap2


def test_clear_catalog_metadata_cache_invalidates() -> None:
    """clear_catalog_metadata_cache forces rebuild on next get."""
    clear_catalog_metadata_cache()
    snap1 = get_catalog_metadata_snapshot()
    clear_catalog_metadata_cache()
    snap2 = get_catalog_metadata_snapshot()
    assert snap1 is not snap2
    assert len(snap1) == len(snap2)


def test_coordinator_build_catalog_uses_warm_path() -> None:
    """get_catalog_metadata_snapshot used by coordinator avoids get_tool_metadata on warm path."""
    clear_catalog_metadata_cache()
    # First call populates cache (get_tool_metadata called per tool)
    snap1 = get_catalog_metadata_snapshot()
    assert len(snap1) > 0

    # Second call returns cache; get_tool_metadata must not be called
    with patch("agent.tools.tool_registry.get_tool_metadata") as mock_meta:
        snap2 = get_catalog_metadata_snapshot()
        mock_meta.assert_not_called()
    assert snap1 is snap2


def test_warm_catalog_metadata_snapshot_primes_cache() -> None:
    """Explicit warm helper should prefill cache and report snapshot size."""
    clear_catalog_metadata_cache()
    count = warm_catalog_metadata_snapshot()
    assert count == len(available_tools())

    with patch("agent.tools.tool_registry.get_tool_metadata") as mock_meta:
        cached = get_catalog_metadata_snapshot()
        mock_meta.assert_not_called()
    assert len(cached) == count
