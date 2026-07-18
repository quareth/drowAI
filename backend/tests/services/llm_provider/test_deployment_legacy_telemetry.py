"""Tests for safe legacy LLM deployment telemetry signals."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID
from backend.models import (
    LLMInferenceConnection,
    LLMModelDeployment,
    User,
    UserLLMSelection,
)
from backend.services.llm_provider import (
    LLMCredentialRef,
    LLMCredentialService,
    LLMConnectionAuthorizationError,
    LLMDeploymentNotFoundError,
    LLMProviderMigrationService,
    LLMProviderSelectionService,
    LLMRuntimeAccessContext,
    LLMRuntimeSelection,
    LLMRuntimeSelectionV2,
)
from backend.services.llm_provider import runtime_client_resolver as resolver_module
from backend.services.llm_provider import selection_service as selection_module
from backend.services.llm_provider.runtime_client_resolver import (
    LLMRuntimeClientResolver,
)
from backend.services.llm_provider.types import DeploymentRef
from backend.services.metrics import utils as metric_utils


def _capture_metric_calls(monkeypatch: pytest.MonkeyPatch, module: object) -> list:
    calls: list[tuple[str, dict[str, str], int]] = []
    monkeypatch.setattr(
        module,
        "safe_inc_labeled",
        lambda name, labels, value=1: calls.append((name, dict(labels), value)),
        raising=False,
    )
    return calls


def _create_user(db: Session, username: str) -> User:
    user = User(
        username=f"{username}-{uuid4().hex}",
        password="unused-test-password-hash",
        email=f"{uuid4().hex}@example.com",
    )
    db.add(user)
    db.flush()
    return user


def _create_mapped_legacy_deployment(
    db: Session,
    *,
    owner: User,
    model: str = "gpt-5.2",
) -> tuple[LLMCredentialService, LLMModelDeployment, LLMInferenceConnection]:
    credential_service = LLMCredentialService(db)
    credential_service.upsert_api_key(
        user_id=owner.id,
        provider=OPENAI_PROVIDER_ID,
        api_key="sk-telemetry-secret",
    )
    LLMProviderSelectionService(
        db,
        credential_service=credential_service,
    ).set_selection(
        user_id=owner.id,
        provider=OPENAI_PROVIDER_ID,
        model=model,
    )
    LLMProviderMigrationService(db).backfill_deployment_identity_for_user(owner.id)
    deployment = db.execute(select(LLMModelDeployment)).scalar_one()
    connection = db.get(LLMInferenceConnection, deployment.connection_id)
    assert connection is not None
    return credential_service, deployment, connection


def test_safe_labeled_metric_names_drop_private_or_unbounded_label_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Metric helper keeps status labels and opaque ids, not topology/secrets."""

    names: list[str] = []
    monkeypatch.setattr(
        metric_utils,
        "safe_inc",
        lambda name, value=1: names.append(name),
    )
    deployment_id = str(uuid4())

    metric_utils.safe_inc_labeled(
        "llm_provider.legacy_identity_read.total",
        {
            "status": "credential_missing",
            "deployment_id": deployment_id,
            "endpoint_url": "https://10.10.10.10:9443/private",
            "api_key": "sk-should-not-leak",
            "model": "gpt-5.2",
        },
    )

    assert names == [
        (
            "llm_provider.legacy_identity_read.total"
            f".deployment_id.{deployment_id}.status.credential_missing"
        )
    ]
    serialized = repr(names)
    for forbidden in ("10.10.10.10", "9443", "sk-should-not-leak", "gpt-5.2"):
        assert forbidden not in serialized


