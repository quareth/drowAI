"""Tenant-bound runner registry service for Runner Control management APIs.

Scope:
- Implements execution-site management, install-token issuance, runner reads,
  runner credential revocation, and stale presence reconciliation under tenant
  isolation constraints.

Boundaries:
- Service-layer orchestration only; no FastAPI router wiring and no protocol
  channel handling.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import os
import re
from typing import Any
from uuid import UUID

from sqlalchemy import delete, exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models.runner_control import (
    ExecutionSite,
    Runner,
    RunnerConnection,
    RunnerControlMessage,
    RunnerCredential,
    RunnerInstallToken,
    RuntimeJob,
)
from backend.services.runner_control.audit import RunnerControlAuditEmitter, RunnerControlAuditService
from backend.services.runner_control.coordination import LeaseExpiryResult, RunnerCoordinationStore
from backend.services.runner_control.credentials import (
    IssuedInstallToken,
    RunnerCredentialService,
)
from backend.services.runner_control.db_coordination import DBRunnerCoordinationStore

_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_LEASE_RECONCILABLE_RUNTIME_JOB_STATUSES = ("queued", "assigned", "dispatching", "dispatched")
_RUNTIME_JOB_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "lost", "expired"})
_RUNNER_HEARTBEAT_FRESHNESS = timedelta(seconds=120)
_RUNNER_ONLINE_STATUSES = frozenset({"active", "online"})


class RunnerRegistryError(RuntimeError):
    """Raised when runner registry operations fail with a stable error code."""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class RunnerPresenceReconciliationResult:
    """Summary for stale runner lease and runtime-job reconciliation pass."""

    lease_expiry: LeaseExpiryResult
    lost_runtime_job_count: int
    expired_runtime_job_count: int
    runtime_job_transition_tenants: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class RunnerSiteConnectivitySummary:
    """Management-observed connectivity summary for one Runner Site."""

    connectivity_status: str
    runner_count: int
    connected_runner_count: int
    last_seen_at: datetime | None


class RunnerRegistryService:
    """Manage tenant-bound runner registry records and credential lifecycle."""

    def __init__(
        self,
        db: Session,
        *,
        credential_service: RunnerCredentialService | None = None,
        coordination_store: RunnerCoordinationStore | None = None,
        audit_emitter: RunnerControlAuditEmitter | None = None,
    ) -> None:
        self._db = db
        self._credential_service = credential_service or RunnerCredentialService(db)
        self._audit = RunnerControlAuditService(emitter=audit_emitter)
        pod_id = str(os.getenv("HOSTNAME") or "local-pod").strip() or "local-pod"
        self._coordination = coordination_store or DBRunnerCoordinationStore(db, pod_id=pod_id)

    def create_execution_site(
        self,
        *,
        tenant_id: int,
        name: str,
        slug: str,
        network_label: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> ExecutionSite:
        """Create a tenant-bound execution site with normalized fields."""

        normalized_name = _normalize_required_text(name=name, field_name="name", max_length=255)
        normalized_slug = _normalize_slug(slug=slug)
        normalized_network = _normalize_optional_text(network_label, max_length=255)
        normalized_labels = _normalize_labels(labels)

        try:
            with self._transaction_context():
                site = ExecutionSite(
                    tenant_id=tenant_id,
                    name=normalized_name,
                    slug=normalized_slug,
                    network_label=normalized_network,
                    status="active",
                    labels_json=normalized_labels,
                )
                self._db.add(site)
                self._db.flush()
                return site
        except IntegrityError as exc:
            raise RunnerRegistryError(
                error_code="EXECUTION_SITE_CONFLICT",
                message="Execution site name or slug already exists for tenant.",
            ) from exc

    def list_execution_sites(self, *, tenant_id: int) -> Sequence[ExecutionSite]:
        """Return tenant-filtered execution sites."""

        return list(
            self._db.execute(
                select(ExecutionSite)
                .where(ExecutionSite.tenant_id == tenant_id)
                .order_by(ExecutionSite.created_at.desc())
            ).scalars()
        )

    def list_runner_site_connectivity(
        self,
        *,
        tenant_id: int,
        now: datetime | None = None,
    ) -> dict[UUID, RunnerSiteConnectivitySummary]:
        """Return Runner Site connectivity derived from non-expired connection leases."""

        observed_at = _ensure_utc(now or datetime.now(tz=UTC))
        sites = self.list_execution_sites(tenant_id=tenant_id)
        runners = list(
            self._db.execute(select(Runner).where(Runner.tenant_id == tenant_id)).scalars()
        )
        connections = list(
            self._db.execute(
                select(RunnerConnection).where(RunnerConnection.tenant_id == tenant_id)
            ).scalars()
        )
        active_connections = [
            connection
            for connection in connections
            if str(connection.status or "").strip().lower() == "active"
            and _ensure_utc(connection.lease_expires_at) > observed_at
        ]
        connected_runner_ids = {connection.runner_id for connection in active_connections}
        connection_last_seen_by_runner: dict[UUID, datetime] = {}
        for connection in connections:
            current = connection_last_seen_by_runner.get(connection.runner_id)
            last_seen = _ensure_utc(connection.last_seen_at)
            if current is None or last_seen > current:
                connection_last_seen_by_runner[connection.runner_id] = last_seen

        summaries: dict[UUID, RunnerSiteConnectivitySummary] = {}
        for site in sites:
            site_runners = [runner for runner in runners if runner.execution_site_id == site.id]
            connected_count = sum(1 for runner in site_runners if runner.id in connected_runner_ids)
            last_seen_candidates: list[datetime] = []
            for runner in site_runners:
                if runner.last_seen_at is not None:
                    last_seen_candidates.append(_ensure_utc(runner.last_seen_at))
                connection_last_seen = connection_last_seen_by_runner.get(runner.id)
                if connection_last_seen is not None:
                    last_seen_candidates.append(connection_last_seen)
            if connected_count > 0:
                connectivity_status = "connected"
            elif site_runners:
                connectivity_status = "offline"
            else:
                connectivity_status = "waiting"
            summaries[site.id] = RunnerSiteConnectivitySummary(
                connectivity_status=connectivity_status,
                runner_count=len(site_runners),
                connected_runner_count=connected_count,
                last_seen_at=max(last_seen_candidates) if last_seen_candidates else None,
            )
        return summaries

    def has_connected_runner_site(self, *, now: datetime | None = None) -> bool:
        """Return whether any Runner Site has a non-expired active connection lease."""

        observed_at = _ensure_utc(now or datetime.now(tz=UTC))
        tenant_ids = self._db.execute(select(ExecutionSite.tenant_id).distinct()).scalars()
        for tenant_id in tenant_ids:
            summaries = self.list_runner_site_connectivity(
                tenant_id=int(tenant_id),
                now=observed_at,
            )
            if any(summary.connectivity_status == "connected" for summary in summaries.values()):
                return True
        return False

    def delete_runner_site(
        self,
        *,
        tenant_id: int,
        execution_site_id: UUID,
        actor_user_id: int | None = None,
        now: datetime | None = None,
    ) -> None:
        """Guard and hard-delete one tenant-bound Runner Site atomically."""

        observed_at = _ensure_utc(now or datetime.now(tz=UTC))
        with self._transaction_context():
            locked_sites = list(
                self._db.execute(
                    select(ExecutionSite)
                    .where(ExecutionSite.tenant_id == tenant_id)
                    .order_by(ExecutionSite.id.asc())
                    .with_for_update()
                ).scalars()
            )
            site = next((candidate for candidate in locked_sites if candidate.id == execution_site_id), None)
            if site is None:
                raise RunnerRegistryError(
                    error_code="RUNNER_SITE_NOT_FOUND",
                    message="Runner Site not found.",
                )

            runners = list(
                self._db.execute(
                    select(Runner)
                    .where(
                        Runner.tenant_id == tenant_id,
                        Runner.execution_site_id == execution_site_id,
                    )
                    .order_by(Runner.id.asc())
                    .with_for_update()
                ).scalars()
            )
            runner_ids = tuple(runner.id for runner in runners)
            connected_runner_ids = self._connected_runner_ids(
                tenant_id=tenant_id,
                runner_ids=runner_ids,
                now=observed_at,
            )
            active_execution_count = self._active_execution_count(
                tenant_id=tenant_id,
                runners=runners,
                connected_runner_ids=connected_runner_ids,
            )
            if active_execution_count > 0:
                raise RunnerRegistryError(
                    error_code="RUNNER_SITE_ACTIVE_EXECUTIONS",
                    message="Runner Site has active executions.",
                    details={"execution_count": active_execution_count},
                )
            if not self._has_connected_replacement_runner(
                tenant_id=tenant_id,
                excluded_execution_site_id=execution_site_id,
                now=observed_at,
            ):
                raise RunnerRegistryError(
                    error_code="RUNNER_SITE_LAST_CONNECTED",
                    message="Connect another Runner Site before removing this one.",
                )

            revoked_tokens = self._db.execute(
                update(RunnerInstallToken)
                .where(
                    RunnerInstallToken.tenant_id == tenant_id,
                    RunnerInstallToken.execution_site_id == execution_site_id,
                    RunnerInstallToken.status == "issued",
                    RunnerInstallToken.used_at.is_(None),
                )
                .values(status="revoked")
            )
            revoked_install_token_count = int(revoked_tokens.rowcount or 0)
            revoked_credentials = self._db.execute(
                update(RunnerCredential)
                .where(
                    RunnerCredential.tenant_id == tenant_id,
                    RunnerCredential.runner_id.in_(runner_ids),
                    RunnerCredential.status != "revoked",
                )
                .values(status="revoked", revoked_at=observed_at)
            ) if runner_ids else None
            revoked_credential_count = int(revoked_credentials.rowcount or 0) if revoked_credentials is not None else 0

            for runner in runners:
                active_connection_ids = tuple(
                    str(connection_id)
                    for connection_id in self._db.execute(
                        select(RunnerConnection.connection_id).where(
                            RunnerConnection.tenant_id == tenant_id,
                            RunnerConnection.runner_id == runner.id,
                            RunnerConnection.status == "active",
                        )
                    ).scalars()
                )
                for connection_id in active_connection_ids:
                    self._coordination.release_connection_lease(
                        tenant_id=tenant_id,
                        runner_id=runner.id,
                        connection_id=connection_id,
                        released_at=observed_at,
                    )

            runtime_job_ids = tuple(
                self._db.execute(
                    select(RuntimeJob.id).where(
                        RuntimeJob.tenant_id == tenant_id,
                        or_(
                            RuntimeJob.execution_site_id == execution_site_id,
                            RuntimeJob.runner_id.in_(runner_ids),
                        ),
                    )
                ).scalars()
            ) if runner_ids else tuple(
                self._db.execute(
                    select(RuntimeJob.id).where(
                        RuntimeJob.tenant_id == tenant_id,
                        RuntimeJob.execution_site_id == execution_site_id,
                    )
                ).scalars()
            )
            message_predicates = []
            if runner_ids:
                message_predicates.append(RunnerControlMessage.runner_id.in_(runner_ids))
            if runtime_job_ids:
                message_predicates.append(RunnerControlMessage.runtime_job_id.in_(runtime_job_ids))
            if message_predicates:
                self._db.execute(
                    delete(RunnerControlMessage).where(
                        RunnerControlMessage.tenant_id == tenant_id,
                        or_(*message_predicates),
                    )
                )
            if runtime_job_ids:
                self._db.execute(
                    delete(RuntimeJob).where(
                        RuntimeJob.tenant_id == tenant_id,
                        RuntimeJob.id.in_(runtime_job_ids),
                    )
                )
            if runner_ids:
                self._db.execute(
                    delete(RunnerConnection).where(
                        RunnerConnection.tenant_id == tenant_id,
                        RunnerConnection.runner_id.in_(runner_ids),
                    )
                )
                self._db.execute(
                    delete(RunnerCredential).where(
                        RunnerCredential.tenant_id == tenant_id,
                        RunnerCredential.runner_id.in_(runner_ids),
                    )
                )
            self._db.execute(
                delete(RunnerInstallToken).where(
                    RunnerInstallToken.tenant_id == tenant_id,
                    RunnerInstallToken.execution_site_id == execution_site_id,
                )
            )
            if runner_ids:
                self._db.execute(
                    delete(Runner).where(
                        Runner.tenant_id == tenant_id,
                        Runner.id.in_(runner_ids),
                    )
                )
            self._db.execute(
                delete(ExecutionSite).where(
                    ExecutionSite.tenant_id == tenant_id,
                    ExecutionSite.id == execution_site_id,
                )
            )
            self._audit.emit(
                event_type="runner_site.deleted",
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                metadata={
                    "execution_site_id": str(execution_site_id),
                    "revoked_install_token_count": revoked_install_token_count,
                    "revoked_credential_count": revoked_credential_count,
                    "deleted_runner_count": len(runner_ids),
                    "deleted_runtime_job_count": len(runtime_job_ids),
                },
            )
            self._db.flush()

    def _connected_runner_ids(
        self,
        *,
        tenant_id: int,
        runner_ids: tuple[UUID, ...],
        now: datetime,
    ) -> set[UUID]:
        if not runner_ids:
            return set()
        return set(
            self._db.execute(
                select(RunnerConnection.runner_id).where(
                    RunnerConnection.tenant_id == tenant_id,
                    RunnerConnection.runner_id.in_(runner_ids),
                    RunnerConnection.status == "active",
                    RunnerConnection.lease_expires_at > now,
                )
            ).scalars()
        )

    def _active_execution_count(
        self,
        *,
        tenant_id: int,
        runners: Sequence[Runner],
        connected_runner_ids: set[UUID],
    ) -> int:
        if not connected_runner_ids:
            return 0
        active_jobs_by_runner: dict[UUID, set[str]] = {runner_id: set() for runner_id in connected_runner_ids}
        jobs = self._db.execute(
            select(RuntimeJob.id, RuntimeJob.runner_id, RuntimeJob.status).where(
                RuntimeJob.tenant_id == tenant_id,
                RuntimeJob.runner_id.in_(connected_runner_ids),
            )
        )
        for runtime_job_id, runner_id, job_status in jobs:
            if runner_id is None or str(job_status or "").strip().lower() in _RUNTIME_JOB_TERMINAL_STATUSES:
                continue
            active_jobs_by_runner[runner_id].add(str(runtime_job_id))

        active_execution_count = 0
        for runner in runners:
            if runner.id not in connected_runner_ids:
                continue
            capacity = runner.capacity_json if isinstance(runner.capacity_json, dict) else {}
            reported_jobs = capacity.get("active_runtime_jobs")
            reported_job_count = len(reported_jobs) if isinstance(reported_jobs, list) else 0
            if isinstance(reported_jobs, list):
                for reported_job in reported_jobs:
                    if not isinstance(reported_job, dict):
                        continue
                    runtime_job_id = str(reported_job.get("runtime_job_id") or "").strip()
                    if runtime_job_id:
                        active_jobs_by_runner[runner.id].add(runtime_job_id)
            try:
                reported_active_tasks = max(0, int(capacity.get("active_tasks") or 0))
            except (TypeError, ValueError):
                reported_active_tasks = 0
            active_execution_count += max(
                len(active_jobs_by_runner[runner.id]),
                reported_job_count,
                reported_active_tasks,
            )
        return active_execution_count

    def _has_connected_replacement_runner(
        self,
        *,
        tenant_id: int,
        excluded_execution_site_id: UUID,
        now: datetime,
    ) -> bool:
        replacement_runners = list(
            self._db.execute(
                select(Runner)
                .join(ExecutionSite, ExecutionSite.id == Runner.execution_site_id)
                .where(
                    Runner.tenant_id == tenant_id,
                    Runner.execution_site_id != excluded_execution_site_id,
                    Runner.status.in_(tuple(_RUNNER_ONLINE_STATUSES)),
                    Runner.last_seen_at.is_not(None),
                    Runner.last_seen_at >= now - _RUNNER_HEARTBEAT_FRESHNESS,
                    ExecutionSite.status == "active",
                )
                .with_for_update()
            ).scalars()
        )
        if not replacement_runners:
            return False
        replacement_ids = tuple(runner.id for runner in replacement_runners)
        connected_ids = self._connected_runner_ids(
            tenant_id=tenant_id,
            runner_ids=replacement_ids,
            now=now,
        )
        if not connected_ids:
            return False
        credential_runner_ids = set(
            self._db.execute(
                select(RunnerCredential.runner_id).where(
                    RunnerCredential.tenant_id == tenant_id,
                    RunnerCredential.runner_id.in_(connected_ids),
                    RunnerCredential.status == "active",
                    RunnerCredential.revoked_at.is_(None),
                    or_(
                        RunnerCredential.expires_at.is_(None),
                        RunnerCredential.expires_at > now,
                    ),
                )
            ).scalars()
        )
        return bool(connected_ids & credential_runner_ids)

    def issue_install_token(
        self,
        *,
        tenant_id: int,
        execution_site_id: UUID,
        created_by_user_id: int,
        ttl_seconds: int | None = None,
    ) -> IssuedInstallToken:
        """Issue one-time install token for an execution site in the same tenant."""

        execution_site = self._db.execute(
            select(ExecutionSite).where(
                ExecutionSite.id == execution_site_id,
                ExecutionSite.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if execution_site is None:
            raise RunnerRegistryError(
                error_code="EXECUTION_SITE_NOT_FOUND",
                message="Execution site not found.",
            )

        ttl = timedelta(seconds=ttl_seconds) if ttl_seconds is not None else None
        with self._transaction_context():
            issued = self._credential_service.issue_install_token(
                tenant_id=tenant_id,
                execution_site_id=execution_site_id,
                created_by_user_id=created_by_user_id,
                ttl=ttl,
            )
            self._audit.emit(
                event_type="runner.install_token_created",
                tenant_id=tenant_id,
                metadata={
                    "execution_site_id": str(execution_site_id),
                    "install_token_id": str(issued.install_token_id),
                    "created_by_user_id": int(created_by_user_id),
                    "ttl_seconds": int(ttl.total_seconds()) if ttl is not None else None,
                },
            )
            return issued

    def list_runners(self, *, tenant_id: int) -> Sequence[Runner]:
        """Return tenant-filtered runner list."""

        return list(
            self._db.execute(
                select(Runner).where(Runner.tenant_id == tenant_id).order_by(Runner.created_at.desc())
            ).scalars()
        )

    def get_runner(self, *, tenant_id: int, runner_id: UUID) -> Runner:
        """Return one tenant-scoped runner or raise not-found."""

        runner = self._db.execute(
            select(Runner).where(
                Runner.id == runner_id,
                Runner.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if runner is None:
            raise RunnerRegistryError(
                error_code="RUNNER_NOT_FOUND",
                message="Runner not found.",
            )
        return runner

    def list_runner_credentials(self, *, tenant_id: int, runner_id: UUID) -> Sequence[RunnerCredential]:
        """Return credentials for one tenant-bound runner."""

        return list(
            self._db.execute(
                select(RunnerCredential)
                .where(
                    RunnerCredential.tenant_id == tenant_id,
                    RunnerCredential.runner_id == runner_id,
                )
                .order_by(RunnerCredential.created_at.desc())
            ).scalars()
        )

    def revoke_runner_credentials(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        actor_user_id: int | None = None,
    ) -> int:
        """Revoke all non-revoked credentials for a tenant-bound runner."""

        runner = self.get_runner(tenant_id=tenant_id, runner_id=runner_id)
        revoked_count = 0
        revoked_at = datetime.now(tz=UTC)
        with self._transaction_context():
            credentials = self.list_runner_credentials(tenant_id=tenant_id, runner_id=runner_id)
            for credential in credentials:
                if credential.revoked_at is not None or str(credential.status or "").lower() == "revoked":
                    continue
                self._credential_service.revoke_runner_credential(credential)
                revoked_count += 1
                self._audit.emit(
                    event_type="runner.credential_revoked",
                    tenant_id=tenant_id,
                    runner_id=runner.id,
                    actor_user_id=actor_user_id,
                    metadata={
                        "credential_id": str(credential.id),
                        "credential_fingerprint": str(credential.credential_fingerprint or "").strip() or None,
                    },
                )
            if revoked_count > 0:
                active_connection_ids = tuple(
                    str(connection_id)
                    for connection_id in self._db.execute(
                        select(RunnerConnection.connection_id).where(
                            RunnerConnection.tenant_id == tenant_id,
                            RunnerConnection.runner_id == runner_id,
                            RunnerConnection.status == "active",
                        )
                    ).scalars()
                )
                for connection_id in active_connection_ids:
                    self._coordination.release_connection_lease(
                        tenant_id=tenant_id,
                        runner_id=runner_id,
                        connection_id=connection_id,
                        released_at=revoked_at,
                    )
                runner.status = "revoked"
            self._db.flush()
        return revoked_count

    def reconcile_stale_presence(self, *, now: datetime | None = None) -> RunnerPresenceReconciliationResult:
        """Expire stale leases and advance expired runtime jobs deterministically."""

        reconciliation_time = _ensure_utc(now or datetime.now(tz=UTC))
        with self._transaction_context():
            lease_expiry = self._coordination.expire_stale_leases(now=reconciliation_time)
            expired_runtime_job_count, expired_runtime_job_tenants = self._expire_runtime_jobs_without_runner(
                now=reconciliation_time
            )
            lost_runtime_job_count, lost_runtime_job_tenants = self._expire_runtime_jobs_without_active_runner_lease(
                now=reconciliation_time
            )
            self._emit_offline_audits(lease_expiry=lease_expiry)
            self._db.flush()

        runtime_job_transition_tenants = tuple(sorted(expired_runtime_job_tenants | lost_runtime_job_tenants))
        return RunnerPresenceReconciliationResult(
            lease_expiry=lease_expiry,
            lost_runtime_job_count=lost_runtime_job_count,
            expired_runtime_job_count=expired_runtime_job_count,
            runtime_job_transition_tenants=runtime_job_transition_tenants,
        )

    def _emit_offline_audits(self, *, lease_expiry: LeaseExpiryResult) -> None:
        for transition in lease_expiry.offline_transitions:
            self._audit.emit(
                event_type="runner.offline",
                tenant_id=transition.tenant_id,
                runner_id=transition.runner_id,
                metadata={
                    "reason": transition.reason,
                    "last_seen_at": transition.last_seen_at.isoformat(),
                },
            )

    def _transaction_context(self) -> AbstractContextManager[object]:
        if self._db.in_transaction():
            return self._db.begin_nested()
        return self._db.begin()

    def _expire_runtime_jobs_without_runner(self, *, now: datetime) -> tuple[int, set[int]]:
        touched_tenant_ids = {
            int(tenant_id)
            for tenant_id in self._db.execute(
                select(RuntimeJob.tenant_id)
                .where(
                    RuntimeJob.runner_id.is_(None),
                    RuntimeJob.status.in_(_LEASE_RECONCILABLE_RUNTIME_JOB_STATUSES),
                    RuntimeJob.lease_expires_at.is_not(None),
                    RuntimeJob.lease_expires_at <= now,
                )
                .distinct()
            ).scalars()
        }
        result = self._db.execute(
            update(RuntimeJob)
            .where(
                RuntimeJob.runner_id.is_(None),
                RuntimeJob.status.in_(_LEASE_RECONCILABLE_RUNTIME_JOB_STATUSES),
                RuntimeJob.lease_expires_at.is_not(None),
                RuntimeJob.lease_expires_at <= now,
            )
            .values(
                status="expired",
                error_code="RUNTIME_JOB_LEASE_EXPIRED",
                error_message="Runtime job lease expired before runner ownership was available.",
            )
        )
        updated_count = int(result.rowcount or 0)
        if updated_count <= 0:
            return 0, set()
        return updated_count, touched_tenant_ids

    def _expire_runtime_jobs_without_active_runner_lease(self, *, now: datetime) -> tuple[int, set[int]]:
        active_lease_exists = exists(
            select(RunnerConnection.id).where(
                RunnerConnection.tenant_id == RuntimeJob.tenant_id,
                RunnerConnection.runner_id == RuntimeJob.runner_id,
                RunnerConnection.status == "active",
                RunnerConnection.lease_expires_at > now,
            )
        )
        touched_tenant_ids = {
            int(tenant_id)
            for tenant_id in self._db.execute(
                select(RuntimeJob.tenant_id)
                .where(
                    RuntimeJob.runner_id.is_not(None),
                    RuntimeJob.status.in_(_LEASE_RECONCILABLE_RUNTIME_JOB_STATUSES),
                    RuntimeJob.lease_expires_at.is_not(None),
                    RuntimeJob.lease_expires_at <= now,
                    ~active_lease_exists,
                )
                .distinct()
            ).scalars()
        }
        result = self._db.execute(
            update(RuntimeJob)
            .where(
                RuntimeJob.runner_id.is_not(None),
                RuntimeJob.status.in_(_LEASE_RECONCILABLE_RUNTIME_JOB_STATUSES),
                RuntimeJob.lease_expires_at.is_not(None),
                RuntimeJob.lease_expires_at <= now,
                ~active_lease_exists,
            )
            .values(
                status="lost",
                error_code="RUNNER_LEASE_EXPIRED",
                error_message="Runner presence lease expired before runtime job completion.",
            )
        )
        updated_count = int(result.rowcount or 0)
        if updated_count <= 0:
            return 0, set()
        return updated_count, touched_tenant_ids


def _normalize_required_text(*, name: str, field_name: str, max_length: int) -> str:
    value = str(name or "").strip()
    if not value:
        raise RunnerRegistryError(
            error_code="RUNNER_VALIDATION_ERROR",
            message=f"{field_name} is required.",
        )
    if len(value) > max_length:
        raise RunnerRegistryError(
            error_code="RUNNER_VALIDATION_ERROR",
            message=f"{field_name} exceeds max length {max_length}.",
        )
    return value


def _normalize_optional_text(value: str | None, *, max_length: int) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) > max_length:
        raise RunnerRegistryError(
            error_code="RUNNER_VALIDATION_ERROR",
            message=f"value exceeds max length {max_length}.",
        )
    return text


def _normalize_slug(*, slug: str) -> str:
    value = str(slug or "").strip().lower()
    if not value:
        raise RunnerRegistryError(
            error_code="RUNNER_VALIDATION_ERROR",
            message="slug is required.",
        )
    if len(value) > 128:
        raise RunnerRegistryError(
            error_code="RUNNER_VALIDATION_ERROR",
            message="slug exceeds max length 128.",
        )
    if not _SLUG_PATTERN.match(value):
        raise RunnerRegistryError(
            error_code="RUNNER_VALIDATION_ERROR",
            message="slug must contain lowercase letters, digits, and internal dashes only.",
        )
    return value


def _normalize_labels(labels: dict[str, str] | None) -> dict[str, str] | None:
    if labels is None:
        return None
    normalized: dict[str, str] = {}
    for key, value in labels.items():
        normalized_key = str(key or "").strip().lower()
        normalized_value = str(value or "").strip()
        if not normalized_key:
            raise RunnerRegistryError(
                error_code="RUNNER_VALIDATION_ERROR",
                message="labels keys must be non-empty.",
            )
        if len(normalized_key) > 64 or len(normalized_value) > 128:
            raise RunnerRegistryError(
                error_code="RUNNER_VALIDATION_ERROR",
                message="labels exceed max key/value length.",
            )
        normalized[normalized_key] = normalized_value
    return normalized


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
