"""Tests for tenant effective retention policy resolution.

These tests cover tenant data-management settings folded into immutable
retention policies without reading cleanup candidate tables.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as backend_models
from backend.config.retention import (
    DEFAULT_REPORT_RETENTION_ENABLED,
    MAX_RETENTION_BATCH_SIZE_PER_TENANT,
    MAX_RETENTION_DAYS,
    MIN_RETENTION_BATCH_SIZE_PER_TENANT,
    MIN_RETENTION_DAYS,
    RETENTION_POLICY_DEFAULTS,
)
from backend.database import Base
from backend.models.data_management import TenantDataManagementSettings
from backend.models.tenant import Tenant
from backend.schemas.data_management import TenantDataManagementSettingsUpdateRequest
from backend.services.data_management_settings_service import (
    DataManagementSettingsService,
)
from backend.services.retention.policies import (
    EffectiveRetentionPolicy,
    RetentionPolicyValidationError,
    resolve_effective_retention_policy,
    resolve_effective_retention_policy_for_tenant,
)


def _build_session_factory() -> sessionmaker[Session]:
    assert backend_models.__all__
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


def _seed_tenant(db: Session) -> int:
    tenant = Tenant(slug=f"tenant-{uuid4().hex[:8]}", name="Tenant")
    db.add(tenant)
    db.commit()
    return int(tenant.id)


class _RecordingQuery:
    def filter(self, *_conditions: object) -> "_RecordingQuery":
        return self

    def one_or_none(self) -> None:
        return None


class _RecordingSession:
    def __init__(self) -> None:
        self.queried_models: list[object] = []

    def query(self, model: object) -> _RecordingQuery:
        self.queried_models.append(model)
        return _RecordingQuery()


def test_defaults_resolve_deterministically_for_tenant_without_settings_row() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant_id = _seed_tenant(db)

        policy = resolve_effective_retention_policy_for_tenant(db, tenant_id=tenant_id)

        assert isinstance(policy, EffectiveRetentionPolicy)
        assert policy.tenant_id == tenant_id
        assert policy.report_retention_enabled is DEFAULT_REPORT_RETENTION_ENABLED
        assert db.query(TenantDataManagementSettings).count() == 0
        for field_name, default_value in RETENTION_POLICY_DEFAULTS.items():
            assert getattr(policy, field_name) == default_value


def test_db_resolver_only_queries_tenant_settings_table() -> None:
    db = _RecordingSession()

    policy = resolve_effective_retention_policy_for_tenant(
        cast(Session, db),
        tenant_id=1,
    )

    assert policy.tenant_id == 1
    assert db.queried_models == [TenantDataManagementSettings]


def test_policy_object_is_immutable() -> None:
    policy = resolve_effective_retention_policy(tenant_id=1)

    with pytest.raises(FrozenInstanceError):
        policy.report_history_retention_days = 1  # type: ignore[misc]


def test_partial_policy_override_resolves_unset_fields_from_named_defaults() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant_id = _seed_tenant(db)
        service = DataManagementSettingsService(db)

        service.update_settings(
            tenant_id=tenant_id,
            payload=TenantDataManagementSettingsUpdateRequest.model_validate(
                {
                    "report_history_retention_days": 45,
                    "operational_log_retention_days": 14,
                }
            ),
        )

        policy = resolve_effective_retention_policy_for_tenant(db, tenant_id=tenant_id)

        assert policy.report_history_retention_days == 45
        assert policy.operational_log_retention_days == 14
        assert (
            policy.checkpoint_retention_days_after_terminal
            == RETENTION_POLICY_DEFAULTS["checkpoint_retention_days_after_terminal"]
        )
        assert (
            policy.retention_batch_size_per_tenant
            == RETENTION_POLICY_DEFAULTS["retention_batch_size_per_tenant"]
        )


def test_explicit_settings_row_folds_report_history_into_effective_policy() -> None:
    settings = TenantDataManagementSettings(
        tenant_id=7,
        report_retention_enabled=False,
        report_history_retention_days=77,
        retention_batch_size_per_tenant=17,
    )

    policy = resolve_effective_retention_policy(tenant_id=7, settings=settings)

    assert policy.tenant_id == 7
    assert policy.report_retention_enabled is False
    assert policy.report_history_retention_days == 77
    assert policy.retention_batch_size_per_tenant == 17
    assert (
        policy.artifact_payload_retention_days
        == RETENTION_POLICY_DEFAULTS["artifact_payload_retention_days"]
    )


def test_policy_resolution_accepts_named_minimum_and_maximum_bounds() -> None:
    minimum_policy = resolve_effective_retention_policy(
        tenant_id=1,
        settings=TenantDataManagementSettings(
            tenant_id=1,
            report_history_retention_days=MIN_RETENTION_DAYS,
            retention_batch_size_per_tenant=MIN_RETENTION_BATCH_SIZE_PER_TENANT,
        ),
    )
    maximum_policy = resolve_effective_retention_policy(
        tenant_id=1,
        settings=TenantDataManagementSettings(
            tenant_id=1,
            report_history_retention_days=MAX_RETENTION_DAYS,
            retention_batch_size_per_tenant=MAX_RETENTION_BATCH_SIZE_PER_TENANT,
        ),
    )

    assert minimum_policy.report_history_retention_days == MIN_RETENTION_DAYS
    assert (
        minimum_policy.retention_batch_size_per_tenant
        == MIN_RETENTION_BATCH_SIZE_PER_TENANT
    )
    assert maximum_policy.report_history_retention_days == MAX_RETENTION_DAYS
    assert (
        maximum_policy.retention_batch_size_per_tenant
        == MAX_RETENTION_BATCH_SIZE_PER_TENANT
    )


def test_policy_resolution_rejects_out_of_bounds_values() -> None:
    valid_policy = resolve_effective_retention_policy(tenant_id=1)

    with pytest.raises(RetentionPolicyValidationError, match="must be between"):
        replace(valid_policy, report_history_retention_days=MIN_RETENTION_DAYS - 1)

    with pytest.raises(RetentionPolicyValidationError, match="must be between"):
        replace(
            valid_policy,
            retention_batch_size_per_tenant=MAX_RETENTION_BATCH_SIZE_PER_TENANT + 1,
        )
