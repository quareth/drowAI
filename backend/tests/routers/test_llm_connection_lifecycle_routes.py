"""Focused managed and proving connection lifecycle route contracts.

These tests exercise the seven wired HTTP adapters, delegated application
workflows, persistence authorities, authorization boundary, transaction
effects, and sanitized response contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session as SQLAlchemySession

from agent.providers.llm.core.capabilities import LLMCapability
from backend.database import SessionLocal
from backend.models import (
    LLMCapabilityObservation,
    LLMConnectionCredential,
    LLMInferenceConnection,
    LLMModelDeployment,
)
from backend.tests.routers.llm_route_test_support import (
    create_client as _client,
    create_user as _user,
)
from backend.services.llm_provider import (
    managed_connection_lifecycle_service as managed_lifecycle_module,
)
from backend.services.llm_provider import (
    LLMCredentialService,
    LLMConnectionAuthorizer,
    LLMConnectionService,
    LLMDeploymentService,
    LLMManagedConnectionLifecycleService,
    LLMProvingConnectionLifecycleService,
    LLMProviderServiceError,
)
from backend.services.llm_provider.application_contracts import (
    ConnectionRefOutcome,
    ConnectionStatusOutcome,
    DeploymentRefOutcome,
    RunnabilityOutcome,
    VerificationOutcome,
)
from backend.services.llm_provider.guarded_transport import GuardedTransportError
from backend.services.llm_provider.operation_registry import (
    CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
    GPT_OSS_20B_PROVING_BASE_URL_ENV,
    GPT_OSS_20B_PROVING_PRESET_ID,
    HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
)
from backend.services.llm_provider.types import (
    GuardedHTTPResponse,
    LLMConnectionCredentialRef,
    LLMConnectionOperation,
    LLMConnectionState,
)


_MANAGED_SECRET = "hf-lifecycle-secret"
_PROVING_SECRET = "prove-lifecycle-secret"


def _assert_secret_absent(
    secret: str,
    *texts: str,
    exceptions: list[BaseException] | None = None,
) -> None:
    observed_texts = list(texts)
    for exc in exceptions or []:
        observed_texts.extend([str(exc), repr(exc)])
    for text in observed_texts:
        assert secret not in text
        assert "encrypted_api_key" not in text


@dataclass
class TxCounter:
    """Count request-local transaction calls while preserving real behavior."""

    active: bool = False
    commits: int = 0
    rollbacks: int = 0

    def start(self) -> None:
        self.active = True
        self.commits = 0
        self.rollbacks = 0

    def stop(self) -> None:
        self.active = False


def _patch_transaction_counter(monkeypatch) -> TxCounter:
    counter = TxCounter()
    original_commit = SQLAlchemySession.commit
    original_rollback = SQLAlchemySession.rollback

    def counted_commit(self):
        if counter.active:
            counter.commits += 1
        return original_commit(self)

    def counted_rollback(self):
        if counter.active:
            counter.rollbacks += 1
        return original_rollback(self)

    monkeypatch.setattr(SQLAlchemySession, "commit", counted_commit)
    monkeypatch.setattr(SQLAlchemySession, "rollback", counted_rollback)
    return counter


def _during_request(counter: TxCounter, fn):
    counter.start()
    try:
        return fn()
    finally:
        counter.stop()


def _connection_ref(connection) -> dict[str, Any]:
    return {
        "connection_id": str(connection.id),
        "expected_revision": int(connection.revision),
    }


def _deployment_ref(deployment) -> dict[str, Any]:
    return {
        "deployment_id": str(deployment.id),
        "expected_revision": int(deployment.revision),
    }


def _managed_connection(
    *,
    user_id: int,
    preset_id: str = HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    secret: str = _MANAGED_SECRET,
    with_product_deployment: bool = False,
):
    db = SessionLocal()
    try:
        preset_meta = {
            HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID: (
                "Hugging Face",
                "openai_compatible_chat",
                "huggingface",
            ),
            NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID: (
                "NVIDIA NIM",
                "openai_compatible_chat",
                "nvidia_nim",
            ),
            CUSTOM_OPENAI_COMPATIBLE_PRESET_ID: (
                "Custom endpoint",
                "openai_compatible_chat",
                "organization_managed",
            ),
        }[preset_id]
        non_secret_config = {"auth_mode": "bearer"}
        if preset_id == CUSTOM_OPENAI_COMPATIBLE_PRESET_ID:
            non_secret_config["base_url"] = "https://llm.example.test/team"
        connection = LLMConnectionService(db).create_draft(
            user_id=user_id,
            display_name=preset_meta[0],
            connection_preset_id=preset_id,
            runtime_family_id=preset_meta[1],
            serving_operator_id=preset_meta[2],
            non_secret_config=non_secret_config,
        )
        LLMCredentialService(db).upsert_connection_api_key(
            user_id=user_id,
            connection_ref=LLMConnectionCredentialRef(
                connection_id=str(connection.id),
                expected_revision=int(connection.revision),
            ),
            provider=preset_id,
            api_key=secret,
        )
        db.refresh(connection)
        deployment = None
        route = None
        if with_product_deployment:
            deployment, route = LLMDeploymentService(db).create_preset_deployment(
                user_id=user_id,
                connection_id=connection.id,
                expected_connection_revision=int(connection.revision),
                wire_model_id="openai/gpt-oss-20b",
                display_name="GPT-OSS 20B",
                canonical_model_id="openai/gpt-oss-20b",
            )
        connection_ref = _connection_ref(connection)
        deployment_ref = _deployment_ref(deployment) if deployment is not None else None
        route_id = route.id if route is not None else None
        db.commit()
        return connection_ref, deployment_ref, route_id
    finally:
        db.close()


def _proving_connection(*, user_id: int, secret: str = _PROVING_SECRET):
    db = SessionLocal()
    try:
        connection = LLMConnectionService(db).create_gpt_oss_20b_proving_draft(
            user_id=user_id,
            display_label="Proof endpoint",
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
        deployment, route = LLMDeploymentService(db).create_gpt_oss_20b_proving_deployment(
            user_id=user_id,
            connection_id=connection.id,
            expected_connection_revision=int(connection.revision),
        )
        result = (
            _connection_ref(connection),
            _deployment_ref(deployment),
            route.id,
        )
        db.commit()
        return result
    finally:
        db.close()


def _count_connections(user_id: int, preset_id: str) -> int:
    db = SessionLocal()
    try:
        return (
            db.query(LLMInferenceConnection)
            .filter(
                LLMInferenceConnection.user_id == user_id,
                LLMInferenceConnection.connection_preset_id == preset_id,
            )
            .count()
        )
    finally:
        db.close()


def _connection_row(connection_ref: dict[str, Any]):
    db = SessionLocal()
    try:
        row = db.get(LLMInferenceConnection, UUID(connection_ref["connection_id"]))
        db.expunge(row)
        return row
    finally:
        db.close()


def _deployment_count_for_connection(connection_ref: dict[str, Any]) -> int:
    db = SessionLocal()
    try:
        return (
            db.query(LLMModelDeployment)
            .filter(
                LLMModelDeployment.connection_id
                == UUID(connection_ref["connection_id"])
            )
            .count()
        )
    finally:
        db.close()


def _recording_authorizer(monkeypatch, events: list[tuple[str, Any]]) -> None:
    original_authorize = LLMConnectionAuthorizer.authorize

    def recording_authorize(self, **kwargs):
        events.append(("authorize", kwargs["operation"]))
        return original_authorize(self, **kwargs)

    monkeypatch.setattr(LLMConnectionAuthorizer, "authorize", recording_authorize)


def _assert_authorize_immediately_before_transport(
    events: list[tuple[str, Any]],
    operation: LLMConnectionOperation,
) -> None:
    assert any(
        events[index][0:2] == ("authorize", operation)
        and events[index + 1][0:2] == ("transport", operation)
        for index in range(len(events) - 1)
    ), events


class _ManagedTransport:
    def __init__(self, events: list[tuple[str, Any]], *, body: bytes = b"{}") -> None:
        self._events = events
        self._body = body

    def execute(self, operation, **kwargs):
        self._events.append(("transport", operation, kwargs))
        return GuardedHTTPResponse(
            status_code=200,
            body=self._body,
            audit_id=f"audit-{operation.value}",
        )


def _patch_managed_transport(monkeypatch, events: list[tuple[str, Any]], *, body: bytes = b"{}") -> None:
    class RecordingGuardedTransport:
        def __init__(self, **_kwargs) -> None:
            pass

        def execute(self, operation, **kwargs):
            return _ManagedTransport(events, body=body).execute(operation, **kwargs)

    monkeypatch.setattr(
        managed_lifecycle_module,
        "GuardedTransport",
        RecordingGuardedTransport,
    )


def _patch_proving_transport(monkeypatch, events: list[tuple[str, Any]]) -> None:
    import backend.services.llm_provider.health_service as health_service

    monkeypatch.setenv(
        GPT_OSS_20B_PROVING_BASE_URL_ENV,
        "http://127.0.0.1:4000/v1",
    )

    class RecordingGuardedTransport:
        def execute(self, operation, **kwargs):
            events.append(("transport", operation, kwargs))
            if operation == LLMConnectionOperation.INVENTORY:
                body = b'{"data":[{"id":"openai/gpt-oss-20b"}]}'
            elif operation == LLMConnectionOperation.CAPABILITY_PROBE:
                body = (
                    b'{"usage":{"prompt_tokens":1,'
                    b'"completion_tokens":1,"total_tokens":2}}'
                )
            else:
                body = b"{}"
            return GuardedHTTPResponse(
                status_code=200,
                body=body,
                audit_id=f"audit-{operation.value}",
            )

    monkeypatch.setattr(health_service, "GuardedTransport", RecordingGuardedTransport)


def test_managed_routes_delegate_once_with_exact_request_adaptation(monkeypatch) -> None:
    """The four managed HTTP adapters pass only explicit primitive workflow inputs."""

    user = _user("llm-managed-adapters")
    connection_id = str(uuid4())
    deployment_id = str(uuid4())
    calls: list[tuple[str, dict[str, object]]] = []
    status_outcome = ConnectionStatusOutcome(
        lifecycle_state="draft",
        connection_ref=ConnectionRefOutcome(
            connection_id=connection_id,
            expected_revision=2,
        ),
        deployment_ref=DeploymentRefOutcome(
            deployment_id=deployment_id,
            expected_revision=1,
        ),
        verification=VerificationOutcome(
            status="failed",
            code="not_tested",
            message="Verification has not run.",
            retryable=False,
        ),
        runnability=RunnabilityOutcome(
            status="capability_unknown",
            selectable=True,
            runnable=False,
            reason="Capability evidence is required.",
        ),
    )
    verification_outcome = VerificationOutcome(
        status="passed",
        code="verified",
        message="Connection endpoint verified",
        retryable=False,
    )

    def record(name: str, outcome):
        def invoke(_self, **kwargs):
            calls.append((name, kwargs))
            return outcome

        return invoke

    monkeypatch.setattr(
        LLMManagedConnectionLifecycleService,
        "save_connection",
        record("save", status_outcome),
    )
    monkeypatch.setattr(
        LLMManagedConnectionLifecycleService,
        "test_connection",
        record("test", verification_outcome),
    )
    monkeypatch.setattr(
        LLMManagedConnectionLifecycleService,
        "refresh_inventory",
        record("refresh", status_outcome),
    )
    monkeypatch.setattr(
        LLMManagedConnectionLifecycleService,
        "enable_connection",
        record("enable", status_outcome),
    )

    client, app = _client(user)
    try:
        created = client.put(
            f"/api/llm/connection-presets/{CUSTOM_OPENAI_COMPATIBLE_PRESET_ID}/connection",
            json={
                "api_key": _MANAGED_SECRET,
                "display_label": "Team endpoint",
                "base_url": "https://llm.example.test/team",
                "wire_model_id": "team/chat-model",
                "model_label": "Team Chat",
                "canonical_model_id": CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
            },
        )
        tested = client.post(
            f"/api/llm/connection-presets/{HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID}/connection/test",
            json={
                "api_key": _MANAGED_SECRET,
                "connection_ref": {
                    "connection_id": connection_id,
                    "expected_revision": 2,
                },
            },
        )
        refreshed = client.post(
            f"/api/llm/connection-presets/{HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID}/connection/refresh",
            json={
                "connection_ref": {
                    "connection_id": connection_id,
                    "expected_revision": 2,
                },
            },
        )
        enabled = client.post(
            f"/api/llm/connection-presets/{HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID}/connection/enable",
            json={
                "connection_ref": {
                    "connection_id": connection_id,
                    "expected_revision": 2,
                },
                "deployment_ref": {
                    "deployment_id": deployment_id,
                    "expected_revision": 1,
                },
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert [response.status_code for response in (created, tested, refreshed, enabled)] == [
        200,
        200,
        200,
        200,
    ]
    assert calls == [
        (
            "save",
            {
                "user_id": user.id,
                "preset_id": CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
                "api_key": _MANAGED_SECRET,
                "connection_id": None,
                "expected_connection_revision": None,
                "display_label": "Team endpoint",
                "base_url": "https://llm.example.test/team",
                "wire_model_id": "team/chat-model",
                "model_label": "Team Chat",
                "canonical_model_id": CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
            },
        ),
        (
            "test",
            {
                "user_id": user.id,
                "preset_id": HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
                "connection_id": connection_id,
                "expected_connection_revision": 2,
                "api_key": _MANAGED_SECRET,
            },
        ),
        (
            "refresh",
            {
                "user_id": user.id,
                "preset_id": HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
                "connection_id": connection_id,
                "expected_connection_revision": 2,
                "api_key": None,
            },
        ),
        (
            "enable",
            {
                "user_id": user.id,
                "preset_id": HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
                "connection_id": connection_id,
                "expected_connection_revision": 2,
                "deployment_id": deployment_id,
                "expected_deployment_revision": 1,
            },
        ),
    ]


def test_proving_routes_delegate_once_with_exact_request_adaptation(monkeypatch) -> None:
    """The three proving HTTP adapters pass only explicit primitive workflow inputs."""

    user = _user("llm-proving-adapters")
    connection_id = str(uuid4())
    deployment_id = str(uuid4())
    calls: list[tuple[str, dict[str, object]]] = []
    status_outcome = ConnectionStatusOutcome(
        lifecycle_state="draft",
        connection_ref=ConnectionRefOutcome(
            connection_id=connection_id,
            expected_revision=2,
        ),
        deployment_ref=DeploymentRefOutcome(
            deployment_id=deployment_id,
            expected_revision=1,
        ),
        verification=VerificationOutcome(
            status="failed",
            code="not_tested",
            message="Verification has not run.",
            retryable=False,
        ),
        runnability=RunnabilityOutcome(
            status="capability_unknown",
            selectable=True,
            runnable=False,
            reason="Usage evidence is required.",
        ),
    )
    verification_outcome = VerificationOutcome(
        status="passed",
        code="verified",
        message="GPT-OSS proving endpoint verified",
        retryable=False,
        model_present=True,
    )

    def record(name: str, outcome):
        def invoke(_self, **kwargs):
            calls.append((name, kwargs))
            return outcome

        return invoke

    monkeypatch.setattr(
        LLMProvingConnectionLifecycleService,
        "create_connection",
        record("create", status_outcome),
    )
    monkeypatch.setattr(
        LLMProvingConnectionLifecycleService,
        "test_connection",
        record("test", verification_outcome),
    )
    monkeypatch.setattr(
        LLMProvingConnectionLifecycleService,
        "enable_connection",
        record("enable", status_outcome),
    )
    counter = _patch_transaction_counter(monkeypatch)
    client, app = _client(user)
    try:
        created = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection",
                json={
                    "api_key": _PROVING_SECRET,
                    "display_label": "Proof endpoint",
                },
            ),
        )
        assert counter.commits == 0
        assert counter.rollbacks == 0
        tested = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection/test",
                json={
                    "api_key": _PROVING_SECRET,
                    "connection_ref": {
                        "connection_id": connection_id,
                        "expected_revision": 2,
                    },
                    "deployment_ref": {
                        "deployment_id": deployment_id,
                        "expected_revision": 1,
                    },
                },
            ),
        )
        assert counter.commits == 0
        assert counter.rollbacks == 0
        enabled = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection/enable",
                json={
                    "connection_ref": {
                        "connection_id": connection_id,
                        "expected_revision": 2,
                    },
                    "deployment_ref": {
                        "deployment_id": deployment_id,
                        "expected_revision": 1,
                    },
                },
            ),
        )
        assert counter.commits == 0
        assert counter.rollbacks == 0
    finally:
        app.dependency_overrides.clear()

    assert [response.status_code for response in (created, tested, enabled)] == [
        200,
        200,
        200,
    ]
    assert calls == [
        (
            "create",
            {
                "user_id": user.id,
                "preset_id": GPT_OSS_20B_PROVING_PRESET_ID,
                "api_key": _PROVING_SECRET,
                "display_label": "Proof endpoint",
            },
        ),
        (
            "test",
            {
                "user_id": user.id,
                "preset_id": GPT_OSS_20B_PROVING_PRESET_ID,
                "api_key": _PROVING_SECRET,
                "connection_id": connection_id,
                "expected_connection_revision": 2,
                "deployment_id": deployment_id,
                "expected_deployment_revision": 1,
            },
        ),
        (
            "enable",
            {
                "user_id": user.id,
                "preset_id": GPT_OSS_20B_PROVING_PRESET_ID,
                "connection_id": connection_id,
                "expected_connection_revision": 2,
                "deployment_id": deployment_id,
                "expected_deployment_revision": 1,
            },
        ),
    ]


def test_managed_create_success_and_missing_secret_failure(monkeypatch, caplog) -> None:
    """Managed create stores supplied secret, preset config, deployment, and transaction outcome."""

    counter = _patch_transaction_counter(monkeypatch)
    user = _user("llm-managed-create")
    client, app = _client(user)
    try:
        created = _during_request(
            counter,
            lambda: client.put(
                f"/api/llm/connection-presets/{OLLAMA_OPENAI_COMPATIBLE_PRESET_ID}/connection",
                json={
                    "api_key": _MANAGED_SECRET,
                    "display_label": "Team endpoint",
                    "base_url": "https://llm.example.test/team",
                    "wire_model_id": "team/chat-model",
                    "model_label": "Team Chat",
                    "canonical_model_id": "openai/gpt-oss-20b",
                },
            ),
        )
        assert created.status_code == 200, created.text
        assert counter.commits == 1
        assert counter.rollbacks == 0
        payload = created.json()
        assert payload["lifecycle_state"] == "draft"
        assert payload["connection_ref"]["expected_revision"] == 2
        assert payload["deployment_ref"]["expected_revision"] == 1
        assert payload["verification"]["code"] == "not_tested"
        assert payload["runnability"]["status"] == "connection_unavailable"
        _assert_secret_absent(_MANAGED_SECRET, created.text, caplog.text)

        db = SessionLocal()
        try:
            connection = db.get(
                LLMInferenceConnection,
                UUID(payload["connection_ref"]["connection_id"]),
            )
            deployment = db.get(
                LLMModelDeployment,
                UUID(payload["deployment_ref"]["deployment_id"]),
            )
            credential = (
                db.query(LLMConnectionCredential)
                .filter(
                    LLMConnectionCredential.connection_id == connection.id,
                )
                .one()
            )
            assert connection.non_secret_config == {"auth_mode": "bearer"}
            assert deployment.wire_model_id == "team/chat-model"
            assert credential.has_api_key is True
            _assert_secret_absent(
                _MANAGED_SECRET,
                json.dumps(connection.non_secret_config),
                caplog.text,
            )
        finally:
            db.close()

        replacement_secret = f"{_MANAGED_SECRET}-replacement"
        updated = _during_request(
            counter,
            lambda: client.put(
                f"/api/llm/connection-presets/{OLLAMA_OPENAI_COMPATIBLE_PRESET_ID}/connection",
                json={
                    "api_key": replacement_secret,
                    "connection_ref": payload["connection_ref"],
                    "display_label": "Updated team endpoint",
                    "base_url": "https://llm.example.test/team",
                    "wire_model_id": "team/chat-model",
                    "model_label": "Team Chat",
                    "canonical_model_id": "openai/gpt-oss-20b",
                },
            ),
        )
        assert updated.status_code == 200, updated.text
        assert counter.commits == 1
        assert counter.rollbacks == 0
        assert (
            updated.json()["connection_ref"]["connection_id"]
            == payload["connection_ref"]["connection_id"]
        )
        assert updated.json()["deployment_ref"] == payload["deployment_ref"]
        assert _count_connections(user.id, OLLAMA_OPENAI_COMPATIBLE_PRESET_ID) == 1
        _assert_secret_absent(replacement_secret, updated.text, caplog.text)

        missing = _during_request(
            counter,
            lambda: client.put(
                f"/api/llm/connection-presets/{OLLAMA_OPENAI_COMPATIBLE_PRESET_ID}/connection",
                json={"base_url": "https://llm.example.test/team"},
            ),
        )
        assert missing.status_code == 400, missing.text
        assert missing.json() == {"detail": "Connection API key is required"}
        assert counter.commits == 0
        assert counter.rollbacks == 1
        assert _count_connections(user.id, OLLAMA_OPENAI_COMPATIBLE_PRESET_ID) == 1
    finally:
        app.dependency_overrides.clear()


def test_managed_disconnect_revokes_credential_and_preserves_connector(caplog) -> None:
    """Disconnect uses the shared credential state without deleting route identity."""

    user = _user("llm-managed-disconnect")
    connection_ref, _deployment_ref_value, _route_id = _managed_connection(
        user_id=user.id,
        with_product_deployment=True,
    )
    connection_id = UUID(connection_ref["connection_id"])
    client, app = _client(user)
    try:
        before = client.get("/api/llm/models")
        assert before.status_code == 200, before.text
        before_provider = next(
            provider
            for provider in before.json()["providers"]
            if provider["id"] == HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID
        )
        assert before_provider["credential"]["has_api_key"] is True

        disconnected = client.request(
            "DELETE",
            (
                "/api/llm/connection-presets/"
                f"{HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID}/connection"
            ),
            json={"connection_ref": connection_ref},
        )
        assert disconnected.status_code == 200, disconnected.text
        assert disconnected.json() == {"success": True}
        _assert_secret_absent(_MANAGED_SECRET, disconnected.text, caplog.text)

        after = client.get("/api/llm/models")
        assert after.status_code == 200, after.text
        after_provider = next(
            provider
            for provider in after.json()["providers"]
            if provider["id"] == HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID
        )
        assert after_provider["credential"]["has_api_key"] is False
        assert after_provider["credential"]["enabled"] is False
    finally:
        app.dependency_overrides.clear()

    db = SessionLocal()
    try:
        assert db.get(LLMInferenceConnection, connection_id) is not None
        assert (
            db.query(LLMConnectionCredential)
            .filter(LLMConnectionCredential.connection_id == connection_id)
            .one_or_none()
            is None
        )
        assert (
            db.query(LLMModelDeployment)
            .filter(LLMModelDeployment.connection_id == connection_id)
            .one_or_none()
            is not None
        )
    finally:
        db.close()


def test_managed_test_health_and_product_probe_ordering(monkeypatch) -> None:
    """Managed test authorizes immediately before health or capability egress."""

    counter = _patch_transaction_counter(monkeypatch)
    events: list[tuple[str, Any]] = []
    _recording_authorizer(monkeypatch, events)
    _patch_managed_transport(monkeypatch, events)
    user = _user("llm-managed-test")
    health_ref, _deployment_ref_value, _route_id = _managed_connection(user_id=user.id)
    probe_ref, probe_deployment_ref, _route_id = _managed_connection(
        user_id=user.id,
        preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        with_product_deployment=True,
    )
    client, app = _client(user)
    try:
        health = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/connection-presets/{HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID}/connection/test",
                json={"api_key": _MANAGED_SECRET, "connection_ref": health_ref},
            ),
        )
        assert health.status_code == 200, health.text
        assert health.json()["message"] == "Connection endpoint verified"
        assert counter.commits == 1
        assert counter.rollbacks == 0
        _assert_authorize_immediately_before_transport(
            events,
            LLMConnectionOperation.HEALTH,
        )

        events.clear()
        probe = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/connection-presets/{NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID}/connection/test",
                json={"connection_ref": probe_ref},
            ),
        )
        assert probe.status_code == 200, probe.text
        assert probe.json()["message"] == "GPT-OSS 20B is ready"
        assert counter.commits == 1
        assert counter.rollbacks == 0
        _assert_authorize_immediately_before_transport(
            events,
            LLMConnectionOperation.CAPABILITY_PROBE,
        )
        transport_event = next(
            event
            for event in events
            if event[0:2] == ("transport", LLMConnectionOperation.CAPABILITY_PROBE)
        )
        assert transport_event[2]["json_body"]["model"] == "openai/gpt-oss-20b"
        assert probe_deployment_ref is not None
        assert _MANAGED_SECRET not in health.text
        assert _MANAGED_SECRET not in probe.text
    finally:
        app.dependency_overrides.clear()


def test_managed_test_stale_revision_fails_before_egress(monkeypatch) -> None:
    """Managed test rejects stale refs before mutation or guarded transport."""

    counter = _patch_transaction_counter(monkeypatch)
    events: list[tuple[str, Any]] = []
    _recording_authorizer(monkeypatch, events)
    _patch_managed_transport(monkeypatch, events)
    user = _user("llm-managed-test-stale")
    connection_ref, _deployment_ref_value, _route_id = _managed_connection(user_id=user.id)
    connection_ref["expected_revision"] += 100
    client, app = _client(user)
    try:
        response = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/connection-presets/{HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID}/connection/test",
                json={"api_key": _MANAGED_SECRET, "connection_ref": connection_ref},
            ),
        )

        assert response.status_code == 400, response.text
        assert response.json() == {
            "detail": "Connection revision does not match the expected revision"
        }
        assert counter.commits == 0
        assert counter.rollbacks == 1
        assert events == []
        assert _MANAGED_SECRET not in response.text
    finally:
        app.dependency_overrides.clear()


def test_managed_ref_routes_reject_preset_mismatch_before_mutation_or_egress(
    monkeypatch,
    caplog,
) -> None:
    """Managed ref-bearing routes reject owned refs sent through another preset."""

    counter = _patch_transaction_counter(monkeypatch)
    events: list[tuple[str, Any]] = []
    _recording_authorizer(monkeypatch, events)
    _patch_managed_transport(monkeypatch, events)
    user = _user("llm-managed-preset-mismatch")
    connection_ref, deployment_ref, _route_id = _managed_connection(
        user_id=user.id,
        preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        with_product_deployment=True,
    )
    assert deployment_ref is not None
    client, app = _client(user)
    try:
        mismatched_test = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/connection-presets/{HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID}/connection/test",
                json={"api_key": _MANAGED_SECRET, "connection_ref": connection_ref},
            ),
        )
        assert mismatched_test.status_code == 400, mismatched_test.text
        assert mismatched_test.json() == {"detail": "Connection preset mismatch"}
        assert counter.commits == 0
        assert counter.rollbacks == 1
        assert events == []
        assert _connection_row(connection_ref).state == LLMConnectionState.DRAFT.value
        assert _deployment_count_for_connection(connection_ref) == 1

        mismatched_refresh = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/connection-presets/{HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID}/connection/refresh",
                json={"connection_ref": connection_ref},
            ),
        )
        assert mismatched_refresh.status_code == 400, mismatched_refresh.text
        assert mismatched_refresh.json() == {"detail": "Connection preset mismatch"}
        assert counter.commits == 0
        assert counter.rollbacks == 1
        assert events == []
        assert _connection_row(connection_ref).state == LLMConnectionState.DRAFT.value
        assert _deployment_count_for_connection(connection_ref) == 1

        mismatched_enable = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/connection-presets/{HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID}/connection/enable",
                json={
                    "connection_ref": connection_ref,
                    "deployment_ref": deployment_ref,
                },
            ),
        )
        assert mismatched_enable.status_code == 400, mismatched_enable.text
        assert mismatched_enable.json() == {"detail": "Connection preset mismatch"}
        assert counter.commits == 0
        assert counter.rollbacks == 1
        assert events == []
        assert _connection_row(connection_ref).state == LLMConnectionState.DRAFT.value
        assert _deployment_count_for_connection(connection_ref) == 1
        _assert_secret_absent(
            _MANAGED_SECRET,
            mismatched_test.text,
            mismatched_refresh.text,
            mismatched_enable.text,
            caplog.text,
        )
    finally:
        app.dependency_overrides.clear()


def test_managed_ref_routes_reject_foreign_refs_before_mutation_or_egress(monkeypatch) -> None:
    """Managed ref-bearing routes reject foreign connection/deployment refs first."""

    counter = _patch_transaction_counter(monkeypatch)
    events: list[tuple[str, Any]] = []
    _recording_authorizer(monkeypatch, events)
    _patch_managed_transport(monkeypatch, events)
    user = _user("llm-managed-foreign")
    foreign = _user("llm-managed-foreign-owner")
    foreign_ref, foreign_deployment_ref, _route_id = _managed_connection(
        user_id=foreign.id,
        preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        with_product_deployment=True,
    )
    owned_ref, _owned_deployment_ref, _route_id = _managed_connection(
        user_id=user.id,
        preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    )
    client, app = _client(user)
    try:
        foreign_test = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/connection-presets/{NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID}/connection/test",
                json={"api_key": _MANAGED_SECRET, "connection_ref": foreign_ref},
            ),
        )
        assert foreign_test.status_code == 400, foreign_test.text
        assert foreign_test.json() == {"detail": "Connection was not found"}
        assert counter.commits == 0
        assert counter.rollbacks == 1
        assert events == []

        foreign_refresh = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/connection-presets/{NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID}/connection/refresh",
                json={"connection_ref": foreign_ref},
            ),
        )
        assert foreign_refresh.status_code == 400, foreign_refresh.text
        assert foreign_refresh.json() == {"detail": "Connection was not found"}
        assert counter.commits == 0
        assert counter.rollbacks == 1
        assert events == []

        assert foreign_deployment_ref is not None
        foreign_enable = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/connection-presets/{NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID}/connection/enable",
                json={
                    "connection_ref": owned_ref,
                    "deployment_ref": foreign_deployment_ref,
                },
            ),
        )
        assert foreign_enable.status_code == 400, foreign_enable.text
        assert foreign_enable.json() == {"detail": "Deployment was not found"}
        assert counter.commits == 0
        assert counter.rollbacks == 1
        assert events == []
        assert _connection_row(owned_ref).state == LLMConnectionState.DRAFT.value
        assert _connection_row(foreign_ref).state == LLMConnectionState.DRAFT.value
    finally:
        app.dependency_overrides.clear()


def test_managed_refresh_success_and_invalid_body_rollback(monkeypatch, caplog) -> None:
    """Managed refresh parses inventory, filters product models, and rolls back bad bodies."""

    counter = _patch_transaction_counter(monkeypatch)
    events: list[tuple[str, Any]] = []
    _recording_authorizer(monkeypatch, events)
    body = (
        b'{"data":[{"id":"openai/gpt-oss-20b"},'
        b'{"id":"provider/unrelated"}]}'
    )
    _patch_managed_transport(monkeypatch, events, body=body)
    user = _user("llm-managed-refresh")
    connection_ref, _deployment_ref_value, _route_id = _managed_connection(
        user_id=user.id,
        preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    )
    events.clear()
    client, app = _client(user)
    try:
        refreshed = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/connection-presets/{NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID}/connection/refresh",
                json={"connection_ref": connection_ref},
            ),
        )
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["deployment_ref"] is not None
        assert refreshed.json()["runnability"]["status"] == "capability_unknown"
        assert counter.commits == 1
        assert counter.rollbacks == 0
        _assert_authorize_immediately_before_transport(
            events,
            LLMConnectionOperation.INVENTORY,
        )
        assert _deployment_count_for_connection(connection_ref) == 1
        assert _MANAGED_SECRET not in refreshed.text
    finally:
        app.dependency_overrides.clear()

    events.clear()
    bad_user = _user("llm-managed-refresh-bad")
    bad_ref, _deployment_ref_value, _route_id = _managed_connection(
        user_id=bad_user.id,
        preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    )
    events.clear()
    _patch_managed_transport(monkeypatch, events, body=b'{"data":[]}')
    client, app = _client(bad_user)
    try:
        failed = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/connection-presets/{NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID}/connection/refresh",
                json={"connection_ref": bad_ref},
            ),
        )
        assert failed.status_code == 400, failed.text
        assert failed.json() == {
            "detail": "Provider inventory response did not include models"
        }
        assert counter.commits == 0
        assert counter.rollbacks == 1
        assert _deployment_count_for_connection(bad_ref) == 0
        assert _MANAGED_SECRET not in failed.text
    finally:
        app.dependency_overrides.clear()

    events.clear()
    invalid_user = _user("llm-managed-refresh-invalid")
    invalid_ref, _deployment_ref_value, _route_id = _managed_connection(
        user_id=invalid_user.id,
        preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    )
    events.clear()
    _patch_managed_transport(monkeypatch, events, body=b'{"data":{}}')
    client, app = _client(invalid_user)
    try:
        invalid = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/connection-presets/{NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID}/connection/refresh",
                json={"connection_ref": invalid_ref},
            ),
        )
        assert invalid.status_code == 400, invalid.text
        assert invalid.json() == {"detail": "Provider inventory response is invalid"}
        assert counter.commits == 0
        assert counter.rollbacks == 1
        assert _deployment_count_for_connection(invalid_ref) == 0
        _assert_authorize_immediately_before_transport(
            events,
            LLMConnectionOperation.INVENTORY,
        )
        _assert_secret_absent(_MANAGED_SECRET, invalid.text, caplog.text)
    finally:
        app.dependency_overrides.clear()


def test_managed_enable_transitions_and_rejects_mismatched_deployment(
    monkeypatch,
    caplog,
) -> None:
    """Managed enable applies draft-to-enabled ordering and rejects mismatched refs."""

    counter = _patch_transaction_counter(monkeypatch)
    user = _user("llm-managed-enable")
    connection_ref, deployment_ref, _route_id = _managed_connection(
        user_id=user.id,
        preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        with_product_deployment=True,
    )
    client, app = _client(user)
    try:
        enabled = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/connection-presets/{NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID}/connection/enable",
                json={
                    "connection_ref": connection_ref,
                    "deployment_ref": deployment_ref,
                },
            ),
        )
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["lifecycle_state"] == "enabled"
        assert enabled.json()["connection_ref"]["expected_revision"] == 4
        assert counter.commits == 1
        assert counter.rollbacks == 0
        assert _connection_row(connection_ref).state == LLMConnectionState.ENABLED.value
    finally:
        app.dependency_overrides.clear()

    other_ref, other_deployment_ref, _route_id = _managed_connection(
        user_id=user.id,
        preset_id=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
        with_product_deployment=True,
    )
    client, app = _client(user)
    try:
        rejected = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/connection-presets/{HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID}/connection/enable",
                json={
                    "connection_ref": other_ref,
                    "deployment_ref": deployment_ref,
                },
            ),
        )
        assert rejected.status_code == 400, rejected.text
        assert rejected.json() == {"detail": "Deployment connection mismatch"}
        assert counter.commits == 0
        assert counter.rollbacks == 1
        assert _connection_row(other_ref).state == LLMConnectionState.DRAFT.value
        assert other_deployment_ref is not None
    finally:
        app.dependency_overrides.clear()

    stale_deployment_ref = dict(other_deployment_ref)
    stale_deployment_ref["expected_revision"] += 100
    client, app = _client(user)
    try:
        stale = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/connection-presets/{HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID}/connection/enable",
                json={
                    "connection_ref": other_ref,
                    "deployment_ref": stale_deployment_ref,
                },
            ),
        )
        assert stale.status_code == 400, stale.text
        assert stale.json() == {"detail": "Deployment revision is stale"}
        assert counter.commits == 0
        assert counter.rollbacks == 1
        assert _connection_row(other_ref).state == LLMConnectionState.DRAFT.value
        _assert_secret_absent(_MANAGED_SECRET, stale.text, caplog.text)
    finally:
        app.dependency_overrides.clear()


def test_proving_create_success_and_service_failure_rollback(monkeypatch, caplog) -> None:
    """Proving create persists credential/deployment on success and rolls back partial failure."""

    counter = _patch_transaction_counter(monkeypatch)
    user = _user("llm-proving-create")
    client, app = _client(user)
    try:
        created = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection",
                json={"api_key": _PROVING_SECRET, "display_label": "Proof endpoint"},
            ),
        )
        assert created.status_code == 200, created.text
        assert counter.commits == 1
        assert counter.rollbacks == 0
        payload = created.json()
        assert payload["lifecycle_state"] == "draft"
        assert payload["connection_ref"]["expected_revision"] == 2
        assert payload["deployment_ref"]["expected_revision"] == 1
        assert payload["verification"]["code"] == "not_tested"
        _assert_secret_absent(_PROVING_SECRET, created.text, caplog.text)
    finally:
        app.dependency_overrides.clear()

    failing_user = _user("llm-proving-create-fail")
    original_create = LLMDeploymentService.create_gpt_oss_20b_proving_deployment
    captured_exceptions: list[BaseException] = []

    def failing_create(self, **kwargs):
        exc = LLMProviderServiceError("deployment route failed")
        captured_exceptions.append(exc)
        raise exc

    monkeypatch.setattr(
        LLMDeploymentService,
        "create_gpt_oss_20b_proving_deployment",
        failing_create,
    )
    client, app = _client(failing_user)
    try:
        failed = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection",
                json={"api_key": _PROVING_SECRET},
            ),
        )
        assert failed.status_code == 400, failed.text
        assert failed.json() == {"detail": "deployment route failed"}
        assert counter.commits == 0
        assert counter.rollbacks == 1
        assert _count_connections(failing_user.id, GPT_OSS_20B_PROVING_PRESET_ID) == 0
        _assert_secret_absent(
            _PROVING_SECRET,
            failed.text,
            caplog.text,
            exceptions=captured_exceptions,
        )
    finally:
        monkeypatch.setattr(
            LLMDeploymentService,
            "create_gpt_oss_20b_proving_deployment",
            original_create,
        )
        app.dependency_overrides.clear()


def test_proving_test_persists_observations_and_rejects_rotated_secret(monkeypatch) -> None:
    """Proving test checks stored/supplied secret equality and records evidence."""

    counter = _patch_transaction_counter(monkeypatch)
    events: list[tuple[str, Any]] = []
    _recording_authorizer(monkeypatch, events)
    _patch_proving_transport(monkeypatch, events)
    user = _user("llm-proving-test")
    connection_ref, deployment_ref, route_id = _proving_connection(user_id=user.id)
    events.clear()
    client, app = _client(user)
    try:
        verified = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection/test",
                json={
                    "api_key": _PROVING_SECRET,
                    "connection_ref": connection_ref,
                    "deployment_ref": deployment_ref,
                },
            ),
        )
        assert verified.status_code == 200, verified.text
        assert verified.json()["status"] == "passed"
        assert verified.json()["usage"] == {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        }
        assert counter.commits == 1
        assert counter.rollbacks == 0
        _assert_authorize_immediately_before_transport(
            events,
            LLMConnectionOperation.INVENTORY,
        )
        _assert_authorize_immediately_before_transport(
            events,
            LLMConnectionOperation.CAPABILITY_PROBE,
        )
        assert _PROVING_SECRET not in verified.text
    finally:
        app.dependency_overrides.clear()

    db = SessionLocal()
    try:
        observations = (
            db.query(LLMCapabilityObservation)
            .filter(
                LLMCapabilityObservation.deployment_id
                == UUID(deployment_ref["deployment_id"]),
                LLMCapabilityObservation.route_id == route_id,
            )
            .order_by(LLMCapabilityObservation.capability.asc())
            .all()
        )
        assert [row.capability for row in observations] == [
            LLMCapability.CHAT.value,
            LLMCapability.USAGE_REPORTING.value,
        ]
        assert all(
            row.constraints["connection_revision"]
            == connection_ref["expected_revision"]
            for row in observations
        )
    finally:
        db.close()

    events.clear()
    client, app = _client(user)
    try:
        rejected = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection/test",
                json={
                    "api_key": "rotated-secret",
                    "connection_ref": connection_ref,
                    "deployment_ref": deployment_ref,
                },
            ),
        )
        assert rejected.status_code == 400, rejected.text
        assert rejected.json() == {
            "detail": "Stored proving credential must pass verification"
        }
        assert counter.commits == 0
        assert counter.rollbacks == 1
        assert events == []
        assert "rotated-secret" not in rejected.text
    finally:
        app.dependency_overrides.clear()


def test_proving_ref_routes_reject_foreign_refs_before_mutation_or_egress(monkeypatch) -> None:
    """Proving ref-bearing routes reject foreign refs before evidence or state changes."""

    counter = _patch_transaction_counter(monkeypatch)
    events: list[tuple[str, Any]] = []
    _recording_authorizer(monkeypatch, events)
    _patch_proving_transport(monkeypatch, events)
    user = _user("llm-proving-foreign")
    foreign = _user("llm-proving-foreign-owner")
    foreign_ref, foreign_deployment_ref, _route_id = _proving_connection(
        user_id=foreign.id
    )
    owned_ref, owned_deployment_ref, _route_id = _proving_connection(user_id=user.id)
    client, app = _client(user)
    try:
        foreign_deployment_test = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection/test",
                json={
                    "api_key": _PROVING_SECRET,
                    "connection_ref": foreign_ref,
                    "deployment_ref": foreign_deployment_ref,
                },
            ),
        )
        assert foreign_deployment_test.status_code == 400, foreign_deployment_test.text
        assert foreign_deployment_test.json() == {"detail": "Deployment was not found"}
        assert counter.commits == 0
        assert counter.rollbacks == 1
        assert events == []

        foreign_connection_test = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection/test",
                json={
                    "api_key": _PROVING_SECRET,
                    "connection_ref": foreign_ref,
                    "deployment_ref": owned_deployment_ref,
                },
            ),
        )
        assert foreign_connection_test.status_code == 400, foreign_connection_test.text
        assert foreign_connection_test.json() == {
            "detail": "Connection credential ref is unavailable"
        }
        assert counter.commits == 0
        assert counter.rollbacks == 1
        assert events == []

        foreign_enable = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection/enable",
                json={
                    "connection_ref": owned_ref,
                    "deployment_ref": foreign_deployment_ref,
                },
            ),
        )
        assert foreign_enable.status_code == 400, foreign_enable.text
        assert foreign_enable.json() == {"detail": "Deployment was not found"}
        assert counter.commits == 0
        assert counter.rollbacks == 1
        assert events == []
        assert _connection_row(owned_ref).state == LLMConnectionState.DRAFT.value
        assert _connection_row(foreign_ref).state == LLMConnectionState.DRAFT.value
    finally:
        app.dependency_overrides.clear()


def test_proving_ref_routes_reject_stale_and_mismatched_owned_refs_before_mutation_or_egress(
    monkeypatch,
    caplog,
) -> None:
    """Proving ref routes reject stale and mismatched owned refs before egress."""

    counter = _patch_transaction_counter(monkeypatch)
    events: list[tuple[str, Any]] = []
    _recording_authorizer(monkeypatch, events)
    _patch_proving_transport(monkeypatch, events)
    user = _user("llm-proving-stale-mismatch")
    first_ref, first_deployment_ref, _route_id = _proving_connection(user_id=user.id)
    db = SessionLocal()
    try:
        first_connection = LLMConnectionService(db).transition_state(
            user_id=user.id,
            connection_id=UUID(first_ref["connection_id"]),
            expected_revision=first_ref["expected_revision"],
            target_state=LLMConnectionState.DISABLED,
        )
        first_ref = _connection_ref(first_connection)
        db.commit()
    finally:
        db.close()
    second_ref, second_deployment_ref, _route_id = _managed_connection(
        user_id=user.id,
        preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        with_product_deployment=True,
    )
    assert second_deployment_ref is not None
    stale_deployment_ref = dict(first_deployment_ref)
    stale_deployment_ref["expected_revision"] += 100
    client, app = _client(user)
    try:
        stale_test = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection/test",
                json={
                    "api_key": _PROVING_SECRET,
                    "connection_ref": first_ref,
                    "deployment_ref": stale_deployment_ref,
                },
            ),
        )
        assert stale_test.status_code == 400, stale_test.text
        assert stale_test.json() == {"detail": "Deployment revision is stale"}
        assert counter.commits == 0
        assert counter.rollbacks == 1
        assert events == []
        assert _connection_row(first_ref).state == LLMConnectionState.DISABLED.value
        _assert_secret_absent(_PROVING_SECRET, stale_test.text, caplog.text)

        stale_enable = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection/enable",
                json={
                    "connection_ref": first_ref,
                    "deployment_ref": stale_deployment_ref,
                },
            ),
        )
        assert stale_enable.status_code == 400, stale_enable.text
        assert stale_enable.json() == {"detail": "Deployment revision is stale"}
        assert counter.commits == 0
        assert counter.rollbacks == 1
        assert events == []
        assert _connection_row(first_ref).state == LLMConnectionState.DISABLED.value
        _assert_secret_absent(_PROVING_SECRET, stale_enable.text, caplog.text)

        mismatch_enable = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection/enable",
                json={
                    "connection_ref": first_ref,
                    "deployment_ref": second_deployment_ref,
                },
            ),
        )
        assert mismatch_enable.status_code == 400, mismatch_enable.text
        assert mismatch_enable.json() == {"detail": "Deployment route is unavailable"}
        assert counter.commits == 0
        assert counter.rollbacks == 1
        assert events == []
        assert _connection_row(first_ref).state == LLMConnectionState.DISABLED.value
        assert _connection_row(second_ref).state == LLMConnectionState.DRAFT.value
        _assert_secret_absent(_PROVING_SECRET, mismatch_enable.text, caplog.text)

        mismatch_test = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection/test",
                json={
                    "api_key": _PROVING_SECRET,
                    "connection_ref": first_ref,
                    "deployment_ref": second_deployment_ref,
                },
            ),
        )
        assert mismatch_test.status_code == 200, mismatch_test.text
        assert mismatch_test.json()["status"] == "failed"
        assert mismatch_test.json()["code"] == "deployment_route_mismatch"
        assert mismatch_test.json()["message"] == "Deployment route is unavailable"
        assert mismatch_test.json()["retryable"] is False
        assert counter.commits == 1
        assert counter.rollbacks == 0
        assert events == []
        assert _connection_row(first_ref).state == LLMConnectionState.DISABLED.value
        assert _connection_row(second_ref).state == LLMConnectionState.DRAFT.value
        _assert_secret_absent(_PROVING_SECRET, mismatch_test.text, caplog.text)
    finally:
        app.dependency_overrides.clear()


def test_proving_enable_requires_evidence_then_rebinds_observation_revision(monkeypatch) -> None:
    """Proving enable gates on evidence, transitions state, and rebinds observations."""

    counter = _patch_transaction_counter(monkeypatch)
    events: list[tuple[str, Any]] = []
    _recording_authorizer(monkeypatch, events)
    _patch_proving_transport(monkeypatch, events)
    user = _user("llm-proving-enable")
    connection_ref, deployment_ref, route_id = _proving_connection(user_id=user.id)
    events.clear()
    client, app = _client(user)
    try:
        rejected = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection/enable",
                json={
                    "connection_ref": connection_ref,
                    "deployment_ref": deployment_ref,
                },
            ),
        )
        assert rejected.status_code == 400, rejected.text
        assert rejected.json() == {
            "detail": "Successful proving verification is required before enablement"
        }
        assert counter.commits == 0
        assert counter.rollbacks == 1
        assert _connection_row(connection_ref).state == LLMConnectionState.DRAFT.value

        verified = client.post(
            f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection/test",
            json={
                "api_key": _PROVING_SECRET,
                "connection_ref": connection_ref,
                "deployment_ref": deployment_ref,
            },
        )
        assert verified.status_code == 200, verified.text

        enabled = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection/enable",
                json={
                    "connection_ref": connection_ref,
                    "deployment_ref": deployment_ref,
                },
            ),
        )
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["lifecycle_state"] == "enabled"
        assert enabled.json()["connection_ref"]["expected_revision"] == 4
        assert enabled.json()["verification"]["code"] == "verified"
        assert counter.commits == 1
        assert counter.rollbacks == 0
        assert _PROVING_SECRET not in enabled.text
    finally:
        app.dependency_overrides.clear()

    db = SessionLocal()
    try:
        observations = (
            db.query(LLMCapabilityObservation)
            .filter(
                LLMCapabilityObservation.deployment_id
                == UUID(deployment_ref["deployment_id"]),
                LLMCapabilityObservation.route_id == route_id,
            )
            .all()
        )
        assert observations
        assert {
            row.constraints["connection_revision"] for row in observations
        } == {4}
    finally:
        db.close()


def test_managed_refresh_transport_error_rolls_back_without_secret(monkeypatch, caplog) -> None:
    """Guarded transport errors preserve safe detail and no partial inventory."""

    counter = _patch_transaction_counter(monkeypatch)
    events: list[tuple[str, Any]] = []
    _recording_authorizer(monkeypatch, events)
    captured_exceptions: list[BaseException] = []

    class FailingGuardedTransport:
        def __init__(self, **_kwargs) -> None:
            pass

        def execute(self, operation, **kwargs):
            events.append(("transport", operation, kwargs))
            exc = GuardedTransportError(
                "safe guarded failure",
                audit_id="audit-refresh-failure",
            )
            captured_exceptions.append(exc)
            raise exc

    monkeypatch.setattr(
        managed_lifecycle_module,
        "GuardedTransport",
        FailingGuardedTransport,
    )
    user = _user("llm-managed-refresh-transport-fail")
    connection_ref, _deployment_ref_value, _route_id = _managed_connection(
        user_id=user.id,
        preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    )
    events.clear()
    client, app = _client(user)
    try:
        response = _during_request(
            counter,
            lambda: client.post(
                f"/api/llm/connection-presets/{NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID}/connection/refresh",
                json={"connection_ref": connection_ref},
            ),
        )
        assert response.status_code == 400, response.text
        assert response.json() == {
            "detail": "safe guarded failure (audit_id=audit-refresh-failure)"
        }
        assert counter.commits == 0
        assert counter.rollbacks == 1
        assert _deployment_count_for_connection(connection_ref) == 0
        _assert_authorize_immediately_before_transport(
            events,
            LLMConnectionOperation.INVENTORY,
        )
        _assert_secret_absent(
            _MANAGED_SECRET,
            response.text,
            caplog.text,
            exceptions=captured_exceptions,
        )
    finally:
        app.dependency_overrides.clear()
