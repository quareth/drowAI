"""Direct workflow tests for GPT-OSS proving connection lifecycles.

These tests prove transaction ownership, owner and revision validation, stored
secret confinement, capability evidence gating, lifecycle order, and inventory
authority observation rebinding without migrating the active FastAPI routes.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import (
    LLMCapabilityObservation,
    LLMInferenceConnection,
    User,
    UserLLMProviderCredential,
)
from backend.services.llm_provider.application_contracts import (
    ConnectionStatusOutcome,
    RunnabilityOutcome,
    VerificationOutcome,
)
from backend.services.llm_provider.connection_authorization import (
    LLMConnectionAuthorizer,
)
from backend.services.llm_provider.connection_service import LLMConnectionService
from backend.services.llm_provider.credential_service import LLMCredentialService
from backend.services.llm_provider.deployment_service import LLMDeploymentService
from backend.services.llm_provider.health_service import LLMProviderHealthService
from backend.services.llm_provider.inventory_service import LLMProviderInventoryService
from backend.services.llm_provider.operation_registry import (
    GPT_OSS_20B_PROVING_BASE_URL_ENV,
    GPT_OSS_20B_PROVING_PRESET_ID,
    HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    ConnectionOperationRegistry,
)
from backend.services.llm_provider.proving_connection_lifecycle_service import (
    LLMProvingConnectionLifecycleService,
)
from backend.services.llm_provider.types import (
    GuardedHTTPResponse,
    LLMConnectionCredentialRef,
    LLMConnectionOperation,
    LLMConnectionState,
    LLMDeploymentValidationError,
    LLMProviderServiceError,
    ProviderConfigurationError,
)

_SECRET = "proving-direct-secret-must-not-appear"


class _RecordingAuthorizer:
    """Record proving authorization while preserving the real policy decision."""

    def __init__(
        self,
        db: Session,
        events: list[tuple[str, object]],
        *,
        operation_registry: ConnectionOperationRegistry,
    ) -> None:
        self._delegate = LLMConnectionAuthorizer(
            db,
            operation_registry=operation_registry,
        )
        self._events = events

    def authorize(self, **kwargs):
        self._events.append(("authorize", kwargs["operation"]))
        return self._delegate.authorize(**kwargs)


class _ProvingTransport:
    """Return bounded inventory and usage evidence for direct workflow tests."""

    def __init__(self, events: list[tuple[str, object]]) -> None:
        self._events = events

    def execute(self, operation, **_kwargs):
        self._events.append(("transport", operation))
        body = (
            b'{"data":[{"id":"openai/gpt-oss-20b"}]}'
            if operation is LLMConnectionOperation.INVENTORY
            else b'{"usage":{"prompt_tokens":1,'
            b'"completion_tokens":1,"total_tokens":2}}'
        )
        return GuardedHTTPResponse(
            status_code=200,
            body=body,
            audit_id=f"audit-{operation.value}",
        )


def _service(
    db: Session,
) -> tuple[LLMProvingConnectionLifecycleService, list[tuple[str, object]]]:
    events: list[tuple[str, object]] = []
    registry = ConnectionOperationRegistry(
        env_getter=lambda name: (
            "https://gpt-oss.example.test"
            if name == GPT_OSS_20B_PROVING_BASE_URL_ENV
            else None
        )
    )
    authorizer = _RecordingAuthorizer(
        db,
        events,
        operation_registry=registry,
    )
    inventory = LLMProviderInventoryService(
        db,
        connection_authorizer=authorizer,
        guarded_transport=_ProvingTransport(events),
        operation_registry=registry,
    )
    health = LLMProviderHealthService(
        db,
        connection_authorizer=authorizer,
        guarded_transport=_ProvingTransport(events),
    )
    return (
        LLMProvingConnectionLifecycleService(
            db,
            health_service=health,
            inventory_service=inventory,
        ),
        events,
    )


def _track_transactions(
    monkeypatch: pytest.MonkeyPatch,
    db: Session,
) -> list[str]:
    events: list[str] = []
    original_commit = db.commit
    original_rollback = db.rollback

    def commit() -> None:
        events.append("commit")
        original_commit()

    def rollback() -> None:
        events.append("rollback")
        original_rollback()

    monkeypatch.setattr(db, "commit", commit)
    monkeypatch.setattr(db, "rollback", rollback)
    return events


def _seed_proving(
    db: Session,
    *,
    user_id: int,
    secret: str = _SECRET,
) -> tuple[LLMInferenceConnection, Any, Any]:
    connection = LLMConnectionService(db).create_gpt_oss_20b_proving_draft(
        user_id=user_id,
    )
    LLMCredentialService(db).upsert_connection_api_key(
        user_id=user_id,
        connection_ref=LLMConnectionCredentialRef(
            connection_id=str(connection.id),
            expected_revision=int(connection.revision),
        ),
        provider=GPT_OSS_20B_PROVING_PRESET_ID,
        api_key=secret,
    )
    db.refresh(connection)
    deployment, route = LLMDeploymentService(
        db
    ).create_gpt_oss_20b_proving_deployment(
        user_id=user_id,
        connection_id=connection.id,
        expected_connection_revision=int(connection.revision),
    )
    db.commit()
    return connection, deployment, route


def _seed_managed_deployment(db: Session, *, user_id: int):
    """Create an owner-scoped deployment bound to a different connection."""

    preset = ConnectionOperationRegistry().get_connection_preset(
        HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID
    )
    connection = LLMConnectionService(db).create_draft(
        user_id=user_id,
        display_name=preset.display_name,
        connection_preset_id=preset.id,
        runtime_family_id=preset.runtime_family_id,
        serving_operator_id=preset.serving_operator_id,
    )
    deployment, _ = LLMDeploymentService(db).create_preset_deployment(
        user_id=user_id,
        connection_id=connection.id,
        expected_connection_revision=int(connection.revision),
        wire_model_id="openai/gpt-oss-20b",
        display_name="Managed GPT-OSS",
        canonical_model_id="openai/gpt-oss-20b",
    )
    db.commit()
    return deployment


def _assert_guarded_probe_order(events: list[tuple[str, object]]) -> None:
    assert events == [
        ("authorize", LLMConnectionOperation.INVENTORY),
        ("transport", LLMConnectionOperation.INVENTORY),
        ("authorize", LLMConnectionOperation.CAPABILITY_PROBE),
        ("transport", LLMConnectionOperation.CAPABILITY_PROBE),
    ]


def test_create_connection_preserves_order_status_and_secret_confinement(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Create stores the secret before deployment and commits one typed status."""

    owner, _ = identity_users
    transactions = _track_transactions(monkeypatch, llm_identity_db)
    service, _ = _service(llm_identity_db)
    calls: list[str] = []
    for dependency, method_name, label in (
        (service._connections, "create_gpt_oss_20b_proving_draft", "draft"),
        (service._credentials, "upsert_connection_api_key", "credential"),
        (service._deployments, "create_gpt_oss_20b_proving_deployment", "deployment"),
        (service._status, "proving_status", "status"),
    ):
        original = getattr(dependency, method_name)

        def recording(*args, _original=original, _label=label, **kwargs):
            calls.append(_label)
            return _original(*args, **kwargs)

        monkeypatch.setattr(dependency, method_name, recording)

    outcome = service.create_connection(
        user_id=owner.id,
        preset_id=GPT_OSS_20B_PROVING_PRESET_ID,
        api_key=_SECRET,
        display_label="Proof endpoint",
    )

    assert isinstance(outcome, ConnectionStatusOutcome)
    assert outcome.lifecycle_state == LLMConnectionState.DRAFT.value
    assert outcome.connection_ref.expected_revision == 2
    assert outcome.deployment_ref is not None
    assert outcome.deployment_ref.expected_revision == 1
    assert outcome.verification is not None
    assert outcome.verification.code == "not_tested"
    assert calls == ["draft", "credential", "deployment", "status"]
    assert transactions == ["commit"]
    connection = llm_identity_db.get(
        LLMInferenceConnection,
        outcome.connection_ref.connection_id,
    )
    credential = llm_identity_db.execute(
        select(UserLLMProviderCredential).where(
            UserLLMProviderCredential.user_id == owner.id,
            UserLLMProviderCredential.provider == GPT_OSS_20B_PROVING_PRESET_ID,
        )
    ).scalar_one()
    assert connection is not None
    assert connection.non_secret_config is None
    assert credential.encrypted_api_key != _SECRET
    assert _SECRET not in credential.encrypted_api_key
    assert _SECRET not in f"{outcome!r}\n{caplog.text}"


