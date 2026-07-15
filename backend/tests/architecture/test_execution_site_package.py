"""Runner Site customer package layout and pack script tests."""

from __future__ import annotations

import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from scripts import package_execution_site

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PACKAGE_TEMPLATE = _REPO_ROOT / "deploy/cloud/execution-site-package"
_PACK_SCRIPT = _REPO_ROOT / "scripts/package_execution_site.py"
_DOCKER_SOCKET_MOUNT = "/var/run/docker.sock:/var/run/docker.sock"

_REQUIRED_TAR_MEMBERS = (
    "drowai-runner-site/install.sh",
    "drowai-runner-site/compose.yml",
    "drowai-runner-site/VERSION",
    "drowai-runner-site/scripts/build_runner_image.py",
    "drowai-runner-site/scripts/package_runner.py",
    "drowai-runner-site/drowai_runner",
    "drowai-runner-site/runtime_shared",
    "drowai-runner-site/deploy/images/Dockerfile.runner",
)


def _service_block(content: str, service: str) -> str:
    marker = f"  {service}:\n"
    start = content.find(marker)
    assert start != -1, f"missing service block: {service}"
    next_service = content.find("\n  ", start + len(marker))
    while next_service != -1:
        following = content[next_service + 1 :]
        if following.startswith("  ") and not following.startswith("    "):
            return content[start:next_service]
        next_service = content.find("\n  ", next_service + 1)
    return content[start:]


def test_execution_site_package_template_exists() -> None:
    compose = (_PACKAGE_TEMPLATE / "compose.yml").read_text(encoding="utf-8")
    lowered = compose.lower()
    runner_enrollment = _service_block(compose, "runner-enrollment")
    runner_config = _service_block(compose, "runner-config")
    runner = _service_block(compose, "runner")

    assert "include:" not in lowered
    assert "runner:" in lowered
    assert "deploy/images/dockerfile.runner" in lowered
    assert "network: ${drowai_docker_build_network:-host}" in lowered
    assert "/var/lib/drowai/config/enrollment.toml" in lowered
    assert "drowai_runner_tenant_id" not in lowered
    assert "drowai_runner_registration_token" not in lowered
    assert _DOCKER_SOCKET_MOUNT not in runner_enrollment
    assert _DOCKER_SOCKET_MOUNT not in runner_config
    assert _DOCKER_SOCKET_MOUNT in runner
    assert (_PACKAGE_TEMPLATE / "install.sh").is_file()


def test_runner_dockerfile_does_not_require_apt_for_docker_cli() -> None:
    dockerfile = (_REPO_ROOT / "deploy/images/Dockerfile.runner").read_text(encoding="utf-8")
    assert "FROM python:3.11-slim-bookworm" in dockerfile
    assert "apt-get" not in dockerfile
    assert "docker-ce-cli" not in dockerfile


def test_package_execution_site_check_passes() -> None:
    proc = subprocess.run(
        [sys.executable, str(_PACK_SCRIPT), "--check"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_package_execution_site_builds_tarball_with_required_layout() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        output_path = Path(temp_dir) / "drowai-runner-site-test.tar.gz"
        proc = subprocess.run(
            [
                sys.executable,
                str(_PACK_SCRIPT),
                "--output",
                str(output_path),
            ],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert output_path.is_file()

        with tarfile.open(output_path, "r:gz") as archive:
            names = set(archive.getnames())
        for member in _REQUIRED_TAR_MEMBERS:
            assert any(name == member or name.startswith(f"{member}/") for name in names), member


def test_package_execution_site_embeds_enrollment_toml() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        enrollment_path = temp_path / "enrollment.toml"
        enrollment_path.write_text("[runner]\nregistration_token = \"rit_test\"\n", encoding="utf-8")
        output_path = temp_path / "drowai-runner-site-test.tar.gz"
        proc = subprocess.run(
            [
                sys.executable,
                str(_PACK_SCRIPT),
                "--enrollment-toml",
                str(enrollment_path),
                "--output",
                str(output_path),
            ],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

        with tarfile.open(output_path, "r:gz") as archive:
            names = set(archive.getnames())
            embedded = archive.extractfile("drowai-runner-site/config/enrollment.toml")
            assert embedded is not None
            content = embedded.read().decode("utf-8")
        assert "drowai-runner-site/config/enrollment.toml" in names
        assert 'registration_token = "rit_test"' in content
        assert "tenant_id" not in content


def test_package_execution_site_embeds_manifest_when_docs_are_absent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "runtime/manifests/runtime-package-manifest.md"
    monkeypatch.setattr(package_execution_site, "DEFAULT_MANIFEST_PATH", tmp_path / "missing-active.md")

    package_execution_site._write_manifest_snapshot(target)

    content = target.read_text(encoding="utf-8")
    assert '"runner_package"' in content
    assert '"drowai_runner"' in content
    assert '"runtime_shared"' in content
