"""Tests for runtime-provider sync bridge behavior."""

from __future__ import annotations

import asyncio
import threading

from backend.services.runtime_provider.sync_bridge import run_provider_operation_sync


def test_run_provider_operation_sync_runs_coro_factory_on_worker_thread() -> None:
    main_thread = threading.get_ident()
    observed: dict[str, int | None] = {"thread": None}

    async def _operation() -> str:
        observed["thread"] = threading.get_ident()
        return "ok"

    async def _runner() -> None:
        result = run_provider_operation_sync(lambda: _operation())
        assert result == "ok"

    asyncio.run(_runner())
    assert observed["thread"] is not None
    assert observed["thread"] != main_thread


def test_run_provider_operation_sync_returns_none_when_coro_factory_raises() -> None:
    async def _runner() -> None:
        def _coro_factory():
            async def _run() -> str:
                raise RuntimeError("provider read failed")

            return _run()

        result = run_provider_operation_sync(_coro_factory, log_context="test read")
        assert result is None

    asyncio.run(_runner())