def test_create_rolls_back_validation_and_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Create rejects invalid entry and removes partial draft/credential writes."""

    owner, _ = identity_users
    transactions = _track_transactions(monkeypatch, llm_identity_db)
    service, _ = _service(llm_identity_db)
    with pytest.raises(ProviderConfigurationError, match="Proving API key is required"):
        service.create_connection(
            user_id=owner.id,
            preset_id=GPT_OSS_20B_PROVING_PRESET_ID,
            api_key=None,
        )
    assert transactions == ["rollback"]

    transactions.clear()
    monkeypatch.setattr(
        service._deployments,
        "create_gpt_oss_20b_proving_deployment",
        lambda **_kwargs: (_ for _ in ()).throw(
            LLMProviderServiceError("deployment route failed")
        ),
    )
    with pytest.raises(LLMProviderServiceError, match="deployment route failed"):
        service.create_connection(
            user_id=owner.id,
            preset_id=GPT_OSS_20B_PROVING_PRESET_ID,
            api_key=_SECRET,
        )
    assert transactions == ["rollback"]
    assert llm_identity_db.execute(select(LLMInferenceConnection)).scalars().all() == []


@pytest.mark.parametrize(
    "method",
    ("create_connection", "test_connection", "enable_connection"),
)
def test_workflows_reject_non_proving_preset_before_side_effects(
    method: str,
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Every entry validates the code-owned proving preset and rolls back once."""

    owner, _ = identity_users
    transactions = _track_transactions(monkeypatch, llm_identity_db)
    service, events = _service(llm_identity_db)
    kwargs: dict[str, object] = {
        "user_id": owner.id,
        "preset_id": HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    }
    if method == "create_connection":
        kwargs["api_key"] = _SECRET
    elif method == "test_connection":
        kwargs.update(
            api_key=_SECRET,
            connection_id="missing",
            expected_connection_revision=1,
            deployment_id="missing",
            expected_deployment_revision=1,
        )
    else:
        kwargs.update(
            connection_id="missing",
            expected_connection_revision=1,
            deployment_id="missing",
            expected_deployment_revision=1,
        )
    with pytest.raises(Exception, match="Unknown proving preset"):
        getattr(service, method)(**kwargs)
    assert transactions == ["rollback"]
    assert events == []


