"""Tests for facade-owned pre-branch intent-phase reasoning streaming.

These checks lock the shared facade behavior that opens a Thinking phase
before branch selection, reuses the existing reasoning event contract, and
keeps the intent-phase reasoning group isolated from later graph reasoning.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock

import pytest

from agent.context.token_counter_registry import TokenEstimate
from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.context.contracts import CLASSIFIER_TRANSCRIPT_WINDOW_KEY
from agent.providers.llm.core.exceptions import LLMProfileNotFoundError
from backend.config import LANGGRAPH_INTENT_CLASSIFIER_TIMEOUT_SEC
from backend.services.langgraph_chat.compression.context_models import (
    CompressionRequiredError,
)
from backend.services.langgraph_chat.contracts import (
    ChatInputs,
    LangGraphChatResult,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.facade import LangGraphChatFacade
from backend.services.langgraph_chat.intent.classifier import IntentClassifier
from backend.services.langgraph_chat.model_role_registry import ModelRoleRegistry


@dataclass
class _ClassifierResult:
    usage: Any = None


class _StubContextBuilder:
    def __init__(self, runtime_config: LangGraphRuntimeConfig) -> None:
        self._runtime_config = runtime_config

    def build_runtime_config(
        self,
        *,
        chat_inputs: ChatInputs,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> LangGraphRuntimeConfig:
        _ = chat_inputs
        _ = metadata
        return self._runtime_config


class _StubHub:
    def __init__(self) -> None:
        self.events: list[Dict[str, Any]] = []

    async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
        self.events.append({"task_id": task_id, "event": event})


def _build_runtime_config(
    *,
    task_id: int = 55,
    api_key: Optional[str] = "test-key",
    deterministic_mode: bool = False,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> LangGraphRuntimeConfig:
    chat_inputs = ChatInputs(
        task_id=task_id,
        user_id=9,
        message="Scan the target",
        conversation_id=f"conv-{task_id}",
        history=[],
        api_key=api_key,
    )
    metadata: Dict[str, Any] = {
        "turn_id": f"task-{task_id}-turn-7",
        "turn_sequence": 7,
        "eligible_routes": ["normal_chat", "simple_tool_execution"],
        "intent_hints": {"tool_hints": ["network_scan"], "targets": []},
        "risk_flags": [],
        METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
            conversation_id=chat_inputs.conversation_id or "",
            turn_id=f"task-{task_id}-turn-7",
            turn_sequence=7,
            messages=list(chat_inputs.history),
        ),
    }
    if deterministic_mode:
        metadata["deterministic_mode"] = True
    if extra_metadata:
        metadata.update(extra_metadata)
    return LangGraphRuntimeConfig(
        chat_inputs=chat_inputs,
        metadata=metadata,
    )


def _build_facade(
    *,
    runtime_config: LangGraphRuntimeConfig,
    intent_classifier: Any,
    handler: AsyncMock,
    turn_compression_service: Any = None,
) -> LangGraphChatFacade:
    facade = LangGraphChatFacade(
        context_builder=_StubContextBuilder(runtime_config),
        intent_classifier=intent_classifier,
        turn_compression_service=turn_compression_service,
    )
    stub_handler = SimpleNamespace(handle=handler)
    facade._handlers = {branch: stub_handler for branch in facade._handlers}
    return facade


def _assert_intent_phase_event_contract(events: list[Dict[str, Any]], task_id: int) -> None:
    assert [entry["event"]["type"] for entry in events] == [
        "reasoning_start",
        "reasoning_delta",
        "reasoning_section_end",
    ]
    for entry in events:
        assert entry["task_id"] == task_id
        metadata = entry["event"]["metadata"]
        assert metadata["id"] == f"task-{task_id}-turn-7"
        assert metadata["turn_sequence"] == 7
        assert metadata["ind"] == 0
        assert metadata["sub_turn_index"] == -1
    assert events[0]["event"]["metadata"]["step"] == "intent_classification"
    assert events[1]["event"]["content"] == "Analyzing request and deciding execution path."
    assert events[2]["event"]["metadata"]["section_name"] == "intent_classification"


def test_facade_uses_configured_intent_classifier_timeout() -> None:
    facade = LangGraphChatFacade()
    assert facade._intent_classifier._client_timeout == LANGGRAPH_INTENT_CLASSIFIER_TIMEOUT_SEC


@pytest.mark.asyncio
async def test_facade_prepares_context_after_build_and_before_classifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    runtime_config = _build_runtime_config()
    runtime_config.chat_inputs.provider = "openai"
    runtime_config.chat_inputs.model = "gpt-5.2"
    runtime_config.chat_inputs.llm_runtime_selection = {
        "provider": "openai",
        "model": "gpt-5.2",
    }
    runtime_config.chat_inputs.history = [
        {"role": "user", "content": "prior message"}
    ]
    runtime_config.chat_inputs.history_source_message_ids = [101]
    live_bundle = runtime_config.metadata[METADATA_CONTEXT_BUNDLE_KEY]
    live_bundle["current_user_turn"] = {
        "role": "user",
        "content": runtime_config.chat_inputs.message,
    }
    live_transcript_window = live_bundle["transcript_window"]
    runtime_services = object()
    handoff: Dict[str, Any] = {}
    context_window = {
        "ceiling_reached": True,
        "recommended_next_action": "compress",
        "compression_candidate": True,
        "max_tokens": 128_000,
        "used_tokens": 1_000,
        "remaining_tokens": 127_000,
        "ratio": 1_000 / 128_000,
        "conversation_id": runtime_config.chat_inputs.conversation_id,
    }
    compression = {
        "applied": True,
        "candidate_request_fits": True,
        "snapshot_persisted": True,
    }
    accounted_requests: list[Dict[str, Any]] = []

    class _RecordingContextBuilder(_StubContextBuilder):
        def build_runtime_config(self, **kwargs: Any) -> LangGraphRuntimeConfig:
            order.append("context")
            return super().build_runtime_config(**kwargs)

    class _RecordingCompressionService:
        async def prepare_preturn_history(self, **kwargs: Any) -> Any:
            order.append("compression")
            assert kwargs["history"] == list(runtime_config.chat_inputs.history)
            assert kwargs["history_source_message_ids"] == [101]
            assert kwargs["turn_id"] == "task-55-turn-7"
            assert kwargs["provider"] == "openai"
            assert kwargs["model"] == "gpt-5.2"
            assert kwargs["context_limit_tokens"] == 128_000
            assert kwargs["request_prompt_tokens"] == 7_321
            assert kwargs["reserved_output_tokens"] == 32_000
            assert (
                kwargs["llm_runtime_selection"]
                is runtime_config.chat_inputs.llm_runtime_selection
            )
            assert kwargs["runtime_services"] is runtime_services
            candidate_history = [
                {"role": "system", "content": "candidate summary"},
                {"role": "user", "content": "retained question"},
                {"role": "assistant", "content": "retained answer"},
            ]
            assert kwargs["candidate_classifier_prompt_counter"](
                candidate_history
            ) == 4_321
            order.append("persist")
            return list(kwargs["history"]), context_window, compression, True

        @staticmethod
        def context_window_handoff_fields(metadata: Any) -> Dict[str, Any]:
            return (
                {"context_window": dict(metadata)} if isinstance(metadata, dict) else {}
            )

    intent_classifier = IntentClassifier(
        model_role_registry=ModelRoleRegistry(env_getter=lambda _name: None)
    )
    original_prepare_request = intent_classifier.prepare_request
    prepare_calls = []

    def _prepare_request(*args: Any, **kwargs: Any) -> Any:
        request = original_prepare_request(*args, **kwargs)
        prepare_calls.append(request)
        return request

    intent_classifier.prepare_request = _prepare_request
    resolved_settings = []
    resolved_requests = []

    async def _classify(
        config: LangGraphRuntimeConfig,
        *,
        call_settings: Any,
        prepared_request: Any,
    ) -> _ClassifierResult:
        order.append("classifier")
        resolved_settings.append(call_settings)
        resolved_requests.append(prepared_request)
        assert config.metadata["context_window"] == context_window
        assert config.metadata["compression"] == compression
        return _ClassifierResult()

    intent_classifier.enrich_runtime_config = AsyncMock(side_effect=_classify)

    async def _handle(config: LangGraphRuntimeConfig) -> LangGraphChatResult:
        order.append("handler")
        return LangGraphChatResult(
            final_text="done",
            conversation_id=config.chat_inputs.conversation_id,
        )

    facade = LangGraphChatFacade(
        context_builder=_RecordingContextBuilder(runtime_config),
        intent_classifier=intent_classifier,
        turn_compression_service=_RecordingCompressionService(),
    )
    handler = SimpleNamespace(handle=AsyncMock(side_effect=_handle))
    facade._handlers = {branch: handler for branch in facade._handlers}
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _StubHub(),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.intent.classifier._load_environment_section",
        lambda *_args, **_kwargs: "",
    )
    def _estimate_classifier_request(**kwargs: Any) -> TokenEstimate:
        accounted_requests.append(dict(kwargs))
        tokens = 4_321 if "candidate summary" in kwargs["user_prompt"] else 7_321
        return TokenEstimate(
            tokens=tokens,
            provider=kwargs["provider"],
            model=kwargs["model"],
            strategy="test",
            precision="exact",
        )

    monkeypatch.setattr(
        "backend.services.langgraph_chat.facade.estimate_llm_request_tokens",
        _estimate_classifier_request,
    )

    await facade.handle_turn(
        runtime_config.chat_inputs,
        runtime_services=runtime_services,
        pre_classifier_context_handoff=handoff,
    )

    assert order == ["context", "compression", "persist", "classifier", "handler"]
    assert len(resolved_settings) == 1
    assert resolved_settings[0].provider == "openai"
    assert resolved_settings[0].model == "gpt-5.2"
    assert len(resolved_requests) == 1
    request = resolved_requests[0]
    assert len(prepare_calls) == 2
    assert prepare_calls[0] is not request
    assert prepare_calls[1] is request
    assert request.call_settings is resolved_settings[0]
    assert accounted_requests[0] == {
        "system_prompt": prepare_calls[0].system_prompt,
        "user_prompt": prepare_calls[0].user_prompt,
        "structured_output": prepare_calls[0].structured_output,
        "provider": prepare_calls[0].call_settings.provider,
        "model": prepare_calls[0].call_settings.model,
    }
    assert accounted_requests[1] == {
        "system_prompt": prepare_calls[1].system_prompt,
        "user_prompt": prepare_calls[1].user_prompt,
        "structured_output": prepare_calls[1].structured_output,
        "provider": prepare_calls[1].call_settings.provider,
        "model": prepare_calls[1].call_settings.model,
    }
    assert "candidate summary" in prepare_calls[1].user_prompt
    assert "retained question" in prepare_calls[1].user_prompt
    assert runtime_config.chat_inputs.message in prepare_calls[1].user_prompt
    assert live_bundle["transcript_window"] is live_transcript_window
    assert live_bundle[CLASSIFIER_TRANSCRIPT_WINDOW_KEY]["turns"] == [
        {"role": "system", "content": "candidate summary"},
        {"role": "user", "content": "retained question"},
        {"role": "assistant", "content": "retained answer"},
    ]
    assert runtime_config.metadata[METADATA_CONTEXT_BUNDLE_KEY] is live_bundle
    assert resolved_requests == [request]
    assert handoff == {
        "context_window": context_window,
        "compression": compression,
        "context_event_emitted": True,
    }


@pytest.mark.asyncio
async def test_facade_rejects_unsupported_accounting_before_classifier_send() -> None:
    """Unknown selected profiles fail accounting without model rerouting or send."""
    runtime_config = _build_runtime_config()
    runtime_config.chat_inputs.provider = "unknown-provider"
    runtime_config.chat_inputs.model = "unknown-model"
    runtime_config.chat_inputs.llm_runtime_selection = {
        "provider": "unknown-provider",
        "model": "unknown-model",
    }
    classifier = IntentClassifier(
        model_role_registry=ModelRoleRegistry(env_getter=lambda _name: None)
    )
    classifier.enrich_runtime_config = AsyncMock()
    compression = AsyncMock()
    compression.prepare_preturn_history = AsyncMock()
    handler = AsyncMock()
    facade = _build_facade(
        runtime_config=runtime_config,
        intent_classifier=classifier,
        handler=handler,
        turn_compression_service=compression,
    )

    with pytest.raises(LLMProfileNotFoundError):
        await facade.handle_turn(runtime_config.chat_inputs)

    classifier.enrich_runtime_config.assert_not_awaited()
    compression.prepare_preturn_history.assert_not_awaited()
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_facade_context_uncompactable_stops_before_classifier_and_live_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Minimum-tail failure leaves live context untouched and never sends."""
    runtime_config = _build_runtime_config()
    runtime_config.chat_inputs.provider = "openai"
    runtime_config.chat_inputs.model = "gpt-5.2"
    live_bundle = runtime_config.metadata[METADATA_CONTEXT_BUNDLE_KEY]
    shared_window = live_bundle["transcript_window"]
    classifier_window = live_bundle[CLASSIFIER_TRANSCRIPT_WINDOW_KEY]
    classifier = IntentClassifier(
        model_role_registry=ModelRoleRegistry(env_getter=lambda _name: None)
    )
    classifier.enrich_runtime_config = AsyncMock()
    compression = SimpleNamespace(
        prepare_preturn_history=AsyncMock(
            side_effect=CompressionRequiredError(
                reason="context_uncompactable",
                detail="summary plus three complete turns exceeds context limit",
            )
        ),
        context_window_handoff_fields=lambda _metadata: {},
    )
    handler = AsyncMock()
    facade = _build_facade(
        runtime_config=runtime_config,
        intent_classifier=classifier,
        handler=handler,
        turn_compression_service=compression,
    )
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _StubHub(),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.intent.classifier._load_environment_section",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.facade.estimate_llm_request_tokens",
        lambda **kwargs: TokenEstimate(
            tokens=900,
            provider=kwargs["provider"],
            model=kwargs["model"],
            strategy="test",
            precision="exact",
        ),
    )

    with pytest.raises(CompressionRequiredError) as exc_info:
        await facade.handle_turn(runtime_config.chat_inputs)

    assert exc_info.value.reason == "context_uncompactable"
    assert live_bundle["transcript_window"] is shared_window
    assert live_bundle[CLASSIFIER_TRANSCRIPT_WINDOW_KEY] is classifier_window
    classifier.enrich_runtime_config.assert_not_awaited()
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_facade_streams_intent_phase_before_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_config = _build_runtime_config()
    hub = _StubHub()
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: hub,
    )

    intent_classifier = AsyncMock()
    intent_classifier.enrich_runtime_config = AsyncMock(return_value=_ClassifierResult())

    async def _handle(config: LangGraphRuntimeConfig) -> LangGraphChatResult:
        assert config is runtime_config
        assert (
            config.metadata["intent_phase_reasoning_text"]
            == "Analyzing request and deciding execution path."
        )
        _assert_intent_phase_event_contract(hub.events, runtime_config.chat_inputs.task_id)
        return LangGraphChatResult(final_text="done", conversation_id=config.chat_inputs.conversation_id)

    handler = AsyncMock(side_effect=_handle)
    facade = _build_facade(
        runtime_config=runtime_config,
        intent_classifier=intent_classifier,
        handler=handler,
    )

    result = await facade.handle_turn(runtime_config.chat_inputs)

    assert result.final_text == "done"
    intent_classifier.enrich_runtime_config.assert_awaited_once_with(runtime_config)
    _assert_intent_phase_event_contract(hub.events, runtime_config.chat_inputs.task_id)


