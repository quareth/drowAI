"""Tests for executor daemon runtime-info/version probes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _run_daemon_probe(flag: str) -> dict[str, object]:
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "kali_executor.executor_daemon",
            flag,
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    return json.loads((result.stdout or "").strip())


def test_runtime_info_probe_outputs_manifest() -> None:
    payload = _run_daemon_probe("--runtime-info")
    assert payload["runtime_contract_version"]
    assert payload["file_comm_schema_version"]
    assert payload["workspace_layout_version"]
    assert payload["semantic_schema_versions"]
    assert payload["supported_tool_families"]


def test_version_probe_outputs_manifest() -> None:
    payload = _run_daemon_probe("--version")
    assert payload["runtime_contract_version"]
