"""Unit and regression tests for LangGraph intent classifier behavior."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from types import SimpleNamespace

import pytest

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from backend.services.langgraph_chat.intent.classifier import (
    ANTHROPIC_INTENT_CLASSIFIER_MAX_TOKENS,
    IntentClassifier,
)
from backend.services.langgraph_chat.contracts import ChatInputs, ExecutionMode, LangGraphRuntimeConfig


def _install_bundle(
    metadata: Dict[str, Any],
    history: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Install a ConversationContextBundle on metadata.

    Phase 5 cutover (``single-assembly-authority``): direct-node tests
    that bypass the facade must populate ``metadata[context_bundle]``
    themselves, since the classifier now raises ``RuntimeError`` when
    the bundle is missing rather than silently falling back.
    """
    bundle = build_conversation_context_bundle(
        conversation_id="conv-1",
        turn_id="turn-1",
        turn_sequence=0,
        messages=list(history),
    )
    metadata[METADATA_CONTEXT_BUNDLE_KEY] = bundle
    return bundle


class _StubClient:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0

    async def chat(self, *, system_prompt: str, user_prompt: str, **kwargs: Any) -> str:  # type: ignore[override]
        self.calls += 1
        return self.response


class _StubUsageClient:
    def __init__(
        self,
        *,
        content: str,
        structured_output: Optional[Dict[str, Any]] = None,
        raise_error: bool = False,
    ) -> None:
        self.content = content
        self.structured_output = structured_output
        self.raise_error = raise_error
        self.calls = 0
        self.last_kwargs: Dict[str, Any] = {}

    async def chat_with_usage(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        self.calls += 1
        self.last_kwargs = dict(kwargs)
        if self.raise_error:
            raise RuntimeError("structured parse failure")
        return SimpleNamespace(
            content=self.content,
            usage=None,
            structured_output=self.structured_output,
        )


class _TimeoutUsageClient:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_usage(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        self.calls += 1
        raise asyncio.TimeoutError()


def _runtime_config(
    metadata: Dict[str, Any],
    *,
    api_key: Optional[str] = "test-key",
    message: str = "Run nmap on 10.10.10.10",
) -> LangGraphRuntimeConfig:
    history = [{"role": "user", "content": "hello"}]
    if METADATA_CONTEXT_BUNDLE_KEY not in metadata:
        _install_bundle(metadata, history)
    chat_inputs = ChatInputs(
        task_id=42,
        user_id=7,
        message=message,
        conversation_id="conv-1",
        history=history,
        api_key=api_key,
        model="gpt-5.2",
    )
    return LangGraphRuntimeConfig(
        chat_inputs=chat_inputs,
        metadata=metadata,
        execution_mode=ExecutionMode.NORMAL_CHAT,
    )


@pytest.mark.asyncio
async def test_intent_classifier_runs_for_normal_chat_inputs() -> None:
    metadata = {
        "eligible_routes": ["normal_chat"],
        "intent_hints": {"tool_hints": [], "targets": []},
    }
    config = _runtime_config(metadata)

    stub_client = _StubClient("{}")

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert stub_client.calls == 1
    assert config.metadata["intent_classifier_label"] == "simple_chat"
    assert config.execution_mode == ExecutionMode.NORMAL_CHAT


@pytest.mark.asyncio
async def test_intent_classifier_uses_runtime_selection_for_resolver_target() -> None:
    metadata = {
        "eligible_routes": ["normal_chat"],
        "intent_hints": {"tool_hints": [], "targets": []},
    }
    config = _runtime_config(metadata)
    config.chat_inputs.model = "gpt-5.2"
    config.llm_runtime_selection = {
        "provider": "openai",
        "model": "gpt-5-mini",
        "credential_ref": {"user_id": 7, "provider": "openai"},
        "reasoning_effort": "low",
    }
    calls: list[Dict[str, Any]] = []

    class _Resolver:
        def get_client(self, selection: Any, **kwargs: Any) -> Any:
            calls.append({"selection": selection, **kwargs})
            return _StubUsageClient(
                content='{"label":"chat","confidence":0.9,"reasoning":"chat"}',
                structured_output={
                    "label": "chat",
                    "confidence": 0.9,
                    "reasoning": "chat",
                },
            )

    config.runtime_services = SimpleNamespace(client_resolver=_Resolver())

    classifier = IntentClassifier()
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert calls[0]["selection"] == config.llm_runtime_selection
    assert calls[0]["runtime_user_id"] == 7
    assert calls[0]["purpose"] == "intent_classifier"
    assert calls[0]["target"].model == "gpt-5-mini"
    assert calls[0]["target"].provider == "openai"


@pytest.mark.asyncio
async def test_intent_classifier_caps_anthropic_non_streaming_output_budget() -> None:
    metadata = {
        "eligible_routes": ["normal_chat"],
        "intent_hints": {"tool_hints": [], "targets": []},
    }
    config = _runtime_config(metadata)
    config.chat_inputs.provider = "anthropic"
    config.chat_inputs.model = "claude-haiku-4-5-20251001"
    stub_client = _StubUsageClient(
        content='{"label":"simple_chat","confidence":0.9,"reasoning":"chat"}',
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert stub_client.calls == 1
    assert stub_client.last_kwargs["max_tokens"] == ANTHROPIC_INTENT_CLASSIFIER_MAX_TOKENS


@pytest.mark.asyncio
async def test_intent_classifier_preserves_openai_output_budget() -> None:
    metadata = {
        "eligible_routes": ["normal_chat"],
        "intent_hints": {"tool_hints": [], "targets": []},
    }
    config = _runtime_config(metadata)
    config.chat_inputs.provider = "openai"
    config.chat_inputs.model = "gpt-5.2"
    stub_client = _StubUsageClient(
        content='{"label":"simple_chat","confidence":0.9,"reasoning":"chat"}',
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert stub_client.calls == 1
    assert stub_client.last_kwargs["max_tokens"] == 32_000


@pytest.mark.asyncio
async def test_intent_classifier_merges_metadata() -> None:
    stub_response = """
    {
      "label": "tool_call",
      "confidence": 0.82,
      "suggested_capabilities": ["tool_call"],
      "risk_flags": ["moderate_risk"],
      "reasoning": "Executing a network scan best addresses the request."
    }
    """
    stub_client = _StubClient(stub_response)

    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat", "simple_tool_execution"],
        "intent_hints": {"tool_hints": ["network_scan"], "targets": ["10.10.10.10"]},
        "risk_flags": [],
    }
    config = _runtime_config(metadata)

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert stub_client.calls == 1
    assert config.metadata["intent_classifier_label"] == "direct_executor"
    assert config.metadata["risk_flags"] == ["moderate_risk"]
    assert "intent_classifier_reasoning" in config.metadata
    assert "simple_tool_execution" in config.metadata["eligible_routes"]

    from agent.graph.state import InteractiveInput

    state = InteractiveInput(
        task_id=config.chat_inputs.task_id,
        message=config.chat_inputs.message,
        metadata=config.metadata,
    ).to_state()

    assert state.facts.intent_hints["classifier_label"] == "direct_executor"
    assert any("network scan" in entry.lower() for entry in state.trace.reasoning)


@pytest.mark.asyncio
async def test_intent_classifier_normalizes_descriptive_capabilities() -> None:
    """Test that descriptive capabilities like 'network scanning' are normalized to enable tool execution route.
    
    This is a regression test for a bug where the classifier would return descriptive
    capabilities (e.g., "network scanning", "port scanning") but the routing logic
    only recognized exact matches like "tool_call". The fix normalizes these using
    the SUGGESTED_CAPABILITY_ALIASES mapping.
    """
    stub_response = """
    {
      "label": "tool_call",
      "confidence": 0.9,
      "suggested_capabilities": ["network scanning", "port scanning"],
      "risk_flags": [],
      "reasoning": "The user has requested a specific network scan using nmap, which is a deterministic tool action."
    }
    """
    stub_client = _StubClient(stub_response)

    # Start with heuristics that detected tool keywords
    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat", "simple_tool_execution"],
        "intent_hints": {"tool_hints": ["network_scan"], "targets": ["127.0.0.1"]},
        "risk_flags": [],
    }
    config = _runtime_config(metadata)

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    # Verify classifier ran and returned result
    assert result is not None
    assert stub_client.calls == 1
    assert config.metadata["intent_classifier_label"] == "direct_executor"
    
    # CRITICAL: eligible_routes must include "simple_tool_execution"
    # This was the bug - descriptive capabilities were ignored and routes got overwritten to just ["normal_chat"]
    assert "simple_tool_execution" in config.metadata["eligible_routes"], \
        f"Bug: eligible_routes={config.metadata['eligible_routes']} missing 'simple_tool_execution'"
    
    # Verify execution mode was set correctly based on label
    assert config.execution_mode == ExecutionMode.SIMPLE_TOOL
    
    # Suggested capabilities may be normalized by classifier internals; the
    # regression we care about is that route eligibility is preserved.
    assert result.signals.suggested_capabilities


@pytest.mark.asyncio
async def test_intent_classifier_sets_binary_request_contract_heuristically() -> None:
    metadata = {
        "eligible_routes": ["normal_chat"],
        "intent_hints": {"tool_hints": [], "targets": []},
    }
    config = _runtime_config(
        metadata,
        message="Determine if port 5432 is open on 10.0.0.5. Give a short answer.",
    )

    classifier = IntentClassifier()
    await classifier.enrich_runtime_config(config)

    contract = config.metadata.get("request_contract")
    assert isinstance(contract, dict)
    assert contract.get("question_type") == "binary_check"
    assert contract.get("answer_style") == "short"
    assert contract.get("terminal_when") == "determined"


@pytest.mark.asyncio
async def test_intent_classifier_keeps_tool_call_for_single_step_executable_request() -> None:
    stub_response = """
    {
      "label": "tool_call",
      "confidence": 0.91,
      "suggested_capabilities": ["tool_call"],
      "risk_flags": [],
      "reasoning": "Tool execution requested."
    }
    """
    stub_client = _StubClient(stub_response)

    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat", "simple_tool_execution"],
        "intent_hints": {"tool_hints": ["network_scan"], "targets": []},
        "risk_flags": [],
    }
    config = _runtime_config(
        metadata,
        message="Scan current docker to find online hosts",
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert config.metadata["intent_classifier_label"] == "direct_executor"
    assert config.execution_mode == ExecutionMode.SIMPLE_TOOL


@pytest.mark.asyncio
async def test_intent_classifier_normalizes_direct_executor_alias_to_tool_call() -> None:
    stub_response = """
    {
      "label": "direct_executor",
      "confidence": 0.91,
      "suggested_capabilities": ["network_scan"],
      "risk_flags": [],
      "reasoning": "The request is one concrete executable action."
    }
    """
    stub_client = _StubClient(stub_response)

    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat", "simple_tool_execution"],
        "intent_hints": {"tool_hints": ["network_scan"], "targets": []},
        "risk_flags": [],
    }
    config = _runtime_config(
        metadata,
        message="Scan current docker to find online hosts",
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert config.metadata["intent_classifier_label"] == "direct_executor"
    assert config.metadata["intent_hints"]["classifier_label"] == "direct_executor"
    assert "simple_tool_execution" in config.metadata["eligible_routes"]
    assert "direct_executor" not in config.metadata["eligible_routes"]
    assert config.execution_mode == ExecutionMode.SIMPLE_TOOL


@pytest.mark.asyncio
async def test_intent_classifier_keeps_low_confidence_deep_reasoning() -> None:
    stub_response = """
    {
      "label": "deep_reasoning",
      "confidence": 0.61,
      "suggested_capabilities": ["deep_reasoning"],
      "risk_flags": [],
      "reasoning": "Likely multi-step."
    }
    """
    stub_client = _StubClient(stub_response)

    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat", "deep_reasoning"],
        "intent_hints": {"tool_hints": ["network_scan"], "targets": ["10.10.10.10"]},
        "risk_flags": [],
    }
    config = _runtime_config(
        metadata,
        message="First scan 10.10.10.10 then enumerate services.",
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert config.metadata["intent_classifier_label"] == "plan_executor"
    assert config.execution_mode == ExecutionMode.DEEP_REASONING


@pytest.mark.asyncio
async def test_intent_classifier_normalizes_plan_executor_alias_to_deep_reasoning() -> None:
    stub_response = """
    {
      "label": "plan_executor",
      "confidence": 0.83,
      "suggested_capabilities": ["network_scan", "port_scan"],
      "risk_flags": [],
      "reasoning": "The request requires sequenced tool execution."
    }
    """
    stub_client = _StubClient(stub_response)

    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat", "deep_reasoning"],
        "intent_hints": {"tool_hints": ["network_scan"], "targets": ["10.10.10.10"]},
        "risk_flags": [],
    }
    config = _runtime_config(
        metadata,
        message="First scan 10.10.10.10 then enumerate services.",
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert config.metadata["intent_classifier_label"] == "plan_executor"
    assert config.metadata["intent_hints"]["classifier_label"] == "plan_executor"
    assert "deep_reasoning" in config.metadata["eligible_routes"]
    assert "plan_executor" not in config.metadata["eligible_routes"]
    assert config.execution_mode == ExecutionMode.DEEP_REASONING


@pytest.mark.asyncio
async def test_intent_classifier_uses_normal_chat_when_classifier_is_skipped() -> None:
    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat", "simple_tool_execution"],
        "intent_hints": {"tool_hints": ["network_scan"], "targets": []},
        "risk_flags": [],
    }
    config = _runtime_config(
        metadata,
        api_key=None,
        message="Scan current docker to find online hosts",
    )

    classifier = IntentClassifier()
    result = await classifier.enrich_runtime_config(config)

    assert result is None
    assert config.metadata["intent_classifier_skipped"] == "missing_llm_runtime"
    assert config.execution_mode == ExecutionMode.NORMAL_CHAT


@pytest.mark.asyncio
async def test_intent_classifier_keeps_tool_call_for_multistep_prompt_when_label_is_tool_call() -> None:
    stub_response = """
    {
      "label": "tool_call",
      "confidence": 0.93,
      "suggested_capabilities": ["tool_call", "network_scan"],
      "risk_flags": [],
      "reasoning": "Run a scan."
    }
    """
    stub_client = _StubClient(stub_response)

    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat", "simple_tool_execution"],
        "intent_hints": {"tool_hints": ["network_scan"], "targets": []},
        "risk_flags": [],
    }
    config = _runtime_config(
        metadata,
        message="First discover hosts, then scan port 5432 on online hosts",
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert config.execution_mode == ExecutionMode.SIMPLE_TOOL
    assert config.metadata["intent_classifier_label"] == "direct_executor"
    assert config.metadata["intent_hints"]["classifier_label"] == "direct_executor"
    assert "simple_tool_execution" in config.metadata["eligible_routes"]


@pytest.mark.asyncio
async def test_intent_classifier_keeps_tool_call_on_multi_step_request_contract() -> None:
    stub_response = """
    {
      "label": "tool_call",
      "confidence": 0.88,
      "suggested_capabilities": ["network_scan"],
      "question_type": "multi_step",
      "risk_flags": [],
      "reasoning": "This should be planned as multiple steps."
    }
    """
    stub_client = _StubClient(stub_response)
    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat", "simple_tool_execution"],
        "intent_hints": {"tool_hints": ["network_scan"], "targets": []},
        "risk_flags": [],
    }
    config = _runtime_config(
        metadata,
        message="Discover online hosts and assess postgres exposure",
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert config.execution_mode == ExecutionMode.SIMPLE_TOOL
    assert config.metadata["intent_classifier_label"] == "direct_executor"
    assert config.metadata["intent_hints"]["classifier_label"] == "direct_executor"
    assert "simple_tool_execution" in config.metadata["eligible_routes"]


@pytest.mark.asyncio
async def test_intent_classifier_uses_normal_chat_when_skipped_for_multistep_message() -> None:
    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat", "simple_tool_execution"],
        "intent_hints": {"tool_hints": ["network_scan"], "targets": []},
        "risk_flags": [],
    }
    config = _runtime_config(
        metadata,
        api_key=None,
        message="Scan network to find online hosts then scan one host for postgre port",
    )

    classifier = IntentClassifier()
    result = await classifier.enrich_runtime_config(config)

    assert result is None
    assert config.metadata["intent_classifier_skipped"] == "missing_llm_runtime"
    assert config.execution_mode == ExecutionMode.NORMAL_CHAT
    assert "deep_reasoning" not in config.metadata["eligible_routes"]


