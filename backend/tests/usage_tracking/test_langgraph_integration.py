"""Tests for LangGraph usage tracking integration.

These tests verify the middleware and handlers correctly capture
and propagate token usage data.
"""

import pytest
import sys
import importlib.util
from unittest.mock import MagicMock, AsyncMock, patch

from backend.services.usage_tracking.models import ProviderUsageComponents, UsageData

# Import middleware directly to avoid langgraph_chat package __init__ which has heavy deps
# The usage_middleware module itself has no langgraph dependencies
import importlib.util
spec = importlib.util.spec_from_file_location(
    "usage_middleware",
    "backend/services/langgraph_chat/runtime/usage_middleware.py"
)
usage_middleware = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage_middleware)

UsageCollector = usage_middleware.UsageCollector
record_turn_usage = usage_middleware.record_turn_usage
record_usage_list_best_effort = usage_middleware.record_usage_list_best_effort
create_usage_aware_llm_wrapper = usage_middleware.create_usage_aware_llm_wrapper


class TestUsageCollector:
    """Tests for UsageCollector."""
    
    def test_empty_collector(self):
        """New collector should be empty."""
        collector = UsageCollector()
        
        assert collector.is_empty
        assert len(collector) == 0
        assert collector.total_tokens == 0
        assert collector.entries == []
    
    def test_add_usage(self):
        """Should accumulate usage entries."""
        collector = UsageCollector()
        
        usage1 = UsageData(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            model="gpt-4o-mini",
        )
        usage2 = UsageData(
            prompt_tokens=200,
            completion_tokens=100,
            total_tokens=300,
            model="gpt-4o",
        )
        
        collector.add(usage1, source="call_1")
        collector.add(usage2, source="call_2")
        
        assert not collector.is_empty
        assert len(collector) == 2
        assert collector.total_tokens == 450
    
    def test_add_none_usage(self):
        """Should skip None usage."""
        collector = UsageCollector()
        
        collector.add(None, source="empty_call")
        
        assert collector.is_empty
        assert len(collector) == 0
    
    def test_add_empty_usage(self):
        """Should skip empty usage."""
        collector = UsageCollector()
        
        empty_usage = UsageData.empty("gpt-4o")
        collector.add(empty_usage, source="empty_call")
        
        assert collector.is_empty
        assert len(collector) == 0
    
    def test_entries_contain_metadata(self):
        """Entries should include source and metadata."""
        collector = UsageCollector()
        
        usage = UsageData(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            model="gpt-4o-mini",
        )
        
        collector.add(usage, source="test_call", metadata={"key": "value"})
        
        assert len(collector.entries) == 1
        entry = collector.entries[0]
        assert entry["usage"] == usage
        assert entry["source"] == "test_call"
        assert entry["metadata"] == {"key": "value"}
    
    def test_clear(self):
        """Clear should remove all entries."""
        collector = UsageCollector()
        
        usage = UsageData(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            model="gpt-4o-mini",
        )
        
        collector.add(usage, source="call_1")
        collector.add(usage, source="call_2")
        
        assert len(collector) == 2
        
        collector.clear()
        
        assert collector.is_empty
        assert len(collector) == 0


class TestRecordTurnUsage:
    """Tests for record_turn_usage helper."""
    
    def test_record_empty_collector(self):
        """Should return 0 for empty collector."""
        mock_db = MagicMock()
        collector = UsageCollector()
        
        result = record_turn_usage(
            db=mock_db,
            task_id=123,
            user_id=1,
            collector=collector,
            source="test",
        )
        
        assert result == 0
    
    def test_record_persists_entries(self):
        """Should persist all entries to database."""
        mock_db = MagicMock()
        
        collector = UsageCollector()
        usage1 = UsageData(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            model="gpt-4o-mini",
        )
        usage2 = UsageData(
            prompt_tokens=200,
            completion_tokens=100,
            total_tokens=300,
            model="gpt-4o",
        )
        
        collector.add(usage1, source="call_1")
        collector.add(usage2, source="call_2")
        
        # Patch the import path that record_turn_usage uses
        with patch('backend.services.usage_tracking.service.UsageTrackingService') as MockService:
            mock_service = MagicMock()
            mock_service.record_usage.return_value = MagicMock()  # Non-None means success
            MockService.return_value = mock_service
            
            result = record_turn_usage(
                db=mock_db,
                task_id=123,
                user_id=1,
                collector=collector,
                source="langgraph",
                conversation_id="conv-123",
            )
            
            assert result == 2
            assert mock_service.record_usage.call_count == 2
            
            # Verify source formatting
            calls = mock_service.record_usage.call_args_list
            assert calls[0][1]["source"] == "langgraph:call_1"
            assert calls[1][1]["source"] == "langgraph:call_2"


