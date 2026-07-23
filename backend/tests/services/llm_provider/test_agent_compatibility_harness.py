"""Tests for the provider-neutral real-contract agent compatibility harness."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from backend.tests.services.llm_provider.agent_compatibility_harness import (
    build_real_intent_request,
    build_real_nmap_tool_spec,
    fake_tool_call,
    normalize_tool_arguments,
    run_agent_compatibility_harness,
    validate_nmap_arguments,
)


class _HarnessClient:
    """Deterministic client double that records the three harness stages."""

    def __init__(self) -> None:
        self.stages: list[str] = []

    async def chat_messages_with_usage(
        self,
        _messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        if "structured_output" in kwargs:
            self.stages.append("intent")
            return SimpleNamespace(
                content='{"label":"direct_executor"}',
                structured_output={"label": "direct_executor"},
            )
        self.stages.append("final")
        return SimpleNamespace(content="Port 5432 is closed.")

    async def chat_with_tools_with_usage(
        self,
        _system: str,
        _user: str,
        tools: list[Any],
        **_kwargs: Any,
    ) -> Any:
        self.stages.append("tool")
        return SimpleNamespace(
            tool_calls=[
                fake_tool_call(
                    name=tools[0].name,
                    arguments={
                        "_builder_intent": "Check PostgreSQL",
                        "target": "127.0.0.1",
                        "ports": "5432",
                        "scan_types": ["-sT"],
                    },
                )
            ]
        )


def test_harness_reuses_production_classifier_and_nmap_contracts() -> None:
    """The harness carries DrowAI's deep schema and exact Nmap enum."""

    intent = build_real_intent_request(provider="openai", model="gpt-5.4-mini")
    assert intent.max_tokens == 32_000
    assert intent.structured_output.name == "intent_classifier"
    assert "prior_turn_reference" in intent.structured_output.schema["properties"]

    nmap = build_real_nmap_tool_spec()
    scan_types = nmap.parameters_schema["$defs"]["ScanType"]
    assert "-sT" in scan_types["enum"]
    assert "-p" not in scan_types["enum"]


def test_harness_locally_rejects_invalid_model_tool_arguments() -> None:
    """Provider acceptance cannot bypass DrowAI's local tool validation."""

    with pytest.raises(ValidationError):
        validate_nmap_arguments(
            {
                "target": "127.0.0.1",
                "ports": "5432",
                "scan_types": ["-p"],
            }
        )


def test_harness_normalizes_responses_api_json_tool_arguments() -> None:
    """Responses API JSON text reaches the same local validator as mappings."""

    assert normalize_tool_arguments(
        '{"target":"127.0.0.1","ports":"5432","scan_types":["-sT"]}'
    ) == {
        "target": "127.0.0.1",
        "ports": "5432",
        "scan_types": ["-sT"],
    }


@pytest.mark.asyncio
async def test_harness_completes_agent_lifecycle_without_executing_tool() -> None:
    """The deterministic harness covers intent, tool, and final stages."""

    client = _HarnessClient()
    result = await run_agent_compatibility_harness(
        client,
        provider="openai",
        model="gpt-5.4-mini",
    )

    assert client.stages == ["intent", "tool", "final"]
    assert result.intent_label == "direct_executor"
    assert result.tool_name.endswith("_nmap")
    assert result.final_answer == "Port 5432 is closed."
