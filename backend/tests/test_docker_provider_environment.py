"""Tests for provider-owned container LLM environment construction."""

from __future__ import annotations

import inspect
import logging
from typing import Optional

from backend.services.llm_provider.environment_service import LLMProviderEnvironmentService
from backend.services import container_utils
from backend.services.docker import container_config
from backend.services.docker.container_config import ContainerConfigBuilder
from backend.services.docker.runtime_config import RuntimeConfig


SECRET = "sk-test-execution-plane-container-secret-do-not-log"
def test_container_config_strips_llm_secrets_from_provider_environment(caplog) -> None:
    """Docker config keeps metadata and rejects provider secret fields."""

    calls: list[tuple[int, Optional[int]]] = []

    def _fake_provider_environment(user_id: int, task_id: Optional[int]) -> dict[str, str]:
        calls.append((user_id, task_id))
        return {
            "LLM_PROVIDER": "openai",
            "LLM_MODEL": "gpt-5.2",
            "OPENAI_API_KEY": SECRET,
        }

    builder = ContainerConfigBuilder(
        RuntimeConfig(),
        workspace_path_resolver=lambda task_id: f"/host/workspaces/task-{task_id}",
        provider_environment_builder=_fake_provider_environment,
    )

    with caplog.at_level(logging.INFO, logger="backend.services.unified_docker_service"):
        config = builder.prepare_container_config(task_id=12, user_id=34)

    assert calls == [(34, 12)]
    environment = config["environment"]
    assert environment["LLM_PROVIDER"] == "openai"
    assert environment["LLM_MODEL"] == "gpt-5.2"
    assert "OPENAI_API_KEY" not in environment
    assert SECRET not in "\n".join(record.getMessage() for record in caplog.records)


def test_openai_provider_environment_service_returns_backend_only_metadata() -> None:
    """OpenAI selection metadata never requires or resolves a credential."""

    class _RuntimeConfigService:
        def build_runtime_selection(
            self,
            *,
            user_id: int,
            require_enabled_credential: bool,
        ):
            assert user_id == 34
            assert require_enabled_credential is False
            return type(
                "RuntimeSelection",
                (),
                {
                    "provider": "openai",
                    "model": "gpt-5.2",
                },
            )()

    class _CredentialService:
        def resolve_secret(self, *_args, **_kwargs):
            raise AssertionError("container environment must not resolve LLM secrets")

    environment = LLMProviderEnvironmentService(
        object(),
        credential_service=_CredentialService(),
        runtime_config_service=_RuntimeConfigService(),
    ).build_environment(user_id=34, task_id=12)

    assert environment == {
        "LLM_PROVIDER": "openai",
        "LLM_MODEL": "gpt-5.2",
    }


def test_container_config_has_no_router_credential_helper_imports() -> None:
    """Docker config must not resolve credentials through settings routers or caches."""

    source = inspect.getsource(container_config)

    assert "get_user_openai_key" not in source
    assert "backend.routers.settings" not in source
    assert "get_cached_api_key" not in source
    assert "cache_api_key" not in source


def test_prepare_container_config_omits_unused_api_base_url_env() -> None:
    """Container env must not inject unused API_BASE_URL (no runtime consumer)."""

    builder = ContainerConfigBuilder(
        RuntimeConfig(),
        workspace_path_resolver=lambda task_id: f"/host/workspaces/task-{task_id}",
    )
    config = builder.prepare_container_config(task_id=12)
    assert "API_BASE_URL" not in config["environment"]
    assert config["environment"]["BACKEND_HOST"] == "host.docker.internal"
    assert config["extra_hosts"] == {"host.docker.internal": "host-gateway"}


def test_prepare_container_config_labels_runtime_canary_ownership(monkeypatch) -> None:
    """An explicit E2E suite id labels containers for safe leak cleanup."""

    monkeypatch.setenv("E2E_RUNTIME_SUITE_ID", "suite-123")
    builder = ContainerConfigBuilder(
        RuntimeConfig(),
        workspace_path_resolver=lambda task_id: f"/host/workspaces/task-{task_id}",
    )

    config = builder.prepare_container_config(task_id=12)

    assert config["labels"]["drowai.e2e_suite_id"] == "suite-123"


def test_prepare_container_config_ignores_invalid_runtime_canary_ownership(monkeypatch) -> None:
    """Untrusted label syntax is never forwarded to Docker."""

    monkeypatch.setenv("E2E_RUNTIME_SUITE_ID", "invalid suite/id")
    builder = ContainerConfigBuilder(
        RuntimeConfig(),
        workspace_path_resolver=lambda task_id: f"/host/workspaces/task-{task_id}",
    )

    config = builder.prepare_container_config(task_id=12)

    assert "drowai.e2e_suite_id" not in config["labels"]


def test_legacy_container_api_key_cache_is_inert() -> None:
    """Deprecated compatibility cache helpers must not retain decrypted keys."""

    container_utils.cache_api_key(99, SECRET)

    assert container_utils.get_cached_api_key(99) is None
    assert not hasattr(container_utils, "_api_key_cache")
