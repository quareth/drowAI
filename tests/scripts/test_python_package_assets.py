"""Verify that Python wheels retain package-relative runtime assets."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile


_REPO_ROOT = Path(__file__).resolve().parents[2]
_ASSET_ROOTS = (
    _REPO_ROOT / "core/prompts/versions",
    _REPO_ROOT / "core/runbooks/builtin",
)


def test_wheel_contains_all_prompt_and_builtin_runbook_assets(tmp_path: Path) -> None:
    expected = {
        path.relative_to(_REPO_ROOT).as_posix()
        for root in _ASSET_ROOTS
        for path in root.rglob("*")
        if path.is_file() and not path.name.startswith(".")
    }

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(tmp_path),
            str(_REPO_ROOT),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    wheels = list(tmp_path.glob("drowai-*.whl"))
    assert len(wheels) == 1
    with ZipFile(wheels[0]) as wheel:
        packaged = {
            name
            for name in wheel.namelist()
            if name.startswith("core/prompts/versions/")
            or name.startswith("core/runbooks/builtin/")
        }

    assert packaged == expected
