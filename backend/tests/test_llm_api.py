"""Tests for LLM selection, model catalog, and runtime model switching routes."""

from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from backend.main import app
from backend.database import SessionLocal
from backend.models.core import User, UserSettings, Task
from backend.models.llm import (
    LLMConversation,
    UserEmbeddingSelection,
    UserLLMSelection,
    UserMemoryLLMSelection,
)
from backend.models.tenant import Tenant, TenantMembership
from backend.routers.settings import get_user_openai_model, is_supported_openai_model
from backend.services.llm_provider import (
    LLMCredentialService,
    LLMProviderSelectionService,
    ProviderConfigurationError,
    ProviderHealthCheckResult,
)
from agent.providers.llm.adapters.openai.responses.client import OpenAIResponsesClient
from agent.providers.llm.core.identity import ANTHROPIC_PROVIDER_ID
from agent.providers.llm.factory import LLMClientFactory
from agent.providers.llm.profiles import (
    ANTHROPIC_DEFAULT_MODEL_ID,
    ANTHROPIC_LISTABLE_MODEL_IDS,
    OPENAI_DEFAULT_MODEL_ID,
    list_catalog_model_profiles,
)
from backend.services.embeddings import (
    DEFAULT_MEMORY_EXTRACTION_MODEL,
    DEFAULT_MEMORY_GATE_MODEL,
    DEFAULT_OPENAI_EMBEDDING_MODEL,
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _ensure_user_task_and_settings(db, username="llmtester"):
    user = db.query(User).filter(User.username == username).first()
    if not user:
        user = User(username=username, password="x", email=f"{username}@example.com")
        db.add(user)
        db.commit()
        db.refresh(user)

    settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
    if not settings:
        settings = UserSettings(user_id=user.id, openai_model="gpt-5.2")
        db.add(settings)
        db.commit()
        db.refresh(settings)

    membership = (
        db.query(TenantMembership)
        .filter(
            TenantMembership.user_id == user.id,
            TenantMembership.status == "active",
        )
        .order_by(TenantMembership.id.asc())
        .first()
    )
    if membership:
        tenant_id = membership.tenant_id
    else:
        tenant = Tenant(
            slug=f"llm-test-{username[:24]}-{uuid4().hex[:12]}",
            name=f"{username} tenant",
        )
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        membership = TenantMembership(
            tenant_id=tenant.id,
            user_id=user.id,
            role="owner",
            status="active",
        )
        db.add(membership)
        db.commit()
        tenant_id = tenant.id

    task = db.query(Task).filter(Task.user_id == user.id).first()
    if not task:
        task = Task(user_id=user.id, tenant_id=tenant_id, name=f"{username}-task")
        db.add(task)
        db.commit()
        db.refresh(task)
    elif task.tenant_id != tenant_id:
        task.tenant_id = tenant_id
        db.add(task)
        db.commit()
        db.refresh(task)

    return user, settings, task


def _auth_header_for(user):
    from backend.auth import create_access_token
    token = create_access_token({"sub": user.username, "user_id": user.id})
    return {"Authorization": f"Bearer {token}"}


def _unique_username(prefix):
    return f"{prefix}-{uuid4().hex}"


def _clear_task_conversations(db, task, user):
    db.query(LLMConversation).filter(
        LLMConversation.task_id == task.id,
        LLMConversation.user_id == user.id,
    ).delete()
    db.commit()


def _seed_shared_tenant_conversation(db):
    owner = User(
        username=_unique_username("llm-tenant-owner"),
        password="x",
        email=f"{uuid4().hex}@example.com",
    )
    peer = User(
        username=_unique_username("llm-tenant-peer"),
        password="x",
        email=f"{uuid4().hex}@example.com",
    )
    foreign = User(
        username=_unique_username("llm-tenant-foreign"),
        password="x",
        email=f"{uuid4().hex}@example.com",
    )
    db.add_all([owner, peer, foreign])
    db.commit()
    db.refresh(owner)
    db.refresh(peer)
    db.refresh(foreign)

    for user in (owner, peer, foreign):
        db.add(UserSettings(user_id=user.id, openai_model="gpt-5.2"))
    db.commit()

    tenant_shared = Tenant(
        slug=f"tenant-shared-{uuid4().hex[:12]}",
        name="Shared Tenant",
    )
    tenant_foreign = Tenant(
        slug=f"tenant-foreign-{uuid4().hex[:12]}",
        name="Foreign Tenant",
    )
    db.add_all([tenant_shared, tenant_foreign])
    db.commit()
    db.refresh(tenant_shared)
    db.refresh(tenant_foreign)

    db.add_all(
        [
            TenantMembership(tenant_id=tenant_shared.id, user_id=owner.id, role="owner", status="active"),
            TenantMembership(tenant_id=tenant_shared.id, user_id=peer.id, role="operator", status="active"),
            TenantMembership(tenant_id=tenant_foreign.id, user_id=foreign.id, role="owner", status="active"),
        ]
    )
    db.commit()

    task = Task(user_id=owner.id, tenant_id=tenant_shared.id, name="shared-tenant-task")
    db.add(task)
    db.commit()
    db.refresh(task)

    row = LLMConversation(
        task_id=task.id,
        tenant_id=tenant_shared.id,
        user_id=owner.id,
        provider="openai",
        model="gpt-5.2",
        conversation_id="shared-conv-1",
        title="Shared tenant conversation",
        status="active",
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    return {
        "owner": owner,
        "peer": peer,
        "foreign": foreign,
        "shared_tenant_id": int(tenant_shared.id),
        "foreign_tenant_id": int(tenant_foreign.id),
        "task": task,
        "row": row,
    }


def _deny_remote_lifecycle(_self, provider_id):
    raise ProviderConfigurationError(
        f"Provider {provider_id} does not support remote conversation lifecycle"
    )


def _patch_openai_conversation_client(monkeypatch, calls, remote_id="remote-conv-1"):
    def fake_create(api_key):
        calls.append(("client", api_key))
        calls.append(("create",))
        return remote_id

    def fake_delete(api_key, conversation_id):
        calls.append(("client", api_key))
        calls.append(("delete", conversation_id))

    monkeypatch.setattr(
        "backend.services.llm_provider.conversation_lifecycle_service."
        "LLMConversationLifecycleService._create_openai_conversation",
        staticmethod(fake_create),
    )
    monkeypatch.setattr(
        "backend.services.llm_provider.conversation_lifecycle_service."
        "LLMConversationLifecycleService._delete_openai_conversation",
        staticmethod(fake_delete),
    )


def _patch_conversation_manager(monkeypatch, calls, active_id=None):
    class FakeConversationManager:
        def __init__(self, task_id):
            self.task_id = task_id

        def get_active_conversation_id(self):
            calls.append(("get_active", self.task_id))
            return active_id

        def create_conversation(self, title=None):
            calls.append(("create_local", self.task_id, title))
            return "local-conv-1"

        def set_openai_conversation_id(self, conversation_id, openai_conversation_id):
            calls.append(
                ("set_openai", self.task_id, conversation_id, openai_conversation_id)
            )

        def reset_openai_conversation(self, conversation_id=None):
            calls.append(("reset_openai", self.task_id, conversation_id))

    monkeypatch.setattr(
        "agent.chat.conversation_manager.ConversationManager",
        FakeConversationManager,
    )


def test_list_models(client):
    db = SessionLocal()
    try:
        user, settings, task = _ensure_user_task_and_settings(db)
        headers = _auth_header_for(user)
        resp = client.get("/api/llm/models", headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "providers" in data
        assert [provider["id"] for provider in data["providers"][:2]] == ["openai", "anthropic"]
        openai_provider = next(p for p in data.get("providers", []) if p.get("id") == "openai")
        anthropic_provider = next(p for p in data.get("providers", []) if p.get("id") == ANTHROPIC_PROVIDER_ID)
        expected_models = [
            {"id": profile.ref.model, "label": profile.display_name}
            for profile in list_catalog_model_profiles()
        ]
        assert [
            {"id": model["id"], "label": model["label"]}
            for model in openai_provider["models"]
        ] == expected_models
        openai_model_ids = [model["id"] for model in openai_provider["models"]]
        assert {
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.4-nano",
            "gpt-5.5",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
        }.issubset(
            openai_model_ids
        )
        assert "gpt-5.4-pro" not in openai_model_ids
        assert "gpt-5.5-pro" not in openai_model_ids
        assert openai_provider["defaultModel"] == OPENAI_DEFAULT_MODEL_ID
        assert openai_provider["available"] is True
        assert openai_provider["selectable"] is True
        assert openai_provider["credential"]["has_api_key"] is False
        first_openai_model = openai_provider["models"][0]
        assert first_openai_model["apiSurface"] == "responses"
        assert "chat" in first_openai_model["capabilities"]
        assert "reasoning_effort" in first_openai_model["capabilities"]
        assert first_openai_model["contextWindowTokens"] == 128000
        assert first_openai_model["maxOutputTokens"] == 32000
        assert first_openai_model["visibleReasoningEfforts"] == ["low", "medium", "high"]
        assert first_openai_model["defaultReasoningEffort"] == "minimal"
        assert first_openai_model["defaultVisibleReasoningEffort"] == "medium"
        assert first_openai_model["toolChoiceModes"] == ["auto", "none", "required", "specific"]
        assert first_openai_model["structuredOutputStrategies"] == ["native_schema"]
        for provider in (openai_provider, anthropic_provider):
            for model in provider["models"]:
                assert model["pricingStatus"] == "available", model["id"]
        gpt55_model = next(model for model in openai_provider["models"] if model["id"] == "gpt-5.5")
        assert gpt55_model["visibleReasoningEfforts"] == ["low", "medium", "high", "xhigh"]
        assert gpt55_model["defaultReasoningEffort"] == "medium"
        gpt56_models = [
            model for model in openai_provider["models"] if model["id"].startswith("gpt-5.6-")
        ]
        assert len(gpt56_models) == 3
        for model in gpt56_models:
            assert model["contextWindowTokens"] == 1_050_000
            assert model["maxOutputTokens"] == 128_000
            assert model["reasoningEfforts"] == ["none", "low", "medium", "high", "xhigh", "max"]
            assert model["visibleReasoningEfforts"] == ["low", "medium", "high", "xhigh", "max"]
            assert model["defaultReasoningEffort"] == "medium"

        assert anthropic_provider["defaultModel"] == ANTHROPIC_DEFAULT_MODEL_ID
        assert anthropic_provider["available"] is True
        assert anthropic_provider["selectable"] is True
        assert anthropic_provider["credential"]["has_api_key"] is False
        first_anthropic_model = anthropic_provider["models"][0]
        anthropic_model_ids = [model["id"] for model in anthropic_provider["models"]]
        assert "claude-opus-4-8" in anthropic_model_ids
        assert "claude-fable-5" in anthropic_model_ids
        assert "claude-sonnet-5" in anthropic_model_ids
        assert "claude-mythos-5" not in anthropic_model_ids
        assert first_anthropic_model["apiSurface"] == "messages"
        assert "chat" in first_anthropic_model["capabilities"]
        assert "reasoning_effort" in first_anthropic_model["capabilities"]
        assert first_anthropic_model["contextWindowTokens"] == 1_000_000
        assert first_anthropic_model["maxOutputTokens"] == 128_000
        assert first_anthropic_model["reasoningEfforts"] == [
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ]
        assert first_anthropic_model["visibleReasoningEfforts"] == [
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ]
        assert first_anthropic_model["defaultReasoningEffort"] == "high"
        assert first_anthropic_model["defaultVisibleReasoningEffort"] == "high"
        assert first_anthropic_model["toolChoiceModes"] == ["auto", "none", "required", "specific"]
        assert first_anthropic_model["structuredOutputStrategies"] == ["prompt_parse"]
    finally:
        db.close()


def test_openai_settings_model_policy_is_profile_backed():
    assert is_supported_openai_model("GPT-5.2") is True
    assert is_supported_openai_model("gpt-5-preview") is True
    assert is_supported_openai_model("gpt-4o-mini") is False
    assert is_supported_openai_model("unknown-model") is False


def test_get_and_set_selection_without_api_key_reports_credential_missing(client):
    db = SessionLocal()
    try:
        user, settings, task = _ensure_user_task_and_settings(db)
        headers = _auth_header_for(user)

        # No API key set yet; preference writes should still succeed.
        resp = client.put("/api/llm/selection", headers=headers, json={"provider": "openai", "model": "gpt-5.2"})
        assert resp.status_code == 200, resp.text

        # Set API key via settings
        resp2 = client.put("/api/settings", headers=headers, json={"openai_api_key": "sk-test"})
        assert resp2.status_code in (200, 201), resp2.text

        # Now selection should succeed
        resp3 = client.put("/api/llm/selection", headers=headers, json={"provider": "openai", "model": "gpt-5"})
        assert resp3.status_code == 200, resp3.text
        data = resp3.json()
        assert data["model"] == "gpt-5"

        # And get should reflect it
        resp4 = client.get("/api/llm/selection", headers=headers)
        assert resp4.status_code == 200
        assert resp4.json()["model"] in ("gpt-5", "gpt-5.2")
        assert resp4.json()["selection_status"]["status"] == "selectable"
    finally:
        db.close()


def test_anthropic_selection_preference_write_does_not_require_credential(client):
    db = SessionLocal()
    try:
        user, _settings, _task = _ensure_user_task_and_settings(
            db,
            username=_unique_username("llmtester-anthropic-selection-no-credential"),
        )
        headers = _auth_header_for(user)
        model = ANTHROPIC_LISTABLE_MODEL_IDS[0]

        selected = client.put(
            "/api/llm/selection",
            headers=headers,
            json={"provider": ANTHROPIC_PROVIDER_ID, "model": model},
        )

        assert selected.status_code == 200, selected.text
        assert selected.json() == {"provider": ANTHROPIC_PROVIDER_ID, "model": model}

        read = client.get("/api/llm/selection", headers=headers)
        assert read.status_code == 200, read.text
        assert read.json()["provider"] == ANTHROPIC_PROVIDER_ID
        assert read.json()["model"] == model
        assert read.json()["selection_status"]["status"] == "credential_missing"
        assert read.json()["selection_status"]["selectable"] is True
        assert read.json()["selection_status"]["runnable"] is False
    finally:
        db.close()


def test_provider_credential_routes_mask_test_and_delete_openai_credential(client, monkeypatch):
    db = SessionLocal()
    try:
        user, _settings, _task = _ensure_user_task_and_settings(
            db,
            username=_unique_username("llmtester-provider-credential"),
        )
        headers = _auth_header_for(user)

        def fake_test_openai_key(api_key):
            assert api_key == "sk-route"
            return ProviderHealthCheckResult(
                provider="openai",
                status="success",
                message="OpenAI API key is valid",
                model_count=3,
            )

        monkeypatch.setattr(
            "backend.services.llm_provider.health_service.LLMProviderHealthService._test_openai_key",
            staticmethod(fake_test_openai_key),
        )

        missing = client.get("/api/llm/providers/openai/credential", headers=headers)
        assert missing.status_code == 200, missing.text
        assert missing.json()["has_api_key"] is False

        tested = client.post(
            "/api/llm/providers/openai/credential/test",
            headers=headers,
            json={"api_key": "sk-route"},
        )
        assert tested.status_code == 200, tested.text
        assert tested.json() == {
            "provider": "openai",
            "status": "success",
            "message": "OpenAI API key is valid",
            "model_count": 3,
        }

        disabled = client.put(
            "/api/llm/providers/openai/credential",
            headers=headers,
            json={"api_key": "sk-route", "enabled": False},
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["has_api_key"] is False
        db.expire_all()
        settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).one()
        assert settings.openai_api_key is None

        stored = client.put(
            "/api/llm/providers/openai/credential",
            headers=headers,
            json={"api_key": "sk-route"},
        )
        assert stored.status_code == 200, stored.text
        assert stored.json()["masked_api_key"] == "***"
        db.expire_all()
        settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).one()
        assert settings.openai_api_key
        assert settings.openai_api_key != "sk-route"

        deleted = client.delete("/api/llm/providers/openai/credential", headers=headers)
        assert deleted.status_code == 200, deleted.text
        assert deleted.json() == {"success": True}
        db.expire_all()
        settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).one()
        assert settings.openai_api_key is None

        after_delete = client.get("/api/llm/providers/openai/credential", headers=headers)
        assert after_delete.status_code == 200, after_delete.text
        assert after_delete.json()["has_api_key"] is False
    finally:
        db.close()


def test_provider_credential_routes_support_anthropic_credential(client, monkeypatch):
    db = SessionLocal()
    try:
        user, _settings, _task = _ensure_user_task_and_settings(
            db,
            username=_unique_username("llmtester-anthropic-credential"),
        )
        headers = _auth_header_for(user)

        def fake_test_anthropic_key(api_key):
            assert api_key == "sk-ant-route"
            return ProviderHealthCheckResult(
                provider=ANTHROPIC_PROVIDER_ID,
                status="success",
                message="Anthropic API key is valid",
                model_count=1,
            )

        monkeypatch.setattr(
            "backend.services.llm_provider.health_service.LLMProviderHealthService._test_anthropic_key",
            staticmethod(fake_test_anthropic_key),
        )

        tested = client.post(
            f"/api/llm/providers/{ANTHROPIC_PROVIDER_ID}/credential/test",
            headers=headers,
            json={"api_key": "sk-ant-route"},
        )
        assert tested.status_code == 200, tested.text
        assert tested.json()["provider"] == ANTHROPIC_PROVIDER_ID

        stored = client.put(
            f"/api/llm/providers/{ANTHROPIC_PROVIDER_ID}/credential",
            headers=headers,
            json={"api_key": "sk-ant-route"},
        )
        assert stored.status_code == 200, stored.text
        assert stored.json()["masked_api_key"] == "***"

        selected = client.put(
            "/api/llm/selection",
            headers=headers,
            json={
                "provider": ANTHROPIC_PROVIDER_ID,
                "model": ANTHROPIC_LISTABLE_MODEL_IDS[0],
            },
        )
        assert selected.status_code == 200, selected.text
        assert selected.json() == {
            "provider": ANTHROPIC_PROVIDER_ID,
            "model": ANTHROPIC_LISTABLE_MODEL_IDS[0],
        }
    finally:
        db.close()


def test_memory_dependency_selection_routes_disabled_by_default(client, monkeypatch):
    monkeypatch.delenv("ENABLE_SEMANTIC_MEMORY_RUNTIME", raising=False)
    db = SessionLocal()
    try:
        user, _settings, _task = _ensure_user_task_and_settings(
            db,
            username=_unique_username("llmtester-memory-disabled"),
        )
        headers = _auth_header_for(user)

        defaults = client.get("/api/llm/memory/selections", headers=headers)
        assert defaults.status_code == 404
        assert defaults.json() == {"detail": "Semantic memory is disabled"}

        embedding = client.put(
            "/api/llm/memory/embedding-selection",
            headers=headers,
            json={"provider": "openai", "model": DEFAULT_OPENAI_EMBEDDING_MODEL},
        )
        assert embedding.status_code == 404
        assert embedding.json() == {"detail": "Semantic memory is disabled"}

        memory_llm = client.put(
            "/api/llm/memory/llm-selection",
            headers=headers,
            json={
                "provider": "openai",
                "gate_model": "gpt-5-nano",
                "extraction_model": "gpt-5-mini",
            },
        )
        assert memory_llm.status_code == 404
        assert memory_llm.json() == {"detail": "Semantic memory is disabled"}
    finally:
        db.close()


def test_memory_dependency_selection_routes_persist_identity_without_credentials(
    client,
    monkeypatch,
):
    monkeypatch.setenv("ENABLE_SEMANTIC_MEMORY_RUNTIME", "true")
    db = SessionLocal()
    try:
        user, _settings, _task = _ensure_user_task_and_settings(
            db,
            username=_unique_username("llmtester-memory-selection"),
        )
        headers = _auth_header_for(user)

        defaults = client.get("/api/llm/memory/selections", headers=headers)
        assert defaults.status_code == 200, defaults.text
        default_body = defaults.json()
        assert default_body["embedding"] == {
            "id": default_body["embedding"]["id"],
            "user_id": user.id,
            "provider": "openai",
            "model": DEFAULT_OPENAI_EMBEDDING_MODEL,
            "dimensions": 1536,
            "vector_family": f"openai:{DEFAULT_OPENAI_EMBEDDING_MODEL}:1536",
            "created_at": default_body["embedding"]["created_at"],
            "updated_at": default_body["embedding"]["updated_at"],
        }
        assert default_body["memory_llm"]["provider"] == "openai"
        assert default_body["memory_llm"]["gate_model"] == DEFAULT_MEMORY_GATE_MODEL
        assert default_body["memory_llm"]["extraction_model"] == DEFAULT_MEMORY_EXTRACTION_MODEL
        assert default_body["embedding_provider"] == "openai"
        assert default_body["embedding_model"] == DEFAULT_OPENAI_EMBEDDING_MODEL
        assert (
            default_body["embedding_vector_family"]
            == f"openai:{DEFAULT_OPENAI_EMBEDDING_MODEL}:1536"
        )
        assert "credential" not in default_body["embedding"]
        assert "credential" not in default_body["memory_llm"]

        embedding = client.put(
            "/api/llm/memory/embedding-selection",
            headers=headers,
            json={"provider": "openai", "model": DEFAULT_OPENAI_EMBEDDING_MODEL},
        )
        assert embedding.status_code == 200, embedding.text
        assert embedding.json()["vector_family"] == f"openai:{DEFAULT_OPENAI_EMBEDDING_MODEL}:1536"

        memory_llm = client.put(
            "/api/llm/memory/llm-selection",
            headers=headers,
            json={
                "provider": "openai",
                "gate_model": "gpt-5-nano",
                "extraction_model": "gpt-5-mini",
            },
        )
        assert memory_llm.status_code == 200, memory_llm.text
        assert memory_llm.json()["provider"] == "openai"

        db.expire_all()
        assert (
            db.query(UserEmbeddingSelection)
            .filter(UserEmbeddingSelection.user_id == user.id)
            .one()
            .vector_family
            == f"openai:{DEFAULT_OPENAI_EMBEDDING_MODEL}:1536"
        )
        assert (
            db.query(UserMemoryLLMSelection)
            .filter(UserMemoryLLMSelection.user_id == user.id)
            .one()
            .extraction_model
            == "gpt-5-mini"
        )
    finally:
        db.close()


def test_memory_dependency_selection_routes_reject_unverified_providers(client, monkeypatch):
    monkeypatch.setenv("ENABLE_SEMANTIC_MEMORY_RUNTIME", "true")
    db = SessionLocal()
    try:
        user, _settings, _task = _ensure_user_task_and_settings(
            db,
            username=_unique_username("llmtester-memory-selection-reject"),
        )
        headers = _auth_header_for(user)

        embedding = client.put(
            "/api/llm/memory/embedding-selection",
            headers=headers,
            json={"provider": ANTHROPIC_PROVIDER_ID, "model": "claude-sonnet-4-5"},
        )
        assert embedding.status_code == 400

        chat_model_as_embedding = client.put(
            "/api/llm/memory/embedding-selection",
            headers=headers,
            json={"provider": "openai", "model": "gpt-5.2"},
        )
        assert chat_model_as_embedding.status_code == 400

        memory_llm = client.put(
            "/api/llm/memory/llm-selection",
            headers=headers,
            json={
                "provider": ANTHROPIC_PROVIDER_ID,
                "gate_model": "claude-sonnet-4-5",
                "extraction_model": "claude-sonnet-4-5",
            },
        )
        assert memory_llm.status_code == 400
    finally:
        db.close()


def test_switch_requires_api_key_and_model_allowed(client, monkeypatch):
    db = SessionLocal()
    try:
        user, settings, task = _ensure_user_task_and_settings(
            db,
            username=_unique_username("llmtester-switch"),
        )
        LLMCredentialService(db).delete(user_id=user.id, provider="openai")
        settings.openai_api_key = None
        db.add(settings)
        db.commit()
        headers = _auth_header_for(user)
        runtime_inputs = []

        async def _fake_append_and_signal(*args, **kwargs):
            runtime_inputs.append({"args": args, "kwargs": kwargs})
            return SimpleNamespace(
                persisted=True,
                signal_sent=False,
                detail="signal not available in test environment",
            )

        monkeypatch.setattr(
            "backend.routers.llm._runtime_input_service",
            SimpleNamespace(append_and_signal=_fake_append_and_signal),
        )

        # Without API key -> 400
        resp = client.post(f"/api/llm/tasks/{task.id}/switch", headers=headers, json={"model": "gpt-5.2"})
        assert resp.status_code == 400

        # With API key
        client.put("/api/settings", headers=headers, json={"openai_api_key": "sk-test"})

        # Unknown model -> 400
        resp2 = client.post(f"/api/llm/tasks/{task.id}/switch", headers=headers, json={"model": "unknown-model"})
        assert resp2.status_code == 400

        # Allowed model -> 200 or 202 (signal may fail in tests, but endpoint should respond OK with signal_sent False)
        resp3 = client.post(f"/api/llm/tasks/{task.id}/switch", headers=headers, json={"model": "gpt-5.2"})
        assert resp3.status_code in (200, 202), resp3.text
        data = resp3.json()
        assert "success" in data
        assert runtime_inputs == [
            {
                "args": (task.id,),
                "kwargs": {
                    "message": "__switch_model:gpt-5.2",
                    "strict_persistence": True,
                    "user_id": user.id,
                    "metadata": {
                        "type": "switch_llm",
                        "command": "switch_llm_model",
                        "provider": "openai",
                        "model": "gpt-5.2",
                        "credential_ref": {
                            "user_id": user.id,
                            "provider": "openai",
                        },
                    },
                },
            }
        ]
    finally:
        db.close()


def test_switch_accepts_explicit_anthropic_provider_model(client, monkeypatch):
    db = SessionLocal()
    try:
        user, _settings, task = _ensure_user_task_and_settings(
            db,
            username=_unique_username("llmtester-switch-anthropic"),
        )
        LLMCredentialService(db).upsert_api_key(
            user_id=user.id,
            provider=ANTHROPIC_PROVIDER_ID,
            api_key="sk-ant-switch",
        )
        db.commit()
        headers = _auth_header_for(user)
        runtime_inputs = []

        async def _fake_append_and_signal(*args, **kwargs):
            runtime_inputs.append({"args": args, "kwargs": kwargs})
            return SimpleNamespace(
                persisted=True,
                signal_sent=False,
                detail="signal not available in test environment",
            )

        monkeypatch.setattr(
            "backend.routers.llm._runtime_input_service",
            SimpleNamespace(append_and_signal=_fake_append_and_signal),
        )

        model = ANTHROPIC_LISTABLE_MODEL_IDS[0]
        resp = client.post(
            f"/api/llm/tasks/{task.id}/switch",
            headers=headers,
            json={"provider": ANTHROPIC_PROVIDER_ID, "model": model},
        )

        assert resp.status_code in (200, 202), resp.text
        assert runtime_inputs[0]["kwargs"]["metadata"] == {
            "type": "switch_llm",
            "command": "switch_llm_model",
            "provider": ANTHROPIC_PROVIDER_ID,
            "model": model,
            "credential_ref": {
                "user_id": user.id,
                "provider": ANTHROPIC_PROVIDER_ID,
            },
        }
    finally:
        db.close()


def test_switch_uses_saved_anthropic_provider_when_provider_omitted(client, monkeypatch):
    db = SessionLocal()
    try:
        user, _settings, task = _ensure_user_task_and_settings(
            db,
            username=_unique_username("llmtester-switch-saved-anthropic"),
        )
        credential_service = LLMCredentialService(db)
        credential_service.upsert_api_key(
            user_id=user.id,
            provider=ANTHROPIC_PROVIDER_ID,
            api_key="sk-ant-switch",
        )
        model = ANTHROPIC_LISTABLE_MODEL_IDS[0]
        LLMProviderSelectionService(db, credential_service=credential_service).set_selection(
            user_id=user.id,
            provider=ANTHROPIC_PROVIDER_ID,
            model=model,
        )
        db.commit()
        headers = _auth_header_for(user)
        runtime_inputs = []

        async def _fake_append_and_signal(*args, **kwargs):
            runtime_inputs.append({"args": args, "kwargs": kwargs})
            return SimpleNamespace(
                persisted=True,
                signal_sent=False,
                detail="signal not available in test environment",
            )

        monkeypatch.setattr(
            "backend.routers.llm._runtime_input_service",
            SimpleNamespace(append_and_signal=_fake_append_and_signal),
        )

        resp = client.post(
            f"/api/llm/tasks/{task.id}/switch",
            headers=headers,
            json={"model": model},
        )

        assert resp.status_code in (200, 202), resp.text
        assert runtime_inputs[0]["kwargs"]["metadata"] == {
            "type": "switch_llm",
            "command": "switch_llm_model",
            "provider": ANTHROPIC_PROVIDER_ID,
            "model": model,
            "credential_ref": {
                "user_id": user.id,
                "provider": ANTHROPIC_PROVIDER_ID,
            },
        }
    finally:
        db.close()


def test_switch_rejects_unavailable_provider_without_openai_fallback(client, monkeypatch):
    original_provider_registry = LLMClientFactory._provider_registry.copy()
    original_prefix_registry = LLMClientFactory._registry.copy()
    db = SessionLocal()
    try:
        LLMClientFactory.clear_registry()
        LLMClientFactory.register_provider("openai", OpenAIResponsesClient)
        user, _settings, task = _ensure_user_task_and_settings(
            db,
            username=_unique_username("llmtester-switch-unavailable-provider"),
        )
        LLMCredentialService(db).upsert_api_key(
            user_id=user.id,
            provider="openai",
            api_key="sk-switch",
        )
        model = ANTHROPIC_LISTABLE_MODEL_IDS[0]
        db.add(
            UserLLMSelection(
                user_id=user.id,
                provider=ANTHROPIC_PROVIDER_ID,
                model=model,
            )
        )
        db.commit()
        headers = _auth_header_for(user)
        runtime_inputs = []

        async def _fake_append_and_signal(*args, **kwargs):
            runtime_inputs.append({"args": args, "kwargs": kwargs})
            return SimpleNamespace(
                persisted=True,
                signal_sent=False,
                detail="signal not available in test environment",
            )

        monkeypatch.setattr(
            "backend.routers.llm._runtime_input_service",
            SimpleNamespace(append_and_signal=_fake_append_and_signal),
        )

        explicit_unavailable = client.post(
            f"/api/llm/tasks/{task.id}/switch",
            headers=headers,
            json={"provider": ANTHROPIC_PROVIDER_ID, "model": model},
        )
        assert explicit_unavailable.status_code == 400
        assert "adapter is not registered" in explicit_unavailable.text

        saved_unavailable = client.post(
            f"/api/llm/tasks/{task.id}/switch",
            headers=headers,
            json={"model": model},
        )
        assert saved_unavailable.status_code == 400
        assert "adapter is not registered" in saved_unavailable.text

        explicit_openai = client.post(
            f"/api/llm/tasks/{task.id}/switch",
            headers=headers,
            json={"provider": "openai", "model": "gpt-5.2"},
        )
        assert explicit_openai.status_code in (200, 202), explicit_openai.text
        assert runtime_inputs == [
            {
                "args": (task.id,),
                "kwargs": {
                    "message": "__switch_model:gpt-5.2",
                    "strict_persistence": True,
                    "user_id": user.id,
                    "metadata": {
                        "type": "switch_llm",
                        "command": "switch_llm_model",
                        "provider": "openai",
                        "model": "gpt-5.2",
                        "credential_ref": {
                            "user_id": user.id,
                            "provider": "openai",
                        },
                    },
                },
            }
        ]
    finally:
        db.close()
        LLMClientFactory._provider_registry = original_provider_registry
        LLMClientFactory._registry = original_prefix_registry


def test_legacy_user_model_is_auto_migrated_to_gpt5_default():
    db = SessionLocal()
    try:
        user, settings, task = _ensure_user_task_and_settings(
            db,
            username=_unique_username("llmtester-legacy-model"),
        )
        db.query(UserLLMSelection).filter(UserLLMSelection.user_id == user.id).delete()
        settings.openai_model = "gpt-4o-mini"
        db.add(settings)
        db.commit()
        db.refresh(settings)

        resolved_model = get_user_openai_model(user.id, db)
        assert resolved_model == OPENAI_DEFAULT_MODEL_ID

        db.refresh(settings)
        assert settings.openai_model == OPENAI_DEFAULT_MODEL_ID
    finally:
        db.close()


def test_selection_get_reconciles_legacy_user_model(client):
    db = SessionLocal()
    try:
        user, settings, task = _ensure_user_task_and_settings(
            db,
            username=_unique_username("llmtester-selection-legacy"),
        )
        db.query(UserLLMSelection).filter(UserLLMSelection.user_id == user.id).delete()
        settings.openai_model = "gpt-4o-mini"
        db.add(settings)
        db.commit()
        headers = _auth_header_for(user)

        resp = client.get("/api/llm/selection", headers=headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["provider"] == "openai"
        assert resp.json()["model"] == OPENAI_DEFAULT_MODEL_ID
        assert resp.json()["selection_status"]["status"] == "credential_missing"
        db.refresh(settings)
        assert settings.openai_model == OPENAI_DEFAULT_MODEL_ID
    finally:
        db.close()


def test_selection_get_rejects_invalid_provider_neutral_row(client):
    db = SessionLocal()
    try:
        user, _settings, _task = _ensure_user_task_and_settings(
            db,
            username=_unique_username("llmtester-invalid-selection"),
        )
        db.add(
            UserLLMSelection(
                user_id=user.id,
                provider="openai",
                model="gpt-4o-mini",
            )
        )
        db.commit()
        headers = _auth_header_for(user)

        resp = client.get("/api/llm/selection", headers=headers)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["provider"] == "openai"
        assert body["model"] == "gpt-4o-mini"
        assert body["selection_status"]["status"] == "model_unavailable"
        assert body["selection_status"]["runnable"] is False
        assert "Only OpenAI GPT-5 Responses models are selectable" in body["selection_status"]["reason"]
    finally:
        db.close()


def test_create_conversation_keeps_openai_lifecycle_behavior(client, monkeypatch):
    db = SessionLocal()
    try:
        user, _settings, task = _ensure_user_task_and_settings(
            db,
            username=_unique_username("llmtester-create-conv"),
        )
        _clear_task_conversations(db, task, user)
        LLMCredentialService(db).upsert_api_key(
            user_id=user.id,
            provider="openai",
            api_key="sk-test",
        )
        db.commit()
        headers = _auth_header_for(user)

        openai_calls = []
        mirror_calls = []
        _patch_openai_conversation_client(monkeypatch, openai_calls)
        _patch_conversation_manager(monkeypatch, mirror_calls)

        resp = client.post(
            f"/api/llm/tasks/{task.id}/conversations",
            headers=headers,
            json={"title": "Phase 6", "model": "gpt-5.2"},
        )

        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["provider"] == "openai"
        assert data["conversation_id"] == "remote-conv-1"
        assert data["is_active"] is True
        assert openai_calls == [("client", "sk-test"), ("create",)]
        assert mirror_calls == [
            ("get_active", task.id),
            ("create_local", task.id, "Phase 6"),
            ("set_openai", task.id, "local-conv-1", "remote-conv-1"),
        ]
    finally:
        db.close()


def test_create_conversation_fails_before_openai_sdk_without_capability(
    client,
    monkeypatch,
):
    db = SessionLocal()
    try:
        user, _settings, task = _ensure_user_task_and_settings(
            db,
            username=_unique_username("llmtester-create-gated"),
        )
        headers = _auth_header_for(user)
        sdk_calls = []
        monkeypatch.setattr(
            "backend.services.llm_provider.conversation_lifecycle_service."
            "LLMConversationLifecycleService._require_remote_lifecycle_provider",
            _deny_remote_lifecycle,
        )
        monkeypatch.setattr(
            "backend.services.llm_provider.conversation_lifecycle_service."
            "LLMConversationLifecycleService._create_openai_conversation",
            staticmethod(lambda *_args: sdk_calls.append("sdk")),
        )

        resp = client.post(
            f"/api/llm/tasks/{task.id}/conversations",
            headers=headers,
            json={"title": "blocked"},
        )

        assert resp.status_code == 501, resp.text
        assert sdk_calls == []
    finally:
        db.close()


def test_reset_conversation_fails_before_openai_side_effects_without_capability(
    client,
    monkeypatch,
):
    db = SessionLocal()
    try:
        user, _settings, task = _ensure_user_task_and_settings(
            db,
            username=_unique_username("llmtester-reset-gated"),
        )
        _clear_task_conversations(db, task, user)
        row = LLMConversation(
            task_id=task.id,
            tenant_id=task.tenant_id,
            user_id=user.id,
            provider="openai",
            model="gpt-5.2",
            conversation_id="remote-conv-reset",
            status="active",
            is_active=True,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        headers = _auth_header_for(user)

        mirror_calls = []
        signal_calls = []

        async def fake_append_and_signal(*_args, **_kwargs):
            signal_calls.append("signal")
            return SimpleNamespace(persisted=True, signal_sent=True, detail="")

        monkeypatch.setattr(
            "backend.services.llm_provider.conversation_lifecycle_service."
            "LLMConversationLifecycleService._require_remote_lifecycle_provider",
            _deny_remote_lifecycle,
        )
        _patch_conversation_manager(monkeypatch, mirror_calls)
        monkeypatch.setattr(
            "backend.routers.llm._runtime_input_service",
            SimpleNamespace(append_and_signal=fake_append_and_signal),
        )

        resp = client.post(
            f"/api/llm/tasks/{task.id}/conversation/reset",
            headers=headers,
        )

        assert resp.status_code == 501, resp.text
        assert mirror_calls == []
        assert signal_calls == []
        db.refresh(row)
        assert row.conversation_id == "remote-conv-reset"
        assert row.status == "active"
    finally:
        db.close()


def test_non_openai_conversation_rows_do_not_trigger_openai_side_effects(
    client,
    monkeypatch,
):
    db = SessionLocal()
    try:
        user, _settings, task = _ensure_user_task_and_settings(
            db,
            username=_unique_username("llmtester-nonopenai"),
        )
        _clear_task_conversations(db, task, user)
        row = LLMConversation(
            task_id=task.id,
            tenant_id=task.tenant_id,
            user_id=user.id,
            provider="anthropic",
            model="claude-test",
            conversation_id="anthropic-conv",
            status="active",
            is_active=False,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        headers = _auth_header_for(user)

        mirror_calls = []
        _patch_conversation_manager(monkeypatch, mirror_calls)

        activate = client.put(
            f"/api/llm/tasks/{task.id}/conversations/{row.id}/activate",
            headers=headers,
        )
        delete = client.delete(
            f"/api/llm/tasks/{task.id}/conversations/{row.id}",
            headers=headers,
        )

        assert activate.status_code == 501, activate.text
        assert "does not support remote conversation lifecycle" in activate.json()["detail"]
        assert delete.status_code == 501, delete.text
        assert "does not support remote conversation lifecycle" in delete.json()["detail"]
        assert mirror_calls == []
        db.refresh(row)
        assert row.is_active is False
        assert row.status == "active"
        assert (
            db.query(LLMConversation)
            .filter(LLMConversation.id == row.id)
            .one_or_none()
            is not None
        )
    finally:
        db.close()


def test_task_conversation_hides_same_tenant_non_owner_user(client):
    db = SessionLocal()
    try:
        seeded = _seed_shared_tenant_conversation(db)
        headers = _auth_header_for(seeded["peer"])
        headers["X-Active-Tenant-Id"] = str(seeded["shared_tenant_id"])

        resp = client.get(
            f"/api/llm/tasks/{seeded['task'].id}/conversation",
            headers=headers,
        )

        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Task not found"
    finally:
        db.close()


def test_task_conversation_write_denied_for_same_tenant_viewer_role(client):
    db = SessionLocal()
    try:
        seeded = _seed_shared_tenant_conversation(db)
        membership = (
            db.query(TenantMembership)
            .filter(
                TenantMembership.tenant_id == seeded["shared_tenant_id"],
                TenantMembership.user_id == seeded["peer"].id,
            )
            .first()
        )
        assert membership is not None
        membership.role = "viewer"
        db.commit()

        headers = _auth_header_for(seeded["peer"])
        headers["X-Active-Tenant-Id"] = str(seeded["shared_tenant_id"])
        resp = client.delete(
            f"/api/llm/tasks/{seeded['task'].id}/conversations/{seeded['row'].id}",
            headers=headers,
        )

        assert resp.status_code == 403, resp.text
        assert "Tenant policy denied action" in resp.json()["detail"]
    finally:
        db.close()


def test_task_conversation_foreign_tenant_access_returns_not_found(client):
    db = SessionLocal()
    try:
        seeded = _seed_shared_tenant_conversation(db)
        headers = _auth_header_for(seeded["foreign"])
        headers["X-Active-Tenant-Id"] = str(seeded["foreign_tenant_id"])

        resp = client.get(
            f"/api/llm/tasks/{seeded['task'].id}/conversation",
            headers=headers,
        )

        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Task not found"
    finally:
        db.close()
