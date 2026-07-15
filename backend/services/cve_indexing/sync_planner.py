"""Planner module for deterministic CVE baseline/delta sync decisions.

Scope:
- Defines planner snapshots and planner decisions.
- Computes baseline/delta/noop plans from persisted cursor state and source hours.

Boundary:
- Contains no network I/O, parsing, or DB write orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

from backend.models.cve import CveIndexState
from backend.services.cve_indexing.contracts import CveSyncKind, CveSyncPlan
from backend.services.cve_indexing.primitives import to_utc_hour


@dataclass(slots=True, frozen=True)
class CveSyncStateSnapshot:
    """Persisted cursor snapshot used for one planning decision."""

    baseline_date: date | None
    applied_delta_hours: tuple[datetime, ...] = ()
    rebuild_required: bool = False


@dataclass(slots=True, frozen=True)
class CveSyncPlannerDecision:
    """Planner result plus whether state should be marked rebuild-required."""

    plan: CveSyncPlan
    rebuild_required: bool = False


def plan_cve_sync(
    *,
    latest_baseline_date: date,
    state: CveSyncStateSnapshot | None,
    available_delta_hours: Iterable[datetime] = (),
) -> CveSyncPlannerDecision:
    """Plan baseline/delta/noop work from persisted cursor state and source cursor data."""

    if state is None or state.baseline_date is None:
        return CveSyncPlannerDecision(
            plan=CveSyncPlan(kind=CveSyncKind.BASELINE, baseline_date=latest_baseline_date),
        )

    if state.rebuild_required:
        return CveSyncPlannerDecision(
            plan=CveSyncPlan(kind=CveSyncKind.BASELINE, baseline_date=latest_baseline_date),
            rebuild_required=True,
        )

    if state.baseline_date != latest_baseline_date:
        return CveSyncPlannerDecision(
            plan=CveSyncPlan(kind=CveSyncKind.BASELINE, baseline_date=latest_baseline_date),
        )

    applied_hours = _normalize_hours(state.applied_delta_hours, latest_baseline_date)
    if _has_hour_gaps(applied_hours):
        return CveSyncPlannerDecision(
            plan=CveSyncPlan(kind=CveSyncKind.BASELINE, baseline_date=latest_baseline_date),
            rebuild_required=True,
        )

    source_hours = _normalize_hours(available_delta_hours, latest_baseline_date)
    if not source_hours:
        return CveSyncPlannerDecision(plan=CveSyncPlan(kind=CveSyncKind.NOOP))

    applied_set = set(applied_hours)
    missing_hours = tuple(hour for hour in source_hours if hour not in applied_set)
    if not missing_hours:
        return CveSyncPlannerDecision(plan=CveSyncPlan(kind=CveSyncKind.NOOP))

    if applied_hours:
        newest_applied = max(applied_hours)
        # If older source hours are still missing while newer ones are marked applied,
        # continuity is broken and we force a rebuild path.
        if any(hour < newest_applied for hour in missing_hours):
            return CveSyncPlannerDecision(
                plan=CveSyncPlan(kind=CveSyncKind.BASELINE, baseline_date=latest_baseline_date),
                rebuild_required=True,
            )

    return CveSyncPlannerDecision(
        plan=CveSyncPlan(kind=CveSyncKind.DELTA, delta_hours=missing_hours),
    )


def normalize_hours(hours: Iterable[datetime], baseline_day: date) -> tuple[datetime, ...]:
    """Normalize hours to UTC and keep only hours from the baseline day."""

    normalized = set()
    for hour in hours:
        normalized_hour = to_utc_hour(hour)
        if normalized_hour.date() == baseline_day:
            normalized.add(normalized_hour)
    return tuple(sorted(normalized))


def _normalize_hours(hours: Iterable[datetime], baseline_day: date) -> tuple[datetime, ...]:
    return normalize_hours(hours, baseline_day)


def _has_hour_gaps(hours: tuple[datetime, ...]) -> bool:
    """True when applied hours contain a continuity gap."""

    if len(hours) < 2:
        return False

    for previous, current in zip(hours, hours[1:]):
        if int((current - previous).total_seconds()) != 3600:
            return True
    return False


def build_applied_hours_snapshot(state: CveIndexState) -> tuple[datetime, ...]:
    """Build a contiguous applied-hour snapshot from persisted baseline/hour cursor."""
    baseline_day = state.last_applied_baseline_date
    latest_applied_hour = state.last_applied_delta_hour_utc
    if baseline_day is None or latest_applied_hour is None:
        return ()

    normalized = to_utc_hour(latest_applied_hour)
    if normalized.date() != baseline_day:
        return ()

    return tuple(
        datetime(
            baseline_day.year,
            baseline_day.month,
            baseline_day.day,
            hour,
            tzinfo=normalized.tzinfo,
        )
        for hour in range(0, normalized.hour + 1)
    )


__all__ = [
    "CveSyncPlannerDecision",
    "CveSyncStateSnapshot",
    "build_applied_hours_snapshot",
    "normalize_hours",
    "plan_cve_sync",
]
