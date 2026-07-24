"""Policy-bound adapter for validated OpenAI-compatible Chat endpoints.

The adapter reuses native Chat Completions parsing while admitting only the
features declared by a code-owned dialect. Endpoint and credential handling
remain outside the dialect contract and guarded by the backend runtime.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from enum import Enum
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List, Mapping
from urllib.parse import urlsplit

import openai

from ...contracts.compat import LLMDialectPolicy
from ...contracts.inference_transport import AsyncLLMInferenceTransport
from ...contracts.structured_output_strategy import (
    normalize_structured_output_strategy,
)
from ...contracts.tool_contracts import normalize_tool_choice_mode
from ...core.base import (
    LLMCallOptions,
    LLMResponse,
    LLMStreamingResponse,
    StructuredOutputSpec,
    ToolCallResult,
    ToolChoiceInput,
    ToolSpecInput,
)
from ...core.capabilities import CapabilityInput, LLMCapability
from ...core.exceptions import (
    LLMCapabilityNotSupportedError,
    LLMConfigurationError,
)
from ...profiles.registry import ModelProfile
from .chat import OpenAIChatClient
from .client_options import openai_sdk_client_options
from .compatible_dialects import (
    AGENT_OPENAI_COMPATIBLE_DIALECT,
    CONSERVATIVE_OPENAI_COMPATIBLE_DIALECT,
    OPENAI_COMPATIBLE_CHAT_ADAPTER_ID,
    OPENAI_COMPATIBLE_CHAT_ADAPTER_VERSION,
    validate_openai_compatible_dialect,
)
from .compatible_request_policies import (
    CompatibleRequestOptions,
    CompatibleRequestPolicy,
    resolve_compatible_request_policy,
)
from .structured_output import (
    StructuredOutputSchemaError,
    build_chat_response_format,
    validate_openai_strict_schema,
)


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


_PLAIN_CALL_OPTIONS = frozenset(
    {"_retries", "max_tokens", "reasoning_effort", "structured_output", "temperature"}
)
_STREAM_CALL_OPTIONS = frozenset({"max_tokens", "reasoning_effort", "temperature"})
_TOOL_CALL_OPTIONS = frozenset(
    {"_retries", "max_tokens", "parallel_tool_calls", "reasoning_effort", "temperature"}
)
_HTTP_SCHEME = "http"
_HTTPS_SCHEME = "https"
_REQUEST_OPTIONS: ContextVar[CompatibleRequestOptions] = ContextVar(
    "compatible_request_options",
    default=CompatibleRequestOptions(),
)


class _PolicyBoundCompatibleChatCompletions:
    """Apply a validated route policy immediately before provider I/O."""

    def __init__(self, completions: Any, policy: CompatibleRequestPolicy) -> None:
        self._completions = completions
        self._policy = policy

    async def create(self, **kwargs: Any) -> Any:
        request_kwargs = self._policy.translate(kwargs, _REQUEST_OPTIONS.get())
        return await self._completions.create(**request_kwargs)


class _PolicyBoundCompatibleChat:
    """Expose a policy-bound ``chat.completions`` namespace."""

    def __init__(self, chat: Any, policy: CompatibleRequestPolicy) -> None:
        self.completions = _PolicyBoundCompatibleChatCompletions(
            chat.completions,
            policy,
        )


class _PolicyBoundCompatibleSDKClient:
    """SDK-compatible proxy that owns final route-policy translation."""

    def __init__(self, sdk_client: Any, policy: CompatibleRequestPolicy) -> None:
        self._sdk_client = sdk_client
        self.chat = _PolicyBoundCompatibleChat(sdk_client.chat, policy)

    async def close(self) -> None:
        close = getattr(self._sdk_client, "close", None)
        if close is None:
            return
        result = close()
        if hasattr(result, "__await__"):
            await result
class _GuardedCompatibleChatCompletions:
    """SDK-shaped Chat Completions facade backed by guarded transport."""

    def __init__(self, transport: AsyncLLMInferenceTransport) -> None:
        self._transport = transport

    async def create(self, **kwargs: Any) -> Any:
        """Execute one Chat Completions request through the guarded boundary."""

        if kwargs.get("stream") is True:
            return _guarded_stream(self._transport.stream_json_events(dict(kwargs)))
        payload = await self._transport.request_json(dict(kwargs))
        return _object_payload(payload)


class _GuardedCompatibleChat:
    """Expose the SDK-compatible ``chat.completions`` namespace."""

    def __init__(self, transport: AsyncLLMInferenceTransport) -> None:
        self.completions = _GuardedCompatibleChatCompletions(transport)


class _GuardedCompatibleSDKClient:
    """Minimal async client contract consumed by ``OpenAIChatClient``."""

    def __init__(self, transport: AsyncLLMInferenceTransport) -> None:
        self.chat = _GuardedCompatibleChat(transport)

    async def close(self) -> None:
        """Match the OpenAI SDK close contract without owning resources."""
        return None


class OpenAICompatibleChatClient(OpenAIChatClient):
    """Chat client constrained by a registered compatible dialect policy."""

    def __init__(
        self,
        *,
        base_url: str,
        auth: CompatibleChatAuth,
        wire_model_id: str,
        dialect_policy: LLMDialectPolicy = CONSERVATIVE_OPENAI_COMPATIBLE_DIALECT,
        request_policy_id: str | None = None,
        reasoning_effort: str | None = None,
        model_profile: ModelProfile | None = None,
        inference_transport: AsyncLLMInferenceTransport | None = None,
    ) -> None:
        validated_base_url = _validate_base_url(
            base_url,
            guarded=inference_transport is not None,
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
        validate_openai_compatible_dialect(dialect_policy)
        default_reasoning_effort = _validate_default_reasoning_effort(
            reasoning_effort,
            dialect_policy=dialect_policy,
        )
        request_policy = resolve_compatible_request_policy(request_policy_id)

        if inference_transport is not None:
            sdk_client = _GuardedCompatibleSDKClient(inference_transport)
        else:
            sdk_client = openai.AsyncOpenAI(
                **openai_sdk_client_options(
                    api_key=auth.credential,
                    base_url=validated_base_url,
                    enforce_credentials=auth.mode is not CompatibleChatAuthMode.NONE,
                )
            )
        sdk_client = _PolicyBoundCompatibleSDKClient(sdk_client, request_policy)

        self._initialize_client_state(
            api_key=auth.credential,
            model=validated_wire_model,
            sdk_client=sdk_client,
        )
        self._base_url = validated_base_url
        self._auth_mode = auth.mode
        self._dialect_policy = dialect_policy
        self._default_reasoning_effort = default_reasoning_effort
        self._model_profile = model_profile

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
        return await super().chat_messages(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            **call_kwargs,
        )

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
        request_options = _REQUEST_OPTIONS.get()
        stream = super().stream_chat_messages(messages, **call_kwargs)
        async for chunk in _with_request_options(stream, request_options):
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
        return await super().chat_messages_with_usage(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
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
        """Stream text and provider-reported usage when the dialect admits it."""
        try:
            call_kwargs = self._validate_call(
                kwargs,
                allowed_legacy=_STREAM_CALL_OPTIONS,
                required_capabilities=(
                    LLMCapability.CHAT,
                    LLMCapability.STREAMING,
                    LLMCapability.STREAMING_USAGE_REPORTING,
                ),
                include_stream_usage=True,
                allow_retries=False,
            )
        except LLMCapabilityNotSupportedError as exc:
            raise _dialect_policy_required("streaming usage") from exc
        request_options = _REQUEST_OPTIONS.get()
        response = await super().stream_chat_messages_with_usage(messages, **call_kwargs)
        return LLMStreamingResponse(
            content_iterator=_with_request_options(
                response.content_iterator,
                request_options,
            ),
            get_final_usage=response.get_final_usage,
        )

    async def chat_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[ToolSpecInput],
        tool_choice: ToolChoiceInput = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        """Run a normalized function-tool call when the dialect admits it."""
        try:
            call_kwargs = self._validate_tool_call(kwargs, tool_choice=tool_choice)
        except LLMCapabilityNotSupportedError as exc:
            raise _dialect_policy_required("tool calls") from exc
        return await super().chat_with_tools(
            system_prompt,
            user_prompt,
            tools,
            tool_choice=tool_choice,
            **call_kwargs,
        )

    async def chat_with_tools_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[ToolSpecInput],
        tool_choice: ToolChoiceInput = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        """Run a usage-tracked normalized function-tool call."""
        try:
            call_kwargs = self._validate_tool_call(
                kwargs,
                tool_choice=tool_choice,
                require_usage=True,
            )
        except LLMCapabilityNotSupportedError as exc:
            raise _dialect_policy_required("tool calls") from exc
        return await super().chat_with_tools_with_usage(
            system_prompt,
            user_prompt,
            tools,
            tool_choice=tool_choice,
            **call_kwargs,
        )

    def _validate_tool_call(
        self,
        kwargs: dict[str, Any],
        *,
        tool_choice: ToolChoiceInput,
        require_usage: bool = False,
    ) -> dict[str, Any]:
        """Validate tool controls against the selected compatible dialect."""

        required = [LLMCapability.CHAT, LLMCapability.TOOLS]
        if require_usage:
            required.append(LLMCapability.USAGE_REPORTING)
        return self._validate_call(
            kwargs,
            allowed_legacy=_TOOL_CALL_OPTIONS,
            required_capabilities=tuple(required),
            tool_choice_mode=_tool_choice_mode(tool_choice),
        )

    def _validate_call(
        self,
        kwargs: dict[str, Any],
        *,
        allowed_legacy: frozenset[str],
        required_capabilities: tuple[CapabilityInput, ...],
        include_stream_usage: bool = False,
        tool_choice_mode: str | None = None,
        allow_retries: bool = True,
    ) -> dict[str, Any]:
        """Validate typed or legacy controls and return native call kwargs."""

        remaining = dict(kwargs)
        typed_options = remaining.pop("call_options", None)
        if typed_options is not None:
            if not isinstance(typed_options, LLMCallOptions):
                raise LLMConfigurationError(
                    "call_options must be LLMCallOptions",
                    provider="OpenAI-compatible",
                )
            _reject_unsupported_options(remaining, allowed=allowed_legacy)
            conflicting_controls = sorted(set(remaining) - {"structured_output"})
            if conflicting_controls:
                raise LLMConfigurationError(
                    "call_options cannot be combined with legacy options: "
                    + ", ".join(conflicting_controls),
                    provider="OpenAI-compatible",
                )
            structured_strategy = typed_options.structured_output_strategy
            if remaining.get("structured_output") is not None:
                if structured_strategy not in {None, "native_schema"}:
                    raise LLMConfigurationError(
                        "Compatible Chat structured output requires native_schema",
                        provider="OpenAI-compatible",
                    )
                structured_strategy = "native_schema"
            resolved_tool_mode = typed_options.tool_choice_mode
            if tool_choice_mode is not None:
                if resolved_tool_mode not in {None, tool_choice_mode}:
                    raise LLMConfigurationError(
                        "call_options tool choice does not match tool_choice",
                        provider="OpenAI-compatible",
                    )
                resolved_tool_mode = tool_choice_mode
            options = replace(
                typed_options,
                tool_choice_mode=resolved_tool_mode,
                structured_output_strategy=structured_strategy,
                include_stream_usage=(
                    typed_options.include_stream_usage or include_stream_usage
                ),
            )
        else:
            _reject_unsupported_options(remaining, allowed=allowed_legacy)
            options = LLMCallOptions(
                temperature=remaining.get("temperature"),
                max_tokens=remaining.get("max_tokens"),
                tool_choice_mode=tool_choice_mode,
                structured_output_strategy=(
                    "native_schema" if remaining.get("structured_output") is not None else None
                ),
                include_stream_usage=include_stream_usage,
                reasoning_effort=remaining.get("reasoning_effort"),
                retry_attempts=remaining.get("_retries"),
                parallel_tool_calls=remaining.get("parallel_tool_calls"),
            )

        options = _options_with_default_reasoning(
            options,
            self._default_reasoning_effort,
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
        _validate_effective_call_options(
            options,
            model_profile=self._model_profile,
            required_capabilities=required_capabilities,
        )
        native_kwargs = _native_call_kwargs(options)
        structured_output = remaining.get("structured_output")
        if structured_output is not None:
            native_kwargs["structured_output"] = structured_output
        if options.parallel_tool_calls is not None:
            native_kwargs["parallel_tool_calls"] = options.parallel_tool_calls
        _REQUEST_OPTIONS.set(
            CompatibleRequestOptions(reasoning_effort=options.reasoning_effort)
        )
        return native_kwargs

    def _attach_structured_response_format(
        self,
        request_kwargs: Dict[str, Any],
        structured_spec: StructuredOutputSpec | None,
    ) -> None:
        """Attach native JSON schema after dialect and schema validation."""

        if structured_spec is None:
            return
        self._dialect_policy.validate_call_options(
            LLMCallOptions(structured_output_strategy="native_schema"),
            required_capabilities=(LLMCapability.STRUCTURED_OUTPUT_NATIVE,),
        )
        try:
            validate_openai_strict_schema(structured_spec)
        except StructuredOutputSchemaError as exc:
            raise LLMConfigurationError(
                str(exc),
                provider="OpenAI-compatible",
            ) from exc
        request_kwargs["response_format"] = build_chat_response_format(structured_spec)


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


def _validate_default_reasoning_effort(
    reasoning_effort: str | None,
    *,
    dialect_policy: LLMDialectPolicy,
) -> str | None:
    """Validate the construction-time default reasoning effort."""

    if reasoning_effort is None:
        return None
    options = LLMCallOptions(reasoning_effort=reasoning_effort)
    dialect_policy.validate_call_options(
        options,
        required_capabilities=(LLMCapability.REASONING_EFFORT,),
    )
    return options.reasoning_effort


def _options_with_default_reasoning(
    options: LLMCallOptions,
    default_reasoning_effort: str | None,
) -> LLMCallOptions:
    """Apply the client default when a call does not choose an effort."""

    if default_reasoning_effort is None or options.reasoning_effort is not None:
        return options
    return replace(options, reasoning_effort=default_reasoning_effort)


def _validate_effective_call_options(
    options: LLMCallOptions,
    *,
    model_profile: ModelProfile | None,
    required_capabilities: tuple[CapabilityInput, ...],
) -> None:
    """Reject controls excluded by the route-effective profile before I/O."""

    if model_profile is None:
        return
    for capability in required_capabilities:
        model_profile.require_capability(capability)
    if options.tool_choice_mode is not None:
        mode = normalize_tool_choice_mode(options.tool_choice_mode)
        if mode not in model_profile.tool_choice_modes:
            raise LLMCapabilityNotSupportedError(
                f"Selected route does not support tool_choice '{mode}'",
                provider="OpenAI-compatible",
                capability=f"tool_choice:{mode}",
            )
    if options.parallel_tool_calls is not None:
        model_profile.require_capability(LLMCapability.PARALLEL_TOOLS)
    if options.structured_output_strategy is not None:
        strategy = normalize_structured_output_strategy(
            options.structured_output_strategy
        )
        if strategy not in model_profile.structured_output_strategies:
            raise LLMCapabilityNotSupportedError(
                f"Selected route does not support structured strategy '{strategy}'",
                provider="OpenAI-compatible",
                capability=f"structured_output:{strategy}",
            )
    if options.include_stream_usage:
        model_profile.require_capability(LLMCapability.STREAMING_USAGE_REPORTING)
    if options.reasoning_effort is not None:
        model_profile.require_capability(LLMCapability.REASONING_EFFORT)
        if (
            model_profile.reasoning_efforts
            and options.reasoning_effort not in model_profile.reasoning_efforts
        ):
            raise LLMCapabilityNotSupportedError(
                (
                    "Selected route does not support reasoning_effort "
                    f"'{options.reasoning_effort}'"
                ),
                provider="OpenAI-compatible",
                capability=f"reasoning_effort:{options.reasoning_effort}",
            )


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


def _tool_choice_mode(tool_choice: ToolChoiceInput) -> str | None:
    """Return the provider-neutral mode represented by a tool-choice value."""

    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        return tool_choice
    mode = getattr(tool_choice, "mode", None)
    if isinstance(mode, str):
        return mode
    if isinstance(tool_choice, Mapping):
        if tool_choice.get("type") == "function" or "function" in tool_choice:
            return "specific"
    raise LLMConfigurationError(
        "Compatible Chat tool_choice is invalid",
        provider="OpenAI-compatible",
    )


def _dialect_policy_required(feature: str) -> LLMConfigurationError:
    """Return a stable fail-closed error for an unavailable dialect feature."""

    return LLMConfigurationError(
        f"Compatible Chat {feature} requires an explicit dialect policy",
        provider="OpenAI-compatible",
    )


def _object_payload(value: Any) -> Any:
    """Convert decoded provider JSON into SDK-like attribute objects."""

    if isinstance(value, dict):
        return SimpleNamespace(
            **{str(key): _object_payload(child) for key, child in value.items()}
        )
    if isinstance(value, list):
        return [_object_payload(child) for child in value]
    return value


async def _guarded_stream(events: AsyncIterator[Any]) -> AsyncIterator[Any]:
    """Yield bounded guarded stream events in the SDK async-iterator shape."""

    async for event in events:
        yield _object_payload(event)


async def _with_request_options(
    stream: AsyncIterator[str],
    options: CompatibleRequestOptions,
) -> AsyncIterator[str]:
    """Run delayed streaming request creation with its validated options."""

    iterator = stream.__aiter__()
    while True:
        token = _REQUEST_OPTIONS.set(options)
        try:
            chunk = await iterator.__anext__()
        except StopAsyncIteration:
            return
        finally:
            _REQUEST_OPTIONS.reset(token)
        yield chunk


__all__ = [
    "AGENT_OPENAI_COMPATIBLE_DIALECT",
    "CompatibleChatAuth",
    "CompatibleChatAuthMode",
    "CONSERVATIVE_OPENAI_COMPATIBLE_DIALECT",
    "OPENAI_COMPATIBLE_CHAT_ADAPTER_ID",
    "OPENAI_COMPATIBLE_CHAT_ADAPTER_VERSION",
    "OpenAICompatibleChatClient",
]
