"""Unit tests for long-term memory retrieval node runtime-service plumbing."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.nodes import memory_retrieval
from backend.services.memory.memory_models import MemorySearchResult, MemoryTier


def _base_state(metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "facts": {
            "task_id": 42,
            "message": "remember context",
            "metadata": metadata or {},
        },
        "trace": {"history": [], "reasoning": [], "scratchpad": ""},
    }


def _summary_from(update: dict[str, Any]) -> str:
    return str(update["facts"]["metadata"]["long_term_memory_summary"])


def _runtime_config(
    *,
    service: Any,
    user_id: int = 7,
    task_id: int = 42,
) -> dict[str, Any]:
    return {
        "configurable": {
            "runtime_services": SimpleNamespace(memory_runtime_service=service),
            "llm_runtime_selection": {
                "provider": "openai",
                "model": "gpt-5.2",
                "credential_ref": {"user_id": user_id, "provider": "openai"},
            },
            "runtime_projection": {"user_id": user_id, "task_id": task_id},
        }
    }


class _MemoryRuntimeService:
    def __init__(self, *, summary: str = "Retrieved memory.") -> None:
        self.summary = summary
        self.calls: list[dict[str, Any]] = []

    async def retrieve_summary(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return self.summary


@pytest.mark.asyncio
async def test_memory_retrieval_clears_summary_when_no_user_id() -> None:
    state = _base_state(
        metadata={
            "working_memory": {"objective": {"text": "Inspect prior host context"}},
            "long_term_memory_summary": "stale value",
        }
    )

    result = await memory_retrieval.memory_retrieval_node(state, context=None)

    assert _summary_from(result) == ""


@pytest.mark.asyncio
async def test_memory_retrieval_clears_summary_when_no_working_memory() -> None:
    state = _base_state(metadata={"user_id": 7, "long_term_memory_summary": "stale value"})

    result = await memory_retrieval.memory_retrieval_node(state, context=None)

    assert _summary_from(result) == ""


@pytest.mark.asyncio
async def test_memory_retrieval_clears_summary_when_runtime_service_missing() -> None:
    state = _base_state(
        metadata={
            "user_id": 7,
            "working_memory": {"objective": {"text": "Find user preferences"}},
            "long_term_memory_summary": "stale value",
        }
    )

    result = await memory_retrieval.memory_retrieval_node(state, context=None)

    assert _summary_from(result) == ""


@pytest.mark.asyncio
async def test_memory_retrieval_uses_backend_runtime_service() -> None:
    service = _MemoryRuntimeService(summary="Context key retrieval works.")
    state = _base_state(
        metadata={
            "user_id": 7,
            "working_memory": {"objective": {"text": "Find user preferences"}},
            "long_term_memory_summary": "stale value",
        }
    )

    result = await memory_retrieval.memory_retrieval_node(
        state,
        context=GraphRuntimeContext(task_id=42, user_id=7),
        config=_runtime_config(service=service),
    )

    assert _summary_from(result) == "Context key retrieval works."
    assert service.calls == [
        {
            "selection": {
                "provider": "openai",
                "model": "gpt-5.2",
                "credential_ref": {"user_id": 7, "provider": "openai"},
            },
            "runtime_user_id": 7,
            "task_id": 42,
            "user_id": 7,
            "query": "Find user preferences",
            "max_results": memory_retrieval.MEMORY_RETRIEVAL_MAX_RESULTS,
            "max_chars": memory_retrieval.MEMORY_RETRIEVAL_SUMMARY_MAX_CHARS,
        }
    ]


@pytest.mark.asyncio
async def test_memory_retrieval_refuses_metadata_user_mismatch() -> None:
    service = _MemoryRuntimeService(summary="should not leak")
    state = _base_state(
        metadata={
            "user_id": 8,
            "working_memory": {"objective": {"text": "Find user preferences"}},
            "long_term_memory_summary": "stale value",
        }
    )

    result = await memory_retrieval.memory_retrieval_node(
        state,
        context=GraphRuntimeContext(task_id=42, user_id=7),
        config=_runtime_config(service=service, user_id=7),
    )

    assert _summary_from(result) == ""
    assert service.calls == []


@pytest.mark.asyncio
async def test_memory_retrieval_clears_summary_on_runtime_service_error() -> None:
    class _FailingMemoryRuntimeService:
        async def retrieve_summary(self, **_kwargs: Any) -> str:
            raise RuntimeError("memory service unavailable")

    state = _base_state(
        metadata={
            "user_id": 7,
            "working_memory": {"objective": {"text": "Retrieve long-term context"}},
            "long_term_memory_summary": "stale value",
        }
    )

    result = await memory_retrieval.memory_retrieval_node(
        state,
        context=None,
        config=_runtime_config(service=_FailingMemoryRuntimeService()),
    )

    assert _summary_from(result) == ""


def test_split_retrieval_limits_queries_engagement_when_total_budget_is_one() -> None:
    assert memory_retrieval._split_retrieval_limits(1) == (0, 1)


def test_render_memory_summary_respects_max_chars() -> None:
    result = MemorySearchResult(
        id="id-long",
        content="A" * 200,
        memory_tier=MemoryTier.USER_PROFILE,
        similarity_score=0.9,
        created_at=datetime.now(timezone.utc),
    )

    summary = memory_retrieval._render_memory_summary([result], [], max_chars=40)

    assert len(summary) == 40
    assert summary.endswith("...")
