"""Verify that Python wheels retain package-relative runtime assets."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
from zipfile import ZipFile


_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_wheel_contains_all_prompt_and_builtin_skill_assets(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    shutil.copytree(
        _REPO_ROOT,
        source_root,
        ignore=shutil.ignore_patterns(
            ".git",
            ".pytest_cache",
            ".venv",
            "__pycache__",
            "*.pyc",
            "artifacts",
            "build",
            "dist",
            "log",
            "logs",
            "node_modules",
            "tests",
            "workspaces",
        ),
    )
    fixture_package = source_root / "core/skills/builtin/package-fixture"
    fixture_package.mkdir(parents=True)
    (fixture_package / "SKILL.md").write_text(
        """---
name: package-fixture
description: Verify zero-registration package data inclusion.
metadata:
  version: "1"
  activation: "mandatory"
  agent-ids: "pathfinder"
---

# Package fixture

Use bounded fixture guidance.
""",
        encoding="utf-8",
    )
    asset_roots = (
        source_root / "core/prompts/versions",
        source_root / "core/skills/builtin",
    )
    expected = {
        path.relative_to(source_root).as_posix()
        for root in asset_roots
        for path in root.rglob("*")
        if path.is_file() and not path.name.startswith(".")
    }
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(wheel_dir),
            str(source_root),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    wheels = list(wheel_dir.glob("drowai-*.whl"))
    assert len(wheels) == 1
    with ZipFile(wheels[0]) as wheel:
        retired_runbook_entries = {
            name for name in wheel.namelist() if name.startswith("core/runbooks/")
        }
        packaged = {
            name
            for name in wheel.namelist()
            if name.startswith("core/prompts/versions/")
            or name.startswith("core/skills/builtin/")
        }

    assert retired_runbook_entries == set()
    assert "core/skills/builtin/package-fixture/SKILL.md" in packaged
    assert packaged == expected
