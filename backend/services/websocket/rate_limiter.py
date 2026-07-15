"""WebSocket rate limiting and circuit breaker logic.

Scope:
- Per-IP and optional per-user connection rate limiting.
- Task-scoped connection quota validation.
- Circuit breaker state transitions for repeated connection failures.

Boundary:
- No websocket transport I/O, connection registry, or streaming logic.
- No channel-specific behavior or payload handling.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

from backend.core.time_utils import utc_now

logger = logging.getLogger("backend.services.ws_rate_limiter")


class CircuitBreakerState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class WSRateLimiter:
    def __init__(
        self,
        *,
        max_connections_per_task: int = 10,
        total_max_connections: int = 1000,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_timeout: int = 300,
        circuit_breaker_half_open_timeout: int = 30,
    ) -> None:
        self.max_connections_per_task = max_connections_per_task
        self.total_max_connections = total_max_connections

        self.connection_limits: Dict[str, int] = defaultdict(int)
        self.circuit_breakers: Dict[int, Dict[str, Any]] = {}
        self.error_counts: Dict[int, int] = defaultdict(int)

        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.circuit_breaker_timeout = circuit_breaker_timeout
        self.circuit_breaker_half_open_timeout = circuit_breaker_half_open_timeout

        self._ip_token_buckets: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"tokens": 10, "last_refill": utc_now()}
        )
        self._user_token_buckets: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"tokens": 10, "last_refill": utc_now()}
        )
        self._ip_sliding_windows: Dict[str, List[datetime]] = defaultdict(list)
        self._user_sliding_windows: Dict[str, List[datetime]] = defaultdict(list)
        self._rate_limit_config = {
            "token_bucket_capacity": 10,
            "token_bucket_refill_rate": 1,
            "sliding_window_size": 60,
            "sliding_window_max": 30,
        }

    async def check_rate_limit(self, client_ip: str) -> bool:
        count = self.connection_limits.get(client_ip, 0)
        if count >= self.max_connections_per_task:
            return False
        self.connection_limits[client_ip] = count + 1
        return True

    async def check_rate_limit_advanced(self, client_ip: str, user_id: Optional[str]) -> bool:
        now = utc_now()
        cfg = self._rate_limit_config
        allowed = True

        ip_bucket = self._ip_token_buckets[client_ip]
        elapsed = (now - ip_bucket["last_refill"]).total_seconds()
        refill = int(elapsed * cfg["token_bucket_refill_rate"])
        if refill > 0:
            ip_bucket["tokens"] = min(cfg["token_bucket_capacity"], ip_bucket["tokens"] + refill)
            ip_bucket["last_refill"] = now
        if ip_bucket["tokens"] > 0:
            ip_bucket["tokens"] -= 1
        else:
            logger.warning("[WSRateLimiter] IP %s rate limited by token bucket.", client_ip)
            allowed = False

        window = self._ip_sliding_windows[client_ip]
        window = [t for t in window if (now - t).total_seconds() < cfg["sliding_window_size"]]
        window.append(now)
        self._ip_sliding_windows[client_ip] = window
        if len(window) > cfg["sliding_window_max"]:
            logger.warning("[WSRateLimiter] IP %s rate limited by sliding window.", client_ip)
            allowed = False

        if user_id:
            user_bucket = self._user_token_buckets[user_id]
            elapsed = (now - user_bucket["last_refill"]).total_seconds()
            refill = int(elapsed * cfg["token_bucket_refill_rate"])
            if refill > 0:
                user_bucket["tokens"] = min(cfg["token_bucket_capacity"], user_bucket["tokens"] + refill)
                user_bucket["last_refill"] = now
            if user_bucket["tokens"] > 0:
                user_bucket["tokens"] -= 1
            else:
                logger.warning("[WSRateLimiter] User %s rate limited by token bucket.", user_id)
                allowed = False

            uwindow = self._user_sliding_windows[user_id]
            uwindow = [t for t in uwindow if (now - t).total_seconds() < cfg["sliding_window_size"]]
            uwindow.append(now)
            self._user_sliding_windows[user_id] = uwindow
            if len(uwindow) > cfg["sliding_window_max"]:
                logger.warning("[WSRateLimiter] User %s rate limited by sliding window.", user_id)
                allowed = False

        if not allowed:
            logger.info("[WSRateLimiter] Rate limit exceeded for IP %s user %s", client_ip, user_id)
        return allowed

    def validate_connection_limits(
        self,
        *,
        task_id: int,
        active_connections: int,
        task_connection_count: int,
    ) -> bool:
        if active_connections >= self.total_max_connections:
            logger.warning("[WSRateLimiter] Total max connections exceeded.")
            return False
        if task_connection_count >= self.max_connections_per_task:
            logger.warning("[WSRateLimiter] Max connections for task %s exceeded.", task_id)
            return False
        return True

    def should_allow_connection(self, task_id: int) -> bool:
        breaker = self.circuit_breakers.get(task_id)
        now = utc_now()
        if not breaker:
            return True

        state = breaker.get("state", CircuitBreakerState.CLOSED)
        opened_at = breaker.get("opened_at")
        half_opened_at = breaker.get("half_opened_at")

        if state == CircuitBreakerState.OPEN:
            if opened_at and now - opened_at > timedelta(seconds=self.circuit_breaker_timeout):
                self.circuit_breakers[task_id]["state"] = CircuitBreakerState.HALF_OPEN
                self.circuit_breakers[task_id]["half_opened_at"] = now
                logger.info("[WSRateLimiter] Task %s moved to HALF_OPEN state.", task_id)
                return True
            logger.warning("[WSRateLimiter] Task %s in OPEN state. Connection denied.", task_id)
            return False

        if state == CircuitBreakerState.HALF_OPEN:
            if half_opened_at and now - half_opened_at > timedelta(
                seconds=self.circuit_breaker_half_open_timeout
            ):
                self.reset_circuit_breaker(task_id)
                logger.info(
                    "[WSRateLimiter] Task %s moved to CLOSED state from HALF_OPEN.", task_id
                )
                return True
            logger.info(
                "[WSRateLimiter] Task %s in HALF_OPEN state. Allowing test connection.", task_id
            )
            return True

        return True

    def record_connection_failure(self, task_id: int) -> None:
        count = self.error_counts.get(task_id, 0) + 1
        self.error_counts[task_id] = count
        if count >= self.circuit_breaker_threshold:
            self.circuit_breakers[task_id] = {
                "state": CircuitBreakerState.OPEN,
                "opened_at": utc_now(),
            }
            logger.warning(
                "[WSRateLimiter] Task %s moved to OPEN state due to repeated failures.",
                task_id,
            )

    def reset_circuit_breaker(self, task_id: int) -> None:
        self.error_counts[task_id] = 0
        if task_id in self.circuit_breakers:
            logger.info("[WSRateLimiter] Task %s circuit breaker reset to CLOSED.", task_id)
            self.circuit_breakers[task_id]["state"] = CircuitBreakerState.CLOSED
            self.circuit_breakers[task_id].pop("opened_at", None)
            self.circuit_breakers[task_id].pop("half_opened_at", None)
        else:
            self.circuit_breakers.pop(task_id, None)

    def decrement_connection_limit(self, client_ip: Optional[str]) -> None:
        if not client_ip:
            return
        if client_ip in self.connection_limits and self.connection_limits[client_ip] > 0:
            self.connection_limits[client_ip] -= 1
