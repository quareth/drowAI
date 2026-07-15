"""OpenAI Chat Completions API provider.

This module implements the LLMClient interface for OpenAI models using
the Chat Completions API (GPT-4, GPT-3.5, etc.).

Note: This provider does NOT handle GPT-5/Responses API. A separate
provider will be created for that in a future iteration.

Token Usage Tracking:
    - chat_messages_with_usage() returns LLMResponse with usage data
    - stream_chat_messages_with_usage() returns LLMStreamingResponse with final usage
    - Original methods (chat_messages, stream_chat_messages) preserved for backward compatibility
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any, AsyncIterator, Dict, List, Optional

from ...core.base import (
    LLMClient,
    LLMResponse,
    LLMStreamingResponse,
    StructuredOutputSpec,
    ToolChoiceInput,
    ToolCall,
    ToolCallResult,
    ToolSpecInput,
)
from ...core.exceptions import (
    LLMAPIError,
    LLMConfigurationError,
    LLMRefusalError,
    LLMResponseError,
    LLMStructuredOutputParseError,
)
from ...contracts.structured_output import (
    StructuredOutputParseError,
    parse_structured_content,
)
from ...contracts.recovery import (
    ResponseParseRetryState,
)
from .structured_output import (
    StructuredOutputSchemaError,
    build_chat_response_format,
    require_openai_native_structured_output_strategy,
    validate_openai_strict_schema,
)
from .tool_contracts import (
    normalize_openai_chat_tool_choice,
    normalize_openai_chat_tool_spec,
)
from .refusal import (
    inspect_openai_chat_stream_chunk,
    raise_for_openai_chat_refusal,
    raise_for_openai_chat_stream_refusal,
)

# Import UsageData for token tracking
try:
    from backend.services.usage_tracking.models import UsageData
    USAGE_TRACKING_AVAILABLE = True
except ImportError:
    USAGE_TRACKING_AVAILABLE = False
    UsageData = None  # type: ignore

logger = logging.getLogger(__name__)


def _safe_inc(metric_name: str) -> None:
    """Increment metrics when backend metrics utilities are available."""
    try:
        from backend.services.metrics.utils import safe_inc

        safe_inc(metric_name)
    except Exception:
        return

# ---------------------------------------------------------------------------
# OpenAI SDK Import with Graceful Fallback
# ---------------------------------------------------------------------------

try:
    import openai
    from openai import APIError, APIConnectionError, RateLimitError, APIStatusError
    
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    openai = None  # type: ignore
    APIError = Exception  # type: ignore
    APIConnectionError = Exception  # type: ignore
    RateLimitError = Exception  # type: ignore
    APIStatusError = Exception  # type: ignore


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TEMPERATURE = 0.1
# High token limit for testing - avoid interruption by token limits
# This is used across all GPT-4 LLM calls unless explicitly overridden
DEFAULT_MAX_TOKENS = 10000
DEFAULT_RETRY_COUNT = 2
INITIAL_RETRY_DELAY = 0.5


# ---------------------------------------------------------------------------
# OpenAI Chat Completions Provider
# ---------------------------------------------------------------------------

class OpenAIChatClient(LLMClient):
    """OpenAI Chat Completions API client.
    
    Implements the LLMClient interface for GPT-4, GPT-3.5, and other models
    that use the Chat Completions API.
    
    Features:
        - Automatic retry with exponential backoff
        - Proper exception wrapping
        - Streaming support
        - Tool/function calling
    
    Example:
        client = OpenAIChatClient(api_key="sk-...", model="gpt-4o-mini")
        response = await client.chat("You are helpful.", "Hello!")
        
        # Streaming
        async for chunk in client.stream_chat_messages(messages):
            print(chunk, end="")
        
        # Tool calling
        result = await client.chat_with_tools(system, user, tools)
        if result.tool_calls:
            for tc in result.tool_calls:
                print(f"Call {tc.name} with {tc.arguments}")
    """
    
    def __init__(
        self, 
        api_key: str, 
        model: str = "gpt-4",
        **kwargs: Any,
    ) -> None:
        """Initialize OpenAI Chat Completions client.
        
        Args:
            api_key: OpenAI API key
            model: Model identifier (e.g., "gpt-4", "gpt-4o-mini", "gpt-3.5-turbo")
            **kwargs: Additional configuration (currently unused, reserved for future)
            
        Raises:
            LLMConfigurationError: If openai library is not installed
        """
        if not OPENAI_AVAILABLE:
            raise LLMConfigurationError(
                "OpenAI library is not installed. Install with: pip install openai",
                provider="OpenAI",
            )
        
        self._api_key = api_key
        self._model = model
        self._client = openai.AsyncOpenAI(api_key=api_key)
        
        logger.debug(f"Initialized OpenAIChatClient with model={model}")
    
    @property
    def model(self) -> str:
        """Return the model identifier."""
        return self._model
    
    # -------------------------------------------------------------------------
    # Core API Methods
    # -------------------------------------------------------------------------
    
    async def chat(
        self, 
        system_prompt: str, 
        user_prompt: str, 
        **kwargs: Any,
    ) -> str:
        """Single-turn chat completion.
        
        Args:
            system_prompt: System instructions for the model
            user_prompt: User message/query
            **kwargs: Additional options:
                - temperature: Sampling temperature (default: 0.1)
                - max_tokens: Maximum response tokens (default: 3000)
                - _retries: Number of retry attempts (default: 2)
                
        Returns:
            Assistant response text
            
        Raises:
            LLMAPIError: If the API call fails after retries
            LLMResponseError: If response is empty or cannot be parsed
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return await self.chat_messages(messages, **kwargs)
    
    async def chat_messages(
        self, 
        messages: List[Dict[str, Any]], 
        **kwargs: Any,
    ) -> str:
        """Multi-turn chat completion with explicit history.
        
        Args:
            messages: Conversation history as list of message dicts
            **kwargs: Additional options (temperature, max_tokens, _retries)
            
        Returns:
            Assistant response text
            
        Raises:
            LLMAPIError: If the API call fails after retries
            LLMResponseError: If response is empty or cannot be parsed
        """
        max_attempts = int(kwargs.get("_retries", DEFAULT_RETRY_COUNT)) + 1
        last_error: Optional[Exception] = None
        parse_retry = ResponseParseRetryState()
        
        for attempt in range(1, max_attempts + 1):
            try:
                structured_spec = self._get_structured_output_spec(kwargs)
                request_kwargs = {
                    "model": self._model,
                    "messages": messages,
                    "temperature": kwargs.get("temperature", DEFAULT_TEMPERATURE),
                    "max_tokens": kwargs.get("max_tokens", DEFAULT_MAX_TOKENS),
                }
                tools = kwargs.get("tools") or []
                if tools:
                    request_kwargs["tools"] = [
                        normalize_openai_chat_tool_spec(tool)
                        for tool in tools
                    ]
                if tools and kwargs.get("tool_choice") is not None:
                    request_kwargs["tool_choice"] = normalize_openai_chat_tool_choice(
                        kwargs.get("tool_choice")
                    )
                self._attach_structured_response_format(request_kwargs, structured_spec)
                response = await self._client.chat.completions.create(**request_kwargs)
                content = response.choices[0].message.content
                raise_for_openai_chat_refusal(
                    response,
                    model=self._model,
                    usage=self._extract_usage_from_response(response),
                    partial_content=content if isinstance(content, str) else None,
                )
                
                if content is None or not str(content).strip():
                    raise LLMResponseError(
                        "OpenAI returned empty response content",
                        provider="OpenAI",
                    )
                self._parse_structured_output(
                    str(content),
                    structured_spec,
                    raw_response=response,
                )
                
                logger.debug(
                    f"OpenAI chat_messages completed: model={self._model}, "
                    f"response_length={len(content)}"
                )
                return content
                
            except LLMRefusalError:
                raise
            except LLMResponseError as exc:
                if parse_retry.should_retry(
                    exc,
                    attempt=attempt,
                    max_attempts=max_attempts,
                ):
                    self._log_retry(attempt, exc, max_attempts)
                    await self._backoff_sleep(attempt)
                    continue
                raise
            except LLMConfigurationError:
                # Fail fast on invalid structured schema contracts
                raise
            except (APIError, APIConnectionError, RateLimitError, APIStatusError) as e:
                last_error = self._wrap_api_error(e)
                if attempt >= max_attempts:
                    logger.warning(
                        f"OpenAI chat_messages failed after {attempt} attempts: {e}"
                    )
                    raise last_error from e
                self._log_retry(attempt, e, max_attempts)
                await self._backoff_sleep(attempt)
            except Exception as e:
                last_error = LLMAPIError(
                    f"Unexpected error during OpenAI chat: {e}",
                    provider="OpenAI",
                )
                if attempt >= max_attempts:
                    logger.warning(
                        f"OpenAI chat_messages failed after {attempt} attempts: {e}"
                    )
                    raise last_error from e
                self._log_retry(attempt, e, max_attempts)
                await self._backoff_sleep(attempt)
        
        # Should not reach here, but satisfy type checker
        raise last_error if last_error else LLMAPIError(
            "OpenAI chat failed", provider="OpenAI"
        )
    
    async def stream_chat_messages(
        self, 
        messages: List[Dict[str, Any]], 
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Stream multi-turn chat completion.
        
        Yields text chunks as they arrive from OpenAI.
        
        Args:
            messages: Conversation history
            **kwargs: Additional options (temperature, max_tokens)
            
        Yields:
            Text chunks (strings)
            
        Raises:
            LLMAPIError: If the API call fails
        """
        try:
            stream = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=kwargs.get("temperature", DEFAULT_TEMPERATURE),
                max_tokens=kwargs.get("max_tokens", DEFAULT_MAX_TOKENS),
                stream=True,
            )
            
            partial_chunks: List[str] = []
            refusal_parts: List[str] = []
            refusal_chunk: Any = None
            response_chunk: Any = None
            captured_usage: Optional["UsageData"] = None
            async for event in stream:
                event_id = getattr(event, "id", None)
                if isinstance(event_id, str) and event_id.strip():
                    response_chunk = event
                event_usage = self._extract_usage_from_response(event)
                if event_usage is not None:
                    captured_usage = event_usage
                try:
                    choice = event.choices[0]
                    delta = getattr(choice, "delta", None)
                    
                    if delta is not None:
                        content = getattr(delta, "content", None)
                        if content:
                            partial_chunks.append(str(content))
                            yield content
                except (IndexError, AttributeError):
                    # Skip malformed events
                    pass
                refusal_count = len(refusal_parts)
                refusal_detected = inspect_openai_chat_stream_chunk(
                    event,
                    refusal_parts=refusal_parts,
                )
                if len(refusal_parts) > refusal_count:
                    refusal_chunk = event
                if refusal_detected:
                    raise_for_openai_chat_stream_refusal(
                        response_chunk or refusal_chunk or event,
                        model=self._model,
                        refusal_parts=refusal_parts,
                        usage=captured_usage,
                        partial_content="".join(partial_chunks),
                        refusal_detected=True,
                    )

            if refusal_parts:
                raise_for_openai_chat_stream_refusal(
                    response_chunk or refusal_chunk,
                    model=self._model,
                    refusal_parts=refusal_parts,
                    usage=captured_usage,
                    partial_content="".join(partial_chunks),
                    refusal_detected=True,
                )
                    
        except (APIError, APIConnectionError, RateLimitError, APIStatusError) as e:
            raise self._wrap_api_error(e) from e
        except LLMRefusalError:
            raise
        except Exception as e:
            raise LLMAPIError(
                f"Streaming failed: {e}",
                provider="OpenAI",
            ) from e
    
    # -------------------------------------------------------------------------
    # Usage-Tracking Methods (Phase 2 + Phase 7)
    # -------------------------------------------------------------------------
    
    async def chat_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        **kwargs: Any,
    ) -> LLMResponse:
        """Single-turn chat completion with usage capture.
        
        This is the usage-tracking equivalent of chat(). Use this method
        when you need token usage data from a single-turn LLM call.
        
        Args:
            system_prompt: System instructions for the model
            user_prompt: User message/query
            **kwargs: Additional options (temperature, max_tokens, etc.)
            
        Returns:
            LLMResponse with content and usage data
            
        Example:
            response = await client.chat_with_usage("You are helpful.", "Hello!")
            print(response.content)
            if response.usage:
                print(f"Used {response.usage.total_tokens} tokens")
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return await self.chat_messages_with_usage(messages, **kwargs)
    
    async def chat_messages_with_usage(
        self, 
        messages: List[Dict[str, Any]], 
        **kwargs: Any,
    ) -> LLMResponse:
        """Multi-turn chat completion with usage tracking.
        
        Same as chat_messages() but returns LLMResponse containing
        both content and token usage data.
        
        Args:
            messages: Conversation history as list of message dicts
            **kwargs: Additional options (temperature, max_tokens, _retries)
            
        Returns:
            LLMResponse with content and usage data
            
        Raises:
            LLMAPIError: If the API call fails after retries
            LLMResponseError: If response is empty or cannot be parsed
        """
        max_attempts = int(kwargs.get("_retries", DEFAULT_RETRY_COUNT)) + 1
        last_error: Optional[Exception] = None
        parse_retry = ResponseParseRetryState()
        
        for attempt in range(1, max_attempts + 1):
            try:
                structured_spec = self._get_structured_output_spec(kwargs)
                request_kwargs = {
                    "model": self._model,
                    "messages": messages,
                    "temperature": kwargs.get("temperature", DEFAULT_TEMPERATURE),
                    "max_tokens": kwargs.get("max_tokens", DEFAULT_MAX_TOKENS),
                }
                tools = kwargs.get("tools") or []
                if tools:
                    request_kwargs["tools"] = [
                        normalize_openai_chat_tool_spec(tool)
                        for tool in tools
                    ]
                if tools and kwargs.get("tool_choice") is not None:
                    request_kwargs["tool_choice"] = normalize_openai_chat_tool_choice(
                        kwargs.get("tool_choice")
                    )
                self._attach_structured_response_format(request_kwargs, structured_spec)
                response = await self._client.chat.completions.create(**request_kwargs)
                usage = self._extract_usage_from_response(response)
                content = response.choices[0].message.content
                raise_for_openai_chat_refusal(
                    response,
                    model=self._model,
                    usage=usage,
                    partial_content=content if isinstance(content, str) else None,
                )
                
                if content is None or not str(content).strip():
                    raise LLMResponseError(
                        "OpenAI returned empty response content",
                        provider="OpenAI",
                    )
                
                # Extract usage data from response
                structured_output = self._parse_structured_output(
                    str(content),
                    structured_spec,
                    raw_response=response,
                )
                
                logger.debug(
                    f"OpenAI chat_messages_with_usage completed: model={self._model}, "
                    f"response_length={len(content)}, "
                    f"tokens={usage.total_tokens if usage else 'N/A'}"
                )
                
                return LLMResponse(
                    content=content,
                    usage=usage,
                    raw=response,
                    structured_output=structured_output,
                )
                
            except LLMRefusalError:
                raise
            except LLMResponseError as exc:
                if parse_retry.should_retry(
                    exc,
                    attempt=attempt,
                    max_attempts=max_attempts,
                ):
                    self._log_retry(attempt, exc, max_attempts)
                    await self._backoff_sleep(attempt)
                    continue
                raise
            except LLMConfigurationError:
                raise
            except (APIError, APIConnectionError, RateLimitError, APIStatusError) as e:
                last_error = self._wrap_api_error(e)
                if attempt >= max_attempts:
                    logger.warning(
                        f"OpenAI chat_messages_with_usage failed after {attempt} attempts: {e}"
                    )
                    raise last_error from e
                self._log_retry(attempt, e, max_attempts)
                await self._backoff_sleep(attempt)
            except Exception as e:
                last_error = LLMAPIError(
                    f"Unexpected error during OpenAI chat: {e}",
                    provider="OpenAI",
                )
                if attempt >= max_attempts:
                    logger.warning(
                        f"OpenAI chat_messages_with_usage failed after {attempt} attempts: {e}"
                    )
                    raise last_error from e
                self._log_retry(attempt, e, max_attempts)
                await self._backoff_sleep(attempt)
        
        raise last_error if last_error else LLMAPIError(
            "OpenAI chat failed", provider="OpenAI"
        )
    
    async def stream_chat_messages_with_usage(
        self, 
        messages: List[Dict[str, Any]], 
        **kwargs: Any,
    ) -> LLMStreamingResponse:
        """Stream multi-turn chat completion with usage tracking.
        
        Returns an LLMStreamingResponse that provides both:
        - An async iterator for content chunks
        - A function to get final usage (call after consuming all chunks)
        
        IMPORTANT: Uses stream_options={"include_usage": True} to get
        usage data in the final streaming chunk.
        
        Args:
            messages: Conversation history
            **kwargs: Additional options (temperature, max_tokens)
            
        Returns:
            LLMStreamingResponse with content iterator and usage accessor
            
        Raises:
            LLMAPIError: If the API call fails
            
        Example:
            response = await client.stream_chat_messages_with_usage(messages)
            async for chunk in response.content_iterator:
                print(chunk, end="")
            usage = response.get_final_usage()
        """
        # Capture usage in closure - will be set from final chunk
        final_usage: List[Optional["UsageData"]] = [None]
        
        async def content_generator() -> AsyncIterator[str]:
            """Generate content chunks and capture final usage."""
            partial_chunks: List[str] = []
            refusal_parts: List[str] = []
            refusal_chunk: Any = None
            response_chunk: Any = None
            try:
                # CRITICAL: include_usage=True enables usage in final chunk
                stream = await self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    temperature=kwargs.get("temperature", DEFAULT_TEMPERATURE),
                    max_tokens=kwargs.get("max_tokens", DEFAULT_MAX_TOKENS),
                    stream=True,
                    stream_options={"include_usage": True},
                )
                
                async for chunk in stream:
                    chunk_id = getattr(chunk, "id", None)
                    if isinstance(chunk_id, str) and chunk_id.strip():
                        response_chunk = chunk
                    # Check for usage in final chunk (has usage but no content)
                    if hasattr(chunk, 'usage') and chunk.usage:
                        final_usage[0] = self._extract_usage_from_response(chunk)
                    
                    # Yield content deltas
                    try:
                        choice = chunk.choices[0] if chunk.choices else None
                        if choice:
                            delta = getattr(choice, "delta", None)
                            if delta is not None:
                                content = getattr(delta, "content", None)
                                if content:
                                    partial_chunks.append(str(content))
                                    yield content
                    except (IndexError, AttributeError):
                        pass
                    refusal_count = len(refusal_parts)
                    refusal_detected = inspect_openai_chat_stream_chunk(
                        chunk,
                        refusal_parts=refusal_parts,
                    )
                    if len(refusal_parts) > refusal_count or refusal_detected:
                        refusal_chunk = chunk

                if refusal_chunk is not None:
                    raise_for_openai_chat_stream_refusal(
                        response_chunk or refusal_chunk,
                        model=self._model,
                        refusal_parts=refusal_parts,
                        usage=final_usage[0],
                        partial_content="".join(partial_chunks),
                        refusal_detected=True,
                    )
                        
            except (APIError, APIConnectionError, RateLimitError, APIStatusError) as e:
                raise self._wrap_api_error(e) from e
            except LLMRefusalError:
                raise
            except Exception as e:
                raise LLMAPIError(
                    f"Streaming failed: {e}",
                    provider="OpenAI",
                ) from e
        
        def get_usage() -> Optional["UsageData"]:
            """Get final usage after streaming completes."""
            return final_usage[0]
        
        return LLMStreamingResponse(
            content_iterator=content_generator(),
            get_final_usage=get_usage,
        )
    
    async def chat_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: List[ToolSpecInput],
        tool_choice: ToolChoiceInput = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        """Chat completion with tool/function calling.
        
        Args:
            system_prompt: System instructions
            user_prompt: User message
            tools: Neutral FunctionToolSpec values or legacy OpenAI dicts.
            tool_choice: Neutral ToolChoice or legacy OpenAI-compatible choice.
            **kwargs: Additional options (temperature, max_tokens, _retries,
                parallel_tool_calls)
            
        Returns:
            ToolCallResult with response content and/or tool calls
            
        Raises:
            LLMAPIError: If the API call fails after retries
        """
        max_attempts = int(kwargs.get("_retries", DEFAULT_RETRY_COUNT)) + 1
        last_error: Optional[Exception] = None
        
        for attempt in range(1, max_attempts + 1):
            try:
                request_kwargs: Dict[str, Any] = {
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": kwargs.get("temperature", DEFAULT_TEMPERATURE),
                    "max_tokens": kwargs.get("max_tokens", DEFAULT_MAX_TOKENS),
                }
                
                if tools:
                    request_kwargs["tools"] = [
                        normalize_openai_chat_tool_spec(tool)
                        for tool in tools
                    ]
                if tool_choice is not None:
                    request_kwargs["tool_choice"] = normalize_openai_chat_tool_choice(tool_choice)
                if kwargs.get("parallel_tool_calls") is not None:
                    request_kwargs["parallel_tool_calls"] = kwargs["parallel_tool_calls"]
                
                response = await self._client.chat.completions.create(**request_kwargs)
                choice = response.choices[0]
                content = choice.message.content
                raise_for_openai_chat_refusal(
                    response,
                    model=self._model,
                    usage=self._extract_usage_from_response(response),
                    partial_content=content if isinstance(content, str) else None,
                )
                
                # Extract tool calls
                tool_calls: Optional[List[ToolCall]] = None
                if hasattr(choice.message, "tool_calls") and choice.message.tool_calls:
                    tool_calls = [
                        ToolCall(
                            id=tc.id,
                            name=tc.function.name,
                            arguments=tc.function.arguments,
                        )
                        for tc in choice.message.tool_calls
                    ]
                
                logger.debug(
                    f"OpenAI chat_with_tools completed: model={self._model}, "
                    f"has_content={content is not None}, "
                    f"tool_calls={len(tool_calls) if tool_calls else 0}"
                )
                
                return ToolCallResult(
                    content=content,
                    tool_calls=tool_calls,
                    raw=response,
                )
                
            except (APIError, APIConnectionError, RateLimitError, APIStatusError) as e:
                last_error = self._wrap_api_error(e)
                if attempt >= max_attempts:
                    logger.warning(
                        f"OpenAI chat_with_tools failed after {attempt} attempts: {e}"
                    )
                    raise last_error from e
                self._log_retry(attempt, e, max_attempts)
                await self._backoff_sleep(attempt)
            except LLMRefusalError:
                raise
            except Exception as e:
                last_error = LLMAPIError(
                    f"Unexpected error during tool call: {e}",
                    provider="OpenAI",
                )
                if attempt >= max_attempts:
                    logger.warning(
                        f"OpenAI chat_with_tools failed after {attempt} attempts: {e}"
                    )
                    raise last_error from e
                self._log_retry(attempt, e, max_attempts)
                await self._backoff_sleep(attempt)
        
        raise last_error if last_error else LLMAPIError(
            "OpenAI tool call failed", provider="OpenAI"
        )
    
    async def chat_with_tools_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: List[ToolSpecInput],
        tool_choice: ToolChoiceInput = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        """Chat completion with tool/function calling and usage tracking.
        
        Same as chat_with_tools() but captures token usage from the API response.
        Use this when you need accurate token accounting for cost tracking.
        
        Args:
            system_prompt: System instructions
            user_prompt: User message
            tools: Neutral FunctionToolSpec values or legacy OpenAI dicts.
            tool_choice: Neutral ToolChoice or legacy OpenAI-compatible choice.
            **kwargs: Additional options (temperature, max_tokens, _retries,
                parallel_tool_calls)
            
        Returns:
            ToolCallResult with usage field populated
            
        Raises:
            LLMAPIError: If the API call fails after retries
        """
        max_attempts = int(kwargs.get("_retries", DEFAULT_RETRY_COUNT)) + 1
        last_error: Optional[Exception] = None
        
        for attempt in range(1, max_attempts + 1):
            try:
                request_kwargs: Dict[str, Any] = {
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": kwargs.get("temperature", DEFAULT_TEMPERATURE),
                    "max_tokens": kwargs.get("max_tokens", DEFAULT_MAX_TOKENS),
                }
                
                if tools:
                    request_kwargs["tools"] = [
                        normalize_openai_chat_tool_spec(tool)
                        for tool in tools
                    ]
                if tool_choice is not None:
                    request_kwargs["tool_choice"] = normalize_openai_chat_tool_choice(tool_choice)
                if kwargs.get("parallel_tool_calls") is not None:
                    request_kwargs["parallel_tool_calls"] = kwargs["parallel_tool_calls"]
                
                response = await self._client.chat.completions.create(**request_kwargs)
                usage = self._extract_usage_from_response(response)
                choice = response.choices[0]
                content = choice.message.content
                raise_for_openai_chat_refusal(
                    response,
                    model=self._model,
                    usage=usage,
                    partial_content=content if isinstance(content, str) else None,
                )
                
                # Extract tool calls
                tool_calls: Optional[List[ToolCall]] = None
                if hasattr(choice.message, "tool_calls") and choice.message.tool_calls:
                    tool_calls = [
                        ToolCall(
                            id=tc.id,
                            name=tc.function.name,
                            arguments=tc.function.arguments,
                        )
                        for tc in choice.message.tool_calls
                    ]
                
                # Extract usage data
                logger.debug(
                    f"OpenAI chat_with_tools_with_usage completed: model={self._model}, "
                    f"has_content={content is not None}, "
                    f"tool_calls={len(tool_calls) if tool_calls else 0}, "
                    f"tokens={usage.total_tokens if usage else 'N/A'}"
                )
                
                return ToolCallResult(
                    content=content,
                    tool_calls=tool_calls,
                    raw=response,
                    usage=usage,
                )
                
            except (APIError, APIConnectionError, RateLimitError, APIStatusError) as e:
                last_error = self._wrap_api_error(e)
                if attempt >= max_attempts:
                    logger.warning(
                        f"OpenAI chat_with_tools_with_usage failed after {attempt} attempts: {e}"
                    )
                    raise last_error from e
                self._log_retry(attempt, e, max_attempts)
                await self._backoff_sleep(attempt)
            except LLMRefusalError:
                raise
            except Exception as e:
                last_error = LLMAPIError(
                    f"Unexpected error during tool call: {e}",
                    provider="OpenAI",
                )
                if attempt >= max_attempts:
                    logger.warning(
                        f"OpenAI chat_with_tools_with_usage failed after {attempt} attempts: {e}"
                    )
                    raise last_error from e
                self._log_retry(attempt, e, max_attempts)
                await self._backoff_sleep(attempt)
        
        raise last_error if last_error else LLMAPIError(
            "OpenAI tool call failed", provider="OpenAI"
        )
    
    # -------------------------------------------------------------------------
    # Helper Methods
    # -------------------------------------------------------------------------

    def _structured_metric_suffix(self, schema_name: str) -> str:
        """Return metric-safe schema suffix."""
        normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", str(schema_name).strip()).strip("_")
        return normalized or "unknown"

    def _get_structured_output_spec(self, kwargs: Dict[str, Any]) -> Optional[StructuredOutputSpec]:
        """Extract and type-check structured output spec from call kwargs."""
        spec = kwargs.get("structured_output")
        if spec is None:
            return None
        if not isinstance(spec, StructuredOutputSpec):
            raise LLMConfigurationError(
                "structured_output must be StructuredOutputSpec",
                provider="OpenAI",
            )
        return spec

    def _attach_structured_response_format(
        self,
        request_kwargs: Dict[str, Any],
        structured_spec: Optional[StructuredOutputSpec],
    ) -> None:
        """Attach Chat Completions json_schema response format when requested."""
        if structured_spec is None:
            return
        require_openai_native_structured_output_strategy(
            structured_spec,
            model=self._model,
        )
        try:
            validate_openai_strict_schema(structured_spec)
        except StructuredOutputSchemaError as exc:
            raise LLMConfigurationError(str(exc), provider="OpenAI") from exc
        request_kwargs["response_format"] = build_chat_response_format(structured_spec)
        suffix = self._structured_metric_suffix(structured_spec.name)
        _safe_inc(f"llm_structured_request_openai_chat_{suffix}")

    def _parse_structured_output(
        self,
        content: str,
        structured_spec: Optional[StructuredOutputSpec],
        *,
        raw_response: Any | None = None,
    ) -> Optional[Dict[str, Any]]:
        """Parse structured content when schema contract is provided."""
        if structured_spec is None:
            return None
        try:
            return parse_structured_content(content, structured_spec)
        except StructuredOutputParseError as exc:
            suffix = self._structured_metric_suffix(structured_spec.name)
            _safe_inc(f"llm_structured_parse_failure_openai_chat_{suffix}")
            diagnostics = self._build_structured_output_diagnostics(raw_response)
            logger.warning(
                "Structured output parse failed (provider=openai_chat schema=%s reason=%s response_id=%s finish_reason=%s)",
                structured_spec.name,
                exc.reason,
                diagnostics.get("response_id"),
                diagnostics.get("finish_reason"),
            )
            raise LLMStructuredOutputParseError(
                str(exc),
                provider="OpenAI",
                schema_name=structured_spec.name,
                parse_reason=exc.reason,
                raw_content=content,
                diagnostics=diagnostics,
            ) from exc

    def _build_structured_output_diagnostics(self, response: Any | None) -> Dict[str, object]:
        """Extract best-effort diagnostics for structured parse failures."""
        if response is None:
            return {}

        diagnostics: Dict[str, object] = {}
        response_id = getattr(response, "id", None)
        if isinstance(response_id, str) and response_id.strip():
            diagnostics["response_id"] = response_id.strip()

        choices = getattr(response, "choices", None)
        if choices:
            try:
                finish_reason = getattr(choices[0], "finish_reason", None)
            except Exception:
                finish_reason = None
            if isinstance(finish_reason, str) and finish_reason.strip():
                diagnostics["finish_reason"] = finish_reason.strip()

        return diagnostics
    
    def _extract_usage_from_response(self, response: Any) -> Optional["UsageData"]:
        """Extract UsageData from Chat Completions API response.
        
        Handles both full responses and streaming final chunks.
        
        Args:
            response: OpenAI API response object
            
        Returns:
            UsageData if available, None otherwise
        """
        if not USAGE_TRACKING_AVAILABLE or UsageData is None:
            return None
        
        return UsageData.from_openai_chat_response(response, self._model)
    
    def _wrap_api_error(self, error: Exception) -> LLMAPIError:
        """Wrap OpenAI SDK exceptions into LLMAPIError."""
        status_code = None
        if hasattr(error, "status_code"):
            status_code = error.status_code
        
        return LLMAPIError(
            f"OpenAI API error: {error}",
            provider="OpenAI",
            status_code=status_code,
        )
    
    def _log_retry(self, attempt: int, error: Exception, max_attempts: int) -> None:
        """Log retry attempt."""
        logger.debug(
            f"OpenAI request attempt {attempt}/{max_attempts} failed: {error}; "
            f"retrying..."
        )
    
    async def _backoff_sleep(self, attempt: int) -> None:
        """Sleep with exponential backoff and jitter."""
        # Exponential backoff: 0.5s, 1s, 2s, ... with 25% jitter
        delay = INITIAL_RETRY_DELAY * (2 ** (attempt - 1))
        jitter = delay * random.random() * 0.25
        sleep_duration = delay + jitter
        
        logger.debug(f"Backing off for {sleep_duration:.2f}s before retry")
        await asyncio.sleep(sleep_duration)


__all__ = ["OpenAIChatClient"]
