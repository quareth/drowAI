"""Tests for bounded GPT-OSS proving health and capability verification."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.providers.llm.core.capabilities import LLMCapability
from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID, ProviderModelRef
from agent.providers.llm.profiles.registry import require_model_profile
from backend.models import (
    LLMCapabilityObservation,
    LLMDeploymentRoute,
    LLMModelDeployment,
    User,
)
from backend.services.llm_provider.connection_service import LLMConnectionService
from backend.services.llm_provider.deployment_service import LLMDeploymentService
from backend.services.llm_provider.effective_profile_service import EffectiveProfileService
from backend.services.llm_provider.health_service import LLMProviderHealthService
from backend.services.llm_provider.inventory_service import LLMProviderInventoryService
from backend.services.llm_provider.operation_registry import (
    GPT_OSS_20B_PROVING_BASE_URL_ENV,
    GPT_OSS_20B_PROVING_PRESET_ID,
)
from backend.services.llm_provider.types import (
    GuardedHTTPResponse,
    LLMConnectionOperation,
    LLMConnectionState,
    ProviderSecret,
)


class _RecordingTransport:
    """Guarded transport double returning queued JSON responses."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def execute(self, operation: Any, **kwargs: Any) -> GuardedHTTPResponse:
        self.calls.append({"operation": operation, **kwargs})
        payload = self._responses.pop(0)
        return GuardedHTTPResponse(
            status_code=200,
            body=json.dumps(payload).encode("utf-8"),
            audit_id=f"probe-{len(self.calls)}",
        )


def _gpt_oss_connection_and_route(
    db: Session,
    *,
    user_id: int,
):
    connection = LLMConnectionService(db).create_gpt_oss_20b_proving_draft(
        user_id=user_id,
    )
    deployment, route = LLMDeploymentService(db).create_gpt_oss_20b_proving_deployment(
        user_id=user_id,
        connection_id=connection.id,
        expected_connection_revision=1,
    )
    return connection, deployment, route


def _capability_observation(
    *,
    deployment: LLMModelDeployment,
    route: LLMDeploymentRoute,
    capability: str,
    constraints: Any,
) -> LLMCapabilityObservation:
    observed_at = datetime.now(timezone.utc)
    return LLMCapabilityObservation(
        id=uuid4(),
        deployment_id=deployment.id,
        route_id=route.id,
        capability=capability,
        support_state="supported",
        constraints=constraints,
        source="gpt_oss_proving_probe",
        observed_at=observed_at,
        expires_at=observed_at + timedelta(hours=1),
        revision=1,
        fingerprint=f"rebind-{capability}",
    )


def test_gpt_oss_probe_requires_inventory_model_and_usage_evidence(
    monkeypatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    monkeypatch.setenv(GPT_OSS_20B_PROVING_BASE_URL_ENV, "https://gpt-oss.example.test")
    owner, _ = identity_users
    profile_before = require_model_profile(
        ProviderModelRef(OPENAI_PROVIDER_ID, "gpt-oss-20b")
    )
    connection, deployment, route = _gpt_oss_connection_and_route(
        llm_identity_db,
        user_id=owner.id,
    )
    transport = _RecordingTransport(
        [
            {"data": [{"id": "openai/gpt-oss-20b"}]},
            {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "total_tokens": 6,
                },
            },
        ]
    )

    result = LLMProviderHealthService(
        llm_identity_db,
        guarded_transport=transport,  # type: ignore[arg-type]
    ).verify_gpt_oss_20b_proving_connection(
        user_id=owner.id,
        connection_id=connection.id,
        expected_connection_revision=1,
        deployment_id=deployment.id,
        route_id=route.id,
        api_key="sk-test-secret",
        credential_fingerprint="fingerprint-a",
    )

    assert result.status == "passed"
    assert result.code == "verified"
    assert result.retryable is False
    assert result.model_present is True
    assert result.usage == {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}
    assert isinstance(result.observed_at, datetime)
    assert result.observed_at.tzinfo is not None
    assert result.expires_at > result.observed_at
    assert "sk-test-secret" not in result.message
    assert "gpt-oss.example.test" not in result.message

    assert transport.calls == [
        {
            "operation": LLMConnectionOperation.INVENTORY,
            "provider": GPT_OSS_20B_PROVING_PRESET_ID,
            "secret": ProviderSecret(
                provider=GPT_OSS_20B_PROVING_PRESET_ID,
                value="sk-test-secret",
            ),
        },
        {
            "operation": LLMConnectionOperation.CAPABILITY_PROBE,
            "provider": GPT_OSS_20B_PROVING_PRESET_ID,
            "secret": ProviderSecret(
                provider=GPT_OSS_20B_PROVING_PRESET_ID,
                value="sk-test-secret",
            ),
            "json_body": {
                "model": "openai/gpt-oss-20b",
                "messages": [{"role": "user", "content": "ping"}],
                "stream": False,
                "max_tokens": 1,
            },
        },
    ]
    assert connection.state == LLMConnectionState.DRAFT.value

    observations = llm_identity_db.execute(
        select(LLMCapabilityObservation).where(
            LLMCapabilityObservation.deployment_id == deployment.id
        )
    ).scalars()
    observed = {(row.capability, row.support_state, row.source) for row in observations}
    assert observed == {
        ("chat", "supported", "gpt_oss_proving_probe"),
        ("usage_reporting", "supported", "gpt_oss_proving_probe"),
    }
    for row in llm_identity_db.execute(
        select(LLMCapabilityObservation).where(
            LLMCapabilityObservation.deployment_id == deployment.id
        )
    ).scalars():
        assert row.constraints == {
            "connection_id": str(connection.id),
            "connection_revision": 1,
            "credential_fingerprint": "fingerprint-a",
        }

    runnability = EffectiveProfileService(llm_identity_db).classify_runnability(
        deployment=deployment,
        route=route,
        required_capabilities=(LLMCapability.CHAT, LLMCapability.TOOLS),
    )
    assert runnability.runnable is False
    assert runnability.status == "capability_unknown"
    assert runnability.missing_capabilities == ("tools",)

    profile_after = require_model_profile(
        ProviderModelRef(OPENAI_PROVIDER_ID, "gpt-oss-20b")
    )
    assert profile_after is profile_before
    assert profile_after.supports(LLMCapability.TOOLS)


