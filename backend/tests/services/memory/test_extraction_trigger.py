"""Validate extraction trigger enqueue and provider-safe worker boundaries."""

from __future__ import annotations

import importlib
import sys
import types
from typing import Any

extraction_trigger = importlib.import_module("backend.services.memory.extraction_trigger")


def _runtime_selection() -> dict[str, Any]:
    return {
        "provider": "openai",
        "model": "gpt-5.2",
        "credential_ref": {"user_id": 1, "provider": "openai"},
        "reasoning_effort": "medium",
    }


def _install_worker_dependencies(monkeypatch, *, raise_on_extract: bool = False):
    state = {
        "session_created": 0,
        "commit": 0,
        "rollback": 0,
        "close": 0,
        "extract_calls": [],
    }

    class _FakeDB:
        def commit(self):
            state["commit"] += 1

        def rollback(self):
            state["rollback"] += 1

        def close(self):
            state["close"] += 1

    def _session_local():
        state["session_created"] += 1
        return _FakeDB()

    mod_db = types.ModuleType("backend.database")
    mod_db.SessionLocal = _session_local
    monkeypatch.setitem(sys.modules, "backend.database", mod_db)

    class _MemoryRuntimeService:
        async def run_extraction(self, **kwargs: Any) -> None:
            state["extract_calls"].append(kwargs)
            if raise_on_extract:
                raise RuntimeError("forced extraction failure")

    monkeypatch.setattr(
        extraction_trigger,
        "_build_memory_runtime_service",
        lambda _db: _MemoryRuntimeService(),
    )
    return state


def test_enqueue_does_not_raise_on_failure(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_SEMANTIC_MEMORY_RUNTIME", "true")
    metric_calls: list[str] = []

    def _raise_no_loop():
        raise RuntimeError("no running event loop")

    monkeypatch.setattr(extraction_trigger.asyncio, "get_running_loop", _raise_no_loop)
    monkeypatch.setattr(
        extraction_trigger,
        "safe_inc",
        lambda metric_name: metric_calls.append(metric_name),
    )

    extraction_trigger.enqueue_memory_extraction(
        user_message="hello",
        assistant_response="world",
        user_id=1,
        task_id=2,
        conversation_id="c1",
        turn_id="t1",
        llm_runtime_selection=_runtime_selection(),
    )
    assert metric_calls == ["memory_extraction_enqueue_failures"]


def test_enqueue_skips_when_semantic_memory_disabled(monkeypatch) -> None:
    monkeypatch.delenv("ENABLE_SEMANTIC_MEMORY_RUNTIME", raising=False)
    calls: list[str] = []

    monkeypatch.setattr(
        extraction_trigger.asyncio,
        "get_running_loop",
        lambda: calls.append("loop"),
    )

    extraction_trigger.enqueue_memory_extraction(
        user_message="hello",
        assistant_response="world",
        user_id=1,
        task_id=2,
        conversation_id="c1",
        turn_id="t1",
        llm_runtime_selection=_runtime_selection(),
    )

    assert calls == []


def test_worker_skips_when_no_user_id(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_SEMANTIC_MEMORY_RUNTIME", "true")
    state = _install_worker_dependencies(monkeypatch)

    extraction_trigger.run_memory_extraction_once(
        user_message="hello",
        assistant_response="world",
        user_id=None,
        task_id=2,
        conversation_id="c1",
        turn_id="t1",
        llm_runtime_selection=_runtime_selection(),
    )

    assert state["session_created"] == 0


def test_worker_skips_when_runtime_selection_missing(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_SEMANTIC_MEMORY_RUNTIME", "true")
    state = _install_worker_dependencies(monkeypatch)

    extraction_trigger.run_memory_extraction_once(
        user_message="hello",
        assistant_response="world",
        user_id=1,
        task_id=2,
        conversation_id="c1",
        turn_id="t1",
    )

    assert state["session_created"] == 0
    assert state["extract_calls"] == []


def test_worker_skips_when_semantic_memory_disabled(monkeypatch) -> None:
    monkeypatch.delenv("ENABLE_SEMANTIC_MEMORY_RUNTIME", raising=False)
    state = _install_worker_dependencies(monkeypatch)

    extraction_trigger.run_memory_extraction_once(
        user_message="hello",
        assistant_response="world",
        user_id=1,
        task_id=2,
        conversation_id="c1",
        turn_id="t1",
        llm_runtime_selection=_runtime_selection(),
    )

    assert state["session_created"] == 0
    assert state["extract_calls"] == []


def test_worker_passes_runtime_selection_snapshot_to_memory_service(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_SEMANTIC_MEMORY_RUNTIME", "true")
    state = _install_worker_dependencies(monkeypatch)
    selection = _runtime_selection()

    extraction_trigger.run_memory_extraction_once(
        user_message="hello",
        assistant_response="world",
        user_id=1,
        task_id=2,
        conversation_id="c1",
        turn_id="t1",
        llm_runtime_selection=selection,
    )

    assert state["commit"] == 1
    assert state["rollback"] == 0
    assert len(state["extract_calls"]) == 1
    call = state["extract_calls"][0]
    assert call["selection"] == selection
    assert call["user_id"] == 1
    assert call["task_id"] == 2
    assert call["conversation_id"] == "c1"
    assert call["turn_id"] == "t1"


def test_worker_rollbacks_on_error(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_SEMANTIC_MEMORY_RUNTIME", "true")
    state = _install_worker_dependencies(monkeypatch, raise_on_extract=True)

    extraction_trigger.run_memory_extraction_once(
        user_message="hello",
        assistant_response="world",
        user_id=1,
        task_id=2,
        conversation_id="c1",
        turn_id="t1",
        llm_runtime_selection=_runtime_selection(),
    )

    assert state["commit"] == 0
    assert state["rollback"] == 1
    assert state["close"] == 1
