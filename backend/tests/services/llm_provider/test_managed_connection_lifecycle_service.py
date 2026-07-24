"""Direct workflow tests for reviewed managed LLM connection lifecycles.

These tests prove transport-neutral orchestration, transaction ownership,
authorization/egress ordering, owner and revision checks, and secret confinement
without migrating the active FastAPI routes.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import (
    LLMConnectionCredential,
    LLMInferenceConnection,
    LLMModelDeployment,
    User,
)
from backend.services.llm_provider import credential_service as credential_module
from backend.services.llm_provider.application_contracts import (
    ConnectionStatusOutcome,
    VerificationOutcome,
)
from backend.services.llm_provider.connection_authorization import (
    LLMConnectionAuthorizer,
)
from backend.services.llm_provider.connection_service import LLMConnectionService
from backend.services.llm_provider.credential_service import LLMCredentialService
from backend.services.llm_provider.deployment_service import LLMDeploymentService
from backend.services.llm_provider.guarded_transport import GuardedTransportError
from backend.services.llm_provider.inventory_service import LLMInventoryService
from backend.services.llm_provider.managed_connection_lifecycle_service import (
    LLMManagedConnectionLifecycleService,
)
from backend.services.llm_provider.operation_registry import (
    CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
    GPT_OSS_20B_PROVING_PRESET_ID,
    HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
    ConnectionOperationRegistry,
)
from backend.services.llm_provider.types import (
    GuardedHTTPResponse,
    LLMAuthMode,
    LLMConnectionCredentialRef,
    LLMConnectionOperation,
    LLMConnectionState,
    LLMProviderServiceError,
    ProviderConfigurationError,
    ProviderSecret,
    RegisteredLLMOperationTarget,
)

_SECRET = "managed-direct-secret-must-not-appear"


class _RecordingAuthorizer:
    """Record authorization while delegating every policy decision."""

    def __init__(self, db: Session, events: list[tuple[str, object]]) -> None:
        self._delegate = LLMConnectionAuthorizer(db)
        self._events = events

    def authorize(self, **kwargs):
        self._events.append(("authorize", kwargs["operation"]))
        return self._delegate.authorize(**kwargs)


class _RecordingTransport:
    """Return one bounded response while recording guarded egress."""

    def __init__(
        self,
        events: list[tuple[str, object]],
        *,
        body: bytes = b"{}",
        failure: GuardedTransportError | None = None,
    ) -> None:
        self._events = events
        self._body = body
        self._failure = failure
        self.calls: list[dict[str, object]] = []

    def execute(self, operation, **kwargs):
        self._events.append(("transport", operation))
        self.calls.append({"operation": operation, **kwargs})
        if self._failure is not None:
            raise self._failure
        return GuardedHTTPResponse(
            status_code=200,
            body=self._body,
            audit_id=f"audit-{operation.value}",
        )


def _service(
    db: Session,
    *,
    body: bytes = b"{}",
    transport_failure: GuardedTransportError | None = None,
) -> tuple[LLMManagedConnectionLifecycleService, list[tuple[str, object]]]:
    events: list[tuple[str, object]] = []
    authorizer = _RecordingAuthorizer(db, events)
    inventory = LLMInventoryService(db, connection_authorizer=authorizer)
    return (
        LLMManagedConnectionLifecycleService(
            db,
            connection_authorizer=authorizer,
            guarded_transport=_RecordingTransport(
                events,
                body=body,
                failure=transport_failure,
            ),
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


def _seed_connection(
    db: Session,
    *,
    user_id: int,
    preset_id: str = HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    with_product_deployment: bool = False,
) -> tuple[LLMInferenceConnection, LLMModelDeployment | None]:
    preset = ConnectionOperationRegistry().get_connection_preset(preset_id)
    config: dict[str, Any] = {"auth_mode": "bearer"}
    if preset.endpoint_config_field is not None:
        config[preset.endpoint_config_field] = "https://llm.example.test/team"
    connection = LLMConnectionService(db).create_draft(
        user_id=user_id,
        display_name=preset.display_name,
        connection_preset_id=preset.id,
        runtime_family_id=preset.runtime_family_id,
        serving_operator_id=preset.serving_operator_id,
        non_secret_config=config,
    )
    LLMCredentialService(db).upsert_connection_api_key(
        user_id=user_id,
        connection_ref=LLMConnectionCredentialRef(
            connection_id=str(connection.id),
            expected_revision=int(connection.revision),
        ),
        provider=preset.id,
        api_key=_SECRET,
    )
    db.refresh(connection)
    deployment = None
    if with_product_deployment:
        deployment, _ = LLMDeploymentService(db).create_preset_deployment(
            user_id=user_id,
            connection_id=connection.id,
            expected_connection_revision=int(connection.revision),
            wire_model_id="openai/gpt-oss-20b",
            display_name="GPT-OSS 20B",
            canonical_model_id="openai/gpt-oss-20b",
        )
    db.commit()
    return connection, deployment


def _assert_authorized_immediately_before_egress(
    events: list[tuple[str, object]],
    operation: LLMConnectionOperation,
) -> None:
    assert any(
        events[index] == ("authorize", operation)
        and events[index + 1] == ("transport", operation)
        for index in range(len(events) - 1)
    ), events


def test_save_connection_commits_typed_status_after_complete_success(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Save stores configuration, credential, deployment, then commits status once."""

    owner, _ = identity_users
    transactions = _track_transactions(monkeypatch, llm_identity_db)
    service, _ = _service(llm_identity_db)

    outcome = service.save_connection(
        user_id=owner.id,
        preset_id=OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
        api_key=_SECRET,
        display_label="Team endpoint",
        base_url="https://llm.example.test/team",
        wire_model_id="team/chat-model",
        model_label="Team Chat",
        canonical_model_id="openai/gpt-oss-20b",
    )

    assert isinstance(outcome, ConnectionStatusOutcome)
    assert outcome.lifecycle_state == LLMConnectionState.DRAFT.value
    assert outcome.connection_ref.expected_revision == 2
    assert outcome.deployment_ref is not None
    assert outcome.deployment_ref.expected_revision == 1
    assert outcome.verification is not None
    assert outcome.verification.code == "not_tested"
    assert outcome.runnability is not None
    assert outcome.runnability.status == "connection_unavailable"
    assert transactions == ["commit"]
    connection = llm_identity_db.get(
        LLMInferenceConnection,
        outcome.connection_ref.connection_id,
    )
    deployment = llm_identity_db.get(
        LLMModelDeployment,
        outcome.deployment_ref.deployment_id,
    )
    assert connection is not None
    assert connection.non_secret_config == {"auth_mode": "bearer"}
    assert deployment is not None
    assert deployment.wire_model_id == "team/chat-model"
    assert _SECRET not in f"{outcome!r}\n{caplog.text}"


