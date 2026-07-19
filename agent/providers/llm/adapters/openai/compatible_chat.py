"""Conservative unregistered adapter for validated OpenAI-compatible endpoints.

The adapter reuses the native Chat Completions implementation but enables only
plain text calls until a code-owned dialect policy explicitly admits tools,
structured output, streaming usage differences, and other optional behavior.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable, Dict, List, Mapping
from urllib.parse import urlsplit

import openai

from ...contracts.compat import LLMDialectPolicy
from ...core.base import (
    LLMCallOptions,
    LLMResponse,
    LLMStreamingResponse,
    ToolCallResult,
    ToolChoiceInput,
    ToolSpecInput,
)
from ...core.capabilities import CapabilityInput, LLMCapability
from ...core.exceptions import (
    LLMCapabilityNotSupportedError,
    LLMConfigurationError,
)
from .chat import OpenAIChatClient
from .client_options import openai_sdk_client_options


class CompatibleChatAuthMode(str, Enum):
    """Authentication modes understood by the conservative compatible client."""

    NONE = "none"
    BEARER = "bearer"


@dataclass(frozen=True, slots=True)
class CompatibleChatAuth:
    """Typed ephemeral auth material for one compatible client."""

    mode: CompatibleChatAuthMode
    credential: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.mode, CompatibleChatAuthMode):
            raise TypeError("mode must be CompatibleChatAuthMode")
        if self.mode is CompatibleChatAuthMode.NONE:
            if self.credential is not None:
                raise ValueError("none auth cannot carry a credential")
            return
        if not isinstance(self.credential, str) or not self.credential.strip():
            raise ValueError("bearer auth requires a credential")

    @classmethod
    def none(cls) -> "CompatibleChatAuth":
        """Return explicit unauthenticated endpoint access."""
        return cls(mode=CompatibleChatAuthMode.NONE)

    @classmethod
    def bearer(cls, credential: str) -> "CompatibleChatAuth":
        """Return explicit bearer/API-key endpoint access."""
        return cls(mode=CompatibleChatAuthMode.BEARER, credential=credential)


_PLAIN_CALL_OPTIONS = frozenset({"_retries", "max_tokens", "temperature"})
_STREAM_CALL_OPTIONS = frozenset({"max_tokens", "temperature"})
OPENAI_COMPATIBLE_CHAT_ADAPTER_ID = "openai_compatible_chat"
OPENAI_COMPATIBLE_CHAT_ADAPTER_VERSION = "1"
_ADAPTER_CAPABILITY_CEILING = frozenset(
    {
        LLMCapability.CHAT,
        LLMCapability.STREAMING,
        LLMCapability.USAGE_REPORTING,
    }
)
_HTTP_SCHEME = "http"
_HTTPS_SCHEME = "https"
CONSERVATIVE_OPENAI_COMPATIBLE_DIALECT = LLMDialectPolicy(
    policy_id="openai_compatible_chat.conservative_v1",
    adapter_id=OPENAI_COMPATIBLE_CHAT_ADAPTER_ID,
    api_surface="chat_completions",
    capabilities=_ADAPTER_CAPABILITY_CEILING,
    max_retry_attempts=2,
)
GuardedCompatibleChatExecutor = Callable[[Mapping[str, Any]], bytes]


class _GuardedCompatibleChatCompletions:
    """SDK-shaped Chat Completions facade backed by guarded transport."""

    def __init__(self, executor: GuardedCompatibleChatExecutor) -> None:
        self._executor = executor

    async def create(self, **kwargs: Any) -> Any:
        """Execute one Chat Completions request through the guarded boundary."""

        body = self._executor(dict(kwargs))
        payload = _decode_guarded_payload(body)
        if kwargs.get("stream") is True:
            return _guarded_stream(payload)
        return _object_payload(payload)


class _GuardedCompatibleChat:
    """Expose the SDK-compatible ``chat.completions`` namespace."""

    def __init__(self, executor: GuardedCompatibleChatExecutor) -> None:
        self.completions = _GuardedCompatibleChatCompletions(executor)


class _GuardedCompatibleSDKClient:
    """Minimal async client contract consumed by ``OpenAIChatClient``."""

    def __init__(self, executor: GuardedCompatibleChatExecutor) -> None:
        self.chat = _GuardedCompatibleChat(executor)

    async def close(self) -> None:
        """Match the OpenAI SDK close contract without owning resources."""
        return None


class OpenAICompatibleChatClient(OpenAIChatClient):
    """Direct-only compatible client with a deliberately narrow call surface."""

    def __init__(
        self,
        *,
        base_url: str,
        auth: CompatibleChatAuth,
        wire_model_id: str,
        dialect_policy: LLMDialectPolicy = CONSERVATIVE_OPENAI_COMPATIBLE_DIALECT,
        guarded_executor: GuardedCompatibleChatExecutor | None = None,
    ) -> None:
        validated_base_url = _validate_base_url(
            base_url,
            guarded=guarded_executor is not None,
        )
        validated_wire_model = _validate_wire_model_id(wire_model_id)
        if not isinstance(auth, CompatibleChatAuth):
            raise LLMConfigurationError(
                "compatible auth must be CompatibleChatAuth",
                provider="OpenAI-compatible",
            )
        if not isinstance(dialect_policy, LLMDialectPolicy):
            raise LLMConfigurationError(
                "dialect_policy must be LLMDialectPolicy",
                provider="OpenAI-compatible",
            )
        dialect_policy.validate_adapter_binding(
            expected_adapter_id=OPENAI_COMPATIBLE_CHAT_ADAPTER_ID,
            allowed_capabilities=_ADAPTER_CAPABILITY_CEILING,
            max_retry_attempts=2,
        )

        if guarded_executor is not None:
            sdk_client = _GuardedCompatibleSDKClient(guarded_executor)
        else:
            sdk_client = openai.AsyncOpenAI(
                **openai_sdk_client_options(
                    api_key=auth.credential,
                    base_url=validated_base_url,
                    enforce_credentials=auth.mode is not CompatibleChatAuthMode.NONE,
                )
            )

        self._initialize_client_state(
            api_key=auth.credential,
            model=validated_wire_model,
            sdk_client=sdk_client,
        )
        self._base_url = validated_base_url
        self._auth_mode = auth.mode
        self._dialect_policy = dialect_policy

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        **kwargs: Any,
    ) -> str:
        """Run a plain single-turn compatible Chat Completions call."""
        call_kwargs = self._validate_call(
            kwargs,
            allowed_legacy=_PLAIN_CALL_OPTIONS,
            required_capabilities=(LLMCapability.CHAT,),
        )
        return await super().chat(system_prompt, user_prompt, **call_kwargs)

    async def chat_messages(
        self,
        messages: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        """Run a plain multi-turn compatible Chat Completions call."""
        call_kwargs = self._validate_call(
            kwargs,
            allowed_legacy=_PLAIN_CALL_OPTIONS,
            required_capabilities=(LLMCapability.CHAT,),
        )
        return await super().chat_messages(messages, **call_kwargs)

    async def stream_chat_messages(
        self,
        messages: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Stream plain compatible text using the native event parser."""
        call_kwargs = self._validate_call(
            kwargs,
            allowed_legacy=_STREAM_CALL_OPTIONS,
            required_capabilities=(
                LLMCapability.CHAT,
                LLMCapability.STREAMING,
            ),
            allow_retries=False,
        )
        async for chunk in super().stream_chat_messages(messages, **call_kwargs):
            yield chunk

    async def chat_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        **kwargs: Any,
    ) -> LLMResponse:
        """Run a plain single-turn call with native-shaped usage capture."""
        call_kwargs = self._validate_call(
            kwargs,
            allowed_legacy=_PLAIN_CALL_OPTIONS,
            required_capabilities=(
                LLMCapability.CHAT,
                LLMCapability.USAGE_REPORTING,
            ),
        )
        return await super().chat_with_usage(
            system_prompt,
            user_prompt,
            **call_kwargs,
        )

    async def chat_messages_with_usage(
        self,
        messages: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> LLMResponse:
        """Run a plain multi-turn call with native-shaped usage capture."""
        call_kwargs = self._validate_call(
            kwargs,
            allowed_legacy=_PLAIN_CALL_OPTIONS,
            required_capabilities=(
                LLMCapability.CHAT,
                LLMCapability.USAGE_REPORTING,
            ),
        )
        return await super().chat_messages_with_usage(messages, **call_kwargs)

    async def stream_chat_messages_with_usage(
        self,
        messages: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> LLMStreamingResponse:
        """Reject streaming usage until dialect behavior is registered."""
        try:
            self._dialect_policy.validate_call_options(
                LLMCallOptions(include_stream_usage=True),
                required_capabilities=(
                    LLMCapability.CHAT,
                    LLMCapability.STREAMING,
                ),
            )
        except (LLMCapabilityNotSupportedError, LLMConfigurationError) as exc:
            raise _dialect_policy_required("streaming usage") from exc
        raise _dialect_policy_required("streaming usage")

    async def chat_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[ToolSpecInput],
        tool_choice: ToolChoiceInput = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        """Reject tool calls until dialect behavior is registered."""
        try:
            self._dialect_policy.validate_call_options(
                LLMCallOptions(),
                required_capabilities=(LLMCapability.CHAT, LLMCapability.TOOLS),
            )
        except (LLMCapabilityNotSupportedError, LLMConfigurationError) as exc:
            raise _dialect_policy_required("tool calls") from exc
        raise _dialect_policy_required("tool calls")

    async def chat_with_tools_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[ToolSpecInput],
        tool_choice: ToolChoiceInput = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        """Reject usage-tracked tools until dialect behavior is registered."""
        try:
            self._dialect_policy.validate_call_options(
                LLMCallOptions(),
                required_capabilities=(
                    LLMCapability.CHAT,
                    LLMCapability.TOOLS,
                    LLMCapability.USAGE_REPORTING,
                ),
            )
        except (LLMCapabilityNotSupportedError, LLMConfigurationError) as exc:
            raise _dialect_policy_required("tool calls") from exc
        raise _dialect_policy_required("tool calls")

    def _validate_call(
        self,
        kwargs: dict[str, Any],
        *,
        allowed_legacy: frozenset[str],
        required_capabilities: tuple[CapabilityInput, ...],
        allow_retries: bool = True,
    ) -> dict[str, Any]:
        """Validate typed or legacy controls and return native call kwargs."""

        remaining = dict(kwargs)
        typed_options = remaining.pop("call_options", None)
        if typed_options is not None:
            if remaining:
                names = ", ".join(sorted(remaining))
                raise LLMConfigurationError(
                    f"call_options cannot be combined with legacy options: {names}",
                    provider="OpenAI-compatible",
                )
            if not isinstance(typed_options, LLMCallOptions):
                raise LLMConfigurationError(
                    "call_options must be LLMCallOptions",
                    provider="OpenAI-compatible",
                )
            options = typed_options
        else:
            _reject_unsupported_options(remaining, allowed=allowed_legacy)
            options = LLMCallOptions(
                temperature=remaining.get("temperature"),
                max_tokens=remaining.get("max_tokens"),
                retry_attempts=remaining.get("_retries"),
            )

        if not allow_retries and options.retry_attempts is not None:
            raise LLMConfigurationError(
                "streaming calls do not support retry_attempts",
                provider="OpenAI-compatible",
            )
        self._dialect_policy.validate_call_options(
            options,
            required_capabilities=required_capabilities,
        )
        return _native_call_kwargs(options)


def _validate_base_url(base_url: str, *, guarded: bool) -> str:
    """Validate base URL syntax while guarded egress owns address policy."""

    if (
        not isinstance(base_url, str)
        or not base_url
        or base_url != base_url.strip()
        or any(character.isspace() for character in base_url)
    ):
        raise LLMConfigurationError(
            "compatible base URL must be a non-empty absolute URL",
            provider="OpenAI-compatible",
        )
    try:
        parsed = urlsplit(base_url)
        parsed.port
    except ValueError as exc:
        raise LLMConfigurationError(
            "compatible base URL is invalid",
            provider="OpenAI-compatible",
        ) from exc
    if (
        parsed.scheme not in {_HTTP_SCHEME, _HTTPS_SCHEME}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == _HTTP_SCHEME and not guarded)
    ):
        raise LLMConfigurationError(
            "compatible base URL must use HTTPS unless guarded egress authorizes "
            "HTTP, and cannot contain userinfo, query, or fragment",
            provider="OpenAI-compatible",
        )
    return base_url


def _validate_wire_model_id(wire_model_id: str) -> str:
    """Validate but never normalize the exact model identifier sent on the wire."""

    if (
        not isinstance(wire_model_id, str)
        or not wire_model_id
        or wire_model_id != wire_model_id.strip()
    ):
        raise LLMConfigurationError(
            "wire_model_id must be non-empty and cannot contain outer whitespace",
            provider="OpenAI-compatible",
        )
    return wire_model_id


def _reject_unsupported_options(
    options: dict[str, Any],
    *,
    allowed: frozenset[str],
) -> None:
    """Fail closed instead of silently dropping unregistered request options."""

    unsupported = sorted(set(options) - allowed)
    if unsupported:
        raise LLMConfigurationError(
            f"Unsupported compatible Chat request parameters: {', '.join(unsupported)}",
            provider="OpenAI-compatible",
        )


def _native_call_kwargs(options: LLMCallOptions) -> dict[str, Any]:
    """Translate validated common controls to the current native adapter keys."""

    kwargs: dict[str, Any] = {}
    if options.temperature is not None:
        kwargs["temperature"] = options.temperature
    if options.max_tokens is not None:
        kwargs["max_tokens"] = options.max_tokens
    if options.retry_attempts is not None:
        kwargs["_retries"] = options.retry_attempts
    return kwargs


def _dialect_policy_required(feature: str) -> LLMConfigurationError:
    """Return the fail-closed error for optional compatible protocol features."""

    return LLMConfigurationError(
        f"Compatible Chat {feature} requires an explicit dialect policy",
        provider="OpenAI-compatible",
    )


def _decode_guarded_payload(body: bytes) -> Any:
    """Decode guarded response bytes into Chat Completions payload objects."""

    if not isinstance(body, bytes):
        raise LLMConfigurationError(
            "guarded compatible response body must be bytes",
            provider="OpenAI-compatible",
        )
    text = body.decode("utf-8")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if any(line.startswith("data:") for line in lines):
        events: list[Any] = []
        for line in lines:
            if not line.startswith("data:"):
                continue
            data = line.removeprefix("data:").strip()
            if data == "[DONE]":
                continue
            events.append(json.loads(data))
        return events
    return json.loads(text)


def _object_payload(value: Any) -> Any:
    """Convert decoded provider JSON into SDK-like attribute objects."""

    if isinstance(value, dict):
        return SimpleNamespace(
            **{str(key): _object_payload(child) for key, child in value.items()}
        )
    if isinstance(value, list):
        return [_object_payload(child) for child in value]
    return value


async def _guarded_stream(events: Any) -> AsyncIterator[Any]:
    """Yield bounded guarded stream events in the SDK async-iterator shape."""

    if isinstance(events, dict):
        yield _object_payload(events)
        return
    if not isinstance(events, list):
        raise LLMConfigurationError(
            "guarded compatible stream response is invalid",
            provider="OpenAI-compatible",
        )
    for event in events:
        yield _object_payload(event)


__all__ = [
    "CompatibleChatAuth",
    "CompatibleChatAuthMode",
    "CONSERVATIVE_OPENAI_COMPATIBLE_DIALECT",
    "GuardedCompatibleChatExecutor",
    "OPENAI_COMPATIBLE_CHAT_ADAPTER_ID",
    "OPENAI_COMPATIBLE_CHAT_ADAPTER_VERSION",
    "OpenAICompatibleChatClient",
]
