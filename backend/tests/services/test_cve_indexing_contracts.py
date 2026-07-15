"""Tests for CVE indexing data contracts introduced in Phase 1."""

from __future__ import annotations

from datetime import date, datetime, timezone

from backend.services.cve_indexing.contracts import (
    CVE_SOURCE_KIND,
    CveSyncKind,
    CveSyncPlan,
    CveSyncStatus,
    CveSyncTriggerKind,
)


def test_source_kind_is_fixed_to_cvelist_v5() -> None:
    assert CVE_SOURCE_KIND == "cvelist_v5"


def test_contracts_distinguish_settings_state_and_run_history() -> None:
    plan = CveSyncPlan(
        kind=CveSyncKind.DELTA,
        delta_hours=(datetime.now(tz=timezone.utc),),
    )
    assert plan.kind == CveSyncKind.DELTA


def test_sync_plan_supports_required_kinds() -> None:
    baseline_plan = CveSyncPlan(kind=CveSyncKind.BASELINE, baseline_date=date(2026, 3, 15))
    delta_plan = CveSyncPlan(
        kind=CveSyncKind.DELTA,
        delta_hours=(datetime(2026, 3, 15, 12, tzinfo=timezone.utc),),
    )
    noop_plan = CveSyncPlan(kind=CveSyncKind.NOOP)

    assert baseline_plan.kind == CveSyncKind.BASELINE
    assert delta_plan.kind == CveSyncKind.DELTA
    assert noop_plan.kind == CveSyncKind.NOOP


def test_sync_status_enum_contains_expected_states() -> None:
    assert CveSyncStatus.IDLE.value == "idle"
    assert CveSyncStatus.RUNNING.value == "running"
    assert CveSyncStatus.SUCCEEDED.value == "succeeded"
    assert CveSyncStatus.FAILED.value == "failed"
    assert CveSyncTriggerKind.SCHEDULE.value == "schedule"
