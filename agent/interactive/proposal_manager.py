from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Optional

from .proposal_store import ProposalStore, ToolProposal


class ProposalManager:
    """High-level helper to create proposals and wait for approval via file polling."""

    def __init__(self, workspace_path: Optional[str] = None) -> None:
        self.store = ProposalStore(workspace_path or os.getenv("WORKSPACE", "/workspace"))

    def create_proposal(self, tool_name: str, parameters: Dict[str, Any], reasoning: str) -> ToolProposal:
        return self.store.save_proposal(tool_name, parameters, reasoning)

    async def wait_for_approval(self, proposal_id: str, poll_interval: float = 1.0, timeout_s: int = 3600) -> str:
        """Poll approvals.jsonl until status != pending or timeout; returns status."""
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            status = self.store.get_status(proposal_id)
            if status in {"approved", "rejected"}:
                return status
            await asyncio.sleep(poll_interval)
        return "rejected"  # safety fallback

    def check_approval_status(self, proposal_id: str) -> str:
        return self.store.get_status(proposal_id)


