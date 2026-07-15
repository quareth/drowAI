"""Validate extraction flow honors tenant baseline exact dedup behavior."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

ROOT_DIR = Path(__file__).resolve().parents[4]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if "core" not in sys.modules:
    core_pkg = types.ModuleType("core")
    core_pkg.__path__ = [str((ROOT_DIR / "core").resolve())]
    sys.modules["core"] = core_pkg

from backend.database import Base
from backend.models.core import User
from backend.models.semantic_memory import SemanticMemory

from backend.services.memory.memory_extraction import MemoryExtractionService
from backend.services.memory.memory_store import MemoryStore


class _StubEmbeddingService:
    def __init__(self, dimensions: int = 1536) -> None:
        self.dimensions = dimensions

    async def embed(self, text: str) -> list[float]:
        return [float(len(text))] * self.dimensions


class _SequenceClient:
    def __init__(self, payloads: list[dict]) -> None:
        self._payloads = list(payloads)

    async def chat_with_usage(self, _system: str, _user: str, **_kwargs):
        payload = self._payloads.pop(0) if self._payloads else {}
        return SimpleNamespace(content="", structured_output=payload, usage=None)


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[User.__table__, SemanticMemory.__table__])
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory()


def _seed_user(db, username: str) -> User:
    row = User(username=username, password="secret")
    db.add(row)
    db.flush()
    return row


@pytest.mark.asyncio
async def test_duplicate_extraction_deduplicated() -> None:
    engine, db = _build_session()
    try:
        user = _seed_user(db, "dedup-user")
        store = MemoryStore(db=db, embedding_service=_StubEmbeddingService())
        gate_client = _SequenceClient([{"extractable": True}, {"extractable": True}])
        extraction_client = _SequenceClient(
            [
                {
                    "facts": [{"content": "User prefers concise output.", "tier": "user_profile"}],
                    "skipped_reason": None,
                },
                {
                    "facts": [{"content": "User prefers concise output.", "tier": "user_profile"}],
                    "skipped_reason": None,
                },
            ]
        )
        service = MemoryExtractionService(store, gate_client, extraction_client)

        first = await service.extract_if_needed(
            "this is long enough",
            "assistant response",
            user_id=user.id,
            tenant_id=None,
            engagement_id=None,
            task_id=None,
            conversation_id="c1",
            turn_id="t1",
        )
        db.commit()
        second = await service.extract_if_needed(
            "this is long enough",
            "assistant response",
            user_id=user.id,
            tenant_id=None,
            engagement_id=None,
            task_id=None,
            conversation_id="c1",
            turn_id="t2",
        )

        assert len(first) == 1
        assert second == []
        rows = db.execute(select(SemanticMemory)).scalars().all()
        assert len(rows) == 1
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_different_facts_both_stored() -> None:
    engine, db = _build_session()
    try:
        user = _seed_user(db, "different-facts-user")
        store = MemoryStore(db=db, embedding_service=_StubEmbeddingService())
        gate_client = _SequenceClient([{"extractable": True}, {"extractable": True}])
        extraction_client = _SequenceClient(
            [
                {
                    "facts": [{"content": "User prefers markdown tables.", "tier": "user_profile"}],
                    "skipped_reason": None,
                },
                {
                    "facts": [{"content": "User prefers JSON output.", "tier": "user_profile"}],
                    "skipped_reason": None,
                },
            ]
        )
        service = MemoryExtractionService(store, gate_client, extraction_client)

        first = await service.extract_if_needed(
            "this is long enough",
            "assistant response",
            user_id=user.id,
            tenant_id=None,
            engagement_id=None,
            task_id=None,
            conversation_id="c1",
            turn_id="t1",
        )
        second = await service.extract_if_needed(
            "this is long enough",
            "assistant response",
            user_id=user.id,
            tenant_id=None,
            engagement_id=None,
            task_id=None,
            conversation_id="c1",
            turn_id="t2",
        )

        assert len(first) == 1
        assert len(second) == 1
        rows = db.execute(select(SemanticMemory)).scalars().all()
        assert len(rows) == 2
    finally:
        db.close()
        engine.dispose()
