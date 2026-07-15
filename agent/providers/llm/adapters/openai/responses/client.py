"""OpenAI Responses API provider for GPT-5 models.

This module implements the LLMClient interface for GPT-5 models using
the OpenAI Responses API (stateless, no conversation management).

Key Design Decisions:
- Stateless: Each call is independent (no conversation IDs)
- History explicit: Pass conversation history in input[] array
- Canonical reasoning: Supports model-aware effort coercion
- Same interface: Returns identical types as OpenAIChatClient
- No fallbacks: Errors fail loudly with proper logging

Note: This provider does NOT use the Conversations API or maintain
conversation state. Conversation history should be passed explicitly
in the messages parameter when needed.

Token Usage Tracking:
    - chat_messages_with_usage() returns LLMResponse with usage data
    - stream_chat_messages_with_usage() returns LLMStreamingResponse with final usage
    - Original methods preserved for backward compatibility
    - Usage comes from response.done event in streaming mode
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from ....core.base import (
    LLMClient,
    LLMResponse,
    LLMStreamingResponse,
    StructuredOutputSpec,
    ToolChoiceInput,
    ToolCall,
    ToolCallResult,
    ToolSpecInput,
)
from ....core.exceptions import (
    LLMAPIError,
    LLMConfigurationError,
    LLMRefusalError,
    LLMResponseError,
)
from ....contracts.recovery import (
    ResponseParseRetryState,
)
from .reasoning import (
    default_reasoning_effort,
    resolve_reasoning_effort,
    validate_reasoning_effort,
)
from .request_builder import (
    build_chat_messages_request_kwargs,
    build_chat_request_kwargs,
    build_tool_request_kwargs,
    convert_messages_to_input,
    convert_tool_choice,
    convert_tools_for_responses,
)
from .response_parser import (
    coerce_text_fragment,
    extract_output_text,
    extract_structured_content_text,
    extract_tool_calls,
    extract_usage_from_response,
)
from ..refusal import (
    raise_for_openai_responses_refusal,
    raise_for_openai_responses_stream_refusal,
)
from .retry import (
    DEFAULT_RETRY_COUNT as RETRY_DEFAULT_RETRY_COUNT,
    INITIAL_RETRY_DELAY as RETRY_INITIAL_RETRY_DELAY,
    backoff_sleep,
    log_retry,
    wrap_api_error,
)
from .stream_parser import (
    extract_stream_delta,
    is_done_event,
)
from .structured import (
    attach_structured_output_format,
    build_structured_output_diagnostics,
    get_structured_output_spec,
    parse_structured_output,
    safe_inc as structured_safe_inc,
    structured_metric_suffix,
)

# Import UsageData for token tracking
try:
    from backend.services.usage_tracking.models import UsageData
    USAGE_TRACKING_AVAILABLE = True
except ImportError:
    USAGE_TRACKING_AVAILABLE = False
    UsageData = None  # type: ignore

logger = logging.getLogger(__name__)


def _safe_obj_value(value: Any, key: str, default: Any = None) -> Any:
    """Read a key from dict-like or SDK objects without raising."""
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _summarize_tool_names(tools: List[Dict[str, Any]]) -> List[str]:
    """Return function names from Responses tool specs without schema details."""
    names: List[str] = []
    for tool in tools or []:
        name = _safe_obj_value(tool, "name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _summarize_response_output_items(response: Any) -> List[Dict[str, Any]]:
    """Return a safe shape summary of Responses output items."""
    output = _safe_obj_value(response, "output", None)
    if not output:
        return []

    summaries: List[Dict[str, Any]] = []
    for item in output:
        item_type = _safe_obj_value(item, "type")
        summary: Dict[str, Any] = {"type": item_type}
        status = _safe_obj_value(item, "status")
        if status:
            summary["status"] = status
        if item_type == "function_call":
            name = _safe_obj_value(item, "name")
            call_id = _safe_obj_value(item, "call_id")
            arguments = _safe_obj_value(item, "arguments")
            summary["name"] = name
            summary["has_call_id"] = bool(call_id)
            summary["arguments_chars"] = len(arguments) if isinstance(arguments, str) else None
        content = _safe_obj_value(item, "content")
        if content:
            summary["content_types"] = [_safe_obj_value(part, "type") for part in content]
        summaries.append(summary)
    return summaries


def _summarize_usage(response: Any) -> Dict[str, Any]:
    """Return token usage fields useful for diagnosing truncated tool calls."""
    usage = _safe_obj_value(response, "usage", None)
    if usage is None:
        return {}
    output_details = _safe_obj_value(usage, "output_tokens_details", None)
    return {
        "input_tokens": _safe_obj_value(usage, "input_tokens"),
        "output_tokens": _safe_obj_value(usage, "output_tokens"),
        "total_tokens": _safe_obj_value(usage, "total_tokens"),
        "reasoning_tokens": _safe_obj_value(output_details, "reasoning_tokens"),
    }


def _safe_inc(metric_name: str) -> None:
    """Increment metrics when backend metrics utilities are available."""
    structured_safe_inc(metric_name)

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
# This is used across all GPT-5 LLM calls unless explicitly overridden
DEFAULT_MAX_TOKENS = 10000
DEFAULT_RETRY_COUNT = RETRY_DEFAULT_RETRY_COUNT
INITIAL_RETRY_DELAY = RETRY_INITIAL_RETRY_DELAY


# ---------------------------------------------------------------------------
# OpenAI Responses API Provider
# ---------------------------------------------------------------------------


class OpenAIResponsesClient(LLMClient):
    """OpenAI Responses API client for GPT-5 models.

    Implements stateless Responses API - no conversation state management.
    Each call is independent; pass history explicitly when needed.

    Features:
        - Automatic retry with exponential backoff
        - Proper exception wrapping (same as OpenAIChatClient)
        - Streaming support via async generator
        - Tool/function calling with Responses API format
        - Configurable model-aware reasoning effort (profile default when omitted)

    Example:
        client = OpenAIResponsesClient(api_key="sk-...", model="gpt-5")
        response = await client.chat("You are helpful.", "Hello!")

        # Streaming
        async for chunk in client.stream_chat_messages(messages):
            print(chunk, end="")

        # Tool calling
        result = await client.chat_with_tools(system, user, tools)
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-5",
        reasoning_effort: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize OpenAI Responses API client.

        Args:
            api_key: OpenAI API key
            model: GPT-5 model identifier (gpt-5, gpt-5-mini, gpt-5.2, etc.)
            reasoning_effort: Optional override for model reasoning effort.
                              Accepted values come from the exact model profile.
            **kwargs: Additional configuration (reserved for future use)

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
        self._resolution_role = str(kwargs.get("resolution_role", "unspecified"))
        self._resolution_source = str(kwargs.get("resolution_source", "unspecified"))
        effort = (
            reasoning_effort
            if reasoning_effort is not None
            else default_reasoning_effort(model)
        )
        self._reasoning_effort = self._validate_reasoning_effort(effort)
        self._client = openai.AsyncOpenAI(api_key=api_key)

        logger.debug(
            "Initialized OpenAIResponsesClient: role=%s model=%s effort=%s source=%s",
            self._resolution_role,
            model,
            self._reasoning_effort,
            self._resolution_source,
        )

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
        """Single-turn chat completion via Responses API.

        Args:
            system_prompt: System instructions (passed as `instructions`)
            user_prompt: User message
            **kwargs: Additional options:
                - max_tokens: Maximum output tokens (default: 3000)
                - reasoning_effort: Override default effort level
                - _retries: Number of retry attempts (default: 2)

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
                request_kwargs = build_chat_request_kwargs(
                    model=self._model,
                    user_prompt=user_prompt,
                    system_prompt=system_prompt,
                    max_output_tokens=kwargs.get("max_tokens", DEFAULT_MAX_TOKENS),
                    reasoning_effort=self._resolve_reasoning_effort(kwargs),
                )
                structured_spec = self._get_structured_output_spec(kwargs)
                self._attach_structured_output_format(request_kwargs, structured_spec)
                response = await self._client.responses.create(**request_kwargs)
                content = self._extract_output_text(response)
                raise_for_openai_responses_refusal(
                    response,
                    model=self._model,
                    usage=self._extract_usage_from_response(response),
                    partial_content=content,
                )

                structured_content = (
                    self._extract_structured_content_text(response)
                    if structured_spec is not None
                    else None
                )
                parse_content = structured_content or content
                if structured_spec is not None:
                    self._parse_structured_output(
                        parse_content or "",
                        structured_spec,
                        raw_response=response,
                    )
                    if (not content or not content.strip()) and structured_content:
                        content = structured_content
                if not content or not content.strip():
                    raise LLMResponseError(
                        "OpenAI Responses API returned empty content",
                        provider="OpenAI",
                    )

                logger.debug(
                    f"Responses API chat completed: model={self._model}, "
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
                # Fail fast on invalid effort/model combinations
                raise
            except (APIError, APIConnectionError, RateLimitError, APIStatusError) as e:
                last_error = self._wrap_api_error(e)
                if attempt >= max_attempts:
                    logger.warning(
                        f"Responses API chat failed after {attempt} attempts: {e}"
                    )
                    raise last_error from e
                self._log_retry(attempt, e, max_attempts)
                await self._backoff_sleep(attempt)
            except Exception as e:
                last_error = LLMAPIError(
                    f"Unexpected error during Responses API chat: {e}",
                    provider="OpenAI",
                )
                if attempt >= max_attempts:
                    logger.warning(
                        f"Responses API chat failed after {attempt} attempts: {e}"
                    )
                    raise last_error from e
                self._log_retry(attempt, e, max_attempts)
                await self._backoff_sleep(attempt)

        # Should not reach here, but satisfy type checker
        raise last_error if last_error else LLMAPIError(
            "Responses API chat failed", provider="OpenAI"
        )

    async def chat_messages(
        self,
        messages: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        """Multi-turn chat with explicit history via Responses API.

        Converts standard messages format to Responses API input format.

        Args:
            messages: Conversation history [{"role": "...", "content": "..."}]
            **kwargs: Additional options (max_tokens, reasoning_effort, _retries)

        Returns:
            Assistant response text

        Raises:
            LLMAPIError: If the API call fails after retries
            LLMResponseError: If response is empty or cannot be parsed
        """
        # Extract system prompt and convert messages to Responses API format
        system_prompt, input_messages = self._convert_messages_to_input(messages)

        max_attempts = int(kwargs.get("_retries", DEFAULT_RETRY_COUNT)) + 1
        last_error: Optional[Exception] = None
        parse_retry = ResponseParseRetryState()

        for attempt in range(1, max_attempts + 1):
            try:
                reasoning_effort = self._resolve_reasoning_effort(kwargs)
                structured_spec = self._get_structured_output_spec(kwargs)
                request_kwargs = build_chat_messages_request_kwargs(
                    model=self._model,
                    input_messages=input_messages,
                    system_prompt=system_prompt,
                    max_output_tokens=kwargs.get("max_tokens", DEFAULT_MAX_TOKENS),
                    reasoning_effort=reasoning_effort,
                )
                tools = kwargs.get("tools") or []
                if tools:
                    request_kwargs["tools"] = self._convert_tools_for_responses(tools)
                if tools and kwargs.get("tool_choice") is not None:
                    request_kwargs["tool_choice"] = self._convert_tool_choice(
                        kwargs.get("tool_choice")
                    )
                self._attach_structured_output_format(request_kwargs, structured_spec)
                response = await self._client.responses.create(**request_kwargs)
                content = self._extract_output_text(response)
                raise_for_openai_responses_refusal(
                    response,
                    model=self._model,
                    usage=self._extract_usage_from_response(response),
                    partial_content=content,
                )

                if not content or not content.strip():
                    raise LLMResponseError(
                        "OpenAI Responses API returned empty content",
                        provider="OpenAI",
                    )
                self._parse_structured_output(
                    content,
                    structured_spec,
                    raw_response=response,
                )

                logger.debug(
                    f"Responses API chat_messages completed: model={self._model}, "
                    f"messages_count={len(messages)}, response_length={len(content)}"
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
                # Fail fast on invalid effort/model combinations
                raise
            except (APIError, APIConnectionError, RateLimitError, APIStatusError) as e:
                last_error = self._wrap_api_error(e)
                if attempt >= max_attempts:
                    logger.warning(
                        f"Responses API chat_messages failed after {attempt} attempts: {e}"
                    )
                    raise last_error from e
                self._log_retry(attempt, e, max_attempts)
                await self._backoff_sleep(attempt)
            except Exception as e:
                last_error = LLMAPIError(
                    f"Unexpected error during Responses API chat_messages: {e}",
                    provider="OpenAI",
                )
                if attempt >= max_attempts:
                    logger.warning(
                        f"Responses API chat_messages failed after {attempt} attempts: {e}"
                    )
                    raise last_error from e
                self._log_retry(attempt, e, max_attempts)
                await self._backoff_sleep(attempt)

        # Should not reach here, but satisfy type checker
        raise last_error if last_error else LLMAPIError(
            "Responses API chat_messages failed", provider="OpenAI"
        )

    async def stream_chat_messages(
        self,
        messages: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Stream multi-turn chat via Responses API.

        Yields text chunks as they arrive, matching OpenAIChatClient behavior.

        Args:
            messages: Conversation history
            **kwargs: Additional options (max_tokens, reasoning_effort)

        Yields:
            Text chunks (strings)

        Raises:
            LLMAPIError: If the API call fails
        """
        system_prompt, input_messages = self._convert_messages_to_input(messages)
        
        logger.info(
            f"[GPT5-STREAM] Starting stream_chat_messages: model={self._model}, "
            f"message_count={len(messages)}, max_tokens={kwargs.get('max_tokens', DEFAULT_MAX_TOKENS)}, "
            f"reasoning_effort={kwargs.get('reasoning_effort', self._reasoning_effort)}"
        )
        
        chunk_count = 0
        total_chars = 0
        event_count = 0
        partial_chunks: List[str] = []
        refusal_parts: List[str] = []

        try:
            reasoning_effort = self._resolve_reasoning_effort(kwargs)
            request_kwargs = build_chat_messages_request_kwargs(
                model=self._model,
                input_messages=input_messages,
                system_prompt=system_prompt,
                max_output_tokens=kwargs.get("max_tokens", DEFAULT_MAX_TOKENS),
                reasoning_effort=reasoning_effort,
            )
            
            async with self._client.responses.stream(**request_kwargs) as stream:
                async for event in stream:
                    event_count += 1
                    
                    chunk = self._extract_stream_delta(event)
                    if chunk:
                        chunk_count += 1
                        total_chars += len(chunk)
                        partial_chunks.append(chunk)
                        yield chunk
                    response = getattr(event, "response", None)
                    raise_for_openai_responses_stream_refusal(
                        event,
                        model=self._model,
                        refusal_parts=refusal_parts,
                        usage=(
                            self._extract_usage_from_response(response)
                            if response is not None
                            else None
                        ),
                        partial_content="".join(partial_chunks),
                    )

            if refusal_parts:
                raise_for_openai_responses_refusal(
                    None,
                    model=self._model,
                    partial_content="".join(partial_chunks),
                    explanation="".join(refusal_parts).strip() or None,
                )
            
            # Log final stats - NO FALLBACK, just loud logging
            logger.info(
                f"[GPT5-STREAM] Stream completed: "
                f"events={event_count}, chunks={chunk_count}, chars={total_chars}"
            )
            
            if chunk_count == 0:
                # FAIL LOUDLY - no fallback
                logger.error(
                    f"[GPT5-STREAM] STREAMING YIELDED 0 CHUNKS from {event_count} events! "
                    f"model={self._model}, reasoning_effort={self._reasoning_effort}. "
                    f"This is a critical streaming extraction failure."
                )

        except (APIError, APIConnectionError, RateLimitError, APIStatusError) as e:
            logger.error(f"[GPT5-STREAM] API error: {e}")
            raise self._wrap_api_error(e) from e
        except LLMConfigurationError:
            raise
        except LLMRefusalError:
            raise
        except Exception as e:
            logger.error(f"[GPT5-STREAM] Streaming failed: {e}", exc_info=True)
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
        """Single-turn chat completion with usage capture via Responses API.
        
        This is the usage-tracking equivalent of chat(). Use this method
        when you need token usage data from a single-turn LLM call.
        
        Args:
            system_prompt: System instructions (passed as `instructions`)
            user_prompt: User message
            **kwargs: Additional options (max_tokens, reasoning_effort, etc.)
            
        Returns:
            LLMResponse with content and usage data (including reasoning_tokens for GPT-5)
            
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
        """Multi-turn chat with explicit history via Responses API with usage tracking.

        Same as chat_messages() but returns LLMResponse containing
        both content and token usage data.

        Args:
            messages: Conversation history [{"role": "...", "content": "..."}]
            **kwargs: Additional options (max_tokens, reasoning_effort, _retries)

        Returns:
            LLMResponse with content and usage data

        Raises:
            LLMAPIError: If the API call fails after retries
            LLMResponseError: If response is empty or cannot be parsed
        """
        # Extract system prompt and convert messages to Responses API format
        system_prompt, input_messages = self._convert_messages_to_input(messages)

        max_attempts = int(kwargs.get("_retries", DEFAULT_RETRY_COUNT)) + 1
        last_error: Optional[Exception] = None
        parse_retry = ResponseParseRetryState()

        for attempt in range(1, max_attempts + 1):
            try:
                reasoning_effort = self._resolve_reasoning_effort(kwargs)
                structured_spec = self._get_structured_output_spec(kwargs)
                request_kwargs = build_chat_messages_request_kwargs(
                    model=self._model,
                    input_messages=input_messages,
                    system_prompt=system_prompt,
                    max_output_tokens=kwargs.get("max_tokens", DEFAULT_MAX_TOKENS),
                    reasoning_effort=reasoning_effort,
                )
                tools = kwargs.get("tools") or []
                if tools:
                    request_kwargs["tools"] = self._convert_tools_for_responses(tools)
                if tools and kwargs.get("tool_choice") is not None:
                    request_kwargs["tool_choice"] = self._convert_tool_choice(
                        kwargs.get("tool_choice")
                    )
                self._attach_structured_output_format(request_kwargs, structured_spec)
                response = await self._client.responses.create(**request_kwargs)
                usage = self._extract_usage_from_response(response)
                content = self._extract_output_text(response)
                raise_for_openai_responses_refusal(
                    response,
                    model=self._model,
                    usage=usage,
                    partial_content=content,
                )

                structured_content = (
                    self._extract_structured_content_text(response)
                    if structured_spec is not None
                    else None
                )
                parse_content = structured_content or content
                structured_output = None
                if structured_spec is not None:
                    structured_output = self._parse_structured_output(
                        parse_content or "",
                        structured_spec,
                        raw_response=response,
                    )
                    if (not content or not content.strip()) and structured_content:
                        content = structured_content
                if not content or not content.strip():
                    raise LLMResponseError(
                        "OpenAI Responses API returned empty content",
                        provider="OpenAI",
                    )

                # Extract usage data from response
                if structured_output is None:
                    structured_output = self._parse_structured_output(
                        content,
                        structured_spec,
                        raw_response=response,
                    )

                logger.debug(
                    f"Responses API chat_messages_with_usage completed: model={self._model}, "
                    f"messages_count={len(messages)}, response_length={len(content)}, "
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
                        f"Responses API chat_messages_with_usage failed after {attempt} attempts: {e}"
                    )
                    raise last_error from e
                self._log_retry(attempt, e, max_attempts)
                await self._backoff_sleep(attempt)
            except Exception as e:
                last_error = LLMAPIError(
                    f"Unexpected error during Responses API chat_messages_with_usage: {e}",
                    provider="OpenAI",
                )
                if attempt >= max_attempts:
                    logger.warning(
                        f"Responses API chat_messages_with_usage failed after {attempt} attempts: {e}"
                    )
                    raise last_error from e
                self._log_retry(attempt, e, max_attempts)
                await self._backoff_sleep(attempt)

        raise last_error if last_error else LLMAPIError(
            "Responses API chat_messages_with_usage failed", provider="OpenAI"
        )

    async def stream_chat_messages_with_usage(
        self,
        messages: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> LLMStreamingResponse:
        """Stream multi-turn chat via Responses API with usage tracking.

        Returns an LLMStreamingResponse that provides both:
        - An async iterator for content chunks
        - A function to get final usage (call after consuming all chunks)

        Usage data is captured from the response.done event.

        Args:
            messages: Conversation history
            **kwargs: Additional options (max_tokens, reasoning_effort)

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
        system_prompt, input_messages = self._convert_messages_to_input(messages)
        
        # Capture usage in closure - will be set from response.done event
        final_usage: List[Optional["UsageData"]] = [None]
        
        async def content_generator() -> AsyncIterator[str]:
            """Generate content chunks and capture usage from response.done event."""
            chunk_count = 0
            total_chars = 0
            event_count = 0
            partial_chunks: List[str] = []
            refusal_parts: List[str] = []

            try:
                reasoning_effort = self._resolve_reasoning_effort(kwargs)
                request_kwargs = build_chat_messages_request_kwargs(
                    model=self._model,
                    input_messages=input_messages,
                    system_prompt=system_prompt,
                    max_output_tokens=kwargs.get("max_tokens", DEFAULT_MAX_TOKENS),
                    reasoning_effort=reasoning_effort,
                )
                
                async with self._client.responses.stream(**request_kwargs) as stream:
                    async for event in stream:
                        event_count += 1
                        
                        # Check for usage in response.done event
                        if is_done_event(event):
                            response = getattr(event, 'response', None)
                            if response:
                                final_usage[0] = self._extract_usage_from_response(response)
                        else:
                            response = getattr(event, "response", None)
                            if response is not None and final_usage[0] is None:
                                final_usage[0] = self._extract_usage_from_response(response)
                        
                        # Extract and yield content delta
                        chunk = self._extract_stream_delta(event)
                        if chunk:
                            chunk_count += 1
                            total_chars += len(chunk)
                            partial_chunks.append(chunk)
                            yield chunk
                        raise_for_openai_responses_stream_refusal(
                            event,
                            model=self._model,
                            refusal_parts=refusal_parts,
                            usage=final_usage[0],
                            partial_content="".join(partial_chunks),
                        )

                if refusal_parts:
                    raise_for_openai_responses_refusal(
                        None,
                        model=self._model,
                        usage=final_usage[0],
                        partial_content="".join(partial_chunks),
                        explanation="".join(refusal_parts).strip() or None,
                    )
                
                logger.debug(
                    f"[GPT5-STREAM-USAGE] Stream completed: "
                    f"events={event_count}, chunks={chunk_count}, chars={total_chars}, "
                    f"usage={'captured' if final_usage[0] else 'not captured'}"
                )

            except (APIError, APIConnectionError, RateLimitError, APIStatusError) as e:
                logger.error(f"[GPT5-STREAM-USAGE] API error: {e}")
                raise self._wrap_api_error(e) from e
            except LLMConfigurationError:
                raise
            except LLMRefusalError:
                raise
            except Exception as e:
                logger.error(f"[GPT5-STREAM-USAGE] Streaming failed: {e}", exc_info=True)
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
        """Chat with tool calling via Responses API.

        Args:
            system_prompt: System instructions
            user_prompt: User message
            tools: Neutral FunctionToolSpec values or legacy OpenAI dicts.
            tool_choice: Neutral ToolChoice or legacy OpenAI-compatible choice.
            **kwargs: Additional options

        Returns:
            ToolCallResult with content and/or tool calls

        Raises:
            LLMAPIError: If the API call fails after retries
        """
        max_attempts = int(kwargs.get("_retries", DEFAULT_RETRY_COUNT)) + 1
        last_error: Optional[Exception] = None

        # Convert tools to Responses API format
        responses_tools = self._convert_tools_for_responses(tools)

        for attempt in range(1, max_attempts + 1):
            try:
                reasoning_effort = self._resolve_reasoning_effort(kwargs)
                request_kwargs = build_tool_request_kwargs(
                    model=self._model,
                    user_prompt=user_prompt,
                    system_prompt=system_prompt,
                    max_output_tokens=kwargs.get("max_tokens", DEFAULT_MAX_TOKENS),
                    reasoning_effort=reasoning_effort,
                    responses_tools=responses_tools,
                    tool_choice=tool_choice,
                    parallel_tool_calls=kwargs.get("parallel_tool_calls"),
                )

                logger.warning(
                    "[OPENAI_RESPONSES_TOOL_REQUEST] model=%s attempt=%s/%s tools=%s "
                    "tool_names=%s tool_choice=%r parallel_tool_calls=%r "
                    "max_output_tokens=%r reasoning_effort=%s",
                    self._model,
                    attempt,
                    max_attempts,
                    len(responses_tools),
                    _summarize_tool_names(responses_tools),
                    request_kwargs.get("tool_choice"),
                    request_kwargs.get("parallel_tool_calls"),
                    request_kwargs.get("max_output_tokens"),
                    reasoning_effort,
                )
                response = await self._client.responses.create(**request_kwargs)
                content = self._extract_output_text(response)
                raise_for_openai_responses_refusal(
                    response,
                    model=self._model,
                    usage=self._extract_usage_from_response(response),
                    partial_content=content,
                )

                # Extract content and tool calls
                tool_calls = self._extract_tool_calls(response)

                logger.debug(
                    f"Responses API chat_with_tools completed: model={self._model}, "
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
                        f"Responses API chat_with_tools failed after {attempt} attempts: {e}"
                    )
                    raise last_error from e
                self._log_retry(attempt, e, max_attempts)
                await self._backoff_sleep(attempt)
            except LLMConfigurationError:
                raise
            except LLMRefusalError:
                raise
            except Exception as e:
                last_error = LLMAPIError(
                    f"Unexpected error during tool call: {e}",
                    provider="OpenAI",
                )
                if attempt >= max_attempts:
                    logger.warning(
                        f"Responses API chat_with_tools failed after {attempt} attempts: {e}"
                    )
                    raise last_error from e
                self._log_retry(attempt, e, max_attempts)
                await self._backoff_sleep(attempt)

        raise last_error if last_error else LLMAPIError(
            "Responses API tool call failed", provider="OpenAI"
        )
    
    async def chat_with_tools_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: List[ToolSpecInput],
        tool_choice: ToolChoiceInput = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        """Chat with tool calling via Responses API with usage tracking.

        Same as chat_with_tools() but captures token usage from the response.
        Use this when you need accurate token accounting for cost tracking.

        Args:
            system_prompt: System instructions
            user_prompt: User message
            tools: Neutral FunctionToolSpec values or legacy OpenAI dicts.
            tool_choice: Neutral ToolChoice or legacy OpenAI-compatible choice.
            **kwargs: Additional options

        Returns:
            ToolCallResult with content, tool calls, and usage

        Raises:
            LLMAPIError: If the API call fails after retries
        """
        max_attempts = int(kwargs.get("_retries", DEFAULT_RETRY_COUNT)) + 1
        last_error: Optional[Exception] = None

        # Convert tools to Responses API format
        responses_tools = self._convert_tools_for_responses(tools)

        for attempt in range(1, max_attempts + 1):
            try:
                request_kwargs = build_tool_request_kwargs(
                    model=self._model,
                    user_prompt=user_prompt,
                    system_prompt=system_prompt,
                    max_output_tokens=kwargs.get("max_tokens", DEFAULT_MAX_TOKENS),
                    reasoning_effort=self._resolve_reasoning_effort(kwargs),
                    responses_tools=responses_tools,
                    tool_choice=tool_choice,
                    parallel_tool_calls=kwargs.get("parallel_tool_calls"),
                )

                response = await self._client.responses.create(**request_kwargs)
                usage = self._extract_usage_from_response(response)
                content = self._extract_output_text(response)
                raise_for_openai_responses_refusal(
                    response,
                    model=self._model,
                    usage=usage,
                    partial_content=content,
                )

                # Extract content and tool calls
                tool_calls = self._extract_tool_calls(response)
                
                # Extract usage data
                parsed_tool_calls = list(tool_calls or [])
                logger.warning(
                    "[OPENAI_RESPONSES_TOOL_RESPONSE] model=%s response_id=%s status=%s "
                    "incomplete_details=%r output_items=%s parsed_tool_calls=%s "
                    "parsed_tool_names=%s has_content=%s usage=%s",
                    self._model,
                    _safe_obj_value(response, "id"),
                    _safe_obj_value(response, "status"),
                    _safe_obj_value(response, "incomplete_details"),
                    _summarize_response_output_items(response),
                    len(parsed_tool_calls),
                    [call.name for call in parsed_tool_calls],
                    content is not None,
                    _summarize_usage(response),
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
                        f"Responses API chat_with_tools_with_usage failed after {attempt} attempts: {e}"
                    )
                    raise last_error from e
                self._log_retry(attempt, e, max_attempts)
                await self._backoff_sleep(attempt)
            except LLMConfigurationError:
                raise
            except LLMRefusalError:
                raise
            except Exception as e:
                last_error = LLMAPIError(
                    f"Unexpected error during tool call: {e}",
                    provider="OpenAI",
                )
                if attempt >= max_attempts:
                    logger.warning(
                        f"Responses API chat_with_tools_with_usage failed after {attempt} attempts: {e}"
                    )
                    raise last_error from e
                self._log_retry(attempt, e, max_attempts)
                await self._backoff_sleep(attempt)

        raise last_error if last_error else LLMAPIError(
            "Responses API tool call failed", provider="OpenAI"
        )

    # -------------------------------------------------------------------------
    # Conversion Helpers
    # -------------------------------------------------------------------------

    def _convert_messages_to_input(
        self,
        messages: List[Dict[str, Any]],
    ) -> tuple[str, List[Dict[str, Any]]]:
        """Convert standard messages to Responses API input format."""
        return convert_messages_to_input(messages)

    def _convert_tools_for_responses(
        self,
        tools: List[ToolSpecInput],
    ) -> List[Dict[str, Any]]:
        """Convert neutral or legacy tools to Responses API format."""
        return convert_tools_for_responses(tools)

    def _convert_tool_choice(self, choice: Any) -> Any:
        """Convert tool_choice to Responses API format."""
        return convert_tool_choice(choice)

    def _validate_reasoning_effort(self, effort: str) -> str:
        """Validate canonical reasoning effort values and model compatibility."""
        return validate_reasoning_effort(effort, self._model)

    def _resolve_reasoning_effort(self, kwargs: Dict[str, Any]) -> str:
        """Resolve and validate per-request effort, defaulting to client policy."""
        return resolve_reasoning_effort(
            kwargs,
            default_effort=self._reasoning_effort,
            model=self._model,
            logger=logger,
            resolution_role=self._resolution_role,
            resolution_source=self._resolution_source,
        )

    def _structured_metric_suffix(self, schema_name: str) -> str:
        """Return metric-safe schema suffix."""
        return structured_metric_suffix(schema_name)

    def _get_structured_output_spec(self, kwargs: Dict[str, Any]) -> Optional[StructuredOutputSpec]:
        """Extract and type-check structured output spec from call kwargs."""
        return get_structured_output_spec(kwargs)

    def _attach_structured_output_format(
        self,
        request_kwargs: Dict[str, Any],
        structured_spec: Optional[StructuredOutputSpec],
    ) -> None:
        """Attach Responses API json_schema format payload when requested."""
        attach_structured_output_format(request_kwargs, structured_spec)

    def _parse_structured_output(
        self,
        content: str,
        structured_spec: Optional[StructuredOutputSpec],
        *,
        raw_response: Any | None = None,
    ) -> Optional[Dict[str, Any]]:
        """Parse structured content when schema contract is provided."""
        return parse_structured_output(
            content,
            structured_spec,
            raw_response=raw_response,
            logger=logger,
        )

    def _build_structured_output_diagnostics(self, response: Any | None) -> Dict[str, object]:
        """Extract best-effort diagnostics for structured parse failures."""
        return build_structured_output_diagnostics(response)

    # -------------------------------------------------------------------------
    # Response Parsing Helpers
    # -------------------------------------------------------------------------

    def _extract_output_text(self, response: Any) -> Optional[str]:
        """Extract text from Responses API response."""
        return extract_output_text(response, logger)

    @staticmethod
    def _coerce_text_fragment(value: Any) -> Optional[str]:
        """Normalize text fragments from SDK response objects."""
        return coerce_text_fragment(value)

    def _extract_structured_content_text(self, response: Any) -> Optional[str]:
        """Extract JSON text for structured-output responses when output_text is absent."""
        return extract_structured_content_text(response, logger)

    def _extract_stream_delta(self, event: Any) -> Optional[str]:
        """Extract text delta from a streaming event."""
        return extract_stream_delta(event, logger)

    def _extract_tool_calls(self, response: Any) -> Optional[List[ToolCall]]:
        """Extract tool calls from Responses API response."""
        return extract_tool_calls(response, logger)

    # -------------------------------------------------------------------------
    # Usage Extraction Helper
    # -------------------------------------------------------------------------
    
    def _extract_usage_from_response(self, response: Any) -> Optional["UsageData"]:
        """Extract UsageData from Responses API response."""
        return extract_usage_from_response(
            response,
            model=self._model,
            usage_tracking_available=USAGE_TRACKING_AVAILABLE,
            usage_data_cls=UsageData,
        )

    # -------------------------------------------------------------------------
    # Error Handling Helpers
    # -------------------------------------------------------------------------

    def _wrap_api_error(self, error: Exception) -> LLMAPIError:
        """Wrap OpenAI SDK exceptions into LLMAPIError."""
        return wrap_api_error(error)

    def _log_retry(self, attempt: int, error: Exception, max_attempts: int) -> None:
        """Log retry attempt."""
        log_retry(logger, attempt, error, max_attempts)

    async def _backoff_sleep(self, attempt: int) -> None:
        """Sleep with exponential backoff and jitter."""
        await backoff_sleep(
            logger,
            attempt,
            initial_retry_delay=INITIAL_RETRY_DELAY,
        )


__all__ = ["OpenAIResponsesClient"]
