"""Lightweight structure checks on ``simple_tool_builder.py``.

Scope
-----
This module preserves only the source-text checks that still match the live
builder structure after the 2026-04-26 ``e0a42b04`` finalization-unification
refactor. The previously co-resident tests for an
``agent.graph.nodes.llm_synthesis`` module (which never existed in this repo),
old node names like ``tool_execution`` / ``failure_reflection`` /
``increment_retry``, and a stale ``def build_simple_tool_graph(*,
checkpointer=None)`` signature have all been removed as part of the worktree
merge recovery (Bucket 1 follow-up).

Real graph behavior is exercised programmatically by
``agent/graph/tests/test_simple_tool_routing_hitl.py``,
``agent/graph/tests/test_simple_tool_hitl_resume_path.py``,
``agent/graph/tests/test_simple_tool_hitl_plan_preparation.py``, and
``agent/tests/test_simple_tool_retry_loop.py``. Source-text guards in this file
exist only to catch accidental reintroduction of removed concepts (e.g. the
old ``artifact_ingestion`` parallel branch).
"""

import os

# Set DATABASE_URL before any backend imports
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


_BUILDER_PATH = "agent/graph/builders/simple_tool_builder.py"


def _read_builder() -> str:
    with open(_BUILDER_PATH, "r", encoding="utf-8") as f:
        return f.read()


def test_graph_registry_integration():
    """Test that the graph registry constants are defined."""
    from agent.graph.builders.simple_tool_builder import GRAPH_NAME
    from agent.graph.graph_names import GRAPH_NAME_SIMPLE_TOOL

    content = _read_builder()
    assert GRAPH_NAME == GRAPH_NAME_SIMPLE_TOOL
    assert "__all__" in content
    assert '"build_simple_tool_graph"' in content or "'build_simple_tool_graph'" in content


def test_no_artifact_ingestion_import():
    """Test that artifact_ingestion node is NOT imported (indexing is a side effect)."""
    content = _read_builder()
    assert "from ..nodes.artifact_ingestion import" not in content


def test_no_parallel_node_registration():
    """Test that artifact_ingestion node is NOT registered (indexing is a side effect)."""
    content = _read_builder()
    assert 'graph.add_node("artifact_ingestion"' not in content


def test_fire_and_forget_documentation():
    """Test that documentation mentions fire-and-forget side effect."""
    content = _read_builder()
    assert "fire-and-forget" in content.lower() or "side effect" in content.lower()
