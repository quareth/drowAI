"""Unit tests for role-based model resolver behavior."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from backend.services.langgraph_chat.model_role_registry import (
    ROLE_CONVERSATION_MAIN,
    ROLE_INTENT_CLASSIFIER,
    ROLE_POST_TOOL_OBSERVATION,
    ROLE_POST_TOOL_ARTICULATOR,
    ROLE_REASONING_MAIN,
    ROLE_TOOL_CATEGORY_SELECTOR,
    ROLE_TOOL_OUTPUT_COMPRESSOR,
    ModelRoleRegistry,
    validate_reasoning_effort_for_model,
)
from core.llm.role_contracts import ROLE_CONTEXT_COMPRESSOR


def test_role_policy_import_does_not_eagerly_import_agent_package() -> None:
    """Backend/core role policy imports must not execute agent package init."""
    project_root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import core.llm.role_policy; "
                "print('agent' in sys.modules); "
                "print('agent.state.state_manager' in sys.modules)"
            ),
        ],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == ["False", "False"]


def test_reasoning_validation_does_not_import_agent_state_runtime() -> None:
    """Profile-backed validation may load provider modules, but not agent runtime."""
    project_root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from core.llm.role_policy import validate_reasoning_effort_for_model; "
                "validate_reasoning_effort_for_model(effort='minimal', model='gpt-5.2'); "
                "print('agent' in sys.modules); "
                "print('agent.state.state_manager' in sys.modules)"
            ),
        ],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == ["True", "False"]


def test_frontend_roles_resolve_deterministically() -> None:
    registry = ModelRoleRegistry(conversation_main_default="gpt-default")

    assert (
        registry.resolve(ROLE_CONVERSATION_MAIN, conversation_model="gpt-5.2")
        == "gpt-5.2"
    )
    assert (
        registry.resolve(
            ROLE_REASONING_MAIN,
            conversation_model="gpt-5.2",
        )
        == "gpt-5.2"
    )
    assert registry.resolve(ROLE_CONVERSATION_MAIN) == "gpt-default"


def test_internal_roles_inherit_selected_openai_model_with_low_reasoning() -> None:
    registry = ModelRoleRegistry()

    output = registry.resolve_call_settings(
        ROLE_TOOL_OUTPUT_COMPRESSOR,
        conversation_provider="openai",
        conversation_model="gpt-5.2",
    )
    category = registry.resolve_call_settings(
        ROLE_TOOL_CATEGORY_SELECTOR,
        conversation_provider="openai",
        conversation_model="gpt-5.2",
    )

    for settings in (output, category):
        assert settings.provider == "openai"
        assert settings.model == "gpt-5.2"
        assert settings.reasoning_effort == "low"
        assert settings.source == "user_selected"


def test_internal_role_uses_low_when_selected_model_omits_minimal() -> None:
    settings = ModelRoleRegistry().resolve_call_settings(
        ROLE_POST_TOOL_ARTICULATOR,
        conversation_provider="openai",
        conversation_model="gpt-5.6-sol",
    )

    assert settings.model == "gpt-5.6-sol"
    assert settings.reasoning_effort == "low"


def test_internal_role_prefers_supported_lower_effort_before_escalating() -> None:
    settings = ModelRoleRegistry(
        internal_reasoning_default="minimal"
    ).resolve_call_settings(
        ROLE_POST_TOOL_ARTICULATOR,
        conversation_provider="openai",
        conversation_model="gpt-5.6-sol",
    )

    assert settings.reasoning_effort == "none"


def test_internal_roles_inherit_selected_anthropic_model_with_low_reasoning() -> None:
    registry = ModelRoleRegistry()

    output = registry.resolve_call_settings(
        ROLE_TOOL_OUTPUT_COMPRESSOR,
        conversation_model="claude-sonnet-4-6",
        conversation_provider="anthropic",
    )
    category = registry.resolve_call_settings(
        ROLE_TOOL_CATEGORY_SELECTOR,
        conversation_model="claude-sonnet-4-6",
        conversation_provider="anthropic",
    )

    for settings in (output, category):
        assert settings.provider == "anthropic"
        assert settings.model == "claude-sonnet-4-6"
        assert settings.reasoning_effort == "low"
        assert settings.source == "user_selected"


@pytest.mark.parametrize(
    "role",
    (
        ROLE_CONVERSATION_MAIN,
        ROLE_REASONING_MAIN,
        ROLE_POST_TOOL_OBSERVATION,
        ROLE_POST_TOOL_ARTICULATOR,
        ROLE_INTENT_CLASSIFIER,
        ROLE_CONTEXT_COMPRESSOR,
        ROLE_TOOL_OUTPUT_COMPRESSOR,
        ROLE_TOOL_CATEGORY_SELECTOR,
    ),
)
def test_gpt_oss_uses_selected_model_for_every_role(role: str) -> None:
    """Open-model agent roles never escape to provider-owned internal models."""

    settings = ModelRoleRegistry().resolve_call_settings(
        role,
        conversation_provider="openai",
        conversation_model="gpt-oss-20b",
        reasoning_effort=None,
    )

    assert settings.provider == "openai"
    assert settings.model == "gpt-oss-20b"
    assert settings.reasoning_effort is None
    assert settings.source == "user_selected"


def test_context_compressor_inherits_conversation_target_through_role_policy() -> None:
    """Context compression uses the canonical explicit inheritance rule."""

    settings = ModelRoleRegistry().resolve_call_settings(
        ROLE_CONTEXT_COMPRESSOR,
        conversation_model="gpt-5.2",
        conversation_provider="openai",
    )

    assert settings.provider == "openai"
    assert settings.model == "gpt-5.2"
    assert settings.reasoning_effort is None
    assert settings.source == "user_selected"


def test_internal_role_omits_effort_when_selected_model_is_not_reasoning_capable() -> None:
    settings = ModelRoleRegistry().resolve_call_settings(
        ROLE_POST_TOOL_ARTICULATOR,
        conversation_provider="anthropic",
        conversation_model="claude-haiku-4-5-20251001",
    )

    assert settings.model == "claude-haiku-4-5-20251001"
    assert settings.reasoning_effort is None


def test_intent_classifier_follows_user_selected_model() -> None:
    registry = ModelRoleRegistry(conversation_main_default="gpt-default")

    user_picked = registry.resolve_call_settings(
        ROLE_INTENT_CLASSIFIER,
        conversation_model="gpt-5.2",
        conversation_provider="openai",
    )
    assert user_picked.model == "gpt-5.2"
    assert user_picked.provider == "openai"
    assert user_picked.source == "user_selected"

    fallback = registry.resolve_call_settings(ROLE_INTENT_CLASSIFIER)
    assert fallback.model == "gpt-default"
    assert fallback.source == "user_selected"


def test_resolve_call_settings_returns_model_effort_and_source() -> None:
    registry = ModelRoleRegistry(conversation_main_default="gpt-default")

    user_settings = registry.resolve_call_settings(
        ROLE_REASONING_MAIN,
        conversation_model="gpt-5.2",
        conversation_provider="openai",
        reasoning_effort="high",
    )
    assert user_settings.provider == "openai"
    assert user_settings.model == "gpt-5.2"
    assert user_settings.reasoning_effort == "high"
    assert user_settings.source == "user_selected"

    internal_settings = registry.resolve_call_settings(
        ROLE_TOOL_OUTPUT_COMPRESSOR,
        conversation_model="gpt-5.2",
        conversation_provider="openai",
        reasoning_effort="xhigh",
    )
    assert internal_settings.model == "gpt-5.2"
    assert internal_settings.provider == "openai"
    assert internal_settings.reasoning_effort == "low"
    assert internal_settings.source == "user_selected"


def test_post_tool_observation_uses_user_selected_role_path() -> None:
    registry = ModelRoleRegistry(conversation_main_default="gpt-default")

    role_settings = registry.resolve_call_settings(
        ROLE_POST_TOOL_OBSERVATION,
        conversation_model="claude-sonnet-4-6",
        conversation_provider="anthropic",
    )
    assert role_settings.provider == "anthropic"
    assert role_settings.model == "claude-sonnet-4-6"
    assert role_settings.reasoning_effort == "high"
    assert role_settings.source == "user_selected"


def test_anthropic_user_selected_role_uses_exact_profile_default() -> None:
    registry = ModelRoleRegistry(conversation_main_default="gpt-default")

    role_settings = registry.resolve_call_settings(
        ROLE_CONVERSATION_MAIN,
        conversation_model="claude-sonnet-4-6",
        conversation_provider="anthropic",
    )

    assert role_settings.provider == "anthropic"
    assert role_settings.model == "claude-sonnet-4-6"
    assert role_settings.reasoning_effort == "high"
    assert role_settings.source == "user_selected"


def test_anthropic_user_selected_role_accepts_fable_xhigh() -> None:
    registry = ModelRoleRegistry(conversation_main_default="gpt-default")

    role_settings = registry.resolve_call_settings(
        ROLE_CONVERSATION_MAIN,
        conversation_model="claude-fable-5",
        conversation_provider="anthropic",
        reasoning_effort="xhigh",
    )

    assert role_settings.model == "claude-fable-5"
    assert role_settings.reasoning_effort == "xhigh"


def test_openai_chat_completion_role_omits_implicit_reasoning_effort() -> None:
    registry = ModelRoleRegistry(conversation_main_default="gpt-default")

    role_settings = registry.resolve_call_settings(
        ROLE_CONVERSATION_MAIN,
        conversation_model="gpt-4o-mini",
        conversation_provider="openai",
    )

    assert role_settings.provider == "openai"
    assert role_settings.model == "gpt-4o-mini"
    assert role_settings.reasoning_effort is None
    assert role_settings.source == "user_selected"


def test_post_tool_articulator_inherits_selected_model_with_low_effort() -> None:
    registry = ModelRoleRegistry(conversation_main_default="gpt-default")

    role_settings = registry.resolve_call_settings(
        ROLE_POST_TOOL_ARTICULATOR,
        conversation_model="claude-sonnet-4-6",
        conversation_provider="anthropic",
    )
    assert role_settings.provider == "anthropic"
    assert role_settings.model == "claude-sonnet-4-6"
    assert role_settings.reasoning_effort == "low"
    assert role_settings.source == "user_selected"


def test_validate_reasoning_effort_rejects_unknown_values() -> None:
    try:
        validate_reasoning_effort_for_model(
            effort="invalid-effort",
            model="gpt-5.2",
        )
    except ValueError as exc:
        assert "Allowed values" in str(exc)
        return
    raise AssertionError("Expected ValueError for unsupported reasoning effort")


def test_validate_reasoning_effort_rejects_xhigh_for_non_pro_model() -> None:
    try:
        validate_reasoning_effort_for_model(
            effort="xhigh",
            model="gpt-5.2",
        )
    except ValueError as exc:
        assert "models that support xhigh" in str(exc)
        return
    raise AssertionError("Expected ValueError for invalid xhigh/model combination")


def test_validate_reasoning_effort_coerces_minimal_to_none_for_gpt52() -> None:
    result = validate_reasoning_effort_for_model(
        effort="minimal",
        model="gpt-5.2",
    )
    assert result == "none"


def test_validate_reasoning_effort_preserves_legacy_variant_minimal_coercion() -> None:
    result = validate_reasoning_effort_for_model(
        effort="minimal",
        model="gpt-5.2-preview",
    )
    assert result == "medium"


def test_validate_reasoning_effort_accepts_provider_argument_for_profile_lookup() -> None:
    result = validate_reasoning_effort_for_model(
        effort="xhigh",
        provider="openai",
        model="gpt-5.2-pro",
    )
    assert result == "xhigh"


def test_validate_reasoning_effort_uses_exact_gpt56_profile() -> None:
    assert validate_reasoning_effort_for_model(
        effort="max",
        provider="openai",
        model="gpt-5.6-sol",
    ) == "max"
    with pytest.raises(ValueError, match="Allowed values"):
        validate_reasoning_effort_for_model(
            effort="minimal",
            provider="openai",
            model="gpt-5.6-sol",
        )


def test_validate_reasoning_effort_rejects_anthropic_without_profile_support() -> None:
    with pytest.raises(ValueError, match="not supported for provider 'anthropic'"):
        validate_reasoning_effort_for_model(
            effort="medium",
            provider="anthropic",
            model="claude-haiku-4-5-20251001",
        )


def test_validate_reasoning_effort_rejects_openai_chat_profile_without_support() -> None:
    with pytest.raises(ValueError, match="not supported for provider 'openai'"):
        validate_reasoning_effort_for_model(
            effort="medium",
            provider="openai",
            model="gpt-4o-mini",
        )


def test_role_effort_scenario_matrix() -> None:
    """All roles share one selected model while lightweight roles use low effort."""

    registry = ModelRoleRegistry(conversation_main_default="gpt-5.2")

    conversation = registry.resolve_call_settings(
        ROLE_CONVERSATION_MAIN,
        conversation_model="gpt-5.2",
        conversation_provider="openai",
        reasoning_effort="low",
    )
    assert conversation.provider == "openai"
    assert conversation.model == "gpt-5.2"
    assert conversation.reasoning_effort == "low"
    assert conversation.source == "user_selected"

    reasoning = registry.resolve_call_settings(
        ROLE_REASONING_MAIN,
        conversation_model="gpt-5.2",
        conversation_provider="openai",
        reasoning_effort="high",
    )
    assert reasoning.provider == "openai"
    assert reasoning.model == "gpt-5.2"
    assert reasoning.reasoning_effort == "high"
    assert reasoning.source == "user_selected"

    post_tool = registry.resolve_call_settings(
        ROLE_POST_TOOL_OBSERVATION,
        conversation_model="gpt-5.2-pro",
        conversation_provider="openai",
        reasoning_effort="xhigh",
    )
    assert post_tool.provider == "openai"
    assert post_tool.model == "gpt-5.2-pro"
    assert post_tool.reasoning_effort == "xhigh"
    assert post_tool.source == "user_selected"

    intent = registry.resolve_call_settings(
        ROLE_INTENT_CLASSIFIER,
        conversation_model="gpt-5.2-pro",
        conversation_provider="openai",
        reasoning_effort="xhigh",
    )
    assert intent.provider == "openai"
    assert intent.model == "gpt-5.2-pro"
    assert intent.reasoning_effort == "xhigh"
    assert intent.source == "user_selected"

    for internal_role in (
        ROLE_TOOL_OUTPUT_COMPRESSOR,
        ROLE_TOOL_CATEGORY_SELECTOR,
        ROLE_POST_TOOL_ARTICULATOR,
    ):
        internal = registry.resolve_call_settings(
            internal_role,
            conversation_model="claude-sonnet-4-6",
            conversation_provider="anthropic",
            reasoning_effort="xhigh",
        )
        assert internal.provider == "anthropic"
        assert internal.model == "claude-sonnet-4-6"
        assert internal.reasoning_effort == "low"
        assert internal.source == "user_selected"
