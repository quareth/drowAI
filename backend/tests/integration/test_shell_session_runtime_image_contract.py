"""Runtime-image proof for provider-backed shell session installed commands.

This module owns the opt-in real-Docker shell contract probe that verifies a
safe installed Kali command traverses the public shell session service and the
runner terminal proxy path without tool-specific production handling.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
import uuid

import pytest

from backend.services.runtime_provider import RuntimeCallScope
from backend.services.terminal import manager as terminal_manager_module
from backend.services.terminal.manager import TerminalSessionManager
from backend.services.terminal.shell_session_service import (
    ShellSessionService,
    ShellSessionServiceConfig,
)
from drowai_runner.control_channel.terminal.pty_adapter import _RunnerPtyAdapter
from drowai_runner.docker_runtime import RunnerDockerRuntime
from drowai_runner.job_store import initialize_runner_job_store
from drowai_runner.terminal_proxy import RunnerTerminalProxy, TerminalProxyResponse
from runtime_shared.runtime_image_contract import default_runtime_image_for_machine
from runtime_shared.shell_session_contracts import (
    ShellExecRequest,
    ShellProcessStatus,
    ShellSessionIdentity,
)


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_RUNTIME_IMAGE_SMOKE") != "1",
    reason="Set RUN_RUNTIME_IMAGE_SMOKE=1 to run the real runtime-image shell probe.",
)


def _runtime_identity(*, task_id: int, tenant_id: int, runner_id: str) -> ShellSessionIdentity:
    return ShellSessionIdentity(
        tenant_id=tenant_id,
        task_id=task_id,
        execution_owner_id="main:turn-installed-command-proof",
        runtime_placement_mode="runner",
        workspace_id=f"task-{task_id}",
        workspace_path="/workspace",
        runner_id=runner_id,
        execution_site_id="site-installed-command-proof",
    )


def _runtime_context(*, task_id: int, tenant_id: int, runner_id: str):
    return SimpleNamespace(
        tenant_id=tenant_id,
        task_id=task_id,
        user_id=3,
        runtime_placement_mode="runner",
        workspace_id=f"task-{task_id}",
        workspace_path="/workspace",
        runner_id=runner_id,
        execution_site_id="site-installed-command-proof",
        runtime_call_scope=RuntimeCallScope.TEST,
    )


def _runtime_operation_result(response: TerminalProxyResponse, *, delegate: dict[str, object]):
    return SimpleNamespace(
        ok=response.accepted,
        error_code=response.error_code,
        error_message=response.error_message,
        metadata={"delegate_result": delegate},
    )


@pytest.mark.integration
@pytest.mark.execution_plane_non_dind_regression
def test_runtime_image_installed_command_completes_through_public_shell_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Run ``nmap --version`` through shell.exec and the runner PTY proxy."""
    docker = pytest.importorskip("docker")
    client = docker.from_env()
    client.ping()
    image = os.getenv("DROWAI_RUNTIME_IMAGE", default_runtime_image_for_machine())
    try:
        client.images.get(image)
    except Exception as exc:
        pytest.fail(f"Runtime image `{image}` is not available locally: {exc}")

    task_id = 73001
    tenant_id = 7
    runner_id = "runner-installed-command-proof"
    runtime_job_id = f"runtime-job-{uuid.uuid4().hex}"
    container_name = f"drowai-shell-contract-{uuid.uuid4().hex[:12]}"
    container = client.containers.run(
        image,
        name=container_name,
        entrypoint="/bin/bash",
        command=["-lc", "sleep 120"],
        detach=True,
        tty=False,
        stdin_open=False,
    )
    provider_session_ids: list[str] = []
    read_cursor = 0
    try:
        docker_runtime = RunnerDockerRuntime(client_factory=lambda: client)
        pty_adapter = _RunnerPtyAdapter(docker_runtime=docker_runtime)
        job_store = initialize_runner_job_store(tmp_path / "jobs.sqlite")
        job_store.start_job(
            runtime_job_id=runtime_job_id,
            tenant_id=str(tenant_id),
            task_id=str(task_id),
            workspace_id=f"task-{task_id}",
            image=image,
            container_id=str(container.id),
        )
        job_store.mark_running(runtime_job_id, container_id=str(container.id))
        terminal_proxy = RunnerTerminalProxy(job_store=job_store, pty_adapter=pty_adapter)

        class _RuntimeImageOperationService:
            def __init__(self, _db) -> None:
                pass

            def context_for_internal_task(self, **_kwargs):
                return _runtime_context(task_id=task_id, tenant_id=tenant_id, runner_id=runner_id)

            async def run_for_context(
                self,
                *,
                context,
                operation: str,
                call,
                payload=None,
                metadata=None,
            ):
                del context, call, metadata
                nonlocal read_cursor
                payload = dict(payload or {})
                if operation == "get_runtime_status":
                    return SimpleNamespace(
                        ok=True,
                        metadata={
                            "delegate_result": {
                                "job_status": "running",
                                "container_status": "running",
                            }
                        },
                    )
                if operation == "open_terminal_session":
                    response = terminal_proxy.open_terminal_session(
                        runtime_job_id=runtime_job_id,
                        session_name=str(payload.get("session_name") or "terminal"),
                        cols=int(payload.get("cols") or 120),
                        rows=int(payload.get("rows") or 30),
                    )
                    delegate = dict(response.metadata or {})
                    provider_session_ids.append(str(delegate.get("session_id") or ""))
                    return _runtime_operation_result(response, delegate=delegate)
                if operation == "send_terminal_input":
                    raw_data = payload.get("data", "")
                    data = (
                        raw_data.decode("utf-8", errors="replace")
                        if isinstance(raw_data, bytes)
                        else str(raw_data)
                    )
                    response = terminal_proxy.send_terminal_input(
                        session_id=str(payload["session_id"]),
                        data=data,
                    )
                    return _runtime_operation_result(response, delegate={})
                if operation == "read_terminal_output":
                    response = terminal_proxy.read_terminal_output(
                        session_id=str(payload["session_id"]),
                        max_bytes=int(payload.get("size") or 4096),
                    )
                    output = ""
                    if isinstance(response.metadata, dict):
                        output = str(response.metadata.get("output") or "")
                    read_cursor += 1
                    return _runtime_operation_result(
                        response,
                        delegate={"data": output, "next_cursor": read_cursor},
                    )
                if operation == "close_terminal_session":
                    response = terminal_proxy.close_terminal_session(
                        session_id=str(payload["session_id"])
                    )
                    return _runtime_operation_result(response, delegate={})
                raise AssertionError(operation)

        monkeypatch.setattr(
            terminal_manager_module,
            "RuntimeOperationService",
            _RuntimeImageOperationService,
        )
        monkeypatch.setattr(
            terminal_manager_module,
            "SessionLocal",
            lambda: SimpleNamespace(close=lambda: None),
        )
        terminal_manager = TerminalSessionManager()
        monkeypatch.setattr(
            terminal_manager,
            "_resolve_internal_runtime_context",
            lambda *, task_id, session_name: _runtime_context(
                task_id=task_id,
                tenant_id=tenant_id,
                runner_id=runner_id,
            ),
        )
        shell_service = ShellSessionService(
            terminal_manager=terminal_manager,
            config=ShellSessionServiceConfig(
                max_active_per_owner=2,
                max_active_per_task=4,
                idle_timeout_sec=300.0,
                cleanup_interval_sec=60.0,
                termination_grace_sec=0.0,
                terminal_io_grace_sec=0.05,
            ),
            runtime_context_resolver=lambda _identity: _runtime_context(
                task_id=task_id,
                tenant_id=tenant_id,
                runner_id=runner_id,
            ),
        )

        update = asyncio.run(
            shell_service.execute(
                identity=_runtime_identity(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    runner_id=runner_id,
                ),
                request=ShellExecRequest(command="nmap --version", yield_time_ms=5_000),
            )
        )

        assert update.process_status is ShellProcessStatus.COMPLETED, update.model_dump()
        assert update.success is True
        assert update.exit_code == 0
        assert update.session_id is None
        assert update.stdin_available is False
        assert "Nmap version" in update.stdout
        assert "https://nmap.org" in update.stdout
        public_payload = update.model_dump_json()
        assert all(
            provider_session_id not in public_payload
            for provider_session_id in provider_session_ids
            if provider_session_id
        )
    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass
