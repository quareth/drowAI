from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


@dataclass
class ToolProposal:
    id: str
    tool_name: str
    parameters: Dict[str, Any]
    reasoning: str
    status: str  # pending|approved|rejected
    created_at: str


class ProposalStore:
    """File-backed proposal store using JSONL files in the task workspace."""

    def __init__(self, workspace_path: Optional[str] = None) -> None:
        base = workspace_path or os.getenv("WORKSPACE", "/workspace")
        self._proposals_file = Path(base) / "proposals.jsonl"
        self._approvals_file = Path(base) / "approvals.jsonl"
        self._counter = 0
        try:
            self._proposals_file.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def _append_jsonl(self, path: Path, obj: Dict[str, Any]) -> None:
        with _LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj) + "\n")

    def _read_jsonl(self, path: Path) -> List[Dict[str, Any]]:
        if not path.exists():
            return []
        with _LOCK:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return [json.loads(line) for line in f if line.strip()]
            except Exception:
                return []

    def _next_id(self) -> str:
        with _LOCK:
            self._counter += 1
            return f"prop_{int(time.time())}_{self._counter:04d}"

    def save_proposal(self, tool_name: str, parameters: Dict[str, Any], reasoning: str) -> ToolProposal:
        """Create and persist a new pending proposal."""
        proposal = ToolProposal(
            id=self._next_id(),
            tool_name=tool_name,
            parameters=parameters,
            reasoning=reasoning or "",
            status="pending",
            created_at=_now_iso(),
        )
        self._append_jsonl(self._proposals_file, asdict(proposal))
        return proposal

    def update_status(self, proposal_id: str, status: str) -> bool:
        """Append an approval status update; read APIs coalesce latest."""
        status = status.lower()
        if status not in {"pending", "approved", "rejected"}:
            return False
        entry = {"id": proposal_id, "status": status, "timestamp": _now_iso()}
        self._append_jsonl(self._approvals_file, entry)
        return True

    def get_pending(self) -> Optional[ToolProposal]:
        """Return the most recent pending proposal if any (coalesced with approvals)."""
        proposals = self._read_jsonl(self._proposals_file)
        approvals = self._read_jsonl(self._approvals_file)
        latest_status: Dict[str, str] = {}
        for ap in approvals:
            pid = str(ap.get("id", ""))
            st = str(ap.get("status", "")).lower()
            if pid:
                latest_status[pid] = st
        # find newest proposal that is still pending
        for row in reversed(proposals):
            pid = str(row.get("id", ""))
            status = latest_status.get(pid, row.get("status", "pending")).lower()
            if status == "pending":
                try:
                    return ToolProposal(
                        id=pid,
                        tool_name=row.get("tool_name", ""),
                        parameters=row.get("parameters", {}) or {},
                        reasoning=row.get("reasoning", ""),
                        status="pending",
                        created_at=row.get("created_at", _now_iso()),
                    )
                except Exception:
                    continue
        return None

    def get_status(self, proposal_id: str) -> str:
        """Return the latest status for a proposal id."""
        status = "pending"
        approvals = self._read_jsonl(self._approvals_file)
        for ap in approvals:
            if str(ap.get("id")) == str(proposal_id):
                status = str(ap.get("status", status)).lower()
        return status

    def list_proposals(self) -> List[Dict[str, Any]]:
        return self._read_jsonl(self._proposals_file)


