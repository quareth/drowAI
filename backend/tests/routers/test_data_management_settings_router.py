"""Router tests for tenant data management settings endpoints."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as backend_models
from backend.config.retention import (
    MAX_RETENTION_BATCH_SIZE_PER_TENANT,
    MAX_RETENTION_DAYS,
    MIN_RETENTION_BATCH_SIZE_PER_TENANT,
    MIN_RETENTION_DAYS,
    RETENTION_POLICY_DEFAULTS,
)
from backend.database import Base
from backend.models.tenant import Tenant
from backend.routers import data_management_settings as routes
from backend.schemas.data_management import TenantDataManagementSettingsUpdateRequest


RETENTION_DAY_FIELDS = tuple(
    field_name
    for field_name in RETENTION_POLICY_DEFAULTS
    if field_name != "retention_batch_size_per_tenant"
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


def _client(factory: sessionmaker[Session], *, tenant_id: int, role: str) -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)

    def fake_current_user():
        return SimpleNamespace(id=11, username="owner", is_active=True)

    def fake_tenant_context():
        return SimpleNamespace(tenant_id=tenant_id, user_id=11, role=role)

    def fake_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[routes.get_current_user] = fake_current_user
    app.dependency_overrides[routes.get_tenant_request_context] = fake_tenant_context
    app.dependency_overrides[routes.get_db] = fake_db
    return TestClient(app)


def test_owner_can_read_and_update_data_management_settings_with_report_retention_flag() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant_id = _seed_tenant(db)

    client = _client(factory, tenant_id=tenant_id, role="owner")
    response = client.get("/api/settings/data-management")

    assert response.status_code == 200
    assert response.json()["report_retention_enabled"] is True
    for field_name, default_value in RETENTION_POLICY_DEFAULTS.items():
        assert response.json()[field_name] == default_value

    updated_retention_values = {
        field_name: default_value + offset
        for offset, (field_name, default_value) in enumerate(
            RETENTION_POLICY_DEFAULTS.items(),
            start=1,
        )
    }
    response = client.put(
        "/api/settings/data-management",
        json={
            "report_retention_enabled": False,
            **updated_retention_values,
        },
    )

    assert response.status_code == 200
    assert response.json()["report_retention_enabled"] is False
    for field_name, expected_value in updated_retention_values.items():
        assert response.json()[field_name] == expected_value

    response = client.get("/api/settings/data-management")

    assert response.status_code == 200
    assert response.json()["report_retention_enabled"] is False
    for field_name, expected_value in updated_retention_values.items():
        assert response.json()[field_name] == expected_value


def test_data_management_settings_schema_validates_retention_bounds() -> None:
    for field_name in RETENTION_DAY_FIELDS:
        TenantDataManagementSettingsUpdateRequest.model_validate(
            {field_name: MIN_RETENTION_DAYS}
        )
        TenantDataManagementSettingsUpdateRequest.model_validate(
            {field_name: MAX_RETENTION_DAYS}
        )

        with pytest.raises(ValidationError):
            TenantDataManagementSettingsUpdateRequest.model_validate(
                {field_name: MIN_RETENTION_DAYS - 1}
            )

        with pytest.raises(ValidationError):
            TenantDataManagementSettingsUpdateRequest.model_validate(
                {field_name: MAX_RETENTION_DAYS + 1}
            )


def test_data_management_settings_schema_validates_batch_size_bounds() -> None:
    TenantDataManagementSettingsUpdateRequest.model_validate(
        {"retention_batch_size_per_tenant": MIN_RETENTION_BATCH_SIZE_PER_TENANT}
    )
    TenantDataManagementSettingsUpdateRequest.model_validate(
        {"retention_batch_size_per_tenant": MAX_RETENTION_BATCH_SIZE_PER_TENANT}
    )

    with pytest.raises(ValidationError):
        TenantDataManagementSettingsUpdateRequest.model_validate(
            {
                "retention_batch_size_per_tenant": (
                    MIN_RETENTION_BATCH_SIZE_PER_TENANT - 1
                )
            }
        )

    with pytest.raises(ValidationError):
        TenantDataManagementSettingsUpdateRequest.model_validate(
            {
                "retention_batch_size_per_tenant": (
                    MAX_RETENTION_BATCH_SIZE_PER_TENANT + 1
                )
            }
        )


@pytest.mark.parametrize(
    ("payload", "expected_detail"),
    [
        (
            {"report_history_retention_days": MAX_RETENTION_DAYS + 1},
            "Retention setting values are out of range.",
        ),
        (
            {
                "retention_batch_size_per_tenant": (
                    MAX_RETENTION_BATCH_SIZE_PER_TENANT + 1
                )
            },
            "Retention setting values are out of range.",
        ),
    ],
)
def test_out_of_range_retention_settings_return_http_400(
    payload: dict[str, int],
    expected_detail: str,
) -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant_id = _seed_tenant(db)

    client = _client(factory, tenant_id=tenant_id, role="owner")
    response = client.put("/api/settings/data-management", json=payload)

    assert response.status_code == 400
    assert response.json() == {"detail": expected_detail}


def test_unknown_data_management_settings_fields_are_rejected() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant_id = _seed_tenant(db)

    client = _client(factory, tenant_id=tenant_id, role="owner")
    response = client.put(
        "/api/settings/data-management",
        json={"report_history_retention_days": 90, "payload_sample": "secret"},
    )

    assert response.status_code == 422


def test_operator_cannot_manage_data_management_settings() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant_id = _seed_tenant(db)

    client = _client(factory, tenant_id=tenant_id, role="operator")
    response = client.put(
        "/api/settings/data-management",
        json={"report_history_retention_days": 90},
    )

    assert response.status_code == 403
