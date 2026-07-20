"""Direct tests for the canonical runtime client builder boundary.

Scope: prove factory argument assembly, reasoning validation, guarded callback
adaptation, secret failure timing, and budget wrapping through
``runtime_client_builder`` after facade cutover.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator
from uuid import uuid4

import pytest

from agent.providers.llm.core.base import ChatMessage, LLMClient, LLMResponse, ToolCallResult
from agent.providers.llm.core.budget_enforcing_client import BudgetEnforcingLLMClient
from agent.providers.llm.core.exceptions import (
    LLMCapabilityNotSupportedError,
    LLMConfigurationError,
)
from agent.providers.llm.core.identity import ProviderModelRef
from agent.providers.llm.profiles.registry import require_model_profile
from backend.services.llm_provider import runtime_client_builder as builder_module
from backend.services.llm_provider.runtime_client_builder import LLMRuntimeClientBuilder
from backend.services.llm_provider.types import (
    DeploymentRef,
    LLMAuthMode,
    LLMCallTarget,
    LLMConnectionOperation,
    LLMCredentialRef,
    LLMRuntimeSelection,
    LLMRuntimeSelectionV2,
    ProviderSecret,
    RegisteredLLMOperationTarget,
    ResolvedAuth,
    ResolvedConnectionTarget,
    ResolvedLLMTarget,
)


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
    auth_none: bool = False,
    dialect_policy_id: str = "openai_responses.native_v1",
) -> ResolvedLLMTarget:
    resolved_auth = (
        ResolvedAuth.none()
        if auth_none
        else ResolvedAuth.with_secret(
            mode=LLMAuthMode.API_KEY,
            provider=provider,
            secret=secret or ProviderSecret(provider=provider, value="sk-builder-secret"),
        )
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


def test_builder_import_boundary_excludes_facade_and_persistence() -> None:
    """The extraction owns construction without depending on resolver or models."""

    source = Path(builder_module.__file__).read_text()

    assert "runtime_client_resolver" not in source
    assert "backend.models" not in source
    assert "sqlalchemy" not in source


def test_builder_reasoning_validation_matches_current_policy() -> None:
    """Reasoning normalization and unsupported-model errors match baseline."""

    builder = LLMRuntimeClientBuilder()

    assert (
        builder.resolve_supported_reasoning_effort(
            ProviderModelRef("openai", "gpt-5.2"),
            " HIGH ",
        )
        == "high"
    )

    with pytest.raises(LLMCapabilityNotSupportedError, match="reasoning_effort"):
        builder.resolve_supported_reasoning_effort(
            ProviderModelRef("anthropic", "claude-haiku-4-5-20251001"),
            "medium",
        )


def test_builder_factory_arguments_and_budget_wrapper_are_field_for_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct builder construction sends exact non-secret target fields to the factory."""

    builder = LLMRuntimeClientBuilder()
    target = _resolved_target(
        exact_wire_model_id="Org/Model-Case:Exact",
        secret=ProviderSecret(provider="openai", value="sk-factory-secret"),
    )
    calls: list[dict[str, Any]] = []
    fake_client = _MinimalClient()

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

    client = builder.build(
        selection=LLMRuntimeSelectionV2(deployment_ref=DeploymentRef(str(uuid4()), 1)),
        resolved_target=target,
        target=LLMCallTarget(
            provider="openai",
            model="Org/Model-Case:Exact",
            reasoning_effort="HIGH",
            role="planner",
        ),
        legacy_call_ref=None,
        legacy_reasoning_effort=None,
        reasoning_effort="HIGH",
        reasoning_effort_was_explicit=False,
        client_kwargs={"temperature": 0.2, "extra_option": {"stable": True}},
    )

    assert isinstance(client, BudgetEnforcingLLMClient)
    assert getattr(client, "_role") == "planner"
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
    assert callable(kwargs["guarded_executor"])


