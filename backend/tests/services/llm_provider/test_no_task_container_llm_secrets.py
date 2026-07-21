"""Prove LLM connection secrets cannot cross into task runtime containers."""

from __future__ import annotations

import asyncio
import json
import logging

from backend.services.docker.container_config import ContainerConfigBuilder
from backend.services.docker.runtime_config import RuntimeConfig
from backend.services.llm_provider.environment_service import LLMProviderEnvironmentService
from backend.services.runtime_provider.contracts import (
    RuntimeActorType,
    RuntimeCallScope,
    RuntimeOperationRequest,
    RuntimeOperationStatus,
    RuntimePlacementMode,
)
from backend.services.runtime_provider.local_docker_provider import LocalDockerRuntimeProvider


SECRET = "llm-connection-secret-must-not-cross-runtime-boundary"


def test_openai_container_environment_never_resolves_connection_secret() -> None:
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
                {"provider": "openai", "model": "gpt-5.2"},
            )()

    class _CredentialService:
        def resolve_secret(self, *_args, **_kwargs):
            raise AssertionError("container environment must not resolve LLM secrets")

    environment = LLMProviderEnvironmentService(
        object(),
        credential_service=_CredentialService(),
        runtime_config_service=_RuntimeConfigService(),
    ).build_environment(user_id=34, task_id=12)

    assert environment == {"LLM_PROVIDER": "openai", "LLM_MODEL": "gpt-5.2"}
    assert SECRET not in json.dumps(environment)


def test_container_config_strips_secret_fields_from_faulty_provider_builder(caplog) -> None:
    def _faulty_provider_environment(_user_id: int, _task_id: int | None) -> dict[str, str]:
        return {
            "LLM_PROVIDER": "openai-compatible",
            "LLM_MODEL": "custom-model",
            "OPENAI_API_KEY": SECRET,
            "ANTHROPIC_API_KEY": SECRET,
            "LLM_ACCESS_TOKEN": SECRET,
            "LLM_CLIENT_SECRET": SECRET,
            "AZURE_OPENAI_KEY": SECRET,
            "HF_TOKEN": SECRET,
            "LLM_PASSWORD": SECRET,
            "AUTHORIZATION": f"Bearer {SECRET}",
        }

    builder = ContainerConfigBuilder(
        RuntimeConfig(),
        workspace_path_resolver=lambda task_id: f"/host/workspaces/task-{task_id}",
        provider_environment_builder=_faulty_provider_environment,
    )

    with caplog.at_level(logging.INFO, logger="backend.services.unified_docker_service"):
        config = builder.prepare_container_config(task_id=12, user_id=34)

    serialized = json.dumps(config, default=str)
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert config["environment"]["LLM_PROVIDER"] == "openai-compatible"
    assert config["environment"]["LLM_MODEL"] == "custom-model"
    assert SECRET not in serialized
    assert SECRET not in logs
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "LLM_ACCESS_TOKEN",
        "LLM_CLIENT_SECRET",
        "AZURE_OPENAI_KEY",
        "HF_TOKEN",
        "LLM_PASSWORD",
        "AUTHORIZATION",
    ):
        assert key not in config["environment"]


def test_provider_environment_failure_does_not_log_exception_secret(caplog) -> None:
    def _failing_provider_environment(_user_id: int, _task_id: int | None) -> dict[str, str]:
        raise RuntimeError(SECRET)

    builder = ContainerConfigBuilder(
        RuntimeConfig(),
        workspace_path_resolver=lambda task_id: f"/host/workspaces/task-{task_id}",
        provider_environment_builder=_failing_provider_environment,
    )

    with caplog.at_level(logging.WARNING, logger="backend.services.unified_docker_service"):
        config = builder.prepare_container_config(task_id=12, user_id=34)

    assert SECRET not in json.dumps(config, default=str)
    assert SECRET not in "\n".join(record.getMessage() for record in caplog.records)


def test_local_provider_rejects_secret_bearing_provision_payload() -> None:
    class _DockerService:
        called = False

        async def create_and_start_container(self, *_args, **_kwargs):
            self.called = True
            return {"status": "running"}

    docker_service = _DockerService()
    provider = LocalDockerRuntimeProvider(docker_service=docker_service)
    request = RuntimeOperationRequest(
        tenant_id="tenant-local",
        task_id=123,
        user_id=7,
        actor_type=RuntimeActorType.USER,
        actor_id=7,
        runtime_placement_mode=RuntimePlacementMode.LOCAL,
        runtime_call_scope=RuntimeCallScope.TEST,
        workspace_id="task-123",
        operation="provision_task_runtime",
        payload={"environment": {"OPENAI_API_KEY": SECRET}},
    )

    result = asyncio.run(provider.provision_task_runtime(request))

    assert result.accepted is False
    assert result.status == RuntimeOperationStatus.REJECTED
    assert result.error_code == "llm_secret_payload_forbidden"
    assert SECRET not in repr(result)
    assert docker_service.called is False
