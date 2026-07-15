"""Build a self-contained Runner Site distribution tarball for customer install.

Stages the flat customer package layout (install.sh, compose.yml, runner sources,
Docker assets) and emits ``dist/drowai-runner-site-<version>.tar.gz``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_TEMPLATE_DIR = REPO_ROOT / "deploy/cloud/execution-site-package"
DEFAULT_MANIFEST_PATH = (
    REPO_ROOT / "runtime/manifests/runtime-package-manifest.md"
)
PACKAGE_RUNNER_SCRIPT = REPO_ROOT / "scripts/package_runner.py"
BUILD_RUNNER_IMAGE_SCRIPT = REPO_ROOT / "scripts/build_runner_image.py"
RUNNER_IMAGE_DIR = REPO_ROOT / "deploy/images"
DIST_DIR = REPO_ROOT / "dist"

RUNNER_ROOTS = ("drowai_runner", "runtime_shared")
IMAGE_FILES = (
    "Dockerfile.runner",
    "runner-requirements.txt",
    "runner-entrypoint.sh",
)
TEMPLATE_FILES = ("install.sh", "compose.yml")
PACKAGE_DIR_NAME = "drowai-runner-site"
_EMBEDDED_RUNNER_MANIFEST = """# Runtime Package Manifest

```json
{
  "runner_package": {
    "python_roots": [
      "drowai_runner",
      "runtime_shared"
    ],
    "python_module_prefixes": [
      "drowai_runner",
      "runtime_shared"
    ]
  },
  "management_only": {
    "python_module_prefixes": [
      "backend.routers",
      "backend.database",
      "backend.models",
      "backend.auth",
      "backend.services.knowledge",
      "backend.services.artifact",
      "backend.services.terminal",
      "backend.services.llm_provider",
      "backend.services.runtime_provider",
      "backend.services.unified_docker_service",
      "client",
      "server"
    ]
  }
}
```
"""


def _resolve_version() -> str:
    try:
        proc = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            value = (proc.stdout or "").strip()
            if value:
                return value.replace("/", "-")
    except OSError:
        pass
    return "unknown"


def _missing_paths(root: Path) -> list[str]:
    missing: list[str] = []
    for name in TEMPLATE_FILES:
        if not (PACKAGE_TEMPLATE_DIR / name).is_file():
            missing.append(f"deploy/cloud/execution-site-package/{name}")
    if not BUILD_RUNNER_IMAGE_SCRIPT.is_file():
        missing.append("scripts/build_runner_image.py")
    if not PACKAGE_RUNNER_SCRIPT.is_file():
        missing.append("scripts/package_runner.py")
    for runner_root in RUNNER_ROOTS:
        if not (REPO_ROOT / runner_root).exists():
            missing.append(runner_root)
    for image_file in IMAGE_FILES:
        if not (RUNNER_IMAGE_DIR / image_file).is_file():
            missing.append(f"deploy/images/{image_file}")
    return missing


def _manifest_source_path() -> Path:
    return DEFAULT_MANIFEST_PATH


def _write_manifest_snapshot(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source = _manifest_source_path()
    if source.is_file():
        shutil.copy2(source, target)
        return
    target.write_text(_EMBEDDED_RUNNER_MANIFEST, encoding="utf-8")


def _run_runner_boundary_check(*, package_root: Path) -> int:
    env = os.environ.copy()
    env["DROWAI_PACKAGE_ROOT"] = str(package_root)
    proc = subprocess.run(
        [sys.executable, str(PACKAGE_RUNNER_SCRIPT), "--check"],
        cwd=str(package_root),
        env=env,
        check=False,
    )
    return int(proc.returncode)


def _copy_tree(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _stage_package(*, staging_root: Path, version: str, enrollment_toml: Path | None = None) -> Path:
    package_dir = staging_root / PACKAGE_DIR_NAME
    package_dir.mkdir(parents=True, exist_ok=True)

    for name in TEMPLATE_FILES:
        shutil.copy2(PACKAGE_TEMPLATE_DIR / name, package_dir / name)
        if name.endswith(".sh"):
            (package_dir / name).chmod(0o755)

    (package_dir / "VERSION").write_text(f"{version}\n", encoding="utf-8")

    scripts_dir = package_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(BUILD_RUNNER_IMAGE_SCRIPT, scripts_dir / "build_runner_image.py")
    shutil.copy2(PACKAGE_RUNNER_SCRIPT, scripts_dir / "package_runner.py")

    manifest_dst = package_dir / DEFAULT_MANIFEST_PATH.relative_to(REPO_ROOT)
    _write_manifest_snapshot(manifest_dst)

    for runner_root in RUNNER_ROOTS:
        _copy_tree(REPO_ROOT / runner_root, package_dir / runner_root)

    for image_file in IMAGE_FILES:
        _copy_tree(RUNNER_IMAGE_DIR / image_file, package_dir / "deploy" / "images" / image_file)

    if enrollment_toml is not None:
        config_dir = package_dir / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(enrollment_toml, config_dir / "enrollment.toml")
        (config_dir / "enrollment.toml").chmod(0o600)

    return package_dir


def _write_tarball(*, package_dir: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output_path, "w:gz") as archive:
        archive.add(package_dir, arcname=package_dir.name)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate inputs and staged layout without writing the tarball.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Output tarball path (default: dist/drowai-runner-site-<version>.tar.gz).",
    )
    parser.add_argument(
        "--enrollment-toml",
        default="",
        help="Optional preconfigured enrollment.toml to embed under config/.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    version = _resolve_version()
    output_path = (
        Path(args.output).resolve()
        if args.output
        else DIST_DIR / f"drowai-runner-site-{version}.tar.gz"
    )
    enrollment_toml = Path(args.enrollment_toml).resolve() if args.enrollment_toml else None

    missing = _missing_paths(REPO_ROOT)
    if enrollment_toml is not None and not enrollment_toml.is_file():
        missing.append(str(enrollment_toml))
    if missing:
        print("[package-runner-site] FAIL: missing required paths:")
        for item in missing:
            print(f"  - {item}")
        return 1

    with tempfile.TemporaryDirectory(prefix="drowai-runner-site-") as temp_dir:
        package_dir = _stage_package(
            staging_root=Path(temp_dir),
            version=version,
            enrollment_toml=enrollment_toml,
        )
        boundary_status = _run_runner_boundary_check(package_root=package_dir)
        if boundary_status != 0:
            print("[package-runner-site] FAIL: runner package boundary check failed.")
            return boundary_status

        if args.check:
            print("[package-runner-site] PASS: package layout and boundary checks succeeded.")
            return 0

        _write_tarball(package_dir=package_dir, output_path=output_path)
        print(f"[package-runner-site] PASS: wrote {output_path}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
