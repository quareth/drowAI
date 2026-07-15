"""Intent persistence boundary regression tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

from backend.services.langgraph_chat.intent.persistence import persist_intent_context


def test_persist_intent_context_writes_management_state() -> None:
    """Intent state should be persisted outside provider-owned runtime workspaces."""
    task_id = 98654322
    runtime_config = SimpleNamespace(
        metadata={},
        chat_inputs=SimpleNamespace(
            task_id=task_id,
            conversation_id="conv-1",
            message="hello",
        ),
    )
    state = SimpleNamespace(
        facts=SimpleNamespace(
            capability="chat",
            intent_hints=["hint"],
            risk_flags=[],
            metadata={"intent_router": {"name": "default"}},
        )
    )

    persist_intent_context(runtime_config, state)

    project_root = Path(__file__).resolve().parents[3]
    state_file = (
        project_root
        / "backend"
        / "management_state"
        / "langgraph_chat_intent"
        / f"task-{task_id}"
        / "agent_state.json"
    )
    assert state_file.exists()
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert isinstance(payload.get("intent_history"), list)
    assert payload["intent_history"][-1]["conversation_id"] == "conv-1"

    shutil.rmtree(state_file.parent, ignore_errors=True)