@pytest.mark.asyncio
async def test_intent_classifier_keeps_tool_call_when_deep_reasoning_disabled() -> None:
    stub_response = """
    {
      "label": "tool_call",
      "confidence": 0.95,
      "suggested_capabilities": ["tool_call"],
      "risk_flags": [],
      "reasoning": "Execute requested scan."
    }
    """
    stub_client = _StubClient(stub_response)
    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat", "simple_tool_execution"],
        "intent_hints": {"tool_hints": ["network_scan"], "targets": []},
        "feature_flags": {"deep_reasoning_enabled": False},
        "risk_flags": [],
    }
    config = _runtime_config(
        metadata,
        message="First discover hosts, then scan port 5432 on online hosts",
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert config.execution_mode == ExecutionMode.SIMPLE_TOOL
    assert config.metadata["intent_hints"]["classifier_label"] == "direct_executor"


@pytest.mark.asyncio
async def test_intent_classifier_keeps_deep_reasoning_for_plan_driven_request_without_target() -> None:
    """Plan-driven request without a concrete target -> DEEP_REASONING.

    Under the v6 boundary, ``deep_reasoning`` is reserved for requests that
    genuinely require upfront decomposition into a tracked plan — a phased
    engagement or a broad multi-surface assessment — not merely messages
    that happen to contain sequencing words. This test uses a phased
    pentest request (the canonical v6 plan_executor example) and verifies
    that when the classifier labels it ``deep_reasoning`` the runtime keeps
    ``DEEP_REASONING`` mode and lists ``deep_reasoning`` in eligible routes.
    """
    stub_response = """
    {
      "label": "deep_reasoning",
      "confidence": 0.88,
      "suggested_capabilities": ["network_scan", "port_scan", "host_discovery"],
      "risk_flags": [],
      "reasoning": "Phased engagement requires upfront decomposition into a tracked plan."
    }
    """
    stub_client = _StubClient(stub_response)

    metadata: Dict[str, Any] = {
        "eligible_routes": [],
        "intent_hints": {"tool_hints": [], "targets": []},
        "risk_flags": [],
    }
    config = _runtime_config(
        metadata,
        message=(
            "Do a full pentest: recon hosts, enumerate services, check "
            "known vulns, and produce a report."
        ),
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert config.metadata["intent_classifier_label"] == "plan_executor"
    assert "deep_reasoning" in config.metadata["eligible_routes"]
    assert config.execution_mode == ExecutionMode.DEEP_REASONING


@pytest.mark.asyncio
async def test_intent_classifier_normalizes_dr_alias_to_deep_reasoning() -> None:
    """Backward-compat alias normalization: ``DR`` -> ``deep_reasoning``.

    This test asserts the label-alias mapping only. The message and stub
    reasoning are deliberately plan-driven (matching the v6 contract for
    ``deep_reasoning``) so the scenario is self-consistent, but the test's
    subject is normalization, not the routing boundary.
    """
    stub_response = """
    {
      "label": "DR",
      "confidence": 0.9,
      "suggested_capabilities": ["network_scan", "vulnerability_scan"],
      "risk_flags": [],
      "reasoning": "Phased engagement; DR alias should normalize to deep_reasoning."
    }
    """
    stub_client = _StubClient(stub_response)
    config = _runtime_config(
        {
            "eligible_routes": [],
            "intent_hints": {"tool_hints": [], "targets": []},
            "risk_flags": [],
        },
        message=(
            "Run a full external assessment on example.com across web, DNS, "
            "and network surfaces."
        ),
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert config.metadata["intent_classifier_label"] == "plan_executor"
    assert config.execution_mode == ExecutionMode.DEEP_REASONING


@pytest.mark.asyncio
async def test_intent_classifier_parses_fenced_json_payload() -> None:
    """JSON-fence parsing: the classifier must strip a ``` ```json fence.

    This test asserts the parser only. The message and reasoning are
    deliberately plan-driven (matching the v6 contract for
    ``deep_reasoning``) so the scenario stays self-consistent, but the
    test's subject is payload parsing, not the routing boundary.
    """
    stub_response = """
    ```json
    {
      "label": "deep_reasoning",
      "confidence": 0.92,
      "suggested_capabilities": ["network_scan", "vulnerability_scan"],
      "risk_flags": [],
      "reasoning": "Broad engagement requires a tracked plan."
    }
    ```
    """
    stub_client = _StubClient(stub_response)
    config = _runtime_config(
        {
            "eligible_routes": [],
            "intent_hints": {"tool_hints": [], "targets": []},
            "risk_flags": [],
        },
        message=(
            "Do a full pentest of 10.0.0.0/24: recon hosts, enumerate "
            "services, check known vulns, and produce a report."
        ),
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert config.metadata["intent_classifier_label"] == "plan_executor"
    assert config.execution_mode == ExecutionMode.DEEP_REASONING


@pytest.mark.asyncio
async def test_intent_classifier_uses_user_selected_model() -> None:
    selected_model: Optional[str] = None

    def _factory(call_settings: Any) -> _StubClient:
        nonlocal selected_model
        selected_model = call_settings.model
        return _StubClient("{}")

    classifier = IntentClassifier(client_factory=_factory)
    config = _runtime_config(
        {
            "eligible_routes": ["normal_chat", "simple_tool_execution"],
            "intent_hints": {"tool_hints": ["network_scan"], "targets": ["10.10.10.10"]},
            "risk_flags": [],
        },
    )
    await classifier.enrich_runtime_config(config)

    assert selected_model == config.chat_inputs.model


@pytest.mark.asyncio
async def test_intent_classifier_prefers_structured_output_payload() -> None:
    stub_client = _StubUsageClient(
        content="this is not json",
        structured_output={
            "label": "tool_call",
            "confidence": 0.89,
            "suggested_capabilities": ["network_scan"],
            "requested_output_format": None,
            "question_type": "binary_check",
            "answer_style": "short",
            "terminal_when": "determined",
            "target_status": "resolved",
            "resolved_target": "10.10.10.10",
            "target_source": "explicit_current_message",
            "target_confidence": 0.98,
            "target_evidence": "User explicitly requested nmap against 10.10.10.10.",
            "prior_target_reuse": "disallow",
            "prior_target_reuse_evidence": "Current message provides its own explicit target.",
            "risk_flags": [],
            "reasoning": "Direct tool action requested.",
        },
    )
    config = _runtime_config(
        {
            "eligible_routes": ["normal_chat", "simple_tool_execution"],
            "intent_hints": {"tool_hints": ["network_scan"], "targets": ["10.10.10.10"]},
            "risk_flags": [],
        },
        message="Run nmap -sn 10.10.10.10",
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert stub_client.calls == 1
    assert config.metadata["intent_classifier_label"] == "direct_executor"
    assert config.execution_mode == ExecutionMode.SIMPLE_TOOL
    assert config.metadata["intent_target_resolution"]["target_status"] == "resolved"
    assert config.metadata["intent_target_resolution"]["resolved_target"] == "10.10.10.10"
    assert config.metadata["intent_target_continuity"]["status"] == "disallow"
    assert config.metadata["intent_hints"]["targets"] == ["10.10.10.10"]


@pytest.mark.asyncio
async def test_intent_classifier_marks_generic_scan_target_unresolved() -> None:
    stub_response = """
    {
      "label": "tool_call",
      "confidence": 0.92,
      "suggested_capabilities": ["tool_call"],
      "requested_output_format": null,
      "question_type": "open_ended",
      "answer_style": "normal",
      "terminal_when": "all_steps_done",
      "target_status": "unresolved",
      "resolved_target": null,
      "target_source": "none",
      "target_confidence": null,
      "target_evidence": "No explicit or referential target in the current request.",
      "prior_target_reuse": "disallow",
      "prior_target_reuse_evidence": "New broad discovery objective.",
      "risk_flags": [],
      "reasoning": "Tool execution requested but target is unspecified."
    }
    """
    stub_client = _StubClient(stub_response)
    config = _runtime_config(
        {
            "eligible_routes": ["normal_chat", "simple_tool_execution"],
            "intent_hints": {"tool_hints": ["network_scan"], "targets": ["127.0.0.1"]},
            "risk_flags": [],
        },
        message="scan network to find online hosts",
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    resolution = config.metadata["intent_target_resolution"]
    assert resolution["target_status"] == "unresolved"
    assert resolution["resolved_target"] is None
    continuity = config.metadata["intent_target_continuity"]
    assert continuity["status"] == "disallow"
    assert config.metadata["intent_hints"]["targets"] == []


@pytest.mark.asyncio
async def test_intent_classifier_marks_ambiguous_referential_target_and_clears_hints() -> None:
    stub_response = """
    {
      "label": "tool_call",
      "confidence": 0.84,
      "suggested_capabilities": ["tool_call"],
      "requested_output_format": null,
      "question_type": "multi_step",
      "answer_style": "normal",
      "terminal_when": "all_steps_done",
      "target_status": "ambiguous",
      "resolved_target": null,
      "target_source": "referential_history",
      "target_confidence": null,
      "target_evidence": "Reference to 'that target' but multiple prior targets exist.",
      "prior_target_reuse": "ambiguous",
      "prior_target_reuse_evidence": "Multiple prior targets are plausible.",
      "risk_flags": [],
      "reasoning": "Referential request is ambiguous."
    }
    """
    stub_client = _StubClient(stub_response)
    config = _runtime_config(
        {
            "eligible_routes": ["normal_chat", "simple_tool_execution"],
            "intent_hints": {"tool_hints": ["network_scan"], "targets": ["10.0.0.5", "10.0.0.6"]},
            "risk_flags": [],
        },
        message="ok now do this to that target",
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    resolution = config.metadata["intent_target_resolution"]
    assert resolution["target_status"] == "ambiguous"
    assert resolution["resolved_target"] is None
    continuity = config.metadata["intent_target_continuity"]
    assert continuity["status"] == "ambiguous"
    assert config.metadata["intent_hints"]["targets"] == []


@pytest.mark.asyncio
async def test_intent_classifier_allows_prior_target_reuse_on_referential_followup() -> None:
    stub_response = """
    {
      "label": "tool_call",
      "confidence": 0.86,
      "suggested_capabilities": ["tool_call"],
      "requested_output_format": null,
      "question_type": "open_ended",
      "answer_style": "normal",
      "terminal_when": "all_steps_done",
      "target_status": "unresolved",
      "resolved_target": null,
      "target_source": "none",
      "target_confidence": null,
      "target_evidence": "Current message does not include explicit target.",
      "prior_target_reuse": "allow",
      "prior_target_reuse_evidence": "User said 'scan it then' and there is one obvious active target.",
      "risk_flags": [],
      "reasoning": "Follow-up should continue with existing target."
    }
    """
    stub_client = _StubClient(stub_response)
    config = _runtime_config(
        {
            "eligible_routes": ["normal_chat", "simple_tool_execution"],
            "intent_hints": {"tool_hints": ["network_scan"], "targets": ["10.0.0.5"]},
            "risk_flags": [],
        },
        message="scan it then",
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    resolution = config.metadata["intent_target_resolution"]
    assert resolution["target_status"] == "unresolved"
    assert resolution["resolved_target"] is None
    continuity = config.metadata["intent_target_continuity"]
    assert continuity["status"] == "allow"
    assert continuity["source"] == "classifier"
    assert config.metadata["intent_hints"]["targets"] == []


@pytest.mark.asyncio
async def test_intent_classifier_does_not_accept_resolved_status_without_target() -> None:
    stub_response = """
    {
      "label": "tool_call",
      "confidence": 0.78,
      "suggested_capabilities": ["tool_call"],
      "requested_output_format": null,
      "question_type": "open_ended",
      "answer_style": "normal",
      "terminal_when": "all_steps_done",
      "target_status": "resolved",
      "resolved_target": "",
      "target_source": "explicit_current_message",
      "target_confidence": 0.7,
      "target_evidence": "No concrete target string present.",
      "prior_target_reuse": "disallow",
      "prior_target_reuse_evidence": "No continuity requested.",
      "risk_flags": [],
      "reasoning": "Malformed target payload."
    }
    """
    stub_client = _StubClient(stub_response)
    config = _runtime_config(
        {
            "eligible_routes": ["normal_chat", "simple_tool_execution"],
            "intent_hints": {"tool_hints": ["network_scan"], "targets": ["127.0.0.1"]},
            "risk_flags": [],
        },
        message="scan network",
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    resolution = config.metadata["intent_target_resolution"]
    assert resolution["target_status"] == "unresolved"
    assert resolution["resolved_target"] is None
    continuity = config.metadata["intent_target_continuity"]
    assert continuity["status"] == "disallow"
    assert config.metadata["intent_hints"]["targets"] == []


@pytest.mark.asyncio
async def test_intent_classifier_structured_failure_follows_skip_path() -> None:
    stub_client = _StubUsageClient(content="", raise_error=True)
    config = _runtime_config(
        {
            "eligible_routes": ["normal_chat", "simple_tool_execution"],
            "intent_hints": {"tool_hints": ["network_scan"], "targets": []},
            "risk_flags": [],
        },
        message="Scan local docker subnet",
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is None
    assert config.metadata["intent_classifier_skipped"] == "llm_error"
    assert config.metadata["intent_target_continuity"]["status"] == "disallow"
    assert config.execution_mode == ExecutionMode.NORMAL_CHAT


@pytest.mark.asyncio
async def test_intent_classifier_timeout_follows_timeout_skip_path() -> None:
    stub_client = _TimeoutUsageClient()
    config = _runtime_config(
        {
            "eligible_routes": ["normal_chat", "simple_tool_execution"],
            "intent_hints": {"tool_hints": ["network_scan"], "targets": []},
            "risk_flags": [],
        },
        message="Scan local docker subnet",
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is None
    assert stub_client.calls == 1
    assert config.metadata["intent_classifier_skipped"] == "timeout"
    assert config.metadata["intent_classifier_error_type"] == "timeout"
    assert config.metadata["intent_classifier_timeout_sec"] == classifier._client_timeout
    assert config.metadata["intent_target_continuity"]["status"] == "disallow"
    assert config.execution_mode == ExecutionMode.NORMAL_CHAT


@pytest.mark.asyncio
async def test_intent_classifier_timeout_logs_canonical_timeout_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stub_client = _TimeoutUsageClient()
    config = _runtime_config(
        {
            "eligible_routes": ["normal_chat", "simple_tool_execution"],
            "intent_hints": {"tool_hints": ["network_scan"], "targets": []},
            "risk_flags": [],
        },
        message="Scan local docker subnet",
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)

    with caplog.at_level("WARNING"):
        result = await classifier.enrich_runtime_config(config)

    assert result is None
    assert (
        f"TIMEOUT | Task {config.chat_inputs.task_id} | INTENT | "
        "classifier_llm_call"
    ) in caplog.text


# ---------------------------------------------------------------------------
# New routing boundary (direct_executor = bounded chain; plan_executor =
# requires formal decomposition). These tests pin the CONTRACT that the v6
# intent prompt teaches: a bounded conditional chain is direct_executor, a
# phased engagement is plan_executor. They stub the LLM to return the label
# the prompt now teaches for these representative messages and assert the
# runtime maps each to the correct execution mode.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intent_classifier_routes_bounded_conditional_chain_to_direct_executor() -> None:
    """Bounded multi-step-without-planning (``ping then if online nmap``).

    The v6 prompt teaches the classifier to label this as direct_executor.
    The runtime must then route to SIMPLE_TOOL — NOT DEEP_REASONING — even
    though the message contains sequencing words like "and if". This is the
    exact case the old prompt mis-classified as plan_executor.
    """
    stub_response = """
    {
      "label": "direct_executor",
      "confidence": 0.88,
      "suggested_capabilities": ["network_scan"],
      "risk_flags": [],
      "reasoning": "Short bounded chain; no formal plan needed."
    }
    """
    stub_client = _StubClient(stub_response)
    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat", "simple_tool_execution"],
        "intent_hints": {"tool_hints": ["network_scan"], "targets": []},
        "risk_flags": [],
    }
    config = _runtime_config(
        metadata,
        message="Ping 10.0.0.5 and if online run nmap on it",
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert config.execution_mode == ExecutionMode.SIMPLE_TOOL
    assert config.metadata["intent_classifier_label"] == "direct_executor"
    assert config.metadata["intent_hints"]["classifier_label"] == "direct_executor"
    assert "simple_tool_execution" in config.metadata["eligible_routes"]


@pytest.mark.asyncio
async def test_intent_classifier_routes_sequenced_probe_chain_to_direct_executor() -> None:
    """Another bounded chain: ``resolve then whois``. Same contract."""
    stub_response = """
    {
      "label": "direct_executor",
      "confidence": 0.86,
      "suggested_capabilities": ["dns_lookup", "osint"],
      "risk_flags": [],
      "reasoning": "Bounded two-step sequence without decomposition."
    }
    """
    stub_client = _StubClient(stub_response)
    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat", "simple_tool_execution"],
        "intent_hints": {"tool_hints": ["dns_lookup"], "targets": []},
        "risk_flags": [],
    }
    config = _runtime_config(
        metadata,
        message="Resolve example.com then whois the resulting IP",
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert config.execution_mode == ExecutionMode.SIMPLE_TOOL


@pytest.mark.asyncio
async def test_intent_classifier_routes_phased_engagement_to_plan_executor() -> None:
    """A request that genuinely requires upfront decomposition.

    The v6 prompt teaches the classifier to label phased engagements
    (recon -> enumeration -> vuln checks -> report) as plan_executor, and
    the runtime must route them to DEEP_REASONING.
    """
    stub_response = """
    {
      "label": "plan_executor",
      "confidence": 0.92,
      "suggested_capabilities": ["network_scan", "vulnerability_scan"],
      "risk_flags": [],
      "reasoning": "Broad phased engagement requires a tracked plan."
    }
    """
    stub_client = _StubClient(stub_response)
    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat", "deep_reasoning"],
        "intent_hints": {"tool_hints": ["network_scan"], "targets": ["10.0.0.0/24"]},
        "risk_flags": [],
    }
    config = _runtime_config(
        metadata,
        message=(
            "Do a full pentest of 10.0.0.0/24: recon hosts, enumerate "
            "services, check known vulns, and produce a report."
        ),
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub_client)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert config.execution_mode == ExecutionMode.DEEP_REASONING
    assert config.metadata["intent_classifier_label"] == "plan_executor"
    assert "deep_reasoning" in config.metadata["eligible_routes"]


# ---------------------------------------------------------------------------
# Tenant baseline schema lock: turn_interpretation nested object.
#
# The classifier contract now carries a nested ``turn_interpretation`` object
# distilling the current turn's resolved meaning, continuation semantics,
# step reference resolution, execution readiness, and downstream retrieval
# seeds. This schema lock only pins the contract — no downstream wiring or storage
# decisions are made here. The tests below pin:
#   1. presence and full shape of the nested object on the classifier's
#      pass-through raw response;
#   2. a vague continuation turn resolved into the nested object;
#   3. step reference resolution (e.g. ``second step``);
#   4. blocked vs ready execution readiness reporting.
# ---------------------------------------------------------------------------

_TURN_INTERPRETATION_REQUIRED_KEYS = frozenset(
    {
        "resolved_user_intent",
        "original_goal",
        "task_seed",
        "overall_goal",
        "continuation_mode",
        "step_reference_text",
        "step_reference_status",
        "resolved_step_title",
        "resolved_step_detail",
        "next_operational_goal",
        "execution_readiness",
        "blocking_reason",
        "success_condition",
        "explicit_constraints",
        "relevant_memory_fragments",
        "suggested_category_focus",
        "retrieval_hints",
    }
)


def _make_turn_interpretation(
    *,
    resolved_user_intent: str,
    original_goal: Optional[str] = None,
    task_seed: Optional[List[str]] = None,
    overall_goal: Optional[str] = None,
    continuation_mode: str = "new_request",
    step_reference_text: Optional[str] = None,
    step_reference_status: str = "none",
    resolved_step_title: Optional[str] = None,
    resolved_step_detail: Optional[str] = None,
    next_operational_goal: Optional[str] = None,
    execution_readiness: str = "ready",
    blocking_reason: Optional[str] = None,
    success_condition: Optional[str] = None,
    explicit_constraints: Optional[List[str]] = None,
    relevant_memory_fragments: Optional[List[str]] = None,
    suggested_category_focus: Optional[List[str]] = None,
    retrieval_hints: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build a fully-enumerated turn_interpretation payload for tests."""
    return {
        "resolved_user_intent": resolved_user_intent,
        "original_goal": original_goal,
        "task_seed": list(task_seed or []),
        "overall_goal": overall_goal,
        "continuation_mode": continuation_mode,
        "step_reference_text": step_reference_text,
        "step_reference_status": step_reference_status,
        "resolved_step_title": resolved_step_title,
        "resolved_step_detail": resolved_step_detail,
        "next_operational_goal": next_operational_goal,
        "execution_readiness": execution_readiness,
        "blocking_reason": blocking_reason,
        "success_condition": success_condition,
        "explicit_constraints": list(explicit_constraints or []),
        "relevant_memory_fragments": list(relevant_memory_fragments or []),
        "suggested_category_focus": list(suggested_category_focus or []),
        "retrieval_hints": list(retrieval_hints or []),
    }


def _structured_output_with_interpretation(
    *,
    label: str,
    turn_interpretation: Dict[str, Any],
    target_status: str = "resolved",
    resolved_target: Optional[str] = "10.129.29.165",
    target_source: str = "explicit_current_message",
    target_confidence: Optional[float] = 0.95,
    target_evidence: Optional[str] = "Target explicit in current user message.",
    prior_target_reuse: str = "disallow",
    prior_target_reuse_evidence: Optional[str] = None,
    prior_turn_reference: Optional[Dict[str, Any]] = None,
    suggested_capabilities: Optional[List[str]] = None,
    reasoning: str = "Stub classifier reasoning.",
) -> Dict[str, Any]:
    return {
        "label": label,
        "confidence": 0.9,
        "suggested_capabilities": list(suggested_capabilities or []),
        "requested_output_format": None,
        "question_type": "open_ended",
        "answer_style": "normal",
        "terminal_when": "all_steps_done",
        "risk_flags": [],
        "target_status": target_status,
        "resolved_target": resolved_target,
        "target_source": target_source,
        "target_confidence": target_confidence,
        "target_evidence": target_evidence,
        "prior_target_reuse": prior_target_reuse,
        "prior_target_reuse_evidence": prior_target_reuse_evidence,
        "prior_turn_reference": prior_turn_reference
        if prior_turn_reference is not None
        else {
            "required": False,
            "operation": "none",
            "status": "none",
            "confidence": None,
            "hints": [],
        },
        "turn_interpretation": turn_interpretation,
        "reasoning": reasoning,
    }


@pytest.mark.asyncio
async def test_intent_classifier_preserves_turn_interpretation_shape() -> None:
    """Shape lock: every required sub-field must survive pass-through."""
    interpretation = _make_turn_interpretation(
        resolved_user_intent="Scan the previously discussed host with nmap.",
        overall_goal="Investigate the target host end-to-end.",
        continuation_mode="continue_prior_work",
        next_operational_goal="Enumerate exposed services on 10.129.29.165.",
        execution_readiness="ready",
        success_condition="A service list for 10.129.29.165 is produced.",
        explicit_constraints=["stay within scope.md", "no exploit attempts"],
        relevant_memory_fragments=["Prior turn confirmed 10.129.29.165 is the target."],
        suggested_category_focus=["information_gathering"],
        retrieval_hints=["nmap service version", "host enumeration"],
    )
    payload = _structured_output_with_interpretation(
        label="direct_executor",
        turn_interpretation=interpretation,
        suggested_capabilities=["network_scan"],
    )
    stub = _StubUsageClient(content="", structured_output=payload)

    config = _runtime_config(
        {
            "eligible_routes": ["normal_chat", "simple_tool_execution"],
            "intent_hints": {"tool_hints": ["network_scan"], "targets": []},
            "risk_flags": [],
        },
        message="continue with the nmap scan on that host",
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    raw = config.metadata["intent_classifier_raw_response"]
    assert isinstance(raw, dict)
    assert "turn_interpretation" in raw
    ti = raw["turn_interpretation"]
    assert isinstance(ti, dict)
    # Fully enumerated nested object — every field in the locked contract is present.
    assert set(ti.keys()) == _TURN_INTERPRETATION_REQUIRED_KEYS
    assert ti["resolved_user_intent"] == interpretation["resolved_user_intent"]
    assert ti["continuation_mode"] == "continue_prior_work"
    assert ti["execution_readiness"] == "ready"
    assert ti["explicit_constraints"] == ["stay within scope.md", "no exploit attempts"]
    assert ti["suggested_category_focus"] == ["information_gathering"]
    # Existing target-resolution assertions remain intact.
    assert config.metadata["intent_target_resolution"]["target_status"] == "resolved"
    assert config.metadata["intent_target_resolution"]["resolved_target"] == "10.129.29.165"


@pytest.mark.asyncio
async def test_intent_classifier_resolves_vague_continuation_turn() -> None:
    """A vague ``ok do it`` turn distills into a concrete resolved intent."""
    interpretation = _make_turn_interpretation(
        resolved_user_intent="Proceed with the previously proposed nmap scan on 10.129.29.165.",
        overall_goal="Continue the prior host investigation.",
        continuation_mode="continue_prior_work",
        next_operational_goal="Run the queued nmap scan against 10.129.29.165.",
        execution_readiness="ready",
        relevant_memory_fragments=[
            "Assistant proposed running nmap -sV on 10.129.29.165 in the prior turn."
        ],
        retrieval_hints=["nmap -sV", "service enumeration"],
    )
    payload = _structured_output_with_interpretation(
        label="direct_executor",
        turn_interpretation=interpretation,
        suggested_capabilities=["network_scan"],
        prior_target_reuse="allow",
        prior_target_reuse_evidence="User approves the previously proposed action on the same host.",
    )
    stub = _StubUsageClient(content="", structured_output=payload)

    config = _runtime_config(
        {
            "eligible_routes": ["normal_chat", "simple_tool_execution"],
            "intent_hints": {"tool_hints": ["network_scan"], "targets": ["10.129.29.165"]},
            "risk_flags": [],
        },
        message="ok do it",
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    ti = config.metadata["intent_classifier_raw_response"]["turn_interpretation"]
    assert ti["continuation_mode"] == "continue_prior_work"
    assert "nmap" in ti["resolved_user_intent"].lower()
    assert ti["next_operational_goal"] is not None and len(ti["next_operational_goal"]) > 0
    assert ti["step_reference_status"] == "none"
    # Continuity assertion from the existing contract must still hold.
    assert config.metadata["intent_target_continuity"]["status"] == "allow"


@pytest.mark.asyncio
async def test_intent_classifier_resolves_step_reference() -> None:
    """``the second step`` must surface a resolved step title and detail."""
    interpretation = _make_turn_interpretation(
        resolved_user_intent="Execute the second step of the earlier plan against 10.129.29.165.",
        overall_goal="Continue investigating the target host.",
        continuation_mode="continue_prior_step",
        step_reference_text="second step",
        step_reference_status="resolved",
        resolved_step_title="Enumerate per-service",
        resolved_step_detail="Run service-focused enumeration on the exposed surface of 10.129.29.165.",
        next_operational_goal="Fingerprint and enumerate the exposed service surface on 10.129.29.165.",
        execution_readiness="ready",
        suggested_category_focus=["information_gathering", "web_applications"],
        retrieval_hints=["service enumeration", "http fingerprinting"],
    )
    payload = _structured_output_with_interpretation(
        label="direct_executor",
        turn_interpretation=interpretation,
        target_source="referential_history",
        target_evidence="Prior turns consistently discuss 10.129.29.165.",
        prior_target_reuse="allow",
        prior_target_reuse_evidence="Turn continues the earlier host investigation.",
        suggested_capabilities=["web_enumeration"],
    )
    stub = _StubUsageClient(content="", structured_output=payload)

    config = _runtime_config(
        {
            "eligible_routes": ["normal_chat", "simple_tool_execution"],
            "intent_hints": {"tool_hints": ["web_enumeration"], "targets": ["10.129.29.165"]},
            "risk_flags": [],
        },
        message="let's do the second step",
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    ti = config.metadata["intent_classifier_raw_response"]["turn_interpretation"]
    assert ti["step_reference_text"] == "second step"
    assert ti["step_reference_status"] == "resolved"
    assert ti["resolved_step_title"] == "Enumerate per-service"
    assert ti["resolved_step_detail"] is not None
    assert ti["continuation_mode"] == "continue_prior_step"
    # Existing target-continuity assertions must remain intact under the new schema.
    assert config.metadata["intent_target_continuity"]["status"] == "allow"


@pytest.mark.asyncio
async def test_intent_classifier_reports_ready_vs_blocked_execution_readiness() -> None:
    """``execution_readiness`` must reflect ready vs blocked with a reason."""
    ready_interpretation = _make_turn_interpretation(
        resolved_user_intent="Scan 10.129.29.165 with nmap -sV.",
        continuation_mode="new_request",
        next_operational_goal="Enumerate services on 10.129.29.165.",
        execution_readiness="ready",
    )
    ready_payload = _structured_output_with_interpretation(
        label="direct_executor",
        turn_interpretation=ready_interpretation,
        suggested_capabilities=["network_scan"],
    )
    ready_stub = _StubUsageClient(content="", structured_output=ready_payload)

    ready_config = _runtime_config(
        {
            "eligible_routes": ["normal_chat", "simple_tool_execution"],
            "intent_hints": {"tool_hints": ["network_scan"], "targets": ["10.129.29.165"]},
            "risk_flags": [],
        },
        message="nmap 10.129.29.165",
    )
    await IntentClassifier(
        client_factory=lambda call_settings: ready_stub,
    ).enrich_runtime_config(ready_config)

    ready_ti = ready_config.metadata["intent_classifier_raw_response"]["turn_interpretation"]
    assert ready_ti["execution_readiness"] == "ready"
    assert ready_ti["blocking_reason"] is None

    blocked_interpretation = _make_turn_interpretation(
        resolved_user_intent="Scan the target host the user is referring to.",
        continuation_mode="ambiguous",
        execution_readiness="blocked",
        blocking_reason="No concrete target resolved from the current turn or prior context.",
    )
    blocked_payload = _structured_output_with_interpretation(
        label="simple_chat",
        turn_interpretation=blocked_interpretation,
        target_status="unresolved",
        resolved_target=None,
        target_source="none",
        target_confidence=None,
        target_evidence="No explicit or referential target in the current request.",
        prior_target_reuse="disallow",
        prior_target_reuse_evidence="No continuity requested.",
        suggested_capabilities=[],
    )
    blocked_stub = _StubUsageClient(content="", structured_output=blocked_payload)

    blocked_config = _runtime_config(
        {
            "eligible_routes": ["normal_chat"],
            "intent_hints": {"tool_hints": [], "targets": []},
            "risk_flags": [],
        },
        message="scan it",
    )
    await IntentClassifier(
        client_factory=lambda call_settings: blocked_stub,
    ).enrich_runtime_config(blocked_config)

    blocked_ti = blocked_config.metadata["intent_classifier_raw_response"]["turn_interpretation"]
    assert blocked_ti["execution_readiness"] == "blocked"
    assert isinstance(blocked_ti["blocking_reason"], str)
    assert blocked_ti["blocking_reason"].strip() != ""
    # Existing target-resolution assertions remain intact.
    assert blocked_config.metadata["intent_target_resolution"]["target_status"] == "unresolved"
    assert blocked_config.metadata["intent_target_resolution"]["resolved_target"] is None


# ---------------------------------------------------------------------------
# Phase 1 schema lock: prior_turn_reference classifier hints.
#
# The classifier now emits resolver hints when the current turn depends on
# earlier transcript content. These hints are metadata only; later phases
# materialize canonical ChatMessage rows.
# ---------------------------------------------------------------------------


def _prior_turn_reference_none() -> Dict[str, Any]:
    """Build the safe empty prior-turn reference shape used by tests."""
    return {
        "required": False,
        "operation": "none",
        "status": "none",
        "confidence": None,
        "hints": [],
    }


@pytest.mark.asyncio
async def test_intent_classifier_persists_resolved_prior_turn_reference() -> None:
    """Resolved prior-turn references keep classifier resolver hints."""
    prior_reference = {
        "required": True,
        "operation": "reference_resolution",
        "status": "resolved",
        "confidence": 0.86,
        "hints": [
            {
                "reference_kind": "rendered_turn",
                "turn_number": 2,
                "speaker": "assistant",
                "anchor_text": "capture packets from that traffic",
                "reason": "The current user asks what that earlier phrase meant.",
                "confidence": 0.91,
            }
        ],
    }
    payload = _structured_output_with_interpretation(
        label="simple_chat",
        turn_interpretation=_make_turn_interpretation(
            resolved_user_intent="Explain the earlier assistant phrase.",
            continuation_mode="continue_prior_step",
            step_reference_text="that traffic",
            step_reference_status="resolved",
            execution_readiness="ready",
        ),
        target_status="unresolved",
        resolved_target=None,
        target_source="none",
        target_confidence=None,
        target_evidence=None,
        prior_turn_reference=prior_reference,
    )
    stub = _StubUsageClient(content="", structured_output=payload)
    config = _runtime_config(
        {
            "eligible_routes": ["normal_chat"],
            "intent_hints": {"tool_hints": [], "targets": []},
            "risk_flags": [],
        },
        message="What did you mean by capturing packets from that traffic?",
    )

    result = await IntentClassifier(
        client_factory=lambda call_settings: stub,
    ).enrich_runtime_config(config)

    assert result is not None
    assert config.metadata["intent_prior_turn_reference"] == prior_reference


@pytest.mark.asyncio
async def test_intent_classifier_persists_ambiguous_prior_turn_reference() -> None:
    """Ambiguous prior-turn references preserve non-authoritative hints."""
    prior_reference = {
        "required": True,
        "operation": "comparison",
        "status": "ambiguous",
        "confidence": 0.68,
        "hints": [
            {
                "reference_kind": "relative_turn",
                "turn_number": None,
                "speaker": "assistant",
                "anchor_text": "previous plan",
                "reason": "Multiple prior assistant plans could match.",
                "confidence": 0.61,
            }
        ],
    }
    payload = _structured_output_with_interpretation(
        label="simple_chat",
        turn_interpretation=_make_turn_interpretation(
            resolved_user_intent="Compare the current request with a prior plan.",
            continuation_mode="ambiguous",
            step_reference_text="previous plan",
            step_reference_status="ambiguous",
            execution_readiness="ambiguous",
        ),
        target_status="ambiguous",
        resolved_target=None,
        target_source="none",
        target_confidence=None,
        target_evidence=None,
        prior_turn_reference=prior_reference,
    )
    stub = _StubUsageClient(content="", structured_output=payload)
    config = _runtime_config(
        {
            "eligible_routes": ["normal_chat"],
            "intent_hints": {"tool_hints": [], "targets": []},
            "risk_flags": [],
        },
        message="Compare that with the previous plan.",
    )

    result = await IntentClassifier(
        client_factory=lambda call_settings: stub,
    ).enrich_runtime_config(config)

    assert result is not None
    reference = config.metadata["intent_prior_turn_reference"]
    assert reference["required"] is True
    assert reference["operation"] == "comparison"
    assert reference["status"] == "ambiguous"
    assert reference["hints"][0]["reference_kind"] == "relative_turn"


@pytest.mark.asyncio
async def test_intent_classifier_normalizes_no_prior_turn_reference_shape() -> None:
    """No-reference and malformed payloads collapse to the safe none shape."""
    payload = _structured_output_with_interpretation(
        label="direct_executor",
        turn_interpretation=_make_turn_interpretation(
            resolved_user_intent="Scan 10.0.0.5.",
            continuation_mode="new_request",
            next_operational_goal="Enumerate open ports on 10.0.0.5.",
        ),
        prior_turn_reference={
            "required": False,
            "operation": "reference_resolution",
            "status": "resolved",
            "confidence": 2.0,
            "hints": [{"reference_kind": "rendered_turn", "turn_number": 0}],
        },
    )
    stub = _StubUsageClient(content="", structured_output=payload)
    config = _runtime_config(
        {
            "eligible_routes": ["normal_chat", "simple_tool_execution"],
            "intent_hints": {"tool_hints": ["network_scan"], "targets": ["10.0.0.5"]},
            "risk_flags": [],
        },
        message="Scan 10.0.0.5.",
    )

    result = await IntentClassifier(
        client_factory=lambda call_settings: stub,
    ).enrich_runtime_config(config)

    assert result is not None
    assert config.metadata["intent_prior_turn_reference"] == _prior_turn_reference_none()


# ---------------------------------------------------------------------------
# Phase 1: intent-brief seed metadata.
# ---------------------------------------------------------------------------


from backend.services.langgraph_chat.intent.briefs import (  # noqa: E402
    METADATA_KEY_INTENT_BRIEF_SEED,
    METADATA_KEY_TURN_INTERPRETATION,
)


_BRIEF_FORBIDDEN_KEYS = frozenset(
    {
        "selected_tools",
        "tool_ids",
        "tool_id",
        "execution_strategy",
        "parameters",
        "tool_parameters",
        "recent_transcript",
        "transcript",
    }
)


def _assert_brief_has_no_transcript_or_tool_fields(payload: Any, path: str = "root") -> None:
    """Recursively verify a brief payload carries no forbidden keys."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            assert key not in _BRIEF_FORBIDDEN_KEYS, (
                f"Forbidden brief key {key!r} appeared at {path}"
            )
            _assert_brief_has_no_transcript_or_tool_fields(value, f"{path}.{key}")
    elif isinstance(payload, list):
        for idx, value in enumerate(payload):
            _assert_brief_has_no_transcript_or_tool_fields(value, f"{path}[{idx}]")


@pytest.mark.asyncio
async def test_intent_classifier_writes_intent_brief_seed_on_happy_path() -> None:
    """Classifier writes the unified seed key when structured output is populated."""
    interpretation = _make_turn_interpretation(
        resolved_user_intent="Scan the previously discussed host with nmap.",
        original_goal=(
            "Scan the previously discussed host with nmap and identify exposed services."
        ),
        task_seed=[
            "Scan the previously discussed host with nmap",
            "Identify exposed services",
        ],
        overall_goal="Investigate the target host end-to-end.",
        continuation_mode="continue_prior_work",
        next_operational_goal="Enumerate exposed services on 10.129.29.165.",
        execution_readiness="ready",
        success_condition="A service list for 10.129.29.165 is produced.",
        explicit_constraints=["stay within scope.md", "no exploit attempts"],
        relevant_memory_fragments=["Prior turn confirmed 10.129.29.165 is the target."],
        suggested_category_focus=["information_gathering"],
        retrieval_hints=["nmap service version", "host enumeration"],
    )
    payload = _structured_output_with_interpretation(
        label="direct_executor",
        turn_interpretation=interpretation,
        suggested_capabilities=["network_scan"],
    )
    stub = _StubUsageClient(content="", structured_output=payload)

    config = _runtime_config(
        {
            "eligible_routes": ["normal_chat", "simple_tool_execution"],
            "intent_hints": {"tool_hints": ["network_scan"], "targets": []},
            "risk_flags": [],
        },
        message="continue with the nmap scan on that host",
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None

    # intent_turn_interpretation is mirrored verbatim from the nested classifier payload.
    assert METADATA_KEY_TURN_INTERPRETATION in config.metadata
    mirrored = config.metadata[METADATA_KEY_TURN_INTERPRETATION]
    assert mirrored == interpretation

    seed = config.metadata[METADATA_KEY_INTENT_BRIEF_SEED]
    assert isinstance(seed, dict)

    # Load-bearing field values derived from the classifier interpretation.
    assert seed["resolved_user_intent"] == interpretation["resolved_user_intent"]
    assert seed["original_goal"] == interpretation["original_goal"]
    assert seed["task_seed"] == interpretation["task_seed"]
    assert seed["continuation_mode"] == "continue_prior_work"
    assert seed["execution_readiness"] == "ready"
    assert seed["resolved_step_title"] is None
    assert seed["resolved_step_detail"] is None
    assert seed["explicit_constraints"] == [
        "stay within scope.md",
        "no exploit attempts",
    ]
    assert seed["suggested_category_focus"] == ["information_gathering"]
    # Target slice picked up from the classifier's target resolution write.
    assert seed["target_status"] == "resolved"
    assert seed["resolved_target"] == "10.129.29.165"


@pytest.mark.asyncio
async def test_intent_classifier_writes_intent_brief_seed_on_skip_missing_llm_runtime() -> None:
    """Classifier skip paths still populate intent-brief seed with defaults."""
    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat", "simple_tool_execution"],
        "intent_hints": {"tool_hints": ["network_scan"], "targets": []},
        "risk_flags": [],
    }
    config = _runtime_config(
        metadata,
        api_key=None,
        message="Scan current docker to find online hosts",
    )

    classifier = IntentClassifier()
    result = await classifier.enrich_runtime_config(config)

    assert result is None
    assert config.metadata["intent_classifier_skipped"] == "missing_llm_runtime"

    assert METADATA_KEY_INTENT_BRIEF_SEED in config.metadata
    seed = config.metadata[METADATA_KEY_INTENT_BRIEF_SEED]

    # Builder-default values — the classifier never ran, so no payload to draw from.
    assert seed["continuation_mode"] == "ambiguous"
    assert seed["execution_readiness"] == "ambiguous"
    assert seed["resolved_user_intent"] is None
    assert seed["original_goal"] is None
    assert seed["task_seed"] == []
    assert seed["explicit_constraints"] == []
    assert seed["suggested_category_focus"] == []
    assert seed["target_status"] == "unresolved"
    assert seed["resolved_target"] is None
    assert seed["target_source"] == "none"


@pytest.mark.asyncio
async def test_intent_classifier_seed_carries_no_transcript_or_tool_fields() -> None:
    """Scope guard: seed payload must not leak transcript/tool-selection fields."""
    interpretation = _make_turn_interpretation(
        resolved_user_intent="Scan 10.129.29.165 with nmap -sV.",
        continuation_mode="new_request",
        next_operational_goal="Enumerate services on 10.129.29.165.",
        execution_readiness="ready",
        explicit_constraints=["no exploit"],
        retrieval_hints=["service enumeration"],
        suggested_category_focus=["information_gathering"],
    )
    payload = _structured_output_with_interpretation(
        label="direct_executor",
        turn_interpretation=interpretation,
        suggested_capabilities=["network_scan"],
    )
    stub = _StubUsageClient(content="", structured_output=payload)

    config = _runtime_config(
        {
            "eligible_routes": ["normal_chat", "simple_tool_execution"],
            "intent_hints": {"tool_hints": ["network_scan"], "targets": ["10.129.29.165"]},
            "risk_flags": [],
            # Inject forbidden keys at the metadata root to prove the builder
            # does not accidentally fan them out into the briefs.
            "selected_tools": ["nmap"],
            "execution_strategy": "sequential",
            "tool_parameters": {"nmap": {"-sV": True}},
        },
        message="nmap 10.129.29.165",
    )

    classifier = IntentClassifier(client_factory=lambda call_settings: stub)
    await classifier.enrich_runtime_config(config)

    _assert_brief_has_no_transcript_or_tool_fields(
        config.metadata[METADATA_KEY_INTENT_BRIEF_SEED],
        path=METADATA_KEY_INTENT_BRIEF_SEED,
    )


# ---------------------------------------------------------------------------
# Phase 2 Task 2.5: classifier prompt tests for plan and chat tiers.
#
# The assertions below pin the guide's recommended test matrix:
#   - plan-mode prompt contains forced route `plan_executor`
#   - chat-mode prompt contains forced route `simple_chat`
#   - no route-policy block when no forced tier exists
#   - classifier still runs for `plan` / `chat` (no `intent_classifier_skipped`)
# ---------------------------------------------------------------------------


class _PromptCapturingClient:
    """Deterministic stub that records the user prompt it receives.

    Used to assert that the classifier user prompt, as rendered by the
    ``core.prompts.builders.intent_classifier`` helper, embeds the
    conditional "Execution Route Policy" block exactly when metadata
    carries ``execution_route_policy`` — and does NOT embed the block
    otherwise.
    """

    def __init__(self, *, content: str) -> None:
        self.content = content
        self.calls = 0
        self.last_user_prompt: Optional[str] = None
        self.last_system_prompt: Optional[str] = None

    async def chat_with_usage(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        self.calls += 1
        self.last_system_prompt = kwargs.get("system_prompt")
        self.last_user_prompt = kwargs.get("user_prompt")
        return SimpleNamespace(
            content=self.content,
            usage=None,
            structured_output=None,
        )


def _plan_route_policy() -> Dict[str, Any]:
    return {
        "source": "agent_mode",
        "agent_mode": "plan",
        "forced_execution_mode": "deep_reasoning",
        "forced_classifier_label": "plan_executor",
    }


def _chat_route_policy() -> Dict[str, Any]:
    return {
        "source": "agent_mode",
        "agent_mode": "chat",
        "forced_execution_mode": "normal_chat",
        "forced_classifier_label": "simple_chat",
    }


@pytest.mark.asyncio
async def test_classifier_still_runs_for_plan_tier() -> None:
    """Task 2.5: `agent_mode=plan` must not skip classification.

    The guide's non-negotiable requirement #2 says `plan`/`chat` must
    not reuse `forced_capability` to achieve routing — that field
    currently short-circuits the classifier. Presence of an
    `execution_route_policy` must keep the classifier on the happy path
    so downstream brief generation still runs.
    """
    metadata = {
        "eligible_routes": ["normal_chat"],
        "intent_hints": {"tool_hints": [], "targets": []},
        "execution_route_policy": _plan_route_policy(),
    }
    config = _runtime_config(metadata)
    stub = _PromptCapturingClient(content='{"label": "simple_chat"}')

    classifier = IntentClassifier(client_factory=lambda call_settings: stub)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert stub.calls == 1
    # `intent_classifier_skipped` must not be written on the happy path.
    assert "intent_classifier_skipped" not in config.metadata


@pytest.mark.asyncio
async def test_classifier_still_runs_for_chat_tier() -> None:
    """Task 2.5: `agent_mode=chat` must not skip classification."""
    metadata = {
        "eligible_routes": ["normal_chat"],
        "intent_hints": {"tool_hints": [], "targets": []},
        "execution_route_policy": _chat_route_policy(),
    }
    config = _runtime_config(metadata)
    stub = _PromptCapturingClient(content='{"label": "simple_chat"}')

    classifier = IntentClassifier(client_factory=lambda call_settings: stub)
    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert stub.calls == 1
    assert "intent_classifier_skipped" not in config.metadata


@pytest.mark.asyncio
async def test_classifier_prompt_carries_plan_route_directive() -> None:
    """Task 2.5: plan-mode classifier prompt contains forced route `plan_executor`.

    The conditional route-policy block is authored by the dedicated
    prompt helper; this test asserts that the builder is actually wired
    into the classifier call — not just that the builder itself works
    in isolation.
    """
    metadata = {
        "eligible_routes": ["normal_chat"],
        "intent_hints": {"tool_hints": [], "targets": []},
        "execution_route_policy": _plan_route_policy(),
    }
    config = _runtime_config(metadata)
    stub = _PromptCapturingClient(content='{"label": "simple_chat"}')

    classifier = IntentClassifier(client_factory=lambda call_settings: stub)
    await classifier.enrich_runtime_config(config)

    prompt = stub.last_user_prompt or ""
    assert "Execution Route Policy:" in prompt
    assert "Forced routing label: plan_executor" in prompt
    assert "Policy source: agent_mode=plan" in prompt


@pytest.mark.asyncio
async def test_classifier_prompt_carries_chat_route_directive() -> None:
    """Task 2.5: chat-mode classifier prompt contains forced route `simple_chat`."""
    metadata = {
        "eligible_routes": ["normal_chat"],
        "intent_hints": {"tool_hints": [], "targets": []},
        "execution_route_policy": _chat_route_policy(),
    }
    config = _runtime_config(metadata)
    stub = _PromptCapturingClient(content='{"label": "simple_chat"}')

    classifier = IntentClassifier(client_factory=lambda call_settings: stub)
    await classifier.enrich_runtime_config(config)

    prompt = stub.last_user_prompt or ""
    assert "Execution Route Policy:" in prompt
    assert "Forced routing label: simple_chat" in prompt
    assert "Policy source: agent_mode=chat" in prompt


@pytest.mark.asyncio
async def test_classifier_prompt_omits_route_policy_without_forced_tier() -> None:
    """Task 2.5: no forced tier -> classifier prompt omits the block entirely.

    Negative control. The ``{route_policy_section}`` template slot must
    collapse to empty string when `execution_route_policy` is absent so
    the classifier prompt shape for `agent`/`agent_full` turns remains
    identical to the v7 prompt body.
    """
    metadata = {
        "eligible_routes": ["normal_chat"],
        "intent_hints": {"tool_hints": [], "targets": []},
    }
    config = _runtime_config(metadata)
    stub = _PromptCapturingClient(content='{"label": "simple_chat"}')

    classifier = IntentClassifier(client_factory=lambda call_settings: stub)
    await classifier.enrich_runtime_config(config)

    prompt = stub.last_user_prompt or ""
    assert "Execution Route Policy:" not in prompt
    assert "Forced routing label" not in prompt