class TestUsageAwareLLMWrapper:
    """Tests for create_usage_aware_llm_wrapper."""
    
    @pytest.mark.asyncio
    async def test_wrapper_collects_usage_from_chat(self):
        """Wrapper should collect usage from chat_messages_with_usage."""
        collector = UsageCollector()
        
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "Hello!"
        mock_response.usage = UsageData(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            model="gpt-4o-mini",
        )
        mock_client.chat_messages_with_usage.return_value = mock_response
        
        wrapped = create_usage_aware_llm_wrapper(
            mock_client,
            collector,
            source="test_call",
        )
        
        response = await wrapped.chat_messages_with_usage([{"role": "user", "content": "Hi"}])
        
        assert response.content == "Hello!"
        assert len(collector) == 1
        assert collector.total_tokens == 15
    
    @pytest.mark.asyncio
    async def test_wrapper_forwards_other_methods(self):
        """Wrapper should forward other methods to underlying client."""
        collector = UsageCollector()
        
        mock_client = MagicMock()
        mock_client.model = "gpt-4o-mini"
        
        wrapped = create_usage_aware_llm_wrapper(
            mock_client,
            collector,
            source="test",
        )
        
        assert wrapped.model == "gpt-4o-mini"

    def test_wrapper_preserves_streaming_usage_method_absence(self):
        """Wrapper should not make unsupported usage-aware streaming appear available."""
        collector = UsageCollector()

        class NonStreamingClient:
            async def chat_messages_with_usage(self, *_args, **_kwargs):
                return MagicMock(usage=None)

        wrapped = create_usage_aware_llm_wrapper(
            NonStreamingClient(),
            collector,
            source="test",
        )

        assert not hasattr(wrapped, "stream_chat_messages_with_usage")

    @pytest.mark.asyncio
    async def test_wrapper_collects_usage_from_streaming_final_usage(self):
        """Wrapper should collect final stream usage when the client supports it."""
        collector = UsageCollector()
        usage = UsageData(
            prompt_tokens=11,
            completion_tokens=7,
            total_tokens=18,
            model="gpt-5.2",
        )

        class StreamingResponse:
            def get_final_usage(self):
                return usage

        class StreamingClient:
            async def chat_messages_with_usage(self, *_args, **_kwargs):
                return MagicMock(usage=None)

            async def stream_chat_messages_with_usage(self, *_args, **_kwargs):
                return StreamingResponse()

        wrapped = create_usage_aware_llm_wrapper(
            StreamingClient(),
            collector,
            source="stream_call",
        )

        assert hasattr(wrapped, "stream_chat_messages_with_usage")
        response = await wrapped.stream_chat_messages_with_usage(
            [{"role": "user", "content": "Hi"}]
        )
        assert response.get_final_usage() is usage
        assert len(collector) == 1
        assert collector.total_tokens == 18


