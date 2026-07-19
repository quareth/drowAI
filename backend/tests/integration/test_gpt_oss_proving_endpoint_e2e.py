"""Opt-in GPT-OSS proving endpoint tests for the full deployment path."""

from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent.providers.llm.core.exceptions import LLMConfigurationError
from backend.auth import create_access_token
from backend.database import SessionLocal
from backend.main import app
from backend.models import (
    LLMInferenceConnection,
    Task,
    Tenant,
    TenantMembership,
    User,
    UserLLMProviderCredential,
    UserSettings,
)
from backend.services.llm_provider import (
    LLMCredentialService,
    LLMProviderSelectionService,
    LLMRuntimeAccessContext,
    LLMRuntimeClientResolver,
)
from backend.services.llm_provider.operation_registry import (
    GPT_OSS_20B_PROVING_API_KEY_ENV,
    GPT_OSS_20B_PROVING_BASE_URL_ENV,
    GPT_OSS_20B_PROVING_E2E_ENV,
    GPT_OSS_20B_PROVING_PRESET_ID,
)
from backend.services.llm_provider.types import (
    GuardedHTTPResponse,
    LLMConnectionCredentialRef,
    LLMConnectionOperation,
)
from backend.services.usage_tracking.pricing_registry import (
    PRICING_UNAVAILABLE,
    get_pricing_quote,
)
from agent.providers.llm.core.identity import ProviderModelRef


