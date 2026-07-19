"""Integration coverage for the GPT-OSS proving runtime path."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from agent.context.context_window_policy import estimate_chat_history_tokens
from agent.context.token_counter_registry import estimate_text_tokens
from agent.providers.llm.core.base import LLMResponse
from agent.providers.llm.core.exceptions import LLMConfigurationError
from agent.providers.llm.core.identity import ProviderModelRef
from agent.providers.llm.factory.client_factory import LLMClientFactory
from backend.database import Base
from backend.models import (
    LLMCapabilityObservation,
    LLMConversation,
    LLMDeploymentRoute,
    LLMInferenceConnection,
    LLMModelDeployment,
    Task,
    Tenant,
    User,
    UserLLMProviderCredential,
    UserLLMSelection,
    UserMemoryLLMSelection,
    UserReportingLLMSelection,
    UserSettings,
)
from backend.services.llm_provider.connection_service import LLMConnectionService
from backend.services.llm_provider.deployment_service import LLMDeploymentService
from backend.services.llm_provider.operation_registry import (
    GPT_OSS_20B_PROVING_BASE_URL_ENV,
    GPT_OSS_20B_PROVING_PRESET_ID,
)
from backend.services.llm_provider.runtime_client_resolver import (
    BudgetEnforcingLLMClient,
    LLMRuntimeClientResolver,
)
from backend.services.llm_provider.types import (
    GuardedHTTPResponse,
    LLMAuthMode,
    LLMConnectionCredentialRef,
    LLMConnectionOperation,
    LLMConnectionState,
    LLMRuntimeAccessContext,
    LLMRuntimeSelectionV2,
    DeploymentRef,
    ProviderSecret,
    ResolvedAuth,
)
from backend.services.usage_tracking.extraction import (
    UsageExtractionTarget,
    extract_usage,
)
from backend.services.usage_tracking.pricing_registry import (
    PRICING_UNAVAILABLE,
    get_pricing_quote,
)


@pytest.fixture
def llm_identity_db() -> Iterator[Session]:
    """Yield an isolated session containing deployment identity tables."""

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            UserSettings.__table__,
            Task.__table__,
            UserLLMProviderCredential.__table__,
            UserLLMSelection.__table__,
            UserReportingLLMSelection.__table__,
            UserMemoryLLMSelection.__table__,
            LLMConversation.__table__,
            LLMInferenceConnection.__table__,
            LLMModelDeployment.__table__,
            LLMDeploymentRoute.__table__,
            LLMCapabilityObservation.__table__,
        ],
    )
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def identity_users(llm_identity_db: Session) -> tuple[User, User]:
    """Create two users for ownership and isolation checks."""

    owner = User(username="gpt-oss-runtime-owner", password="hashed")
    other = User(username="gpt-oss-runtime-other", password="hashed")
    llm_identity_db.add_all([owner, other])
    llm_identity_db.flush()
    return owner, other


class _CredentialService:
    """Connection-auth double that records the runtime resolver request."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self.calls: list[dict[str, Any]] = []

    def resolve_connection_auth(
        self,
        connection_ref: LLMConnectionCredentialRef,
        *,
        runtime_user_id: int,
        task_id: int | None = None,
        purpose: str,
        auth_mode: LLMAuthMode | str,
    ) -> ResolvedAuth:
        self.calls.append(
            {
                "connection_ref": connection_ref,
                "runtime_user_id": runtime_user_id,
                "task_id": task_id,
                "purpose": purpose,
                "auth_mode": auth_mode,
            }
        )
        mode = auth_mode if isinstance(auth_mode, LLMAuthMode) else LLMAuthMode(auth_mode)
        return ResolvedAuth.with_secret(
            mode=mode,
            provider=GPT_OSS_20B_PROVING_PRESET_ID,
            secret=ProviderSecret(
                provider=GPT_OSS_20B_PROVING_PRESET_ID,
                value="sk-gpt-oss-runtime",
            ),
        )