class TestGraphUsageProjectionHelpers:
    """Manual usage projection paths must not drop provider-specific fields."""

    def test_node_utils_object_fallback_preserves_provider_components(self):
        from agent.graph.nodes.node_utils import _usage_to_dict

        usage = MagicMock()
        usage.to_dict.side_effect = RuntimeError("force fallback")
        usage.prompt_tokens = 150
        usage.completion_tokens = 50
        usage.total_tokens = 200
        usage.model = "claude-sonnet-4-5"
        usage.provider = "anthropic"
        usage.cached_tokens = 0
        usage.reasoning_tokens = 0
        usage.api_surface = "messages"
        usage.cache_reporting = "unknown"
        usage.provider_usage_components = ProviderUsageComponents(
            provider="anthropic",
            api_surface="messages",
            components={"input_tokens": 100, "output_tokens": 50},
        )

        result = _usage_to_dict(usage, "planner")

        assert result is not None
        assert result["api_surface"] == "messages"
        assert result["cache_reporting"] == "unknown"
        assert result["provider_usage_components"] == {
            "provider": "anthropic",
            "api_surface": "messages",
            "components": {"input_tokens": 100, "output_tokens": 50},
        }

    def test_node_utils_attaches_known_request_mode(self):
        from agent.graph.nodes.node_utils import _usage_to_dict

        usage = UsageData(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            model="gpt-4o-mini",
            provider="openai",
            api_surface="chat_completions",
            cache_reporting="reported",
        )

        result = _usage_to_dict(
            usage,
            "simple_chat",
            request_mode="streaming",
        )

        assert result is not None
        assert result["request_mode"] == "streaming"

    def test_node_utils_dict_fallback_preserves_provider_components(self):
        from agent.graph.nodes.node_utils import _usage_to_dict

        components = {
            "provider": "anthropic",
            "api_surface": "messages",
            "components": {"input_tokens": 100, "output_tokens": 50},
        }

        result = _usage_to_dict(
            {
                "prompt_tokens": 150,
                "completion_tokens": 50,
                "total_tokens": 200,
                "model": "claude-sonnet-4-5",
                "provider": "anthropic",
                "api_surface": "messages",
                "cache_reporting": "unknown",
                "provider_usage_components": components,
            },
            "planner",
        )

        assert result is not None
        assert result["provider_usage_components"] == components

    def test_intent_classifier_injection_preserves_provider_components(self):
        from backend.services.langgraph_chat.facade_helpers import (
            inject_intent_classifier_usage,
        )

        usage = UsageData(
            prompt_tokens=150,
            completion_tokens=50,
            total_tokens=200,
            model="claude-sonnet-4-5",
            provider="anthropic",
            api_surface="messages",
            provider_usage_components=ProviderUsageComponents(
                provider="anthropic",
                api_surface="messages",
                components={"input_tokens": 100, "output_tokens": 50},
            ),
        )
        runtime_config = MagicMock()
        runtime_config.metadata = {"_intent_classifier_usage": usage}
        initial_state = {"trace": {"usage_records": []}}

        injected = inject_intent_classifier_usage(
            initial_state=initial_state,
            runtime_config=runtime_config,
        )

        assert injected == 200
        record = initial_state["trace"]["usage_records"][0]
        assert record["provider_usage_components"] == {
            "provider": "anthropic",
            "api_surface": "messages",
            "components": {"input_tokens": 100, "output_tokens": 50},
        }


