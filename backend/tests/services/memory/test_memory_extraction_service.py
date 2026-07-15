"""Validate MemoryExtractionService orchestration with mocked dependencies."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT_DIR = Path(__file__).resolve().parents[4]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if "core" not in sys.modules:
    core_pkg = types.ModuleType("core")
    core_pkg.__path__ = [str((ROOT_DIR / "core").resolve())]
    sys.modules["core"] = core_pkg

from backend.services.memory.memory_extraction import MemoryExtractionService
from backend.services.memory.memory_models import MemoryTier
import backend.services.memory.memory_extraction as memory_extraction_module


class _FakeClient:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls: list[dict] = []

    async def chat_with_usage(self, _system: str, _user: str, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content="", structured_output=self._payload, usage=None)


class _FakeMemoryStore:
    def __init__(self) -> None:
        self.requests = []

    async def store(self, request):
        self.requests.append(request)
        return SimpleNamespace(id=f"m{len(self.requests)}")


@pytest.mark.asyncio
async def test_should_extract_returns_false_for_structural_fail() -> None:
    gate_client = _FakeClient({"extractable": True})
    extraction_client = _FakeClient({"facts": [], "skipped_reason": None})
    store = _FakeMemoryStore()
    service = MemoryExtractionService(store, gate_client, extraction_client)

    assert await service.should_extract("ok", "assistant response") is False
    assert len(gate_client.calls) == 0


@pytest.mark.asyncio
async def test_should_extract_returns_false_for_pure_tool_output() -> None:
    gate_client = _FakeClient({"extractable": True})
    extraction_client = _FakeClient({"facts": [], "skipped_reason": None})
    store = _FakeMemoryStore()
    service = MemoryExtractionService(store, gate_client, extraction_client)

    tool_only_response = """```text
PORT     STATE SERVICE
22/tcp   open  ssh
443/tcp  open  https
```"""
    assert await service.should_extract("please continue the assessment", tool_only_response) is False
    assert len(gate_client.calls) == 0


@pytest.mark.asyncio
async def test_should_extract_returns_true_when_gate_says_yes() -> None:
    gate_client = _FakeClient({"extractable": True})
    extraction_client = _FakeClient({"facts": [], "skipped_reason": None})
    store = _FakeMemoryStore()
    service = MemoryExtractionService(store, gate_client, extraction_client)

    assert await service.should_extract("this is long enough", "assistant response") is True
    assert len(gate_client.calls) == 1
    assert (
        gate_client.calls[0]["structured_output"]
        is memory_extraction_module.MEMORY_GATE_STRUCTURED_OUTPUT
    )


@pytest.mark.asyncio
async def test_should_extract_returns_false_when_gate_says_no() -> None:
    gate_client = _FakeClient({"extractable": False})
    extraction_client = _FakeClient({"facts": [], "skipped_reason": None})
    store = _FakeMemoryStore()
    service = MemoryExtractionService(store, gate_client, extraction_client)

    assert await service.should_extract("this is long enough", "assistant response") is False


@pytest.mark.asyncio
async def test_extract_stores_user_profile_fact() -> None:
    gate_client = _FakeClient({"extractable": True})
    extraction_client = _FakeClient(
        {
            "facts": [{"content": "User prefers concise replies.", "tier": "user_profile"}],
            "skipped_reason": None,
        }
    )
    store = _FakeMemoryStore()
    service = MemoryExtractionService(store, gate_client, extraction_client)

    await service.extract(
        "this is long enough",
        "assistant response",
        user_id=10,
        tenant_id=None,
        engagement_id=20,
        task_id=30,
        conversation_id="conv-1",
        turn_id="turn-1",
    )

    assert len(store.requests) == 1
    request = store.requests[0]
    assert request.memory_tier == MemoryTier.USER_PROFILE
    assert request.engagement_id is None


@pytest.mark.asyncio
async def test_extract_stores_task_engagement_fact() -> None:
    gate_client = _FakeClient({"extractable": True})
    extraction_client = _FakeClient(
        {
            "facts": [
                {
                    "content": "Focus on web app first.",
                    "tier": "task_engagement",
                }
            ],
            "skipped_reason": None,
        }
    )
    store = _FakeMemoryStore()
    service = MemoryExtractionService(store, gate_client, extraction_client)

    await service.extract(
        "this is long enough",
        "assistant response",
        user_id=10,
        tenant_id=1,
        engagement_id=99,
        task_id=30,
        conversation_id="conv-1",
        turn_id="turn-1",
    )

    assert len(store.requests) == 1
    request = store.requests[0]
    assert request.memory_tier == MemoryTier.TASK_ENGAGEMENT
    assert request.tenant_id == 1
    assert request.engagement_id == 99


@pytest.mark.asyncio
async def test_extract_masks_fact_content_before_store() -> None:
    raw_secret = "memory-secret-12345"
    gate_client = _FakeClient({"extractable": True})
    extraction_client = _FakeClient(
        {
            "facts": [
                {
                    "content": f"The captured login password is password={raw_secret}.",
                    "tier": "task_engagement",
                }
            ],
            "skipped_reason": None,
        }
    )
    store = _FakeMemoryStore()
    service = MemoryExtractionService(store, gate_client, extraction_client)

    await service.extract(
        "this is long enough",
        "assistant response",
        user_id=10,
        tenant_id=1,
        engagement_id=99,
        task_id=30,
        conversation_id="conv-1",
        turn_id="turn-1",
    )

    assert len(store.requests) == 1
    assert raw_secret not in store.requests[0].content
    assert "<DURABLE_SECRET_MASK:" in store.requests[0].content


@pytest.mark.asyncio
async def test_extract_respects_max_facts() -> None:
    gate_client = _FakeClient({"extractable": True})
    extraction_client = _FakeClient(
        {
            "facts": [
                {"content": f"Fact sentence {idx}.", "tier": "user_profile"}
                for idx in range(10)
            ],
            "skipped_reason": None,
        }
    )
    store = _FakeMemoryStore()
    service = MemoryExtractionService(store, gate_client, extraction_client)

    await service.extract(
        "this is long enough",
        "assistant response",
        user_id=10,
        tenant_id=1,
        engagement_id=99,
        task_id=30,
        conversation_id="conv-1",
        turn_id="turn-1",
    )

    assert len(store.requests) == memory_extraction_module.MEMORY_EXTRACTION_MAX_FACTS_PER_TURN


@pytest.mark.asyncio
async def test_extract_if_needed_skips_when_gate_false() -> None:
    gate_client = _FakeClient({"extractable": False})
    extraction_client = _FakeClient(
        {"facts": [{"content": "Should not store.", "tier": "user_profile"}], "skipped_reason": None}
    )
    store = _FakeMemoryStore()
    service = MemoryExtractionService(store, gate_client, extraction_client)

    results = await service.extract_if_needed(
        "this is long enough",
        "assistant response",
        user_id=10,
        tenant_id=1,
        engagement_id=99,
        task_id=30,
        conversation_id="conv-1",
        turn_id="turn-1",
    )

    assert results == []
    assert len(extraction_client.calls) == 0
    assert len(store.requests) == 0


@pytest.mark.asyncio
async def test_extract_if_needed_extracts_when_gate_true() -> None:
    gate_client = _FakeClient({"extractable": True})
    extraction_client = _FakeClient(
        {
            "facts": [{"content": "User likes tables in markdown.", "tier": "user_profile"}],
            "skipped_reason": None,
        }
    )
    store = _FakeMemoryStore()
    service = MemoryExtractionService(store, gate_client, extraction_client)

    results = await service.extract_if_needed(
        "this is long enough",
        "assistant response",
        user_id=10,
        tenant_id=1,
        engagement_id=99,
        task_id=30,
        conversation_id="conv-1",
        turn_id="turn-1",
    )

    assert len(extraction_client.calls) == 1
    assert len(store.requests) == 1
    assert len(results) == 1
