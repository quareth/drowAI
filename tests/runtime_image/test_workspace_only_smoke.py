"""Workspace-only runtime-image smoke tests for packaged executor assets."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from runtime_shared.runtime_image_contract import default_runtime_image_for_machine

RUNTIME_IMAGE = os.getenv("DROWAI_RUNTIME_IMAGE", default_runtime_image_for_machine())
RUN_RUNTIME_IMAGE_SMOKE = os.getenv("RUN_RUNTIME_IMAGE_SMOKE") == "1"
DOCKER_AVAILABLE = shutil.which("docker") is not None


def _run_docker(args: list[str], *, workspace: Path | None = None) -> subprocess.CompletedProcess[str]:
    command = ["docker", "run", "--rm"]
    if workspace is not None:
        command.extend(["-v", f"{workspace}:/workspace"])
    command.extend([RUNTIME_IMAGE, *args])
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _ensure_runtime_image_available() -> None:
    inspect = subprocess.run(
        ["docker", "image", "inspect", RUNTIME_IMAGE],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspect.returncode != 0:
        pytest.fail(
            f"Runtime image `{RUNTIME_IMAGE}` is not available locally. "
            "Pull/build it first, then rerun the smoke test."
        )


def _load_json_from_stdout(stdout: str) -> dict[str, object]:
    """Parse the last JSON line from runtime command stdout."""
    for line in reversed((stdout or "").splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        if candidate.startswith("{") and candidate.endswith("}"):
            return json.loads(candidate)
    raise AssertionError("Expected JSON payload in runtime stdout output.")


@pytest.mark.integration
@pytest.mark.execution_plane_non_dind_regression
@pytest.mark.skipif(
    not DOCKER_AVAILABLE or not RUN_RUNTIME_IMAGE_SMOKE,
    reason="Docker/runtime image smoke disabled (set RUN_RUNTIME_IMAGE_SMOKE=1).",
)
def test_workspace_only_file_comm_smoke(tmp_path: Path) -> None:
    _ensure_runtime_image_available()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    init_result = _run_docker(
        ["python3", "/opt/drowai/runtime/python/workspace_init.py"],
        workspace=workspace,
    )
    assert init_result.returncode == 0, init_result.stderr

    command_id = str(uuid.uuid4())
    command_payload = {
        "id": command_id,
        "timestamp": "2026-01-01T00:00:00Z",
        "command": "printf smoke-target",
        "cwd": "/workspace",
        "env": {},
        "timeout": 5.0,
    }
    commands_file = workspace / "commands.jsonl"
    commands_file.write_text(json.dumps(command_payload) + "\n", encoding="utf-8")

    process_once = _run_docker(
        [
            "python3",
            "-c",
            (
                "import asyncio; "
                "from kali_executor.communication.file_comm import FileCommExecutor; "
                "from kali_executor.executor_daemon import process_commands_once; "
                "asyncio.run(process_commands_once(FileCommExecutor('/workspace'), '/workspace'))"
            ),
        ],
        workspace=workspace,
    )
    assert process_once.returncode == 0, process_once.stderr

    results_file = workspace / "results.jsonl"
    result_lines = [line for line in results_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert result_lines, "Expected at least one file-comm result row."

    result = json.loads(result_lines[-1])
    assert result["id"] == command_id
    assert result["success"] is True
    assert result["stdout"] == "smoke-target"


@pytest.mark.integration
@pytest.mark.execution_plane_non_dind_regression
@pytest.mark.skipif(
    not DOCKER_AVAILABLE or not RUN_RUNTIME_IMAGE_SMOKE,
    reason="Docker/runtime image smoke disabled (set RUN_RUNTIME_IMAGE_SMOKE=1).",
)
def test_runtime_image_packages_service_access_clients() -> None:
    _ensure_runtime_image_available()

    result = _run_docker(
        [
            "python3",
            "-c",
            (
                "import shutil; "
                "assert shutil.which('lftp'); "
                "assert shutil.which('ssh'); "
                "assert shutil.which('sshpass'); "
                "print('service-access-clients-ok')"
            ),
        ]
    )
    assert result.returncode == 0, result.stderr
    assert "service-access-clients-ok" in result.stdout


@pytest.mark.integration
@pytest.mark.execution_plane_non_dind_regression
@pytest.mark.skipif(
    not DOCKER_AVAILABLE or not RUN_RUNTIME_IMAGE_SMOKE,
    reason="Docker/runtime image smoke disabled (set RUN_RUNTIME_IMAGE_SMOKE=1).",
)
def test_runtime_image_requires_executor_daemon_entrypoint_path() -> None:
    _ensure_runtime_image_available()

    runtime_info = _run_docker(
        ["python3", "/opt/drowai/runtime/python/executor_daemon.py", "--runtime-info"]
    )
    assert runtime_info.returncode == 0, runtime_info.stderr
    payload = _load_json_from_stdout(runtime_info.stdout or "")
    assert payload["runtime_contract_version"]

    missing_path = _run_docker(
        ["python3", "/opt/drowai/runtime/python/executor_daemon.py.missing", "--runtime-info"]
    )
    assert missing_path.returncode != 0


@pytest.mark.integration
@pytest.mark.execution_plane_non_dind_regression
@pytest.mark.skipif(
    not DOCKER_AVAILABLE or not RUN_RUNTIME_IMAGE_SMOKE,
    reason="Docker/runtime image smoke disabled (set RUN_RUNTIME_IMAGE_SMOKE=1).",
)
def test_runtime_image_python_inventory_is_minimal_and_cache_free() -> None:
    _ensure_runtime_image_available()

    result = _run_docker(
        [
            "python3",
            "-c",
            (
                "import json, pathlib; "
                "root=pathlib.Path('/opt/drowai/runtime/python'); "
                "entry=root/'executor_daemon.py'; "
                "package_entry=root/'kali_executor/executor_daemon.py'; "
                "expected={'workspace_init.py','executor_daemon.py',"
                "'kali_executor/__init__.py','kali_executor/executor_daemon.py',"
                "'kali_executor/communication/__init__.py',"
                "'kali_executor/communication/file_comm.py',"
                "'runtime_shared/__init__.py',"
                "'runtime_shared/file_comm_contracts.py',"
                "'runtime_shared/runtime_manifest.py',"
                "'runtime_shared/vpn_observability.py'}; "
                "actual={str(path.relative_to(root)) for path in root.rglob('*.py')}; "
                "caches=[str(path.relative_to(root)) for path in root.rglob('*') "
                "if path.name == '__pycache__' or path.suffix in {'.pyc','.pyo'}]; "
                "assert actual == expected, (actual, expected); "
                "assert not caches, caches; "
                "assert entry.is_symlink(); "
                "assert entry.resolve() == package_entry.resolve(); "
                "print(json.dumps({'python_sources':sorted(actual),'caches':caches}))"
            ),
        ]
    )

    assert result.returncode == 0, result.stderr