def test_save_connection_reuses_the_user_preset_singleton(
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Repeated connector saves rotate the credential without creating identity rows."""

    monkeypatch.setattr(
        credential_module,
        "encrypt_api_key",
        lambda value: f"encrypted:{value}",
    )
    monkeypatch.setattr(
        credential_module,
        "decrypt_api_key",
        lambda value: value.removeprefix("encrypted:"),
    )
    owner, _ = identity_users
    service, _ = _service(llm_identity_db)

    created = service.save_connection(
        user_id=owner.id,
        preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        api_key="first-placeholder-key",
        wire_model_id="openai/gpt-oss-20b",
        model_label="GPT-OSS 20B",
        canonical_model_id="openai/gpt-oss-20b",
    )
    updated = service.save_connection(
        user_id=owner.id,
        preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        api_key="replacement-placeholder-key",
        wire_model_id="openai/gpt-oss-20b",
        model_label="GPT-OSS 20B",
        canonical_model_id="openai/gpt-oss-20b",
    )

    assert updated.connection_ref.connection_id == created.connection_ref.connection_id
    assert updated.deployment_ref == created.deployment_ref
    assert len(
        llm_identity_db.execute(
            select(LLMInferenceConnection).where(
                LLMInferenceConnection.user_id == owner.id,
                LLMInferenceConnection.connection_preset_id
                == NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
            )
        ).scalars().all()
    ) == 1
    assert len(
        llm_identity_db.execute(
            select(LLMModelDeployment).where(
                LLMModelDeployment.connection_id
                == updated.connection_ref.connection_id,
            )
        ).scalars().all()
    ) == 1
    resolved = service._credentials.resolve_connection_auth(
        LLMConnectionCredentialRef(
            connection_id=updated.connection_ref.connection_id,
            expected_revision=updated.connection_ref.expected_revision,
        ),
        runtime_user_id=owner.id,
        purpose="test-connector-update",
        auth_mode=LLMAuthMode.BEARER,
    )
    assert resolved.secret is not None
    assert resolved.secret.value == "replacement-placeholder-key"


def test_disconnect_connection_revokes_credential_and_preserves_connector(
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Disconnect removes the secret while preserving reusable deployment identity."""

    owner, _ = identity_users
    connection, deployment = _seed_connection(
        llm_identity_db,
        user_id=owner.id,
        with_product_deployment=True,
    )
    assert deployment is not None
    connection_id = connection.id
    deployment_id = deployment.id
    connection = LLMConnectionService(llm_identity_db).transition_state(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=int(connection.revision),
        target_state=LLMConnectionState.DISABLED,
    )
    connection = LLMConnectionService(llm_identity_db).transition_state(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=int(connection.revision),
        target_state=LLMConnectionState.ENABLED,
    )
    llm_identity_db.commit()
    transactions = _track_transactions(monkeypatch, llm_identity_db)
    service, _ = _service(llm_identity_db)

    service.disconnect_connection(
        user_id=owner.id,
        preset_id=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
        connection_id=str(connection.id),
        expected_connection_revision=int(connection.revision),
    )

    assert transactions == ["commit"]
    preserved_connection = llm_identity_db.get(LLMInferenceConnection, connection_id)
    assert preserved_connection is not None
    assert preserved_connection.state == LLMConnectionState.DISABLED.value
    assert llm_identity_db.get(LLMModelDeployment, deployment_id) is not None
    assert llm_identity_db.get(LLMConnectionCredential, connection_id) is None


def test_save_connection_rolls_back_missing_secret_and_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Save rolls back validation and post-credential failures without partial rows."""

    owner, _ = identity_users
    transactions = _track_transactions(monkeypatch, llm_identity_db)
    service, _ = _service(llm_identity_db)
    with pytest.raises(
        ProviderConfigurationError,
        match="Connection API key is required",
    ):
        service.save_connection(
            user_id=owner.id,
            preset_id=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
            api_key=None,
            base_url="https://llm.example.test/team",
        )
    assert transactions == ["rollback"]

    transactions.clear()
    monkeypatch.setattr(
        service._inventory,
        "register_custom_model",
        lambda **_kwargs: (_ for _ in ()).throw(
            LLMProviderServiceError("deployment route failed")
        ),
    )
    with pytest.raises(LLMProviderServiceError, match="deployment route failed"):
        service.save_connection(
            user_id=owner.id,
            preset_id=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
            api_key=_SECRET,
            base_url="https://llm.example.test/team",
            wire_model_id="team/chat-model",
        )
    assert transactions == ["rollback"]
    assert llm_identity_db.execute(select(LLMInferenceConnection)).scalars().all() == []


def test_connection_health_and_product_probe_preserve_order_and_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Test chooses health or probe and authorizes immediately before egress."""

    owner, _ = identity_users
    health_connection, _ = _seed_connection(
        llm_identity_db,
        user_id=owner.id,
    )
    probe_connection, _ = _seed_connection(
        llm_identity_db,
        user_id=owner.id,
        preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        with_product_deployment=True,
    )
    transactions = _track_transactions(monkeypatch, llm_identity_db)
    service, events = _service(llm_identity_db)

    health = service.test_connection(
        user_id=owner.id,
        preset_id=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
        connection_id=str(health_connection.id),
        expected_connection_revision=int(health_connection.revision),
        api_key=_SECRET,
    )
    assert health == VerificationOutcome(
        status="passed",
        code="verified",
        message="Connection endpoint verified",
        retryable=False,
    )
    assert transactions == ["commit"]
    _assert_authorized_immediately_before_egress(
        events,
        LLMConnectionOperation.HEALTH,
    )

    transactions.clear()
    events.clear()
    probe = service.test_connection(
        user_id=owner.id,
        preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        connection_id=str(probe_connection.id),
        expected_connection_revision=int(probe_connection.revision),
    )
    assert probe.message == "GPT-OSS 20B is ready"
    assert transactions == ["commit"]
    _assert_authorized_immediately_before_egress(
        events,
        LLMConnectionOperation.CAPABILITY_PROBE,
    )
    assert _SECRET not in f"{health!r}\n{probe!r}\n{caplog.text}"


def test_connector_update_keeps_endpoint_and_secret_bound_to_one_identity(
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Updating a connector replaces its endpoint and secret without a duplicate."""

    monkeypatch.setattr(
        credential_module,
        "encrypt_api_key",
        lambda value: f"encrypted:{value}",
    )
    monkeypatch.setattr(
        credential_module,
        "decrypt_api_key",
        lambda value: value.removeprefix("encrypted:"),
    )
    owner, _ = identity_users
    service, _ = _service(llm_identity_db)
    connection_a = service.save_connection(
        user_id=owner.id,
        preset_id=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
        api_key="key-a-placeholder",
        display_label="Endpoint A",
        base_url="https://a.example.test",
    )
    connection_b = service.save_connection(
        user_id=owner.id,
        preset_id=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
        api_key="key-b-placeholder",
        display_label="Endpoint B",
        base_url="https://b.example.test",
    )

    service.test_connection(
        user_id=owner.id,
        preset_id=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
        connection_id=connection_b.connection_ref.connection_id,
        expected_connection_revision=connection_b.connection_ref.expected_revision,
    )

    assert (
        connection_b.connection_ref.connection_id
        == connection_a.connection_ref.connection_id
    )
    transport = service._transport
    assert isinstance(transport, _RecordingTransport)
    call = transport.calls[-1]
    secret = call["secret"]
    target = call["operation_target"]
    assert isinstance(secret, ProviderSecret)
    assert isinstance(target, RegisteredLLMOperationTarget)
    assert secret.value == "key-b-placeholder"
    assert target.url == "https://b.example.test/v1/models"


@pytest.mark.parametrize("workflow", ("test", "refresh", "enable"))
def test_ref_workflows_reject_stale_foreign_and_preset_mismatch_before_egress(
    workflow: str,
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Ref-bearing workflows validate owner/revision/preset before mutation or egress."""

    owner, other = identity_users
    connection, deployment = _seed_connection(
        llm_identity_db,
        user_id=owner.id,
        preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        with_product_deployment=True,
    )
    foreign, _ = _seed_connection(
        llm_identity_db,
        user_id=other.id,
        preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    )
    transactions = _track_transactions(monkeypatch, llm_identity_db)
    service, events = _service(llm_identity_db)

    def invoke(connection_id: str, revision: int, preset_id: str) -> object:
        if workflow == "test":
            return service.test_connection(
                user_id=owner.id,
                preset_id=preset_id,
                connection_id=connection_id,
                expected_connection_revision=revision,
                api_key=_SECRET,
            )
        if workflow == "refresh":
            return service.refresh_inventory(
                user_id=owner.id,
                preset_id=preset_id,
                connection_id=connection_id,
                expected_connection_revision=revision,
                api_key=_SECRET,
            )
        return service.enable_connection(
            user_id=owner.id,
            preset_id=preset_id,
            connection_id=connection_id,
            expected_connection_revision=revision,
            deployment_id=str(deployment.id) if deployment is not None else None,
            expected_deployment_revision=(
                int(deployment.revision) if deployment is not None else None
            ),
        )

    with pytest.raises(LLMProviderServiceError, match="revision"):
        invoke(
            str(connection.id),
            int(connection.revision) + 100,
            NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        )
    with pytest.raises(LLMProviderServiceError, match="Connection was not found"):
        invoke(
            str(foreign.id),
            int(foreign.revision),
            NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        )
    with pytest.raises(ProviderConfigurationError, match="Connection preset mismatch"):
        invoke(
            str(connection.id),
            int(connection.revision),
            HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
        )
    assert transactions == ["rollback", "rollback", "rollback"]
    assert events == []
    assert connection.state == LLMConnectionState.DRAFT.value


def test_refresh_inventory_uses_canonical_parser_and_commits_single_status(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Refresh authorizes, executes, parses, persists, selects, then commits."""

    owner, _ = identity_users
    connection, _ = _seed_connection(
        llm_identity_db,
        user_id=owner.id,
        preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    )
    body = (
        b'{"data":[{"id":"openai/gpt-oss-20b"},'
        b'{"id":"provider/unrelated"}]}'
    )
    transactions = _track_transactions(monkeypatch, llm_identity_db)
    service, events = _service(llm_identity_db, body=body)
    parser_calls: list[bytes] = []
    original_parser = LLMInventoryService.parse_inventory_model_ids

    def recording_parser(value: bytes) -> tuple[str, ...]:
        parser_calls.append(value)
        return original_parser(value)

    monkeypatch.setattr(
        service._inventory,
        "parse_inventory_model_ids",
        recording_parser,
    )
    outcome = service.refresh_inventory(
        user_id=owner.id,
        preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        connection_id=str(connection.id),
        expected_connection_revision=int(connection.revision),
    )

    assert isinstance(outcome, ConnectionStatusOutcome)
    assert outcome.deployment_ref is not None
    assert outcome.runnability is not None
    assert outcome.runnability.status == "capability_unknown"
    assert parser_calls == [body]
    assert transactions == ["commit"]
    _assert_authorized_immediately_before_egress(
        events,
        LLMConnectionOperation.INVENTORY,
    )
    assert _SECRET not in f"{outcome!r}\n{caplog.text}"


@pytest.mark.parametrize(
    ("body", "detail"),
    (
        (b'{"data":[]}', "Provider inventory response did not include models"),
        (b'{"data":{}}', "Provider inventory response is invalid"),
    ),
)
def test_refresh_inventory_rolls_back_parser_failures_without_partial_rows(
    body: bytes,
    detail: str,
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Refresh rolls back canonical parser failures after guarded egress."""

    owner, _ = identity_users
    connection, _ = _seed_connection(
        llm_identity_db,
        user_id=owner.id,
        preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    )
    transactions = _track_transactions(monkeypatch, llm_identity_db)
    service, events = _service(llm_identity_db, body=body)
    with pytest.raises(ProviderConfigurationError, match=detail):
        service.refresh_inventory(
            user_id=owner.id,
            preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
            connection_id=str(connection.id),
            expected_connection_revision=int(connection.revision),
        )
    assert transactions == ["rollback"]
    assert llm_identity_db.execute(select(LLMModelDeployment)).scalars().all() == []
    _assert_authorized_immediately_before_egress(
        events,
        LLMConnectionOperation.INVENTORY,
    )


def test_refresh_transport_failure_rolls_back_and_preserves_sanitized_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """A sanitized transport failure rolls back once without secret disclosure."""

    owner, _ = identity_users
    connection, _ = _seed_connection(
        llm_identity_db,
        user_id=owner.id,
        preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    )
    failure = GuardedTransportError(
        "safe guarded failure",
        audit_id="audit-refresh-failure",
    )
    transactions = _track_transactions(monkeypatch, llm_identity_db)
    service, events = _service(llm_identity_db, transport_failure=failure)
    with pytest.raises(GuardedTransportError) as caught:
        service.refresh_inventory(
            user_id=owner.id,
            preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
            connection_id=str(connection.id),
            expected_connection_revision=int(connection.revision),
        )
    assert caught.value is failure
    assert transactions == ["rollback"]
    _assert_authorized_immediately_before_egress(
        events,
        LLMConnectionOperation.INVENTORY,
    )
    assert _SECRET not in f"{caught.value!s}\n{caught.value!r}\n{caplog.text}"


def test_invalid_key_transport_failure_reuses_provider_health_error(
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Managed connection checks expose the same clean invalid-key error as providers."""

    owner, _ = identity_users
    connection, _ = _seed_connection(
        llm_identity_db,
        user_id=owner.id,
        preset_id=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    )
    failure = GuardedTransportError(
        "Guarded upstream response rejected",
        audit_id="managed-invalid-key",
        status_code=401,
    )
    transactions = _track_transactions(monkeypatch, llm_identity_db)
    service, _ = _service(llm_identity_db, transport_failure=failure)

    with pytest.raises(
        ProviderConfigurationError,
        match="^Invalid Hugging Face API key$",
    ):
        service.test_connection(
            user_id=owner.id,
            preset_id=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
            connection_id=str(connection.id),
            expected_connection_revision=int(connection.revision),
        )

    assert transactions == ["rollback"]


def test_enable_connection_transitions_and_validates_deployment_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Enable validates deployment binding, transitions twice, and commits status."""

    owner, _ = identity_users
    connection, deployment = _seed_connection(
        llm_identity_db,
        user_id=owner.id,
        preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        with_product_deployment=True,
    )
    assert deployment is not None
    transactions = _track_transactions(monkeypatch, llm_identity_db)
    service, _ = _service(llm_identity_db)
    outcome = service.enable_connection(
        user_id=owner.id,
        preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        connection_id=str(connection.id),
        expected_connection_revision=int(connection.revision),
        deployment_id=str(deployment.id),
        expected_deployment_revision=int(deployment.revision),
    )
    assert outcome.lifecycle_state == LLMConnectionState.ENABLED.value
    assert outcome.connection_ref.expected_revision == 4
    assert transactions == ["commit"]

    other, other_deployment = _seed_connection(
        llm_identity_db,
        user_id=owner.id,
        with_product_deployment=True,
    )
    assert other_deployment is not None
    transactions.clear()
    with pytest.raises(ProviderConfigurationError, match="Deployment revision is stale"):
        service.enable_connection(
            user_id=owner.id,
            preset_id=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
            connection_id=str(other.id),
            expected_connection_revision=int(other.revision),
            deployment_id=str(other_deployment.id),
            expected_deployment_revision=int(other_deployment.revision) + 100,
        )
    with pytest.raises(
        ProviderConfigurationError,
        match="Deployment connection mismatch",
    ):
        service.enable_connection(
            user_id=owner.id,
            preset_id=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
            connection_id=str(other.id),
            expected_connection_revision=int(other.revision),
            deployment_id=str(deployment.id),
            expected_deployment_revision=int(deployment.revision),
        )
    assert transactions == ["rollback", "rollback"]
    assert other.state == LLMConnectionState.DRAFT.value


@pytest.mark.parametrize(
    "method",
    ("save_connection", "test_connection", "refresh_inventory", "enable_connection"),
)
def test_managed_workflows_reject_proving_preset_and_roll_back(
    method: str,
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Every managed entry point rejects the proving preset before side effects."""

    owner, _ = identity_users
    transactions = _track_transactions(monkeypatch, llm_identity_db)
    service, events = _service(llm_identity_db)
    kwargs: dict[str, object] = {
        "user_id": owner.id,
        "preset_id": GPT_OSS_20B_PROVING_PRESET_ID,
    }
    if method == "save_connection":
        kwargs["api_key"] = _SECRET
    else:
        kwargs.update(
            connection_id="00000000-0000-0000-0000-000000000001",
            expected_connection_revision=1,
        )
    with pytest.raises(
        ProviderConfigurationError,
        match="Use proving preset routes for GPT-OSS proving",
    ):
        getattr(service, method)(**kwargs)
    assert transactions == ["rollback"]
    assert events == []
