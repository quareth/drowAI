"""Runner-owned Docker runtime operations and config builders.

This module stays backend-free and owns runner-side image/container lifecycle
operations behind an injectable Docker client interface that is testable with
fakes.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from drowai_runner.runtime_image import RuntimeManifestVerification, verify_runtime_info_payload
from runtime_shared.docker_contracts import (
    CONTAINER_RUNTIME_INPUT_PATH,
    CONTAINER_VPN_CONFIG_PATH,
    CONTAINER_WORKSPACE_PATH,
    DEFAULT_RESOURCE_LIMITS,
    WORKSPACE_CONTROL_MOUNT_POLICY,
    build_container_volumes,
    build_fail_closed_runtime_command,
    build_runtime_contract_environment,
    build_runtime_pythonpath,
    build_runtime_startup_command,
)
from runtime_shared.workspace_bind_paths import resolve_workspace_bind_source
from runtime_shared.docker_network_manager import (
    DockerTaskNetworkManager,
    NetworkProvisionResult,
)
from runtime_shared.runtime_image_contract import is_digest_pinned_runtime_image
from runtime_shared.runtime_network import build_runtime_network_spec, parse_runtime_network_pool

_RUNTIME_INFO_COMMAND = [
    "python3",
    "/opt/drowai/runtime/python/executor_daemon.py",
    "--runtime-info",
]
_TENANT_SEGMENT_PATTERN = re.compile(r"[^a-z0-9-]+")
_PENTEST_RUNTIME_CAPABILITIES = ["NET_ADMIN"]
_PENTEST_RUNTIME_USER = "root"
_RUNNER_NETWORK_OWNER = "runner"

logger = logging.getLogger(__name__)


def _coerce_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _sanitize_tenant_segment(tenant_id: str | int) -> str:
    normalized = str(tenant_id).strip().lower()
    normalized = _TENANT_SEGMENT_PATTERN.sub("-", normalized).strip("-")
    return normalized or "tenant"


def build_runner_container_name(*, tenant_id: str | int, task_id: int) -> str:
    """Build a deterministic tenant/task-safe container name."""
    tenant_segment = _sanitize_tenant_segment(tenant_id)
    return f"drowai-{tenant_segment}-task-{task_id}"


def _vpn_capabilities_enabled(vpn_enabled: bool) -> bool:
    return vpn_enabled


def build_runner_container_config(
    *,
    tenant_id: str | int = "tenant",
    task_id: int,
    image_name: str,
    workspace_path: Path,
    control_path: Path | None = None,
    mount_policy: str = WORKSPACE_CONTROL_MOUNT_POLICY,
    runtime_path_source: str = "image-internal",
    activation_reason: str = "fixed_image_internal",
    vpn_enabled: bool = False,
    extra_environment: Mapping[str, str] | None = None,
    runner_root: Path | None = None,
    host_bind_root: Path | None = None,
    network_name: str | None = None,
) -> dict[str, Any]:
    """Build a backend-free Docker create payload for runner-owned execution."""
    resolved_runner_root = (runner_root or workspace_path.parent).expanduser().resolve()
    resolved_control_path = control_path or (
        resolved_runner_root / "control" / workspace_path.name
    )
    workspace_bind_source = resolve_workspace_bind_source(
        Path(workspace_path),
        runner_root=resolved_runner_root,
        host_bind_root=host_bind_root.expanduser().resolve() if host_bind_root else None,
    )
    control_bind_source = resolve_workspace_bind_source(
        Path(resolved_control_path),
        runner_root=resolved_runner_root,
        host_bind_root=host_bind_root.expanduser().resolve() if host_bind_root else None,
    )
    volumes = build_container_volumes(
        workspace_mount_source=workspace_bind_source,
        control_mount_source=control_bind_source,
        mount_policy=mount_policy,
    )
    command = build_runtime_startup_command(
        runtime_path_source=runtime_path_source,
        activation_reason=activation_reason,
    )
    environment = {
        "TASK_ID": str(task_id),
        "WORKSPACE": CONTAINER_WORKSPACE_PATH,
        "PYTHONPATH": build_runtime_pythonpath(),
        "DROWAI_RUNTIME_PATH_SOURCE": runtime_path_source,
        "DROWAI_MOUNT_POLICY": mount_policy,
        "DROWAI_RUNTIME_INPUT_PATH": CONTAINER_RUNTIME_INPUT_PATH,
        "VPN_ENABLED": "true" if vpn_enabled else "false",
    }
    environment.update(build_runtime_contract_environment())
    if vpn_enabled:
        # Point the in-container vpn-manager at the workspace-materialized config
        # so the startup connect step resolves the same file the control plane
        # wrote via the runtime.vpn.config operation.
        environment["VPN_CONFIG"] = CONTAINER_VPN_CONFIG_PATH
    if extra_environment:
        environment.update(dict(extra_environment))

    config: dict[str, Any] = {
        "name": build_runner_container_name(tenant_id=tenant_id, task_id=task_id),
        "image": image_name,
        "environment": environment,
        "volumes": volumes,
        "detach": True,
        "tty": False,
        "stdin_open": False,
        "user": _PENTEST_RUNTIME_USER,
        "cap_add": list(_PENTEST_RUNTIME_CAPABILITIES),
        "command": ["/bin/bash", "-c", build_fail_closed_runtime_command(command)],
        "working_dir": "/opt/drowai/runtime/python",
    }
    if _vpn_capabilities_enabled(vpn_enabled):
        config["devices"] = ["/dev/net/tun:/dev/net/tun:rwm"]
    if network_name:
        config["network"] = network_name
        config["environment"]["DROWAI_RUNTIME_NETWORK"] = network_name
    config.update(DEFAULT_RESOURCE_LIMITS)
    return config


@dataclass(frozen=True, slots=True)
class ExecProbeResult:
    """Exec probe response for lightweight runtime checks."""

    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class RunnerDockerRuntime:
    """Runner-owned Docker runtime wrapper with fake-client-friendly operations."""

    client_factory: Callable[[], Any]

    def _client(self) -> Any:
        return self.client_factory()

    def ensure_runtime_image(
        self,
        image_name: str,
        *,
        pull_if_missing: bool,
        refresh_if_tagged: bool = False,
    ) -> bool:
        """Ensure an image exists and optionally refresh mutable tagged references."""
        client = self._client()
        image_exists = False
        try:
            client.images.get(image_name)
        except Exception:
            if not pull_if_missing:
                raise
        else:
            image_exists = True

        should_refresh = (
            image_exists
            and refresh_if_tagged
            and not is_digest_pinned_runtime_image(image_name)
        )
        if image_exists and not should_refresh:
            return False

        try:
            client.images.pull(image_name)
        except Exception:
            if not image_exists:
                raise
            logger.warning(
                "Failed to refresh tagged runtime image %s; using the existing local image.",
                image_name,
            )
            return False
        return True

    def ensure_task_network(
        self,
        *,
        tenant_id: str | int,
        task_id: str | int,
        container_name: str,
        runtime_identity: str,
        pool_cidr: str,
    ) -> NetworkProvisionResult:
        """Atomically create or validate the runner-owned per-task bridge."""
        spec = build_runtime_network_spec(
            container_name=container_name,
            runtime_identity=runtime_identity,
            tenant_id=tenant_id,
            task_id=task_id,
            runtime_owner=_RUNNER_NETWORK_OWNER,
            pool=parse_runtime_network_pool(pool_cidr),
        )
        return DockerTaskNetworkManager(self.client_factory).ensure(spec)

    def remove_task_network(
        self,
        *,
        tenant_id: str | int,
        task_id: str | int,
        container_name: str,
        runtime_identity: str,
        pool_cidr: str,
    ) -> bool:
        """Remove an empty, correctly owned runner bridge; never disconnect endpoints."""
        spec = build_runtime_network_spec(
            container_name=container_name,
            runtime_identity=runtime_identity,
            tenant_id=tenant_id,
            task_id=task_id,
            runtime_owner=_RUNNER_NETWORK_OWNER,
            pool=parse_runtime_network_pool(pool_cidr),
        )
        return DockerTaskNetworkManager(self.client_factory).remove_empty(spec)

    def remove_orphan_task_network(self, container_name: str) -> bool:
        """Remove an empty runner-owned bridge discovered during recovery."""
        from runtime_shared.runtime_network import build_runtime_network_name

        return DockerTaskNetworkManager(self.client_factory).remove_empty_owned(
            name=build_runtime_network_name(container_name),
            runtime_owner=_RUNNER_NETWORK_OWNER,
        )

    def create_container(self, config: Mapping[str, Any]) -> str:
        """Create a container from a config payload and return id."""
        container = self._client().containers.create(**dict(config))
        return str(container.id)

    def start_container(self, container_id: str) -> None:
        self._client().containers.get(container_id).start()

    def stop_container(self, container_id: str, *, timeout_seconds: int = 10) -> None:
        self._client().containers.get(container_id).stop(timeout=timeout_seconds)

    def pause_container(self, container_id: str) -> None:
        self._client().containers.get(container_id).pause()

    def resume_container(self, container_id: str) -> None:
        self._client().containers.get(container_id).unpause()

    def remove_container(self, container_id: str, *, force: bool = False) -> None:
        try:
            container = self._client().containers.get(container_id)
        except Exception as exc:
            if _is_container_not_found_error(exc):
                return
            raise
        container.remove(force=force)

    def find_container_id_by_name(self, container_name: str) -> str | None:
        """Return a container id by deterministic name, or None when absent."""
        try:
            container = self._client().containers.get(container_name)
        except Exception as exc:
            if _is_container_not_found_error(exc):
                return None
            raise
        return str(container.id)

    def send_signal(self, container_id: str, signal_name: str) -> tuple[bool, str | None]:
        """Best-effort signal delivery helper for runtime-input notifications."""
        try:
            self._client().containers.get(container_id).kill(signal=signal_name)
        except Exception as exc:
            return (False, str(exc))
        return (True, None)

    def container_logs(self, container_id: str, *, tail: int = 200) -> str:
        logs = self._client().containers.get(container_id).logs(tail=tail, timestamps=True)
        return _coerce_output(logs)

    def container_status(self, container_id: str) -> str:
        container = self._client().containers.get(container_id)
        container.reload()
        return str(getattr(container, "status", "unknown"))

    def container_metrics(self, container_id: str) -> dict[str, Any]:
        stats = self._client().containers.get(container_id).stats(stream=False)
        memory_usage = ((stats.get("memory_stats") or {}).get("usage"))
        memory_limit = ((stats.get("memory_stats") or {}).get("limit"))
        cpu_total = (
            ((stats.get("cpu_stats") or {}).get("cpu_usage") or {}).get("total_usage")
        )
        return {
            "memory_usage": memory_usage,
            "memory_limit": memory_limit,
            "cpu_total_usage": cpu_total,
        }

    def exec_probe(
        self,
        container_id: str,
        command: list[str],
        *,
        timeout_seconds: int = 10,
    ) -> ExecProbeResult:
        """Run a bounded command probe in the runtime container."""
        container = self._client().containers.get(container_id)
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(container.exec_run, command)
        try:
            exit_code, output = future.result(timeout=max(1, timeout_seconds))
        except TimeoutError:
            future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            return ExecProbeResult(
                exit_code=124,
                stdout="",
                stderr=f"Probe timed out after {max(1, timeout_seconds)} seconds.",
            )
        finally:
            if not future.cancelled():
                executor.shutdown(wait=False, cancel_futures=True)
        return ExecProbeResult(
            exit_code=int(exit_code),
            stdout=_coerce_output(output),
            stderr="",
        )

    def verify_runtime_manifest(self, container_id: str) -> RuntimeManifestVerification:
        """Read and validate runtime manifest contract from the running container."""
        probe = self.exec_probe(container_id, _RUNTIME_INFO_COMMAND)
        if probe.exit_code != 0:
            raise RuntimeError(f"Runtime info probe failed: {probe.stdout.strip()}")
        try:
            payload = json.loads(probe.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Runtime info probe returned invalid JSON payload.") from exc
        return verify_runtime_info_payload(payload)


def _is_container_not_found_error(exc: Exception) -> bool:
    """Return true for Docker SDK/container client not-found errors."""
    class_name = exc.__class__.__name__.lower()
    message = str(exc).lower()
    return (
        isinstance(exc, KeyError)
        or class_name == "notfound"
        or "no such container" in message
        or "not found" in message
    )
