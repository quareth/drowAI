"""Tests for Dockerfile/manifest runtime-image copy-source parity checks."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from scripts import build_runtime_image


EXPECTED_RUNTIME_PYTHON_SOURCES = [
    "agent/workspace_init.py",
    "kali_executor/__init__.py",
    "kali_executor/communication/__init__.py",
    "kali_executor/communication/file_comm.py",
    "kali_executor/executor_daemon.py",
    "runtime_shared/__init__.py",
    "runtime_shared/file_comm_contracts.py",
    "runtime_shared/runtime_manifest.py",
    "runtime_shared/vpn_observability.py",
]


def test_kali_base_installs_and_verifies_fping() -> None:
    dockerfile = build_runtime_image.DEFAULT_KALI_BASE_DOCKERFILE_PATH.read_text(
        encoding="utf-8"
    )
    install_block = dockerfile.split("apt-get install", maxsplit=1)[1].split(
        "&& apt-get clean", maxsplit=1
    )[0]
    verification_block = dockerfile.split("for binary in", maxsplit=1)[1]

    assert "fping \\" in install_block
    assert "fping" in verification_block


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        "# manifest\n\n```json\n" + json.dumps(payload, indent=2) + "\n```\n",
        encoding="utf-8",
    )


def test_runtime_python_copy_sources_only_include_runtime_python_destination(
    tmp_path: Path,
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "\n".join(
            [
                "FROM base",
                "COPY agent/tools/ /opt/drowai/runtime/python/agent/tools/",
                "COPY backend/ /opt/drowai/runtime/backend/",
                "COPY ./kali_executor/ /opt/drowai/runtime/python/kali_executor/",
            ]
        ),
        encoding="utf-8",
    )

    sources = build_runtime_image._runtime_python_copy_sources(dockerfile)  # noqa: SLF001
    assert sources == ["agent/tools", "kali_executor"]


def test_disallowed_runtime_copy_sources_detects_management_roots(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "\n".join(
            [
                "FROM base",
                "COPY backend/scripts/vpn/vpn-manager.sh /opt/drowai/runtime/vpn/vpn-manager.sh",
                "COPY runtime/vpn/vpn-manager.sh /opt/drowai/runtime/vpn/vpn-manager.sh",
                "COPY agent/tools/ /opt/drowai/runtime/python/agent/tools/",
            ]
        ),
        encoding="utf-8",
    )

    copy_sources = build_runtime_image._runtime_copy_sources(dockerfile)  # noqa: SLF001
    disallowed = build_runtime_image._disallowed_runtime_copy_sources(copy_sources)  # noqa: SLF001
    assert disallowed == ["agent/tools", "backend/scripts/vpn/vpn-manager.sh"]


def test_expected_runtime_python_sources_merge_roots_and_entrypoints(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.md"
    _write_manifest(
        manifest_path,
        {
            "runtime_image": {
                "python_roots": [
                    "agent/tools",
                    "runtime_shared",
                ],
                "required_entrypoint_sources": [
                    "agent/workspace_init.py",
                    "kali_executor/executor_daemon.py",
                ],
            }
        },
    )
    payload = build_runtime_image._load_manifest(manifest_path)  # noqa: SLF001

    expected = build_runtime_image._expected_runtime_python_sources(payload)  # noqa: SLF001
    assert expected == [
        "agent/tools",
        "agent/workspace_init.py",
        "kali_executor/executor_daemon.py",
        "runtime_shared",
    ]


def test_runtime_image_manifest_uses_exact_file_allowlist() -> None:
    payload = build_runtime_image._load_manifest(  # noqa: SLF001
        build_runtime_image.DEFAULT_MANIFEST_PATH
    )

    assert build_runtime_image._expected_runtime_python_sources(payload) == (  # noqa: SLF001
        EXPECTED_RUNTIME_PYTHON_SOURCES
    )
    assert build_runtime_image._directory_runtime_roots(payload) == []  # noqa: SLF001


def test_runtime_dockerfile_copies_only_allowlisted_python_files() -> None:
    dockerfile = build_runtime_image.DEFAULT_DOCKERFILE_PATH

    assert build_runtime_image._runtime_python_copy_sources(dockerfile) == (  # noqa: SLF001
        EXPECTED_RUNTIME_PYTHON_SOURCES
    )
    assert build_runtime_image._directory_runtime_python_copy_sources(dockerfile) == []  # noqa: SLF001


def test_directory_runtime_python_copy_sources_are_rejected(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "COPY runtime_shared/ /opt/drowai/runtime/python/runtime_shared/\n",
        encoding="utf-8",
    )

    assert build_runtime_image._directory_runtime_python_copy_sources(dockerfile) == [  # noqa: SLF001
        "runtime_shared"
    ]


def test_check_mode_rejects_directory_python_roots_and_copies(tmp_path: Path) -> None:
    payload = build_runtime_image._load_manifest(  # noqa: SLF001
        build_runtime_image.DEFAULT_MANIFEST_PATH
    )
    payload["runtime_image"]["python_roots"] = ["runtime_shared"]
    manifest = tmp_path / "manifest.md"
    _write_manifest(manifest, payload)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "\n".join(
            [
                "FROM base",
                "COPY runtime_shared/ /opt/drowai/runtime/python/runtime_shared/",
                "COPY agent/workspace_init.py /opt/drowai/runtime/python/workspace_init.py",
                "COPY kali_executor/executor_daemon.py /opt/drowai/runtime/python/kali_executor/executor_daemon.py",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(build_runtime_image.REPO_ROOT / "scripts/build_runtime_image.py"),
            "--check",
            "--manifest",
            str(manifest),
            "--dockerfile",
            str(dockerfile),
        ],
        cwd=build_runtime_image.REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "python_roots must list explicit files" in result.stdout
    assert "COPY sources must be explicit files" in result.stdout


def test_dockerignore_excludes_sensitive_and_generated_build_context() -> None:
    dockerignore = build_runtime_image.REPO_ROOT / ".dockerignore"

    patterns = {
        line.strip()
        for line in dockerignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {
        ".git/",
        ".env",
        ".env.*",
        ".encryption_key",
        "**/.env",
        "**/.encryption_key",
        ".drowai-local/",
        ".drowai-runner/",
        ".drowai-runner-cloud/",
        ".venv*/",
        "node_modules/",
        "**/node_modules/",
        "**/__pycache__/",
        "**/tests/",
        "**/log/",
        "*.db",
        "**/*.db",
        "*.sqlite",
        "*.log",
        "**/*.log.*",
        "docs/",
        "/artifacts/",
        "/output/",
        "/workspace/",
    } <= patterns
