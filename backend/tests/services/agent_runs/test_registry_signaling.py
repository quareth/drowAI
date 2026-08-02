"""Direct tests for process-local registry state-change signaling."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from backend.services.agent_runs.registry_signaling import RegistryStateSignal
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry


@pytest.mark.asyncio
async def test_transition_before_wait_returns_without_blocking() -> None:
    owner_lock = asyncio.Lock()
    signal = RegistryStateSignal(owner_lock)

    async with owner_lock:
        signal.mark_changed_locked()
    await signal.notify_after_commit()

    assert await signal.state_version() == 1
    assert await signal.wait_for_state_change(after_version=0) == 1


@pytest.mark.asyncio
async def test_wait_before_transition_observes_notify_after_commit() -> None:
    owner_lock = asyncio.Lock()
    signal = RegistryStateSignal(owner_lock)
    waiter = asyncio.create_task(signal.wait_for_state_change(after_version=0))
    await asyncio.sleep(0)

    async with owner_lock:
        signal.mark_changed_locked()
    assert waiter.done() is False

    await signal.notify_after_commit()

    assert await asyncio.wait_for(waiter, timeout=1) == 1


@pytest.mark.asyncio
async def test_predicate_wait_ignores_unrelated_wakeups_until_scoped_ready() -> None:
    owner_lock = asyncio.Lock()
    signal = RegistryStateSignal(owner_lock)
    scoped_ready = False

    def _predicate() -> str | None:
        return "ready" if scoped_ready else None

    waiter = asyncio.create_task(
        signal.wait_for_predicate(after_version=0, predicate=_predicate)
    )
    await asyncio.sleep(0)

    await signal.notify_after_commit()
    await asyncio.sleep(0)
    assert waiter.done() is False

    async with owner_lock:
        signal.mark_changed_locked()
    await signal.notify_after_commit()
    await asyncio.sleep(0)
    assert waiter.done() is False

    async with owner_lock:
        scoped_ready = True
        signal.mark_changed_locked()
    await signal.notify_after_commit()

    assert await asyncio.wait_for(waiter, timeout=1) == "ready"


@pytest.mark.asyncio
async def test_multiple_waiters_observe_single_committed_transition() -> None:
    owner_lock = asyncio.Lock()
    signal = RegistryStateSignal(owner_lock)
    waiters = [
        asyncio.create_task(signal.wait_for_state_change(after_version=0))
        for _ in range(5)
    ]
    await asyncio.sleep(0)

    async with owner_lock:
        signal.mark_changed_locked()
    await signal.notify_after_commit()

    assert await asyncio.wait_for(asyncio.gather(*waiters), timeout=1) == [
        1,
        1,
        1,
        1,
        1,
    ]


@pytest.mark.asyncio
async def test_wait_cancellation_propagates_and_future_waiters_still_work() -> None:
    owner_lock = asyncio.Lock()
    signal = RegistryStateSignal(owner_lock)
    waiter = asyncio.create_task(signal.wait_for_state_change(after_version=0))
    await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    next_waiter = asyncio.create_task(signal.wait_for_state_change(after_version=0))
    await asyncio.sleep(0)
    async with owner_lock:
        signal.mark_changed_locked()
    await signal.notify_after_commit()

    assert await asyncio.wait_for(next_waiter, timeout=1) == 1


@pytest.mark.asyncio
async def test_predicate_wait_returns_immediate_inactive_value() -> None:
    owner_lock = asyncio.Lock()
    signal = RegistryStateSignal(owner_lock)

    assert (
        await signal.wait_for_predicate(
            after_version=await signal.state_version(),
            predicate=lambda: "inactive",
        )
        == "inactive"
    )


def test_registry_signal_boundary_and_facade_delegates_to_signal() -> None:
    signal_path = (
        Path(__file__).resolve().parents[3]
        / "services/agent_runs/registry_signaling.py"
    )
    registry_path = (
        Path(__file__).resolve().parents[3] / "services/agent_runs/registry.py"
    )
    signal_source = signal_path.read_text(encoding="utf-8")
    signal_tree = ast.parse(signal_source)
    imports: set[tuple[int, str]] = set()
    for node in ast.walk(signal_tree):
        if isinstance(node, ast.Import):
            imports.update((0, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add((node.level, node.module))

    assert imports == {
        (0, "__future__"),
        (0, "asyncio"),
        (0, "collections.abc"),
        (0, "typing"),
    }
    assert "self._owner_lock = owner_lock" in signal_source
    assert "asyncio.Lock()" not in signal_source
    assert "_runs" not in signal_source
    assert "_claims" not in signal_source
    assert "_store" not in signal_source
    assert "safe_inc" not in signal_source
    assert "safe_gauge" not in signal_source
    assert "logger" not in signal_source
    assert RegistryStateSignal.__module__.endswith("registry_signaling")

    registry_source = registry_path.read_text(encoding="utf-8")
    registry_tree = ast.parse(registry_source)
    registry_imports_signaling = any(
        (
            isinstance(node, ast.ImportFrom)
            and node.module
            in {"registry_signaling", "backend.services.agent_runs.registry_signaling"}
        )
        or (
            isinstance(node, ast.ImportFrom)
            and node.module is None
            and any(alias.name == "registry_signaling" for alias in node.names)
        )
        for node in ast.walk(registry_tree)
    )
    assert registry_imports_signaling is True
    assert "_state_version" not in registry_source
    assert "_state_changed" not in registry_source
    assert "def _mark_state_changed_locked" not in registry_source
    assert "def _notify_state_changed" not in registry_source
    assert "asyncio.Condition" not in registry_source
    first_registry = ProcessLocalAgentRunRegistry()
    second_registry = ProcessLocalAgentRunRegistry()
    assert isinstance(first_registry._signal, RegistryStateSignal)
    assert isinstance(second_registry._signal, RegistryStateSignal)
    assert first_registry._signal is not second_registry._signal
