"""Direct tests for the canonical runtime client builder boundary.

Scope: prove factory argument assembly, reasoning validation, guarded callback
adaptation, secret failure timing, and budget wrapping through
``runtime_client_builder`` after facade cutover.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agent.providers.llm.core.budget_enforcing_client import BudgetEnforcingLLMClient
from agent.providers.llm.core.exceptions import (
    LLMCapabilityNotSupportedError,
    LLMConfigurationError,
)
from agent.providers.llm.core.identity import ProviderModelRef
from agent.providers.llm.adapters.openai.compatible_dialects import (
    OPENAI_COMPATIBLE_CHAT_ADAPTER_ID,
)
from agent.providers.llm.profiles.registry import require_model_profile
from backend.services.llm_provider import runtime_client_builder as builder_module
from backend.services.llm_provider.runtime_client_builder import LLMRuntimeClientBuilder
from backend.services.llm_provider.types import (
    DeploymentRef,
    LLMAuthMode,
    LLMCallTarget,
    LLMCredentialRef,
    LLMRuntimeSelection,
    LLMRuntimeSelectionV2,
    ProviderSecret,
    ResolvedAuth,
    ResolvedConnectionTarget,
    ResolvedLLMTarget,
)
from backend.tests.services.llm_provider.runtime_client_test_support import (
    MinimalRuntimeClient as _MinimalClient,
    operation_target as _operation_target,
)


def _resolved_target(
    *,
    provider: str = "openai",
    connection_preset_id: str = "openai",
    exact_wire_model_id: str = "gpt-5.2",
    effective_model: str | None = "gpt-5.2",
    secret: ProviderSecret | None = None,
    auth_none: bool = False,
    adapter_id: str | None = None,
    api_surface: str = "responses",
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
        adapter_id=adapter_id or f"{provider}_responses",
        adapter_version="1",
        api_surface=api_surface,
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


def test_builder_forwards_explicit_compatible_adapter_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolved route adapter is forwarded to the provider-neutral factory."""

    builder = LLMRuntimeClientBuilder()
    calls: list[dict[str, Any]] = []

    def fake_get_client(
        *,
        provider_model: ProviderModelRef,
        api_key: str,
        **kwargs: Any,
    ) -> _MinimalClient:
        calls.append(
            {
                "provider_model": provider_model,
                "api_key": api_key,
                "kwargs": dict(kwargs),
            }
        )
        return _MinimalClient()

    monkeypatch.setattr(builder_module.LLMClientFactory, "get_client", fake_get_client)
    resolved_target = _resolved_target(
        effective_model="gpt-oss-20b",
        exact_wire_model_id="openai/gpt-oss-20b",
        adapter_id=OPENAI_COMPATIBLE_CHAT_ADAPTER_ID,
        api_surface="chat_completions",
        dialect_policy_id="openai_compatible_chat.agent_v1",
    )

    builder.build(
        selection=LLMRuntimeSelectionV2(
            deployment_ref=DeploymentRef(str(uuid4()), 1)
        ),
        resolved_target=resolved_target,
        target=None,
        legacy_call_ref=None,
        legacy_reasoning_effort=None,
        reasoning_effort=None,
        reasoning_effort_was_explicit=False,
        client_kwargs={},
    )

    assert calls[0]["kwargs"]["adapter_id"] == OPENAI_COMPATIBLE_CHAT_ADAPTER_ID


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
    assert kwargs["inference_transport"].__class__.__name__ == (
        "GuardedAsyncInferenceTransport"
    )


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


@pytest.mark.asyncio
async def test_builder_guarded_transport_binds_authorized_target_and_sanitized_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async transport binds only the approved operation target and secret object."""

    builder = LLMRuntimeClientBuilder()
    secret = ProviderSecret(provider="openai", value="sk-guarded-secret")
    target = _resolved_target(secret=secret)
    construction_calls: list[dict[str, Any]] = []

    class _GuardedTransport:
        def __init__(self, **kwargs: Any) -> None:
            construction_calls.append(dict(kwargs))

        async def request_json(self, _json_body: Any) -> Any:
            return {"ok": True}

    monkeypatch.setattr(builder_module, "GuardedAsyncInferenceTransport", _GuardedTransport)
    captured_transports: list[Any] = []

    def fake_get_client(*, provider_model: ProviderModelRef, api_key: str, **kwargs: Any) -> _MinimalClient:
        captured_transports.append(kwargs["inference_transport"])
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
    captured_transports.clear()
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
        await captured_transports[0].request_json(body)
    assert secret.value not in str(exc_info.value)
