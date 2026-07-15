"""Tests for provider-neutral engagement report section generation."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.providers.llm.core.base import LLMResponse
import backend.services.reporting.report_section_generator as generator_module
from backend.services.llm_provider.types import LLMCredentialRef, LLMRuntimeSelection
from backend.services.reporting.contracts import (
    REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED,
    REPORT_SECTION_SCHEMA_VERSION,
)
from backend.services.reporting.report_section_generator import (
    ReportSectionGenerationError,
    ReportSectionGenerator,
)
from backend.services.reporting.report_section_prompt import RenderedReportSectionPrompt
from backend.services.usage_tracking.models import UsageData
from core.llm.structured_schemas import ENGAGEMENT_REPORT_SECTION_STRUCTURED_OUTPUT


def _section_payload() -> dict[str, Any]:
    return {
        "schema_version": REPORT_SECTION_SCHEMA_VERSION,
        "section_id": "executive_summary",
        "section_type": "summary",
        "title": "Executive Summary",
        "status": "ready",
        "content_markdown": "Supported executive summary.",
        "blocks": [],
        "source_refs": {
            "task_memo_ids": ["memo-1"],
            "knowledge_refs": ["finding:1"],
            "evidence_refs": ["evidence:web:1"],
        },
        "unsupported_notes": [],
        "generation_notes": [],
    }


def _rendered_prompt() -> RenderedReportSectionPrompt:
    return RenderedReportSectionPrompt(
        system_prompt="section system instructions",
        user_prompt="bounded section context",
        metadata={
            "prompt_family": "engagement_report_section",
            "prompt_version": "v1",
            "prompt_template_ids": [
                "engagement_report_section_system",
                "engagement_report_section_user",
            ],
            "section_id": "executive_summary",
            "report_type": "pentest",
            "section_schema_name": "engagement_report_section",
            "section_schema_version": REPORT_SECTION_SCHEMA_VERSION,
        },
        report_context_json='{"selected_memos":[{"memo_id":"memo-1"}]}',
        section_plan_json='{"section_id":"executive_summary"}',
    )


class _FakeClient:
    model = "gpt-section"

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
            raise RuntimeError("raw provider output SECRET_MODEL_OUTPUT")
        return LLMResponse(
            content="",
            structured_output=self.payload,
            usage=UsageData(
                prompt_tokens=17,
                completion_tokens=19,
                total_tokens=36,
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
            model="gpt-section",
            credential_ref=LLMCredentialRef(user_id=user_id, provider="openai"),
        )


@pytest.mark.asyncio
async def test_generator_uses_offline_payload_only_in_explicit_e2e_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(generator_module, "E2E_DETERMINISTIC_MODE", True, raising=False)
    client = _FakeClient(_section_payload(), fail=True)
    runtime_config = _FakeRuntimeConfigService(_FakeResolver(client))
    selection = _FakeReportingSelectionService(fail_selection=True)
    generator = ReportSectionGenerator(
        object(),
        runtime_config_service=runtime_config,  # type: ignore[arg-type]
        reporting_selection_service=selection,  # type: ignore[arg-type]
    )
    rendered = RenderedReportSectionPrompt(
        system_prompt="must not reach provider",
        user_prompt="must not reach provider",
        metadata={
            "section_id": "executive_summary",
            "report_type": "pentest",
            "section_schema_version": REPORT_SECTION_SCHEMA_VERSION,
        },
        report_context_json=(
            '{"allowed_task_memo_ids":["memo-1"],'
            '"allowed_knowledge_refs":["finding:1"],'
            '"allowed_evidence_refs":["evidence:web:1"]}'
        ),
        section_plan_json=(
            '{"section_id":"executive_summary","section_type":"summary",'
            '"title":"Executive Summary"}'
        ),
    )

    result = await generator.generate(user_id=7, rendered_prompt=rendered)

    assert result.payload["section_id"] == "executive_summary"
    assert result.payload["status"] == "ready"
    assert "Deterministic E2E" in result.payload["content_markdown"]
    assert result.metadata["provider"] == "deterministic_e2e"
    assert result.metadata["model"] == "offline-report-section-v1"
    assert selection.selection_calls == []
    assert client.calls == []


@pytest.mark.asyncio
async def test_generator_calls_runtime_client_with_structured_section_output() -> None:
    client = _FakeClient(_section_payload())
    resolver = _FakeResolver(client)
    runtime_config = _FakeRuntimeConfigService(resolver)
    reporting_selection = _FakeReportingSelectionService()
    generator = ReportSectionGenerator(
        object(),
        runtime_config_service=runtime_config,  # type: ignore[arg-type]
        reporting_selection_service=reporting_selection,  # type: ignore[arg-type]
    )

    result = await generator.generate(
        user_id=7,
        task_id=None,
        rendered_prompt=_rendered_prompt(),
    )

    assert result.payload["section_id"] == "executive_summary"
    assert reporting_selection.selection_calls == [7]
    assert resolver.calls[0]["kwargs"] == {
        "runtime_user_id": 7,
        "task_id": None,
        "purpose": "reporting.engagement_report_section",
    }
    assert client.calls == [
        {
            "system_prompt": "section system instructions",
            "user_prompt": "bounded section context",
            "kwargs": {
                "structured_output": ENGAGEMENT_REPORT_SECTION_STRUCTURED_OUTPUT
            },
        }
    ]
    assert result.metadata["provider"] == "openai"
    assert result.metadata["model"] == "gpt-section"
    assert result.metadata["prompt_version"] == "v1"
    assert result.metadata["structured_schema_version"] == REPORT_SECTION_SCHEMA_VERSION
    assert result.metadata["usage"] == {
        "prompt_tokens": 17,
        "completion_tokens": 19,
        "total_tokens": 36,
        "model": "gpt-section",
        "provider": "openai",
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "api_surface": "unknown",
        "cache_reporting": "unknown",
        "source": "reporting.engagement_report_section",
    }
    assert isinstance(result.metadata["duration_ms"], int)


@pytest.mark.asyncio
async def test_generator_returns_typed_error_for_runtime_failure() -> None:
    client = _FakeClient(_section_payload())
    runtime_config = _FakeRuntimeConfigService(_FakeResolver(client))
    generator = ReportSectionGenerator(
        object(),
        runtime_config_service=runtime_config,  # type: ignore[arg-type]
        reporting_selection_service=_FakeReportingSelectionService(fail_selection=True),  # type: ignore[arg-type]
    )

    with pytest.raises(ReportSectionGenerationError) as exc_info:
        await generator.generate(
            user_id=7,
            rendered_prompt=_rendered_prompt(),
        )

    error = exc_info.value
    assert error.reason == REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED
    assert error.safe_message == (
        "LLM runtime is unavailable for report section generation."
    )
    assert "api_key" not in error.safe_message
    assert "duration_ms" in error.metadata
    assert error.retryable is False


@pytest.mark.asyncio
async def test_generator_returns_typed_error_for_provider_failure() -> None:
    client = _FakeClient(_section_payload(), fail=True)
    runtime_config = _FakeRuntimeConfigService(_FakeResolver(client))
    generator = ReportSectionGenerator(
        object(),
        runtime_config_service=runtime_config,  # type: ignore[arg-type]
        reporting_selection_service=_FakeReportingSelectionService(),  # type: ignore[arg-type]
    )

    with pytest.raises(ReportSectionGenerationError) as exc_info:
        await generator.generate(
            user_id=7,
            rendered_prompt=_rendered_prompt(),
        )

    error = exc_info.value
    assert error.reason == REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED
    assert error.safe_message == "LLM report section generation failed."
    assert "SECRET_MODEL_OUTPUT" not in error.safe_message
    assert error.metadata["provider"] == "openai"
    assert error.metadata["model"] == "gpt-section"
    assert error.retryable is False


@pytest.mark.asyncio
async def test_generator_rejects_missing_structured_payload() -> None:
    client = _FakeClient(None)
    runtime_config = _FakeRuntimeConfigService(_FakeResolver(client))
    generator = ReportSectionGenerator(
        object(),
        runtime_config_service=runtime_config,  # type: ignore[arg-type]
        reporting_selection_service=_FakeReportingSelectionService(),  # type: ignore[arg-type]
    )

    with pytest.raises(ReportSectionGenerationError) as exc_info:
        await generator.generate(
            user_id=7,
            rendered_prompt=_rendered_prompt(),
        )

    error = exc_info.value
    assert error.reason == REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED
    assert error.safe_message == (
        "LLM report section generation did not return structured output."
    )
    assert "usage" in error.metadata
    assert error.retryable is True


def test_report_section_generator_uses_only_allowed_llm_boundaries() -> None:
    path = Path("backend/services/reporting/report_section_generator.py")
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
