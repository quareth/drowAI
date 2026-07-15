"""Tests for websocket rate limiter and circuit breaker behavior."""

from __future__ import annotations

from datetime import timedelta

import pytest

from backend.core.time_utils import utc_now
from backend.services.websocket.rate_limiter import CircuitBreakerState, WSRateLimiter


@pytest.mark.asyncio
async def test_check_rate_limit_blocks_after_capacity() -> None:
    limiter = WSRateLimiter(max_connections_per_task=2)

    assert await limiter.check_rate_limit("1.1.1.1")
    assert await limiter.check_rate_limit("1.1.1.1")
    assert not await limiter.check_rate_limit("1.1.1.1")


def test_circuit_breaker_moves_open_then_half_open_after_timeout() -> None:
    limiter = WSRateLimiter(circuit_breaker_threshold=1, circuit_breaker_timeout=1)

    limiter.record_connection_failure(99)
    assert limiter.circuit_breakers[99]["state"] == CircuitBreakerState.OPEN
    assert not limiter.should_allow_connection(99)

    limiter.circuit_breakers[99]["opened_at"] = utc_now() - timedelta(seconds=2)
    assert limiter.should_allow_connection(99)
    assert limiter.circuit_breakers[99]["state"] == CircuitBreakerState.HALF_OPEN
