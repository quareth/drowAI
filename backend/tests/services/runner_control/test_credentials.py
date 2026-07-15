"""Tests for Runner Control runner install-token and credential service behavior."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models.core import User
from backend.models.runner_control import ExecutionSite, Runner, RunnerCredential, RunnerInstallToken
from backend.models.tenant import Tenant
from backend.services.runner_control import credentials as credentials_module
from backend.services.runner_control.credentials import (
    RunnerCredentialAuthError,
    RunnerCredentialService,
    RunnerInstallTokenValidationError,
)


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            ExecutionSite.__table__,
            Runner.__table__,
            RunnerInstallToken.__table__,
            RunnerCredential.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return factory()


def _seed_runner_context(db: Session) -> tuple[int, int, object, object]:
    tenant = Tenant(slug="tenant-one", name="Tenant One")
    user = User(username="owner", password="hashed")
    db.add_all([tenant, user])
    db.flush()

    site = ExecutionSite(
        tenant_id=tenant.id,
        name="Primary Site",
        slug="primary-site",
        status="active",
    )
    db.add(site)
    db.flush()

    runner = Runner(
        tenant_id=tenant.id,
        execution_site_id=site.id,
        name="runner-alpha",
        status="active",
    )
    db.add(runner)
    db.commit()
    return tenant.id, user.id, site, runner


def test_issue_install_token_persists_only_hash_and_returns_plaintext_once() -> None:
    db = _build_session()
    tenant_id, user_id, site, _runner = _seed_runner_context(db)
    service = RunnerCredentialService(db)

    issued = service.issue_install_token(
        tenant_id=tenant_id,
        execution_site_id=site.id,
        created_by_user_id=user_id,
    )

    row = db.get(RunnerInstallToken, issued.install_token_id)
    assert row is not None
    assert row.token_hash != issued.plaintext_token
    assert issued.plaintext_token.startswith("rit_")
    assert "sha256$" in row.token_hash


def test_verify_install_token_rejects_used_expired_revoked_and_wrong_tenant() -> None:
    db = _build_session()
    tenant_id, user_id, site, _runner = _seed_runner_context(db)
    service = RunnerCredentialService(db)

    issued = service.issue_install_token(
        tenant_id=tenant_id,
        execution_site_id=site.id,
        created_by_user_id=user_id,
    )

    assert service.verify_install_token(tenant_id=tenant_id, plaintext_token=issued.plaintext_token)

    service.mark_install_token_used(
        service.verify_install_token(tenant_id=tenant_id, plaintext_token=issued.plaintext_token)
    )
    with pytest.raises(RunnerInstallTokenValidationError) as used_error:
        service.verify_install_token(tenant_id=tenant_id, plaintext_token=issued.plaintext_token)
    assert used_error.value.error_code == "RUNNER_INSTALL_TOKEN_INVALID"

    expired = service.issue_install_token(
        tenant_id=tenant_id,
        execution_site_id=site.id,
        created_by_user_id=user_id,
        ttl=timedelta(seconds=-1),
    )
    with pytest.raises(RunnerInstallTokenValidationError) as expired_error:
        service.verify_install_token(tenant_id=tenant_id, plaintext_token=expired.plaintext_token)
    assert expired_error.value.error_code == "RUNNER_INSTALL_TOKEN_INVALID"

    revoked = service.issue_install_token(
        tenant_id=tenant_id,
        execution_site_id=site.id,
        created_by_user_id=user_id,
    )
    revoked_row = db.get(RunnerInstallToken, revoked.install_token_id)
    revoked_row.status = "revoked"
    db.flush()
    with pytest.raises(RunnerInstallTokenValidationError) as revoked_error:
        service.verify_install_token(tenant_id=tenant_id, plaintext_token=revoked.plaintext_token)
    assert revoked_error.value.error_code == "RUNNER_INSTALL_TOKEN_INVALID"

    wrong_tenant = service.issue_install_token(
        tenant_id=tenant_id,
        execution_site_id=site.id,
        created_by_user_id=user_id,
    )
    with pytest.raises(RunnerInstallTokenValidationError) as tenant_error:
        service.verify_install_token(tenant_id=tenant_id + 99, plaintext_token=wrong_tenant.plaintext_token)
    assert tenant_error.value.error_code == "RUNNER_INSTALL_TOKEN_INVALID"


def test_install_token_verification_uses_constant_time_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _build_session()
    tenant_id, user_id, site, _runner = _seed_runner_context(db)
    service = RunnerCredentialService(db)
    issued = service.issue_install_token(
        tenant_id=tenant_id,
        execution_site_id=site.id,
        created_by_user_id=user_id,
    )

    called = {"count": 0}
    original = credentials_module.compare_digest

    def _tracking_compare(left: str, right: str) -> bool:
        called["count"] += 1
        return original(left, right)

    monkeypatch.setattr(credentials_module, "compare_digest", _tracking_compare)

    service.verify_install_token(tenant_id=tenant_id, plaintext_token=issued.plaintext_token)

    assert called["count"] >= 1


def test_install_token_verification_uses_constant_time_compare_for_missing_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _build_session()
    tenant_id, _user_id, _site, _runner = _seed_runner_context(db)
    service = RunnerCredentialService(db)

    called = {"count": 0}
    original = credentials_module.compare_digest

    def _tracking_compare(left: str, right: str) -> bool:
        called["count"] += 1
        return original(left, right)

    monkeypatch.setattr(credentials_module, "compare_digest", _tracking_compare)

    with pytest.raises(RunnerInstallTokenValidationError) as error:
        service.verify_install_token(tenant_id=tenant_id, plaintext_token="rit_missing_token")

    assert error.value.error_code == "RUNNER_INSTALL_TOKEN_INVALID"
    assert called["count"] >= 1


def test_issue_runner_credential_persists_hash_fingerprint_and_expiration() -> None:
    db = _build_session()
    tenant_id, _user_id, _site, runner = _seed_runner_context(db)
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    service = RunnerCredentialService(db, now_provider=lambda: now)

    issued = service.issue_runner_credential(tenant_id=tenant_id, runner_id=runner.id)

    row = db.get(RunnerCredential, issued.credential_id)
    assert row is not None
    assert row.secret_hash != issued.plaintext_secret
    assert row.credential_fingerprint == issued.credential_fingerprint
    assert row.expires_at.replace(tzinfo=timezone.utc) == now + timedelta(days=90)
    assert issued.plaintext_secret.startswith("rsec_")


def test_authenticate_runner_credential_rejects_expired_revoked_or_invalid_secret() -> None:
    db = _build_session()
    tenant_id, _user_id, _site, runner = _seed_runner_context(db)
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    service = RunnerCredentialService(db, now_provider=lambda: now)

    valid = service.issue_runner_credential(tenant_id=tenant_id, runner_id=runner.id)
    matched = service.authenticate_runner_credential(
        tenant_id=tenant_id,
        runner_id=runner.id,
        plaintext_secret=valid.plaintext_secret,
    )
    assert matched.id == valid.credential_id
    assert matched.last_used_at.replace(tzinfo=timezone.utc) == now

    with pytest.raises(RunnerCredentialAuthError) as invalid_error:
        service.authenticate_runner_credential(
            tenant_id=tenant_id,
            runner_id=runner.id,
            plaintext_secret="wrong-secret",
        )
    assert invalid_error.value.error_code == "RUNNER_AUTH_INVALID"

    revoked = service.issue_runner_credential(tenant_id=tenant_id, runner_id=runner.id)
    revoked_row = db.get(RunnerCredential, revoked.credential_id)
    service.revoke_runner_credential(revoked_row)
    with pytest.raises(RunnerCredentialAuthError) as revoked_error:
        service.authenticate_runner_credential(
            tenant_id=tenant_id,
            runner_id=runner.id,
            plaintext_secret=revoked.plaintext_secret,
        )
    assert revoked_error.value.error_code == "RUNNER_AUTH_REVOKED"

    expired_now = now + timedelta(days=91)
    service_with_future_time = RunnerCredentialService(db, now_provider=lambda: expired_now)
    expired = service_with_future_time.issue_runner_credential(tenant_id=tenant_id, runner_id=runner.id)
    later = expired_now + timedelta(days=91)
    expired_checker = RunnerCredentialService(db, now_provider=lambda: later)

    with pytest.raises(RunnerCredentialAuthError) as expired_error:
        expired_checker.authenticate_runner_credential(
            tenant_id=tenant_id,
            runner_id=runner.id,
            plaintext_secret=expired.plaintext_secret,
        )
    assert expired_error.value.error_code == "RUNNER_AUTH_EXPIRED"


def test_mask_helpers_do_not_return_raw_values() -> None:
    raw_token = "rit_super_secret_install_token_value"
    raw_secret = "rsec_super_secret_runner_secret_value"

    fields = RunnerCredentialService.build_masked_log_fields(
        install_token=raw_token,
        runner_secret=raw_secret,
        credential_fingerprint="fp-123",
    )

    assert fields["install_token"] != raw_token
    assert fields["runner_secret"] != raw_secret
    assert "super_secret" not in fields["install_token"]
    assert "super_secret" not in fields["runner_secret"]
    assert fields["credential_fingerprint"] == "fp-123"


def test_runner_secret_hash_verification_uses_constant_time_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _build_session()
    tenant_id, _user_id, _site, runner = _seed_runner_context(db)
    service = RunnerCredentialService(db)
    issued = service.issue_runner_credential(tenant_id=tenant_id, runner_id=runner.id)

    called = {"count": 0}
    original = credentials_module.compare_digest

    def _tracking_compare(left: str, right: str) -> bool:
        called["count"] += 1
        return original(left, right)

    monkeypatch.setattr(credentials_module, "compare_digest", _tracking_compare)

    service.authenticate_runner_credential(
        tenant_id=tenant_id,
        runner_id=runner.id,
        plaintext_secret=issued.plaintext_secret,
    )

    assert called["count"] >= 1
