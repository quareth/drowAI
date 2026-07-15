"""Tests for Execution Plane LLM provider service boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from agent.providers.llm.contracts.tool_contracts import FunctionToolSpec
from agent.providers.llm.factory import LLMClientFactory
from agent.providers.llm.core.base import LLMResponse
from agent.providers.llm.core.exceptions import (
    LLMCapabilityNotSupportedError,
    LLMConfigurationError,
)
from agent.providers.llm.core.identity import ANTHROPIC_PROVIDER_ID, OPENAI_PROVIDER_ID, ProviderModelRef
from agent.providers.llm.adapters.openai.responses.client import OpenAIResponsesClient
from agent.providers.llm.profiles import (
    ANTHROPIC_LISTABLE_MODEL_IDS,
    OPENAI_DEFAULT_MODEL_ID,
    OPENAI_LISTABLE_MODEL_IDS,
)
from backend.database import SessionLocal
from backend.models import (
    Task,
    User,
    UserLLMProviderCredential,
    UserLLMSelection,
    UserMemoryLLMSelection,
    UserSettings,
)
from backend.services.container_utils import cache_api_key, get_cached_api_key
from backend.services.llm_provider import (
    CredentialAuthorizationError,
    CredentialNotFoundError,
    LLMCredentialRef,
    LLMCredentialService,
    LLMProviderCatalogService,
    LLMProviderEnvironmentService,
    LLMProviderHealthService,
    LLMProviderMigrationService,
    LLMProviderSelectionService,
    LLMRuntimeConfigService,
    LLMRuntimeServices,
    LLMRuntimeSelection,
    ProviderConfigurationError,
    ProviderHealthCheckResult,
    ProviderSecret,
    ReportingLLMSelectionMissingError,
    ReportingLLMSelectionService,
    attach_runtime_services,
    encrypt_api_key,
    strip_runtime_services,
)
from backend.services.llm_provider import runtime_client_resolver as resolver_module
from backend.services.llm_provider import reporting_selection_service as reporting_selection_module
from core.llm.role_policy import ROLE_TOOL_OUTPUT_COMPRESSOR


def _create_user(db, username_prefix: str = "llm-provider-svc") -> User:
    user = User(
        username=f"{username_prefix}-{uuid4().hex}",
        password="x",
        email=f"{uuid4().hex}@example.com",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_catalog_service_uses_tenant_baseline_profiles() -> None:
    catalog = LLMProviderCatalogService()

    providers = catalog.list_providers()

    openai_provider = next(provider for provider in providers if provider.id == OPENAI_PROVIDER_ID)
    anthropic_provider = next(provider for provider in providers if provider.id == ANTHROPIC_PROVIDER_ID)
    assert openai_provider.default_model == OPENAI_DEFAULT_MODEL_ID
    assert [model.id for model in openai_provider.models] == list(
        sorted(OPENAI_LISTABLE_MODEL_IDS)
    )
    assert [model.id for model in anthropic_provider.models] == list(
        sorted(ANTHROPIC_LISTABLE_MODEL_IDS)
    )
    assert catalog.require_selectable_model(OPENAI_PROVIDER_ID, "GPT-5.2").ref.model == "gpt-5.2"
    assert catalog.require_selectable_model(
        ANTHROPIC_PROVIDER_ID,
        ANTHROPIC_LISTABLE_MODEL_IDS[0],
    ).ref.model == ANTHROPIC_LISTABLE_MODEL_IDS[0]


def test_catalog_marks_listable_provider_unavailable_without_registered_adapter() -> None:
    original_provider_registry = LLMClientFactory._provider_registry.copy()
    original_prefix_registry = LLMClientFactory._registry.copy()
    try:
        LLMClientFactory.clear_registry()
        LLMClientFactory.register_provider(OPENAI_PROVIDER_ID, OpenAIResponsesClient)

        catalog = LLMProviderCatalogService()
        providers = catalog.list_providers()

        assert [provider.id for provider in providers] == [OPENAI_PROVIDER_ID, ANTHROPIC_PROVIDER_ID]
        anthropic_provider = next(provider for provider in providers if provider.id == ANTHROPIC_PROVIDER_ID)
        assert anthropic_provider.available is False
        assert anthropic_provider.selectable is False
        assert catalog.list_provider_models(ANTHROPIC_PROVIDER_ID) == ()
        with pytest.raises(ProviderConfigurationError, match="adapter is not registered"):
            catalog.require_selectable_model(
                ANTHROPIC_PROVIDER_ID,
                ANTHROPIC_LISTABLE_MODEL_IDS[0],
            )
    finally:
        LLMClientFactory._provider_registry = original_provider_registry
        LLMClientFactory._registry = original_prefix_registry


def test_credential_service_stores_encrypted_mirror_and_resolves_with_authorization() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db)
        other = _create_user(db, "llm-provider-other")
        task = Task(user_id=user.id, name="owned")
        db.add(task)
        db.commit()
        db.refresh(task)

        cache_api_key(user.id, "stale-key")
        status = LLMCredentialService(db).upsert_api_key(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            api_key="sk-provider-secret",
        )
        db.commit()

        credential = db.query(UserLLMProviderCredential).filter_by(user_id=user.id).one()
        settings = db.query(UserSettings).filter_by(user_id=user.id).one()
        assert status.masked_api_key == "***"
        assert credential.encrypted_api_key != "sk-provider-secret"
        assert credential.encrypted_api_key == settings.openai_api_key
        assert get_cached_api_key(user.id) is None

        service = LLMCredentialService(db)
        secret = service.resolve_secret(
            LLMCredentialRef(user_id=user.id, provider=OPENAI_PROVIDER_ID),
            runtime_user_id=user.id,
            task_id=task.id,
            purpose="test",
        )
        assert secret.value == "sk-provider-secret"

        with pytest.raises(CredentialAuthorizationError):
            service.resolve_secret(
                LLMCredentialRef(user_id=user.id, provider=OPENAI_PROVIDER_ID),
                runtime_user_id=other.id,
                task_id=None,
                purpose="forged",
            )
    finally:
        db.close()


def test_empty_legacy_openai_mirror_does_not_disable_provider_credential() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db)
        service = LLMCredentialService(db)
        service.upsert_api_key(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            api_key="sk-canonical",
        )
        db.commit()

        settings = db.query(UserSettings).filter_by(user_id=user.id).one()
        settings.openai_api_key = None
        db.commit()

        secret = service.resolve_secret(
            LLMCredentialRef(user_id=user.id, provider=OPENAI_PROVIDER_ID),
            runtime_user_id=user.id,
            task_id=None,
            purpose="canonical-read",
        )
        credential = db.query(UserLLMProviderCredential).filter_by(user_id=user.id).one()

        assert secret.value == "sk-canonical"
        assert credential.enabled is True
        assert credential.has_api_key is True
    finally:
        db.close()


def test_migration_service_copies_legacy_ciphertext_without_double_encrypting() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db)
        encrypted = encrypt_api_key("sk-legacy")
        settings = UserSettings(
            user_id=user.id,
            openai_api_key=encrypted,
            openai_model="gpt-5-mini",
        )
        db.add(settings)
        db.commit()

        LLMProviderMigrationService(db).backfill_legacy_openai_for_user(user.id)
        db.commit()

        credential = db.query(UserLLMProviderCredential).filter_by(user_id=user.id).one()
        selection = db.query(UserLLMSelection).filter_by(user_id=user.id).one()
        assert credential.encrypted_api_key == encrypted
        assert selection.provider == OPENAI_PROVIDER_ID
        assert selection.model == "gpt-5-mini"
        assert LLMCredentialService(db).get_openai_api_key_compat(user.id) == "sk-legacy"
    finally:
        db.close()


def test_selection_service_reconciles_legacy_invalid_model_to_current_default() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db)
        db.add(UserSettings(user_id=user.id, openai_model="gpt-4o-mini"))
        db.commit()

        model = LLMProviderSelectionService(db).get_openai_model_compat(user.id)
        db.commit()

        settings = db.query(UserSettings).filter_by(user_id=user.id).one()
        selection = db.query(UserLLMSelection).filter_by(user_id=user.id).one()
        assert model == OPENAI_DEFAULT_MODEL_ID
        assert settings.openai_model == OPENAI_DEFAULT_MODEL_ID
        assert selection.provider == OPENAI_PROVIDER_ID
        assert selection.model == OPENAI_DEFAULT_MODEL_ID
    finally:
        db.close()


def test_selection_service_rejects_invalid_provider_neutral_selection() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db)
        db.add(
            UserLLMSelection(
                user_id=user.id,
                provider=OPENAI_PROVIDER_ID,
                model="gpt-4o-mini",
            )
        )
        db.commit()

        with pytest.raises(ProviderConfigurationError):
            LLMProviderSelectionService(db).get_selection(user.id)
    finally:
        db.close()


def test_selection_read_preserves_unavailable_saved_provider_without_runtime_fallback() -> None:
    original_provider_registry = LLMClientFactory._provider_registry.copy()
    original_prefix_registry = LLMClientFactory._registry.copy()
    db = SessionLocal()
    try:
        LLMClientFactory.clear_registry()
        LLMClientFactory.register_provider(OPENAI_PROVIDER_ID, OpenAIResponsesClient)
        user = _create_user(db, "llm-provider-unavailable-selection")
        credential_service = LLMCredentialService(db)
        credential_service.upsert_api_key(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            api_key="sk-runtime",
        )
        saved_model = ANTHROPIC_LISTABLE_MODEL_IDS[0]
        db.add(
            UserLLMSelection(
                user_id=user.id,
                provider=ANTHROPIC_PROVIDER_ID,
                model=saved_model,
            )
        )
        db.commit()

        selection_service = LLMProviderSelectionService(
            db,
            credential_service=credential_service,
        )
        read = selection_service.get_selection_read(user.id)

        assert read.selection.provider == ANTHROPIC_PROVIDER_ID
        assert read.selection.model == saved_model
        assert read.status.status == "adapter_unavailable"
        assert read.status.selectable is False
        assert read.status.runnable is False

        runtime_service = LLMRuntimeConfigService(
            db,
            credential_service=credential_service,
            selection_service=selection_service,
        )
        with pytest.raises(ProviderConfigurationError, match="adapter is not registered"):
            runtime_service.build_runtime_selection(user_id=user.id)

        explicit_openai = runtime_service.build_runtime_selection(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            model="gpt-5.2",
        )
        assert explicit_openai.provider == OPENAI_PROVIDER_ID
        assert explicit_openai.model == "gpt-5.2"
        assert explicit_openai.credential_ref == LLMCredentialRef(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
        )

        with pytest.raises(ProviderConfigurationError, match="adapter is not registered"):
            runtime_service.build_runtime_selection(
                user_id=user.id,
                provider=ANTHROPIC_PROVIDER_ID,
                model=saved_model,
            )
    finally:
        db.close()
        LLMClientFactory._provider_registry = original_provider_registry
        LLMClientFactory._registry = original_prefix_registry


def test_runtime_config_returns_credential_ref_without_secret() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db)
        credential_service = LLMCredentialService(db)
        credential_service.upsert_api_key(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            api_key="sk-runtime",
        )
        LLMProviderSelectionService(db, credential_service=credential_service).set_selection(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            model="gpt-5.2",
        )
        db.commit()

        selection = LLMRuntimeConfigService(db).build_runtime_selection(user_id=user.id)

        payload = selection.to_dict()
        assert payload == {
            "provider": OPENAI_PROVIDER_ID,
            "model": "gpt-5.2",
            "credential_ref": {"user_id": user.id, "provider": OPENAI_PROVIDER_ID},
            "reasoning_effort": None,
        }
        assert "sk-runtime" not in repr(payload)
    finally:
        db.close()


def test_reporting_selection_requires_explicit_configuration_without_fallback() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db, "llm-provider-reporting-unset")
        service = ReportingLLMSelectionService(db)

        read = service.get_selection_read(user.id)

        assert read.selection is None
        assert read.status.status == "unset"
        assert read.status.runnable is False
        with pytest.raises(ReportingLLMSelectionMissingError):
            service.build_runtime_selection(user_id=user.id)
    finally:
        db.close()


def test_reporting_selection_is_offline_runnable_only_in_explicit_e2e_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reporting_selection_module, "E2E_DETERMINISTIC_MODE", True, raising=False)
    db = SessionLocal()
    try:
        user = _create_user(db, "llm-provider-reporting-e2e")
        service = ReportingLLMSelectionService(db)

        read = service.get_selection_read(user.id)
        selection = service.build_runtime_selection(user_id=user.id)

        assert read.selection is None
        assert read.status.status == "deterministic_e2e"
        assert read.status.runnable is True
        assert selection.provider == "deterministic_e2e"
        assert selection.model == "offline-report-section-v1"
        assert selection.credential_ref.user_id == user.id
    finally:
        db.close()


def test_reporting_selection_builds_runtime_selection_with_enabled_credential() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db, "llm-provider-reporting-runtime")
        credential_service = LLMCredentialService(db)
        credential_service.upsert_api_key(
            user_id=user.id,
            provider=ANTHROPIC_PROVIDER_ID,
            api_key="sk-ant-reporting",
        )
        model = ANTHROPIC_LISTABLE_MODEL_IDS[0]
        service = ReportingLLMSelectionService(
            db,
            credential_service=credential_service,
        )
        service.set_selection(
            user_id=user.id,
            provider=ANTHROPIC_PROVIDER_ID,
            model=model,
        )
        db.commit()

        selection = service.build_runtime_selection(user_id=user.id)

        assert selection.provider == ANTHROPIC_PROVIDER_ID
        assert selection.model == model
        assert selection.credential_ref == LLMCredentialRef(
            user_id=user.id,
            provider=ANTHROPIC_PROVIDER_ID,
        )
        assert "sk-ant-reporting" not in repr(selection.to_dict())
    finally:
        db.close()


def test_reporting_selection_runtime_requires_enabled_credential() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db, "llm-provider-reporting-no-credential")
        service = ReportingLLMSelectionService(db)
        service.set_selection(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            model="gpt-5.2",
        )
        db.commit()

        read = service.get_selection_read(user.id)

        assert read.status.status == "credential_missing"
        assert read.status.runnable is False
        with pytest.raises(ProviderConfigurationError):
            service.build_runtime_selection(user_id=user.id)
    finally:
        db.close()


def test_runtime_services_disable_semantic_memory_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENABLE_SEMANTIC_MEMORY_RUNTIME", raising=False)
    db = SessionLocal()
    try:
        services = LLMRuntimeConfigService(db).build_runtime_services()

        assert services.client_resolver is not None
        assert services.memory_runtime_service is None
    finally:
        db.close()


def test_runtime_services_resolve_persisted_memory_dependency_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_SEMANTIC_MEMORY_RUNTIME", "true")
    db = SessionLocal()
    try:
        user = _create_user(db, "llm-provider-memory-runtime")
        credential_service = LLMCredentialService(db)
        credential_service.upsert_api_key(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            api_key="sk-memory-runtime",
        )
        db.add(
            UserMemoryLLMSelection(
                user_id=user.id,
                provider=OPENAI_PROVIDER_ID,
                gate_model="gpt-5",
                extraction_model="gpt-5-mini",
            )
        )
        db.commit()

        services = LLMRuntimeConfigService(db).build_runtime_services()
        assert services.memory_runtime_service is not None
        memory_selection = services.memory_runtime_service._resolve_memory_llm_selection(
            runtime_user_id=user.id,
            db=db,
        )

        assert memory_selection is not None
        assert memory_selection.provider == OPENAI_PROVIDER_ID
        assert memory_selection.gate_model == "gpt-5"
        assert memory_selection.extraction_model == "gpt-5-mini"
        assert memory_selection.credential_ref == LLMCredentialRef(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
        )
    finally:
        db.close()


def test_runtime_client_resolver_uses_explicit_role_target(monkeypatch: pytest.MonkeyPatch) -> None:
    db = SessionLocal()
    calls: list[dict[str, Any]] = []
    try:
        user = _create_user(db)
        service = LLMCredentialService(db)
        service.upsert_api_key(user_id=user.id, provider=OPENAI_PROVIDER_ID, api_key="sk-resolver")
        db.commit()
        selection = LLMRuntimeConfigService(db).build_runtime_selection(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            model="gpt-5.2",
        )
        target = LLMRuntimeConfigService(db).resolve_role_target(
            selection,
            ROLE_TOOL_OUTPUT_COMPRESSOR,
        )

        def fake_get_client(*, provider_model: ProviderModelRef, api_key: str, **kwargs):
            calls.append(
                {
                    "provider_model": provider_model,
                    "api_key": api_key,
                    "reasoning_effort": kwargs.get("reasoning_effort"),
                }
            )
            return object()

        monkeypatch.setattr(resolver_module.LLMClientFactory, "get_client", fake_get_client)

        client = resolver_module.LLMRuntimeClientResolver(service).get_client(
            selection,
            target=target,
            runtime_user_id=user.id,
            task_id=None,
            purpose="role-test",
        )

        assert client is not None
        assert calls == [
            {
                "provider_model": ProviderModelRef(OPENAI_PROVIDER_ID, "gpt-5-nano"),
                "api_key": "sk-resolver",
                "reasoning_effort": "minimal",
            }
        ]
    finally:
        db.close()


def test_runtime_client_resolver_uses_selected_credential_for_same_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class _CredentialService:
        requested_refs: list[tuple[int, str]] = []

        def get_credential_ref(self, user_id: int, provider: str) -> LLMCredentialRef:
            self.requested_refs.append((user_id, provider))
            return LLMCredentialRef(user_id=user_id, provider=provider)

        def resolve_secret(
            self,
            credential_ref: LLMCredentialRef,
            *,
            runtime_user_id: int,
            task_id: int | None,
            purpose: str,
        ) -> ProviderSecret:
            return ProviderSecret(provider=credential_ref.provider, value=f"sk-{credential_ref.provider}")

    service = _CredentialService()

    def fake_get_client(*, provider_model: ProviderModelRef, api_key: str, **kwargs):
        calls.append({"provider_model": provider_model, "api_key": api_key})
        return object()

    monkeypatch.setattr(resolver_module.LLMClientFactory, "get_client", fake_get_client)

    selection = LLMRuntimeSelection(
        provider="openai",
        model="gpt-5.2",
        credential_ref=LLMCredentialRef(user_id=7, provider="openai"),
    )

    resolver_module.LLMRuntimeClientResolver(service).get_client(
        selection,
        target=ProviderModelRef("openai", "gpt-5-mini"),
        runtime_user_id=7,
        task_id=None,
        purpose="same-provider",
    )

    assert service.requested_refs == []
    assert calls == [
        {
            "provider_model": ProviderModelRef("openai", "gpt-5-mini"),
            "api_key": "sk-openai",
        }
    ]


def test_runtime_client_resolver_resolves_target_provider_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class _CredentialService:
        requested_refs: list[tuple[int, str]] = []

        def get_credential_ref(self, user_id: int, provider: str) -> LLMCredentialRef:
            self.requested_refs.append((user_id, provider))
            return LLMCredentialRef(user_id=user_id, provider=provider)

        def resolve_secret(
            self,
            credential_ref: LLMCredentialRef,
            *,
            runtime_user_id: int,
            task_id: int | None,
            purpose: str,
        ) -> ProviderSecret:
            return ProviderSecret(provider=credential_ref.provider, value=f"sk-{credential_ref.provider}")

    service = _CredentialService()

    def fake_get_client(*, provider_model: ProviderModelRef, api_key: str, **kwargs):
        calls.append({"provider_model": provider_model, "api_key": api_key})
        return object()

    monkeypatch.setattr(resolver_module.LLMClientFactory, "get_client", fake_get_client)

    selection = LLMRuntimeSelection(
        provider="openai",
        model="gpt-5.2",
        credential_ref=LLMCredentialRef(user_id=7, provider="openai"),
    )

    resolver_module.LLMRuntimeClientResolver(service).get_client(
        selection,
        target=ProviderModelRef("anthropic", "claude-test"),
        runtime_user_id=7,
        task_id=None,
        purpose="cross-provider",
    )

    assert service.requested_refs == [(7, "anthropic")]
    assert calls == [
        {
            "provider_model": ProviderModelRef("anthropic", "claude-test"),
            "api_key": "sk-anthropic",
        }
    ]


def test_runtime_client_resolver_delegates_anthropic_profile_default_to_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class _CredentialService:
        def resolve_secret(
            self,
            credential_ref: LLMCredentialRef,
            *,
            runtime_user_id: int,
            task_id: int | None,
            purpose: str,
        ) -> ProviderSecret:
            return ProviderSecret(provider=credential_ref.provider, value="sk-anthropic")

    def fake_get_client(*, provider_model: ProviderModelRef, api_key: str, **kwargs):
        calls.append(
            {
                "provider_model": provider_model,
                "api_key": api_key,
                "kwargs": kwargs,
            }
        )
        return object()

    monkeypatch.setattr(resolver_module.LLMClientFactory, "get_client", fake_get_client)

    selection = LLMRuntimeSelection(
        provider="anthropic",
        model=ANTHROPIC_LISTABLE_MODEL_IDS[0],
        credential_ref=LLMCredentialRef(user_id=7, provider="anthropic"),
    )

    resolver_module.LLMRuntimeClientResolver(_CredentialService()).get_client(
        selection,
        runtime_user_id=7,
        task_id=None,
        purpose="anthropic-main",
    )

    assert calls == [
        {
            "provider_model": ProviderModelRef("anthropic", ANTHROPIC_LISTABLE_MODEL_IDS[0]),
            "api_key": "sk-anthropic",
            "kwargs": {},
        }
    ]


def test_runtime_client_resolver_propagates_supported_anthropic_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class _CredentialService:
        def resolve_secret(
            self,
            credential_ref: LLMCredentialRef,
            *,
            runtime_user_id: int,
            task_id: int | None,
            purpose: str,
        ) -> ProviderSecret:
            return ProviderSecret(provider=credential_ref.provider, value="sk-anthropic")

    def fake_get_client(*, provider_model: ProviderModelRef, api_key: str, **kwargs):
        calls.append({"provider_model": provider_model, "kwargs": kwargs})
        return object()

    monkeypatch.setattr(resolver_module.LLMClientFactory, "get_client", fake_get_client)

    selection = LLMRuntimeSelection(
        provider="anthropic",
        model="claude-sonnet-5",
        credential_ref=LLMCredentialRef(user_id=7, provider="anthropic"),
        reasoning_effort="xhigh",
    )

    resolver_module.LLMRuntimeClientResolver(_CredentialService()).get_client(
        selection,
        runtime_user_id=7,
        task_id=None,
        purpose="anthropic-main",
    )

    assert calls == [
        {
            "provider_model": ProviderModelRef("anthropic", "claude-sonnet-5"),
            "kwargs": {"reasoning_effort": "xhigh"},
        }
    ]


def test_runtime_client_resolver_rejects_explicit_haiku_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_called = False

    class _CredentialService:
        def resolve_secret(
            self,
            credential_ref: LLMCredentialRef,
            *,
            runtime_user_id: int,
            task_id: int | None,
            purpose: str,
        ) -> ProviderSecret:
            raise AssertionError("credential should not be resolved before capability failure")

    def fake_get_client(*args: Any, **kwargs: Any) -> object:
        nonlocal factory_called
        factory_called = True
        return object()

    monkeypatch.setattr(resolver_module.LLMClientFactory, "get_client", fake_get_client)

    selection = LLMRuntimeSelection(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        credential_ref=LLMCredentialRef(user_id=7, provider="anthropic"),
        reasoning_effort="medium",
    )

    with pytest.raises(LLMCapabilityNotSupportedError, match="does not support reasoning_effort"):
        resolver_module.LLMRuntimeClientResolver(_CredentialService()).get_client(
            selection,
            runtime_user_id=7,
            task_id=None,
            purpose="anthropic-main",
        )

    assert factory_called is False


def test_runtime_client_resolver_rejects_kwarg_haiku_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_called = False

    class _CredentialService:
        def resolve_secret(
            self,
            credential_ref: LLMCredentialRef,
            *,
            runtime_user_id: int,
            task_id: int | None,
            purpose: str,
        ) -> ProviderSecret:
            raise AssertionError("credential should not be resolved before capability failure")

    def fake_get_client(*args: Any, **kwargs: Any) -> object:
        nonlocal factory_called
        factory_called = True
        return object()

    monkeypatch.setattr(resolver_module.LLMClientFactory, "get_client", fake_get_client)

    selection = LLMRuntimeSelection(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        credential_ref=LLMCredentialRef(user_id=7, provider="anthropic"),
    )

    with pytest.raises(LLMCapabilityNotSupportedError, match="does not support reasoning_effort"):
        resolver_module.LLMRuntimeClientResolver(_CredentialService()).get_client(
            selection,
            runtime_user_id=7,
            task_id=None,
            purpose="anthropic-main",
            reasoning_effort="medium",
        )

    assert factory_called is False


def test_runtime_client_resolver_rejects_openai_chat_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_called = False

    class _CredentialService:
        def resolve_secret(
            self,
            credential_ref: LLMCredentialRef,
            *,
            runtime_user_id: int,
            task_id: int | None,
            purpose: str,
        ) -> ProviderSecret:
            raise AssertionError("credential should not be resolved before capability failure")

    def fake_get_client(*args: Any, **kwargs: Any) -> object:
        nonlocal factory_called
        factory_called = True
        return object()

    monkeypatch.setattr(resolver_module.LLMClientFactory, "get_client", fake_get_client)

    selection = LLMRuntimeSelection(
        provider="openai",
        model="gpt-4o-mini",
        credential_ref=LLMCredentialRef(user_id=7, provider="openai"),
        reasoning_effort="medium",
    )

    with pytest.raises(LLMCapabilityNotSupportedError, match="does not support reasoning_effort"):
        resolver_module.LLMRuntimeClientResolver(_CredentialService()).get_client(
            selection,
            runtime_user_id=7,
            task_id=None,
            purpose="legacy-openai-chat",
        )

    assert factory_called is False


@pytest.mark.asyncio
async def test_runtime_client_resolver_preserves_openai_intent_classifier_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CredentialService:
        def resolve_secret(
            self,
            credential_ref: LLMCredentialRef,
            *,
            runtime_user_id: int,
            task_id: int | None,
            purpose: str,
        ) -> ProviderSecret:
            return ProviderSecret(provider=credential_ref.provider, value="sk-openai")

    class _Client:
        model = "gpt-5.2"

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def chat_with_usage(self, *_args: Any, **kwargs: Any) -> LLMResponse:
            self.calls.append(dict(kwargs))
            return LLMResponse(content="ok")

    fake_client = _Client()

    def fake_get_client(*_args: Any, **_kwargs: Any) -> _Client:
        return fake_client

    monkeypatch.setattr(resolver_module.LLMClientFactory, "get_client", fake_get_client)

    selection = LLMRuntimeSelection(
        provider=OPENAI_PROVIDER_ID,
        model="gpt-5.2",
        credential_ref=LLMCredentialRef(user_id=7, provider=OPENAI_PROVIDER_ID),
    )
    client = resolver_module.LLMRuntimeClientResolver(_CredentialService()).get_client(
        selection,
        runtime_user_id=7,
        task_id=None,
        purpose="budget-openai",
        resolution_role="intent_classifier",
    )

    await client.chat_with_usage("system", "user", max_tokens=32_000)

    assert fake_client.calls == [{"max_tokens": 32_000}]


@pytest.mark.asyncio
async def test_runtime_client_resolver_clamps_openai_budget_above_profile_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CredentialService:
        def resolve_secret(
            self,
            credential_ref: LLMCredentialRef,
            *,
            runtime_user_id: int,
            task_id: int | None,
            purpose: str,
        ) -> ProviderSecret:
            return ProviderSecret(provider=credential_ref.provider, value="sk-openai")

    class _Client:
        model = "gpt-5.2"

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def chat_with_usage(self, *_args: Any, **kwargs: Any) -> LLMResponse:
            self.calls.append(dict(kwargs))
            return LLMResponse(content="ok")

    fake_client = _Client()

    def fake_get_client(*_args: Any, **_kwargs: Any) -> _Client:
        return fake_client

    monkeypatch.setattr(resolver_module.LLMClientFactory, "get_client", fake_get_client)

    selection = LLMRuntimeSelection(
        provider=OPENAI_PROVIDER_ID,
        model="gpt-5.2",
        credential_ref=LLMCredentialRef(user_id=7, provider=OPENAI_PROVIDER_ID),
    )
    client = resolver_module.LLMRuntimeClientResolver(_CredentialService()).get_client(
        selection,
        runtime_user_id=7,
        task_id=None,
        purpose="budget-openai",
        resolution_role="intent_classifier",
    )

    await client.chat_with_usage("system", "user", max_tokens=64_000)

    assert fake_client.calls == [{"max_tokens": 32_000}]


@pytest.mark.asyncio
async def test_runtime_client_resolver_clamps_openai_chat_budget_to_legacy_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CredentialService:
        def resolve_secret(
            self,
            credential_ref: LLMCredentialRef,
            *,
            runtime_user_id: int,
            task_id: int | None,
            purpose: str,
        ) -> ProviderSecret:
            return ProviderSecret(provider=credential_ref.provider, value="sk-openai")

    class _Client:
        model = "gpt-4o"

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def chat_messages_with_usage(self, *_args: Any, **kwargs: Any) -> LLMResponse:
            self.calls.append(dict(kwargs))
            return LLMResponse(content="ok")

    fake_client = _Client()

    def fake_get_client(*_args: Any, **_kwargs: Any) -> _Client:
        return fake_client

    monkeypatch.setattr(resolver_module.LLMClientFactory, "get_client", fake_get_client)

    selection = LLMRuntimeSelection(
        provider=OPENAI_PROVIDER_ID,
        model="gpt-4o",
        credential_ref=LLMCredentialRef(user_id=7, provider=OPENAI_PROVIDER_ID),
    )
    client = resolver_module.LLMRuntimeClientResolver(_CredentialService()).get_client(
        selection,
        runtime_user_id=7,
        task_id=None,
        purpose="budget-openai-chat",
        resolution_role="conversation_main",
    )

    await client.chat_messages_with_usage(
        [{"role": "user", "content": "hello"}],
        max_tokens=32_000,
    )

    assert fake_client.calls == [{"max_tokens": 10_000}]


@pytest.mark.asyncio
async def test_runtime_client_resolver_applies_default_output_budget_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CredentialService:
        def resolve_secret(
            self,
            credential_ref: LLMCredentialRef,
            *,
            runtime_user_id: int,
            task_id: int | None,
            purpose: str,
        ) -> ProviderSecret:
            return ProviderSecret(provider=credential_ref.provider, value="sk-openai")

    class _Client:
        model = "gpt-5.2"

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def chat_with_usage(self, *_args: Any, **kwargs: Any) -> LLMResponse:
            self.calls.append(dict(kwargs))
            return LLMResponse(content="ok")

    fake_client = _Client()

    def fake_get_client(*_args: Any, **_kwargs: Any) -> _Client:
        return fake_client

    monkeypatch.setattr(resolver_module.LLMClientFactory, "get_client", fake_get_client)

    selection = LLMRuntimeSelection(
        provider=OPENAI_PROVIDER_ID,
        model="gpt-5.2",
        credential_ref=LLMCredentialRef(user_id=7, provider=OPENAI_PROVIDER_ID),
    )
    client = resolver_module.LLMRuntimeClientResolver(_CredentialService()).get_client(
        selection,
        runtime_user_id=7,
        task_id=None,
        purpose="budget-openai-default",
        resolution_role="intent_classifier",
    )

    await client.chat_with_usage("system", "user")

    assert fake_client.calls == [{"max_tokens": 10_000}]


@pytest.mark.asyncio
async def test_runtime_client_resolver_rejects_anthropic_output_budget_before_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CredentialService:
        def resolve_secret(
            self,
            credential_ref: LLMCredentialRef,
            *,
            runtime_user_id: int,
            task_id: int | None,
            purpose: str,
        ) -> ProviderSecret:
            return ProviderSecret(provider=credential_ref.provider, value="sk-anthropic")

    class _Client:
        model = ANTHROPIC_LISTABLE_MODEL_IDS[0]

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def chat_messages_with_usage(self, *_args: Any, **kwargs: Any) -> LLMResponse:
            self.calls.append(dict(kwargs))
            return LLMResponse(content="ok")

    fake_client = _Client()

    def fake_get_client(*_args: Any, **_kwargs: Any) -> _Client:
        return fake_client

    monkeypatch.setattr(resolver_module.LLMClientFactory, "get_client", fake_get_client)

    selection = LLMRuntimeSelection(
        provider=ANTHROPIC_PROVIDER_ID,
        model=ANTHROPIC_LISTABLE_MODEL_IDS[0],
        credential_ref=LLMCredentialRef(user_id=7, provider=ANTHROPIC_PROVIDER_ID),
    )
    client = resolver_module.LLMRuntimeClientResolver(_CredentialService()).get_client(
        selection,
        runtime_user_id=7,
        task_id=None,
        purpose="budget-anthropic",
        resolution_role="conversation_main",
    )

    with pytest.raises(LLMConfigurationError, match="max_output_tokens"):
        await client.chat_messages_with_usage(
            [{"role": "user", "content": "hello"}],
            max_tokens=999_999,
        )

    assert fake_client.calls == []


@pytest.mark.asyncio
async def test_runtime_client_resolver_rejects_context_overflow_before_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CredentialService:
        def resolve_secret(
            self,
            credential_ref: LLMCredentialRef,
            *,
            runtime_user_id: int,
            task_id: int | None,
            purpose: str,
        ) -> ProviderSecret:
            return ProviderSecret(provider=credential_ref.provider, value="sk-anthropic")

    class _Client:
        model = "claude-haiku-4-5-20251001"

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def chat_messages_with_usage(self, *_args: Any, **kwargs: Any) -> LLMResponse:
            self.calls.append(dict(kwargs))
            return LLMResponse(content="ok")

    fake_client = _Client()

    def fake_get_client(*_args: Any, **_kwargs: Any) -> _Client:
        return fake_client

    monkeypatch.setattr(resolver_module.LLMClientFactory, "get_client", fake_get_client)
    monkeypatch.setattr(
        resolver_module,
        "estimate_chat_history_tokens",
        lambda **_kwargs: SimpleNamespace(tokens=199_500),
    )

    selection = LLMRuntimeSelection(
        provider=ANTHROPIC_PROVIDER_ID,
        model="claude-haiku-4-5-20251001",
        credential_ref=LLMCredentialRef(user_id=7, provider=ANTHROPIC_PROVIDER_ID),
    )
    client = resolver_module.LLMRuntimeClientResolver(_CredentialService()).get_client(
        selection,
        runtime_user_id=7,
        task_id=None,
        purpose="budget-anthropic-context",
        resolution_role="conversation_main",
    )

    with pytest.raises(LLMConfigurationError, match="context_window_tokens"):
        await client.chat_messages_with_usage(
            [{"role": "user", "content": "hello"}],
            max_tokens=1_000,
        )

    assert fake_client.calls == []


@pytest.mark.asyncio
async def test_runtime_client_resolver_rejects_context_estimation_failure_before_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CredentialService:
        def resolve_secret(
            self,
            credential_ref: LLMCredentialRef,
            *,
            runtime_user_id: int,
            task_id: int | None,
            purpose: str,
        ) -> ProviderSecret:
            return ProviderSecret(provider=credential_ref.provider, value="sk-anthropic")

    class _Client:
        model = "claude-haiku-4-5-20251001"

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def chat_messages_with_usage(self, *_args: Any, **kwargs: Any) -> LLMResponse:
            self.calls.append(dict(kwargs))
            return LLMResponse(content="ok")

    fake_client = _Client()

    def fake_get_client(*_args: Any, **_kwargs: Any) -> _Client:
        return fake_client

    def fail_estimate(**_kwargs: Any) -> SimpleNamespace:
        raise ValueError("estimator failure")

    monkeypatch.setattr(resolver_module.LLMClientFactory, "get_client", fake_get_client)
    monkeypatch.setattr(resolver_module, "estimate_chat_history_tokens", fail_estimate)

    selection = LLMRuntimeSelection(
        provider=ANTHROPIC_PROVIDER_ID,
        model="claude-haiku-4-5-20251001",
        credential_ref=LLMCredentialRef(user_id=7, provider=ANTHROPIC_PROVIDER_ID),
    )
    client = resolver_module.LLMRuntimeClientResolver(_CredentialService()).get_client(
        selection,
        runtime_user_id=7,
        task_id=None,
        purpose="budget-anthropic-estimator-failure",
        resolution_role="conversation_main",
    )

    with pytest.raises(LLMConfigurationError, match="Unable to estimate context tokens"):
        await client.chat_messages_with_usage(
            [{"role": "user", "content": "hello"}],
            max_tokens=1_000,
        )

    assert fake_client.calls == []


@pytest.mark.asyncio
async def test_runtime_client_resolver_counts_tool_payloads_for_context_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CredentialService:
        def resolve_secret(
            self,
            credential_ref: LLMCredentialRef,
            *,
            runtime_user_id: int,
            task_id: int | None,
            purpose: str,
        ) -> ProviderSecret:
            return ProviderSecret(provider=credential_ref.provider, value="sk-anthropic")

    class _Client:
        model = "claude-haiku-4-5-20251001"

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def chat_with_tools_with_usage(self, *_args: Any, **kwargs: Any) -> LLMResponse:
            self.calls.append(dict(kwargs))
            return LLMResponse(content="ok")

    fake_client = _Client()

    def fake_get_client(*_args: Any, **_kwargs: Any) -> _Client:
        return fake_client

    monkeypatch.setattr(resolver_module.LLMClientFactory, "get_client", fake_get_client)
    monkeypatch.setattr(
        resolver_module,
        "estimate_chat_history_tokens",
        lambda **_kwargs: SimpleNamespace(tokens=198_500),
    )
    seen_budget_payloads: list[Any] = []

    def fake_estimate_json_tokens(payload: Any, **_kwargs: Any) -> SimpleNamespace:
        seen_budget_payloads.append(payload)
        return SimpleNamespace(tokens=700)

    monkeypatch.setattr(
        resolver_module,
        "estimate_json_tokens",
        fake_estimate_json_tokens,
    )

    selection = LLMRuntimeSelection(
        provider=ANTHROPIC_PROVIDER_ID,
        model="claude-haiku-4-5-20251001",
        credential_ref=LLMCredentialRef(user_id=7, provider=ANTHROPIC_PROVIDER_ID),
    )
    client = resolver_module.LLMRuntimeClientResolver(_CredentialService()).get_client(
        selection,
        runtime_user_id=7,
        task_id=None,
        purpose="budget-anthropic-tools-context",
        resolution_role="conversation_main",
    )

    with pytest.raises(LLMConfigurationError, match="context_window_tokens"):
        await client.chat_with_tools_with_usage(
            "system",
            "user",
            tools=[
                FunctionToolSpec(
                    tool_id="large_tool",
                    name="large_tool",
                    description="Large schema",
                    parameters_schema={"type": "object"},
                )
            ],
            max_tokens=1_000,
        )

    assert seen_budget_payloads == [
        {
            "tools": [
                {
                    "tool_id": "large_tool",
                    "name": "large_tool",
                    "description": "Large schema",
                    "parameters_schema": {"type": "object"},
                }
            ],
            "tool_choice": "auto",
        }
    ]
    assert fake_client.calls == []


def test_runtime_client_resolver_fails_when_target_provider_credential_missing() -> None:
    class _CredentialService:
        def get_credential_ref(self, user_id: int, provider: str) -> LLMCredentialRef:
            raise CredentialNotFoundError(f"{provider} credential is not configured")

        def resolve_secret(
            self,
            credential_ref: LLMCredentialRef,
            *,
            runtime_user_id: int,
            task_id: int | None,
            purpose: str,
        ) -> ProviderSecret:
            return ProviderSecret(provider=credential_ref.provider, value="sk-openai")

    selection = LLMRuntimeSelection(
        provider="openai",
        model="gpt-5.2",
        credential_ref=LLMCredentialRef(user_id=7, provider="openai"),
    )

    with pytest.raises(CredentialNotFoundError):
        resolver_module.LLMRuntimeClientResolver(_CredentialService()).get_client(
            selection,
            target=ProviderModelRef("anthropic", "claude-test"),
            runtime_user_id=7,
            task_id=None,
            purpose="cross-provider",
        )


def test_runtime_services_strip_removes_live_resolver() -> None:
    db = SessionLocal()
    try:
        services = LLMRuntimeServices(
            client_resolver=resolver_module.LLMRuntimeClientResolver(LLMCredentialService(db))
        )
        config = attach_runtime_services(
            {"configurable": {"thread_id": "task-1"}, "metadata": {"provider": "openai"}},
            services,
        )

        stripped = strip_runtime_services(config)

        assert config["configurable"]["runtime_services"] is services
        assert "runtime_services" not in stripped["configurable"]
        assert stripped["configurable"]["thread_id"] == "task-1"
    finally:
        db.close()


def test_environment_service_preserves_openai_container_variables() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db)
        service = LLMCredentialService(db)
        service.upsert_api_key(user_id=user.id, provider=OPENAI_PROVIDER_ID, api_key="sk-env")
        LLMProviderSelectionService(db, credential_service=service).set_selection(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            model="gpt-5.2",
        )
        db.commit()

        environment = LLMProviderEnvironmentService(db).build_environment(user_id=user.id)

        assert environment == {
            "LLM_PROVIDER": OPENAI_PROVIDER_ID,
            "LLM_MODEL": "gpt-5.2",
            "OPENAI_API_KEY": "sk-env",
        }
    finally:
        db.close()


def test_environment_service_returns_backend_only_anthropic_metadata() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db)
        service = LLMCredentialService(db)
        service.upsert_api_key(
            user_id=user.id,
            provider=ANTHROPIC_PROVIDER_ID,
            api_key="sk-ant-env",
        )
        LLMProviderSelectionService(db, credential_service=service).set_selection(
            user_id=user.id,
            provider=ANTHROPIC_PROVIDER_ID,
            model=ANTHROPIC_LISTABLE_MODEL_IDS[0],
        )
        db.commit()

        environment = LLMProviderEnvironmentService(db).build_environment(user_id=user.id)

        assert environment == {
            "LLM_PROVIDER": ANTHROPIC_PROVIDER_ID,
            "LLM_MODEL": ANTHROPIC_LISTABLE_MODEL_IDS[0],
        }
        assert "ANTHROPIC_API_KEY" not in environment
        assert "OPENAI_API_KEY" not in environment
    finally:
        db.close()


def test_health_service_routes_anthropic_credential_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SessionLocal()
    try:
        user = _create_user(db)
        calls: list[str] = []

        def fake_test_anthropic_key(api_key: str) -> ProviderHealthCheckResult:
            calls.append(api_key)
            return ProviderHealthCheckResult(
                provider=ANTHROPIC_PROVIDER_ID,
                status="success",
                message="Anthropic API key is valid",
                model_count=1,
            )

        monkeypatch.setattr(
            LLMProviderHealthService,
            "_test_anthropic_key",
            staticmethod(fake_test_anthropic_key),
        )

        result = LLMProviderHealthService(db).test_credential(
            user_id=user.id,
            provider=ANTHROPIC_PROVIDER_ID,
            api_key="sk-ant-health",
        )

        assert calls == ["sk-ant-health"]
        assert result.provider == ANTHROPIC_PROVIDER_ID
        assert result.status == "success"
    finally:
        db.close()


def test_credential_disable_clears_legacy_mirror_and_prevents_resolution() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db)
        service = LLMCredentialService(db)
        service.upsert_api_key(user_id=user.id, provider=OPENAI_PROVIDER_ID, api_key="sk-delete")
        db.commit()

        service.disable(user_id=user.id, provider=OPENAI_PROVIDER_ID)
        db.commit()

        credential = db.query(UserLLMProviderCredential).filter_by(user_id=user.id).one()
        settings = db.query(UserSettings).filter_by(user_id=user.id).one()
        assert credential.enabled is False
        assert credential.encrypted_api_key == ""
        assert settings.openai_api_key is None
        with pytest.raises(CredentialNotFoundError):
            service.get_credential_ref(user.id, OPENAI_PROVIDER_ID)
    finally:
        db.close()
