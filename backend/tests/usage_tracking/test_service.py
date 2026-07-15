"""Tests for UsageTrackingService.

These tests verify the service correctly records and queries token usage
from the database.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from backend.services.usage_tracking.insights_models import UsageRecordMetadata
from backend.services.usage_tracking.models import (
    ProviderUsageComponents,
    UsageData,
    TaskUsageSummary,
)
from backend.services.usage_tracking.service import UsageTrackingService


class TestUsageTrackingServiceRecordUsage:
    """Tests for UsageTrackingService.record_usage()"""
    
    def test_record_usage_creates_record(self):
        """Should create LLMUsageRecord in database."""
        # Mock database session
        mock_db = MagicMock()
        mock_record = MagicMock()
        mock_db.add = MagicMock()
        mock_db.commit = MagicMock()
        mock_db.refresh = MagicMock()
        
        # Patch LLMUsageRecord to capture creation
        with patch('backend.services.usage_tracking.service.LLMUsageRecord') as MockRecord:
            MockRecord.return_value = mock_record
            
            service = UsageTrackingService(mock_db)
            usage = UsageData(
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
                model="gpt-4o-mini",
            )
            
            result = service.record_usage(
                task_id=123,
                user_id=1,
                usage=usage,
                source="test_source",
                conversation_id="conv-123",
            )
            
            # Verify record was created with correct values
            MockRecord.assert_called_once()
            call_kwargs = MockRecord.call_args[1]
            assert call_kwargs["task_id"] == 123
            assert call_kwargs["user_id"] == 1
            assert call_kwargs["prompt_tokens"] == 100
            assert call_kwargs["completion_tokens"] == 50
            assert call_kwargs["total_tokens"] == 150
            assert call_kwargs["model"] == "gpt-4o-mini"
            assert call_kwargs["source"] == "test_source"
            assert call_kwargs["conversation_id"] == "conv-123"
            
            # Verify DB operations
            mock_db.add.assert_called_once_with(mock_record)
            mock_db.commit.assert_called_once()
            mock_db.refresh.assert_called_once_with(mock_record)
    
    def test_record_usage_skips_empty_usage(self):
        """Should skip recording when usage is empty."""
        mock_db = MagicMock()
        
        service = UsageTrackingService(mock_db)
        usage = UsageData.empty("gpt-4o")
        
        result = service.record_usage(
            task_id=123,
            user_id=1,
            usage=usage,
            source="test",
        )
        
        assert result is None
        mock_db.add.assert_not_called()
        mock_db.commit.assert_not_called()
    
    def test_record_usage_persists_canonical_usage_metadata(self):
        """When ``usage_metadata`` is provided, it is serialized into
        ``request_metadata`` via ``serialize_usage_metadata`` so downstream
        insights groups on role/node/branch instead of parsing ``source``."""
        mock_db = MagicMock()
        mock_record = MagicMock()

        with patch("backend.services.usage_tracking.service.LLMUsageRecord") as MockRecord:
            MockRecord.return_value = mock_record

            service = UsageTrackingService(mock_db)
            usage = UsageData(
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
                model="gpt-4o-mini",
            )
            usage_metadata = UsageRecordMetadata(
                role="planner",
                node_name="tool_selector",
                execution_branch="simple_tool",
                provider="openai",
                api_surface="chat_completions",
                request_mode="streaming",
                cache_reporting="reported",
                turn_index=2,
            )

            service.record_usage(
                task_id=7,
                user_id=1,
                usage=usage,
                source="langgraph",
                conversation_id="conv-7",
                usage_metadata=usage_metadata,
            )

            MockRecord.assert_called_once()
            call_kwargs = MockRecord.call_args[1]
            # Canonical metadata round-trips 1:1 into request_metadata.
            assert call_kwargs["request_metadata"] == {
                "role": "planner",
                "node_name": "tool_selector",
                "execution_branch": "simple_tool",
                "provider": "openai",
                "api_surface": "chat_completions",
                "request_mode": "streaming",
                "cache_reporting": "reported",
                "turn_index": 2,
            }
            # Coarse source is still preserved for routing/debug.
            assert call_kwargs["source"] == "langgraph"

    def test_record_usage_without_metadata_keeps_legacy_dict(self):
        """When ``usage_metadata`` is absent, the legacy ``metadata`` dict is
        stored with additive canonical provider metadata when it can be lifted
        from ``UsageData``."""
        mock_db = MagicMock()

        with patch("backend.services.usage_tracking.service.LLMUsageRecord") as MockRecord:
            MockRecord.return_value = MagicMock()

            service = UsageTrackingService(mock_db)
            usage = UsageData(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                model="gpt-4o-mini",
            )

            service.record_usage(
                task_id=9,
                user_id=1,
                usage=usage,
                source="legacy_caller",
                metadata={"debug": "info"},
            )

            call_kwargs = MockRecord.call_args[1]
            assert call_kwargs["request_metadata"] == {
                "debug": "info",
                "provider": "openai",
                "api_surface": "unknown",
                "cache_reporting": "unknown",
            }

    def test_record_usage_fills_missing_canonical_fields_from_usage(self):
        mock_db = MagicMock()

        with patch("backend.services.usage_tracking.service.LLMUsageRecord") as MockRecord:
            MockRecord.return_value = MagicMock()

            service = UsageTrackingService(mock_db)
            usage = UsageData(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                model="claude-sonnet-4-5",
                provider="anthropic",
                api_surface="messages",
                cache_reporting="unknown",
            )

            service.record_usage(
                task_id=9,
                user_id=1,
                usage=usage,
                source="langgraph",
                usage_metadata=UsageRecordMetadata(role="planner"),
            )

            request_metadata = MockRecord.call_args[1]["request_metadata"]
            assert request_metadata["provider"] == "anthropic"
            assert request_metadata["api_surface"] == "messages"
            assert request_metadata["cache_reporting"] == "unknown"

    def test_record_usage_canonical_metadata_wins_over_legacy_dict(self):
        """When both are provided, the canonical contract wins so insights
        never sees stale debug-style dicts shadowing the typed contract."""
        mock_db = MagicMock()

        with patch("backend.services.usage_tracking.service.LLMUsageRecord") as MockRecord:
            MockRecord.return_value = MagicMock()

            service = UsageTrackingService(mock_db)
            usage = UsageData(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                model="gpt-4o-mini",
            )

            service.record_usage(
                task_id=9,
                user_id=1,
                usage=usage,
                source="langgraph",
                metadata={"debug": "shouldnotwin"},
                usage_metadata=UsageRecordMetadata(role="finalizer"),
            )

            call_kwargs = MockRecord.call_args[1]
            assert call_kwargs["request_metadata"]["role"] == "finalizer"
            assert "debug" not in call_kwargs["request_metadata"]

    def test_record_usage_merges_provider_components_with_canonical_metadata(self):
        mock_db = MagicMock()

        with patch("backend.services.usage_tracking.service.LLMUsageRecord") as MockRecord:
            MockRecord.return_value = MagicMock()

            service = UsageTrackingService(mock_db)
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

            service.record_usage(
                task_id=9,
                user_id=1,
                usage=usage,
                source="langgraph",
                usage_metadata=UsageRecordMetadata(role="planner"),
            )

            request_metadata = MockRecord.call_args[1]["request_metadata"]
            assert request_metadata["role"] == "planner"
            assert request_metadata["provider"] == "anthropic"
            assert request_metadata["api_surface"] == "messages"
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

    def test_record_usage_merges_provider_components_with_legacy_metadata(self):
        mock_db = MagicMock()

        with patch("backend.services.usage_tracking.service.LLMUsageRecord") as MockRecord:
            MockRecord.return_value = MagicMock()

            service = UsageTrackingService(mock_db)
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

            service.record_usage(
                task_id=9,
                user_id=1,
                usage=usage,
                source="legacy_caller",
                metadata={"debug": "info"},
            )

            request_metadata = MockRecord.call_args[1]["request_metadata"]
            assert request_metadata["debug"] == "info"
            assert request_metadata["provider"] == "anthropic"
            assert request_metadata["api_surface"] == "messages"
            assert request_metadata["provider_usage_components"] == {
                "provider": "anthropic",
                "api_surface": "messages",
                "components": {"input_tokens": 100, "output_tokens": 50},
            }

    def test_record_usage_handles_db_error(self):
        """Should handle database errors gracefully."""
        mock_db = MagicMock()
        mock_db.commit.side_effect = Exception("DB error")
        mock_db.rollback = MagicMock()
        
        with patch('backend.services.usage_tracking.service.LLMUsageRecord'):
            service = UsageTrackingService(mock_db)
            usage = UsageData(
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
                model="gpt-4o-mini",
            )
            
            result = service.record_usage(
                task_id=123,
                user_id=1,
                usage=usage,
                source="test",
            )
            
            assert result is None
            mock_db.rollback.assert_called_once()


class TestUsageTrackingServiceGetTaskUsage:
    """Tests for UsageTrackingService.get_task_usage()"""
    
    def test_get_task_usage_returns_summary(self):
        """Should return aggregated usage summary."""
        mock_db = MagicMock()
        
        # Mock aggregation query result - use a real tuple instead of MagicMock
        aggregation_result = (
            1000,  # prompt_tokens
            500,   # completion_tokens
            1500,  # total_tokens
            100,   # cached_tokens
            0,     # reasoning_tokens
            5,     # call_count
            datetime(2026, 1, 1),  # first_call
            datetime(2026, 1, 17),  # last_call
        )
        mock_agg_result = MagicMock()
        mock_agg_result.one.return_value = aggregation_result
        
        mock_provider_model_result = MagicMock()
        mock_provider_model_result.all.return_value = [
            ("openai", "gpt-4o-mini", 500, 250, 50, 0),
            ("openai", "gpt-4o", 500, 250, 50, 0),
        ]
        
        # Track call order to return correct mock
        call_count = [0]
        
        def execute_side_effect(query):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_agg_result
            return mock_provider_model_result
        
        mock_db.execute.side_effect = execute_side_effect

        service = UsageTrackingService(mock_db)
        summary = service.get_task_usage(task_id=123)

        assert summary.task_id == 123
        assert summary.total_prompt_tokens == 1000
        assert summary.total_completion_tokens == 500
        assert summary.total_tokens == 1500
        assert summary.call_count == 5
        assert summary.pricing_status == "available"
        assert summary.unpriced_providers == []
        assert summary.unpriced_models == []

    def test_get_task_usage_prices_registered_anthropic_model(self):
        """Aggregate summaries use registered Anthropic pricing."""
        mock_db = MagicMock()
        mock_agg_result = MagicMock()
        mock_agg_result.one.return_value = (
            100,
            50,
            150,
            0,
            0,
            1,
            datetime(2026, 1, 1),
            datetime(2026, 1, 1),
        )
        mock_provider_model_result = MagicMock()
        mock_provider_model_result.all.return_value = [
            ("anthropic", "claude-sonnet-4-6", 100, 50, 0, 0),
        ]

        call_count = [0]

        def execute_side_effect(_query):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_agg_result
            return mock_provider_model_result

        mock_db.execute.side_effect = execute_side_effect

        summary = UsageTrackingService(mock_db).get_task_usage(task_id=123)

        assert summary.pricing_status == "available"
        assert summary.unpriced_providers == []
        assert summary.unpriced_models == []
    
    def test_get_task_usage_handles_error(self):
        """Should return empty summary on error."""
        mock_db = MagicMock()
        mock_db.execute.side_effect = Exception("DB error")
        
        service = UsageTrackingService(mock_db)
        summary = service.get_task_usage(task_id=123)
        
        assert summary.task_id == 123
        assert summary.total_tokens == 0
        assert summary.call_count == 0

    def test_calculate_task_cost_groups_by_provider_and_model(self):
        """Anthropic rows use Anthropic pricing rather than OpenAI defaults."""
        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.all.return_value = [
            ("openai", "gpt-4o-mini", 1_000_000, 0, 0, 0),
            ("anthropic", "claude-sonnet-4-6", 1_000_000, 1_000_000, 0, 0),
        ]
        mock_db.execute.return_value = mock_result

        service = UsageTrackingService(mock_db)

        assert service._calculate_task_cost(task_id=123) == pytest.approx(18.15)


class TestUsageTrackingServiceGetBreakdown:
    """Tests for UsageTrackingService.get_task_usage_breakdown()"""
    
    def test_get_breakdown_returns_records(self):
        """Should return list of usage records."""
        mock_db = MagicMock()
        
        mock_records = [MagicMock(), MagicMock()]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = mock_records
        mock_db.execute.return_value = mock_result
        
        service = UsageTrackingService(mock_db)
        result = service.get_task_usage_breakdown(task_id=123)
        
        assert len(result) == 2
    
    def test_get_breakdown_handles_error(self):
        """Should return empty list on error."""
        mock_db = MagicMock()
        mock_db.execute.side_effect = Exception("DB error")
        
        service = UsageTrackingService(mock_db)
        result = service.get_task_usage_breakdown(task_id=123)
        
        assert result == []


class TestUsageTrackingServiceGetUserUsage:
    """Tests for UsageTrackingService.get_user_usage()"""
    
    def test_get_user_usage_returns_aggregated(self):
        """Should return aggregated user usage."""
        mock_db = MagicMock()
        
        mock_result = MagicMock()
        mock_result.one.return_value = (
            5000,  # prompt_tokens
            2500,  # completion_tokens
            7500,  # total_tokens
            25,    # call_count
        )
        mock_db.execute.return_value = mock_result
        
        service = UsageTrackingService(mock_db)
        result = service.get_user_usage(user_id=1)
        
        assert result["user_id"] == 1
        assert result["total_prompt_tokens"] == 5000
        assert result["total_completion_tokens"] == 2500
        assert result["total_tokens"] == 7500
        assert result["call_count"] == 25
    
    def test_get_user_usage_with_since_filter(self):
        """Should filter by date when since is provided."""
        mock_db = MagicMock()
        
        mock_result = MagicMock()
        mock_result.one.return_value = (1000, 500, 1500, 5)
        mock_db.execute.return_value = mock_result
        
        since = datetime.now() - timedelta(days=7)
        
        service = UsageTrackingService(mock_db)
        result = service.get_user_usage(user_id=1, since=since)
        
        assert result["since"] == since.isoformat()
