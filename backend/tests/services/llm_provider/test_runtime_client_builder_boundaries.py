"""Characterize runtime client construction through the facade and builder.

Scope: lock factory arguments, guarded callback adaptation, reasoning validation
timing, secret failure timing, and wrapper application at the resolver boundary
while patching construction seams at their canonical builder owner.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, AsyncIterator
from uuid import uuid4

import pytest

from agent.providers.llm.core.base import ChatMessage, LLMClient, LLMResponse, ToolCallResult
from agent.providers.llm.core.exceptions import (
    LLMCapabilityNotSupportedError,
    LLMConfigurationError,
)
from agent.providers.llm.core.budget_enforcing_client import BudgetEnforcingLLMClient
from agent.providers.llm.core.identity import ProviderModelRef
from agent.providers.llm.profiles.registry import require_model_profile
from backend.services.llm_provider import runtime_client_builder as builder_module
from backend.services.llm_provider import runtime_client_resolver as resolver_module
from backend.services.llm_provider.runtime_client_resolver import (
    LLMRuntimeClientResolver,
)
from backend.services.llm_provider.types import (
    DeploymentRef,
    LLMAuthMode,
    LLMCallTarget,
    LLMConnectionOperation,
    LLMCredentialRef,
    LLMRuntimeAccessContext,
    LLMRuntimeSelection,
    LLMRuntimeSelectionV2,
    ProviderSecret,
    RegisteredLLMOperationTarget,
    ResolvedAuth,
    ResolvedConnectionTarget,
    ResolvedLLMTarget,
)


class _CredentialService:
    """Minimal credential double; resolved targets carry request secrets."""

    def resolve_secret(self, *_args: Any, **_kwargs: Any) -> ProviderSecret:
        raise AssertionError("resolve_secret is not used by get_client after target resolution")


class _MinimalClient(LLMClient):
    """Concrete fake client returned by the factory boundary."""

    model = "factory-model"

    async def chat(self, system_prompt: str, user_prompt: str, **kwargs: Any) -> str:
        return "chat"

    async def chat_messages(self, messages: list[ChatMessage], **kwargs: Any) -> str:
        return "chat_messages"

    async def stream_chat_messages(
        self,
        messages: list[ChatMessage],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        yield "stream"

    async def chat_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        **kwargs: Any,
    ) -> LLMResponse:
        return LLMResponse(content="usage")

    async def chat_messages_with_usage(
        self,
        messages: list[ChatMessage],
        **kwargs: Any,
    ) -> LLMResponse:
        return LLMResponse(content="messages_usage")

    async def chat_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[Any],
        tool_choice: Any = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        return ToolCallResult(content="tools", tool_calls=None, raw=None)

    async def chat_with_tools_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[Any],
        tool_choice: Any = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        return ToolCallResult(content="tools_usage", tool_calls=None, raw=None)


def _operation_target(provider: str = "openai") -> RegisteredLLMOperationTarget:
    return RegisteredLLMOperationTarget(
        operation=LLMConnectionOperation.INFERENCE,
        provider=provider,
        method="POST",
        url=f"https://{provider}.example.test/v1/chat/completions",
        client_base_url=f"https://{provider}.example.test/v1",
        expected_host=f"{provider}.example.test",
        allowed_ports=frozenset({443}),
        allowed_path_prefixes=("/v1",),
    )


def _resolved_target(
    *,
    provider: str = "openai",
    connection_preset_id: str = "openai",
    exact_wire_model_id: str = "gpt-5.2",
    effective_model: str | None = "gpt-5.2",
    secret: ProviderSecret | None = None,
    dialect_policy_id: str = "openai_responses.native_v1",
) -> ResolvedLLMTarget:
    resolved_auth = (
        ResolvedAuth.with_secret(
            mode=LLMAuthMode.API_KEY,
            provider=provider,
            secret=secret or ProviderSecret(provider=provider, value="sk-builder-secret"),
        )
        if secret is not None or provider != "none"
        else ResolvedAuth.none()
    )
    return ResolvedLLMTarget(
        connection=ResolvedConnectionTarget(
            connection_id=str(uuid4()),
            connection_revision=3,
            connection_preset_id=connection_preset_id,
            runtime_family_id=f"{provider}_native",
            serving_operator_id=provider,
            transport_origin="backend",
            endpoint_policy_id="fixed_provider",
            endpoint=f"https://{provider}.example.test/v1/chat/completions",
            operation_target=_operation_target(provider),
            resolved_auth=resolved_auth,
        ),
        deployment_id=str(uuid4()),
        deployment_revision=7,
        route_id=str(uuid4()),
        adapter_id=f"{provider}_responses",
        adapter_version="1",
        api_surface="responses",
        dialect_policy_id=dialect_policy_id,
        canonical_model_id=effective_model,
        exact_wire_model_id=exact_wire_model_id,
        effective_profile=(
            require_model_profile(ProviderModelRef(provider, effective_model))
            if effective_model is not None
            else None
        ),
    )


def test_legacy_reasoning_validation_precedes_trusted_context_and_target_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy capability failures occur before trusted context normalization."""

    resolver = LLMRuntimeClientResolver(_CredentialService())
    selection = LLMRuntimeSelection(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        credential_ref=LLMCredentialRef(user_id=7, provider="anthropic"),
        reasoning_effort="medium",
    )

    def fail_resolve_target(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("target resolution should not run before legacy reasoning validation")

    monkeypatch.setattr(resolver, "resolve_target", fail_resolve_target)

    with pytest.raises(LLMCapabilityNotSupportedError, match="reasoning_effort"):
        resolver.get_client(
            selection,
            access_context={"runtime_user_id": 7},  # type: ignore[arg-type]
            purpose="legacy-reasoning",
        )


def test_v2_reasoning_validation_uses_effective_profile_after_target_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V2 reasoning validation waits for the resolved effective profile."""

    resolver = LLMRuntimeClientResolver(_CredentialService())
    selection = LLMRuntimeSelectionV2(
        deployment_ref=DeploymentRef(str(uuid4()), 1),
        reasoning_effort="medium",
    )
    events: list[str] = []

    def fake_resolve_target(*_args: Any, **_kwargs: Any) -> ResolvedLLMTarget:
        events.append("resolve_target")
        return _resolved_target(
            provider="anthropic",
            connection_preset_id="anthropic",
            exact_wire_model_id="claude-haiku-4-5-20251001",
            effective_model="claude-haiku-4-5-20251001",
            secret=ProviderSecret(provider="anthropic", value="sk-v2-secret"),
            dialect_policy_id="anthropic_messages.native_v1",
        )

    def fail_factory(*_args: Any, **_kwargs: Any) -> None:
        events.append("factory")
        raise AssertionError("factory should not run after V2 reasoning validation failure")

    monkeypatch.setattr(resolver, "resolve_target", fake_resolve_target)
    monkeypatch.setattr(builder_module.LLMClientFactory, "get_client", fail_factory)

    with pytest.raises(LLMCapabilityNotSupportedError, match="reasoning_effort"):
        resolver.get_client(
            selection,
            access_context=LLMRuntimeAccessContext(runtime_user_id=7),
            purpose="v2-reasoning",
        )

    assert events == ["resolve_target"]


def test_v2_factory_arguments_and_wrapper_are_field_for_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Current construction sends exact non-secret target fields to the factory."""

    resolver = LLMRuntimeClientResolver(_CredentialService())
    target = _resolved_target(
        exact_wire_model_id="Org/Model-Case:Exact",
        secret=ProviderSecret(provider="openai", value="sk-factory-secret"),
    )
    calls: list[dict[str, Any]] = []
    fake_client = _MinimalClient()

    monkeypatch.setattr(resolver, "resolve_target", lambda *_args, **_kwargs: target)

    def fake_get_client(*, provider_model: ProviderModelRef, api_key: str, **kwargs: Any) -> _MinimalClient:
        calls.append(
            {
                "provider_model": provider_model,
                "api_key": api_key,
                "kwargs": dict(kwargs),
            }
        )
        return fake_client

    monkeypatch.setattr(builder_module.LLMClientFactory, "get_client", fake_get_client)

    client = resolver.get_client(
        LLMRuntimeSelectionV2(deployment_ref=DeploymentRef(str(uuid4()), 1)),
        access_context=LLMRuntimeAccessContext(runtime_user_id=7),
        target=LLMCallTarget(
            provider="openai",
            model="Org/Model-Case:Exact",
            reasoning_effort="HIGH",
            role="planner",
        ),
        purpose="factory",
        temperature=0.2,
        extra_option={"stable": True},
    )

    assert isinstance(client, BudgetEnforcingLLMClient)
    assert calls[0]["provider_model"] == ProviderModelRef("openai", "Org/Model-Case:Exact")
    assert calls[0]["api_key"] == target.connection.resolved_auth.secret.value
    kwargs = calls[0]["kwargs"]
    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["temperature"] == 0.2
    assert kwargs["extra_option"] == {"stable": True}
    assert kwargs["model_profile"] is target.effective_profile
    assert kwargs["base_url"] == target.connection.operation_target.client_base_url
    assert kwargs["wire_model_id"] == "Org/Model-Case:Exact"
    assert kwargs["dialect_policy_id"] == "openai_responses.native_v1"
    assert kwargs["inference_transport"].__class__.__name__ == (
        "GuardedAsyncInferenceTransport"
    )


def test_legacy_matching_profile_omits_model_profile_and_profileless_target_is_unwrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy construction currently omits matching profiles and skips profile-less wrapping."""

    resolver = LLMRuntimeClientResolver(_CredentialService())
    selection = LLMRuntimeSelection(
        provider="openai",
        model="gpt-5.2",
        credential_ref=LLMCredentialRef(user_id=7, provider="openai"),
    )
    calls: list[dict[str, Any]] = []
    fake_client = _MinimalClient()

    def fake_get_client(*, provider_model: ProviderModelRef, api_key: str, **kwargs: Any) -> _MinimalClient:
        calls.append({"provider_model": provider_model, "api_key": api_key, "kwargs": dict(kwargs)})
        return fake_client

    monkeypatch.setattr(builder_module.LLMClientFactory, "get_client", fake_get_client)
    monkeypatch.setattr(
        resolver,
        "resolve_target",
        lambda *_args, **_kwargs: _resolved_target(
            exact_wire_model_id="gpt-5.2",
            effective_model="gpt-5.2",
            secret=ProviderSecret(provider="openai", value="sk-legacy-secret"),
        ),
    )

    wrapped = resolver.get_client(
        selection,
        access_context=LLMRuntimeAccessContext(runtime_user_id=7),
        purpose="legacy-profile",
    )

    assert isinstance(wrapped, BudgetEnforcingLLMClient)
    assert "model_profile" not in calls[-1]["kwargs"]

    monkeypatch.setattr(
        resolver,
        "resolve_target",
        lambda *_args, **_kwargs: _resolved_target(
            exact_wire_model_id="detached-model",
            effective_model=None,
            secret=ProviderSecret(provider="openai", value="sk-profileless-secret"),
        ),
    )

    unwrapped = resolver.get_client(
        selection,
        access_context=LLMRuntimeAccessContext(runtime_user_id=7),
        purpose="legacy-profileless",
    )

    assert unwrapped is fake_client
    assert "model_profile" not in calls[-1]["kwargs"]


def test_missing_secret_fails_before_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unauthenticated resolved auth is rejected before adapter construction."""

    resolver = LLMRuntimeClientResolver(_CredentialService())
    target = _resolved_target(secret=ProviderSecret(provider="openai", value="sk-unused"))
    target = ResolvedLLMTarget(
        connection=ResolvedConnectionTarget(
            connection_id=target.connection.connection_id,
            connection_revision=target.connection.connection_revision,
            connection_preset_id=target.connection.connection_preset_id,
            runtime_family_id=target.connection.runtime_family_id,
            serving_operator_id=target.connection.serving_operator_id,
            transport_origin=target.connection.transport_origin,
            endpoint_policy_id=target.connection.endpoint_policy_id,
            endpoint=target.connection.endpoint,
            operation_target=target.connection.operation_target,
            resolved_auth=ResolvedAuth.none(),
        ),
        deployment_id=target.deployment_id,
        deployment_revision=target.deployment_revision,
        route_id=target.route_id,
        adapter_id=target.adapter_id,
        adapter_version=target.adapter_version,
        api_surface=target.api_surface,
        dialect_policy_id=target.dialect_policy_id,
        canonical_model_id=target.canonical_model_id,
        exact_wire_model_id=target.exact_wire_model_id,
        effective_profile=target.effective_profile,
    )
    factory_called = False

    def fake_get_client(*_args: Any, **_kwargs: Any) -> _MinimalClient:
        nonlocal factory_called
        factory_called = True
        return _MinimalClient()

    monkeypatch.setattr(resolver, "resolve_target", lambda *_args, **_kwargs: target)
    monkeypatch.setattr(builder_module.LLMClientFactory, "get_client", fake_get_client)

    with pytest.raises(LLMConfigurationError, match="unauthenticated construction"):
        resolver.get_client(
            LLMRuntimeSelectionV2(deployment_ref=DeploymentRef(str(uuid4()), 1)),
            access_context=LLMRuntimeAccessContext(runtime_user_id=7),
            purpose="missing-secret",
        )

    assert factory_called is False


@pytest.mark.asyncio
async def test_guarded_transport_binds_registered_target_and_sanitized_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async transport binds only the approved operation target and secret object."""

    resolver = LLMRuntimeClientResolver(_CredentialService())
    secret = ProviderSecret(provider="openai", value="sk-guarded-secret")
    target = _resolved_target(secret=secret)
    construction_calls: list[dict[str, Any]] = []

    class _GuardedTransport:
        def __init__(self, **kwargs: Any) -> None:
            construction_calls.append(dict(kwargs))

        async def request_json(self, _json_body: Any) -> Any:
            return {"ok": True}

    monkeypatch.setattr(resolver, "resolve_target", lambda *_args, **_kwargs: target)
    monkeypatch.setattr(builder_module, "GuardedAsyncInferenceTransport", _GuardedTransport)

    captured_transports: list[Any] = []

    def fake_get_client(*, provider_model: ProviderModelRef, api_key: str, **kwargs: Any) -> _MinimalClient:
        captured_transports.append(kwargs["inference_transport"])
        return _MinimalClient()

    monkeypatch.setattr(builder_module.LLMClientFactory, "get_client", fake_get_client)

    resolver.get_client(
        LLMRuntimeSelectionV2(deployment_ref=DeploymentRef(str(uuid4()), 1)),
        access_context=LLMRuntimeAccessContext(runtime_user_id=7),
        purpose="guarded",
    )
    body = {"model": "gpt-5.2", "messages": [{"role": "user", "content": "hi"}]}

    assert await captured_transports[0].request_json(body) == {"ok": True}
    assert construction_calls == [
        {
            "secret": secret,
            "operation_target": target.connection.operation_target,
        }
    ]

    class _FailingGuardedTransport:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def request_json(self, _json_body: Any) -> Any:
            raise LLMConfigurationError("guarded transport rejected request")

    monkeypatch.setattr(
        builder_module,
        "GuardedAsyncInferenceTransport",
        _FailingGuardedTransport,
    )
    monkeypatch.setattr(builder_module.LLMClientFactory, "get_client", fake_get_client)
    captured_transports.clear()
    resolver.get_client(
        LLMRuntimeSelectionV2(deployment_ref=DeploymentRef(str(uuid4()), 1)),
        access_context=LLMRuntimeAccessContext(runtime_user_id=7),
        purpose="guarded-failure",
    )

    with pytest.raises(LLMConfigurationError, match="guarded transport rejected request") as exc_info:
        await captured_transports[0].request_json(body)
    assert secret.value not in str(exc_info.value)
