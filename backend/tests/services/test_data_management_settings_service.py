"""Service tests for tenant data management settings persistence.

These tests verify default resolution and update validation at the service
boundary, below the API schema layer.
"""

from __future__ import annotations

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
    RETENTION_POLICY_DEFAULTS,
)
from backend.database import Base
from backend.models.data_management import TenantDataManagementSettings
from backend.models.tenant import Tenant
from backend.schemas.data_management import TenantDataManagementSettingsUpdateRequest
from backend.services.data_management_settings_service import (
    DataManagementSettingsService,
    DataManagementSettingsValidationError,
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


def test_new_tenants_receive_all_default_retention_settings() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant_id = _seed_tenant(db)
        settings = DataManagementSettingsService(db).get_settings(tenant_id=tenant_id)

        assert settings.report_retention_enabled is DEFAULT_REPORT_RETENTION_ENABLED
        for field_name, default_value in RETENTION_POLICY_DEFAULTS.items():
            assert getattr(settings, field_name) == default_value


def test_partial_updates_preserve_unspecified_retention_settings() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant_id = _seed_tenant(db)
        service = DataManagementSettingsService(db)

        service.update_settings(
            tenant_id=tenant_id,
            payload=TenantDataManagementSettingsUpdateRequest.model_validate(
                {
                    "operational_log_retention_days": 45,
                    "report_history_retention_days": 120,
                    "retention_batch_size_per_tenant": 17,
                }
            ),
        )

        response = service.update_settings(
            tenant_id=tenant_id,
            payload=TenantDataManagementSettingsUpdateRequest.model_validate(
                {
                    "report_retention_enabled": False,
                    "task_retention_days_after_terminal": 365,
                }
            ),
        )

        assert response.report_retention_enabled is False
        assert response.task_retention_days_after_terminal == 365
        assert response.operational_log_retention_days == 45
        assert response.report_history_retention_days == 120
        assert response.retention_batch_size_per_tenant == 17


def test_existing_report_retention_flag_is_preserved() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant_id = _seed_tenant(db)
        db.add(
            TenantDataManagementSettings(
                tenant_id=tenant_id,
                report_retention_enabled=False,
                **RETENTION_POLICY_DEFAULTS,
            )
        )
        db.commit()

        settings = DataManagementSettingsService(db).get_settings(tenant_id=tenant_id)

        assert settings.report_retention_enabled is False


@pytest.mark.parametrize(
    "payload",
    [
        TenantDataManagementSettingsUpdateRequest.model_construct(
            operational_log_retention_days=MAX_RETENTION_DAYS + 1
        ),
        TenantDataManagementSettingsUpdateRequest.model_construct(
            retention_batch_size_per_tenant=MAX_RETENTION_BATCH_SIZE_PER_TENANT + 1
        ),
        TenantDataManagementSettingsUpdateRequest.model_construct(
            report_history_retention_days=None
        ),
        TenantDataManagementSettingsUpdateRequest.model_construct(
            task_retention_days_after_terminal=True
        ),
        TenantDataManagementSettingsUpdateRequest.model_construct(
            report_retention_enabled="disabled"
        ),
    ],
)
def test_invalid_service_update_values_raise_validation_error(
    payload: TenantDataManagementSettingsUpdateRequest,
) -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant_id = _seed_tenant(db)
        service = DataManagementSettingsService(db)

        with pytest.raises(DataManagementSettingsValidationError):
            service.update_settings(tenant_id=tenant_id, payload=payload)
