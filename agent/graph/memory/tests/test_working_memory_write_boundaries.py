"""Guardrails for working-memory write ownership boundaries."""

from __future__ import annotations

import re
from pathlib import Path


def test_working_memory_writes_are_centralized() -> None:
    """Ensure production writes route through ``MemoryManager`` primitives.

    The architectural invariant this test enforces is **"working-memory
    mutations go through ``MemoryManager`` primitives, not raw dict
    surgery."** Each entry in ``allowed_files`` either *is* a
    ``MemoryManager`` reducer host (the working-memory node), or it
    explicitly delegates to one (``update_active_handles`` /
    ``reduce_tool_result`` / ``reduce_post_tool_*``). A write in any
    other file is presumed to be reaching into the dict directly and
    is flagged.

    See also: ``docs/issues/working-memory-write-boundary-violation.md``
    (closed 2026-04-27).
    """
    repo_root = Path(__file__).resolve().parents[4]
    agent_root = repo_root / "agent" / "graph"
    write_pattern = re.compile(r'(?:facts\.)?metadata\["working_memory"\]\s*=')
    # Each file below mutates working memory ONLY through MemoryManager
    # primitives; the inline assignment is the final write-back of the
    # primitive's result.
    allowed_files = {
        # Hosts the canonical working-memory node + the public ``apply_*``
        # helpers (``apply_post_tool_active_decision``,
        # ``apply_post_tool_candidate_findings``).
        (agent_root / "nodes" / "working_memory.py").resolve(),
        # Tool-execution subgraph orchestrator: writes WM after the
        # iteration-memory append step.
        (agent_root / "subgraphs" / "tool_execution.py").resolve(),
        # Target-sync helper writes back ``metadata["working_memory"]``
        # only after delegating mutation to ``update_active_handles``
        # (a MemoryManager primitive) to keep the intent:target referent
        # aligned with active plan/todo intent.
        (agent_root / "context" / "runtime_state.py").resolve(),
        # Routes through ``MemoryManager.reduce_tool_result`` (injected
        # via ``memory_reduce_tool_result_fn``) to fold tool-execution
        # output into working memory during result-state projection.
        (agent_root / "subgraphs" / "tool_execution_runtime" / "result_state_projection.py").resolve(),
    }

    violations: list[str] = []
    for path in agent_root.rglob("*.py"):
        if "/tests/" in path.as_posix():
            continue
        content = path.read_text(encoding="utf-8")
        if write_pattern.search(content) and path.resolve() not in allowed_files:
            violations.append(path.relative_to(repo_root).as_posix())

    assert not violations, f"Unexpected working_memory write locations: {violations}"