GPT_OSS_20B_PROVING_STREAMING_ENV = "DROWAI_GPT_OSS_20B_PROVING_STREAMING"
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def _unique_username(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _seed_user_task(db, *, username: str) -> tuple[User, Task]:
    user = User(username=username, password="x", email=f"{username}@example.com")
    db.add(user)
    db.commit()
    db.refresh(user)
    db.add(UserSettings(user_id=user.id, openai_model="gpt-5.2"))
    tenant = Tenant(
        slug=f"gpt-oss-proof-{uuid4().hex[:12]}",
        name="GPT-OSS proof tenant",
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    db.add(
        TenantMembership(
            tenant_id=tenant.id,
            user_id=user.id,
            role="owner",
            status="active",
        )
    )
    task = Task(user_id=user.id, tenant_id=tenant.id, name="gpt-oss-proof-task")
    db.add(task)
    db.commit()
    db.refresh(task)
    return user, task


def _auth_header_for(user: User) -> dict[str, str]:
    token = create_access_token({"sub": user.username, "user_id": user.id})
    return {"Authorization": f"Bearer {token}"}


def _missing_e2e_reason() -> str | None:
    if not _env_enabled(GPT_OSS_20B_PROVING_E2E_ENV):
        return f"{GPT_OSS_20B_PROVING_E2E_ENV} is not enabled"
    missing = [
        name
        for name in (
            GPT_OSS_20B_PROVING_BASE_URL_ENV,
            GPT_OSS_20B_PROVING_API_KEY_ENV,
        )
        if not os.getenv(name)
    ]
    if missing:
        return "missing " + ", ".join(missing)
    return None


def _env_enabled(name: str) -> bool:
    """Return whether an opt-in test flag is explicitly enabled."""

    return os.getenv(name, "").strip().lower() in _TRUTHY_ENV_VALUES


def _assert_secret_safe_response(
    payload: dict,
    *,
    secret: str | None = None,
    endpoint_base_url: str | None = None,
) -> None:
    material = json.dumps(payload).lower()
    if "api_key" in material or "authorization" in material:
        pytest.fail("response leaked credential field names")
    if endpoint_base_url and endpoint_base_url.lower() in material:
        pytest.fail("response leaked endpoint configuration")
    if secret and secret.lower() in material:
        pytest.fail("response leaked secret material")


def test_gpt_oss_proving_route_flow_is_owner_scoped_and_secret_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route flow creates, verifies, enables, selects, and resolves one deployment."""

    monkeypatch.setenv(GPT_OSS_20B_PROVING_BASE_URL_ENV, "https://gpt-oss.example.test")
    transport_calls: list[dict] = []

    def fake_execute(self, operation, provider, secret, json_body=None):
        del self
        transport_calls.append(
            {
                "operation": LLMConnectionOperation(operation),
                "provider": provider,
                "secret": secret.value,
                "json_body": json_body,
            }
        )
        assert provider == GPT_OSS_20B_PROVING_PRESET_ID
        assert secret.value == "test-route-proof-key"
        if LLMConnectionOperation(operation) is LLMConnectionOperation.INVENTORY:
            return GuardedHTTPResponse(
                status_code=200,
                body=json.dumps(
                    {"data": [{"id": "openai/gpt-oss-20b"}]},
                ).encode(),
                audit_id="inventory-audit",
            )
        assert json_body == {
            "model": "openai/gpt-oss-20b",
            "messages": [{"role": "user", "content": "ping"}],
            "stream": False,
            "max_tokens": 1,
        }
        return GuardedHTTPResponse(
            status_code=200,
            body=json.dumps(
                {
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {
                        "prompt_tokens": 4,
                        "completion_tokens": 2,
                        "total_tokens": 6,
                    },
                }
            ).encode(),
            audit_id="probe-audit",
        )

    monkeypatch.setattr(
        "backend.services.llm_provider.guarded_transport.GuardedTransport.execute",
        fake_execute,
    )

    db = SessionLocal()
    try:
        user, task = _seed_user_task(
            db,
            username=_unique_username("gpt-oss-route-proof"),
        )
        headers = _auth_header_for(user)
    finally:
        db.close()

    with TestClient(app) as client:
        created = client.post(
            f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection",
            headers=headers,
            json={"api_key": "test-route-proof-key"},
        )
        assert created.status_code == 200, created.text
        created_body = created.json()
        _assert_secret_safe_response(created_body)
        assert created_body["lifecycle_state"] == "draft"
        assert created_body["verification"]["code"] == "not_tested"
        connection_ref = created_body["connection_ref"]
        deployment_ref = created_body["deployment_ref"]

        draft_selected = client.put(
            "/api/llm/selection",
            headers=headers,
            json={"deployment_ref": deployment_ref},
        )
        assert draft_selected.status_code == 400

        mismatched = client.post(
            f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection/test",
            headers=headers,
            json={
                "api_key": "different-route-proof-key",
                "connection_ref": connection_ref,
                "deployment_ref": deployment_ref,
            },
        )
        assert mismatched.status_code == 400
        assert transport_calls == []

        db = SessionLocal()
        try:
            connection = db.get(
                LLMInferenceConnection,
                connection_ref["connection_id"],
            )
            assert connection is not None
            assert connection.user_id == user.id
            assert connection.legacy_default_provider is None
            credential = (
                db.query(UserLLMProviderCredential)
                .filter(
                    UserLLMProviderCredential.user_id == user.id,
                    UserLLMProviderCredential.provider == GPT_OSS_20B_PROVING_PRESET_ID,
                )
                .one()
            )
            assert credential.has_api_key
            assert credential.encrypted_api_key != "test-route-proof-key"
        finally:
            db.close()

        verified = client.post(
            f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection/test",
            headers=headers,
            json={
                "api_key": "test-route-proof-key",
                "connection_ref": connection_ref,
                "deployment_ref": deployment_ref,
            },
        )
        assert verified.status_code == 200, verified.text
        verified_body = verified.json()
        _assert_secret_safe_response(verified_body)
        assert verified_body["code"] == "verified"
        assert verified_body["usage"] == {
            "prompt_tokens": 4,
            "completion_tokens": 2,
            "total_tokens": 6,
        }

        pre_enable_selected = client.put(
            "/api/llm/selection",
            headers=headers,
            json={"deployment_ref": deployment_ref},
        )
        assert pre_enable_selected.status_code == 400

        enabled = client.post(
            f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection/enable",
            headers=headers,
            json={"connection_ref": connection_ref, "deployment_ref": deployment_ref},
        )
        assert enabled.status_code == 200, enabled.text
        enabled_body = enabled.json()
        _assert_secret_safe_response(enabled_body)
        assert enabled_body["lifecycle_state"] == "enabled"
        assert enabled_body["runnability"]["runnable"] is True

        selected = client.put(
            "/api/llm/selection",
            headers=headers,
            json={"deployment_ref": deployment_ref},
        )
        assert selected.status_code == 200, selected.text
        assert selected.json()["provider"] == "openai"
        assert selected.json()["model"] == "gpt-oss-20b"

    db = SessionLocal()
    try:
        runtime_selection = LLMProviderSelectionService(
            db
        ).build_deployment_runtime_selection(user_id=user.id)
        resolver = LLMRuntimeClientResolver(LLMCredentialService(db), db=db)
        runtime_client = resolver.get_client(
            runtime_selection,
            access_context=LLMRuntimeAccessContext(
                runtime_user_id=user.id,
                task_id=task.id,
                tenant_id=task.tenant_id,
            ),
            purpose="gpt-oss-route-proof",
        )
        assert runtime_client.model == "openai/gpt-oss-20b"
    finally:
        db.close()

    assert [call["operation"] for call in transport_calls] == [
        LLMConnectionOperation.INVENTORY,
        LLMConnectionOperation.CAPABILITY_PROBE,
    ]
    quote = get_pricing_quote(ProviderModelRef("openai", "gpt-oss-20b"))
    assert quote.status == PRICING_UNAVAILABLE
    assert quote.schedule is None


def test_gpt_oss_enable_rejects_credential_rotation_after_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rotated stored credential must pass verification before enablement."""

    monkeypatch.setenv(GPT_OSS_20B_PROVING_BASE_URL_ENV, "https://gpt-oss.example.test")

    def fake_execute(self, operation, provider, secret, json_body=None):
        del self, json_body
        assert provider == GPT_OSS_20B_PROVING_PRESET_ID
        assert secret.value == "stored-route-proof-key"
        if LLMConnectionOperation(operation) is LLMConnectionOperation.INVENTORY:
            return GuardedHTTPResponse(
                status_code=200,
                body=json.dumps({"data": [{"id": "openai/gpt-oss-20b"}]}).encode(),
                audit_id="inventory-audit",
            )
        return GuardedHTTPResponse(
            status_code=200,
            body=json.dumps(
                {
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {
                        "prompt_tokens": 4,
                        "completion_tokens": 2,
                        "total_tokens": 6,
                    },
                }
            ).encode(),
            audit_id="probe-audit",
        )

    monkeypatch.setattr(
        "backend.services.llm_provider.guarded_transport.GuardedTransport.execute",
        fake_execute,
    )

    db = SessionLocal()
    try:
        user, _task = _seed_user_task(
            db,
            username=_unique_username("gpt-oss-rotation-proof"),
        )
        headers = _auth_header_for(user)
    finally:
        db.close()

    with TestClient(app) as client:
        created = client.post(
            f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection",
            headers=headers,
            json={"api_key": "stored-route-proof-key"},
        )
        assert created.status_code == 200, created.text
        connection_ref = created.json()["connection_ref"]
        deployment_ref = created.json()["deployment_ref"]

        verified = client.post(
            f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection/test",
            headers=headers,
            json={
                "api_key": "stored-route-proof-key",
                "connection_ref": connection_ref,
                "deployment_ref": deployment_ref,
            },
        )
        assert verified.status_code == 200, verified.text

        db = SessionLocal()
        try:
            LLMCredentialService(db).upsert_connection_api_key(
                user_id=user.id,
                connection_ref=LLMConnectionCredentialRef(
                    connection_id=connection_ref["connection_id"],
                    expected_revision=connection_ref["expected_revision"],
                ),
                provider=GPT_OSS_20B_PROVING_PRESET_ID,
                api_key="rotated-route-proof-key",
            )
            connection = db.get(
                LLMInferenceConnection,
                connection_ref["connection_id"],
            )
            assert connection is not None
            rotated_ref = {
                "connection_id": str(connection.id),
                "expected_revision": int(connection.revision),
            }
            db.commit()
        finally:
            db.close()

        enabled = client.post(
            f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection/enable",
            headers=headers,
            json={"connection_ref": rotated_ref, "deployment_ref": deployment_ref},
        )
        assert enabled.status_code == 400


@pytest.mark.skipif(_missing_e2e_reason() is not None, reason=_missing_e2e_reason())
@pytest.mark.asyncio
async def test_real_gpt_oss_proving_endpoint_opt_in_e2e() -> None:
    """Use the configured real endpoint only when explicitly opted in."""

    api_key = os.environ[GPT_OSS_20B_PROVING_API_KEY_ENV]
    endpoint_base_url = os.environ[GPT_OSS_20B_PROVING_BASE_URL_ENV]
    db = SessionLocal()
    try:
        user, task = _seed_user_task(
            db,
            username=_unique_username("gpt-oss-real-proof"),
        )
        headers = _auth_header_for(user)
    finally:
        db.close()

    with TestClient(app) as client:
        created = client.post(
            f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection",
            headers=headers,
            json={"api_key": api_key},
        )
        assert created.status_code == 200, created.text
        created_body = created.json()
        _assert_secret_safe_response(
            created_body,
            secret=api_key,
            endpoint_base_url=endpoint_base_url,
        )
        assert created_body["lifecycle_state"] == "draft"
        assert created_body["verification"]["code"] == "not_tested"
        connection_ref = created_body["connection_ref"]
        deployment_ref = created_body["deployment_ref"]
        verified = client.post(
            f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection/test",
            headers=headers,
            json={
                "api_key": api_key,
                "connection_ref": connection_ref,
                "deployment_ref": deployment_ref,
            },
        )
        assert verified.status_code == 200, verified.text
        verified_body = verified.json()
        _assert_secret_safe_response(
            verified_body,
            secret=api_key,
            endpoint_base_url=endpoint_base_url,
        )
        assert verified_body["code"] == "verified"
        assert verified_body["model_present"] is True
        assert verified_body["usage"]["prompt_tokens"] > 0
        assert verified_body["usage"]["completion_tokens"] >= 0
        assert verified_body["usage"]["total_tokens"] >= (
            verified_body["usage"]["prompt_tokens"]
            + verified_body["usage"]["completion_tokens"]
        )
        enabled = client.post(
            f"/api/llm/proving-presets/{GPT_OSS_20B_PROVING_PRESET_ID}/connection/enable",
            headers=headers,
            json={"connection_ref": connection_ref, "deployment_ref": deployment_ref},
        )
        assert enabled.status_code == 200, enabled.text
        enabled_body = enabled.json()
        _assert_secret_safe_response(
            enabled_body,
            secret=api_key,
            endpoint_base_url=endpoint_base_url,
        )
        assert enabled_body["lifecycle_state"] == "enabled"
        assert enabled_body["runnability"]["runnable"] is True
        selected = client.put(
            "/api/llm/selection",
            headers=headers,
            json={"deployment_ref": deployment_ref},
        )
        assert selected.status_code == 200, selected.text
        selected_body = selected.json()
        _assert_secret_safe_response(
            selected_body,
            secret=api_key,
            endpoint_base_url=endpoint_base_url,
        )
        assert selected_body["provider"] == "openai"
        assert selected_body["model"] == "gpt-oss-20b"
        assert selected_body["deployment_ref"] == deployment_ref

    db = SessionLocal()
    try:
        runtime_selection = LLMProviderSelectionService(
            db
        ).build_deployment_runtime_selection(user_id=user.id)
        runtime_client = LLMRuntimeClientResolver(
            LLMCredentialService(db),
            db=db,
        ).get_client(
            runtime_selection,
            access_context=LLMRuntimeAccessContext(
                runtime_user_id=user.id,
                task_id=task.id,
                tenant_id=task.tenant_id,
            ),
            purpose="gpt-oss-real-proof",
        )
        assert runtime_client.model == "openai/gpt-oss-20b"
        with pytest.raises(LLMConfigurationError, match="max_tokens=0"):
            await runtime_client.chat_messages_with_usage(
                [{"role": "user", "content": "Reply with ok."}],
                max_tokens=0,
            )
        response = await runtime_client.chat_messages_with_usage(
            [{"role": "user", "content": "Reply with ok."}],
            max_tokens=4,
        )
        assert response.content
        assert response.usage is not None
        assert response.usage.prompt_tokens > 0
        assert response.usage.completion_tokens >= 0
        assert response.usage.total_tokens > 0
        quote = get_pricing_quote(ProviderModelRef("openai", "gpt-oss-20b"))
        assert quote.status == PRICING_UNAVAILABLE
        assert quote.schedule is None
        if _env_enabled(GPT_OSS_20B_PROVING_STREAMING_ENV):
            chunks = [
                chunk
                async for chunk in runtime_client.stream_chat_messages(
                    [{"role": "user", "content": "Reply with ok."}],
                    max_tokens=4,
                )
            ]
            assert "".join(chunks).strip()
        with pytest.raises(LLMConfigurationError, match="streaming usage"):
            await runtime_client.stream_chat_messages_with_usage(
                [{"role": "user", "content": "Reply with ok."}],
                max_tokens=4,
            )
    finally:
        db.close()
