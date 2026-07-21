"""Tests for provider-neutral task closure memo generation."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.providers.llm.core.base import LLMResponse
from backend.services.llm_provider.types import (
    DeploymentRef,
    LLMCredentialRef,
    LLMRuntimeSelection,
    LLMRuntimeSelectionV2,
)
from backend.services.reporting.contracts import (
    TASK_CLOSURE_MEMO_CONTRACTS,
    TASK_CLOSURE_MEMO_SCHEMA_VERSION,
    TASK_MEMO_ERROR_GENERATION_FAILED,
    TASK_MEMO_ERROR_LLM_RUNTIME_UNAVAILABLE,
)
from backend.services.reporting.memo_generator import (
    TaskClosureMemoGenerationError,
    TaskClosureMemoGenerator,
)
from backend.services.reporting.memo_prompt import RenderedTaskClosureMemoPrompt
from backend.services.usage_tracking.models import UsageData
from core.llm.structured_schemas import TASK_CLOSURE_MEMO_STRUCTURED_OUTPUT


def _memo_payload() -> dict[str, Any]:
    return {
        "task_name": "Inspect service exposure",
        "summary": "Task closure memo summary.",
        "include_in_report_recommendation": {
            "include": True,
            "reason": "Evidence-backed service exposure was observed.",
        },
        "actions_performed": [
            {"text": "Reviewed service scan output.", "source": "evidence"}
        ],
        "reportable_observations": [
            {
                "text": "HTTPS was exposed.",
                "confidence": "high",
                "evidence_refs": ["evidence_archive:1"],
                "knowledge_refs": [],
            }
        ],
        "possible_findings": [],
        "limitations": [],
        "unsupported_notes": [],
        "evidence_refs": ["evidence_archive:1"],
        "knowledge_refs": [],
    }


def _rendered_prompt() -> RenderedTaskClosureMemoPrompt:
    return RenderedTaskClosureMemoPrompt(
        system_prompt="system instructions",
        user_prompt="bounded context",
        metadata={
            "prompt_family": "task_closure_memo",
            "prompt_version": "v1",
            "prompt_template_ids": [
                "task_closure_memo_system",
                "task_closure_memo_user",
            ],
        },
        memo_context_json='{"task":{"task_id":42}}',
    )


class _FakeClient:
    model = "gpt-test"

    def __init__(
        self,
        payload: dict[str, Any] | None,
        *,
        fail: bool = False,
    ) -> None:
        self.payload = payload
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def chat_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        **kwargs: Any,
    ) -> LLMResponse:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "kwargs": kwargs,
            }
        )
        if self.fail:
            raise RuntimeError("provider returned an unsafe diagnostic")
        return LLMResponse(
            content="",
            structured_output=self.payload,
            usage=UsageData(
                prompt_tokens=11,
                completion_tokens=13,
                total_tokens=24,
                model=self.model,
                provider="openai",
            ),
        )


class _FakeResolver:
    def __init__(self, client: _FakeClient) -> None:
        self.client = client
        self.calls: list[dict[str, Any]] = []

    def get_client(self, selection: Any, **kwargs: Any) -> _FakeClient:
        self.calls.append({"selection": selection, "kwargs": kwargs})
        return self.client


class _FakeRuntimeConfigService:
    def __init__(
        self,
        resolver: _FakeResolver,
    ) -> None:
        self.resolver = resolver

    def build_runtime_services(self) -> Any:
        return SimpleNamespace(client_resolver=self.resolver)


class _FakeReportingSelectionService:
    def __init__(self, *, fail_selection: bool = False) -> None:
        self.fail_selection = fail_selection
        self.selection_calls: list[int] = []

    def build_runtime_selection(self, *, user_id: int) -> LLMRuntimeSelection:
        self.selection_calls.append(user_id)
        if self.fail_selection:
            raise RuntimeError("api_key=should-not-surface")
        return LLMRuntimeSelection(
            provider="openai",
            model="gpt-test",
            credential_ref=LLMCredentialRef(user_id=user_id, provider="openai"),
        )


@pytest.mark.asyncio
async def test_generator_calls_runtime_client_with_structured_output() -> None:
    client = _FakeClient(_memo_payload())
    resolver = _FakeResolver(client)
    runtime_config = _FakeRuntimeConfigService(resolver)
    reporting_selection = _FakeReportingSelectionService()
    generator = TaskClosureMemoGenerator(
        object(),
        runtime_config_service=runtime_config,  # type: ignore[arg-type]
        reporting_selection_service=reporting_selection,  # type: ignore[arg-type]
    )

    result = await generator.generate(
        user_id=7,
        task_id=42,
        rendered_prompt=_rendered_prompt(),
    )

    assert result.payload["task_name"] == "Inspect service exposure"
    assert reporting_selection.selection_calls == [7]
    assert resolver.calls[0]["kwargs"] == {
        "runtime_user_id": 7,
        "task_id": 42,
        "purpose": TASK_CLOSURE_MEMO_CONTRACTS.generation_purpose,
    }
    assert client.calls[0]["system_prompt"] == "system instructions"
    assert client.calls[0]["user_prompt"] == "bounded context"
    assert (
        client.calls[0]["kwargs"]["structured_output"]
        is TASK_CLOSURE_MEMO_STRUCTURED_OUTPUT
    )
    assert result.metadata["provider"] == "openai"
    assert result.metadata["model"] == "gpt-test"
    assert result.metadata["memo_schema_version"] == TASK_CLOSURE_MEMO_SCHEMA_VERSION
    assert result.metadata["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 13,
        "total_tokens": 24,
        "model": "gpt-test",
        "provider": "openai",
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "api_surface": "unknown",
        "cache_reporting": "unknown",
        "source": TASK_CLOSURE_MEMO_CONTRACTS.generation_purpose,
    }
    assert isinstance(result.metadata["duration_ms"], int)


@pytest.mark.asyncio
async def test_generator_accepts_deployment_runtime_selection() -> None:
    client = _FakeClient(_memo_payload())
    resolver = _FakeResolver(client)
    runtime_config = _FakeRuntimeConfigService(resolver)
    generator = TaskClosureMemoGenerator(
        object(),
        runtime_config_service=runtime_config,  # type: ignore[arg-type]
        reporting_selection_service=_FakeReportingSelectionService(),  # type: ignore[arg-type]
    )
    selection = LLMRuntimeSelectionV2(
        deployment_ref=DeploymentRef(
            deployment_id="a8f42649-173c-4f2a-8634-42f3403d08e4",
            expected_revision=3,
        ),
        reasoning_effort="medium",
        legacy_provider="openai",
        legacy_model="gpt-5-mini",
    ).to_dict()

    result = await generator.generate(
        user_id=7,
        task_id=42,
        rendered_prompt=_rendered_prompt(),
        runtime_selection=selection,
    )

    assert resolver.calls[0]["selection"].to_dict() == selection
    assert resolver.calls[0]["kwargs"]["runtime_user_id"] == 7
    assert result.metadata["provider"] == "openai"
    assert result.metadata["reasoning_effort"] == "medium"


@pytest.mark.asyncio
async def test_generator_returns_typed_error_for_runtime_failure() -> None:
    client = _FakeClient(_memo_payload())
    runtime_config = _FakeRuntimeConfigService(_FakeResolver(client))
    generator = TaskClosureMemoGenerator(
        object(),
        runtime_config_service=runtime_config,  # type: ignore[arg-type]
        reporting_selection_service=_FakeReportingSelectionService(
            fail_selection=True
        ),  # type: ignore[arg-type]
    )

    with pytest.raises(TaskClosureMemoGenerationError) as exc_info:
        await generator.generate(
            user_id=7,
            task_id=42,
            rendered_prompt=_rendered_prompt(),
        )

    error = exc_info.value
    assert error.reason == TASK_MEMO_ERROR_LLM_RUNTIME_UNAVAILABLE
    assert error.safe_message == "LLM runtime is unavailable for memo generation."
    assert "api_key" not in error.safe_message
    assert "duration_ms" in error.metadata


@pytest.mark.asyncio
async def test_generator_returns_typed_error_for_call_failure() -> None:
    client = _FakeClient(_memo_payload(), fail=True)
    runtime_config = _FakeRuntimeConfigService(_FakeResolver(client))
    generator = TaskClosureMemoGenerator(
        object(),
        runtime_config_service=runtime_config,  # type: ignore[arg-type]
        reporting_selection_service=_FakeReportingSelectionService(),  # type: ignore[arg-type]
    )

    with pytest.raises(TaskClosureMemoGenerationError) as exc_info:
        await generator.generate(
            user_id=7,
            task_id=42,
            rendered_prompt=_rendered_prompt(),
        )

    error = exc_info.value
    assert error.reason == TASK_MEMO_ERROR_GENERATION_FAILED
    assert error.safe_message == "LLM memo generation failed."
    assert "unsafe diagnostic" not in error.safe_message
    assert error.metadata["provider"] == "openai"
    assert error.metadata["model"] == "gpt-test"


@pytest.mark.asyncio
async def test_generator_rejects_missing_structured_payload() -> None:
    client = _FakeClient(None)
    runtime_config = _FakeRuntimeConfigService(_FakeResolver(client))
    generator = TaskClosureMemoGenerator(
        object(),
        runtime_config_service=runtime_config,  # type: ignore[arg-type]
        reporting_selection_service=_FakeReportingSelectionService(),  # type: ignore[arg-type]
    )

    with pytest.raises(TaskClosureMemoGenerationError) as exc_info:
        await generator.generate(
            user_id=7,
            task_id=42,
            rendered_prompt=_rendered_prompt(),
        )

    error = exc_info.value
    assert error.reason == TASK_MEMO_ERROR_GENERATION_FAILED
    assert error.safe_message == (
        "LLM memo generation did not return structured output."
    )
    assert "usage" in error.metadata


def test_memo_generator_uses_only_allowed_llm_boundaries() -> None:
    path = Path("backend/services/reporting/memo_generator.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert "backend.services.llm_provider" in imported_modules
    assert "core.llm.structured_schemas" in imported_modules
    assert not {
        module
        for module in imported_modules
        if module.startswith(("openai", "anthropic", "langchain", "langgraph"))
    }
