"""Tests for LLM provider base types and ABC.

Tests cover:
- ToolCall dataclass immutability
- ToolCallResult structure
- LLMClient ABC cannot be instantiated directly
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List

import pytest

from agent.providers.llm.core.base import LLMClient, LLMResponse, ToolCall, ToolCallResult


class TestToolCall:
    """Tests for ToolCall dataclass."""
    
    def test_create_tool_call(self) -> None:
        """Test creating a ToolCall instance."""
        tc = ToolCall(
            id="call_123",
            name="search_web",
            arguments='{"query": "test"}',
        )
        
        assert tc.id == "call_123"
        assert tc.name == "search_web"
        assert tc.arguments == '{"query": "test"}'
    
    def test_tool_call_is_frozen(self) -> None:
        """Test that ToolCall is immutable (frozen dataclass)."""
        tc = ToolCall(id="call_123", name="test", arguments="{}")
        
        with pytest.raises(AttributeError):
            tc.id = "new_id"  # type: ignore
        
        with pytest.raises(AttributeError):
            tc.name = "new_name"  # type: ignore
    
    def test_tool_call_equality(self) -> None:
        """Test ToolCall equality based on values."""
        tc1 = ToolCall(id="call_123", name="test", arguments="{}")
        tc2 = ToolCall(id="call_123", name="test", arguments="{}")
        tc3 = ToolCall(id="call_456", name="test", arguments="{}")
        
        assert tc1 == tc2
        assert tc1 != tc3
    
    def test_tool_call_hashable(self) -> None:
        """Test that ToolCall is hashable (can be used in sets/dict keys)."""
        tc1 = ToolCall(id="call_123", name="test", arguments="{}")
        tc2 = ToolCall(id="call_123", name="test", arguments="{}")
        
        # Should be usable in a set
        tool_set = {tc1, tc2}
        assert len(tool_set) == 1
        
        # Should be usable as dict key
        tool_dict = {tc1: "value"}
        assert tool_dict[tc2] == "value"


class TestToolCallResult:
    """Tests for ToolCallResult dataclass."""
    
    def test_create_with_content_only(self) -> None:
        """Test creating result with only content."""
        result = ToolCallResult(
            content="Hello, world!",
            tool_calls=None,
            raw={"response": "data"},
        )
        
        assert result.content == "Hello, world!"
        assert result.tool_calls is None
        assert result.raw == {"response": "data"}
    
    def test_create_with_tool_calls_only(self) -> None:
        """Test creating result with only tool calls."""
        tc = ToolCall(id="call_1", name="search", arguments='{"q": "test"}')
        result = ToolCallResult(
            content=None,
            tool_calls=[tc],
            raw={},
        )
        
        assert result.content is None
        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "search"
    
    def test_create_with_both(self) -> None:
        """Test creating result with both content and tool calls."""
        tc = ToolCall(id="call_1", name="search", arguments="{}")
        result = ToolCallResult(
            content="I'll search for that.",
            tool_calls=[tc],
            raw={},
        )
        
        assert result.content is not None
        assert result.tool_calls is not None
    
    def test_tool_call_result_is_mutable(self) -> None:
        """Test that ToolCallResult is mutable (not frozen)."""
        result = ToolCallResult(content="test", tool_calls=None, raw={})
        
        # Should be able to modify
        result.content = "modified"
        assert result.content == "modified"


class TestLLMClientABC:
    """Tests for LLMClient abstract base class."""

    def test_message_and_tool_docs_are_provider_neutral(self) -> None:
        """Base docs should describe the neutral tenant_baseline compatibility contract."""
        chat_doc = LLMClient.chat_messages.__doc__ or ""
        tool_doc = LLMClient.chat_with_tools.__doc__ or ""

        assert "text-first conversation history" in chat_doc
        assert "provider-native" in chat_doc
        assert "input_text" not in chat_doc
        assert "FunctionToolSpec" in tool_doc
        assert "Tool definitions in OpenAI format" not in tool_doc
    
    def test_cannot_instantiate_directly(self) -> None:
        """Test that LLMClient cannot be instantiated directly."""
        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            LLMClient()  # type: ignore
    
    def test_subclass_must_implement_all_methods(self) -> None:
        """Test that subclass must implement all abstract methods."""
        
        # Incomplete implementation
        class IncompleteClient(LLMClient):
            @property
            def model(self) -> str:
                return "test"
            
            async def chat(self, system_prompt: str, user_prompt: str, **kwargs: Any) -> str:
                return "response"
            
            # Missing: chat_messages, stream_chat_messages, chat_with_tools
        
        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            IncompleteClient()  # type: ignore
    
    def test_complete_subclass_can_instantiate(self) -> None:
        """Test that a complete subclass can be instantiated."""
        
        class CompleteClient(LLMClient):
            @property
            def model(self) -> str:
                return "test-model"
            
            async def chat(self, system_prompt: str, user_prompt: str, **kwargs: Any) -> str:
                return "response"
            
            async def chat_messages(self, messages: List[Dict[str, Any]], **kwargs: Any) -> str:
                return "response"
            
            async def stream_chat_messages(
                self, messages: List[Dict[str, Any]], **kwargs: Any
            ) -> AsyncIterator[str]:
                yield "chunk"
            
            async def chat_with_tools(
                self,
                system_prompt: str,
                user_prompt: str,
                tools: List[Dict[str, Any]],
                tool_choice: Any = "auto",
                **kwargs: Any,
            ) -> ToolCallResult:
                return ToolCallResult(content="response", tool_calls=None, raw={})

            async def chat_with_usage(
                self, system_prompt: str, user_prompt: str, **kwargs: Any
            ) -> LLMResponse:
                return LLMResponse(content="response")

            async def chat_messages_with_usage(
                self, messages: List[Dict[str, Any]], **kwargs: Any
            ) -> LLMResponse:
                return LLMResponse(content="response")

            async def chat_with_tools_with_usage(
                self,
                system_prompt: str,
                user_prompt: str,
                tools: List[Dict[str, Any]],
                tool_choice: Any = "auto",
                **kwargs: Any,
            ) -> ToolCallResult:
                return ToolCallResult(content="response", tool_calls=None, raw={})
        
        # Should not raise
        client = CompleteClient()
        assert client.model == "test-model"
