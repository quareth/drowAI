"""Backend-free Docker runtime contracts shared by backend and runner code.

This module centralizes stable runtime paths, mount-policy names, startup
command fragments, and default resource limits used by the packaged runtime
image contract. It intentionally contains deterministic helpers only.
"""

from __future__ import annotations

from typing import Dict, List

from runtime_shared.runtime_manifest import (
    FILE_COMM_SCHEMA_VERSION,
    RUNTIME_CONTRACT_VERSION,
    SEMANTIC_SCHEMA_VERSIONS,
    WORKSPACE_LAYOUT_VERSION,
)

RUNTIME_PATH_MODE_IMAGE_INTERNAL = "image-internal"
RUNTIME_PATH_SOURCE_IMAGE_INTERNAL = "image-internal"

IMAGE_INTERNAL_PYTHON_ROOT = "/opt/drowai/runtime/python"
IMAGE_INTERNAL_VPN_SCRIPT_PATH = "/opt/drowai/runtime/vpn/vpn-manager.sh"
CONTAINER_WORKSPACE_PATH = "/workspace"
CONTAINER_CONTROL_PATH = "/run/drowai/control"
CONTAINER_RUNTIME_INPUT_PATH = (
    f"{CONTAINER_CONTROL_PATH}/runtime-input/user_input.jsonl"
)

# Canonical in-workspace OpenVPN config location for runner-owned containers.
# The workspace is already task-scoped, so a fixed file name is sufficient and
# keeps the runner write target, the container ``VPN_CONFIG`` env, and the
# reconnect probe aligned on a single source of truth.
RUNNER_VPN_CONFIG_FILE_NAME = "task.ovpn"
CONTAINER_VPN_CONFIG_PATH = (
    f"{CONTAINER_CONTROL_PATH}/vpn/{RUNNER_VPN_CONFIG_FILE_NAME}"
)

WORKSPACE_CONTROL_MOUNT_POLICY = "workspace-control"

DEFAULT_RESOURCE_LIMITS: Dict[str, object] = {
    "mem_limit": "2g",
    "memswap_limit": "2g",
    "cpu_period": 100000,
    "cpu_quota": 150000,
    "shm_size": "256m",
    "ulimits": [
        {"name": "nofile", "soft": 65536, "hard": 65536},
        {"name": "nproc", "soft": 4096, "hard": 4096},
    ],
}


def build_runtime_contract_environment() -> Dict[str, str]:
    """Return the expected runtime-image contract environment."""
    import json

    return {
        "DROWAI_EXPECTED_RUNTIME_CONTRACT_VERSION": RUNTIME_CONTRACT_VERSION,
        "DROWAI_EXPECTED_FILE_COMM_SCHEMA_VERSION": FILE_COMM_SCHEMA_VERSION,
        "DROWAI_EXPECTED_WORKSPACE_LAYOUT_VERSION": WORKSPACE_LAYOUT_VERSION,
        "DROWAI_EXPECTED_SEMANTIC_SCHEMA_VERSIONS": json.dumps(
            dict(SEMANTIC_SCHEMA_VERSIONS), sort_keys=True
        ),
    }


def build_fail_closed_runtime_command(base_command: str) -> str:
    """Probe the image manifest and start only when every contract matches."""
    probe = (
        "python3 -c \"import json, os, subprocess, sys; "
        "payload=json.loads(subprocess.check_output(["
        "'python3','/opt/drowai/runtime/python/executor_daemon.py','--runtime-info'"
        "],text=True)); "
        "expected={'runtime_contract_version':os.environ.get('DROWAI_EXPECTED_RUNTIME_CONTRACT_VERSION'),"
        "'file_comm_schema_version':os.environ.get('DROWAI_EXPECTED_FILE_COMM_SCHEMA_VERSION'),"
        "'workspace_layout_version':os.environ.get('DROWAI_EXPECTED_WORKSPACE_LAYOUT_VERSION'),"
        "'semantic_schema_versions':json.loads(os.environ.get('DROWAI_EXPECTED_SEMANTIC_SCHEMA_VERSIONS','{}'))}; "
        "mismatch=[key for key,value in expected.items() if payload.get(key)!=value]; "
        "sys.exit(0 if not mismatch else 23)\""
    )
    return f"{probe} && {base_command}"


def build_runtime_pythonpath() -> str:
    """Return the runtime PYTHONPATH for image-internal startup."""
    return IMAGE_INTERNAL_PYTHON_ROOT


def build_runtime_startup_command(
    runtime_path_source: str,
    activation_reason: str,
    vpn_script_path: str = IMAGE_INTERNAL_VPN_SCRIPT_PATH,
) -> str:
    """Build deterministic runtime startup command chain."""
    return (
        f"echo '[runtime-mode] source={runtime_path_source} "
        f"activation={activation_reason}'; "
        "if [ \"${VPN_ENABLED:-}\" = \"true\" ]; then "
        f"echo '[runtime-mode] vpn_script={vpn_script_path}'; "
        "if [ -f \"${VPN_CONFIG:-/vpn/task.ovpn}\" ]; then "
        f"echo '[runtime-mode] Starting VPN via {vpn_script_path}...' && "
        f"bash {vpn_script_path} connect || echo '[runtime-mode] VPN start failed'; "
        "else "
        "echo '[runtime-mode] VPN config pending; waiting for runtime materialization'; "
        "fi; "
        "fi; "
        f"python3 {IMAGE_INTERNAL_PYTHON_ROOT}/workspace_init.py && "
        f"python3 {IMAGE_INTERNAL_PYTHON_ROOT}/executor_daemon.py"
    )


def build_workspace_bootstrap_commands(task_id: int) -> List[str]:
    """Return canonical post-start workspace bootstrap commands."""
    workspaces_root = f"{IMAGE_INTERNAL_PYTHON_ROOT}/workspaces"
    return [
        f"mkdir -p {workspaces_root}",
        f"ln -sf {CONTAINER_WORKSPACE_PATH} {workspaces_root}/task-{task_id}",
        f"ls -la {workspaces_root}/",
        f"ls -la {CONTAINER_WORKSPACE_PATH}/",
    ]


def build_container_volumes(
    workspace_mount_source: str,
    control_mount_source: str,
    mount_policy: str,
) -> Dict[str, Dict[str, str]]:
    """Build the writable data and read-only control mounts for task containers."""
    if mount_policy != WORKSPACE_CONTROL_MOUNT_POLICY:
        raise ValueError(f"Unsupported mount policy: {mount_policy}")
    return {
        workspace_mount_source: {"bind": CONTAINER_WORKSPACE_PATH, "mode": "rw"},
        control_mount_source: {"bind": CONTAINER_CONTROL_PATH, "mode": "ro"},
    }