class TestRecordUsageListBestEffort:
    """Tests for record_usage_list_best_effort helper."""

    def test_record_usage_list_best_effort_skips_empty_input(self):
        """No session should be created when usage_list is empty."""
        session_factory = MagicMock()

        record_usage_list_best_effort(
            task_id=123,
            user_id=1,
            usage_list=[],
            source="langgraph_resume",
            conversation_id="conv-1",
            session_factory=session_factory,
        )

        session_factory.assert_not_called()

    def test_record_usage_list_best_effort_records_each_non_none_usage(self):
        """Persist each non-None usage entry and close session."""
        session = MagicMock()
        usage_a = UsageData(prompt_tokens=10, completion_tokens=5, total_tokens=15, model="gpt-4o-mini")
        usage_b = UsageData(prompt_tokens=20, completion_tokens=10, total_tokens=30, model="gpt-4o-mini")

        with patch("backend.services.usage_tracking.service.UsageTrackingService") as MockService:
            service = MagicMock()
            MockService.return_value = service
            record_usage_list_best_effort(
                task_id=321,
                user_id=9,
                usage_list=[usage_a, None, usage_b],
                source="langgraph_resume",
                conversation_id="conv-9",
                session_factory=lambda: session,
            )

        assert service.record_usage.call_count == 2
        first_call = service.record_usage.call_args_list[0][1]
        second_call = service.record_usage.call_args_list[1][1]
        assert first_call["source"] == "langgraph_resume"
        assert second_call["source"] == "langgraph_resume"
        assert first_call["task_id"] == 321
        assert second_call["task_id"] == 321
        # Legacy plain-UsageData items pass through without canonical metadata.
        assert first_call.get("usage_metadata") is None
        assert second_call.get("usage_metadata") is None
        session.close.assert_called_once()

    def test_record_usage_list_best_effort_forwards_envelope_metadata(self):
        """A ``UsageRecordWithMetadata`` envelope is unwrapped so the
        canonical metadata reaches ``UsageTrackingService.record_usage`` and
        ultimately ``LLMUsageRecord.request_metadata``."""
        from backend.services.usage_tracking.insights_models import (
            UsageRecordMetadata,
            UsageRecordWithMetadata,
        )

        session = MagicMock()
        usage = UsageData(
            prompt_tokens=10, completion_tokens=5, total_tokens=15, model="gpt-4o-mini"
        )
        metadata = UsageRecordMetadata(
            role="planner",
            node_name="tool_selector",
            execution_branch="simple_tool",
            provider="openai",
            turn_index=4,
        )
        envelope = UsageRecordWithMetadata(usage=usage, metadata=metadata)

        with patch("backend.services.usage_tracking.service.UsageTrackingService") as MockService:
            service = MagicMock()
            MockService.return_value = service
            record_usage_list_best_effort(
                task_id=77,
                user_id=2,
                usage_list=[envelope],
                source="langgraph",
                conversation_id="conv-77",
                session_factory=lambda: session,
            )

        assert service.record_usage.call_count == 1
        call_kwargs = service.record_usage.call_args_list[0][1]
        # The envelope is unwrapped: ``usage`` is the underlying UsageData
        # and the canonical metadata is forwarded verbatim.
        assert call_kwargs["usage"] is usage
        assert call_kwargs["usage_metadata"] is metadata
        assert call_kwargs["source"] == "langgraph"

    def test_record_usage_list_best_effort_handles_mixed_envelope_and_plain(self):
        """A list mixing plain ``UsageData`` and envelopes is processed
        without dropping either side — defensive behavior so incremental
        roll-out does not require every call-site to adopt the envelope
        at the same time."""
        from backend.services.usage_tracking.insights_models import (
            UsageRecordMetadata,
            UsageRecordWithMetadata,
        )

        session = MagicMock()
        plain = UsageData(prompt_tokens=1, completion_tokens=1, total_tokens=2, model="m")
        envelope = UsageRecordWithMetadata(
            usage=UsageData(prompt_tokens=3, completion_tokens=3, total_tokens=6, model="m"),
            metadata=UsageRecordMetadata(role="simple_chat"),
        )

        with patch("backend.services.usage_tracking.service.UsageTrackingService") as MockService:
            service = MagicMock()
            MockService.return_value = service
            record_usage_list_best_effort(
                task_id=1,
                user_id=1,
                usage_list=[plain, envelope],
                source="langgraph",
                conversation_id=None,
                session_factory=lambda: session,
            )

        assert service.record_usage.call_count == 2
        first_call = service.record_usage.call_args_list[0][1]
        second_call = service.record_usage.call_args_list[1][1]
        assert first_call.get("usage_metadata") is None
        assert second_call["usage_metadata"].role == "simple_chat"