def test_gpt_oss_probe_fails_closed_without_provider_reported_usage(
    monkeypatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    monkeypatch.setenv(GPT_OSS_20B_PROVING_BASE_URL_ENV, "https://gpt-oss.example.test")
    owner, _ = identity_users
    connection, deployment, route = _gpt_oss_connection_and_route(
        llm_identity_db,
        user_id=owner.id,
    )
    transport = _RecordingTransport(
        [
            {"data": [{"id": "openai/gpt-oss-20b"}]},
            {"choices": [{"message": {"content": "ok"}}]},
        ]
    )

    result = LLMProviderHealthService(
        llm_identity_db,
        guarded_transport=transport,  # type: ignore[arg-type]
    ).verify_gpt_oss_20b_proving_connection(
        user_id=owner.id,
        connection_id=connection.id,
        expected_connection_revision=1,
        deployment_id=deployment.id,
        route_id=route.id,
        api_key="sk-test-secret",
        credential_fingerprint="fingerprint-a",
    )

    assert result.status == "failed"
    assert result.code == "usage_unavailable"
    assert result.retryable is False
    assert result.model_present is True
    assert result.usage is None
    assert result.observed_at <= datetime.now(timezone.utc)
    assert result.expires_at > result.observed_at
    assert "sk-test-secret" not in result.message
    assert "gpt-oss.example.test" not in result.message
    assert llm_identity_db.execute(
        select(LLMCapabilityObservation).where(
            LLMCapabilityObservation.deployment_id == deployment.id
        )
    ).scalar_one_or_none() is None


def test_gpt_oss_runnability_rejects_expired_observations(
    monkeypatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Expired capability evidence does not satisfy proving runnability."""

    monkeypatch.setenv(GPT_OSS_20B_PROVING_BASE_URL_ENV, "https://gpt-oss.example.test")
    owner, _ = identity_users
    connection, deployment, route = _gpt_oss_connection_and_route(
        llm_identity_db,
        user_id=owner.id,
    )
    observed_at = datetime.now(timezone.utc) - timedelta(hours=2)
    for index, capability in enumerate((LLMCapability.CHAT, LLMCapability.USAGE_REPORTING), start=1):
        llm_identity_db.add(
            LLMCapabilityObservation(
                id=uuid4(),
                deployment_id=deployment.id,
                route_id=route.id,
                capability=capability.value,
                support_state="supported",
                constraints={
                    "connection_id": str(connection.id),
                    "connection_revision": 1,
                    "credential_fingerprint": "fingerprint-a",
                },
                source="gpt_oss_proving_probe",
                observed_at=observed_at,
                expires_at=observed_at + timedelta(hours=1),
                revision=index,
                fingerprint=f"expired-{index}",
            )
        )
    llm_identity_db.flush()

    runnability = EffectiveProfileService(llm_identity_db).classify_runnability(
        deployment=deployment,
        route=route,
        required_capabilities=(LLMCapability.CHAT, LLMCapability.USAGE_REPORTING),
        connection_id=str(connection.id),
        connection_revision=1,
        credential_fingerprint="fingerprint-a",
    )

    assert runnability.runnable is False
    assert runnability.status == "capability_unknown"


def test_gpt_oss_probe_fails_closed_when_inventory_lacks_exact_model(
    monkeypatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    monkeypatch.setenv(GPT_OSS_20B_PROVING_BASE_URL_ENV, "https://gpt-oss.example.test")
    owner, _ = identity_users
    connection, deployment, route = _gpt_oss_connection_and_route(
        llm_identity_db,
        user_id=owner.id,
    )
    transport = _RecordingTransport([{"data": [{"id": "openai/GPT-OSS-20B"}]}])

    result = LLMProviderHealthService(
        llm_identity_db,
        guarded_transport=transport,  # type: ignore[arg-type]
    ).verify_gpt_oss_20b_proving_connection(
        user_id=owner.id,
        connection_id=connection.id,
        expected_connection_revision=1,
        deployment_id=deployment.id,
        route_id=route.id,
        api_key="sk-test-secret",
        credential_fingerprint="fingerprint-a",
    )

    assert result.status == "failed"
    assert result.code == "model_not_found"
    assert result.model_present is False
    assert result.retryable is False
    assert len(transport.calls) == 1


def test_proving_observation_revision_rebinding_updates_only_exact_matches(
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Revision rebinding preserves selection, ignored rows, and transaction ownership."""

    owner, _ = identity_users
    connection, deployment, route = _gpt_oss_connection_and_route(
        llm_identity_db,
        user_id=owner.id,
    )
    other_deployment = LLMDeploymentService(llm_identity_db).create_deployment(
        user_id=owner.id,
        connection_id=connection.id,
        expected_connection_revision=1,
        wire_model_id="other-model",
        display_name="Other model",
        discovery_source="test",
    )
    other_route = LLMDeploymentRoute(
        id=uuid4(),
        deployment_id=deployment.id,
        adapter_id="other-adapter",
        adapter_version="1",
        api_surface="other-surface",
        dialect_policy_id="other-dialect",
        enabled=True,
    )
    llm_identity_db.add(other_route)

    matching_constraints = {
        "connection_id": str(connection.id),
        "connection_revision": 1,
        "credential_fingerprint": "fingerprint-a",
        "nested": {"preserved": True},
    }
    matching_string_revision = {
        "connection_id": str(connection.id),
        "connection_revision": "1",
        "credential_fingerprint": "fingerprint-b",
        "extra": "preserved",
    }
    seeded = {
        "matching-int": _capability_observation(
            deployment=deployment,
            route=route,
            capability="matching-int",
            constraints=matching_constraints,
        ),
        "matching-string": _capability_observation(
            deployment=deployment,
            route=route,
            capability="matching-string",
            constraints=matching_string_revision,
        ),
        "wrong-deployment": _capability_observation(
            deployment=other_deployment,
            route=route,
            capability="wrong-deployment",
            constraints={
                "connection_id": str(connection.id),
                "connection_revision": 1,
            },
        ),
        "wrong-route": _capability_observation(
            deployment=deployment,
            route=other_route,
            capability="wrong-route",
            constraints={
                "connection_id": str(connection.id),
                "connection_revision": 1,
            },
        ),
        "non-dict": _capability_observation(
            deployment=deployment,
            route=route,
            capability="non-dict",
            constraints=["not", "a", "mapping"],
        ),
        "wrong-connection": _capability_observation(
            deployment=deployment,
            route=route,
            capability="wrong-connection",
            constraints={
                "connection_id": str(uuid4()),
                "connection_revision": 1,
            },
        ),
        "missing-revision": _capability_observation(
            deployment=deployment,
            route=route,
            capability="missing-revision",
            constraints={"connection_id": str(connection.id)},
        ),
        "non-integer-revision": _capability_observation(
            deployment=deployment,
            route=route,
            capability="non-integer-revision",
            constraints={
                "connection_id": str(connection.id),
                "connection_revision": "not-an-integer",
            },
        ),
        "wrong-prior-revision": _capability_observation(
            deployment=deployment,
            route=route,
            capability="wrong-prior-revision",
            constraints={
                "connection_id": str(connection.id),
                "connection_revision": 2,
            },
        ),
    }
    llm_identity_db.add_all(seeded.values())
    llm_identity_db.flush()
    ignored_constraints = {
        name: row.constraints
        for name, row in seeded.items()
        if not name.startswith("matching-")
    }

    connections = LLMConnectionService(llm_identity_db)
    connections.transition_state(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=1,
        target_state=LLMConnectionState.DISABLED,
    )
    connections.transition_state(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=2,
        target_state=LLMConnectionState.ENABLED,
    )
    assert connection.revision == 3

    transaction_events: list[str] = []
    original_flush = llm_identity_db.flush

    def recording_flush(*args: Any, **kwargs: Any) -> None:
        transaction_events.append("flush")
        original_flush(*args, **kwargs)

    monkeypatch.setattr(llm_identity_db, "flush", recording_flush)
    monkeypatch.setattr(
        llm_identity_db,
        "commit",
        lambda: transaction_events.append("commit"),
    )
    monkeypatch.setattr(
        llm_identity_db,
        "rollback",
        lambda: transaction_events.append("rollback"),
    )

    LLMProviderInventoryService(
        llm_identity_db
    ).rebind_proving_observation_revision(
        deployment=deployment,
        route=route,
        connection=connection,
        previous_connection_revision=1,
    )

    assert transaction_events == ["flush"]
    llm_identity_db.expire_all()
    refreshed = {
        row.capability: row
        for row in llm_identity_db.execute(
            select(LLMCapabilityObservation)
        ).scalars()
    }
    assert refreshed["matching-int"].constraints == {
        **matching_constraints,
        "connection_revision": 3,
    }
    assert refreshed["matching-string"].constraints == {
        **matching_string_revision,
        "connection_revision": 3,
    }
    for capability, constraints in ignored_constraints.items():
        assert refreshed[capability].constraints == constraints
