"""Tests for shared structured-contract retry behavior.

This module verifies that the common retry helper handles both structured
contract violations and retryable LLM timeout errors without stage-specific
retry loops.
"""

import logging

import pytest

from agent.reasoning.structured_contract_recovery import run_structured_contract_retry
from core.llm.timeout_runtime import LLMTimeoutError


def _timeout_error() -> LLMTimeoutError:
    """Build a representative retryable LLM timeout error."""
    return LLMTimeoutError(
        task_id=123,
        component="PLANNER",
        operation="tool_selection_llm_call",
        timeout_sec=120,
        outcome="selection_timeout",
    )


@pytest.mark.asyncio
async def test_structured_contract_retry_recovers_after_llm_timeout() -> None:
    calls = 0

    async def _operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _timeout_error()
        return "ok"

    result = await run_structured_contract_retry(
        operation=_operation,
        logger=logging.getLogger("test.structured_contract.timeout_recovery"),
        stage="planner",
        contract="action_plan",
        max_attempts=2,
        backoff_seconds=0,
    )

    assert result == "ok"
    assert calls == 2


@pytest.mark.asyncio
async def test_structured_contract_retry_exhausts_llm_timeout() -> None:
    calls = 0

    async def _operation() -> str:
        nonlocal calls
        calls += 1
        raise _timeout_error()

    with pytest.raises(LLMTimeoutError):
        await run_structured_contract_retry(
            operation=_operation,
            logger=logging.getLogger("test.structured_contract.timeout_exhausted"),
            stage="planner",
            contract="action_plan",
            max_attempts=2,
            backoff_seconds=0,
        )

    assert calls == 2
