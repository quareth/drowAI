"""Shared deployment environment defaults for runner-based product profiles.

Centralizes standalone and Runner Site env keys so compose examples,
verification scripts, and local launchers stay aligned without duplication.
"""

from __future__ import annotations

import json
from typing import Mapping

DEFAULT_RUNNER_CAPABILITIES: tuple[str, ...] = (
    "docker",
    "file_comm",
    "pty",
    "terminal_stream_v1",
    "tool_command.v1",
    "tooling_plane.commands.v1",
)

DEFAULT_RUNNER_DATA_DIR = "/var/lib/drowai"
DEFAULT_RUNNER_IMAGE = "drowai/runner:local"
_PRODUCT_RUNTIME_POLICY_PROFILES = frozenset({"single_host", "distributed"})


def product_runtime_policy_env(
    *,
    profile: str,
    object_store_backend: str = "local",
) -> dict[str, str]:
    """Return generated product runtime policy env for Management processes."""
    normalized_profile = str(profile or "").strip().lower()
    if normalized_profile not in _PRODUCT_RUNTIME_POLICY_PROFILES:
        raise ValueError(f"Unsupported product deployment profile: {profile}")
    return {
        "DROWAI_DEPLOYMENT_PROFILE": normalized_profile,
        "TASK_RUNTIME_PLACEMENT_MODE_DEFAULT": "runner",
        "ENABLE_CLOUD_RUNNER_CONTROL": "true",
        "RUNNER_TOOL_COMMAND_ENABLED": "true",
        "DATA_PLANE_OBJECT_STORE_BACKEND": (
            str(object_store_backend or "local").strip().lower()
        ),
    }


def runner_control_env(
    *,
    control_plane_url: str,
    runtime_image: str | None = None,
    runner_root: str = DEFAULT_RUNNER_DATA_DIR,
    host_bind_root: str = DEFAULT_RUNNER_DATA_DIR,
    tls_verify: bool = True,
    allow_insecure_cloud_endpoint: bool = False,
    dev_mode: bool = False,
    labels: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return runner env required for managed Management connectivity."""
    resolved_labels = labels or {
        "deployment": "runner-site",
        "site": "on-prem",
    }
    env = {
        "DROWAI_RUNNER_CONTROL_PLANE_URL": control_plane_url.strip(),
        "DROWAI_RUNNER_ROOT": runner_root,
        "DROWAI_RUNNER_HOST_BIND_ROOT": host_bind_root,
        "DROWAI_RUNNER_TLS_VERIFY": "true" if tls_verify else "false",
        "DROWAI_RUNNER_ALLOW_INSECURE_CLOUD_ENDPOINT": (
            "true" if allow_insecure_cloud_endpoint else "false"
        ),
        "DROWAI_RUNNER_DEV_MODE": "true" if dev_mode else "false",
        "DROWAI_RUNNER_LABELS": json.dumps(dict(resolved_labels), sort_keys=True),
        "DROWAI_RUNNER_CAPABILITIES": ",".join(DEFAULT_RUNNER_CAPABILITIES),
        "PYTHONUNBUFFERED": "1",
    }
    if runtime_image:
        env["DROWAI_RUNTIME_IMAGE"] = runtime_image.strip()
    return env


def single_host_management_env(
    *,
    control_plane_url: str,
    runtime_image: str | None = None,
    runner_root: str = DEFAULT_RUNNER_DATA_DIR,
    host_bind_root: str = DEFAULT_RUNNER_DATA_DIR,
) -> dict[str, str]:
    """Return env shared by backend and runner in the standalone profile."""
    management = product_runtime_policy_env(profile="single_host")
    runner = runner_control_env(
        control_plane_url=control_plane_url,
        runtime_image=runtime_image,
        runner_root=runner_root,
        host_bind_root=host_bind_root,
        tls_verify=False,
        allow_insecure_cloud_endpoint=True,
        dev_mode=False,
        labels={
            "deployment": "standalone-platform",
            "site": "local",
        },
    )
    return {**management, **runner}


def execution_site_env(
    *,
    control_plane_url: str,
    runtime_image: str | None = None,
    runner_root: str = DEFAULT_RUNNER_DATA_DIR,
    host_bind_root: str = DEFAULT_RUNNER_DATA_DIR,
) -> dict[str, str]:
    """Return env for a remote Runner Site connecting to Management."""
    return runner_control_env(
        control_plane_url=control_plane_url,
        runtime_image=runtime_image,
        runner_root=runner_root,
        host_bind_root=host_bind_root,
        tls_verify=True,
        allow_insecure_cloud_endpoint=False,
        dev_mode=False,
    )


def required_env_keys(profile: str) -> frozenset[str]:
    """Return required operator-supplied env keys for a deployment profile."""
    if profile in {"execution-site", "standalone"}:
        return frozenset()
    raise ValueError(f"Unknown deployment profile: {profile}")
