"""Opt-in live matrix for DrowAI's production agent compatibility harness."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

import pytest

from agent.providers.llm.adapters.openai.compatible_dialects import (
    OPENAI_COMPATIBLE_CHAT_ADAPTER_ID,
)
from agent.providers.llm.adapters.openai.compatible_request_policies import (
    MISTRAL_SMALL_REQUEST_POLICY_ID,
)
from agent.providers.llm.adapters.openai.responses.client import (
    OpenAIResponsesClient,
)
from agent.providers.llm.core.identity import ProviderModelRef
from agent.providers.llm.factory.client_factory import LLMClientFactory
from agent.providers.llm.profiles.registry import require_model_profile
from backend.tests.services.llm_provider.agent_compatibility_harness import (
    run_agent_compatibility_harness,
)


LIVE_AGENT_COMPATIBILITY_ENV = "DROWAI_LIVE_AGENT_COMPATIBILITY"


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

    profile = require_model_profile(
        ProviderModelRef("mistral", "mistral-small-2603")
    )
    return LLMClientFactory.get_client(
        provider_model=profile.ref,
        model_profile=profile,
        adapter_id=OPENAI_COMPATIBLE_CHAT_ADAPTER_ID,
        api_key=os.environ["MISTRAL_API_KEY"],
        base_url="https://api.mistral.ai/v1",
        wire_model_id="mistral-small-latest",
        dialect_policy_id="openai_compatible_chat.mistral_v1",
        request_policy_id=MISTRAL_SMALL_REQUEST_POLICY_ID,
        reasoning_effort="none",
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
        provider="mistral",
        model="mistral-small-2603",
        api_key_env="MISTRAL_API_KEY",
        client_factory=_mistral_small_client,
        reasoning_effort="none",
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
