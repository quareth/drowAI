""": Integration tests for SSE metadata preservation in OpenAI chunks.

Validates that:
- SSE endpoint preserves metadata (ind, step_type) in OpenAI-style chunks
- metadata field is present in all chunks
- ind and step_type are correctly set for frontend grouping
- Frontend can extract metadata (observation vs message separation)
- OpenAI client compatibility: extra metadata field does not break parsing"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from agent.graph.contracts.streaming_constants import (
    ANSWER_PHASE_INDEX,
    OBSERVATION_PHASE_INDEX,
    STEP_MESSAGE_DELTA,
)

# Import private chunking helpers to test metadata preservation without full SSE
from backend.routers.agent_reasoning import (
    _build_chunk_metadata,
    _create_automatic_chunking_config,
    _create_interactive_chunking_config,
    _stream_chunks_with_config,
    _stream_optimized_realtime,
    _stream_standard_with_delays,
)


# --- Helpers ---


async def collect_data_payloads(
    task_id: int = 1,
    conv_id: str = "conv-1",
    anchor_seq: int = 1,
    content: str = "Hello",
    *,
    ind: int = ANSWER_PHASE_INDEX,
    step_type: str = STEP_MESSAGE_DELTA,
    interactive: bool = True,
) -> List[Dict[str, Any]]:
    """Run chunking and return list of parsed 'data' JSON payloads (OpenAI chunk shape)."""
    config = (
        _create_interactive_chunking_config()
        if interactive
        else _create_automatic_chunking_config(True, 0)
    )
    payloads: List[Dict[str, Any]] = []
    async for chunk in _stream_chunks_with_config(
        content,
        task_id,
        conv_id,
        anchor_seq,
        config,
        ind=ind,
        step_type=step_type,
    ):
        if chunk.startswith("data: "):
            raw = chunk[6:].strip()
            if raw == "[DONE]":
                continue
            try:
                payloads.append(json.loads(raw))
            except json.JSONDecodeError:
                pass
    return payloads


def parse_chunk_metadata(chunk: Dict[str, Any]) -> Dict[str, Any]:
    """Extract metadata from an OpenAI-style chunk (frontend extraction logic)."""
    return (chunk.get("metadata") or {}).copy()


# --- 1. Metadata field present ---


class TestSSEMetadataFieldPresent:
    """SSE OpenAI-style chunks include a metadata field."""

    @pytest.mark.asyncio
    async def test_interactive_chunks_have_metadata(self) -> None:
        payloads = await collect_data_payloads(content="Hi", interactive=True)
        assert payloads, "At least one chunk expected"
        for p in payloads:
            assert "metadata" in p, f"Chunk must have metadata key: {list(p.keys())}"
            meta = p["metadata"]
            assert isinstance(meta, dict), "metadata must be a dict"

    @pytest.mark.asyncio
    async def test_automatic_chunks_have_metadata(self) -> None:
        payloads = await collect_data_payloads(content="Hi", interactive=False)
        assert payloads, "At least one chunk expected"
        for p in payloads:
            assert "metadata" in p, f"Chunk must have metadata key: {list(p.keys())}"


# --- 2. ind and step_type correctly set ---


class TestSSEIndAndStepTypePreserved:
    """Chunks preserve ind and step_type for frontend grouping."""

    @pytest.mark.asyncio
    async def test_message_chunks_have_ind_two_and_step_type(self) -> None:
        payloads = await collect_data_payloads(
            content="Answer",
            ind=ANSWER_PHASE_INDEX,
            step_type=STEP_MESSAGE_DELTA,
        )
        assert payloads
        for p in payloads:
            meta = parse_chunk_metadata(p)
            assert meta.get("ind") == ANSWER_PHASE_INDEX, (
                f"Message chunks must have ind={ANSWER_PHASE_INDEX}, got {meta.get('ind')}"
            )
            assert meta.get("step_type") == STEP_MESSAGE_DELTA

    @pytest.mark.asyncio
    async def test_observation_chunks_preserve_ind_three(self) -> None:
        payloads = await collect_data_payloads(
            content="Obs",
            ind=OBSERVATION_PHASE_INDEX,
            step_type="observation_delta",
        )
        assert payloads
        for p in payloads:
            meta = parse_chunk_metadata(p)
            assert meta.get("ind") == OBSERVATION_PHASE_INDEX, (
                f"Observation chunks must have ind={OBSERVATION_PHASE_INDEX}, got {meta.get('ind')}"
            )
            assert meta.get("step_type") == "observation_delta"

    @pytest.mark.asyncio
    async def test_metadata_includes_conversation_id_and_streaming(self) -> None:
        conv_id = "test-conv-42"
        payloads = await collect_data_payloads(
            content="x",
            conv_id=conv_id,
            ind=ANSWER_PHASE_INDEX,
            step_type=STEP_MESSAGE_DELTA,
        )
        assert payloads
        for p in payloads:
            meta = parse_chunk_metadata(p)
            assert meta.get("conversation_id") == conv_id or meta.get("conversationId") == conv_id
            assert meta.get("streaming") is True


# --- 3. Frontend extraction ---


class TestFrontendCanExtractMetadata:
    """Frontend can extract ind/step_type from chunk.metadata."""

    @pytest.mark.asyncio
    async def test_extract_ind_for_grouping(self) -> None:
        payloads = await collect_data_payloads(
            content="Hi",
            ind=ANSWER_PHASE_INDEX,
            step_type=STEP_MESSAGE_DELTA,
        )
        assert payloads
        for p in payloads:
            meta = parse_chunk_metadata(p)
            ind = meta.get("ind")
            assert ind is not None and ind >= 0, "Frontend needs ind for grouping"
            step_type = meta.get("step_type")
            assert isinstance(step_type, str), "Frontend needs step_type"

    @pytest.mark.asyncio
    async def test_observation_and_message_different_ind(self) -> None:
        msg_payloads = await collect_data_payloads(
            content="M",
            ind=ANSWER_PHASE_INDEX,
            step_type=STEP_MESSAGE_DELTA,
        )
        obs_payloads = await collect_data_payloads(
            content="O",
            ind=OBSERVATION_PHASE_INDEX,
            step_type="observation_delta",
        )
        assert msg_payloads and obs_payloads
        msg_ind = parse_chunk_metadata(msg_payloads[0]).get("ind")
        obs_ind = parse_chunk_metadata(obs_payloads[0]).get("ind")
        assert msg_ind != obs_ind, "Observation and message must have distinct ind (no blending)"


# --- 4. OpenAI format compliance ---


class TestOpenAIChunkFormatCompliance:
    """Chunks remain valid OpenAI-style; extra metadata does not break clients."""

    @pytest.mark.asyncio
    async def test_chunk_has_required_openai_fields(self) -> None:
        payloads = await collect_data_payloads(content="Hi")
        assert payloads
        for p in payloads:
            assert "id" in p or "object" in p, "OpenAI chunk must have id/object"
            assert p.get("object") == "chat.completion.chunk"
            assert "choices" in p
            assert isinstance(p["choices"], list)
            assert len(p["choices"]) >= 1
            delta = p["choices"][0].get("delta", {})
            assert "content" in delta

    @pytest.mark.asyncio
    async def test_openai_client_compatibility(self) -> None:
        """Standard OpenAI client can parse chunk; extra top-level 'metadata' is allowed."""
        payloads = await collect_data_payloads(content="Hello")
        assert payloads
        for p in payloads:
            # Re-serialize and parse as a client would
            raw = json.dumps(p)
            parsed = json.loads(raw)
            assert parsed.get("object") == "chat.completion.chunk"
            content = parsed.get("choices", [{}])[0].get("delta", {}).get("content", "")
            assert isinstance(content, str)
            # Metadata is optional for strict OpenAI clients; presence must not break parsing
            if "metadata" in parsed:
                meta = parsed["metadata"]
                assert isinstance(meta, dict)
                assert "ind" in meta or "streaming" in meta


# --- 5. _build_chunk_metadata unit ---


class TestBuildChunkMetadata:
    """Unit tests for chunk metadata builder."""

    def test_build_chunk_metadata_has_ind_step_type(self) -> None:
        meta = _build_chunk_metadata("c1", ANSWER_PHASE_INDEX, STEP_MESSAGE_DELTA)
        assert meta["ind"] == ANSWER_PHASE_INDEX
        assert meta["step_type"] == STEP_MESSAGE_DELTA
        assert meta["conversation_id"] == "c1"
        assert meta["conversationId"] == "c1"
        assert meta["streaming"] is True
