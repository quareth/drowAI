"""Tests for CVE durable lease claim, heartbeat refresh, and stale-run recovery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.models.cve import CveIndexState, CveIndexSyncRun
from backend.services.cve_indexing.lease_service import CveSyncLeaseService


def _make_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    CveIndexSyncRun.__table__.create(bind=engine)
    CveIndexState.__table__.create(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return SessionLocal()


def test_claim_allows_single_owner_until_release() -> None:
    session = _make_session()
    try:
        service = CveSyncLeaseService(session)
        first = service.claim(owner_id="owner-a", ttl_seconds=60)
        second = service.claim(owner_id="owner-b", ttl_seconds=60)
        released = service.release(owner_id="owner-a")
        third = service.claim(owner_id="owner-b", ttl_seconds=60)
    finally:
        session.close()

    assert first.claimed is True
    assert second.claimed is False
    assert second.reason == "already_running"
    assert released is True
    assert third.claimed is True


def test_refresh_extends_lease_expiry_for_current_owner() -> None:
    session = _make_session()
    try:
        service = CveSyncLeaseService(session)
        service.claim(owner_id="owner-a", ttl_seconds=60)
        state_before = session.query(CveIndexState).order_by(CveIndexState.id.asc()).first()
        assert state_before is not None
        expires_before = state_before.lease_expires_at
        refreshed = service.refresh(owner_id="owner-a", ttl_seconds=120)
        state_after = session.query(CveIndexState).order_by(CveIndexState.id.asc()).first()
    finally:
        session.close()

    assert refreshed is True
    assert state_after is not None
    assert state_after.lease_expires_at is not None
    assert expires_before is not None
    assert state_after.lease_expires_at >= expires_before


def test_recover_orphaned_running_when_lease_metadata_missing() -> None:
    session = _make_session()
    now = datetime.now(UTC)
    run = CveIndexSyncRun(
        trigger_kind="manual",
        sync_kind="baseline",
        status="running",
        started_at=now - timedelta(minutes=20),
    )
    state = CveIndexState(
        last_sync_status="running",
        last_attempt_started_at=now - timedelta(minutes=20),
        active_run_id=1,
        rebuild_required=False,
    )
    session.add(run)
    session.commit()
    session.add(state)
    session.commit()

    try:
        service = CveSyncLeaseService(session)
        result = service.recover_orphaned_running()
        refreshed_state = session.query(CveIndexState).order_by(CveIndexState.id.asc()).first()
        refreshed_run = session.query(CveIndexSyncRun).order_by(CveIndexSyncRun.id.asc()).first()
    finally:
        session.close()

    assert result.recovered is True
    assert refreshed_state is not None
    assert refreshed_state.last_sync_status == "failed"
    assert refreshed_state.active_run_id is None
    assert refreshed_run is not None
    assert refreshed_run.status == "failed"


def test_recover_orphaned_running_when_lease_is_expired() -> None:
    session = _make_session()
    now = datetime.now(UTC)
    run = CveIndexSyncRun(
        trigger_kind="schedule",
        sync_kind="delta",
        status="running",
        started_at=now - timedelta(minutes=2),
    )
    session.add(run)
    session.commit()
    state = CveIndexState(
        last_sync_status="running",
        last_attempt_started_at=now - timedelta(minutes=2),
        active_run_id=run.id,
        lease_owner_id="owner-a",
        lease_heartbeat_at=now - timedelta(minutes=2),
        lease_expires_at=now - timedelta(seconds=1),
        rebuild_required=False,
    )
    session.add(state)
    session.commit()

    try:
        service = CveSyncLeaseService(session)
        result = service.recover_orphaned_running()
        refreshed_state = session.query(CveIndexState).order_by(CveIndexState.id.asc()).first()
    finally:
        session.close()

    assert result.recovered is True
    assert refreshed_state is not None
    assert refreshed_state.last_sync_status == "failed"
    assert refreshed_state.lease_owner_id is None


def test_force_fail_owner_run_only_matches_current_owner() -> None:
    session = _make_session()
    now = datetime.now(UTC)
    run = CveIndexSyncRun(
        trigger_kind="manual",
        sync_kind="baseline",
        status="running",
        started_at=now - timedelta(minutes=1),
    )
    session.add(run)
    session.commit()
    state = CveIndexState(
        last_sync_status="running",
        last_attempt_started_at=now - timedelta(minutes=1),
        active_run_id=run.id,
        lease_owner_id="owner-a",
        lease_heartbeat_at=now - timedelta(seconds=20),
        lease_expires_at=now + timedelta(seconds=20),
        rebuild_required=False,
    )
    session.add(state)
    session.commit()

    try:
        service = CveSyncLeaseService(session)
        denied = service.force_fail_owner_run(owner_id="owner-b")
        applied = service.force_fail_owner_run(owner_id="owner-a")
    finally:
        session.close()

    assert denied.recovered is False
    assert applied.recovered is True


def test_claim_succeeds_when_status_running_but_lease_columns_empty() -> None:
    session = _make_session()
    now = datetime.now(UTC)
    state = CveIndexState(
        last_sync_status="running",
        last_attempt_started_at=now - timedelta(minutes=5),
        lease_owner_id=None,
        lease_heartbeat_at=None,
        lease_expires_at=None,
        rebuild_required=False,
    )
    session.add(state)
    session.commit()

    try:
        service = CveSyncLeaseService(session)
        result = service.claim(owner_id="owner-a", ttl_seconds=60)
        refreshed_state = session.query(CveIndexState).order_by(CveIndexState.id.asc()).first()
    finally:
        session.close()

    assert result.claimed is True
    assert refreshed_state is not None
    assert refreshed_state.lease_owner_id == "owner-a"
    assert refreshed_state.lease_heartbeat_at is not None
    assert refreshed_state.lease_expires_at is not None


def test_force_clear_all_resets_running_state() -> None:
    session = _make_session()
    now = datetime.now(UTC)
    run = CveIndexSyncRun(
        trigger_kind="manual",
        sync_kind="delta",
        status="running",
        started_at=now - timedelta(minutes=1),
    )
    session.add(run)
    session.commit()
    state = CveIndexState(
        last_sync_status="running",
        last_attempt_started_at=now - timedelta(minutes=1),
        active_run_id=run.id,
        lease_owner_id="owner-a",
        lease_heartbeat_at=now - timedelta(seconds=15),
        lease_expires_at=now + timedelta(seconds=30),
        rebuild_required=False,
    )
    session.add(state)
    session.commit()

    try:
        service = CveSyncLeaseService(session)
        result = service.force_clear_all()
        refreshed_state = session.query(CveIndexState).order_by(CveIndexState.id.asc()).first()
        refreshed_run = session.query(CveIndexSyncRun).order_by(CveIndexSyncRun.id.asc()).first()
    finally:
        session.close()

    assert result.recovered is True
    assert result.reason == "force_cleared"
    assert refreshed_state is not None
    assert refreshed_state.last_sync_status == "failed"
    assert refreshed_state.active_run_id is None
    assert refreshed_state.lease_owner_id is None
    assert refreshed_state.lease_heartbeat_at is None
    assert refreshed_state.lease_expires_at is None
    assert refreshed_run is not None
    assert refreshed_run.status == "failed"