class _FakeChatCompletions:
    """SDK chat endpoint double that must not be reached by budget failures."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(
                prompt_tokens=7,
                completion_tokens=3,
                total_tokens=10,
            ),
        )


class _FakeAsyncOpenAI:
    """Small AsyncOpenAI double that exposes the chat completions shape."""

    instances: list["_FakeAsyncOpenAI"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = dict(kwargs)
        self.completions = _FakeChatCompletions()
        self.chat = SimpleNamespace(completions=self.completions)
        self.instances.append(self)


@pytest.mark.asyncio
async def test_gpt_oss_runtime_uses_shared_authorities_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    monkeypatch.setenv(GPT_OSS_20B_PROVING_BASE_URL_ENV, "https://gpt-oss.example.test")
    monkeypatch.setattr(
        "agent.providers.llm.adapters.openai.compatible_chat.openai.AsyncOpenAI",
        _FakeAsyncOpenAI,
    )
    guarded_calls: list[dict[str, Any]] = []

    def fake_execute(self, operation, provider, secret, json_body=None):
        del self
        guarded_calls.append(
            {
                "operation": LLMConnectionOperation(operation),
                "provider": provider,
                "secret": secret.value,
                "json_body": dict(json_body or {}),
            }
        )
        return GuardedHTTPResponse(
            status_code=200,
            body=json.dumps(
                {
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {
                        "prompt_tokens": 7,
                        "completion_tokens": 3,
                        "total_tokens": 10,
                    },
                }
            ).encode(),
            audit_id="runtime-guarded-audit",
        )

    monkeypatch.setattr(
        "backend.services.llm_provider.runtime_client_resolver.GuardedTransport.execute",
        fake_execute,
    )
    owner, _ = identity_users
    llm_identity_db.add(Tenant(id=74, slug="gpt-oss-runtime", name="GPT-OSS Runtime"))
    llm_identity_db.flush()
    task = Task(user_id=owner.id, tenant_id=74, name="GPT-OSS runtime task")
    llm_identity_db.add(task)
    llm_identity_db.flush()

    connections = LLMConnectionService(llm_identity_db)
    connection = connections.create_gpt_oss_20b_proving_draft(user_id=owner.id)
    deployment, route = LLMDeploymentService(
        llm_identity_db
    ).create_gpt_oss_20b_proving_deployment(
        user_id=owner.id,
        connection_id=connection.id,
        expected_connection_revision=1,
    )
    connections.transition_state(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=connection.revision,
        target_state=LLMConnectionState.DISABLED,
    )
    connections.transition_state(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=connection.revision,
        target_state=LLMConnectionState.ENABLED,
    )

    credentials = _CredentialService(llm_identity_db)
    selection = LLMRuntimeSelectionV2(
        deployment_ref=DeploymentRef(str(deployment.id), int(deployment.revision)),
        preferred_route_id=str(route.id),
    )
    resolver = LLMRuntimeClientResolver(credentials, db=llm_identity_db)

    client = resolver.get_client(
        selection,
        access_context=LLMRuntimeAccessContext(
            runtime_user_id=owner.id,
            task_id=task.id,
            tenant_id=74,
        ),
        purpose="gpt-oss-runtime-test",
        resolution_role="conversation_main",
    )

    assert isinstance(client, BudgetEnforcingLLMClient)
    assert client.model == "openai/gpt-oss-20b"
    assert credentials.calls[0]["auth_mode"] is LLMAuthMode.BEARER
    assert _FakeAsyncOpenAI.instances == []
    assert LLMClientFactory.list_providers()["openai"].split(", ") == [
        "OpenAIChatClient",
        "OpenAIResponsesClient",
    ]
    assert "gpt-oss-20b" not in LLMClientFactory.list_prefix_registrations()

    with pytest.raises(LLMConfigurationError, match="max_tokens=0"):
        await client.chat_messages_with_usage(
            [{"role": "user", "content": "hello"}],
            max_tokens=0,
        )
    assert guarded_calls == []

    response = await client.chat_messages_with_usage(
        [{"role": "user", "content": "hello"}],
        max_tokens=4,
    )
    assert response.content == "ok"
    assert response.usage is not None
    assert response.usage.total_tokens == 10
    assert guarded_calls == [
        {
            "operation": LLMConnectionOperation.INFERENCE,
            "provider": GPT_OSS_20B_PROVING_PRESET_ID,
            "secret": "sk-gpt-oss-runtime",
            "json_body": {
                "model": "openai/gpt-oss-20b",
                "messages": [{"role": "user", "content": "hello"}],
                "temperature": 0.1,
                "max_tokens": 4,
            },
        }
    ]

    text_estimate = estimate_text_tokens(
        "tokenizer provenance",
        provider="openai",
        model="gpt-oss-20b",
    )
    history_estimate = estimate_chat_history_tokens(
        provider="openai",
        model="openai/gpt-oss-20b",
        history=[{"role": "user", "content": "hello"}],
    )
    assert text_estimate.precision == "heuristic"
    assert text_estimate.strategy == "openai_gpt_oss_unverified_tokenizer_heuristic"
    assert history_estimate.precision == "heuristic"
    assert history_estimate.strategy == "openai_gpt_oss_unverified_tokenizer_heuristic"

    usage = extract_usage(
        SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=7,
                completion_tokens=3,
                total_tokens=10,
            )
        ),
        UsageExtractionTarget(
            provider="openai",
            model="gpt-oss-20b",
            api_surface="chat_completions",
        ),
    )
    assert usage.prompt_tokens == 7
    assert usage.completion_tokens == 3
    assert usage.total_tokens == 10
    assert not hasattr(usage, "estimated_tokens")

    empty_usage = LLMResponse(content="ok", usage=None).usage
    assert empty_usage is None
    quote = get_pricing_quote(ProviderModelRef("openai", "gpt-oss-20b"))
    assert quote.status == PRICING_UNAVAILABLE
    assert quote.schedule is None
