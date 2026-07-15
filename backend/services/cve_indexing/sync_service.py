"""Planner and orchestration for deterministic CVE baseline/delta sync runs.

Scope:
- Encodes cursor rules for baseline, delta, and noop planning.
- Executes sync plans, upserts CVE records, and writes run/state durability rows.

Boundary:
- Does not expose HTTP routes or background scheduling policies.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.models.cve import CveIndexState, CveIndexSyncRun, CveRecord
from backend.services.cve_indexing.contracts import CveSyncKind, CveSyncPlan, CveSyncTriggerKind
from backend.services.cve_indexing.parser import CveParsedRecord, CveZipParser
from backend.services.cve_indexing.primitives import utc_now
from backend.services.cve_indexing.sync_record_projection import (
    apply_projection_state,
    apply_record_update,
    hash_record_payload,
    new_cve_record,
)
from backend.services.cve_indexing.state_store import get_or_create_cve_index_state
from backend.services.cve_indexing.source_client import CveSourceAsset, CveSourceClient
from backend.services.cve_indexing.sync_planner import (
    CveSyncPlannerDecision,
    CveSyncStateSnapshot,
    build_applied_hours_snapshot,
    plan_cve_sync,
)


@dataclass(slots=True, frozen=True)
class CveSyncCounters:
    """Mutable run counters collapsed into one immutable summary value."""

    processed: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0


class CveSyncService:
    """Executes one sync run and persists run/state bookkeeping."""

    def __init__(
        self,
        db: Session,
        *,
        source_client: CveSourceClient | None = None,
        parser: CveZipParser | None = None,
        batch_size: int = 500,
        on_progress: Callable[[CveIndexSyncRun], None] | None = None,
    ) -> None:
        self._db = db
        self._source_client = source_client or CveSourceClient()
        self._parser = parser or CveZipParser()
        self._batch_size = max(1, int(batch_size))
        self._on_progress = on_progress

    def run_sync(self, *, trigger_kind: CveSyncTriggerKind = CveSyncTriggerKind.SYSTEM) -> CveIndexSyncRun:
        """Plan and execute one baseline/delta/noop CVE sync run."""
        return self.run_sync_with_lease(
            trigger_kind=trigger_kind,
            owner_id=None,
            lease_ttl_seconds=60,
        )

    def run_sync_with_lease(
        self,
        *,
        trigger_kind: CveSyncTriggerKind = CveSyncTriggerKind.SYSTEM,
        owner_id: str | None,
        lease_ttl_seconds: int,
    ) -> CveIndexSyncRun:
        """Plan and execute one sync run while preserving durable lease ownership metadata."""
        state = self._get_or_create_state()
        run = CveIndexSyncRun(
            trigger_kind=trigger_kind.value,
            sync_kind=CveSyncKind.NOOP.value,
            status="running",
            phase="resolving",
            progress_updated_at=utc_now(),
        )
        self._db.add(run)
        self._db.commit()
        self._db.refresh(run)

        now = utc_now()
        state.last_sync_status = "running"
        state.last_attempt_started_at = now
        state.last_attempt_finished_at = None
        state.last_error = None
        state.active_run_id = run.id
        state.current_phase = "resolving"
        state.progress_updated_at = now
        if owner_id:
            state.lease_owner_id = owner_id
            state.lease_heartbeat_at = now
            state.lease_expires_at = now + timedelta(seconds=max(1, int(lease_ttl_seconds)))
        self._db.add(state)
        self._db.commit()

        plan = CveSyncPlan(kind=CveSyncKind.NOOP)
        try:
            self._mark_phase(run=run, state=state, phase="resolving")
            latest_baseline = self._source_client.resolve_latest_baseline_asset()
            applied_hours = build_applied_hours_snapshot(state)
            available_delta_assets = self._source_client.resolve_missing_delta_assets(
                baseline_day=latest_baseline.baseline_day,
                applied_hours=applied_hours,
            )
            available_delta_hours = tuple(
                asset.delta_hour_utc
                for asset in available_delta_assets
                if asset.delta_hour_utc is not None
            )

            decision = plan_cve_sync(
                latest_baseline_date=latest_baseline.baseline_day,
                state=CveSyncStateSnapshot(
                    baseline_date=state.last_applied_baseline_date,
                    applied_delta_hours=applied_hours,
                    rebuild_required=bool(state.rebuild_required),
                ),
                available_delta_hours=available_delta_hours,
            )
            plan = decision.plan
            if decision.rebuild_required:
                state.rebuild_required = True
            run.sync_kind = plan.kind.value
            run.baseline_date = plan.baseline_date
            run.delta_from_hour_utc = plan.delta_hours[0] if plan.delta_hours else None
            run.delta_to_hour_utc = plan.delta_hours[-1] if plan.delta_hours else None
            self._db.add(run)
            self._db.add(state)
            self._db.commit()

            counters = self._execute_plan(
                plan=plan,
                latest_baseline=latest_baseline,
                available_delta_assets=available_delta_assets,
                run=run,
                state=state,
            )
            self._mark_run_succeeded(run=run, state=state, plan=plan, counters=counters)
        except Exception as exc:
            self._safe_rollback()
            self._mark_run_failed(
                run=run,
                state=state,
                plan=plan,
                error_message=str(exc),
            )
            raise

        return run

    def _execute_plan(
        self,
        *,
        plan: CveSyncPlan,
        latest_baseline: CveSourceAsset,
        available_delta_assets: tuple[CveSourceAsset, ...],
        run: CveIndexSyncRun,
        state: CveIndexState,
    ) -> CveSyncCounters:
        if plan.kind == CveSyncKind.NOOP:
            return CveSyncCounters()

        if plan.kind == CveSyncKind.BASELINE:
            return self._ingest_assets((latest_baseline,), run=run, state=state)

        assets_by_hour = {
            asset.delta_hour_utc: asset
            for asset in available_delta_assets
            if asset.delta_hour_utc is not None
        }
        ordered_assets: list[CveSourceAsset] = []
        for hour in plan.delta_hours:
            asset = assets_by_hour.get(hour)
            if asset is None:
                raise RuntimeError(f"Missing delta asset for planned hour {hour.isoformat()}.")
            ordered_assets.append(asset)
        return self._ingest_assets(tuple(ordered_assets), run=run, state=state)

    def _ingest_assets(
        self,
        assets: tuple[CveSourceAsset, ...],
        *,
        run: CveIndexSyncRun,
        state: CveIndexState,
    ) -> CveSyncCounters:
        processed = 0
        inserted = 0
        updated = 0
        skipped = 0
        batch: list[CveParsedRecord] = []

        self._mark_phase(run=run, state=state, phase="downloading")
        for asset in assets:
            payload = self._source_client.download_asset(asset)
            self._mark_phase(run=run, state=state, phase="upserting")
            for record in self._parser.iter_records(payload):
                batch.append(record)
                if len(batch) >= self._batch_size:
                    chunk = self._upsert_batch(
                        batch,
                        run=run,
                        state=state,
                        cumulative=CveSyncCounters(
                            processed=processed,
                            inserted=inserted,
                            updated=updated,
                            skipped=skipped,
                        ),
                    )
                    processed += chunk.processed
                    inserted += chunk.inserted
                    updated += chunk.updated
                    skipped += chunk.skipped
                    batch = []

        if batch:
            chunk = self._upsert_batch(
                batch,
                run=run,
                state=state,
                cumulative=CveSyncCounters(
                    processed=processed,
                    inserted=inserted,
                    updated=updated,
                    skipped=skipped,
                ),
            )
            processed += chunk.processed
            inserted += chunk.inserted
            updated += chunk.updated
            skipped += chunk.skipped

        return CveSyncCounters(
            processed=processed,
            inserted=inserted,
            updated=updated,
            skipped=skipped,
        )

    def _upsert_batch(
        self,
        batch: list[CveParsedRecord],
        *,
        run: CveIndexSyncRun | None = None,
        state: CveIndexState | None = None,
        cumulative: CveSyncCounters | None = None,
    ) -> CveSyncCounters:
        cve_ids = {record.cve_id for record in batch}
        existing_by_cve_id = self._load_existing_records(cve_ids)

        processed = 0
        inserted = 0
        updated = 0
        skipped = 0

        for record in batch:
            processed += 1
            existing = existing_by_cve_id.get(record.cve_id)
            if existing is None:
                created = new_cve_record(record)
                self._db.add(created)
                # Keep an in-memory mirror so duplicate CVE IDs in the same batch
                # become updates/skips instead of violating unique(cve_id).
                existing_by_cve_id[record.cve_id] = created
                inserted += 1
                continue

            if hash_record_payload(existing.cve_json) == record.content_hash:
                if self._should_refresh_projection(existing):
                    apply_projection_state(
                        target=existing,
                        cve_id=record.cve_id,
                        cve_json=record.raw_json,
                    )
                    self._db.add(existing)
                    updated += 1
                    continue
                skipped += 1
                continue

            apply_record_update(existing, record)
            self._db.add(existing)
            updated += 1

        self._db.commit()
        chunk = CveSyncCounters(
            processed=processed,
            inserted=inserted,
            updated=updated,
            skipped=skipped,
        )
        if run is not None and state is not None:
            previous = cumulative or CveSyncCounters()
            progress_at = utc_now()
            run.processed_records = previous.processed + chunk.processed
            run.inserted_records = previous.inserted + chunk.inserted
            run.updated_records = previous.updated + chunk.updated
            run.progress_updated_at = progress_at
            state.progress_updated_at = progress_at
            self._db.add(run)
            self._db.add(state)
            self._db.commit()
            if self._on_progress is not None:
                self._on_progress(run)
        return chunk

    def _load_existing_records(self, cve_ids: set[str]) -> dict[str, CveRecord]:
        if not cve_ids:
            return {}
        rows = (
            self._db.query(CveRecord)
            .filter(CveRecord.cve_id.in_(list(cve_ids)))
            .all()
        )
        return {row.cve_id: row for row in rows}

    @staticmethod
    def _should_refresh_projection(row: CveRecord) -> bool:
        status = str(getattr(row, "projection_status", "") or "").strip().lower()
        if status in {"pending", "projection_error"}:
            return True
        if getattr(row, "projection_last_projected_at", None) is None:
            return True
        return False

    def _get_or_create_state(self) -> CveIndexState:
        return get_or_create_cve_index_state(self._db, lock=False)

    def _safe_rollback(self) -> None:
        try:
            self._db.rollback()
        except SQLAlchemyError:
            return

    def _mark_run_succeeded(
        self,
        *,
        run: CveIndexSyncRun,
        state: CveIndexState,
        plan: CveSyncPlan,
        counters: CveSyncCounters,
    ) -> None:
        finished_at = utc_now()
        run.phase = "finalizing"
        run.progress_updated_at = finished_at
        run.status = "succeeded"
        run.finished_at = finished_at
        run.processed_records = counters.processed
        run.inserted_records = counters.inserted
        run.updated_records = counters.updated
        run.error_message = None
        self._db.add(run)

        state.last_sync_status = "succeeded"
        state.last_successful_sync_at = finished_at
        state.last_attempt_finished_at = finished_at
        state.last_error = None
        state.active_run_id = None
        state.current_phase = "finalizing"
        state.progress_updated_at = finished_at
        state.lease_owner_id = None
        state.lease_heartbeat_at = None
        state.lease_expires_at = None
        if plan.kind == CveSyncKind.BASELINE:
            state.last_applied_baseline_date = plan.baseline_date
            state.last_applied_delta_hour_utc = None
            state.rebuild_required = False
        elif plan.kind == CveSyncKind.DELTA and plan.delta_hours:
            state.last_applied_delta_hour_utc = plan.delta_hours[-1]
            if state.last_applied_baseline_date is None:
                state.last_applied_baseline_date = plan.delta_hours[-1].date()
        self._db.add(state)
        self._db.commit()

    def _mark_run_failed(
        self,
        *,
        run: CveIndexSyncRun,
        state: CveIndexState,
        plan: CveSyncPlan,
        error_message: str,
    ) -> None:
        finished_at = utc_now()
        run.phase = "finalizing"
        run.progress_updated_at = finished_at
        run.status = "failed"
        run.finished_at = finished_at
        run.error_message = error_message
        self._db.add(run)

        state.last_sync_status = "failed"
        state.last_attempt_finished_at = finished_at
        state.last_error = error_message
        state.active_run_id = None
        state.current_phase = "finalizing"
        state.progress_updated_at = finished_at
        state.lease_owner_id = None
        state.lease_heartbeat_at = None
        state.lease_expires_at = None
        if plan.kind == CveSyncKind.DELTA:
            state.rebuild_required = True
        self._db.add(state)
        self._db.commit()

    def _mark_phase(
        self,
        *,
        run: CveIndexSyncRun,
        state: CveIndexState,
        phase: str,
    ) -> None:
        progress_at = utc_now()
        run.phase = phase
        run.progress_updated_at = progress_at
        state.current_phase = phase
        state.progress_updated_at = progress_at
        self._db.add(run)
        self._db.add(state)
        self._db.commit()

__all__ = [
    "CveSyncCounters",
    "CveSyncService",
    "CveSyncPlannerDecision",
    "CveSyncStateSnapshot",
    "plan_cve_sync",
]
