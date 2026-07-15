"""Tests for deterministic CVE sync cursor planning behavior."""

from __future__ import annotations

from datetime import UTC, date, datetime

from backend.services.cve_indexing.contracts import CveSyncKind
from backend.services.cve_indexing.sync_planner import CveSyncStateSnapshot, plan_cve_sync


def _hour(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def test_no_existing_state_returns_baseline() -> None:
    baseline_day = date(2026, 3, 15)

    decision = plan_cve_sync(latest_baseline_date=baseline_day, state=None)

    assert decision.plan.kind == CveSyncKind.BASELINE
    assert decision.plan.baseline_date == baseline_day


def test_same_baseline_day_with_missing_hours_returns_ordered_delta() -> None:
    baseline_day = date(2026, 3, 15)
    state = CveSyncStateSnapshot(
        baseline_date=baseline_day,
        applied_delta_hours=(_hour(baseline_day, 10), _hour(baseline_day, 11)),
    )

    decision = plan_cve_sync(
        latest_baseline_date=baseline_day,
        state=state,
        available_delta_hours=(
            _hour(baseline_day, 10),
            _hour(baseline_day, 11),
            _hour(baseline_day, 12),
            _hour(baseline_day, 13),
        ),
    )

    assert decision.plan.kind == CveSyncKind.DELTA
    assert decision.plan.delta_hours == (_hour(baseline_day, 12), _hour(baseline_day, 13))


def test_new_baseline_day_returns_baseline() -> None:
    old_baseline = date(2026, 3, 14)
    new_baseline = date(2026, 3, 15)
    state = CveSyncStateSnapshot(baseline_date=old_baseline)

    decision = plan_cve_sync(latest_baseline_date=new_baseline, state=state)

    assert decision.plan.kind == CveSyncKind.BASELINE
    assert decision.plan.baseline_date == new_baseline


def test_rebuild_required_flag_forces_baseline() -> None:
    baseline_day = date(2026, 3, 15)
    state = CveSyncStateSnapshot(baseline_date=baseline_day, rebuild_required=True)

    decision = plan_cve_sync(latest_baseline_date=baseline_day, state=state)

    assert decision.plan.kind == CveSyncKind.BASELINE
    assert decision.rebuild_required is True


def test_fully_up_to_date_state_returns_noop() -> None:
    baseline_day = date(2026, 3, 15)
    state = CveSyncStateSnapshot(
        baseline_date=baseline_day,
        applied_delta_hours=(
            _hour(baseline_day, 10),
            _hour(baseline_day, 11),
        ),
    )

    decision = plan_cve_sync(
        latest_baseline_date=baseline_day,
        state=state,
        available_delta_hours=(_hour(baseline_day, 10), _hour(baseline_day, 11)),
    )

    assert decision.plan.kind == CveSyncKind.NOOP


def test_broken_delta_continuity_escalates_to_rebuild_required() -> None:
    baseline_day = date(2026, 3, 15)
    state = CveSyncStateSnapshot(
        baseline_date=baseline_day,
        applied_delta_hours=(
            _hour(baseline_day, 10),
            _hour(baseline_day, 12),
        ),
    )

    decision = plan_cve_sync(
        latest_baseline_date=baseline_day,
        state=state,
        available_delta_hours=(
            _hour(baseline_day, 10),
            _hour(baseline_day, 11),
            _hour(baseline_day, 12),
        ),
    )

    assert decision.plan.kind == CveSyncKind.BASELINE
    assert decision.rebuild_required is True
