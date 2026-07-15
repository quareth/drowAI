"""Tests for platform installation service gating."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models import User
from backend.models.platform_installation import PlatformInstallation
from backend.services.platform.installation_service import PlatformInstallationService


@pytest.fixture
def db() -> Session:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(autouse=True)
def _reset_platform_installation(db: Session):
    from backend.models.platform_installation import PlatformInstallation

    db.query(PlatformInstallation).delete()
    db.query(User).delete()
    db.commit()
    yield
    db.query(PlatformInstallation).delete()
    db.query(User).delete()
    db.commit()


def test_setup_required_for_standalone_when_not_complete(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "single_host")
    service = PlatformInstallationService(db)
    assert service.is_wizard_enabled() is True
    assert service.is_setup_required() is True


def test_setup_not_required_when_complete(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "single_host")
    service = PlatformInstallationService(db)
    service.mark_complete(network_config={"gateway": "10.0.0.1"})
    db.commit()
    assert service.is_setup_required() is False
    assert service.get_status() == "complete"


def test_wizard_enabled_for_distributed_profile(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "distributed")
    service = PlatformInstallationService(db)
    assert service.is_wizard_enabled() is True
    assert service.is_setup_required() is True


def test_repair_legacy_installation_marks_complete_when_users_exist(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "dev_local")
    db.add(User(username="legacy-admin", password="hashed", email="legacy@example.com"))
    db.commit()

    service = PlatformInstallationService(db)
    repaired = service.repair_legacy_installation_if_needed()
    db.commit()

    assert repaired is True
    assert service.is_complete() is True
    assert service.get_status() == "complete"


def test_repair_legacy_skips_when_installation_row_exists_without_completion(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "dev_local")
    db.add(User(username="legacy-admin", password="hashed", email="legacy@example.com"))
    db.add(PlatformInstallation(id=1, deployment_profile="dev_local", network_config={}, display_defaults={}))
    db.commit()

    service = PlatformInstallationService(db)
    repaired = service.repair_legacy_installation_if_needed()
    db.commit()

    assert repaired is False
    assert service.is_setup_required() is True


def test_network_and_display_defaults_persist(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "single_host")
    service = PlatformInstallationService(db)
    service.mark_complete(
        network_config={"domain": "drowai.local"},
        display_defaults={"timezone": "UTC"},
    )
    db.commit()

    record = db.get(PlatformInstallation, 1)
    assert record is not None
    assert record.network_config == {"domain": "drowai.local"}
    assert record.display_defaults == {"timezone": "UTC"}


def test_provisioning_and_failure_state_persist(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "single_host")
    service = PlatformInstallationService(db)

    service.mark_provisioning(provisioning_metadata={"tenant_id": 1})
    db.commit()
    assert service.get_status() == "provisioning"
    assert service.is_setup_required() is True

    service.mark_failed(setup_error="Setup artifact publication failed.")
    db.commit()
    assert service.get_status() == "failed"
    assert service.get_setup_error() == "Setup artifact publication failed."
    assert service.is_setup_required() is True
