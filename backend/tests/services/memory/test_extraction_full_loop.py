"""Validate Runner Control full loop: extract now, retrieve next session, planner sees memory."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT_DIR = Path(__file__).resolve().parents[4]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if "core" not in sys.modules:
    core_pkg = types.ModuleType("core")
    core_pkg.__path__ = [str((ROOT_DIR / "core").resolve())]
    sys.modules["core"] = core_pkg
if "core.prompts" not in sys.modules:
    prompts_pkg = types.ModuleType("core.prompts")
    prompts_pkg.__path__ = [str((ROOT_DIR / "core" / "prompts").resolve())]
    sys.modules["core.prompts"] = prompts_pkg

from agent.graph.nodes import memory_retrieval
from agent.graph.state import FactsState, InteractiveState
from agent.graph.subgraphs.tool_execution import _build_planner_context
from agent.tool_runtime.coordinator import ToolExecutionRequest
from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.semantic_memory import SemanticMemory
from backend.services.embeddings.base import EmbeddingModelRef, EmbeddingProfile
from backend.services.memory.memory_extraction import MemoryExtractionService
from backend.services.memory.memory_store import MemoryStore
from backend.services.memory.runtime_service import MemoryRuntimeService


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


class _RuntimeResolver:
    def resolve_secret(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return SimpleNamespace(value="test-key")


class _EmbeddingFactory:
    def create(self, _selection, *, api_key: str):  # noqa: ANN001
        assert api_key == "test-key"
        provider = _StubEmbeddingService()
        provider.profile = EmbeddingProfile(
            ref=EmbeddingModelRef(provider="openai", model="text-embedding-3-small"),
            dimensions=provider.dimensions,
            vector_family="openai:text-embedding-3-small:1536",
        )
        return provider


@pytest.mark.asyncio
async def test_extraction_then_next_session_retrieval_reaches_planner_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(
        engine,
        tables=[User.__table__, Engagement.__table__, Task.__table__, SemanticMemory.__table__],
    )

    session_a = SessionLocal()
    session_b = SessionLocal()
    try:
        user = User(username="runner-control-loop-user", password="secret")
        session_a.add(user)
        session_a.flush()

        engagement = Engagement(
            user_id=user.id,
            name="Runner Control Test Engagement",
            description="Integration test engagement",
        )
        session_a.add(engagement)
        session_a.flush()

        task = Task(
            user_id=user.id,
            engagement_id=engagement.id,
            name="Runner Control Test Task",
            description="Memory extraction full loop test",
        )
        session_a.add(task)
        session_a.flush()

        memory_sentence = "User prefers concise remediation steps."
        extraction_service = MemoryExtractionService(
            memory_store=MemoryStore(session_a, _StubEmbeddingService()),
            gate_client=_SequenceClient([{"extractable": True}]),
            extraction_client=_SequenceClient(
                [{"facts": [{"content": memory_sentence, "tier": "user_profile"}], "skipped_reason": None}]
            ),
        )

        stored = await extraction_service.extract_if_needed(
            "Please keep explanations concise and practical.",
            "Understood. I will keep responses short and practical.",
            user_id=user.id,
            tenant_id=None,
            engagement_id=engagement.id,
            task_id=task.id,
            conversation_id="conv-a",
            turn_id="turn-a",
        )
        assert len(stored) == 1
        session_a.commit()

        monkeypatch.setattr("backend.database.SessionLocal", lambda: session_b)
        retrieval_update = await memory_retrieval.memory_retrieval_node(
            {
                "facts": {
                    "task_id": task.id,
                    "message": "Continue the assessment",
                    "metadata": {
                        "user_id": user.id,
                        "working_memory": {"objective": {"text": "continue assessment"}},
                    },
                },
                "trace": {"history": [], "reasoning": [], "scratchpad": ""},
            },
            context=None,
            config={
                "configurable": {
                    "runtime_services": SimpleNamespace(
                        memory_runtime_service=MemoryRuntimeService(
                            client_resolver=_RuntimeResolver(),
                            embedding_factory=_EmbeddingFactory(),
                        )
                    ),
                    "llm_runtime_selection": {
                        "provider": "openai",
                        "model": "gpt-5.2",
                        "credential_ref": {"user_id": user.id, "provider": "openai"},
                    },
                    "runtime_projection": {"user_id": user.id, "task_id": task.id},
                }
            },
        )

        summary = retrieval_update["facts"]["metadata"]["long_term_memory_summary"]
        assert memory_sentence in summary

        interactive = InteractiveState(
            facts=FactsState(
                task_id=task.id,
                message="Continue the assessment",
                capability="simple_tool_execution",
                metadata=retrieval_update["facts"]["metadata"],
            )
        )
        request = ToolExecutionRequest(
            capability="simple_tool_execution",
            targets=[],
            message="Continue the assessment",
            task_id=task.id,
            metadata=interactive.facts.metadata,
        )
        planner_context = _build_planner_context(interactive, request)
        # Phase 4 cutover: ``metadata["long_term_memory_summary"]`` is out of
        # the planner hot path (``no-ltm-in-hot-path``). The retrieval node
        # still writes the summary in metadata for parked groundwork, but the
        # planner context omits the key entirely.
        assert "long_term_memory_summary" not in planner_context
    finally:
        session_a.close()
        session_b.close()
        engine.dispose()
