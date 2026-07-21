"""Base types and abstract interface for LLM providers.

This module defines the contract that all LLM providers must implement.
Graph nodes depend only on this interface, never on concrete implementations.
Clients may be used as async context managers for deterministic resource cleanup.

Response Types:
    - LLMResponse: Container for content + usage from non-streaming calls
    - LLMStreamingResponse: Container for streaming iterator + final usage accessor
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import math
from typing import Any, AsyncIterator, Callable, Dict, List, Mapping, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.services.usage_tracking.models import UsageData
    from ..contracts.tool_contracts import FunctionToolSpec, ToolChoice


ChatMessage = Mapping[str, Any]
if TYPE_CHECKING:
    ToolSpecInput = FunctionToolSpec | Dict[str, Any]
    ToolChoiceInput = ToolChoice | Dict[str, Any] | str | None
else:
    ToolSpecInput = Any
    ToolChoiceInput = Any


@dataclass(frozen=True)
class ToolCall:
    """Normalized tool call from LLM response.
    
    This is a provider-agnostic representation of a tool/function call
    requested by the LLM. All providers must normalize their native
    tool call format into this structure.
    
    Attributes:
        id: Unique identifier for this tool call (for correlating results)
        name: Name of the tool/function to call
        arguments: JSON string containing the tool arguments
    """
    id: str
    name: str
    arguments: str  # JSON string


@dataclass
class ToolCallResult:
    """Standardized result from chat_with_tools().
    
    Contains both the text response (if any) and any tool calls requested
    by the model. At least one of content or tool_calls will be present.
    
    Attributes:
        content: Optional text response from the model
        tool_calls: Optional list of tool calls requested by the model
        raw: Original provider response for debugging/logging
        usage: Optional token usage data (only set when using chat_with_tools_with_usage)
    """
    content: Optional[str]
    tool_calls: Optional[List[ToolCall]]
    raw: Any  # Original response for debugging
    usage: Optional["UsageData"] = None


@dataclass(frozen=True, slots=True)
class StructuredOutputSpec:
    """Schema contract for structured JSON output.
    
    Attributes:
        name: Stable schema identifier used in API requests and diagnostics
        schema: JSON Schema object enforced by provider request/parse flow
        strict: Whether provider should enforce strict schema generation
    """
    name: str
    schema: Dict[str, Any]
    strict: bool = True


@dataclass(frozen=True, slots=True)
class LLMCallOptions:
    """Typed non-secret controls for one provider-neutral LLM call.

    Message content, tools, schemas, endpoint data, authentication material,
    headers, and executable behavior are deliberately outside this object.
    """

    temperature: float | None = None
    max_tokens: int | None = None
    tool_choice_mode: str | None = None
    structured_output_strategy: str | None = None
    include_stream_usage: bool = False
    reasoning_effort: str | None = None
    retry_attempts: int | None = None
    parallel_tool_calls: bool | None = None

    def __post_init__(self) -> None:
        if self.temperature is not None:
            if isinstance(self.temperature, bool) or not isinstance(
                self.temperature, (int, float)
            ):
                raise TypeError("temperature must be a finite non-negative number")
            normalized_temperature = float(self.temperature)
            if not math.isfinite(normalized_temperature) or normalized_temperature < 0:
                raise ValueError("temperature must be a finite non-negative number")
            object.__setattr__(self, "temperature", normalized_temperature)

        if self.max_tokens is not None and (
            isinstance(self.max_tokens, bool)
            or not isinstance(self.max_tokens, int)
            or self.max_tokens <= 0
        ):
            raise ValueError("max_tokens must be a positive integer")

        for field_name in ("tool_choice_mode", "structured_output_strategy"):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError(f"{field_name} must be a non-empty string")

        if not isinstance(self.include_stream_usage, bool):
            raise TypeError("include_stream_usage must be a boolean")

        if self.reasoning_effort is not None and (
            not isinstance(self.reasoning_effort, str)
            or not self.reasoning_effort.strip()
            or self.reasoning_effort != self.reasoning_effort.strip()
        ):
            raise ValueError(
                "reasoning_effort must be non-empty without outer whitespace"
            )

        if self.retry_attempts is not None and (
            isinstance(self.retry_attempts, bool)
            or not isinstance(self.retry_attempts, int)
            or self.retry_attempts < 0
        ):
            raise ValueError("retry_attempts must be a non-negative integer")

        if self.parallel_tool_calls is not None and not isinstance(
            self.parallel_tool_calls, bool
        ):
            raise TypeError("parallel_tool_calls must be a boolean or None")


@dataclass(slots=True)
class LLMResponse:
    """Response container with content and usage data.
    
    This is the standard return type for non-streaming LLM calls.
    It combines the response content with token usage information
    captured from the API response.
    
    Attributes:
        content: The text content of the LLM response
        usage: Token usage data (prompt + completion tokens) if available
        raw: Original API response for debugging/logging
        structured_output: Parsed JSON object validated against structured_output schema
        
    Example:
        response = await client.chat_messages_with_usage(messages)
        print(response.content)
        if response.usage:
            print(f"Used {response.usage.total_tokens} tokens")
    """
    content: str
    usage: Optional["UsageData"] = None
    raw: Optional[Any] = None
    structured_output: Optional[Dict[str, Any]] = None


@dataclass(slots=True)
class LLMStreamingResponse:
    """Container for streaming response with final usage accessor.
    
    Streaming responses emit content incrementally, but token usage
    is only available in the final chunk (for Chat Completions API)
    or final event (for Responses API). This container provides
    access to both.
    
    Usage:
        stream_response = await client.stream_chat_messages_with_usage(messages)
        
        # Iterate over content chunks
        async for chunk in stream_response.content_iterator:
            print(chunk, end="")
        
        # Get final usage after iteration completes
        usage = stream_response.get_final_usage()
        if usage:
            print(f"Used {usage.total_tokens} tokens")
    
    Note:
        - get_final_usage() must be called AFTER fully consuming content_iterator
        - Returns None if usage is unavailable or iteration incomplete
    """
    content_iterator: AsyncIterator[str]
    get_final_usage: Callable[[], Optional["UsageData"]]


class LLMClient(ABC):
    """Abstract interface for LLM providers.
    
    All providers must implement these methods with identical semantics.
    Graph nodes depend only on this interface, enabling seamless provider
    switching without code changes.
    
    Implementation Guidelines:
        - All methods must raise explicit exceptions on failure (no silent returns)
        - Retry logic should be implemented in concrete classes
        - Logging should use standard logging module with __name__
        - Provider-specific errors must be wrapped in LLMProviderError subclasses
    
    Example:
        class MyProvider(LLMClient):
            @property
            def model(self) -> str:
                return self._model
            
            async def chat(self, system_prompt: str, user_prompt: str, **kwargs) -> str:
                # Implementation here
                pass
    """

    async def __aenter__(self) -> LLMClient:
        """Return this client for optional async context-managed ownership."""
        return self

    async def __aexit__(
        self,
        exc_type: Any,
        exc_value: Any,
        traceback: Any,
    ) -> None:
        """Release resources when leaving an async ownership scope."""
        await self.aclose()

    async def aclose(self) -> None:
        """Release owned resources, or safely do nothing when none exist."""
        return None
    
    @property
    @abstractmethod
    def model(self) -> str:
        """Return the model identifier.
        
        Returns:
            The model name/identifier used by this client (e.g., "gpt-4o-mini")
        """
        raise NotImplementedError
    
    @abstractmethod
    async def chat(
        self, 
        system_prompt: str, 
        user_prompt: str, 
        **kwargs: Any,
    ) -> str:
        """Single-turn chat completion.
        
        Sends a simple system + user message pair and returns the assistant response.
        
        Args:
            system_prompt: System instructions for the model
            user_prompt: User message/query
            **kwargs: Provider-specific options (temperature, max_tokens, etc.)
            
        Returns:
            Assistant response text
            
        Raises:
            LLMAPIError: If the API call fails
            LLMResponseError: If response parsing fails or response is empty
        """
        raise NotImplementedError
    
    @abstractmethod
    async def chat_messages(
        self, 
        messages: List[ChatMessage],
        **kwargs: Any,
    ) -> str:
        """Multi-turn chat completion with explicit history.
        
        Sends a text-first conversation history and returns the assistant
        response. In the tenant_baseline contract, each message is a provider-neutral mapping with a
        normalized ``role`` and ``content``. Text content may be passed as a
        string. Existing OpenAI-compatible content lists remain accepted only as
        compatibility input for current OpenAI history paths; provider-native
        content part names are not the generic contract.
        
        Args:
            messages: List of role/content message mappings.
            **kwargs: Provider-specific options (temperature, max_tokens, etc.)
            
        Returns:
            Assistant response text
            
        Raises:
            LLMAPIError: If the API call fails
            LLMResponseError: If response parsing fails or response is empty
        """
        raise NotImplementedError
    
    @abstractmethod
    async def stream_chat_messages(
        self, 
        messages: List[ChatMessage],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Stream multi-turn chat completion.
        
        Yields text chunks as they arrive from the provider. This is the
        preferred method for user-facing responses as it enables real-time
        streaming to the UI.
        
        Args:
            messages: Conversation history using the text-first neutral
                message envelope described by chat_messages().
            **kwargs: Provider-specific options (temperature, max_tokens, etc.)
            
        Yields:
            Text chunks (strings) as they arrive
            
        Raises:
            LLMAPIError: If the API call fails
            LLMResponseError: If response parsing fails
            
        Note:
            Implementations must be proper async generators. The full response
            can be reconstructed by joining all yielded chunks.
        """
        raise NotImplementedError
        # Type hint requires yield to make this a generator
        yield ""  # pragma: no cover - abstract method
    
    @abstractmethod
    async def chat_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        **kwargs: Any,
    ) -> LLMResponse:
        """Single-turn chat completion with usage tracking.
        
        This is the usage-tracking equivalent of chat(). Use this method
        when you need token usage data from a single-turn LLM call.
        
        Args:
            system_prompt: System instructions for the model
            user_prompt: User message/query
            **kwargs: Provider-specific options (temperature, max_tokens, etc.)
            
        Returns:
            LLMResponse with content and usage data
            
        Raises:
            LLMAPIError: If the API call fails
            LLMResponseError: If response parsing fails or response is empty
        """
        raise NotImplementedError

    @abstractmethod
    async def chat_messages_with_usage(
        self,
        messages: List[ChatMessage],
        **kwargs: Any,
    ) -> LLMResponse:
        """Multi-turn chat completion with explicit history and usage tracking.

        This is the usage-tracking equivalent of chat_messages(). Runtime
        wrappers and graph nodes may depend on this method for non-streaming
        usage capture across providers.

        Args:
            messages: Conversation history using the text-first neutral
                message envelope described by chat_messages().
            **kwargs: Provider-specific options (temperature, max_tokens, etc.)

        Returns:
            LLMResponse with content and usage data.

        Raises:
            LLMAPIError: If the API call fails
            LLMResponseError: If response parsing fails or response is empty
        """
        raise NotImplementedError
    
    @abstractmethod
    async def chat_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: List[ToolSpecInput],
        tool_choice: ToolChoiceInput = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        """Chat completion with tool/function calling.
        
        Enables the model to request tool executions. The model may return
        text, tool calls, or both.
        
        Args:
            system_prompt: System instructions for the model
            user_prompt: User message/query
            tools: Provider-neutral FunctionToolSpec values. Legacy dict inputs
                are accepted only for OpenAI compatibility and must be
                normalized by adapters before request construction.
            tool_choice: Provider-neutral ToolChoice or legacy strategy value.
            **kwargs: Provider-specific options (temperature, max_tokens, etc.)
            
        Returns:
            ToolCallResult containing response content and/or tool calls
            
        Raises:
            LLMAPIError: If the API call fails
            LLMResponseError: If response parsing fails
        """
        raise NotImplementedError
    
    @abstractmethod
    async def chat_with_tools_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: List[ToolSpecInput],
        tool_choice: ToolChoiceInput = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        """Chat completion with tool/function calling and usage tracking.
        
        Same as chat_with_tools() but captures token usage data.
        Use this when you need accurate token accounting.
        
        Args:
            system_prompt: System instructions for the model
            user_prompt: User message/query
            tools: Provider-neutral FunctionToolSpec values. Legacy dict inputs
                are accepted only for OpenAI compatibility.
            tool_choice: Provider-neutral ToolChoice or legacy strategy value.
            **kwargs: Provider-specific options (temperature, max_tokens, etc.)
            
        Returns:
            ToolCallResult with usage field populated
            
        Raises:
            LLMAPIError: If the API call fails
            LLMResponseError: If response parsing fails
        """
        raise NotImplementedError


__all__ = [
    "ChatMessage",
    "LLMCallOptions",
    "LLMClient",
    "LLMResponse",
    "LLMStreamingResponse",
    "StructuredOutputSpec",
    "ToolChoiceInput",
    "ToolCall",
    "ToolCallResult",
    "ToolSpecInput",
]
