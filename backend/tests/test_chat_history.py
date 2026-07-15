"""
Characterization tests for conversation history loading (ChatMessage-only).

Covers edge cases for _build_conversation_history and history loading:
- ConversationHistoryReader history loading and tree traversal
- Format conversion (ChatMessage -> OpenAI)
- Empty conversations
- Regenerate-from-middle: sibling branches included in history
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from sqlalchemy.orm import Session

from backend.models.chat import ChatMessage, ToolCall
from backend.routers.chat import _build_conversation_history
from backend.services.chat.conversation_history_reader import ConversationHistoryReader
import backend.services.langgraph_chat.compression.snapshot_repository as compression_snapshot_repository
from backend.services.langgraph_chat.compression.snapshot_repository import (
    CompressionSnapshotRepository,
)


def _make_chatmessage_execute_result(rows):
    """Helper: mock execute return for ChatMessage path (scalars().unique().all())."""
    result = Mock()
    result.scalars.return_value.unique.return_value.all.return_value = rows
    return result


def _create_chat_message(
    db,
    task_id,
    conversation_id,
    parent_id,
    message_type,
    message,
    reasoning_tokens=None,
    observation_tokens=None,
    turn_number=None,
):
    """Create a ChatMessage row and return it. Shared for endpoint and history tests."""
    row = ChatMessage(
        task_id=task_id,
        conversation_id=conversation_id,
        parent_message_id=parent_id,
        message_type=message_type,
        message=message,
        reasoning_tokens=reasoning_tokens,
        observation_tokens=observation_tokens,
        turn_number=turn_number,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _create_tool_call(
    db,
    chat_message_id,
    tool_name,
    tool_arguments,
    tool_result,
    turn_index,
    tab_index,
    reasoning_tokens=None,
):
    """Create a ToolCall row and return it. Shared for endpoint and history tests."""
    row = ToolCall(
        chat_message_id=chat_message_id,
        tool_call_id=f"tc-{chat_message_id}-{turn_index}-{tab_index}",
        tool_name=tool_name,
        tool_arguments=tool_arguments or {},
        tool_result=tool_result,
        turn_index=turn_index,
        tab_index=tab_index,
        reasoning_tokens=reasoning_tokens,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class TestChatHistoryLoading:
    """Characterization tests for _build_conversation_history (dual-store)."""

    def test_empty_conversation_returns_empty_list(self):
        """Empty conversation_id returns []."""
        result = _build_conversation_history(Mock(), task_id=1, conversation_id=None)
        assert result == []

    def test_none_conversation_id_returns_empty_list(self):
        """None conversation_id returns []."""
        result = _build_conversation_history(Mock(), task_id=1, conversation_id=None)
        assert result == []

    def test_store_detection_uses_chatmessage_when_non_empty(self):
        """When ChatMessage has messages, use ChatMessage store (no AgentLog call)."""
        db = Mock(spec=Session)
        chat_msg_user = Mock()
        chat_msg_user.id = 1
        chat_msg_user.parent_message_id = None
        chat_msg_user.latest_child_message_id = 2
        chat_msg_user.message_type = "user"
        chat_msg_user.message = "Hello"
        chat_msg_user.tool_calls = []
        chat_msg_assistant = Mock()
        chat_msg_assistant.id = 2
        chat_msg_assistant.parent_message_id = 1
        chat_msg_assistant.latest_child_message_id = None
        chat_msg_assistant.message_type = "assistant"
        chat_msg_assistant.message = "Hi"
        chat_msg_assistant.tool_calls = []
        db.execute.side_effect = [
            _make_chatmessage_execute_result([chat_msg_user, chat_msg_assistant]),
        ]
        source_message_ids: list[int] = []
        result = _build_conversation_history(
            db,
            task_id=1,
            conversation_id="conv-1",
            source_message_ids_out=source_message_ids,
        )
        assert len(result) == 2
        assert result[0]["role"] == "user" and result[0]["content"] == "Hello"
        assert result[1]["role"] == "assistant" and result[1]["content"] == "Hi"
        assert source_message_ids == [1, 2]
        assert db.execute.call_count == 1

    def test_chatmessage_empty_returns_empty_list(self):
        """When ChatMessage returns empty, history is empty (no fallback)."""
        db = Mock(spec=Session)
        db.execute.side_effect = [
            _make_chatmessage_execute_result([]),
        ]
        result = _build_conversation_history(db, task_id=1, conversation_id="conv-a")
        assert result == []

    def test_build_history_no_longer_passes_fixed_turn_limit(self, monkeypatch):
        """Main-path history builder should rely on centralized token-window policy, not fixed count."""
        captured_kwargs = {}

        class _FakeConversationHistoryReader:
            def __init__(self, _db):
                return None

            def build_aligned_openai_conversation_history(self, **kwargs):
                captured_kwargs.update(kwargs)
                return SimpleNamespace(messages=(), source_message_ids=())

        monkeypatch.setattr(
            "backend.routers.chat.submit.ConversationHistoryReader",
            _FakeConversationHistoryReader,
        )

        _build_conversation_history(Mock(spec=Session), task_id=12, conversation_id="conv-12")

        assert captured_kwargs["task_id"] == 12
        assert captured_kwargs["conversation_id"] == "conv-12"
        assert "limit" not in captured_kwargs


class TestLoadFromChatMessage:
    """Tests for ConversationHistoryReader.get_conversation_history (tree traversal, limit)."""

    def test_returns_empty_when_no_messages(self):
        """Empty list when no ChatMessage rows for task/conversation."""
        db = Mock(spec=Session)
        db.execute.side_effect = [_make_chatmessage_execute_result([])]
        result = ConversationHistoryReader(db).get_conversation_history(task_id=1, conversation_id="c1")
        assert result == []

    def test_returns_ordered_messages_respecting_tree(self):
        """Tree traversal order (roots then children by created_at) is preserved."""
        m1 = Mock()
        m1.id = 1
        m1.parent_message_id = None
        m1.latest_child_message_id = 2
        m1.message_type = "user"
        m1.message = "Hi"
        m1.tool_calls = []
        m1.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
        m2 = Mock()
        m2.id = 2
        m2.parent_message_id = 1
        m2.latest_child_message_id = None
        m2.message_type = "assistant"
        m2.message = "Hello"
        m2.tool_calls = []
        m2.created_at = datetime(2024, 1, 2, tzinfo=timezone.utc)
        db = Mock(spec=Session)
        db.execute.side_effect = [_make_chatmessage_execute_result([m1, m2])]
        result = ConversationHistoryReader(db).get_conversation_history(task_id=1, conversation_id="c1")
        assert len(result) == 2
        assert result[0].message == "Hi" and result[1].message == "Hello"

    def test_regenerate_from_middle_includes_sibling_branches(self):
        """Sibling branches (e.g. regenerated from middle) are all included in history."""
        # Root, then two children (siblings): original branch and regenerated branch
        root = Mock()
        root.id = 1
        root.parent_message_id = None
        root.message_type = "user"
        root.message = "Root"
        root.tool_calls = []
        root.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
        child1 = Mock()
        child1.id = 2
        child1.parent_message_id = 1
        child1.message_type = "assistant"
        child1.message = "First reply"
        child1.tool_calls = []
        child1.created_at = datetime(2024, 1, 2, tzinfo=timezone.utc)
        child2 = Mock()
        child2.id = 3
        child2.parent_message_id = 1
        child2.message_type = "assistant"
        child2.message = "Regenerated reply"
        child2.tool_calls = []
        child2.created_at = datetime(2024, 1, 3, tzinfo=timezone.utc)
        db = Mock(spec=Session)
        db.execute.side_effect = [_make_chatmessage_execute_result([root, child1, child2])]
        result = ConversationHistoryReader(db).get_conversation_history(task_id=1, conversation_id="c1")
        assert len(result) == 3
        assert result[0].message == "Root"
        # Both siblings should appear (ordered by created_at)
        assert result[1].message == "First reply"
        assert result[2].message == "Regenerated reply"

    def test_regenerate_from_middle_deep_branch_included(self):
        """Regenerate-from-middle: root -> msg1 -> msg2 (original) and root -> msg3 (regenerated); all included."""
        root = Mock()
        root.id = 1
        root.parent_message_id = None
        root.message_type = "user"
        root.message = "Root"
        root.tool_calls = []
        root.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
        msg1 = Mock()
        msg1.id = 2
        msg1.parent_message_id = 1
        msg1.message_type = "assistant"
        msg1.message = "Reply 1"
        msg1.tool_calls = []
        msg1.created_at = datetime(2024, 1, 2, tzinfo=timezone.utc)
        msg2 = Mock()
        msg2.id = 3
        msg2.parent_message_id = 2
        msg2.message_type = "user"
        msg2.message = "Follow-up"
        msg2.tool_calls = []
        msg2.created_at = datetime(2024, 1, 3, tzinfo=timezone.utc)
        msg3 = Mock()
        msg3.id = 4
        msg3.parent_message_id = 1
        msg3.message_type = "assistant"
        msg3.message = "Regenerated from root"
        msg3.tool_calls = []
        msg3.created_at = datetime(2024, 1, 4, tzinfo=timezone.utc)
        db = Mock(spec=Session)
        db.execute.side_effect = [
            _make_chatmessage_execute_result([root, msg1, msg2, msg3])
        ]
        result = ConversationHistoryReader(db).get_conversation_history(task_id=1, conversation_id="c1")
        assert len(result) == 4
        ids_in_order = [m.id for m in result]
        assert ids_in_order[0] == 1
        # Children of root (2 and 4) by created_at, then subtree of 2 (3)
        assert 2 in ids_in_order and 3 in ids_in_order and 4 in ids_in_order
        assert result[0].message == "Root"
        messages = [m.message for m in result]
        assert "Reply 1" in messages
        assert "Follow-up" in messages
        assert "Regenerated from root" in messages

    def test_limit_none_returns_unbounded_history(self):
        """Passing limit=None keeps full ordered history for token-window callers."""
        msgs = []
        for idx in range(3):
            m = Mock()
            m.id = idx + 1
            m.parent_message_id = None if idx == 0 else idx
            m.message_type = "user" if idx % 2 == 0 else "assistant"
            m.message = f"m{idx + 1}"
            m.tool_calls = []
            m.created_at = datetime(2024, 1, idx + 1, tzinfo=timezone.utc)
            msgs.append(m)

        db = Mock(spec=Session)
        db.execute.side_effect = [_make_chatmessage_execute_result(msgs)]
        result = ConversationHistoryReader(db).get_conversation_history(
            task_id=1,
            conversation_id="c1",
            limit=None,
        )

        assert [m.id for m in result] == [1, 2, 3]

    def test_system_summary_markers_hidden_by_default(self):
        """SYSTEM_SUMMARY is excluded from user-visible history by default."""
        user = Mock()
        user.id = 1
        user.parent_message_id = None
        user.message_type = "user"
        user.message = "hello"
        user.tool_calls = []
        user.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)

        summary = Mock()
        summary.id = 2
        summary.parent_message_id = 1
        summary.message_type = "SYSTEM_SUMMARY"
        summary.message = "compressed snapshot"
        summary.tool_calls = []
        summary.created_at = datetime(2024, 1, 2, tzinfo=timezone.utc)

        assistant = Mock()
        assistant.id = 3
        assistant.parent_message_id = 2
        assistant.message_type = "assistant"
        assistant.message = "continuing"
        assistant.tool_calls = []
        assistant.created_at = datetime(2024, 1, 3, tzinfo=timezone.utc)

        db = Mock(spec=Session)
        db.execute.side_effect = [_make_chatmessage_execute_result([user, summary, assistant])]
        result = ConversationHistoryReader(db).get_conversation_history(task_id=1, conversation_id="c1")

        assert [m.id for m in result] == [1, 3]


class TestConvertChatMessagesToOpenAI:
    """Tests for ConversationHistoryReader.convert_chat_messages_to_openai."""

    def test_user_and_assistant_roles_converted(self):
        """User and assistant message_types become role/content."""
        user_msg = Mock()
        user_msg.message_type = "user"
        user_msg.message = "Hello"
        user_msg.tool_calls = []
        asst_msg = Mock()
        asst_msg.message_type = "assistant"
        asst_msg.message = "Hi there"
        asst_msg.tool_calls = []
        result = ConversationHistoryReader(Mock(spec=Session)).convert_chat_messages_to_openai([user_msg, asst_msg])
        assert len(result) == 2
        assert result[0] == {"role": "user", "content": "Hello"}
        assert result[1] == {"role": "assistant", "content": "Hi there"}

    def test_system_messages_skipped(self):
        """SYSTEM message_type is skipped in history."""
        user_msg = Mock()
        user_msg.message_type = "user"
        user_msg.message = "Hi"
        user_msg.tool_calls = []
        system_msg = Mock()
        system_msg.message_type = "SYSTEM"
        system_msg.message = "You are a bot"
        system_msg.tool_calls = []
        asst_msg = Mock()
        asst_msg.message_type = "assistant"
        asst_msg.message = "Hello"
        asst_msg.tool_calls = []
        result = ConversationHistoryReader(Mock(spec=Session)).convert_chat_messages_to_openai([user_msg, system_msg, asst_msg])
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"

    def test_system_summary_messages_skipped(self):
        """SYSTEM_SUMMARY message_type is skipped in user-visible OpenAI history."""
        user_msg = Mock()
        user_msg.message_type = "user"
        user_msg.message = "Hi"
        user_msg.tool_calls = []
        summary_msg = Mock()
        summary_msg.message_type = "SYSTEM_SUMMARY"
        summary_msg.message = "compressed summary"
        summary_msg.tool_calls = []
        asst_msg = Mock()
        asst_msg.message_type = "assistant"
        asst_msg.message = "Hello"
        asst_msg.tool_calls = []
        result = ConversationHistoryReader(Mock(spec=Session)).convert_chat_messages_to_openai(
            [user_msg, summary_msg, asst_msg]
        )
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"

    def test_assistant_with_tool_calls_includes_tool_calls(self):
        """Assistant message with tool_calls gets OpenAI tool_calls format."""
        tc = Mock()
        tc.tool_call_id = "call_abc"
        tc.tool_name = "get_weather"
        tc.tool_arguments = {"location": "NYC"}
        asst_msg = Mock()
        asst_msg.message_type = "assistant"
        asst_msg.message = "Calling tool."
        asst_msg.tool_calls = [tc]
        result = ConversationHistoryReader(Mock(spec=Session)).convert_chat_messages_to_openai([asst_msg])
        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        assert "tool_calls" in result[0]
        assert len(result[0]["tool_calls"]) == 1
        assert result[0]["tool_calls"][0]["id"] == "call_abc"
        assert result[0]["tool_calls"][0]["function"]["name"] == "get_weather"
        assert "location" in result[0]["tool_calls"][0]["function"]["arguments"]

    def test_message_ordering_preserved(self):
        """Order of input messages is preserved in output."""
        msgs = []
        for i in range(5):
            m = Mock()
            m.message_type = "user" if i % 2 == 0 else "assistant"
            m.message = f"msg_{i}"
            m.tool_calls = []
            msgs.append(m)
        result = ConversationHistoryReader(Mock(spec=Session)).convert_chat_messages_to_openai(msgs)
        assert [x["content"] for x in result] == ["msg_0", "msg_1", "msg_2", "msg_3", "msg_4"]


class TestBuildOpenAIConversationHistory:
    """Tests for compression-aware build_openai_conversation_history behavior."""

    def test_includes_latest_summary_and_post_summary_turns_only(self):
        db = Mock(spec=Session)
        reader = ConversationHistoryReader(db)

        old_user = Mock()
        old_user.id = 1
        old_user.parent_message_id = None
        old_user.message_type = "user"
        old_user.message = "old question"
        old_user.tool_calls = []
        old_user.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)

        old_asst = Mock()
        old_asst.id = 2
        old_asst.task_id = 1
        old_asst.conversation_id = "conv-1"
        old_asst.parent_message_id = 1
        old_asst.message_type = "assistant"
        old_asst.message = "old answer"
        old_asst.tool_calls = []
        old_asst.created_at = datetime(2024, 1, 2, tzinfo=timezone.utc)

        summary = Mock()
        summary.id = 3
        summary.task_id = 1
        summary.conversation_id = "conv-1"
        summary.parent_message_id = 2
        summary.message_type = "SYSTEM_SUMMARY"
        summary.message = "compressed continuity snapshot"
        summary.tool_calls = []
        summary.citations = {
            "context_compression": {"through_message_id": 2}
        }
        summary.created_at = datetime(2024, 1, 3, tzinfo=timezone.utc)

        new_user = Mock()
        new_user.id = 4
        new_user.parent_message_id = 3
        new_user.message_type = "user"
        new_user.message = "new question"
        new_user.tool_calls = []
        new_user.created_at = datetime(2024, 1, 4, tzinfo=timezone.utc)

        new_asst = Mock()
        new_asst.id = 5
        new_asst.parent_message_id = 4
        new_asst.message_type = "assistant"
        new_asst.message = "new answer"
        new_asst.tool_calls = []
        new_asst.created_at = datetime(2024, 1, 5, tzinfo=timezone.utc)

        db.execute.side_effect = [_make_chatmessage_execute_result([old_user, old_asst, summary, new_user, new_asst])]
        history = reader.build_openai_conversation_history(task_id=1, conversation_id="conv-1")

        assert [item["role"] for item in history] == ["system", "user", "assistant"]
        assert history[0]["content"] == "compressed continuity snapshot"
        assert history[1]["content"] == "new question"
        assert history[2]["content"] == "new answer"

    def test_uses_latest_committed_summary_as_canonical_system_message(self):
        db = Mock(spec=Session)
        reader = ConversationHistoryReader(db)

        old_user = Mock()
        old_user.id = 1
        old_user.parent_message_id = None
        old_user.message_type = "user"
        old_user.message = "old question"
        old_user.tool_calls = []
        old_user.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)

        old_summary = Mock()
        old_summary.id = 2
        old_summary.parent_message_id = 1
        old_summary.message_type = "SYSTEM_SUMMARY"
        old_summary.message = "old committed summary"
        old_summary.tool_calls = []
        old_summary.created_at = datetime(2024, 1, 2, tzinfo=timezone.utc)

        mid_user = Mock()
        mid_user.id = 3
        mid_user.parent_message_id = 2
        mid_user.message_type = "user"
        mid_user.message = "mid question"
        mid_user.tool_calls = []
        mid_user.created_at = datetime(2024, 1, 3, tzinfo=timezone.utc)

        mid_asst = Mock()
        mid_asst.id = 4
        mid_asst.task_id = 1
        mid_asst.conversation_id = "conv-1"
        mid_asst.parent_message_id = 3
        mid_asst.message_type = "assistant"
        mid_asst.message = "mid answer"
        mid_asst.tool_calls = []
        mid_asst.created_at = datetime(2024, 1, 4, tzinfo=timezone.utc)

        latest_summary = Mock()
        latest_summary.id = 5
        latest_summary.task_id = 1
        latest_summary.conversation_id = "conv-1"
        latest_summary.parent_message_id = 4
        latest_summary.message_type = "SYSTEM_SUMMARY"
        latest_summary.message = "latest committed summary"
        latest_summary.tool_calls = []
        latest_summary.citations = {
            "context_compression": {"through_message_id": 4}
        }
        latest_summary.created_at = datetime(2024, 1, 5, tzinfo=timezone.utc)

        new_user = Mock()
        new_user.id = 6
        new_user.parent_message_id = 5
        new_user.message_type = "user"
        new_user.message = "new question"
        new_user.tool_calls = []
        new_user.created_at = datetime(2024, 1, 6, tzinfo=timezone.utc)

        db.execute.side_effect = [
            _make_chatmessage_execute_result(
                [old_user, old_summary, mid_user, mid_asst, latest_summary, new_user]
            )
        ]
        history = reader.build_openai_conversation_history(task_id=1, conversation_id="conv-1")

        assert [item["role"] for item in history] == ["system", "user"]
        assert history[0]["content"] == "latest committed summary"
        assert history[1]["content"] == "new question"


class TestCompressionEpochMetadata:
    """Tests for compression epoch persistence/read and recompression guard."""

    def test_reads_latest_compression_epoch_metadata(self):
        summary = Mock()
        summary.citations = {
            "context_compression": {
                "epoch_id": "epoch-7",
                "source_tokens": 1200,
            }
        }
        db = Mock(spec=Session)
        db.execute.return_value.scalar_one_or_none.return_value = summary

        metadata = CompressionSnapshotRepository(db).latest_epoch_metadata(
            task_id=1,
            conversation_id="conv-1",
        )

        assert metadata is not None
        assert metadata.epoch_id == "epoch-7"
        assert metadata.source_tokens == 1200

    def test_persist_snapshot_writes_epoch_metadata(self, monkeypatch):
        db = Mock(spec=Session)
        db.execute.return_value.scalar_one_or_none.return_value = None

        summary_msg = Mock()
        summary_msg.id = 99
        fake_chat = Mock()
        fake_chat.reserve_message.return_value = summary_msg
        monkeypatch.setattr(
            compression_snapshot_repository,
            "ChatMessageService",
            lambda _db: fake_chat,
        )

        CompressionSnapshotRepository(db).persist_snapshot(
            task_id=1,
            conversation_id="conv-1",
            summary_text="snapshot",
            token_count=123,
            compression_epoch_id="epoch-9",
            source_tokens=3210,
        )

        fake_chat.update_message.assert_called_once()
        _, kwargs = fake_chat.update_message.call_args
        assert kwargs["citations"]["context_compression"]["epoch_id"] == "epoch-9"
        assert kwargs["citations"]["context_compression"]["source_tokens"] == 3210
