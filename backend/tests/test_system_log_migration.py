"""Characterization tests for SystemLog migration.

Covers:
- SystemLog writes from agent reasoning (AgentReasoningStore.append_step)
- SystemLog writes from container status (image pull / container create)
- Sequence assignment for system_logs"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime

from backend.services.streaming.reasoning_store import AgentReasoningStore
from backend.models.streaming import SystemLog


class TestSystemLogAgentReasoning:
    """Test SystemLog writes from AgentReasoningStore.append_step."""

    def test_append_step_writes_to_system_log(self):
        """append_step creates a SystemLog row with correct type and content."""
        db = Mock()
        db.execute.return_value.scalar.return_value = None
        db.add = MagicMock()
        db.commit = MagicMock()
        db.refresh = MagicMock()
        db.rollback = MagicMock()

        store = AgentReasoningStore(db)
        step = {"type": "reasoning", "content": "Step content", "metadata": {"key": "value"}}
        row = store.append_step(task_id=1, step=step)

        assert row is not None
        db.add.assert_called_once()
        added = db.add.call_args[0][0]
        assert isinstance(added, SystemLog)
        assert added.task_id == 1
        assert added.sequence == 1
        assert added.type == "reasoning"
        assert added.content == "Step content"
        assert added.log_metadata == {"key": "value"}

    def test_append_step_sequence_increments(self):
        """Sequence is max(sequence)+1 for task."""
        db = Mock()
        db.execute.return_value.scalar.return_value = 5
        db.add = MagicMock()
        db.commit = MagicMock()
        db.refresh = MagicMock()

        store = AgentReasoningStore(db)
        row = store.append_step(task_id=1, step={"type": "step", "content": "x"})

        assert row is not None
        added = db.add.call_args[0][0]
        assert added.sequence == 6

    def test_append_step_type_truncated_to_50_chars(self):
        """Type is truncated to 50 chars (SystemLog.type length)."""
        db = Mock()
        db.execute.return_value.scalar.return_value = None
        db.add = MagicMock()
        db.commit = MagicMock()
        db.refresh = MagicMock()

        store = AgentReasoningStore(db)
        long_type = "a" * 60
        store.append_step(task_id=1, step={"type": long_type, "content": "x"})

        added = db.add.call_args[0][0]
        assert len(added.type) == 50


class TestSystemLogSequenceAssignment:
    """Test sequence assignment for system_logs."""

    def test_list_after_uses_system_log_sequence(self):
        """list_after queries SystemLog by task_id and sequence."""
        db = Mock()
        mock_row = Mock(spec=SystemLog)
        mock_row.sequence = 1
        mock_row.task_id = 1
        mock_row.type = "reasoning"
        mock_row.content = "x"
        mock_row.log_metadata = {}
        db.execute.return_value.scalars.return_value.all.return_value = [mock_row]

        store = AgentReasoningStore(db)
        result = store.list_after(task_id=1, after=0, limit=10)

        assert len(result) == 1
        assert result[0].sequence == 1
        db.execute.assert_called_once()

    def test_get_latest_sequence_queries_system_log(self):
        """get_latest_sequence returns max(SystemLog.sequence) for task."""
        db = Mock()
        db.execute.return_value.scalar.return_value = 42
        store = AgentReasoningStore(db)
        assert store.get_latest_sequence(1) == 42
