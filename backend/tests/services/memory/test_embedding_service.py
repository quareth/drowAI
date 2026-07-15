"""Validate OpenAI embedding provider contract with mocked provider calls."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from backend.services.embeddings.providers.openai import OpenAIEmbeddingProvider


@dataclass
class _FakeEmbeddingItem:
    index: int
    embedding: list[float]


@dataclass
class _FakeEmbeddingResponse:
    data: list[_FakeEmbeddingItem]


class _FakeEmbeddingsAPI:
    def __init__(self) -> None:
        self.last_call: dict | None = None

    async def create(self, *, model: str, input, dimensions: int) -> _FakeEmbeddingResponse:
        self.last_call = {"model": model, "input": input, "dimensions": dimensions}
        if isinstance(input, list):
            data = []
            for idx, text in enumerate(input):
                data.append(_FakeEmbeddingItem(index=idx, embedding=[float(len(text))] * dimensions))
            # Return out of order to verify embed_batch sorts by item.index.
            data = list(reversed(data))
            return _FakeEmbeddingResponse(data=data)
        return _FakeEmbeddingResponse(data=[_FakeEmbeddingItem(index=0, embedding=[float(len(input))] * dimensions)])


class _FakeClient:
    def __init__(self, embeddings_api: _FakeEmbeddingsAPI) -> None:
        self.embeddings = embeddings_api


def test_get_client_prefers_explicit_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    class _FakeLLMClient:
        def __init__(self, *, api_key: str) -> None:
            seen["api_key"] = api_key
            self.embeddings = _FakeEmbeddingsAPI()

    monkeypatch.setattr("backend.services.embeddings.providers.openai.AsyncOpenAI", _FakeLLMClient)
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")

    service = OpenAIEmbeddingProvider(api_key="explicit-key")
    _ = service._get_client()

    assert seen["api_key"] == "explicit-key"


def test_get_client_raises_when_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    service = OpenAIEmbeddingProvider(api_key=None)

    with pytest.raises(ValueError, match="OpenAI API key is required"):
        service._get_client()


def test_get_client_does_not_fall_back_to_env_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    service = OpenAIEmbeddingProvider(api_key=None)

    with pytest.raises(ValueError, match="OpenAI API key is required"):
        service._get_client()


@pytest.mark.asyncio
async def test_embed_returns_correct_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _FakeEmbeddingsAPI()
    service = OpenAIEmbeddingProvider(
        model="text-embedding-3-small",
        dimensions=8,
        api_key="test-key",
    )
    monkeypatch.setattr(service, "_get_client", lambda: _FakeClient(api))

    vector = await service.embed("hello")
    assert len(vector) == 8
    assert vector == [5.0] * 8


@pytest.mark.asyncio
async def test_embed_batch_preserves_order(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _FakeEmbeddingsAPI()
    service = OpenAIEmbeddingProvider(
        model="text-embedding-3-small",
        dimensions=4,
        api_key="test-key",
    )
    monkeypatch.setattr(service, "_get_client", lambda: _FakeClient(api))

    result = await service.embed_batch(["a", "bbb"])
    assert result == [[1.0] * 4, [3.0] * 4]


@pytest.mark.asyncio
async def test_embed_empty_string_raises() -> None:
    service = OpenAIEmbeddingProvider(api_key="test-key")
    with pytest.raises(ValueError, match="must not be empty"):
        await service.embed("   ")


@pytest.mark.asyncio
async def test_embed_batch_empty_list_raises() -> None:
    service = OpenAIEmbeddingProvider(api_key="test-key")
    with pytest.raises(ValueError, match="must not be empty"):
        await service.embed_batch([])


@pytest.mark.asyncio
async def test_configurable_model_and_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _FakeEmbeddingsAPI()
    service = OpenAIEmbeddingProvider(
        model="text-embedding-3-large",
        dimensions=12,
        api_key="test-key",
    )
    monkeypatch.setattr(service, "_get_client", lambda: _FakeClient(api))

    await service.embed("shape")
    assert service.model == "text-embedding-3-large"
    assert service.dimensions == 12
    assert api.last_call == {
        "model": "text-embedding-3-large",
        "input": "shape",
        "dimensions": 12,
    }
