"""Lightweight metrics aggregator:
- Emits periodic log lines with simple counters for SSE and reasoning APIs
This avoids adding a full metrics stack; can be replaced with Prometheus later."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import DefaultDict

from backend.config import METRICS_ENABLED, METRICS_LOG_INTERVAL_SEC

logger = logging.getLogger("backend.services.metrics")


class Metrics:
    def __init__(self) -> None:
        self.enabled = METRICS_ENABLED
        self.counters: DefaultDict[str, int] = defaultdict(int)
        self.gauges: DefaultDict[str, float] = defaultdict(float)
        self._task: asyncio.Task | None = None

    def inc(self, name: str, value: int = 1) -> None:
        if not self.enabled:
            return
        self.counters[name] += value

    def gauge(self, name: str, value: float) -> None:
        if not self.enabled:
            return
        self.gauges[name] = float(value)

    def snapshot(self) -> dict:
        """Return a shallow copy of current counters/gauges without resetting."""
        if not self.enabled:
            return {"enabled": False}
        return {
            "enabled": True,
            "counters": dict(self.counters),
            "gauges": dict(self.gauges),
        }

    async def start(self) -> None:
        if not self.enabled or self._task:
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(METRICS_LOG_INTERVAL_SEC)
                if not self.enabled:
                    continue
                if self.counters or self.gauges:
                    logger.info("[metrics] counters=%s gauges=%s", dict(self.counters), dict(self.gauges))
                    # Reset counters after reporting (gauges are level-based)
                    self.counters.clear()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.error("metrics loop error", exc_info=True)


metrics = Metrics()