@pytest.mark.asyncio
async def test_facade_streams_intent_phase_in_deterministic_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_config = _build_runtime_config(deterministic_mode=True)
    hub = _StubHub()
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: hub,
    )

    intent_classifier = AsyncMock()
    intent_classifier.enrich_runtime_config = AsyncMock()
    compression_service = SimpleNamespace(
        prepare_preturn_history=AsyncMock(
            side_effect=RuntimeError("deterministic mode must bypass compaction")
        ),
        context_window_handoff_fields=lambda _metadata: {},
    )

    async def _handle(config: LangGraphRuntimeConfig) -> LangGraphChatResult:
        assert config.metadata["intent_classifier_skipped"] == "deterministic_mode"
        _assert_intent_phase_event_contract(hub.events, runtime_config.chat_inputs.task_id)
        return LangGraphChatResult(final_text="done", conversation_id=config.chat_inputs.conversation_id)

    handler = AsyncMock(side_effect=_handle)
    facade = _build_facade(
        runtime_config=runtime_config,
        intent_classifier=intent_classifier,
        handler=handler,
        turn_compression_service=compression_service,
    )

    await facade.handle_turn(runtime_config.chat_inputs)

    intent_classifier.enrich_runtime_config.assert_not_called()
    compression_service.prepare_preturn_history.assert_not_awaited()
    _assert_intent_phase_event_contract(hub.events, runtime_config.chat_inputs.task_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra_metadata", "api_key", "client_factory", "expected_skip_reason"),
    [
        ({"forced_capability": "respond_only"}, "test-key", lambda *_: None, "forced_capability"),
        ({}, None, None, "missing_llm_runtime"),
        (
            {},
            "test-key",
            lambda *_: SimpleNamespace(
                chat_with_usage=AsyncMock(side_effect=RuntimeError("boom")),
                chat=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            "llm_error",
        ),
    ],
)
async def test_facade_streams_intent_phase_for_classifier_skip_paths(
    monkeypatch: pytest.MonkeyPatch,
    extra_metadata: Dict[str, Any],
    api_key: Optional[str],
    client_factory: Any,
    expected_skip_reason: str,
) -> None:
    runtime_config = _build_runtime_config(api_key=api_key, extra_metadata=extra_metadata)
    hub = _StubHub()
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: hub,
    )

    classifier = IntentClassifier(client_factory=client_factory)

    async def _handle(config: LangGraphRuntimeConfig) -> LangGraphChatResult:
        assert config.metadata["intent_classifier_skipped"] == expected_skip_reason
        _assert_intent_phase_event_contract(hub.events, runtime_config.chat_inputs.task_id)
        return LangGraphChatResult(final_text="done", conversation_id=config.chat_inputs.conversation_id)

    handler = AsyncMock(side_effect=_handle)
    facade = _build_facade(
        runtime_config=runtime_config,
        intent_classifier=classifier,
        handler=handler,
    )

    await facade.handle_turn(runtime_config.chat_inputs)

    _assert_intent_phase_event_contract(hub.events, runtime_config.chat_inputs.task_id)


@pytest.mark.asyncio
async def test_facade_closes_intent_phase_when_classifier_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_config = _build_runtime_config()
    hub = _StubHub()
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: hub,
    )

    classifier = AsyncMock()
    classifier.enrich_runtime_config = AsyncMock(side_effect=RuntimeError("classifier failed"))

    facade = _build_facade(
        runtime_config=runtime_config,
        intent_classifier=classifier,
        handler=AsyncMock(),
    )

    with pytest.raises(RuntimeError, match="classifier failed"):
        await facade.handle_turn(runtime_config.chat_inputs)

    _assert_intent_phase_event_contract(hub.events, runtime_config.chat_inputs.task_id)
