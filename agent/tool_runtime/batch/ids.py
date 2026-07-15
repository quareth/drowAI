"""Canonical id-mint sites for the tool-batch contract.

This module is the **single source of truth** for minting both
``tool_batch_id`` and ``tool_call_id``. After Phase 3, the mint authority
for ``tool_call_id`` is :func:`mint_tool_call_id`, called by
``agent/reasoning/batch_commit.py:commit_tool_batch`` when an envelope
from the builder is converted into a :class:`ToolBatch`.

Legacy contract preserved by the coordinator
--------------------------------------------

Today ``tool_call_id`` can also enter the runtime via
``request.metadata["tool_call_id"]`` — set upstream by the LangGraph chat
facade for single-tool flows. The coordinator continues to honor an
inbound ``metadata["tool_call_id"]`` so legacy single-tool callers do not
lose stable identity, and falls back to a freshly minted id otherwise.

Do not introduce a third mint site. If a new caller needs an id, route it
through this module so audit + telemetry stay coherent.
"""

from __future__ import annotations

import uuid


def mint_tool_batch_id() -> str:
    """Return a fresh, globally-unique ``tool_batch_id``."""
    return f"tb_{uuid.uuid4().hex}"


def mint_tool_call_id() -> str:
    """Return a fresh, globally-unique ``tool_call_id``.

    Called by ``batch_commit.commit_tool_batch`` for every committed call.
    The coordinator's legacy fallback path may instead reuse an inbound
    ``metadata["tool_call_id"]`` if one is present (see module docstring).
    """
    return f"tc_{uuid.uuid4().hex}"
