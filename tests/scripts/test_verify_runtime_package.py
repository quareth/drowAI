"""Tests for static runtime package verifier behaviors.

Scope:
- Validate manifest parsing from markdown JSON.
- Validate check-mode failure reporting for forbidden imports.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from scripts import verify_runtime_package


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        "# manifest\n\n```json\n" + json.dumps(payload, indent=2) + "\n```\n",
        encoding="utf-8",
    )


def test_runtime_package_manifest_loads_from_repo_contract() -> None:
    manifest = verify_runtime_package._load_manifest(  # noqa: SLF001
        verify_runtime_package.DEFAULT_MANIFEST_PATH
    )

    assert manifest.runtime_image_python_roots
    assert isinstance(manifest.runtime_image_excluded_module_prefixes, tuple)
    assert manifest.management_only_module_prefixes


def test_verify_runtime_package_check_reports_forbidden_imports(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    runtime_root = Path(tempfile.mkdtemp(prefix="verify-runtime-", dir=repo_root))
    try:
        (runtime_root / "bad.py").write_text(
            "from backend.services.knowledge import web_common\n",
            encoding="utf-8",
        )

        manifest_path = tmp_path / "manifest.md"
        _write_manifest(
            manifest_path,
            {
                "runtime_image": {
                    "python_roots": [str(runtime_root.relative_to(repo_root))]
                },
                "management_only": {
                    "python_module_prefixes": ["backend.services.knowledge"],
                },
                "temporary_exceptions": [],
            },
        )

        result = subprocess.run(
            [
                sys.executable,
                str(repo_root / "scripts/verify_runtime_package.py"),
                "--manifest",
                str(manifest_path),
            ],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )

        output = (result.stdout or "") + (result.stderr or "")
        assert result.returncode == 1
        assert "[verify-runtime-package] runtime_contract_version=" in output
        assert "[verify-runtime-package] included_runtime_roots:" in output
        assert "[verify-runtime-package] FAIL" in output
        assert "forbidden import `backend.services.knowledge`" in output
    finally:
        shutil.rmtree(runtime_root, ignore_errors=True)


def test_verify_runtime_package_honors_excluded_module_prefixes(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    runtime_root = Path(tempfile.mkdtemp(prefix="verify-runtime-excluded-", dir=repo_root))
    try:
        (runtime_root / "excluded_bad.py").write_text(
            "from backend.services.knowledge import web_common\n",
            encoding="utf-8",
        )

        manifest_path = tmp_path / "manifest.md"
        _write_manifest(
            manifest_path,
            {
                "runtime_image": {
                    "python_roots": [str(runtime_root.relative_to(repo_root))],
                    "excluded_module_prefixes": [
                        f"{runtime_root.relative_to(repo_root).as_posix().replace('/', '.')}.excluded_bad"
                    ],
                },
                "management_only": {
                    "python_module_prefixes": ["backend.services.knowledge"],
                },
                "temporary_exceptions": [],
            },
        )

        result = subprocess.run(
            [
                sys.executable,
                str(repo_root / "scripts/verify_runtime_package.py"),
                "--manifest",
                str(manifest_path),
            ],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )

        output = (result.stdout or "") + (result.stderr or "")
        assert result.returncode == 0
        assert "[verify-runtime-package] runtime_contract_version=" in output
        assert "[verify-runtime-package] included_runtime_roots:" in output
        assert "[verify-runtime-package] PASS" in output
    finally:
        shutil.rmtree(runtime_root, ignore_errors=True)
