"""Check and build the managed runner Docker image with package boundary guardrails.

This script validates runner-package import boundaries via ``package_runner.py``
and, in build mode, invokes ``docker build`` for ``deploy/images/Dockerfile.runner``.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

def _resolve_package_root() -> Path:
    override = os.environ.get("DROWAI_PACKAGE_ROOT", "").strip()
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parents[1]


REPO_ROOT = _resolve_package_root()
DEFAULT_DOCKERFILE_PATH = REPO_ROOT / "deploy/images/Dockerfile.runner"
DEFAULT_IMAGE_TAG = "drowai/runner:local"
DEFAULT_BUILD_NETWORK = os.environ.get("DROWAI_DOCKER_BUILD_NETWORK", "").strip()
PACKAGE_RUNNER_SCRIPT = REPO_ROOT / "scripts/package_runner.py"


def _run_package_check() -> int:
    proc = subprocess.run(
        [sys.executable, str(PACKAGE_RUNNER_SCRIPT), "--check"],
        cwd=str(REPO_ROOT),
        check=False,
    )
    return int(proc.returncode)


def _run_docker_build(*, dockerfile_path: Path, image_tag: str, build_network: str | None) -> int:
    print("[build-runner-image] Running docker build (this can take several minutes)...")
    command = ["docker", "build", "-f", str(dockerfile_path), "-t", image_tag]
    if build_network:
        command.extend(["--network", build_network])
    command.append(str(REPO_ROOT))
    proc = subprocess.run(command, cwd=str(REPO_ROOT), check=False)
    return int(proc.returncode)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dockerfile",
        default=str(DEFAULT_DOCKERFILE_PATH),
        help="Runner Dockerfile path.",
    )
    parser.add_argument(
        "--image-tag",
        default=DEFAULT_IMAGE_TAG,
        help="Docker image tag for build mode.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run check-only diagnostics without building a Docker image.",
    )
    parser.add_argument(
        "--network",
        default=DEFAULT_BUILD_NETWORK,
        help="Optional Docker build network mode, such as 'host'.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    dockerfile_path = Path(args.dockerfile).resolve()

    if args.check:
        print("[build-runner-image] CHECK mode enabled (no image build will be run).")
    else:
        print("[build-runner-image] BUILD mode enabled.")
    print(f"[build-runner-image] image_tag={args.image_tag}")
    print(f"[build-runner-image] dockerfile={dockerfile_path}")
    if args.network:
        print(f"[build-runner-image] build_network={args.network}")

    if not dockerfile_path.exists():
        print(f"[build-runner-image] ERROR: dockerfile not found: {dockerfile_path}")
        return 2

    package_status = _run_package_check()
    if package_status != 0:
        print("[build-runner-image] FAIL: runner package boundary check failed.")
        return package_status

    if args.check:
        print("[build-runner-image] PASS: runner package boundary checks succeeded.")
        return 0

    status = _run_docker_build(
        dockerfile_path=dockerfile_path,
        image_tag=str(args.image_tag),
        build_network=str(args.network or "").strip() or None,
    )
    if status != 0:
        print("[build-runner-image] FAIL: docker build failed.")
        return status

    print(f"[build-runner-image] PASS: built image {args.image_tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
