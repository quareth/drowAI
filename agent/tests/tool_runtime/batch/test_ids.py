"""Phase 1 tests for ``agent.tool_runtime.batch.ids``.

Covers:

- ``test_tool_call_id_unique``: 1000 mints have no collisions.
- ``test_tool_batch_id_unique``: 1000 mints have no collisions.
"""

from __future__ import annotations

from agent.tool_runtime.batch.ids import mint_tool_batch_id, mint_tool_call_id


def test_tool_call_id_unique():
    minted = {mint_tool_call_id() for _ in range(1000)}
    assert len(minted) == 1000


def test_tool_batch_id_unique():
    minted = {mint_tool_batch_id() for _ in range(1000)}
    assert len(minted) == 1000
