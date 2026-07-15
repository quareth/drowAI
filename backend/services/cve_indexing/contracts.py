"""Contracts for CVE indexing source settings, sync state, and run history.

Scope:
- Defines typed enums/dataclasses for the CVE indexing MVP sync domain.

Boundary:
- Contains no fetch/planning/orchestration logic; only data contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Literal

CVE_SOURCE_KIND: Literal["cvelist_v5"] = "cvelist_v5"


class CveSyncStatus(str, Enum):
    """Lifecycle status for sync state and run history."""

    IDLE = "idle"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CveSyncPhase(str, Enum):
    """Current execution phase within one sync run."""

    RESOLVING = "resolving"
    DOWNLOADING = "downloading"
    UPSERTING = "upserting"
    FINALIZING = "finalizing"


class CveSyncKind(str, Enum):
    """Type of sync work planned or executed."""

    BASELINE = "baseline"
    DELTA = "delta"
    NOOP = "noop"


class CveSyncTriggerKind(str, Enum):
    """Source trigger that starts a sync run."""

    MANUAL = "manual"
    SCHEDULE = "schedule"
    SYSTEM = "system"


@dataclass(slots=True, frozen=True)
class CveSyncPlan:
    """Planned sync operation for one scheduler decision."""

    kind: CveSyncKind
    baseline_date: date | None = None
    delta_hours: tuple[datetime, ...] = ()


__all__ = [
    "CVE_SOURCE_KIND",
    "CveSyncKind",
    "CveSyncPlan",
    "CveSyncPhase",
    "CveSyncStatus",
    "CveSyncTriggerKind",
]