def test_connection_records_evidence_and_commits_sanitized_verification(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Test validates refs and secret, fingerprints, probes, then commits evidence."""

    owner, _ = identity_users
    connection, deployment, route = _seed_proving(
        llm_identity_db,
        user_id=owner.id,
    )
    transactions = _track_transactions(monkeypatch, llm_identity_db)
    service, events = _service(llm_identity_db)
    outcome = service.test_connection(
        user_id=owner.id,
        preset_id=GPT_OSS_20B_PROVING_PRESET_ID,
        api_key=_SECRET,
        connection_id=str(connection.id),
        expected_connection_revision=int(connection.revision),
        deployment_id=str(deployment.id),
        expected_deployment_revision=int(deployment.revision),
    )

    assert isinstance(outcome, VerificationOutcome)
    assert outcome.status == "passed", outcome
    assert outcome.code == "verified"
    assert outcome.model_present is True
    assert outcome.usage is not None
    assert outcome.usage.total_tokens == 2
    assert transactions == ["commit"]
    _assert_guarded_probe_order(events)
    observations = llm_identity_db.execute(
        select(LLMCapabilityObservation).where(
            LLMCapabilityObservation.deployment_id == deployment.id,
            LLMCapabilityObservation.route_id == route.id,
        )
    ).scalars().all()
    assert {row.capability for row in observations} == {"chat", "usage_reporting"}
    assert all(
        row.constraints["connection_revision"] == int(connection.revision)
        for row in observations
    )
    assert _SECRET not in f"{outcome!r}\n{caplog.text}"


def test_connection_rejects_stale_deployment_and_rotated_secret_before_egress(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Test preserves revision and exact stored-secret failures without egress."""

    owner, _ = identity_users
    connection, deployment, _ = _seed_proving(
        llm_identity_db,
        user_id=owner.id,
    )
    transactions = _track_transactions(monkeypatch, llm_identity_db)
    service, events = _service(llm_identity_db)
    common = dict(
        user_id=owner.id,
        preset_id=GPT_OSS_20B_PROVING_PRESET_ID,
        connection_id=str(connection.id),
        expected_connection_revision=int(connection.revision),
        deployment_id=str(deployment.id),
    )
    with pytest.raises(ProviderConfigurationError, match="Deployment revision is stale"):
        service.test_connection(
            **common,
            expected_deployment_revision=int(deployment.revision) + 1,
            api_key=_SECRET,
        )
    with pytest.raises(
        ProviderConfigurationError,
        match="Stored proving credential must pass verification",
    ) as caught:
        service.test_connection(
            **common,
            expected_deployment_revision=int(deployment.revision),
            api_key="rotated-secret",
        )
    with pytest.raises(LLMProviderServiceError, match="revision is stale"):
        service.test_connection(
            **{
                **common,
                "expected_connection_revision": int(connection.revision) + 1,
            },
            expected_deployment_revision=int(deployment.revision),
            api_key=_SECRET,
        )
    assert transactions == ["rollback", "rollback", "rollback"]
    assert events == []
    assert "rotated-secret" not in f"{caught.value!s}\n{caught.value!r}\n{caplog.text}"


def test_ref_workflows_reject_foreign_and_mismatched_refs_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Owner and connection/deployment binding failures precede evidence and state work."""

    owner, other = identity_users
    connection, deployment, _ = _seed_proving(llm_identity_db, user_id=owner.id)
    foreign, foreign_deployment, _ = _seed_proving(
        llm_identity_db,
        user_id=other.id,
    )
    second_deployment = _seed_managed_deployment(llm_identity_db, user_id=owner.id)
    transactions = _track_transactions(monkeypatch, llm_identity_db)
    service, events = _service(llm_identity_db)
    with pytest.raises(LLMProviderServiceError, match="Deployment was not found"):
        service.test_connection(
            user_id=owner.id,
            preset_id=GPT_OSS_20B_PROVING_PRESET_ID,
            api_key=_SECRET,
            connection_id=str(foreign.id),
            expected_connection_revision=int(foreign.revision),
            deployment_id=str(foreign_deployment.id),
            expected_deployment_revision=int(foreign_deployment.revision),
        )
    with pytest.raises(
        LLMProviderServiceError,
        match="Connection credential ref is unavailable",
    ):
        service.test_connection(
            user_id=owner.id,
            preset_id=GPT_OSS_20B_PROVING_PRESET_ID,
            api_key=_SECRET,
            connection_id=str(foreign.id),
            expected_connection_revision=int(foreign.revision),
            deployment_id=str(deployment.id),
            expected_deployment_revision=int(deployment.revision),
        )
    with pytest.raises(ProviderConfigurationError, match="Deployment revision is stale"):
        service.enable_connection(
            user_id=owner.id,
            preset_id=GPT_OSS_20B_PROVING_PRESET_ID,
            connection_id=str(connection.id),
            expected_connection_revision=int(connection.revision),
            deployment_id=str(deployment.id),
            expected_deployment_revision=int(deployment.revision) + 1,
        )
    with pytest.raises(LLMProviderServiceError, match="revision"):
        service.enable_connection(
            user_id=owner.id,
            preset_id=GPT_OSS_20B_PROVING_PRESET_ID,
            connection_id=str(connection.id),
            expected_connection_revision=int(connection.revision) + 1,
            deployment_id=str(deployment.id),
            expected_deployment_revision=int(deployment.revision),
        )
    with pytest.raises(LLMDeploymentValidationError, match="Deployment route is unavailable"):
        service.enable_connection(
            user_id=owner.id,
            preset_id=GPT_OSS_20B_PROVING_PRESET_ID,
            connection_id=str(connection.id),
            expected_connection_revision=int(connection.revision),
            deployment_id=str(second_deployment.id),
            expected_deployment_revision=int(second_deployment.revision),
        )
    assert transactions == ["rollback"] * 5
    assert events == []
    assert connection.state == LLMConnectionState.DRAFT.value
    assert deployment.connection_id == connection.id


def test_enable_requires_evidence_then_transitions_rebinds_and_commits(
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Enable gates before mutation and rebinds verified evidence after transitions."""

    owner, _ = identity_users
    connection, deployment, route = _seed_proving(llm_identity_db, user_id=owner.id)
    transactions = _track_transactions(monkeypatch, llm_identity_db)
    service, _ = _service(llm_identity_db)
    kwargs = dict(
        user_id=owner.id,
        preset_id=GPT_OSS_20B_PROVING_PRESET_ID,
        connection_id=str(connection.id),
        expected_connection_revision=int(connection.revision),
        deployment_id=str(deployment.id),
        expected_deployment_revision=int(deployment.revision),
    )
    with pytest.raises(
        ProviderConfigurationError,
        match="Successful proving verification is required before enablement",
    ):
        service.enable_connection(**kwargs)
    assert transactions == ["rollback"]
    assert connection.state == LLMConnectionState.DRAFT.value

    verifier, _ = _service(llm_identity_db)
    verifier.test_connection(api_key=_SECRET, **kwargs)
    transactions.clear()
    calls: list[tuple[str, object]] = []
    original_transition = service._connections.transition_state
    original_rebind = service._inventory.rebind_proving_observation_revision
    original_status = service._status.proving_status

    def transition(**values):
        calls.append(("transition", values["target_state"]))
        return original_transition(**values)

    def rebind(**values):
        calls.append(("rebind", values["previous_connection_revision"]))
        return original_rebind(**values)

    def status(**values):
        calls.append(("status", values["connection"].revision))
        return original_status(**values)

    monkeypatch.setattr(service._connections, "transition_state", transition)
    monkeypatch.setattr(service._inventory, "rebind_proving_observation_revision", rebind)
    monkeypatch.setattr(service._status, "proving_status", status)
    outcome = service.enable_connection(**kwargs)

    assert outcome.lifecycle_state == LLMConnectionState.ENABLED.value
    assert outcome.connection_ref.expected_revision == 4
    assert outcome.verification is not None
    assert outcome.verification.code == "verified"
    assert calls == [
        ("transition", LLMConnectionState.DISABLED),
        ("transition", LLMConnectionState.ENABLED),
        ("rebind", 2),
        ("status", 4),
    ]
    assert transactions == ["commit"]
    observations = llm_identity_db.execute(
        select(LLMCapabilityObservation).where(
            LLMCapabilityObservation.deployment_id == deployment.id,
            LLMCapabilityObservation.route_id == route.id,
        )
    ).scalars().all()
    assert observations
    assert all(row.constraints["connection_revision"] == 4 for row in observations)


def test_enable_rejects_rotated_credential_fingerprint_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Evidence bound to the prior encrypted credential cannot enable after rotation."""

    owner, _ = identity_users
    connection, deployment, _ = _seed_proving(llm_identity_db, user_id=owner.id)
    verifier, _ = _service(llm_identity_db)
    common = dict(
        user_id=owner.id,
        preset_id=GPT_OSS_20B_PROVING_PRESET_ID,
        connection_id=str(connection.id),
        deployment_id=str(deployment.id),
        expected_deployment_revision=int(deployment.revision),
    )
    verifier.test_connection(
        api_key=_SECRET,
        expected_connection_revision=int(connection.revision),
        **common,
    )
    LLMCredentialService(llm_identity_db).upsert_connection_api_key(
        user_id=owner.id,
        connection_ref=LLMConnectionCredentialRef(
            connection_id=str(connection.id),
            expected_revision=int(connection.revision),
        ),
        provider=GPT_OSS_20B_PROVING_PRESET_ID,
        api_key="rotated-secret",
    )
    llm_identity_db.refresh(connection)
    llm_identity_db.commit()
    transactions = _track_transactions(monkeypatch, llm_identity_db)
    service, events = _service(llm_identity_db)

    with pytest.raises(
        ProviderConfigurationError,
        match="Successful proving verification is required before enablement",
    ):
        service.enable_connection(
            expected_connection_revision=int(connection.revision),
            **common,
        )
    assert transactions == ["rollback"]
    assert events == []
    assert connection.state == LLMConnectionState.DRAFT.value


def test_enable_rolls_back_unenableable_state_without_rebinding(
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """An invalid lifecycle state rolls back once before observation rebinding."""

    owner, _ = identity_users
    connection, deployment, _ = _seed_proving(llm_identity_db, user_id=owner.id)
    service, _ = _service(llm_identity_db)
    service.test_connection(
        user_id=owner.id,
        preset_id=GPT_OSS_20B_PROVING_PRESET_ID,
        api_key=_SECRET,
        connection_id=str(connection.id),
        expected_connection_revision=int(connection.revision),
        deployment_id=str(deployment.id),
        expected_deployment_revision=int(deployment.revision),
    )
    transactions = _track_transactions(monkeypatch, llm_identity_db)
    rebinds: list[object] = []
    monkeypatch.setattr(
        service._connections,
        "get_owned_at_revision",
        lambda **_values: type(
            "InvalidStateConnection",
            (),
            {
                "id": connection.id,
                "user_id": owner.id,
                "revision": int(connection.revision),
                "state": "invalid",
            },
        )(),
    )
    monkeypatch.setattr(
        service._status,
        "proving_runnability",
        lambda **_values: RunnabilityOutcome(
            status="runnable",
            selectable=True,
            runnable=True,
        ),
    )
    monkeypatch.setattr(
        service._inventory,
        "rebind_proving_observation_revision",
        lambda **values: rebinds.append(values),
    )
    with pytest.raises(ProviderConfigurationError, match="not enableable"):
        service.enable_connection(
            user_id=owner.id,
            preset_id=GPT_OSS_20B_PROVING_PRESET_ID,
            connection_id=str(connection.id),
            expected_connection_revision=int(connection.revision),
            deployment_id=str(deployment.id),
            expected_deployment_revision=int(deployment.revision),
        )
    assert transactions == ["rollback"]
    assert rebinds == []