class TestRecordUsageListBestEffortRoundTrip:
    """End-to-end test: envelope list -> ``record_usage_list_best_effort`` ->
    ``UsageTrackingService.record_usage`` -> ``LLMUsageRecord.request_metadata``.

    This exercises the exact real-write path Task 1.2 is responsible for and
    is the regression guard against handlers re-narrowing metadata in the
    future."""

    def test_canonical_metadata_round_trips_to_llm_usage_record(self):
        from backend.services.usage_tracking.insights_models import (
            UsageRecordMetadata,
            UsageRecordWithMetadata,
        )

        session = MagicMock()
        usage = UsageData(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            model="gpt-4o-mini",
        )
        envelope = UsageRecordWithMetadata(
            usage=usage,
            metadata=UsageRecordMetadata(
                role="planner",
                node_name="tool_selector",
                execution_branch="simple_tool",
                provider="openai",
                turn_index=4,
            ),
        )

        with patch("backend.services.usage_tracking.service.LLMUsageRecord") as MockRecord:
            MockRecord.return_value = MagicMock()

            record_usage_list_best_effort(
                task_id=42,
                user_id=7,
                usage_list=[envelope],
                source="langgraph",
                conversation_id="conv-42",
                session_factory=lambda: session,
            )

            MockRecord.assert_called_once()
            call_kwargs = MockRecord.call_args[1]
            assert call_kwargs["task_id"] == 42
            assert call_kwargs["user_id"] == 7
            assert call_kwargs["prompt_tokens"] == 100
            assert call_kwargs["completion_tokens"] == 50
            assert call_kwargs["source"] == "langgraph"
            # Canonical metadata serialized into the JSON column.
            assert call_kwargs["request_metadata"] == {
                "role": "planner",
                "node_name": "tool_selector",
                "execution_branch": "simple_tool",
                "provider": "openai",
                "api_surface": "unknown",
                "request_mode": "unknown",
                "cache_reporting": "unknown",
                "turn_index": 4,
            }

    def test_plain_usage_data_persists_without_canonical_metadata(self):
        """Missing metadata still persists safely: ``request_metadata`` is
        ``None`` (no canonical contract was attached), but the row is
        otherwise identical to the legacy behavior."""
        session = MagicMock()
        usage = UsageData(
            prompt_tokens=10, completion_tokens=5, total_tokens=15, model="gpt-4o-mini"
        )

        with patch("backend.services.usage_tracking.service.LLMUsageRecord") as MockRecord:
            MockRecord.return_value = MagicMock()

            record_usage_list_best_effort(
                task_id=1,
                user_id=1,
                usage_list=[usage],
                source="langgraph_legacy",
                conversation_id=None,
                session_factory=lambda: session,
            )

            call_kwargs = MockRecord.call_args[1]
            # No envelope -> no canonical metadata attached. This is safe:
            # insights queries treat missing ``request_metadata`` as the
            # explicit ``"unknown"`` bucket.
            assert call_kwargs["request_metadata"] is None

    def test_provider_usage_components_round_trip_to_request_metadata(self):
        from backend.services.usage_tracking.insights_models import (
            UsageRecordMetadata,
            UsageRecordWithMetadata,
        )

        session = MagicMock()
        usage = UsageData(
            prompt_tokens=150,
            completion_tokens=50,
            total_tokens=200,
            model="claude-sonnet-4-5",
            provider="anthropic",
            api_surface="messages",
            provider_usage_components=ProviderUsageComponents(
                provider="anthropic",
                api_surface="messages",
                components={
                    "input_tokens": 100,
                    "cache_creation_input_tokens": 30,
                    "cache_read_input_tokens": 20,
                    "output_tokens": 50,
                },
            ),
        )
        envelope = UsageRecordWithMetadata(
            usage=usage,
            metadata=UsageRecordMetadata(
                role="planner",
                node_name="decision_router",
                execution_branch="deep_reasoning",
                provider="anthropic",
                api_surface="messages",
            ),
        )

        with patch("backend.services.usage_tracking.service.LLMUsageRecord") as MockRecord:
            MockRecord.return_value = MagicMock()

            record_usage_list_best_effort(
                task_id=42,
                user_id=7,
                usage_list=[envelope],
                source="langgraph",
                conversation_id="conv-42",
                session_factory=lambda: session,
            )

            request_metadata = MockRecord.call_args[1]["request_metadata"]
            assert request_metadata["provider_usage_components"] == {
                "provider": "anthropic",
                "api_surface": "messages",
                "components": {
                    "input_tokens": 100,
                    "cache_creation_input_tokens": 30,
                    "cache_read_input_tokens": 20,
                    "output_tokens": 50,
                },
            }


