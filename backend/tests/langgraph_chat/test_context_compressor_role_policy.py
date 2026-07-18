"""Tests for canonical context-compressor role policy and inheritance."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, get_args

import pytest

import backend.services.langgraph_chat.compression.context_service as context_module
from backend.services.langgraph_chat.compression.context_models import (
    CompressionPolicy,
    CompressionRequiredError,
    ContextCompressionRequest,
)
from backend.services.langgraph_chat.compression.context_service import (
    ContextCompressionService,
)
from core.llm.role_contracts import (
    ROLE_CONTEXT_COMPRESSOR,
    RoleCallSettings,
    RoleKey,
)
from core.llm.role_policy import ModelRoleRegistry
from core.llm.role_requirements import (
    CONVERSATION_INHERITED_ROLE_KEYS,
    get_role_requirements,
)


class _Manager:
    """Deterministic token manager for policy-path compression tests."""

    def estimate_tokens_from_history(
        self,
        *,
        history: list[dict[str, Any]],
        provider: str,
        model: str,
        projected_user_message: str | None = None,
    ) -> int:
        del provider, model
        if projected_user_message is not None:
            return 10
        if history and history[0].get("role") == "system":
            return 10
        if history:
            return len(str(history[0].get("content", "")))
        return 0


def _request(
    *,
    provider: str = "anthropic",
    model: str = "claude-sonnet-4-6",
) -> ContextCompressionRequest:
    """Build a request carrying the submitted conversation turn target."""

    return ContextCompressionRequest(
        task_id=1,
        conversation_id="conv-1",
        max_tokens=100,
        provider=provider,
        model=model,
        conversation_history=[{"role": "user", "content": "hello"}],
        policy=CompressionPolicy(
            trigger_percent=100,
            target_min_percent=20,
            target_max_percent=30,
        ),
    )


def test_context_compressor_is_canonical_with_capacity_requirements() -> None:
    """The role vocabulary and requirements declare compressor constraints."""

    requirements = get_role_requirements(ROLE_CONTEXT_COMPRESSOR)

    assert ROLE_CONTEXT_COMPRESSOR in get_args(RoleKey)
    assert ROLE_CONTEXT_COMPRESSOR in CONVERSATION_INHERITED_ROLE_KEYS
    assert requirements.required_capabilities == (
        "chat",
        "context_window",
        "max_output_tokens",
    )


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("openai", "gpt-5.2"),
        ("anthropic", "claude-sonnet-4-6"),
    ],
)
def test_role_policy_explicitly_inherits_exact_conversation_target(
    provider: str,
    model: str,
) -> None:
    """Compressor resolution returns the submitted target without substitution."""

    settings = ModelRoleRegistry().resolve_call_settings(
        ROLE_CONTEXT_COMPRESSOR,
        conversation_provider=provider,
        conversation_model=model,
    )

    assert settings == RoleCallSettings(
        provider=provider,
        model=model,
        reasoning_effort=None,
        source="user_selected",
    )


def test_role_policy_fails_closed_without_compatible_inherited_target() -> None:
    """Missing profiles cannot trigger an internal or global model fallback."""

    fallback_calls: list[tuple[str, str]] = []

    def _unexpected_fallback(provider: str, role: str) -> Any:
        fallback_calls.append((provider, role))
        raise AssertionError("context compressor must not use internal fallback")

    registry = ModelRoleRegistry(
        conversation_main_default="gpt-5.2",
        internal_model_resolver=_unexpected_fallback,  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="context_compressor"):
        registry.resolve_call_settings(
            ROLE_CONTEXT_COMPRESSOR,
            conversation_provider="custom",
            conversation_model="unregistered-model",
        )

    assert fallback_calls == []


def test_compression_service_obtains_settings_from_role_policy() -> None:
    """The service passes the submitted target through the canonical registry."""

    registry_calls: list[dict[str, Any]] = []
    compressor_calls: list[RoleCallSettings] = []

    class _Registry:
        def resolve_call_settings(self, role: str, **kwargs: Any) -> RoleCallSettings:
            registry_calls.append({"role": role, **kwargs})
            return RoleCallSettings(
                provider="anthropic",
                model="claude-sonnet-4-6",
                reasoning_effort=None,
                source="user_selected",
            )

    async def _compressor(
        _system_prompt: str,
        _user_prompt: str,
        call_settings: RoleCallSettings,
    ) -> str:
        compressor_calls.append(call_settings)
        return "x" * 25

    service = ContextCompressionService(
        compressor=_compressor,
        model_role_registry=_Registry(),  # type: ignore[arg-type]
        context_window_manager_factory=lambda _max_tokens: _Manager(),
    )

    outcome = asyncio.run(service.compress(_request()))

    assert outcome.pass_count == 1
    assert registry_calls == [
        {
            "role": ROLE_CONTEXT_COMPRESSOR,
            "conversation_provider": "anthropic",
            "conversation_model": "claude-sonnet-4-6",
        }
    ]
    assert compressor_calls == [
        RoleCallSettings(
            provider="anthropic",
            model="claude-sonnet-4-6",
            reasoning_effort=None,
            source="user_selected",
        )
    ]


def test_compression_uses_existing_required_failure_for_incompatible_target() -> None:
    """An invalid inherited target fails before invocation, with no fallback."""

    compressor_calls = 0

    async def _compressor(*_args: Any) -> str:
        nonlocal compressor_calls
        compressor_calls += 1
        return "x" * 25

    service = ContextCompressionService(
        compressor=_compressor,
        model_role_registry=ModelRoleRegistry(
            conversation_main_default="gpt-5.2"
        ),
        context_window_manager_factory=lambda _max_tokens: _Manager(),
    )

    with pytest.raises(CompressionRequiredError) as exc_info:
        asyncio.run(
            service.compress(
                _request(provider="custom", model="unregistered-model")
            )
        )

    assert exc_info.value.reason == "compressor_target_incompatible"
    assert compressor_calls == 0


def test_context_service_does_not_construct_role_settings_from_raw_target() -> None:
    """Compression has no service-local provider/model settings construction."""

    source = inspect.getsource(context_module)

    assert "ProviderModelRef(" not in source
    assert "RoleCallSettings(" not in source
