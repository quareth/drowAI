"""Tests for shared timeout runtime wrappers and canonical timeout logging."""

from __future__ import annotations

import asyncio
import logging

import pytest

from core.llm.timeout_runtime import (
    LLMTimeoutError,
    format_timeout_log_message,
    iter_with_idle_timeout,
    wait_for_with_timeout,
)


def test_format_timeout_log_message_uses_task_na_when_missing() -> None:
    assert format_timeout_log_message(
        task_id=None,
        component="TEST_COMPONENT",
        operation="test_operation",
        timeout_sec=60,
        outcome="request_timeout",
        details="extra=value",
    ) == (
        "TIMEOUT | Task n/a | TEST_COMPONENT | test_operation | "
        "timeout_sec=60.00 | outcome=request_timeout | extra=value"
    )


@pytest.mark.asyncio
async def test_wait_for_with_timeout_logs_and_reraises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.timeout_runtime.wait_for")

    async def _slow() -> str:
        await asyncio.sleep(0.05)
        return "never"

    with caplog.at_level(logging.WARNING):
        with pytest.raises(LLMTimeoutError) as exc_info:
            await wait_for_with_timeout(
                _slow(),
                timeout_sec=0.01,
                component="TEST_COMPONENT",
                operation="wait_for_case",
                logger=logger,
                task_id=123,
                outcome="request_timeout",
                details="mode=unit_test",
            )

    assert isinstance(exc_info.value, asyncio.TimeoutError)
    assert exc_info.value.error_code == "llm_timeout"
    assert exc_info.value.retryable is True
    assert exc_info.value.retry_mode == "checkpoint"
    assert exc_info.value.diagnostics == {
        "component": "TEST_COMPONENT",
        "operation": "wait_for_case",
        "timeout_sec": 0.01,
        "outcome": "request_timeout",
        "task_id": "123",
        "details": "mode=unit_test",
    }
    assert (
        "TIMEOUT | Task 123 | TEST_COMPONENT | wait_for_case | "
        "timeout_sec=0.01 | outcome=request_timeout | mode=unit_test"
    ) in caplog.text


@pytest.mark.asyncio
async def test_iter_with_idle_timeout_allows_progress_without_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.timeout_runtime.iter_ok")

    async def _stream():
        yield "a"
        await asyncio.sleep(0.005)
        yield "b"

    with caplog.at_level(logging.WARNING):
        chunks = [
            chunk
            async for chunk in iter_with_idle_timeout(
                _stream(),
                timeout_sec=0.05,
                component="TEST_COMPONENT",
                operation="stream_ok",
                logger=logger,
                task_id=5,
            )
        ]

    assert chunks == ["a", "b"]
    assert "TIMEOUT |" not in caplog.text


@pytest.mark.asyncio
async def test_iter_with_idle_timeout_logs_and_reraises_on_stall(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.timeout_runtime.iter_timeout")

    async def _stalled_stream():
        yield "a"
        await asyncio.sleep(0.05)
        yield "b"

    with caplog.at_level(logging.WARNING):
        with pytest.raises(LLMTimeoutError) as exc_info:
            async for _chunk in iter_with_idle_timeout(
                _stalled_stream(),
                timeout_sec=0.01,
                component="TEST_COMPONENT",
                operation="stream_timeout",
                logger=logger,
                task_id=7,
                outcome="stream_idle_timeout",
                details="path=unit_test",
            ):
                pass

    assert isinstance(exc_info.value, asyncio.TimeoutError)
    assert exc_info.value.diagnostics == {
        "component": "TEST_COMPONENT",
        "operation": "stream_timeout",
        "timeout_sec": 0.01,
        "outcome": "stream_idle_timeout",
        "task_id": "7",
        "details": "path=unit_test",
    }
    assert (
        "TIMEOUT | Task 7 | TEST_COMPONENT | stream_timeout | "
        "timeout_sec=0.01 | outcome=stream_idle_timeout | path=unit_test"
    ) in caplog.text


@pytest.mark.asyncio
async def test_wait_for_with_timeout_redacts_sensitive_diagnostics() -> None:
    logger = logging.getLogger("test.timeout_runtime.redacted")

    async def _slow() -> str:
        await asyncio.sleep(0.05)
        return "never"

    with pytest.raises(LLMTimeoutError) as exc_info:
        await wait_for_with_timeout(
            _slow(),
            timeout_sec=0.01,
            component="TEST_COMPONENT",
            operation="redacted_case",
            logger=logger,
            details="authorization=Bearer secret-token",
        )

    assert exc_info.value.diagnostics["details"] == "<redacted>"