def test_selection_reads_emit_unmapped_and_auth_missing_status_metrics(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selection telemetry exposes status labels without provider secrets."""

    calls = _capture_metric_calls(monkeypatch, selection_module)
    owner, _ = identity_users
    llm_identity_db.add(
        UserLLMSelection(
            user_id=owner.id,
            provider=OPENAI_PROVIDER_ID,
            model="gpt-5.2",
        )
    )
    llm_identity_db.flush()

    read = LLMProviderSelectionService(llm_identity_db).get_selection_read(owner.id)
    compat_model = LLMProviderSelectionService(
        llm_identity_db
    ).get_openai_model_compat(owner.id)

    assert read.status.status == "credential_missing"
    assert compat_model == "gpt-5.2"
    assert (
        "llm_provider.selection_status.total",
        {"status": "credential_missing"},
        1,
    ) in calls
    assert (
        "llm_provider.legacy_identity_read.total",
        {"status": "unmapped"},
        1,
    ) in calls
    assert (
        "llm_provider.legacy_compat_read.total",
        {"status": "selected"},
        1,
    ) in calls
    assert "sk-" not in repr(calls)


def test_runtime_resolver_emits_legacy_mapping_and_revision_status_metrics(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime telemetry distinguishes mapped legacy, stale, and denied states."""

    calls = _capture_metric_calls(monkeypatch, resolver_module)
    owner, _ = identity_users
    credential_service, deployment, connection = _create_mapped_legacy_deployment(
        llm_identity_db,
        owner=owner,
    )
    resolver = LLMRuntimeClientResolver(credential_service, db=llm_identity_db)
    access_context = LLMRuntimeAccessContext(runtime_user_id=owner.id)

    resolver.resolve_target(
        LLMRuntimeSelection(
            provider=OPENAI_PROVIDER_ID,
            model="gpt-5.2",
            credential_ref=LLMCredentialRef(
                user_id=owner.id,
                provider=OPENAI_PROVIDER_ID,
            ),
        ),
        access_context=access_context,
        purpose="telemetry-test",
    )

    assert (
        "llm_provider.legacy_identity_read.total",
        {
            "status": "mapped",
            "deployment_id": str(deployment.id),
        },
        1,
    ) in calls

    stale_selection = LLMRuntimeSelectionV2(
        deployment_ref=DeploymentRef(
            deployment_id=str(deployment.id),
            expected_revision=int(deployment.revision) + 1,
        )
    )
    with pytest.raises(LLMDeploymentNotFoundError):
        resolver.resolve_target(
            stale_selection,
            access_context=access_context,
            purpose="telemetry-test",
        )
    assert (
        "llm_provider.deployment_resolution.total",
        {
            "status": "stale_revision",
            "deployment_id": str(deployment.id),
        },
        1,
    ) in calls

    connection.state = "disabled"
    llm_identity_db.flush()
    denied_selection = LLMRuntimeSelectionV2(
        deployment_ref=DeploymentRef(
            deployment_id=str(deployment.id),
            expected_revision=int(deployment.revision),
        )
    )
    with pytest.raises(LLMConnectionAuthorizationError):
        resolver.resolve_target(
            denied_selection,
            access_context=access_context,
            purpose="telemetry-test",
        )
    assert (
        "llm_provider.deployment_resolution.total",
        {
            "status": "connection_not_enabled",
            "deployment_id": str(deployment.id),
            "connection_id": str(connection.id),
        },
        1,
    ) in calls
    assert "sk-telemetry-secret" not in repr(calls)


def test_runtime_resolver_emits_connection_revision_conflict_metric(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authorization revision conflicts become explicit safe telemetry."""

    calls = _capture_metric_calls(monkeypatch, resolver_module)
    owner, _ = identity_users
    credential_service, deployment, connection = _create_mapped_legacy_deployment(
        llm_identity_db,
        owner=owner,
    )

    class _StaleAuthorizer:
        def authorize(self, **_kwargs):
            raise LLMConnectionAuthorizationError(
                code="stale_connection_revision",
                message="Connection revision is stale",
            )

    resolver = LLMRuntimeClientResolver(
        credential_service,
        db=llm_identity_db,
        connection_authorizer=_StaleAuthorizer(),
    )
    with pytest.raises(LLMConnectionAuthorizationError):
        resolver.resolve_target(
            LLMRuntimeSelectionV2(
                deployment_ref=DeploymentRef(
                    deployment_id=str(deployment.id),
                    expected_revision=int(deployment.revision),
                )
            ),
            access_context=LLMRuntimeAccessContext(runtime_user_id=owner.id),
            purpose="telemetry-test",
        )

    assert (
        "llm_provider.deployment_resolution.total",
        {
            "status": "connection_revision_conflict",
            "deployment_id": str(deployment.id),
            "connection_id": str(connection.id),
        },
        1,
    ) in calls
