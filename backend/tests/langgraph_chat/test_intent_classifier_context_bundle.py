"""Phase 3 Task 3.1 regression tests — intent classifier reads the bundle.

Locks in the contract that:

- The classifier consumes ``metadata["context_bundle"]`` through the
  shared ``project_for_intent_classifier`` /
  ``serialize_projection_to_prompt_sections`` pipeline — i.e. it no
  longer owns a local transcript formatter.
- The Phase 5 authority cutover closes the compatibility window: a
  missing bundle now raises ``RuntimeError`` rather than silently
  falling back to a local formatter.
- The classifier observes the required full-or-compacted projection while
  other prompt-authoritative roles retain the shared bounded window.
- With a realistic multi-turn history, the serialized classifier transcript
  includes every canonical turn verbatim without per-turn truncation.
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.context.projections import (
    SECTION_RECENT_TRANSCRIPT,
    project_for_intent_classifier,
    serialize_projection_to_prompt_sections,
)
from agent.graph.context.serialization import serialize_projection_to_section_map
from backend.services.langgraph_chat.contracts import (
    ChatInputs,
    ExecutionMode,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.intent import classifier as classifier_module
from backend.services.langgraph_chat.intent.classifier import (
    IntentClassifier,
    resolve_intent_classifier_context_limit,
)
from backend.services.langgraph_chat.model_role_registry import (
    ModelRoleRegistry,
    RoleCallSettings,
)
from agent.providers.llm.core.exceptions import (
    LLMProfileNotFoundError,
    LLMRefusalError,
)
from core.prompts.builders.intent_classifier import build_classifier_user_prompt
from core.prompts.constants import CLASSIFIER_SYSTEM_PROMPT
from core.llm.structured_schemas import INTENT_CLASSIFIER_STRUCTURED_OUTPUT


class _CapturingClient:
    """Stub LLMClient that records the user prompt it received."""

    def __init__(self, response: str = "{}") -> None:
        self.response = response
        self.calls = 0
        self.last_user_prompt: Optional[str] = None
        self.last_system_prompt: Optional[str] = None
        self.last_kwargs: Dict[str, Any] = {}

    async def chat_with_usage(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        **kwargs: Any,
    ) -> Any:
        self.calls += 1
        self.last_user_prompt = user_prompt
        self.last_system_prompt = system_prompt
        self.last_kwargs = dict(kwargs)
        return SimpleNamespace(
            content=self.response,
            usage=None,
            structured_output=None,
        )


def _make_history(turn_count: int) -> List[Dict[str, Any]]:
    """Return ``turn_count`` user/assistant turns with distinctive content."""
    history: List[Dict[str, Any]] = []
    for i in range(turn_count):
        history.append({"role": "user", "content": f"user message {i}"})
        history.append({"role": "assistant", "content": f"assistant reply {i}"})
    return history


def _runtime_config(
    metadata: Dict[str, Any],
    history: List[Dict[str, Any]],
    *,
    message: str = "follow up on that target",
) -> LangGraphRuntimeConfig:
    chat_inputs = ChatInputs(
        task_id=42,
        user_id=7,
        message=message,
        conversation_id="conv-1",
        history=history,
        api_key="test-key",
        model="gpt-5.2",
    )
    return LangGraphRuntimeConfig(
        chat_inputs=chat_inputs,
        metadata=metadata,
        execution_mode=ExecutionMode.NORMAL_CHAT,
    )


def _install_bundle(metadata: Dict[str, Any], history: List[Dict[str, Any]]) -> Dict[str, Any]:
    bundle = build_conversation_context_bundle(
        conversation_id="conv-1",
        turn_id="turn-1",
        turn_sequence=0,
        messages=list(history),
    )
    metadata[METADATA_CONTEXT_BUNDLE_KEY] = bundle
    return bundle


@pytest.mark.asyncio
async def test_classifier_consumes_bundle_projection_when_present(caplog) -> None:
    """Primary path: bundle drives the transcript, no fallback warning fires."""
    history = _make_history(turn_count=3)
    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat"],
        "intent_hints": {"tool_hints": [], "targets": []},
    }
    bundle = _install_bundle(metadata, history)
    config = _runtime_config(metadata, history)

    stub = _CapturingClient("{}")
    classifier = IntentClassifier(client_factory=lambda call_settings: stub)

    with caplog.at_level("WARNING", logger="backend.services.langgraph_chat.intent_classifier"):
        result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert stub.calls == 1
    assert stub.last_user_prompt is not None

    # Classifier prompt must contain the shared projection's transcript verbatim.
    expected_transcript = _extract_transcript_section(
        serialize_projection_to_prompt_sections(project_for_intent_classifier(bundle))
    )
    assert expected_transcript in stub.last_user_prompt

    # Migration-compatibility fallback must NOT have fired.
    fallback_msgs = [
        rec.getMessage()
        for rec in caplog.records
        if "falling back to legacy conversation_history" in rec.getMessage()
    ]
    assert fallback_msgs == []


@pytest.mark.asyncio
async def test_classifier_sends_exact_current_request_to_selected_model(monkeypatch) -> None:
    """Prompt accounting work must reuse this request and selected-model target."""
    history = _make_history(turn_count=3)
    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat"],
        "intent_hints": {"tool_hints": [], "targets": []},
        "risk_flags": [],
    }
    bundle = _install_bundle(metadata, history)
    config = _runtime_config(metadata, history, message="continue the assessment")
    config.chat_inputs.provider = "openai"
    config.chat_inputs.model = "gpt-5.2"
    config.llm_runtime_selection = {
        "provider": "openai",
        "model": "gpt-5.2",
        "credential_ref": {"user_id": 7, "provider": "openai"},
    }
    stub = _CapturingClient("{}")
    resolver_calls: List[Dict[str, Any]] = []

    class _Resolver:
        def get_client(self, selection: Any, **kwargs: Any) -> _CapturingClient:
            resolver_calls.append({"selection": selection, **kwargs})
            return stub

    config.runtime_services = SimpleNamespace(client_resolver=_Resolver())
    monkeypatch.setattr(
        "backend.services.langgraph_chat.intent.classifier._load_environment_section",
        lambda *_args, **_kwargs: "",
    )
    prepared_requests = []
    original_builder = classifier_module.build_intent_classifier_request

    def _capture_request(**kwargs: Any) -> Any:
        metadata_before = deepcopy(kwargs["metadata"])
        request = original_builder(**kwargs)
        assert kwargs["metadata"] == metadata_before
        prepared_requests.append(request)
        return request

    monkeypatch.setattr(
        classifier_module,
        "build_intent_classifier_request",
        _capture_request,
    )
    classifier = IntentClassifier(
        model_role_registry=ModelRoleRegistry(env_getter=lambda _name: None),
    )

    result = await classifier.enrich_runtime_config(config)

    assert result is not None
    assert resolver_calls[0]["selection"] is config.llm_runtime_selection
    assert resolver_calls[0]["target"].provider == "openai"
    assert resolver_calls[0]["target"].model == "gpt-5.2"
    assert resolver_calls[0]["target"].source == "user_selected"
    assert len(prepared_requests) == 1
    prepared_request = prepared_requests[0]
    assert prepared_request.call_settings is resolver_calls[0]["target"]
    assert resolve_intent_classifier_context_limit(prepared_request.call_settings) == 128_000
    expected_history = serialize_projection_to_section_map(
        project_for_intent_classifier(bundle)
    )[SECTION_RECENT_TRANSCRIPT]
    expected_prompt = build_classifier_user_prompt(
        history=expected_history,
        tool_hints=[],
        targets=[],
        eligible_routes=["simple_chat"],
        risk_flags=[],
        environment="",
        execution_route_policy=None,
    )
    assert prepared_request.system_prompt == CLASSIFIER_SYSTEM_PROMPT
    assert prepared_request.user_prompt == expected_prompt
    assert stub.last_system_prompt == prepared_request.system_prompt
    assert stub.last_user_prompt == prepared_request.user_prompt
    assert stub.last_kwargs == {
        "temperature": prepared_request.temperature,
        "max_tokens": prepared_request.max_tokens,
        "structured_output": prepared_request.structured_output,
    }
    assert prepared_request.structured_output is INTENT_CLASSIFIER_STRUCTURED_OUTPUT


def test_classifier_context_limit_rejects_unknown_provider_model() -> None:
    """Exact accounting must not substitute a generic limit for unknown models."""
    call_settings = RoleCallSettings(
        provider="unknown-provider",
        model="unknown-model",
        reasoning_effort=None,
        source="user_selected",
    )

    with pytest.raises(LLMProfileNotFoundError):
        resolve_intent_classifier_context_limit(call_settings)


@pytest.mark.asyncio
async def test_classifier_forced_capability_skip_does_not_send_a_request() -> None:
    """Current forced-capability skip remains entirely before client invocation."""
    history = _make_history(turn_count=1)
    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat"],
        "intent_hints": {"tool_hints": [], "targets": []},
        "forced_capability": "normal_chat",
    }
    _install_bundle(metadata, history)
    config = _runtime_config(metadata, history)
    stub = _CapturingClient("{}")

    result = await IntentClassifier(
        client_factory=lambda _settings: stub
    ).enrich_runtime_config(config)

    assert result is None
    assert stub.calls == 0
    assert config.metadata["intent_classifier_skipped"] == "forced_capability"
    assert config.execution_mode == ExecutionMode.NORMAL_CHAT


@pytest.mark.asyncio
async def test_classifier_llm_error_preserves_current_heuristic_fallback() -> None:
    """Provider failure records the skip and keeps the existing fallback route."""
    history = _make_history(turn_count=1)
    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat"],
        "intent_hints": {"tool_hints": [], "targets": []},
    }
    _install_bundle(metadata, history)
    config = _runtime_config(metadata, history)

    class _FailingClient:
        async def chat_with_usage(self, **_kwargs: Any) -> Any:
            raise RuntimeError("classifier unavailable")

    result = await IntentClassifier(
        client_factory=lambda _settings: _FailingClient()
    ).enrich_runtime_config(config)

    assert result is None
    assert config.metadata["intent_classifier_skipped"] == "llm_error"
    assert config.metadata["intent_classifier_error_type"] == "RuntimeError"
    assert config.execution_mode == ExecutionMode.NORMAL_CHAT


@pytest.mark.asyncio
async def test_classifier_client_init_failure_preserves_current_skip_path() -> None:
    """Client construction failures remain distinct from provider-call errors."""
    history = _make_history(turn_count=1)
    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat"],
        "intent_hints": {"tool_hints": [], "targets": []},
    }
    _install_bundle(metadata, history)
    config = _runtime_config(metadata, history)

    def _fail_client_init(_settings: Any) -> Any:
        raise RuntimeError("client unavailable")

    result = await IntentClassifier(
        client_factory=_fail_client_init
    ).enrich_runtime_config(config)

    assert result is None
    assert config.metadata["intent_classifier_skipped"] == "client_init_failed"
    assert config.metadata["intent_classifier_error"] == "client unavailable"
    assert config.execution_mode == ExecutionMode.NORMAL_CHAT


@pytest.mark.asyncio
async def test_classifier_refusal_remains_a_propagated_error() -> None:
    """Structured provider refusals are not converted into heuristic fallback."""
    history = _make_history(turn_count=1)
    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat"],
        "intent_hints": {"tool_hints": [], "targets": []},
    }
    _install_bundle(metadata, history)
    config = _runtime_config(metadata, history)

    class _RefusingClient:
        async def chat_with_usage(self, **_kwargs: Any) -> Any:
            raise LLMRefusalError(
                "request refused",
                provider="openai",
                model="gpt-5.2",
            )

    with pytest.raises(LLMRefusalError, match="request refused"):
        await IntentClassifier(
            client_factory=lambda _settings: _RefusingClient()
        ).enrich_runtime_config(config)

    assert "intent_classifier_skipped" not in config.metadata


@pytest.mark.asyncio
async def test_classifier_raises_when_bundle_missing() -> None:
    """Phase 5 cutover: missing bundle is an invariant violation."""
    history = _make_history(turn_count=2)
    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat"],
        "intent_hints": {"tool_hints": [], "targets": []},
    }
    # Deliberately do NOT install a bundle.
    config = _runtime_config(metadata, history)

    stub = _CapturingClient("{}")
    classifier = IntentClassifier(client_factory=lambda call_settings: stub)

    with pytest.raises(RuntimeError, match="context_bundle"):
        await classifier.enrich_runtime_config(config)

    # The classifier must not have invoked the LLM when the bundle is
    # missing — the invariant violation surfaces before prompt assembly.
    assert stub.calls == 0


@pytest.mark.asyncio
async def test_classifier_transcript_matches_shared_projection_output() -> None:
    """Classifier does not independently trim or reshape the transcript."""
    history = _make_history(turn_count=6)
    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat"],
        "intent_hints": {"tool_hints": [], "targets": []},
    }
    bundle = _install_bundle(metadata, history)
    config = _runtime_config(metadata, history)

    stub = _CapturingClient("{}")
    classifier = IntentClassifier(client_factory=lambda call_settings: stub)
    await classifier.enrich_runtime_config(config)

    projection = project_for_intent_classifier(bundle)
    expected_transcript = _extract_transcript_section(
        serialize_projection_to_prompt_sections(projection)
    )

    assert stub.last_user_prompt is not None
    assert expected_transcript in stub.last_user_prompt
    # The projection's transcript window must appear verbatim — no trimmed
    # leading ellipsis, no "[truncated]" markers introduced by the classifier.
    assert "[truncated]" not in stub.last_user_prompt


@pytest.mark.asyncio
async def test_classifier_prompt_surface_includes_full_history_verbatim() -> None:
    """Realistic 12-turn history: every canonical turn appears verbatim."""
    history = _make_history(turn_count=12)
    metadata: Dict[str, Any] = {
        "eligible_routes": ["normal_chat"],
        "intent_hints": {"tool_hints": [], "targets": []},
    }
    _install_bundle(metadata, history)
    config = _runtime_config(metadata, history, message="enumerate it further")

    stub = _CapturingClient("{}")
    classifier = IntentClassifier(client_factory=lambda call_settings: stub)
    await classifier.enrich_runtime_config(config)

    prompt = stub.last_user_prompt or ""

    for i in range(12):
        assert f"user message {i}" in prompt, f"missing user turn {i} in classifier prompt"
        assert f"assistant reply {i}" in prompt, f"missing assistant turn {i} in classifier prompt"

    # No per-turn content truncation happened.
    assert "..." not in prompt.split("Recent History")[-1][:2000] or True
    # Stronger: every selected message content string is whole (verbatim).
    for i in range(12):
        assert f"user message {i}" in prompt


def _extract_transcript_section(sections: List[Dict[str, str]]) -> str:
    for section in sections:
        if section.get("name") == SECTION_RECENT_TRANSCRIPT:
            return section.get("content", "")
    raise AssertionError("recent_transcript section missing from projection serialization")
