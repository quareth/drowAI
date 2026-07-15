"""Deployment compose boundary tests for product packaging profiles."""

from __future__ import annotations

import re
from pathlib import Path

from deploy.env_contract import required_env_keys

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEPLOY_COMPOSE_DIR = _REPO_ROOT / "deploy/compose"
_DEPLOY_CLOUD_DIR = _REPO_ROOT / "deploy/cloud"
_DEPLOY_README = _REPO_ROOT / "deploy/README.md"
_STANDALONE_INSTALL_SCRIPT = _REPO_ROOT / "deploy/scripts/install-standalone.sh"
_FRONTEND_NGINX_CONFIG = _REPO_ROOT / "deploy/images/nginx.frontend.conf"
_FORBIDDEN_MARKERS = (
    "dind",
    "DIND_WORKSPACE_BASE",
    "DOCKER_HOST=tcp://dind",
)
_FORBIDDEN_RUNNER_STORAGE = (
    ".drowai-data",
    "/opt/drowai/data",
)
_RUNNER_MOUNT = "/var/lib/drowai:/var/lib/drowai"
_DOCKER_SOCKET_MOUNT = "/var/run/docker.sock:/var/run/docker.sock"
_POSTGRES_BINDING = (
    '"${POSTGRES_BIND_ADDRESS:-127.0.0.1}:${POSTGRES_PORT:-5432}:5432"'
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


def _compose_files() -> list[Path]:
    return sorted(_DEPLOY_COMPOSE_DIR.glob("*.yml"))


def _nginx_location_block(content: str, location: str) -> str:
    marker = f"location {location} {{"
    start = content.index(marker)
    end = content.index("\n    }", start)
    return content[start:end]


def _assert_management_network_boundary(content: str, *, has_runner: bool) -> None:
    config_init = _service_block(content, "config-init")
    postgres = _service_block(content, "postgres")
    backend = _service_block(content, "backend")
    frontend = _service_block(content, "frontend")

    assert _POSTGRES_BINDING in postgres
    assert "BACKEND_PORT" not in backend
    assert "\n    ports:" not in backend

    assert "- drowai-data" in config_init
    assert "- drowai-platform" not in config_init
    assert "- drowai-data" in postgres
    assert "- drowai-platform" not in postgres
    assert "- drowai-platform" in backend
    assert "- drowai-data" in backend
    assert "- drowai-platform" in frontend
    assert "- drowai-data" not in frontend

    assert "  drowai-platform:" in content
    assert "  drowai-data:" in content

    if has_runner:
        runner = _service_block(content, "runner")
        assert "- drowai-platform" in runner
        assert "- drowai-data" not in runner


def test_deploy_compose_profiles_exist() -> None:
    names = {path.name for path in _compose_files()}
    assert "standalone.yml" in names
    assert "execution-site.yml" not in names
    assert "single-host.yml" not in names
    assert (_DEPLOY_CLOUD_DIR / "control-plane.yml").is_file()
    assert (_DEPLOY_CLOUD_DIR / "execution-site-package/compose.yml").is_file()


def test_deploy_compose_profiles_do_not_use_dind_topology() -> None:
    violations: list[str] = []
    for compose_path in _compose_files():
        if compose_path.name.startswith("_"):
            continue
        content = compose_path.read_text(encoding="utf-8")
        lowered = content.lower()
        for marker in _FORBIDDEN_MARKERS:
            if marker.lower() in lowered:
                violations.append(f"{compose_path.name}: found `{marker}`")
    assert not violations, "Product compose must not use DinD topology:\n" + "\n".join(
        violations
    )


def test_standalone_compose_declares_runner_managed_stack() -> None:
    content = (_DEPLOY_COMPOSE_DIR / "standalone.yml").read_text(encoding="utf-8")
    backend = _service_block(content, "backend")
    runner = _service_block(content, "runner")

    assert "config-init:" in content
    assert "entrypoint:" in content
    assert "backend.config_bootstrap" in content
    assert "POSTGRES_PASSWORD_FILE: /var/lib/drowai/secrets/postgres_password" in content
    assert "drowai_config:/var/lib/drowai/config" in content
    assert "drowai_secrets:/var/lib/drowai/secrets" in content
    assert "TASK_RUNTIME_PLACEMENT_MODE_DEFAULT: runner" in content
    assert "ENABLE_CLOUD_RUNNER_CONTROL" in content
    assert 'INSTALL_DOCKER_CLI: "false"' in content
    assert "DROWAI_SKIP_STARTUP_INIT_DB" not in content
    assert "DATABASE_URL:" not in content
    assert "DROWAI_RUNTIME_IMAGE:" not in content
    assert "ENCRYPTION_KEY:" not in content
    assert "DROWAI_ENV_FILE: /app/.env" not in content
    assert "OPENAI_API_KEY" not in content
    assert "VITE_ENABLE_MULTI_TASK_STREAM_MANAGER" not in content
    assert "./.env:/app/.env" not in content
    assert "profiles:" not in content
    assert "DROWAI_RUNNER_CONFIG: /var/lib/drowai/config/enrollment.toml" in content
    assert "python -m drowai_runner --config /var/lib/drowai/config/enrollment.toml health" in content
    assert "DROWAI_RUNNER_REGISTRATION_TOKEN:" not in content
    assert "DROWAI_RUNNER_TENANT_ID:" not in content
    assert "DROWAI_RUNNER_REGISTRATION_TOKEN: ${DROWAI_RUNNER_REGISTRATION_TOKEN:?" not in content
    assert _RUNNER_MOUNT in content
    assert _DOCKER_SOCKET_MOUNT not in backend
    assert _DOCKER_SOCKET_MOUNT in runner
    assert "DROWAI_RUNNER_ROOT: /var/lib/drowai" in content
    assert "DROWAI_RUNNER_HOST_BIND_ROOT: /var/lib/drowai" in content
    assert "./deploy/postgres/init:/docker-entrypoint-initdb.d:ro" in content
    assert '"${FRONTEND_PORT:-80}:80"' in content
    assert 'test: ["CMD", "wget", "-q", "-O", "/dev/null", "http://localhost:80"]' in content
    assert "target: production" in content.lower()
    assert "deploy/compose/_shared.yml" not in content
    _assert_management_network_boundary(content, has_runner=True)
    for marker in _FORBIDDEN_RUNNER_STORAGE:
        assert marker not in content, f"standalone.yml must not reference `{marker}`"


def test_cloud_control_plane_compose_is_distributed_control_stack() -> None:
    content = (_DEPLOY_CLOUD_DIR / "control-plane.yml").read_text(encoding="utf-8")
    lowered = content.lower()
    backend = _service_block(content, "backend")

    assert "drowai_deployment_profile: distributed" in lowered
    assert "runner:" not in lowered
    assert "config-init:" in content
    assert "entrypoint:" in content
    assert "backend.config_bootstrap" in content
    assert "POSTGRES_PASSWORD_FILE: /var/lib/drowai/secrets/postgres_password" in content
    assert "deploy/compose/_shared.yml" not in content
    assert "postgres:" in lowered
    assert "backend:" in lowered
    assert "frontend:" in lowered
    assert "DATABASE_URL:" not in content
    assert "./deploy/postgres/init:/docker-entrypoint-initdb.d:ro" in content
    assert "DROWAI_RUNTIME_IMAGE:" not in content
    assert "ENCRYPTION_KEY:" not in content
    assert "Set ENCRYPTION_KEY" not in content
    assert "OPENAI_API_KEY" not in content
    assert "DROWAI_RUNNER_REGISTRATION_TOKEN:" not in content
    assert "DROWAI_RUNNER_TENANT_ID:" not in content
    assert "VITE_ENABLE_MULTI_TASK_STREAM_MANAGER" not in content
    assert '"${FRONTEND_PORT:-80}:80"' in content
    assert 'test: ["CMD", "wget", "-q", "-O", "/dev/null", "http://localhost:80"]' in content
    assert _DOCKER_SOCKET_MOUNT not in content
    assert _DOCKER_SOCKET_MOUNT not in backend
    _assert_management_network_boundary(content, has_runner=False)
    for marker in _FORBIDDEN_RUNNER_STORAGE:
        assert marker not in content, f"control-plane.yml must not reference `{marker}`"


def test_legacy_deployment_entrypoints_are_removed() -> None:
    assert not (_DEPLOY_COMPOSE_DIR / "execution-site.yml").exists()
    assert not (_DEPLOY_COMPOSE_DIR / "single-host.yml").exists()
    assert not (_DEPLOY_COMPOSE_DIR / "management-plane.yml").exists()
    assert not (_DEPLOY_COMPOSE_DIR / "_shared.yml").exists()
    assert not (_REPO_ROOT / "deploy/scripts/install-execution-site.sh").exists()
    assert not (_REPO_ROOT / "deploy/scripts/install-single-host.sh").exists()


def test_standalone_required_env_excludes_wizard_generated_runner_token() -> None:
    assert "DROWAI_RUNNER_REGISTRATION_TOKEN" not in required_env_keys("standalone")
    assert "DROWAI_RUNNER_REGISTRATION_TOKEN" not in required_env_keys("execution-site")
    assert "DROWAI_RUNTIME_IMAGE" not in required_env_keys("standalone")
    assert "DROWAI_RUNTIME_IMAGE" not in required_env_keys("execution-site")


def test_legacy_standalone_installer_uses_generated_enrollment_artifact() -> None:
    content = _STANDALONE_INSTALL_SCRIPT.read_text(encoding="utf-8")

    assert "BACKEND_PORT" not in content
    assert '"$(frontend_url)/api/health"' in content
    assert '"$(frontend_url)/api/setup/status"' in content
    assert "DROWAI_RUNNER_REGISTRATION_TOKEN" not in content
    assert "DROWAI_RUNNER_TENANT_ID" not in content
    assert "wait_for_runner_enrollment_artifact" in content
    assert "/var/lib/drowai/config/enrollment.toml" in content
    assert "exec -T backend" in content
    assert "--force-recreate runner" in content


def test_deployment_readme_names_canonical_standalone_compose_path() -> None:
    content = _DEPLOY_README.read_text(encoding="utf-8")

    assert "The canonical standalone entrypoint is the Compose profile" in content
    assert "-f deploy/compose/standalone.yml" in content


def test_execution_site_compose_uses_runner_config_service() -> None:
    content = (_DEPLOY_CLOUD_DIR / "execution-site-package/compose.yml").read_text(encoding="utf-8")
    runner_enrollment = _service_block(content, "runner-enrollment")
    runner_config = _service_block(content, "runner-config")
    runner = _service_block(content, "runner")

    assert "runner-config:" in content
    assert "configure" in content
    assert "--config" in content
    assert "/var/lib/drowai/config/enrollment.toml" in content
    assert "Stored runner credentials found; keeping existing active runner config." in content
    assert "mv \"$$tmp\" /var/lib/drowai/config/enrollment.toml" in content
    assert "\"$tmp\"" not in runner_enrollment
    assert "\"$credential\"" not in runner_enrollment
    assert "[ ! -s /var/lib/drowai/config/enrollment.toml ]" not in content
    assert "DROWAI_RUNNER_REGISTRATION_TOKEN:" not in content
    assert "DROWAI_RUNNER_CONTROL_PLANE_URL:" not in content
    assert "DROWAI_RUNNER_TENANT_ID:" not in content
    assert _DOCKER_SOCKET_MOUNT not in runner_enrollment
    assert _DOCKER_SOCKET_MOUNT not in runner_config
    assert _DOCKER_SOCKET_MOUNT in runner


def test_frontend_nginx_proxies_api_websocket_upgrades_for_runner_channel() -> None:
    content = _FRONTEND_NGINX_CONFIG.read_text(encoding="utf-8")
    api_block = _nginx_location_block(content, "/api/")
    ws_block = _nginx_location_block(content, "/ws")

    assert "map $http_upgrade $connection_upgrade" in content
    assert "proxy_http_version 1.1;" in api_block
    assert "proxy_set_header Upgrade $http_upgrade;" in api_block
    assert "proxy_set_header Connection $connection_upgrade;" in api_block
    assert "proxy_set_header Connection $connection_upgrade;" in ws_block


def test_frontend_nginx_sets_compression_and_cache_policy() -> None:
    content = _FRONTEND_NGINX_CONFIG.read_text(encoding="utf-8")
    hashed_asset_pattern = r"^/assets/.+-[A-Za-z0-9_-]{8}\.[^/]+$"
    assets_block = _nginx_location_block(
        content, f'~ "{hashed_asset_pattern}"'
    )
    index_block = _nginx_location_block(content, "= /index.html")
    spa_block = _nginx_location_block(content, "/")

    assert "gzip on;" in content
    assert "gzip_vary on;" in content
    assert "gzip_min_length 1024;" in content
    for media_type in (
        "text/plain",
        "text/css",
        "application/javascript",
        "application/json",
        "application/xml",
        "image/svg+xml",
    ):
        assert media_type in content

    assert re.fullmatch(hashed_asset_pattern, "/assets/index-Bl2WViQO.js")
    assert not re.fullmatch(hashed_asset_pattern, "/assets/not-hashed.js")
    assert not re.fullmatch(hashed_asset_pattern, "/assets/index-1234567.js")
    assert not re.fullmatch(hashed_asset_pattern, "/assets/index-123456789.js")
    assert 'add_header Cache-Control "public, max-age=31536000, immutable";' in assets_block
    assert "immutable\" always;" not in assets_block
    assert "try_files $uri =404;" in assets_block
    assert 'add_header Cache-Control "no-cache" always;' in index_block
    assert "try_files $uri $uri/ /index.html;" in spa_block