def test_builder_legacy_profile_rules_and_profileless_wrapper_condition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy matching profiles omit model_profile and profile-less targets stay unwrapped."""

    builder = LLMRuntimeClientBuilder()
    selection = LLMRuntimeSelection(
        provider="openai",
        model="gpt-5.2",
        credential_ref=LLMCredentialRef(user_id=7, provider="openai"),
    )
    legacy_call_ref = ProviderModelRef("openai", "gpt-5.2")
    calls: list[dict[str, Any]] = []
    fake_client = _MinimalClient()

    def fake_get_client(*, provider_model: ProviderModelRef, api_key: str, **kwargs: Any) -> _MinimalClient:
        calls.append({"provider_model": provider_model, "api_key": api_key, "kwargs": dict(kwargs)})
        return fake_client

    monkeypatch.setattr(builder_module.LLMClientFactory, "get_client", fake_get_client)

    wrapped = builder.build(
        selection=selection,
        resolved_target=_resolved_target(
            exact_wire_model_id="gpt-5.2",
            effective_model="gpt-5.2",
            secret=ProviderSecret(provider="openai", value="sk-legacy-secret"),
        ),
        target=None,
        legacy_call_ref=legacy_call_ref,
        legacy_reasoning_effort=None,
        reasoning_effort=None,
        reasoning_effort_was_explicit=False,
        client_kwargs={},
    )

    assert isinstance(wrapped, BudgetEnforcingLLMClient)
    assert "model_profile" not in calls[-1]["kwargs"]

    unwrapped = builder.build(
        selection=selection,
        resolved_target=_resolved_target(
            exact_wire_model_id="detached-model",
            effective_model=None,
            secret=ProviderSecret(provider="openai", value="sk-profileless-secret"),
        ),
        target=None,
        legacy_call_ref=legacy_call_ref,
        legacy_reasoning_effort=None,
        reasoning_effort=None,
        reasoning_effort_was_explicit=False,
        client_kwargs={},
    )

    assert unwrapped is fake_client
    assert "model_profile" not in calls[-1]["kwargs"]


def test_builder_v2_profile_and_secret_failures_precede_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V2 profile-less and unauthenticated targets fail before adapter construction."""

    builder = LLMRuntimeClientBuilder()
    factory_called = False

    def fake_get_client(*_args: Any, **_kwargs: Any) -> _MinimalClient:
        nonlocal factory_called
        factory_called = True
        return _MinimalClient()

    monkeypatch.setattr(builder_module.LLMClientFactory, "get_client", fake_get_client)
    selection = LLMRuntimeSelectionV2(deployment_ref=DeploymentRef(str(uuid4()), 1))

    with pytest.raises(LLMConfigurationError, match="effective profile is unavailable"):
        builder.build(
            selection=selection,
            resolved_target=_resolved_target(effective_model=None),
            target=None,
            legacy_call_ref=None,
            legacy_reasoning_effort=None,
            reasoning_effort=None,
            reasoning_effort_was_explicit=False,
            client_kwargs={},
        )
    assert factory_called is False

    with pytest.raises(LLMConfigurationError, match="unauthenticated construction"):
        builder.build(
            selection=selection,
            resolved_target=_resolved_target(auth_none=True),
            target=None,
            legacy_call_ref=None,
            legacy_reasoning_effort=None,
            reasoning_effort=None,
            reasoning_effort_was_explicit=False,
            client_kwargs={},
        )
    assert factory_called is False


def test_builder_guarded_executor_uses_registered_operation_and_sanitized_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guarded callback forwards only the approved operation target and secret object."""

    builder = LLMRuntimeClientBuilder()
    secret = ProviderSecret(provider="openai", value="sk-guarded-secret")
    target = _resolved_target(secret=secret)
    execute_calls: list[dict[str, Any]] = []

    class _GuardedTransport:
        def execute(self, operation: Any, **kwargs: Any) -> SimpleNamespace:
            execute_calls.append({"operation": operation, **kwargs})
            return SimpleNamespace(body=b'{"ok":true}')

    monkeypatch.setattr(builder_module, "GuardedTransport", _GuardedTransport)
    captured_executor: list[Any] = []

    def fake_get_client(*, provider_model: ProviderModelRef, api_key: str, **kwargs: Any) -> _MinimalClient:
        captured_executor.append(kwargs["guarded_executor"])
        return _MinimalClient()

    monkeypatch.setattr(builder_module.LLMClientFactory, "get_client", fake_get_client)

    builder.build(
        selection=LLMRuntimeSelectionV2(deployment_ref=DeploymentRef(str(uuid4()), 1)),
        resolved_target=target,
        target=None,
        legacy_call_ref=None,
        legacy_reasoning_effort=None,
        reasoning_effort=None,
        reasoning_effort_was_explicit=False,
        client_kwargs={},
    )
    body = {"model": "gpt-5.2", "messages": [{"role": "user", "content": "hi"}]}

    assert captured_executor[0](body) == b'{"ok":true}'
    assert execute_calls == [
        {
            "operation": LLMConnectionOperation.INFERENCE,
            "provider": "openai",
            "secret": secret,
            "json_body": body,
            "operation_target": target.connection.operation_target,
        }
    ]

    class _FailingGuardedTransport:
        def execute(self, *_args: Any, **_kwargs: Any) -> None:
            raise LLMConfigurationError("guarded transport rejected request")

    monkeypatch.setattr(builder_module, "GuardedTransport", _FailingGuardedTransport)
    captured_executor.clear()
    builder.build(
        selection=LLMRuntimeSelectionV2(deployment_ref=DeploymentRef(str(uuid4()), 1)),
        resolved_target=target,
        target=None,
        legacy_call_ref=None,
        legacy_reasoning_effort=None,
        reasoning_effort=None,
        reasoning_effort_was_explicit=False,
        client_kwargs={},
    )

    with pytest.raises(LLMConfigurationError, match="guarded transport rejected request") as exc_info:
        captured_executor[0](body)
    assert secret.value not in str(exc_info.value)
