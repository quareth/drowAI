"""Tests for conversation history propagation to graph nodes.

These tests verify that conversation history flows correctly from
ChatInputs through build_metadata() to graph nodes.

NOTE: These tests require langgraph to be installed. They will be skipped
if langgraph is not available in the environment.
"""

import os

# Set mock DATABASE_URL before any imports
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")

import pytest

# Check if langgraph is available
try:
    import langgraph
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False

# Skip all tests in this module if langgraph is not available
pytestmark = pytest.mark.skipif(
    not LANGGRAPH_AVAILABLE,
    reason="langgraph not installed"
)


def _make_runtime_config_with_bundle(chat_inputs):
    """Build a LangGraphRuntimeConfig with a pre-seeded ``context_bundle``.

    After the Phase 6 single-assembly cutover, ``build_metadata`` no
    longer assembles the bundle — it copies the one placed in
    ``runtime_config.metadata`` by ``LangGraphContextBuilder``. Tests
    that construct the runtime config by hand must seed the bundle
    themselves so ``build_metadata`` can find it.
    """
    from agent.graph.context.builder import (
        METADATA_CONTEXT_BUNDLE_KEY,
        build_conversation_context_bundle,
    )
    from backend.services.langgraph_chat.contracts import (
        ExecutionMode,
        LangGraphRuntimeConfig,
    )

    bundle = build_conversation_context_bundle(
        conversation_id=chat_inputs.conversation_id or "",
        turn_id="",
        turn_sequence=0,
        messages=list(chat_inputs.history),
    )
    return LangGraphRuntimeConfig(
        chat_inputs=chat_inputs,
        execution_mode=ExecutionMode.NORMAL_CHAT,
        metadata={METADATA_CONTEXT_BUNDLE_KEY: bundle},
        persistence=None,
    )


class TestBuildMetadataHistoryPropagation:
    """Test that build_metadata() correctly propagates conversation history."""

    @pytest.fixture
    def sample_history(self):
        """Sample conversation history."""
        return [
            {"role": "user", "content": "Scan 192.168.1.1"},
            {"role": "assistant", "content": "I found ports 22, 80, and 443 open."},
            {"role": "user", "content": "What services are running on port 80?"},
        ]

    def test_build_metadata_includes_history(self, sample_history):
        """Test that build_metadata includes the full history list."""
        from backend.services.langgraph_chat.contracts import ChatInputs
        from backend.services.langgraph_chat.facade_helpers import build_metadata

        chat_inputs = ChatInputs(
            task_id=1,
            user_id=1,
            message="Check for vulnerabilities",
            conversation_id="conv-123",
            history=sample_history,
            api_key=None,
            model=None,
        )
        runtime_config = _make_runtime_config_with_bundle(chat_inputs)

        metadata = build_metadata(chat_inputs, runtime_config)

        assert "conversation_history" in metadata
        assert metadata["conversation_history"] == sample_history
        assert len(metadata["conversation_history"]) == 3

    def test_build_metadata_includes_history_turns_count(self, sample_history):
        """Test that build_metadata includes the history_turns count."""
        from backend.services.langgraph_chat.contracts import ChatInputs
        from backend.services.langgraph_chat.facade_helpers import build_metadata

        chat_inputs = ChatInputs(
            task_id=1,
            user_id=1,
            message="Check for vulnerabilities",
            conversation_id="conv-123",
            history=sample_history,
            api_key=None,
            model=None,
        )
        runtime_config = _make_runtime_config_with_bundle(chat_inputs)

        metadata = build_metadata(chat_inputs, runtime_config)

        assert "history_turns" in metadata
        assert metadata["history_turns"] == 3

    def test_build_metadata_empty_history(self):
        """Test that build_metadata handles empty history correctly."""
        from backend.services.langgraph_chat.contracts import ChatInputs
        from backend.services.langgraph_chat.facade_helpers import build_metadata

        chat_inputs = ChatInputs(
            task_id=1,
            user_id=1,
            message="Start a new scan",
            conversation_id="conv-456",
            history=[],
            api_key=None,
            model=None,
        )
        runtime_config = _make_runtime_config_with_bundle(chat_inputs)

        metadata = build_metadata(chat_inputs, runtime_config)

        assert "conversation_history" in metadata
        assert metadata["conversation_history"] == []
        assert metadata["history_turns"] == 0

    def test_history_is_list_copy(self, sample_history):
        """Test that history in metadata is a list copy, not iterator."""
        from backend.services.langgraph_chat.contracts import ChatInputs
        from backend.services.langgraph_chat.facade_helpers import build_metadata

        chat_inputs = ChatInputs(
            task_id=1,
            user_id=1,
            message="Test",
            conversation_id="conv-789",
            history=sample_history,
            api_key=None,
            model=None,
        )
        runtime_config = _make_runtime_config_with_bundle(chat_inputs)

        metadata = build_metadata(chat_inputs, runtime_config)

        # Should be a list, not an iterator
        assert isinstance(metadata["conversation_history"], list)

        # Should be able to iterate multiple times
        first_pass = list(metadata["conversation_history"])
        second_pass = list(metadata["conversation_history"])
        assert first_pass == second_pass == sample_history


# NOTE: ``TestHistoryFormatterIntegration`` was removed in Phase 6.
# The ``format_conversation_history`` helper it exercised was deleted
# after the Phase 5 cutover eliminated its last prompt consumer —
# cross-turn continuity is now rendered by
# ``serialize_projection_to_prompt_sections`` in
# ``agent/graph/context/projections.py``.


class TestBackwardCompatibility:
    """Test backward compatibility of history module re-exports."""

    def test_post_tool_reasoning_imports_work(self):
        """Test that imports from post_tool_reasoning/history.py still work."""
        from agent.graph.nodes.post_tool_reasoning.history import (
            MAX_HISTORY_ENTRIES,
            MAX_HISTORY_CONTENT_CHARS,
            truncate_content,
            build_conversation_history,
            build_conversation_history_from_state,
        )

        # Constants should be available. 2026-04-14: per-entry budget was
        # scaled 4x as part of the memory/prompt char-limit uplift.
        assert MAX_HISTORY_ENTRIES == 120
        assert MAX_HISTORY_CONTENT_CHARS == 8000

        # Functions should be callable
        assert callable(truncate_content)
        assert callable(build_conversation_history)
        assert callable(build_conversation_history_from_state)

    def test_truncate_content_works(self):
        """Test that truncate_content works correctly."""
        from agent.graph.nodes.post_tool_reasoning.history import truncate_content

        short = "Hello"
        assert truncate_content(short) == "Hello"

        long = "x" * 600
        result = truncate_content(long, max_chars=100)
        assert len(result) <= 100
        assert result.endswith("…")

    def test_build_conversation_history_works(self):
        """Test that build_conversation_history works correctly."""
        from agent.graph.nodes.post_tool_reasoning.history import build_conversation_history

        result = build_conversation_history(
            trace_observations=["Observation 1"],
            trace_reasoning=["Decision: proceed"],
        )

        assert "No prior context available" in result
