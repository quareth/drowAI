"""Tests for checkpoint continuation helper contracts.

These checks cover helper-level contracts used by resume/retry orchestration
after facade internals delegate checkpoint continuation to focused services.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import patch

import pytest

from backend.services.langgraph_chat.checkpoint.continuation_service import (
    CheckpointContinuationService,
    extract_resume_conversation_id,
)
from backend.services.langgraph_chat.checkpoint.execution_config import (
    build_checkpoint_execution_config,
    resolve_resume_runtime_path_label,
)
from backend.services.langgraph_chat.exceptions import HITLError
from backend.services.llm_provider.types import ProviderConfigurationError

GRAPH_THREAD_ID = "a" * 32
DEPLOYMENT_ID = "11111111-1111-4111-8111-111111111111"
ALT_DEPLOYMENT_ID = "22222222-2222-4222-8222-222222222222"


def _v2_selection(
    *,
    deployment_id: str = DEPLOYMENT_ID,
    model: str = "gpt-5.2",
    reasoning_effort: str | None = None,
) -> Dict[str, Any]:
    selection: Dict[str, Any] = {
        "schema_version": 2,
        "deployment_ref": {
            "deployment_id": deployment_id,
            "expected_revision": 1,
        },
        "legacy_provider": "openai",
        "legacy_model": model,
    }
    if reasoning_effort is not None:
        selection["reasoning_effort"] = reasoning_effort
    return selection


def test_build_checkpoint_execution_config_includes_required_fields() -> None:
    """Checkpoint config always includes thread and graph identity."""
    config = build_checkpoint_execution_config(
        task_id=7,
        graph_thread_id=GRAPH_THREAD_ID,
        graph_name="simple_tool",
    )
    assert config["configurable"]["thread_id"] == f"graph-{GRAPH_THREAD_ID}"
    assert config["configurable"]["graph_name"] == "simple_tool"
    assert "runtime_path" in config["configurable"]


def test_build_checkpoint_execution_config_carries_provider_runtime_for_invocation() -> None:
    """Resume config carries non-secret LLM runtime plus live invocation services."""
    runtime_services = object()
    selection = _v2_selection(reasoning_effort="medium")

    config = build_checkpoint_execution_config(
        task_id=7,
        graph_thread_id=GRAPH_THREAD_ID,
        user_id=9,
        tenant_id=3,
        runtime_placement_mode="local",
        workspace_id="task-7",
        actor_type="agent",
        actor_id="langgraph",
        runner_id="runner-a",
        execution_site_id="site-a",
        graph_name="simple_tool",
        llm_runtime_selection=selection,
        runtime_services=runtime_services,
    )

    configurable = config["configurable"]
    assert configurable["llm_runtime_selection"] == selection
    assert configurable["runtime_services"] is runtime_services
    assert configurable["runtime_projection"] == {
        "task_id": 7,
        "graph_thread_id": GRAPH_THREAD_ID,
        "tenant_id": 3,
        "user_id": 9,
        "runtime_placement_mode": "local",
        "workspace_id": "task-7",
        "actor_type": "agent",
        "actor_id": "langgraph",
        "runner_id": "runner-a",
        "execution_site_id": "site-a",
        "llm_runtime_selection": selection,
        "reasoning_effort": "medium",
    }
    assert "credential_ref" not in repr(configurable)
    assert "api_key" not in repr(configurable)


def test_build_checkpoint_execution_config_can_strip_invocation_services() -> None:
    """Checkpoint/state callers can remove live runtime services from config."""
    from backend.services.llm_provider.runtime_services import strip_runtime_services

    config = build_checkpoint_execution_config(
        task_id=7,
        graph_thread_id=GRAPH_THREAD_ID,
        graph_name="simple_tool",
        runtime_services=object(),
    )

    stripped = strip_runtime_services(config)

    assert "runtime_services" in config["configurable"]
    assert "runtime_services" not in stripped["configurable"]


def _build_continuation_service(*, captured_configs: list[Dict[str, Any]]) -> CheckpointContinuationService:
    class _CheckpointerService:
        def get_checkpointer(self, _task_id: int) -> Any:
            class _Context:
                async def __aenter__(self) -> Any:
                    return object()

                async def __aexit__(self, *_args: Any) -> None:
                    return None

            return _Context()

    class _Executor:
        async def stream_graph(
            self,
            _compiled: Any,
            _graph_input: Any,
            config: Dict[str, Any],
            *_args: Any,
            **_kwargs: Any,
        ) -> Any:
            captured_configs.append(config)
            return SimpleNamespace(
                final_state={"checkpoint": "interrupted-before-llm"},
                interrupted=True,
                interrupt={"kind": "approval"},
                metadata={},
            )

    return CheckpointContinuationService(
        checkpointer_service=_CheckpointerService(),
        executor=_Executor(),
        streaming_adapter=None,
        build_checkpoint_execution_config=build_checkpoint_execution_config,
        hydrate_container_from_checkpoint_state=lambda *_args, **_kwargs: None,
        extract_resume_conversation_id=lambda _state: "",
        resolve_resume_turn_number=lambda **_kwargs: 0,
        persist_chat_message_from_container=lambda **_kwargs: None,
        build_result=lambda **_kwargs: None,
    )


@pytest.mark.asyncio
async def test_continuation_rebuilds_runtime_dependencies_from_user_id(monkeypatch) -> None:
    """Resume can attach live runtime services without trusting checkpoint secrets."""
    captured_configs: list[Dict[str, Any]] = []

    class _Session:
        def close(self) -> None:
            return None

    class _Selection:
        def to_dict(self) -> Dict[str, Any]:
            return _v2_selection()

    class _RuntimeConfigService:
        def __init__(self, _db: Any) -> None:
            return None

        def build_continuation_selection(
            self,
            *,
            user_id: int,
            checkpoint_hint: Dict[str, Any] | None = None,
        ) -> _Selection:
            assert user_id == 77
            assert checkpoint_hint is None
            return _Selection()

        def build_runtime_services(self) -> Any:
            return SimpleNamespace(client_resolver=object())

    monkeypatch.setattr(
        "backend.services.langgraph_chat.checkpoint.continuation_service.SessionLocal",
        lambda: _Session(),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.checkpoint.continuation_service.LLMRuntimeConfigService",
        _RuntimeConfigService,
    )
    service = _build_continuation_service(captured_configs=captured_configs)

    async def _compile_graph(**_kwargs: Any) -> object:
        return object()

    monkeypatch.setattr(
        service,
        "_compile_graph_for_name",
        _compile_graph,
    )

    result = await service.resume_from_interrupt(
        task_id=7,
        graph_thread_id=GRAPH_THREAD_ID,
        user_id=77,
        response={"approved": True},
    )

    assert result.metadata["interrupt"] is True
    configurable = captured_configs[0]["configurable"]
    assert configurable["llm_runtime_selection"] == _v2_selection()
    assert configurable["runtime_projection"]["user_id"] == 77
    assert configurable["runtime_services"].client_resolver is not None
    assert "credential_ref" not in repr(configurable)
    assert "api_key" not in repr(configurable)


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation_method", ["resume", "retry"])
async def test_continuation_runtime_services_remain_live_through_llm_resolution(
    monkeypatch,
    continuation_method: str,
) -> None:
    """Resume/retry fallback runtime services stay usable until graph LLM calls finish."""
    from agent.graph.utils.llm_resolver import resolve_llm_client

    sessions: list[Any] = []
    resolver_calls: list[Dict[str, Any]] = []

    class _Session:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class _Selection:
        def to_dict(self) -> Dict[str, Any]:
            return _v2_selection()

    class _Resolver:
        def __init__(self, db: _Session) -> None:
            self._db = db

        def get_client(self, selection: Any, **kwargs: Any) -> object:
            assert self._db.closed is False
            resolver_calls.append({"selection": selection, **kwargs})
            return object()

    class _RuntimeConfigService:
        def __init__(self, db: _Session) -> None:
            self._db = db

        def build_continuation_selection(
            self,
            *,
            user_id: int,
            checkpoint_hint: Dict[str, Any] | None = None,
        ) -> _Selection:
            assert user_id == 77
            assert checkpoint_hint is None
            return _Selection()

        def build_runtime_services(self) -> Any:
            return SimpleNamespace(client_resolver=_Resolver(self._db))

    class _CheckpointerService:
        def get_checkpointer(self, _task_id: int) -> Any:
            class _Context:
                async def __aenter__(self) -> Any:
                    return object()

                async def __aexit__(self, *_args: Any) -> None:
                    return None

            return _Context()

    class _Executor:
        async def stream_graph(
            self,
            _compiled: Any,
            _graph_input: Any,
            config: Dict[str, Any],
            *_args: Any,
            **_kwargs: Any,
        ) -> Any:
            resolve_llm_client(
                {"provider": "openai", "model": "gpt-5.2"},
                config=config,
            )
            assert "api_key" not in repr(config)
            return SimpleNamespace(
                final_state={"checkpoint": "interrupted-after-llm"},
                interrupted=True,
                interrupt={"kind": "approval"},
                metadata={},
            )

    def _session_factory() -> _Session:
        session = _Session()
        sessions.append(session)
        return session

    monkeypatch.setattr(
        "backend.services.langgraph_chat.checkpoint.continuation_service.SessionLocal",
        _session_factory,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.checkpoint.continuation_service.LLMRuntimeConfigService",
        _RuntimeConfigService,
    )
    service = CheckpointContinuationService(
        checkpointer_service=_CheckpointerService(),
        executor=_Executor(),
        streaming_adapter=None,
        build_checkpoint_execution_config=build_checkpoint_execution_config,
        hydrate_container_from_checkpoint_state=lambda *_args, **_kwargs: None,
        extract_resume_conversation_id=lambda _state: "",
        resolve_resume_turn_number=lambda **_kwargs: 0,
        persist_chat_message_from_container=lambda **_kwargs: None,
        build_result=lambda **_kwargs: None,
    )

    async def _compile_graph(**_kwargs: Any) -> object:
        return object()

    monkeypatch.setattr(service, "_compile_graph_for_name", _compile_graph)

    if continuation_method == "resume":
        result = await service.resume_from_interrupt(
            task_id=7,
            graph_thread_id=GRAPH_THREAD_ID,
            user_id=77,
            response={"approved": True},
        )
    else:
        result = await service.retry_from_checkpoint(
            task_id=7,
            graph_thread_id=GRAPH_THREAD_ID,
            user_id=77,
            graph_name="simple_tool",
        )

    assert result.metadata["interrupt"] is True
    assert resolver_calls
    assert resolver_calls[0]["runtime_user_id"] == 77
    assert resolver_calls[0]["task_id"] == 7
    assert sessions
    assert sessions[-1].closed is True


@pytest.mark.asyncio
async def test_continuation_resolves_checkpoint_runtime_hint(monkeypatch) -> None:
    """Resume keeps checkpoint provider/model while resolving credentials live."""
    captured_configs: list[Dict[str, Any]] = []
    checkpoint_hints: list[Dict[str, Any] | None] = []

    class _Session:
        def close(self) -> None:
            return None

    class _Selection:
        def to_dict(self) -> Dict[str, Any]:
            return _v2_selection(
                deployment_id=ALT_DEPLOYMENT_ID,
                model="gpt-4o-mini",
                reasoning_effort="low",
            )

    class _RuntimeConfigService:
        def __init__(self, _db: Any) -> None:
            return None

        def build_continuation_selection(
            self,
            *,
            user_id: int,
            checkpoint_hint: Dict[str, Any] | None = None,
        ) -> _Selection:
            assert user_id == 77
            checkpoint_hints.append(checkpoint_hint)
            return _Selection()

        def build_runtime_services(self) -> Any:
            return SimpleNamespace(client_resolver=object())

    class _CompiledGraph:
        async def aget_state(self, _config: Dict[str, Any]) -> Any:
            return SimpleNamespace(
                values={
                    "facts": {
                        "metadata": {
                            "llm_runtime_selection": {
                                "provider": "openai",
                                "model": "gpt-4o-mini",
                                "credential_ref": {
                                    "user_id": 999,
                                    "provider": "openai",
                                },
                                "reasoning_effort": "low",
                                "api_key": "sk-checkpoint-secret",
                            }
                        }
                    }
                }
            )

    monkeypatch.setattr(
        "backend.services.langgraph_chat.checkpoint.continuation_service.SessionLocal",
        lambda: _Session(),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.checkpoint.continuation_service.LLMRuntimeConfigService",
        _RuntimeConfigService,
    )
    service = _build_continuation_service(captured_configs=captured_configs)

    async def _compile_graph(**_kwargs: Any) -> _CompiledGraph:
        return _CompiledGraph()

    monkeypatch.setattr(service, "_compile_graph_for_name", _compile_graph)

    result = await service.resume_from_interrupt(
        task_id=7,
        graph_thread_id=GRAPH_THREAD_ID,
        user_id=77,
        response={"approved": True},
        checkpoint_id="cp-1",
        llm_runtime_selection={
            "provider": "openai",
            "model": "gpt-5.2",
            "credential_ref": {"user_id": 77, "provider": "openai"},
            "reasoning_effort": "medium",
        },
        runtime_services=SimpleNamespace(client_resolver=object()),
    )

    assert checkpoint_hints == [
        {"provider": "openai", "model": "gpt-4o-mini", "reasoning_effort": "low"}
    ]
    configurable = captured_configs[0]["configurable"]
    assert configurable["llm_runtime_selection"] == _v2_selection(
        deployment_id=ALT_DEPLOYMENT_ID,
        model="gpt-4o-mini",
        reasoning_effort="low",
    )
    assert result.metadata["llm_runtime_selection"] == configurable[
        "llm_runtime_selection"
    ]
    assert "sk-checkpoint-secret" not in repr(captured_configs[0])
    assert "'user_id': 999" not in repr(captured_configs[0])


@pytest.mark.asyncio
async def test_continuation_fails_from_invalid_legacy_checkpoint_hint(
    monkeypatch,
) -> None:
    """Invalid legacy checkpoint hints stop resume instead of using fallback."""
    captured_configs: list[Dict[str, Any]] = []

    class _Session:
        def close(self) -> None:
            return None

    class _RuntimeConfigService:
        def __init__(self, _db: Any) -> None:
            return None

        def build_continuation_selection(
            self,
            *,
            user_id: int,
            checkpoint_hint: Dict[str, Any] | None = None,
        ) -> Any:
            assert user_id == 77
            if checkpoint_hint is not None:
                raise ProviderConfigurationError("legacy model is not selectable")
            raise AssertionError("invalid checkpoint hint should stop resume")

        def build_runtime_services(self) -> Any:
            raise AssertionError("invalid checkpoint hint should stop resume")

    class _CompiledGraph:
        async def aget_state(self, _config: Dict[str, Any]) -> Any:
            return SimpleNamespace(
                values={
                    "facts": {
                        "metadata": {
                            "llm_runtime_selection": {
                                "provider": "openai",
                                "model": "gpt-4o-mini",
                            }
                        }
                    }
                }
            )

    monkeypatch.setattr(
        "backend.services.langgraph_chat.checkpoint.continuation_service.SessionLocal",
        lambda: _Session(),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.checkpoint.continuation_service.LLMRuntimeConfigService",
        _RuntimeConfigService,
    )
    service = _build_continuation_service(captured_configs=captured_configs)

    async def _compile_graph(**_kwargs: Any) -> _CompiledGraph:
        return _CompiledGraph()

    explicit_selection = {
        "provider": "openai",
        "model": "gpt-5.2",
        "credential_ref": {"user_id": 77, "provider": "openai"},
        "reasoning_effort": "medium",
    }
    explicit_services = SimpleNamespace(client_resolver=object())
    monkeypatch.setattr(service, "_compile_graph_for_name", _compile_graph)

    with pytest.raises(HITLError, match="legacy model is not selectable"):
        await service.resume_from_interrupt(
            task_id=7,
            graph_thread_id=GRAPH_THREAD_ID,
            user_id=77,
            response={"approved": True},
            checkpoint_id="cp-legacy",
            llm_runtime_selection=explicit_selection,
            runtime_services=explicit_services,
        )

    assert captured_configs == []


def test_build_checkpoint_execution_config_sets_recursion_limit() -> None:
    """Resume config must set recursion_limit to match initial-turn config."""
    from backend.services.langgraph_chat.hitl_constants import GRAPH_RECURSION_LIMIT

    config = build_checkpoint_execution_config(
        task_id=7,
        graph_thread_id=GRAPH_THREAD_ID,
        graph_name="simple_tool",
    )
    assert config["recursion_limit"] == GRAPH_RECURSION_LIMIT


def test_build_checkpoint_execution_config_coerces_checkpoint_to_string() -> None:
    """Checkpoint id is serialized as string for LangGraph configurable payload."""
    config = build_checkpoint_execution_config(
        task_id=11,
        graph_thread_id=GRAPH_THREAD_ID,
        graph_name="deep_reasoning",
        checkpoint_id=456,
        interrupt_id="  int-123  ",
    )
    assert config["configurable"]["checkpoint_id"] == "456"
    assert config["configurable"]["interrupt_id"] == "int-123"


def test_build_checkpoint_execution_config_keeps_timing_markers() -> None:
    """Approval/resume timing markers are preserved for resume diagnostics."""
    config = build_checkpoint_execution_config(
        task_id=3,
        graph_thread_id=GRAPH_THREAD_ID,
        graph_name="simple_tool",
        approval_received_at=1.25,
        resume_worker_start_at=2.5,
    )
    assert config["configurable"]["approval_received_at"] == 1.25
    assert config["configurable"]["resume_worker_start_at"] == 2.5


def test_extract_resume_conversation_id_reads_facts_payload() -> None:
    """Conversation id is extracted from final_state facts when available."""
    final_state = {"facts": {"conversation_id": "conv-xyz"}}
    assert extract_resume_conversation_id(final_state) == "conv-xyz"
    assert extract_resume_conversation_id({"facts": {}}) == ""
    assert extract_resume_conversation_id(None) == ""


def test_resolve_resume_runtime_path_label_unknown_on_probe_failure() -> None:
    """Warmup probe failures return unknown runtime-path label."""
    with patch(
        "backend.services.langgraph_chat.runtime.warmup_service.get_shared_runtime_warmup_service",
        side_effect=RuntimeError("warmup unavailable"),
    ):
        assert resolve_resume_runtime_path_label(99) == "unknown"
