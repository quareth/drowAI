"""ConversationManager state-path boundary regressions."""

from __future__ import annotations

import shutil
from pathlib import Path

from agent.chat.conversation_manager import ConversationManager


def test_conversation_manager_uses_management_state_root() -> None:
    """Conversation metadata should live outside provider-owned runtime workspace paths."""
    task_id = 98654321
    manager = ConversationManager(task_id)
    conversation_id = manager.ensure_default_conversation()

    assert conversation_id
    state_root = manager.state_root
    assert "management_state" in str(state_root)
    assert "workspaces" not in str(state_root)
    assert manager.index_file.exists()

    shutil.rmtree(state_root, ignore_errors=True)
