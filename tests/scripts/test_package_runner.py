"""Tests for runner package check/build script behavior."""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import json
from pathlib import Path

from scripts import package_runner


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "package_runner.py"


def test_package_runner_check_mode_passes() -> None:
    proc = subprocess.run(
        ["python3", str(SCRIPT_PATH), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "[package-runner] runner_version=" in proc.stdout
    assert "PASS: runner packaging checks passed." in proc.stdout


def test_package_runner_build_mode_writes_tarball(tmp_path: Path) -> None:
    output_path = tmp_path / "drowai-runner-package.tar.gz"
    proc = subprocess.run(
        ["python3", str(SCRIPT_PATH), "--output", str(output_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert output_path.exists()
    with tarfile.open(output_path, "r:gz") as archive:
        names = set(archive.getnames())
    assert "drowai_runner" in names
    assert "runtime_shared" in names
    assert "bin/drowai-runner" in names


def test_packaged_runner_cli_starts_without_backend_source(tmp_path: Path) -> None:
    output_path = tmp_path / "drowai-runner-package.tar.gz"
    build = subprocess.run(
        ["python3", str(SCRIPT_PATH), "--output", str(output_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0

    install_root = tmp_path / "install-root"
    install_root.mkdir()
    with tarfile.open(output_path, "r:gz") as archive:
        archive.extractall(install_root)

    runner = subprocess.run(
        [sys.executable, "-m", "drowai_runner", "health"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(install_root),
            # Keep health outcome deterministic across hosts by disabling docker lookup.
            "PATH": str(tmp_path / "empty-bin"),
        },
    )
    assert runner.returncode == 3
    assert '"status": "failed"' in runner.stdout


def test_dependency_violations_use_manifest_management_prefixes(tmp_path: Path) -> None:
    temp_root = REPO_ROOT / "tmp_runner_manifest_test_root"
    temp_root.mkdir(exist_ok=True)
    try:
        (temp_root / "module.py").write_text(
            "from backend.services.runtime_provider import registry\n",
            encoding="utf-8",
        )
        manifest_payload = {
            "runner_package": {"python_roots": [str(temp_root.relative_to(REPO_ROOT))]},
            "management_only": {
                "python_module_prefixes": [
                    "backend.services.runtime_provider",
                ]
            },
        }
        manifest_file = tmp_path / "manifest.md"
        manifest_file.write_text(
            "```json\n" + json.dumps(manifest_payload) + "\n```",
            encoding="utf-8",
        )
        payload = package_runner._load_manifest(manifest_file)  # noqa: SLF001
        forbidden = package_runner._runner_forbidden_import_prefixes(payload)  # noqa: SLF001
        violations = package_runner._dependency_violations(  # noqa: SLF001
            roots=[str(temp_root.relative_to(REPO_ROOT))],
            forbidden_prefixes=forbidden,
        )
        assert violations
        assert "backend.services.runtime_provider" in violations[0]
    finally:
        for item in temp_root.rglob("*"):
            if item.is_file():
                item.unlink()
        temp_root.rmdir()
