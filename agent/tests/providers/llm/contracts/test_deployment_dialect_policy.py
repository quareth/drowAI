"""Tests for typed deployment call options and data-only dialect policies."""

from __future__ import annotations

from dataclasses import asdict, FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.providers.llm.adapters.openai.compatible_chat import (
    CompatibleChatAuth,
    OpenAICompatibleChatClient,
)
from agent.providers.llm.contracts.compat import LLMDialectPolicy
from agent.providers.llm.core.base import LLMCallOptions
from agent.providers.llm.core.capabilities import LLMCapability
from agent.providers.llm.core.exceptions import (
    LLMCapabilityNotSupportedError,
    LLMConfigurationError,
)
from agent.providers.llm.factory.client_factory import LLMClientFactory


def _policy(**overrides: object) -> LLMDialectPolicy:
    """Build a minimal compatible Chat policy for contract tests."""

    values: dict[str, object] = {
        "policy_id": "test.compatible_chat",
        "adapter_id": "openai_compatible_chat",
        "api_surface": "chat_completions",
        "capabilities": frozenset(
            {
                LLMCapability.CHAT,
                LLMCapability.STREAMING,
                LLMCapability.USAGE_REPORTING,
            }
        ),
        "max_retry_attempts": 2,
    }
    values.update(overrides)
    return LLMDialectPolicy(**values)


def test_call_options_are_typed_immutable_and_non_secret() -> None:
    """Call options expose only bounded non-secret request controls."""

    options = LLMCallOptions(
        temperature=0.4,
        max_tokens=128,
        tool_choice_mode="auto",
        structured_output_strategy="native_schema",
        include_stream_usage=True,
        reasoning_effort="medium",
        retry_attempts=1,
        parallel_tool_calls=False,
    )

    assert options.temperature == 0.4
    with pytest.raises(FrozenInstanceError):
        options.temperature = 0.8  # type: ignore[misc]
    assert set(asdict(options)) == {
        "temperature",
        "max_tokens",
        "tool_choice_mode",
        "structured_output_strategy",
        "include_stream_usage",
        "reasoning_effort",
        "retry_attempts",
        "parallel_tool_calls",
    }


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("temperature", -0.1),
        ("max_tokens", 0),
        ("retry_attempts", -1),
        ("reasoning_effort", " "),
    ],
)
def test_call_options_reject_invalid_values(field_name: str, value: object) -> None:
    """Invalid option values fail at contract construction."""

    with pytest.raises((TypeError, ValueError)):
        LLMCallOptions(**{field_name: value})


def test_dialect_policy_has_no_network_secret_or_executable_fields() -> None:
    """Policy serialization contains no endpoint, header, secret, or callback lane."""

    policy = _policy()
    serialized = asdict(policy)

    assert set(serialized) == {
        "policy_id",
        "adapter_id",
        "api_surface",
        "capabilities",
        "tool_choice_modes",
        "structured_output_strategies",
        "reasoning_efforts",
        "max_retry_attempts",
    }
    for forbidden in ("endpoint", "header", "secret", "token", "callback"):
        assert all(forbidden not in key for key in serialized)

    with pytest.raises(TypeError):
        LLMDialectPolicy(
            policy_id="unsafe",
            adapter_id="openai_compatible_chat",
            api_surface="chat_completions",
            capabilities=frozenset({LLMCapability.CHAT}),
            headers={"Authorization": "secret"},  # type: ignore[call-arg]
        )
    with pytest.raises(TypeError):
        LLMDialectPolicy(
            policy_id="unsafe",
            adapter_id="openai_compatible_chat",
            api_surface="chat_completions",
            capabilities=frozenset({LLMCapability.CHAT}),
            request_callback=lambda: None,  # type: ignore[call-arg]
        )


@pytest.mark.parametrize(
    "options",
    [
        LLMCallOptions(tool_choice_mode="auto"),
        LLMCallOptions(structured_output_strategy="native_schema"),
        LLMCallOptions(include_stream_usage=True),
        LLMCallOptions(reasoning_effort="medium"),
        LLMCallOptions(parallel_tool_calls=False),
    ],
)
def test_policy_rejects_unsupported_capabilities(options: LLMCallOptions) -> None:
    """Optional request features fail closed when policy capability is absent."""

    with pytest.raises(LLMCapabilityNotSupportedError):
        _policy().validate_call_options(options)


