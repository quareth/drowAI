"""Contract tests for Mistral Small through the reviewed compatible route."""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.orm import Session

from agent.providers.llm.adapters.openai.chat import _final_text_content
from agent.providers.llm.adapters.openai.compatible_request_policies import (
    CompatibleRequestOptions,
    MISTRAL_SMALL_REQUEST_POLICY_ID,
    resolve_compatible_request_policy,
)
from agent.providers.llm.core.capabilities import LLMCapability
from agent.providers.llm.core.identity import ProviderModelRef
from agent.providers.llm.profiles.registry import require_model_profile
from backend.services.llm_provider.catalog_service import (
    LLMProviderCatalogService,
    _default_visible_reasoning_effort,
    _visible_reasoning_efforts,
)
from backend.models import User
from backend.services.llm_provider.catalog_projection_service import (
    LLMCatalogProjectionService,
)
from backend.services.llm_provider.credential_service import LLMCredentialService
from backend.services.llm_provider.effective_profile_service import (
    EffectiveProfileService,
)
from backend.services.llm_provider.operation_registry import (
    MISTRAL_OPENAI_COMPATIBLE_PRESET_ID,
    ConnectionOperationRegistry,
)
from backend.services.usage_tracking.pricing_registry import get_pricing_quote


def _mistral_effective_profile():
    preset = ConnectionOperationRegistry().get_connection_preset(
        MISTRAL_OPENAI_COMPATIBLE_PRESET_ID
    )
    return EffectiveProfileService().resolve(
        connection=SimpleNamespace(connection_preset_id=preset.id),
        deployment=SimpleNamespace(
            wire_model_id=preset.exact_wire_model_id,
            canonical_model_id=preset.canonical_model_id,
            display_name=preset.display_name,
            lifecycle_state="active",
        ),
        route=SimpleNamespace(
            adapter_id=preset.adapter_id,
            adapter_version=preset.adapter_version,
            api_surface=preset.api_surface,
            dialect_policy_id=preset.dialect_policy_id,
            route_config={
                "preset_id": preset.id,
                "request_policy_id": preset.request_policy_id,
            },
        ),
    )


def test_mistral_small_profile_is_route_effective() -> None:
    """The reviewed preset intersects the canonical model and route dialect."""

    canonical = require_model_profile(
        ProviderModelRef("mistral", "mistral-small-2603")
    )
    effective = _mistral_effective_profile()

    assert canonical.context_window_tokens == 256_000
    assert effective.ref == canonical.ref
    assert effective.reasoning_efforts == frozenset({"none", "high"})
    assert effective.default_reasoning_effort == "none"
    assert _visible_reasoning_efforts(effective) == ("none", "high")
    assert _default_visible_reasoning_effort(effective) == "none"
    assert effective.tool_choice_modes == frozenset({"auto", "required"})
    assert effective.supports(LLMCapability.STRUCTURED_OUTPUT_NATIVE)
    assert effective.supports(LLMCapability.PARALLEL_TOOLS)


def test_unconfigured_mistral_is_projected_through_reviewed_catalog(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """The normal catalog exposes Mistral without a provider-specific UI path."""

    owner, _ = identity_users
    catalog = LLMProviderCatalogService()
    providers = catalog.list_providers()
    credentials = LLMCredentialService(llm_identity_db, catalog_service=catalog)
    outcome = LLMCatalogProjectionService(llm_identity_db).project(
        user_id=owner.id,
        providers=providers,
        credential_statuses={
            provider.id: credentials.get_masked_status(owner.id, provider.id)
            for provider in providers
        },
    )

    mistral = next(
        provider
        for provider in outcome.providers
        if provider.id == MISTRAL_OPENAI_COMPATIBLE_PRESET_ID
    )
    model = mistral.models[0]
    assert mistral.label == "Mistral"
    assert model.id == "mistral-small-latest"
    assert model.canonical_model_id == "mistral/mistral-small-2603"
    assert model.reasoning_efforts == ("none", "high")
    assert model.visible_reasoning_efforts == ("none", "high")
    assert model.default_reasoning_effort == "none"
    assert model.connection is not None


def test_mistral_request_policy_preserves_neutral_semantics() -> None:
    """Neutral required-tool choice maps to Mistral's equivalent wire value."""

    policy = resolve_compatible_request_policy(MISTRAL_SMALL_REQUEST_POLICY_ID)
    payload = policy.translate(
        {"tool_choice": "required"},
        CompatibleRequestOptions(reasoning_effort="high"),
    )

    assert payload == {"tool_choice": "any", "reasoning_effort": "high"}


def test_mistral_reasoning_chunks_keep_only_final_text() -> None:
    """Visible answers exclude provider reasoning chunks."""

    content = _final_text_content(
        [
            {"type": "thinking", "thinking": [{"type": "text", "text": "private"}]},
            {"type": "text", "text": "Port 5432 is closed."},
        ]
    )

    assert content == "Port 5432 is closed."


def test_mistral_small_pricing_is_available() -> None:
    """The canonical model uses the reviewed Mistral token schedule."""

    quote = get_pricing_quote(
        ProviderModelRef("mistral", "mistral-small-2603"),
        api_surface="chat_completions",
    )

    assert quote.status == "available"
    assert quote.schedule is not None
    assert str(quote.schedule.component_prices_per_million["input_tokens"]) == "0.15"
    assert str(quote.schedule.component_prices_per_million["output_tokens"]) == "0.60"
