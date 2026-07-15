"""Collect management-host resource metrics without HTTP or task concerns.

The service reads memory, workspace-filesystem storage, and host uptime. Task
counts remain tenant-scoped and are intentionally sourced from the tasks API.
"""

from __future__ import annotations

import time
from pathlib import Path

import psutil

from backend.config.workspace_config import WorkspaceConfig
from backend.core.time_utils import utc_now
from backend.schemas.system_metrics import ResourceUsage, SystemMetricsResponse


class SystemMetricsService:
    """Build point-in-time system resource snapshots for presentation clients."""

    def __init__(self, *, storage_path: Path | None = None) -> None:
        self._storage_path = storage_path or WorkspaceConfig.get_project_root()

    def collect(self) -> SystemMetricsResponse:
        """Return current memory, workspace-filesystem, and uptime metrics."""

        memory = psutil.virtual_memory()
        storage = psutil.disk_usage(str(self._storage_path))
        uptime_seconds = max(0, int(time.time() - psutil.boot_time()))

        return SystemMetricsResponse(
            memory=ResourceUsage(
                total_bytes=int(memory.total),
                used_bytes=int(memory.used),
                available_bytes=int(memory.available),
                usage_percent=float(memory.percent),
            ),
            storage=ResourceUsage(
                total_bytes=int(storage.total),
                used_bytes=int(storage.used),
                available_bytes=int(storage.free),
                usage_percent=float(storage.percent),
            ),
            uptime_seconds=uptime_seconds,
            collected_at=utc_now(),
        )
