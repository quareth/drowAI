import subprocess
from pathlib import Path
from typing import Callable, Dict, List

import pytest
from pytest import MonkeyPatch


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """Create a temporary workspace with an artifacts directory."""
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(exist_ok=True)
    return tmp_path


@pytest.fixture
def mock_subprocess_run() -> Callable[..., subprocess.CompletedProcess]:
    """Factory to mock subprocess.run returning a configurable CompletedProcess."""

    def _factory(stdout: str = "", stderr: str = "", returncode: int = 0):
        def _runner(cmd, capture_output, text, timeout):
            return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

        return _runner

    return _factory


def assert_execution_model_compliance(tool, args_model, *, stdout: str = "ok"):
    """Quick compliance helper validating build/parse/artifacts/run lifecycle."""
    cmd = tool.build_command(args_model)
    assert isinstance(cmd, list) and all(isinstance(p, str) for p in cmd)

    metadata = tool.parse_output(stdout, "", 0, args_model)
    assert isinstance(metadata, dict)

    artifacts = tool.create_artifacts(stdout, args_model, timestamp=1700000000)
    assert isinstance(artifacts, list)

    def _mock_run(command, capture_output, text, timeout):
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    result = None
    with MonkeyPatch().context() as mp:
        mp.setattr(subprocess, "run", _mock_run)
        result = tool.run(args_model)

    assert result is not None and result.success is True
    assert result.metadata

