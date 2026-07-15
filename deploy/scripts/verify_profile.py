"""Validate DrowAI deployment compose profiles and env contract alignment.

Performs static checks on product deployment artifacts under ``deploy/`` without
starting containers. Optional smoke hooks can be added in future iterations.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deploy.env_contract import DEFAULT_RUNNER_DATA_DIR, required_env_keys  # noqa: E402

DEPLOY_ROOT = REPO_ROOT / "deploy"
COMPOSE_DIR = DEPLOY_ROOT / "compose"
CLOUD_DIR = DEPLOY_ROOT / "cloud"
EXECUTION_SITE_PACKAGE_DIR = CLOUD_DIR / "execution-site-package"
ENV_DIR = DEPLOY_ROOT / "env"
PACKAGE_RUNNER_SCRIPT = REPO_ROOT / "scripts/package_runner.py"
BUILD_RUNNER_IMAGE_SCRIPT = REPO_ROOT / "scripts/build_runner_image.py"
PACKAGE_EXECUTION_SITE_SCRIPT = REPO_ROOT / "scripts/package_execution_site.py"

_PROFILES: dict[str, dict[str, Path]] = {
    "cloud": {
        "control_plane": CLOUD_DIR / "control-plane.yml",
        "compose": EXECUTION_SITE_PACKAGE_DIR / "compose.yml",
        "install": EXECUTION_SITE_PACKAGE_DIR / "install.sh",
    },
    "standalone": {
        "compose": COMPOSE_DIR / "standalone.yml",
        "env_example": ENV_DIR / "standalone.env.example",
    },
}

_FORBIDDEN_COMPOSE_MARKERS = (
    "dind",
    "DIND_WORKSPACE_BASE",
    "DOCKER_HOST=tcp://dind",
)

_FORBIDDEN_RUNNER_STORAGE_MARKERS = (
    ".drowai-data",
    "/opt/drowai/data",
)

_REQUIRED_RUNNER_MOUNT = "/var/lib/drowai:/var/lib/drowai"
_DOCKER_SOCKET_MOUNT = "/var/run/docker.sock:/var/run/docker.sock"
_RAW_RUNNER_ENV_MARKERS = (
    "DROWAI_RUNNER_REGISTRATION_TOKEN",
    "DROWAI_RUNNER_TENANT_ID",
)
_MANAGEMENT_RUNTIME_ENV_MARKERS = (
    "TASK_RUNTIME_PLACEMENT_MODE_DEFAULT: runner",
    'ENABLE_CLOUD_RUNNER_CONTROL: "true"',
    'RUNNER_TOOL_COMMAND_ENABLED: "true"',
)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _service_block(content: str, service: str) -> str:
    marker = f"  {service}:\n"
    start = content.find(marker)
    if start == -1:
        return ""
    next_service = content.find("\n  ", start + len(marker))
    while next_service != -1:
        following = content[next_service + 1 :]
        if following.startswith("  ") and not following.startswith("    "):
            return content[start:next_service]
        next_service = content.find("\n  ", next_service + 1)
    return content[start:]


def _services_with_marker(content: str, marker: str) -> list[str]:
    services_block = (
        content.split("services:", 1)[1] if "services:" in content else content
    )
    names: list[str] = []
    for line in services_block.splitlines():
        if (
            line.startswith("  ")
            and not line.startswith("    ")
            and line.rstrip().endswith(":")
        ):
            names.append(line.strip().rstrip(":"))
    return [
        name
        for name in names
        if marker in _service_block(content, name)
    ]


def _validate_no_raw_runner_env(
    profile: str,
    content: str,
    compose_name: str,
) -> list[str]:
    errors: list[str] = []
    for marker in _RAW_RUNNER_ENV_MARKERS:
        if marker in content:
            errors.append(
                f"{profile}: product compose must not require raw runner env "
                f"`{marker}` in {compose_name}"
            )
    return errors


def _validate_management_runtime_env(
    profile: str,
    content: str,
    compose_name: str,
    service: str = "backend",
) -> list[str]:
    errors: list[str] = []
    service_content = _service_block(content, service)
    if not service_content:
        errors.append(f"{profile}: compose file missing `{service}` service")
        return errors
    for marker in _MANAGEMENT_RUNTIME_ENV_MARKERS:
        if marker not in service_content:
            errors.append(
                f"{profile}: `{service}` must set runner-only Management env "
                f"`{marker}` in {compose_name}"
            )
    if _DOCKER_SOCKET_MOUNT in service_content:
        errors.append(
            f"{profile}: `{service}` must not mount the Docker socket in {compose_name}"
        )
    return errors


def _validate_docker_socket_ownership(
    profile: str,
    content: str,
    compose_name: str,
    *,
    expected_services: set[str],
) -> list[str]:
    owners = set(_services_with_marker(content, _DOCKER_SOCKET_MOUNT))
    if owners != expected_services:
        expected = ", ".join(sorted(expected_services)) or "none"
        actual = ", ".join(sorted(owners)) or "none"
        return [
            f"{profile}: Docker socket owners in {compose_name} must be {expected}; found {actual}"
        ]
    return []


def _validate_profile_files(profile: str) -> list[str]:
    errors: list[str] = []
    paths = _PROFILES[profile]
    for label, path in paths.items():
        if not path.exists():
            errors.append(f"{profile}: missing {label} file: {path}")
    if profile == "standalone":
        env_example = paths.get("env_example")
        if env_example is not None and not env_example.exists():
            errors.append(f"{profile}: missing env_example file: {env_example}")
    return errors


def _validate_env_example(profile: str, env_path: Path) -> list[str]:
    errors: list[str] = []
    if not env_path.exists():
        return errors

    content = _read_text(env_path)
    for key in sorted(required_env_keys(profile)):
        if f"{key}=" not in content:
            errors.append(f"{profile}: env example missing key `{key}` in {env_path.name}")
    return errors


def _validate_compose_content(profile: str, compose_path: Path) -> list[str]:
    errors: list[str] = []
    content = _read_text(compose_path)
    lowered = content.lower()
    errors.extend(_validate_no_raw_runner_env(profile, content, compose_path.name))
    for marker in _FORBIDDEN_COMPOSE_MARKERS:
        if marker.lower() in lowered:
            errors.append(
                f"{profile}: forbidden compose marker `{marker}` in {compose_path.name}"
            )

    for marker in _FORBIDDEN_RUNNER_STORAGE_MARKERS:
        if marker in content:
            errors.append(
                f"{profile}: forbidden runner storage marker `{marker}` in {compose_path.name}"
            )

    if profile == "standalone":
        if "services:" not in lowered:
            errors.append(f"{profile}: compose file missing services block")
        for service in ("postgres", "backend", "frontend", "runner"):
            if f"{service}:" not in lowered:
                errors.append(f"{profile}: compose file missing `{service}` service")
        errors.extend(
            _validate_management_runtime_env(profile, content, compose_path.name)
        )
        errors.extend(
            _validate_docker_socket_ownership(
                profile,
                content,
                compose_path.name,
                expected_services={"runner"},
            )
        )
        if 'install_docker_cli: "false"' not in content.replace("'", '"').lower():
            errors.append(f"{profile}: backend build must set INSTALL_DOCKER_CLI=false")
        if "target: production" not in lowered:
            errors.append(f"{profile}: frontend build must use production target")
        if "docker-entrypoint-initdb.d" not in content:
            errors.append(f"{profile}: postgres must mount init scripts for pgvector")
        if "./deploy/postgres/init:/docker-entrypoint-initdb.d:ro" not in content:
            errors.append(f"{profile}: postgres init mount must be repo-root relative")
        if _REQUIRED_RUNNER_MOUNT not in content:
            errors.append(f"{profile}: runner must mount `{_REQUIRED_RUNNER_MOUNT}`")
        if "drowai_runner_root: /var/lib/drowai" not in lowered:
            errors.append(f"{profile}: runner must set DROWAI_RUNNER_ROOT=/var/lib/drowai")
        if "drowai_runner_host_bind_root: /var/lib/drowai" not in lowered:
            errors.append(f"{profile}: runner must set DROWAI_RUNNER_HOST_BIND_ROOT=/var/lib/drowai")

    if profile == "cloud":
        if "include:" in lowered:
            errors.append(f"{profile}: customer package compose must not use include")
        if "runner:" not in lowered:
            errors.append(f"{profile}: compose file missing runner service")
        if "runner-config:" not in lowered:
            errors.append(f"{profile}: compose file missing runner-config service")
        if "drowai_runner_config: /var/lib/drowai/config/enrollment.toml" not in lowered:
            errors.append(f"{profile}: runner must read /var/lib/drowai/config/enrollment.toml")
        if "drowai_runner_tenant_id" in lowered:
            errors.append(f"{profile}: product runner package must not require tenant id")
        if "deploy/images/dockerfile.runner" not in lowered:
            errors.append(f"{profile}: compose must reference deploy/images/Dockerfile.runner")
        errors.extend(
            _validate_docker_socket_ownership(
                profile,
                content,
                compose_path.name,
                expected_services={"runner"},
            )
        )

    return errors


def _validate_cloud_control_plane(compose_path: Path) -> list[str]:
    errors: list[str] = []
    if not compose_path.exists():
        return errors
    content = _read_text(compose_path)
    lowered = content.lower()
    if "drowai_deployment_profile: distributed" not in lowered:
        errors.append("cloud: control plane must set DROWAI_DEPLOYMENT_PROFILE=distributed")
    if "runner:" in lowered:
        errors.append("cloud: control plane compose must not define a runner service")
    errors.extend(_validate_no_raw_runner_env("cloud", content, compose_path.name))
    errors.extend(_validate_management_runtime_env("cloud", content, compose_path.name))
    errors.extend(
        _validate_docker_socket_ownership(
            "cloud",
            content,
            compose_path.name,
            expected_services=set(),
        )
    )
    if "./deploy/postgres/init:/docker-entrypoint-initdb.d:ro" not in content:
        errors.append("cloud: control plane postgres init mount must be repo-root relative")
    return errors


def _validate_env_contract_defaults() -> list[str]:
    errors: list[str] = []
    if DEFAULT_RUNNER_DATA_DIR != "/var/lib/drowai":
        errors.append(
            f"env_contract DEFAULT_RUNNER_DATA_DIR must be /var/lib/drowai (got {DEFAULT_RUNNER_DATA_DIR})"
        )
    return errors


def _run_script(script_path: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(script_path), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return int(proc.returncode), output.strip()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "profile",
        nargs="?",
        choices=tuple(_PROFILES.keys()),
        help="Optional profile name to validate. Defaults to all profiles.",
    )
    parser.add_argument(
        "--skip-package-check",
        action="store_true",
        help="Skip runner package boundary verification.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    profiles = [args.profile] if args.profile else list(_PROFILES.keys())
    errors: list[str] = []

    errors.extend(_validate_env_contract_defaults())

    for profile in profiles:
        errors.extend(_validate_profile_files(profile))
        compose_path = _PROFILES[profile]["compose"]
        if compose_path.exists():
            errors.extend(_validate_compose_content(profile, compose_path))
        if profile == "standalone":
            env_example_path = _PROFILES[profile].get("env_example")
            if env_example_path is not None and env_example_path.exists():
                errors.extend(_validate_env_example(profile, env_example_path))
        if profile == "cloud":
            control_plane_path = _PROFILES[profile].get("control_plane")
            if control_plane_path is not None:
                errors.extend(_validate_cloud_control_plane(control_plane_path))

    if not args.skip_package_check:
        package_status, package_output = _run_script(PACKAGE_RUNNER_SCRIPT, "--check")
        if package_status != 0:
            errors.append("runner package boundary check failed")
            if package_output:
                errors.append(package_output)
        execution_site_status, execution_site_output = _run_script(
            PACKAGE_EXECUTION_SITE_SCRIPT,
            "--check",
        )
        if execution_site_status != 0:
            errors.append("execution site package check failed")
            if execution_site_output:
                errors.append(execution_site_output)
        runner_image_status, runner_image_output = _run_script(
            BUILD_RUNNER_IMAGE_SCRIPT,
            "--check",
        )
        if runner_image_status != 0:
            errors.append("runner image packaging check failed")
            if runner_image_output:
                errors.append(runner_image_output)

    if errors:
        print("[verify-profile] FAIL:")
        for item in errors:
            print(f"  - {item}")
        return 1

    print(f"[verify-profile] PASS: validated profile(s): {', '.join(profiles)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
