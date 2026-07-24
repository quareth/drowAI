"""Opt-in live matrix for DrowAI's production agent compatibility harness."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

import pytest

from agent.providers.llm.adapters.openai.responses.client import (
    OpenAIResponsesClient,
)
from agent.providers.llm.factory.client_factory import LLMClientFactory
from agent.providers.llm.profiles.registry import require_model_profile
from backend.services.llm_provider.operation_registry import (
    MISTRAL_OPENAI_COMPATIBLE_PRESET_ID,
    ConnectionOperationRegistry,
)
from backend.services.llm_provider.types import LLMConnectionOperation
from backend.tests.services.llm_provider.agent_compatibility_harness import (
    run_agent_compatibility_harness,
)


LIVE_AGENT_COMPATIBILITY_ENV = "DROWAI_LIVE_AGENT_COMPATIBILITY"
_MISTRAL_REGISTRY = ConnectionOperationRegistry(env_getter=lambda _name: None)
_MISTRAL_PRESET = _MISTRAL_REGISTRY.get_connection_preset(
    MISTRAL_OPENAI_COMPATIBLE_PRESET_ID
)
_MISTRAL_TARGET = _MISTRAL_REGISTRY.resolve(
    LLMConnectionOperation.INFERENCE,
    provider=_MISTRAL_PRESET.id,
)
_MISTRAL_PROFILE = require_model_profile(_MISTRAL_PRESET.canonical_ref)


@dataclass(frozen=True, slots=True)
class LiveHarnessCase:
    """One deployment factory and its provider-neutral harness controls."""

    evidence_id: str
    provider: str
    model: str
    api_key_env: str
    client_factory: Callable[[], Any]
    reasoning_effort: str | None


def _env_enabled(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _openai_mini_client() -> OpenAIResponsesClient:
    """Build the existing OpenAI Responses adapter for GPT-5.4 Mini."""

    return OpenAIResponsesClient(
        api_key=os.environ["OPENAI_API_KEY"],
        model="gpt-5.4-mini",
        reasoning_effort="low",
    )


def _mistral_small_client():
    """Build Mistral through the production route-adapter factory contract."""

    return LLMClientFactory.get_client(
        provider_model=_MISTRAL_PROFILE.ref,
        model_profile=_MISTRAL_PROFILE,
        adapter_id=_MISTRAL_PRESET.adapter_id,
        api_key=os.environ["MISTRAL_API_KEY"],
        base_url=_MISTRAL_TARGET.client_base_url,
        wire_model_id=_MISTRAL_PRESET.exact_wire_model_id,
        dialect_policy_id=_MISTRAL_PRESET.dialect_policy_id,
        request_policy_id=_MISTRAL_PRESET.request_policy_id,
        reasoning_effort=_MISTRAL_PROFILE.default_reasoning_effort,
    )


LIVE_HARNESS_CASES = (
    LiveHarnessCase(
        evidence_id="openai_gpt_5_4_mini",
        provider="openai",
        model="gpt-5.4-mini",
        api_key_env="OPENAI_API_KEY",
        client_factory=_openai_mini_client,
        reasoning_effort="low",
    ),
    LiveHarnessCase(
        evidence_id="mistral_small_4",
        provider=_MISTRAL_PROFILE.ref.provider,
        model=_MISTRAL_PROFILE.ref.model,
        api_key_env="MISTRAL_API_KEY",
        client_factory=_mistral_small_client,
        reasoning_effort=_MISTRAL_PROFILE.default_reasoning_effort,
    ),
)


@pytest.mark.parametrize(
    "case",
    LIVE_HARNESS_CASES,
    ids=[case.evidence_id for case in LIVE_HARNESS_CASES],
)
@pytest.mark.asyncio
async def test_live_deployment_completes_real_drowai_agent_harness(
    case: LiveHarnessCase,
) -> None:
    """A supported deployment must complete DrowAI's real agent contracts."""

    if not _env_enabled(LIVE_AGENT_COMPATIBILITY_ENV):
        pytest.skip(f"{LIVE_AGENT_COMPATIBILITY_ENV} is not enabled")
    if not os.getenv(case.api_key_env):
        pytest.skip(f"{case.api_key_env} is not set")

    client = case.client_factory()
    try:
        result = await run_agent_compatibility_harness(
            client,
            provider=case.provider,
            model=case.model,
            reasoning_effort=case.reasoning_effort,
        )
    finally:
        await client.aclose()

    assert result.intent_label
    assert result.tool_name.endswith("_nmap")
    assert result.final_answer
