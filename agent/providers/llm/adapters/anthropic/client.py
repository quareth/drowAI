"""Anthropic Messages API adapter for provider-neutral LLM calls.

This module owns Anthropic SDK construction, Messages request shaping,
response text extraction, and usage normalization. Application and graph code
must continue to call the provider-neutral ``LLMClient`` methods.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Mapping

from ...core.base import (
    ChatMessage,
    LLMClient,
    LLMResponse,
    LLMStreamingResponse,
    StructuredOutputSpec,
    ToolCallResult,
    ToolSpecInput,
)
from ...core.capabilities import LLMCapability
from ...core.exceptions import (
    LLMAPIError,
    LLMCapabilityNotSupportedError,
    LLMConfigurationError,
    LLMRefusalError,
    LLMResponseError,
)
from ...core.reasoning_policy import validate_reasoning_effort_for_provider_model
from ...contracts.recovery import (
    ResponseParseRetryState,
)
from .response_parser import (
    extract_anthropic_text,
    extract_anthropic_tool_calls,
    raise_for_anthropic_refusal,
)
from .structured_output import (
    apply_anthropic_structured_output_prompt,
    parse_anthropic_structured_output,
    require_anthropic_prompt_parse_structured_output_strategy,
)
from .tool_contracts import (
    coerce_tool_choice,
    normalize_anthropic_tool_choice,
    normalize_anthropic_tool_spec,
)
from ...core.identity import ANTHROPIC_PROVIDER_ID, ProviderModelRef
from ...profiles import ANTHROPIC_API_SURFACE_MESSAGES, require_model_profile

try:
    from backend.services.usage_tracking.models import UsageData

    USAGE_TRACKING_AVAILABLE = True
except ImportError:
    UsageData = None  # type: ignore
    USAGE_TRACKING_AVAILABLE = False

import anthropic
from anthropic import APIConnectionError, APIError, APIStatusError, RateLimitError

ANTHROPIC_AVAILABLE = True


logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 4096
DEFAULT_RETRY_COUNT = 2
INITIAL_RETRY_DELAY = 0.5
_NO_SAMPLING_MODEL_IDS = frozenset(
    {
        "claude-fable-5",
        "claude-mythos-5",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-sonnet-5",
    }
)
_ALWAYS_ADAPTIVE_THINKING_MODEL_IDS = frozenset(
    {"claude-fable-5", "claude-mythos-5"}
)


class AnthropicMessagesClient(LLMClient):
    """Anthropic Messages API client implementing the neutral LLM contract."""

    def __init__(self, api_key: str, model: str, **kwargs: Any) -> None:
        if not api_key or not isinstance(api_key, str) or not api_key.strip():
            raise LLMConfigurationError(
                "Anthropic API key is required",
                provider=ANTHROPIC_PROVIDER_ID,
            )
        if not model or not isinstance(model, str) or not model.strip():
            raise LLMConfigurationError(
                "Anthropic model is required",
                provider=ANTHROPIC_PROVIDER_ID,
            )

        self._model = model
        self._reasoning_effort = self._resolve_reasoning_effort(
            kwargs.get("reasoning_effort")
        )
        sdk_options = {"api_key": api_key.strip()}
        base_url = kwargs.get("base_url")
        if base_url is not None:
            if (
                not isinstance(base_url, str)
                or not base_url
                or base_url != base_url.strip()
            ):
                raise LLMConfigurationError(
                    "Anthropic client base URL must be a non-empty string",
                    provider=ANTHROPIC_PROVIDER_ID,
                )
            sdk_options["base_url"] = base_url
        self._client = anthropic.AsyncAnthropic(**sdk_options)
        self._close_lock = asyncio.Lock()
        self._closed = False

    @property
    def model(self) -> str:
        """Return the provider request model."""
        return self._model

    async def aclose(self) -> None:
        """Close the owned Anthropic SDK client exactly once."""
        async with self._close_lock:
            if self._closed:
                return
            await self._client.close()
            self._closed = True

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        **kwargs: Any,
    ) -> str:
        """Single-turn chat through Anthropic Messages."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return await self.chat_messages(messages, **kwargs)

    async def chat_messages(
        self,
        messages: list[ChatMessage],
        **kwargs: Any,
    ) -> str:
        """Multi-turn chat through Anthropic Messages."""
        response = await self.chat_messages_with_usage(messages, **kwargs)
        return response.content

    async def stream_chat_messages(
        self,
        messages: list[ChatMessage],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Stream text chunks from Anthropic Messages."""
        request_kwargs = self._build_request_kwargs(messages, kwargs)
        partial_chunks: list[str] = []
        try:
            async with self._client.messages.stream(**request_kwargs) as stream:
                async for text in stream.text_stream:
                    if text:
                        chunk = str(text)
                        partial_chunks.append(chunk)
                        yield chunk
                final_message = await self._resolve_stream_final_message(stream)
                if final_message is not None:
                    raise_for_anthropic_refusal(
                        final_message,
                        model=self._model,
                        usage=self._extract_usage(final_message),
                        partial_content="".join(partial_chunks),
                    )
        except (APIError, APIConnectionError, RateLimitError, APIStatusError) as exc:
            raise self._wrap_api_error(exc) from exc
        except LLMResponseError:
            raise
        except Exception as exc:
            raise LLMAPIError(
                f"Unexpected error during Anthropic streaming chat: {exc}",
                provider=ANTHROPIC_PROVIDER_ID,
            ) from exc

    async def stream_chat_messages_with_usage(
        self,
        messages: list[ChatMessage],
        **kwargs: Any,
    ) -> LLMStreamingResponse:
        """Stream Anthropic Messages text and expose final normalized usage."""
        structured_spec = self._get_structured_output_spec(kwargs)
        if structured_spec is not None:
            raise LLMConfigurationError(
                "Anthropic streaming structured_output is not supported",
                provider=ANTHROPIC_PROVIDER_ID,
            )
        request_kwargs = self._build_request_kwargs(messages, kwargs)
        final_usage: list[Any] = [None]

        async def content_generator() -> AsyncIterator[str]:
            partial_chunks: list[str] = []
            try:
                async with self._client.messages.stream(**request_kwargs) as stream:
                    async for text in stream.text_stream:
                        if text:
                            chunk = str(text)
                            partial_chunks.append(chunk)
                            yield chunk

                    final_message = await self._resolve_stream_final_message(stream)
                    if final_message is not None:
                        refusal_usage = self._extract_usage(final_message)
                        final_usage[0] = refusal_usage
                        raise_for_anthropic_refusal(
                            final_message,
                            model=self._model,
                            usage=refusal_usage,
                            partial_content="".join(partial_chunks),
                        )
            except (APIError, APIConnectionError, RateLimitError, APIStatusError) as exc:
                raise self._wrap_api_error(exc) from exc
            except (LLMConfigurationError, LLMResponseError):
                raise
            except Exception as exc:
                raise LLMAPIError(
                    f"Unexpected error during Anthropic usage-aware streaming chat: {exc}",
                    provider=ANTHROPIC_PROVIDER_ID,
                ) from exc

        def get_usage() -> Any:
            return final_usage[0]

        return LLMStreamingResponse(
            content_iterator=content_generator(),
            get_final_usage=get_usage,
        )

    async def chat_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        **kwargs: Any,
    ) -> LLMResponse:
        """Single-turn chat with usage tracking."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return await self.chat_messages_with_usage(messages, **kwargs)

    async def chat_messages_with_usage(
        self,
        messages: list[ChatMessage],
        **kwargs: Any,
    ) -> LLMResponse:
        """Multi-turn chat with usage tracking."""
        max_attempts = int(kwargs.get("_retries", DEFAULT_RETRY_COUNT)) + 1
        last_error: Exception | None = None
        parse_retry = ResponseParseRetryState()

        for attempt in range(1, max_attempts + 1):
            try:
                structured_spec = self._get_structured_output_spec(kwargs)
                require_anthropic_prompt_parse_structured_output_strategy(
                    structured_spec,
                    model=self._model,
                )
                request_messages = apply_anthropic_structured_output_prompt(
                    messages,
                    structured_spec,
                )
                request_kwargs = self._build_request_kwargs(request_messages, kwargs)
                response = await self._client.messages.create(**request_kwargs)

                usage = self._extract_usage(response)
                partial_content = extract_anthropic_text(
                    response,
                    allow_empty=True,
                    allowed_block_types={"text", "tool_use"},
                )
                raise_for_anthropic_refusal(
                    response,
                    model=self._model,
                    usage=usage,
                    partial_content=partial_content,
                )
                content = extract_anthropic_text(response)
                if not content.strip():
                    raise LLMResponseError(
                        "Anthropic returned empty response content",
                        provider=ANTHROPIC_PROVIDER_ID,
                    )

                structured_output = parse_anthropic_structured_output(
                    content,
                    structured_spec,
                    response,
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
                    await self._backoff_sleep(attempt)
                    continue
                raise
            except (LLMConfigurationError, LLMCapabilityNotSupportedError):
                raise
            except (APIError, APIConnectionError, RateLimitError, APIStatusError) as exc:
                last_error = self._wrap_api_error(exc)
                if attempt >= max_attempts:
                    logger.warning(
                        "Anthropic chat_messages_with_usage failed after %s attempts: %s",
                        attempt,
                        exc,
                    )
                    raise last_error from exc
                await self._backoff_sleep(attempt)
            except Exception as exc:
                last_error = LLMAPIError(
                    f"Unexpected error during Anthropic chat: {exc}",
                    provider=ANTHROPIC_PROVIDER_ID,
                )
                if attempt >= max_attempts:
                    logger.warning(
                        "Anthropic chat_messages_with_usage failed after %s attempts: %s",
                        attempt,
                        exc,
                    )
                    raise last_error from exc
                await self._backoff_sleep(attempt)

        raise last_error or LLMAPIError(
            "Anthropic chat_messages_with_usage failed",
            provider=ANTHROPIC_PROVIDER_ID,
        )

    async def chat_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[ToolSpecInput],
        tool_choice: Any = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        """Chat completion with Anthropic client tool calling."""
        result = await self.chat_with_tools_with_usage(
            system_prompt,
            user_prompt,
            tools,
            tool_choice,
            **kwargs,
        )
        result.usage = None
        return result

    async def chat_with_tools_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[ToolSpecInput],
        tool_choice: Any = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        """Chat completion with Anthropic client tool calling and usage."""
        if not tools:
            self._validate_empty_tool_request(tool_choice)
            response = await self.chat_with_usage(system_prompt, user_prompt, **kwargs)
            return ToolCallResult(
                content=response.content,
                tool_calls=None,
                raw=response.raw,
                usage=response.usage,
            )

        request_kwargs = self._build_request_kwargs(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            kwargs,
        )
        self._validate_tool_request(tool_choice)
        request_kwargs["tools"] = [normalize_anthropic_tool_spec(tool) for tool in tools]
        request_kwargs["tool_choice"] = normalize_anthropic_tool_choice(tool_choice)

        try:
            response = await self._client.messages.create(**request_kwargs)
            usage = self._extract_usage(response)
            partial_content = extract_anthropic_text(
                response,
                allow_empty=True,
                allowed_block_types={"text", "tool_use"},
            )
            raise_for_anthropic_refusal(
                response,
                model=self._model,
                usage=usage,
                partial_content=partial_content,
            )
            content = extract_anthropic_text(
                response,
                allow_empty=True,
                allowed_block_types={"text", "tool_use"},
            )
            tool_calls = extract_anthropic_tool_calls(response)
            if not content.strip() and not tool_calls:
                raise LLMResponseError(
                    "Anthropic returned no text or tool calls",
                    provider=ANTHROPIC_PROVIDER_ID,
                )
            return ToolCallResult(
                content=content or None,
                tool_calls=tool_calls or None,
                raw=response,
                usage=usage,
            )
        except (LLMResponseError, LLMConfigurationError):
            raise
        except (APIError, APIConnectionError, RateLimitError, APIStatusError) as exc:
            raise self._wrap_api_error(exc) from exc
        except Exception as exc:
            raise LLMAPIError(
                f"Unexpected error during Anthropic tool call: {exc}",
                provider=ANTHROPIC_PROVIDER_ID,
            ) from exc

    def _build_request_kwargs(
        self,
        messages: list[ChatMessage],
        kwargs: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Build Anthropic Messages request kwargs from neutral history."""
        system_prompt, request_messages = self._convert_messages(messages)
        request_kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": kwargs.get("max_tokens", DEFAULT_MAX_TOKENS),
            "messages": request_messages,
        }
        if system_prompt:
            request_kwargs["system"] = system_prompt
        reasoning_effort = self._resolve_reasoning_effort(
            kwargs.get("reasoning_effort", self._reasoning_effort)
        )
        if reasoning_effort is not None:
            request_kwargs["output_config"] = {"effort": reasoning_effort}
        thinking = kwargs.get("thinking")
        if thinking is not None:
            self._validate_thinking_config(thinking)
            request_kwargs["thinking"] = thinking
        if (
            kwargs.get("temperature") is not None
            and self._model not in _NO_SAMPLING_MODEL_IDS
        ):
            request_kwargs["temperature"] = kwargs["temperature"]
        return request_kwargs

    def _resolve_reasoning_effort(self, effort: Any) -> str | None:
        """Resolve and validate effort against the exact Anthropic profile."""
        profile = require_model_profile(
            ProviderModelRef(ANTHROPIC_PROVIDER_ID, self._model)
        )
        resolved_effort = effort
        if resolved_effort is None:
            resolved_effort = profile.default_reasoning_effort
        if resolved_effort is None:
            return None
        try:
            return validate_reasoning_effort_for_provider_model(
                effort=str(resolved_effort),
                provider=ANTHROPIC_PROVIDER_ID,
                model=self._model,
            )
        except ValueError as exc:
            raise LLMConfigurationError(
                str(exc),
                provider=ANTHROPIC_PROVIDER_ID,
            ) from exc

    def _validate_thinking_config(self, thinking: Any) -> None:
        """Reject thinking configurations known to be invalid for exact models."""
        if not isinstance(thinking, Mapping):
            raise LLMConfigurationError(
                "Anthropic thinking configuration must be a mapping",
                provider=ANTHROPIC_PROVIDER_ID,
            )
        thinking_type = str(thinking.get("type") or "").strip().lower()
        if (
            self._model in _ALWAYS_ADAPTIVE_THINKING_MODEL_IDS
            and thinking_type == "disabled"
        ):
            raise LLMConfigurationError(
                f"Model '{self._model}' requires adaptive thinking",
                provider=ANTHROPIC_PROVIDER_ID,
            )
        if self._model in _NO_SAMPLING_MODEL_IDS and thinking_type == "enabled":
            raise LLMConfigurationError(
                f"Model '{self._model}' does not support manual extended thinking",
                provider=ANTHROPIC_PROVIDER_ID,
            )

    def _validate_tool_request(self, tool_choice: Any) -> None:
        """Validate tool capability and requested choice mode before SDK calls."""
        profile = require_model_profile(
            ProviderModelRef(ANTHROPIC_PROVIDER_ID, self._model)
        )
        profile.require_capability(LLMCapability.TOOLS)
        choice = coerce_tool_choice(tool_choice)
        if choice.mode not in profile.tool_choice_modes:
            raise LLMCapabilityNotSupportedError(
                (
                    f"Model '{self._model}' does not support tool_choice "
                    f"mode '{choice.mode}'"
                ),
                provider=ANTHROPIC_PROVIDER_ID,
                capability=f"tool_choice:{choice.mode}",
            )

    @staticmethod
    def _validate_empty_tool_request(tool_choice: Any) -> None:
        """Reject forced tool choices when no callable tool schemas are supplied."""
        choice = coerce_tool_choice(tool_choice)
        if choice.mode in {"required", "specific"}:
            raise LLMCapabilityNotSupportedError(
                (
                    f"Anthropic tool_choice mode '{choice.mode}' requires at "
                    "least one tool schema"
                ),
                provider=ANTHROPIC_PROVIDER_ID,
                capability=f"tool_choice:{choice.mode}",
            )

    def _convert_messages(
        self,
        messages: list[ChatMessage],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert neutral text-first messages into Anthropic Messages input."""
        system_parts: list[str] = []
        request_messages: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role", "")).strip().lower()
            content = self._normalize_message_content(message)
            if role == "system":
                if content:
                    system_parts.append(content)
                continue
            if role not in {"user", "assistant"}:
                raise LLMConfigurationError(
                    f"Anthropic Messages does not support role '{role}'",
                    provider=ANTHROPIC_PROVIDER_ID,
                )
            if message.get("tool_calls"):
                raise LLMCapabilityNotSupportedError(
                    "Anthropic historical assistant tool_calls are not supported yet",
                    provider=ANTHROPIC_PROVIDER_ID,
                    capability="tools",
                )
            request_messages.append({"role": role, "content": content})

        if not request_messages:
            raise LLMConfigurationError(
                "Anthropic Messages requires at least one user or assistant message",
                provider=ANTHROPIC_PROVIDER_ID,
            )
        return "\n\n".join(system_parts) or None, request_messages

    @staticmethod
    def _normalize_message_content(message: Mapping[str, Any]) -> str:
        """Return text content from a neutral message envelope."""
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, Mapping) and part.get("type") in {"text", "input_text"}:
                    parts.append(str(part.get("text", "")))
                elif isinstance(part, str):
                    parts.append(part)
                else:
                    raise LLMConfigurationError(
                        "Anthropic adapter only accepts text message content in runner_control",
                        provider=ANTHROPIC_PROVIDER_ID,
                    )
            return "\n".join(part for part in parts if part)
        if content is None:
            return ""
        raise LLMConfigurationError(
            "Anthropic adapter only accepts text message content in runner_control",
            provider=ANTHROPIC_PROVIDER_ID,
        )

    @staticmethod
    def _get_structured_output_spec(kwargs: Mapping[str, Any]) -> StructuredOutputSpec | None:
        """Return a structured-output spec from kwargs when present."""
        structured_spec = kwargs.get("structured_output")
        if structured_spec is None:
            return None
        if not isinstance(structured_spec, StructuredOutputSpec):
            raise LLMConfigurationError(
                "structured_output must be a StructuredOutputSpec",
                provider=ANTHROPIC_PROVIDER_ID,
            )
        return structured_spec

    def _extract_usage(self, response: Any) -> Any:
        """Extract provider-aware usage data from Anthropic response."""
        if not USAGE_TRACKING_AVAILABLE or UsageData is None:
            return None
        return UsageData.from_anthropic_messages_response(response, self._model)

    @staticmethod
    async def _resolve_stream_final_message(stream: Any) -> Any:
        """Return the final accumulated Anthropic message from a stream object."""
        getter = getattr(stream, "get_final_message", None)
        if callable(getter):
            result = getter()
            if hasattr(result, "__await__"):
                return await result
            return result
        return (
            getattr(stream, "current_message_snapshot", None)
            or getattr(stream, "final_message", None)
        )

    @staticmethod
    def _wrap_api_error(exc: Exception) -> LLMAPIError:
        """Wrap Anthropic SDK exceptions in provider-neutral API errors."""
        return LLMAPIError(
            f"Anthropic API request failed: {exc}",
            provider=ANTHROPIC_PROVIDER_ID,
            status_code=getattr(exc, "status_code", None),
        )

    @staticmethod
    async def _backoff_sleep(attempt: int) -> None:
        """Sleep before retrying an Anthropic request."""
        await asyncio.sleep(INITIAL_RETRY_DELAY * (2 ** (attempt - 1)))


__all__ = [
    "ANTHROPIC_AVAILABLE",
    "AnthropicMessagesClient",
    "ANTHROPIC_API_SURFACE_MESSAGES",
]