class TestExtractUsageFromState:
    """``_extract_usage_from_state`` is the handler-boundary normalizer
    that turns ``trace.usage_records`` dicts into
    ``UsageRecordWithMetadata`` envelopes. These tests lock in the role /
    node_name / execution_branch mapping so handlers don't silently
    narrow back to plain ``UsageData`` in a future refactor."""

    def _build_state_with_records(self, records):
        class _Trace:
            pass

        class _State:
            pass

        trace = _Trace()
        trace.usage_records = records
        state = _State()
        state.trace = trace
        return state

    def test_produces_envelope_list_with_canonical_metadata(self):
        try:
            from backend.services.langgraph_chat.handlers.normal_chat_handler import (
                _extract_usage_from_state,
            )
        except ImportError:
            pytest.skip("langgraph_chat package not available")

        state = self._build_state_with_records(
            [
                {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "model": "gpt-4o-mini",
                    "provider": "openai",
                    "source": "simple_chat",
                },
                {
                    "prompt_tokens": 7,
                    "completion_tokens": 3,
                    "total_tokens": 10,
                    "model": "gpt-4o-mini",
                    "provider": "openai",
                    "source": "decision_router",
                },
            ]
        )

        result = _extract_usage_from_state(
            state, execution_branch="deep_reasoning", turn_index=2
        )

        assert result is not None and len(result) == 2

        first, second = result
        assert first.usage.total_tokens == 15
        assert first.metadata.role == "simple_chat"
        assert first.metadata.node_name == "simple_chat"
        assert first.metadata.execution_branch == "deep_reasoning"
        assert first.metadata.provider == "openai"
        assert first.metadata.turn_index == 2

        # decision_router -> planner / decision_router per the canonical map
        assert second.metadata.role == "planner"
        assert second.metadata.node_name == "decision_router"
        assert second.metadata.execution_branch == "deep_reasoning"

    def test_returns_none_when_no_records(self):
        try:
            from backend.services.langgraph_chat.handlers.normal_chat_handler import (
                _extract_usage_from_state,
            )
        except ImportError:
            pytest.skip("langgraph_chat package not available")

        state = self._build_state_with_records([])
        assert _extract_usage_from_state(state, execution_branch="simple_chat") is None

    def test_unknown_source_falls_back_to_unknown_role(self):
        try:
            from backend.services.langgraph_chat.handlers.normal_chat_handler import (
                _extract_usage_from_state,
            )
        except ImportError:
            pytest.skip("langgraph_chat package not available")

        state = self._build_state_with_records(
            [
                {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                    "model": "gpt-4o-mini",
                    "provider": "openai",
                    "source": "brand_new_node",
                }
            ]
        )

        result = _extract_usage_from_state(state, execution_branch="simple_chat")
        assert result is not None and len(result) == 1
        assert result[0].metadata.role == "unknown"
        assert result[0].metadata.node_name == "unknown"
        # But execution_branch + provider survive because they come from
        # the handler, not the source string.
        assert result[0].metadata.execution_branch == "simple_chat"
        assert result[0].metadata.provider == "openai"

    def test_preserves_surface_cache_reporting_and_provider_components(self):
        try:
            from backend.services.langgraph_chat.handlers.normal_chat_handler import (
                _extract_usage_from_state,
            )
        except ImportError:
            pytest.skip("langgraph_chat package not available")

        components = {
            "provider": "anthropic",
            "api_surface": "messages",
            "components": {
                "input_tokens": 100,
                "cache_creation_input_tokens": 30,
                "cache_read_input_tokens": 20,
                "output_tokens": 50,
            },
        }
        state = self._build_state_with_records(
            [
                {
                    "prompt_tokens": 150,
                    "completion_tokens": 50,
                    "total_tokens": 200,
                    "model": "claude-sonnet-4-5",
                    "provider": "anthropic",
                    "api_surface": "messages",
                    "cache_reporting": "unknown",
                    "request_mode": "streaming",
                    "provider_usage_components": components,
                    "source": "decision_router",
                }
            ]
        )

        result = _extract_usage_from_state(
            state, execution_branch="deep_reasoning", turn_index=2
        )

        assert result is not None and len(result) == 1
        envelope = result[0]
        assert envelope.usage.api_surface == "messages"
        assert envelope.usage.cache_reporting == "unknown"
        assert envelope.usage.provider_usage_components is not None
        assert envelope.usage.provider_usage_components.to_dict() == components
        assert envelope.metadata.api_surface == "messages"
        assert envelope.metadata.cache_reporting == "unknown"
        assert envelope.metadata.request_mode == "streaming"

    def test_tool_output_compressor_source_persists_with_canonical_metadata(self):
        try:
            from backend.services.langgraph_chat.handlers.normal_chat_handler import (
                _extract_usage_from_state,
            )
        except ImportError:
            pytest.skip("langgraph_chat package not available")

        components = {
            "provider": "anthropic",
            "api_surface": "messages",
            "components": {"input_tokens": 4065, "output_tokens": 465},
        }
        state = self._build_state_with_records(
            [
                {
                    "prompt_tokens": 4065,
                    "completion_tokens": 465,
                    "total_tokens": 4530,
                    "model": "claude-haiku-4-5-20251001",
                    "provider": "anthropic",
                    "api_surface": "messages",
                    "cache_reporting": "unknown",
                    "request_mode": "non_streaming",
                    "provider_usage_components": components,
                    "source": "tool_output_compressor",
                }
            ]
        )

        result = _extract_usage_from_state(
            state, execution_branch="simple_tool", turn_index=1
        )

        assert result is not None and len(result) == 1
        envelope = result[0]
        assert envelope.usage.model == "claude-haiku-4-5-20251001"
        assert envelope.usage.provider == "anthropic"
        assert envelope.usage.provider_usage_components is not None
        assert envelope.usage.provider_usage_components.to_dict() == components
        assert envelope.metadata.role == "tool_output_compressor"
        assert envelope.metadata.node_name == "tool_output_compressor"
        assert envelope.metadata.api_surface == "messages"
        assert envelope.metadata.request_mode == "non_streaming"


