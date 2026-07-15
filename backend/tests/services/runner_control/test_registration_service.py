"""Tests for Runner Control runner registration transaction and metadata behavior."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models.core import User
from backend.models.runner_control import ExecutionSite, Runner, RunnerCredential, RunnerInstallToken
from backend.models.tenant import Tenant
from backend.services.runner_control.credentials import RunnerCredentialService
from backend.services.runner_control.registration_service import (
    MAX_LABEL_COUNT,
    RunnerRegistrationError,
    RunnerRegistrationRequest,
    RunnerRegistrationService,
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


def _seed_context(db: Session) -> tuple[int, int, ExecutionSite]:
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
    db.commit()
    return tenant.id, user.id, site


def test_register_runner_success_marks_token_used_creates_credential_and_emits_redacted_audit() -> None:
    db = _build_session()
    tenant_id, user_id, site = _seed_context(db)
    credential_service = RunnerCredentialService(db)
    issued_token = credential_service.issue_install_token(
        tenant_id=tenant_id,
        execution_site_id=site.id,
        created_by_user_id=user_id,
    )

    audit_events: list[dict[str, object]] = []
    registration = RunnerRegistrationService(
        db,
        credential_service=credential_service,
        audit_emitter=audit_events.append,
        channel_endpoint="wss://control.example.com/api/runner-control/channel",
        protocol_version="runner_control.v1",
        heartbeat_interval_seconds=45,
    )

    result = registration.register_runner(
        RunnerRegistrationRequest(
            tenant_id=tenant_id,
            install_token=issued_token.plaintext_token,
            runner_name="  runner-alpha  ",
            runner_version=" 1.2.3 ",
            labels={" Region ": " us-west ", "rack": "r1"},
            capabilities=["Docker", "kali", "docker"],
        )
    )

    runner = db.get(Runner, result.runner_id)
    assert runner is not None
    assert result.tenant_id == tenant_id
    assert runner.status == "registered"
    assert runner.name == "runner-alpha"
    assert runner.version == "1.2.3"
    assert runner.labels_json == {"region": "us-west", "rack": "r1"}
    assert runner.capabilities_json == ["docker", "kali"]

    token_row = db.get(RunnerInstallToken, issued_token.install_token_id)
    assert token_row is not None
    assert token_row.status == "used"
    assert token_row.used_at is not None

    credential = db.get(RunnerCredential, result.credential_id)
    assert credential is not None
    assert credential.secret_hash != result.credential_secret
    assert credential.credential_fingerprint == result.credential_fingerprint

    assert result.endpoint_metadata == {
        "channel_endpoint": "wss://control.example.com/api/runner-control/channel",
        "protocol_version": "runner_control.v1",
        "heartbeat_interval_seconds": 45,
    }

    assert len(audit_events) == 1
    audit_event = audit_events[0]
    assert audit_event["event_type"] == "runner.registered"
    metadata = audit_event["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["install_token"] != issued_token.plaintext_token
    assert metadata["runner_secret"] != result.credential_secret
    assert "rsec_" not in str(metadata["runner_secret"])


def test_register_runner_resolves_tenant_from_enrollment_token_without_request_tenant() -> None:
    db = _build_session()
    tenant_id, user_id, site = _seed_context(db)
    credential_service = RunnerCredentialService(db)
    issued_token = credential_service.issue_install_token(
        tenant_id=tenant_id,
        execution_site_id=site.id,
        created_by_user_id=user_id,
    )
    registration = RunnerRegistrationService(db, credential_service=credential_service)

    result = registration.register_runner(
        RunnerRegistrationRequest(
            install_token=issued_token.plaintext_token,
            runner_name="runner-site-host-1",
        )
    )

    runner = db.get(Runner, result.runner_id)
    assert runner is not None
    assert result.tenant_id == tenant_id
    assert runner.tenant_id == tenant_id
    assert runner.execution_site_id == site.id


def test_register_runner_rejects_replayed_install_token() -> None:
    db = _build_session()
    tenant_id, user_id, site = _seed_context(db)
    credential_service = RunnerCredentialService(db)
    issued_token = credential_service.issue_install_token(
        tenant_id=tenant_id,
        execution_site_id=site.id,
        created_by_user_id=user_id,
    )

    registration = RunnerRegistrationService(db, credential_service=credential_service)
    request = RunnerRegistrationRequest(
        tenant_id=tenant_id,
        install_token=issued_token.plaintext_token,
        runner_name="runner-alpha",
    )

    first = registration.register_runner(request)
    assert first.runner_id

    with pytest.raises(RunnerRegistrationError) as replay_error:
        registration.register_runner(request)
    assert replay_error.value.error_code == "RUNNER_INSTALL_TOKEN_INVALID"


def test_register_runner_is_transactional_when_credential_issue_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _build_session()
    tenant_id, user_id, site = _seed_context(db)
    credential_service = RunnerCredentialService(db)
    issued_token = credential_service.issue_install_token(
        tenant_id=tenant_id,
        execution_site_id=site.id,
        created_by_user_id=user_id,
    )

    def _raise_failure(*_: object, **__: object) -> object:
        raise RuntimeError("credential store unavailable")

    monkeypatch.setattr(credential_service, "issue_runner_credential", _raise_failure)

    registration = RunnerRegistrationService(db, credential_service=credential_service)

    with pytest.raises(RunnerRegistrationError) as error:
        registration.register_runner(
            RunnerRegistrationRequest(
                tenant_id=tenant_id,
                install_token=issued_token.plaintext_token,
                runner_name="runner-rolls-back",
            )
        )
    assert error.value.error_code == "RUNNER_REGISTRATION_FAILED"

    runner_count = db.execute(
        select(func.count()).select_from(Runner).where(Runner.tenant_id == tenant_id)
    ).scalar_one()
    assert runner_count == 0

    token_row = db.get(RunnerInstallToken, issued_token.install_token_id)
    assert token_row is not None
    assert token_row.status == "issued"
    assert token_row.used_at is None


def test_register_runner_normalizes_metadata_and_enforces_size_limits() -> None:
    db = _build_session()
    tenant_id, user_id, site = _seed_context(db)
    credential_service = RunnerCredentialService(db)
    issued_token = credential_service.issue_install_token(
        tenant_id=tenant_id,
        execution_site_id=site.id,
        created_by_user_id=user_id,
    )
    registration = RunnerRegistrationService(db, credential_service=credential_service)

    too_many_labels = {f"k{i}": "v" for i in range(MAX_LABEL_COUNT + 1)}

    with pytest.raises(RunnerRegistrationError) as error:
        registration.register_runner(
            RunnerRegistrationRequest(
                tenant_id=tenant_id,
                install_token=issued_token.plaintext_token,
                runner_name="runner-limits",
                labels=too_many_labels,
            )
        )

    assert error.value.error_code == "RUNNER_METADATA_INVALID"


def test_register_runner_attaches_to_precreated_runner_slot() -> None:
    db = _build_session()
    tenant_id, user_id, site = _seed_context(db)

    precreated = Runner(
        tenant_id=tenant_id,
        execution_site_id=site.id,
        name="runner-slot",
        status="inactive",
        version="0.9.0",
    )
    db.add(precreated)
    db.commit()

    credential_service = RunnerCredentialService(db, now_provider=lambda: datetime(2026, 5, 22, tzinfo=timezone.utc))
    issued_token = credential_service.issue_install_token(
        tenant_id=tenant_id,
        execution_site_id=site.id,
        created_by_user_id=user_id,
    )

    registration = RunnerRegistrationService(db, credential_service=credential_service)
    result = registration.register_runner(
        RunnerRegistrationRequest(
            tenant_id=tenant_id,
            install_token=issued_token.plaintext_token,
            runner_name="runner-slot",
            runner_version="1.0.0",
            capabilities={"docker": "true", "gpu": "false"},
        )
    )

    assert result.runner_id == precreated.id
    attached = db.get(Runner, precreated.id)
    assert attached is not None
    assert attached.status == "registered"
    assert attached.version == "1.0.0"
    assert attached.capabilities_json == {"docker": "true", "gpu": "false"}

    runner_count = db.execute(
        select(func.count()).select_from(Runner).where(Runner.tenant_id == tenant_id)
    ).scalar_one()
    assert runner_count == 1