def test_policy_rejects_unregistered_modes_strategies_and_retry_behavior() -> None:
    """Policy allowlists constrain tool modes, schema strategy, and retries."""

    policy = _policy(
        capabilities=frozenset(
            {
                LLMCapability.CHAT,
                LLMCapability.TOOLS,
                LLMCapability.STRUCTURED_OUTPUT_NATIVE,
            }
        ),
        tool_choice_modes=frozenset({"auto"}),
        structured_output_strategies=frozenset({"native_schema"}),
        max_retry_attempts=1,
    )

    with pytest.raises(LLMCapabilityNotSupportedError, match="required"):
        policy.validate_call_options(LLMCallOptions(tool_choice_mode="required"))
    with pytest.raises(LLMCapabilityNotSupportedError, match="prompt_parse"):
        policy.validate_call_options(
            LLMCallOptions(structured_output_strategy="prompt_parse")
        )
    with pytest.raises(LLMConfigurationError, match="retry"):
        policy.validate_call_options(LLMCallOptions(retry_attempts=2))


def test_adapter_binding_rejects_unregistered_prompt_parse_strategy() -> None:
    """Prompt parsing cannot bypass the executable adapter's strategy ceiling."""

    policy = _policy(structured_output_strategies=frozenset({"prompt_parse"}))

    with pytest.raises(LLMConfigurationError, match="structured_output_strategies"):
        _compatible_client(policy)


def _compatible_client(
    policy: LLMDialectPolicy,
) -> tuple[OpenAICompatibleChatClient, MagicMock]:
    """Create a compatible adapter with a mocked SDK transport."""

    message = SimpleNamespace(content="ok", refusal=None, tool_calls=None)
    response = SimpleNamespace(
        id="response",
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=None,
    )
    sdk_client = MagicMock()
    sdk_client.close = AsyncMock()
    sdk_client.chat.completions.create = AsyncMock(return_value=response)
    with patch(
        "agent.providers.llm.adapters.openai.compatible_chat.openai.AsyncOpenAI",
        MagicMock(return_value=sdk_client),
    ):
        client = OpenAICompatibleChatClient(
            base_url="https://inference.example/v1",
            auth=CompatibleChatAuth.bearer("test-key"),
            wire_model_id="Vendor/Model.Name-20B",
            dialect_policy=policy,
        )
    return client, sdk_client


@pytest.mark.asyncio
async def test_compatible_adapter_validates_typed_options_before_request() -> None:
    """Typed options are policy-checked and translated only after validation."""

    client, sdk = _compatible_client(_policy())

    await client.chat_messages(
        [{"role": "user", "content": "hello"}],
        call_options=LLMCallOptions(
            temperature=0.3,
            max_tokens=96,
            retry_attempts=1,
        ),
    )

    assert sdk.chat.completions.create.await_args.kwargs == {
        "model": "Vendor/Model.Name-20B",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.3,
        "max_tokens": 96,
    }


@pytest.mark.asyncio
async def test_compatible_adapter_fails_closed_before_sdk_call() -> None:
    """A policy capability denial prevents the outbound SDK operation."""

    client, sdk = _compatible_client(_policy())

    with pytest.raises(LLMCapabilityNotSupportedError):
        await client.chat_messages(
            [{"role": "user", "content": "hello"}],
            call_options=LLMCallOptions(structured_output_strategy="native_schema"),
        )

    sdk.chat.completions.create.assert_not_awaited()


def test_compatible_adapter_rejects_wrong_or_overbroad_policy_binding() -> None:
    """Policy data cannot select another executable adapter or expand its ceiling."""

    with pytest.raises(LLMConfigurationError, match="adapter"):
        _compatible_client(_policy(adapter_id="some_other_adapter"))
    with pytest.raises(LLMConfigurationError, match="capabilities"):
        _compatible_client(
            _policy(
                    capabilities=frozenset(
                        {
                            LLMCapability.CHAT,
                            LLMCapability.REMOTE_CONVERSATION_LIFECYCLE,
                        }
                    )
            )
        )
    with pytest.raises(LLMConfigurationError, match="retry limit"):
        _compatible_client(_policy(max_retry_attempts=3))


def test_typed_policy_does_not_enable_new_factory_selection() -> None:
    """Adding policy data does not register an adapter, provider, or model."""

    registrations = LLMClientFactory.list_providers()
    prefixes = LLMClientFactory.list_prefix_registrations()

    assert all("OpenAICompatibleChatClient" not in value for value in registrations.values())
    assert all(value != "OpenAICompatibleChatClient" for value in prefixes.values())