class TestLangGraphChatResultUsage:
    """Tests for usage in LangGraphChatResult.
    
    These tests require langgraph to be installed since the contracts module
    has dependencies that are hard to mock with dynamic import.
    """
    
    def test_result_with_usage(self):
        """LangGraphChatResult should store usage data."""
        try:
            from backend.services.langgraph_chat.contracts import LangGraphChatResult
        except ImportError:
            pytest.skip("langgraph_chat package not available")
        
        usage_list = [
            UsageData(
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
                model="gpt-4o-mini",
            ),
            UsageData(
                prompt_tokens=200,
                completion_tokens=100,
                total_tokens=300,
                model="gpt-4o",
            ),
        ]
        
        result = LangGraphChatResult(
            final_text="Hello!",
            conversation_id="conv-123",
            usage=usage_list,
        )
        
        assert result.usage == usage_list
        assert result.total_tokens == 450
    
    def test_result_without_usage(self):
        """LangGraphChatResult should handle missing usage."""
        try:
            from backend.services.langgraph_chat.contracts import LangGraphChatResult
        except ImportError:
            pytest.skip("langgraph_chat package not available")

        result = LangGraphChatResult(
            final_text="Hello!",
            conversation_id="conv-123",
        )

        assert result.usage is None
        assert result.total_tokens == 0

    def test_result_total_tokens_unwraps_metadata_envelope(self):
        """``total_tokens`` must transparently sum through the
        ``UsageRecordWithMetadata`` envelope — no caller should have to
        unwrap manually after Task 1.2 widens the ``usage`` type."""
        try:
            from backend.services.langgraph_chat.contracts import LangGraphChatResult
            from backend.services.usage_tracking.insights_models import (
                UsageRecordMetadata,
                UsageRecordWithMetadata,
            )
        except ImportError:
            pytest.skip("langgraph_chat package not available")

        usage_a = UsageData(
            prompt_tokens=100, completion_tokens=50, total_tokens=150, model="gpt-4o-mini"
        )
        usage_b = UsageData(
            prompt_tokens=200, completion_tokens=100, total_tokens=300, model="gpt-4o"
        )

        # Mix envelopes and plain UsageData to prove the property handles both.
        result = LangGraphChatResult(
            final_text="ok",
            conversation_id="conv",
            usage=[
                UsageRecordWithMetadata(
                    usage=usage_a, metadata=UsageRecordMetadata(role="simple_chat")
                ),
                usage_b,
            ],
        )

        assert result.total_tokens == 450
