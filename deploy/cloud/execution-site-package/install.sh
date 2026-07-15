#!/usr/bin/env bash
# DrowAI Runner Site helper - validates Docker, builds the runner image, and starts the runner.
set -euo pipefail

PACKAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${PACKAGE_ROOT}/compose.yml"
RUNNER_DATA_DIR="/var/lib/drowai"
SKIP_BUILD=0

log() { printf '[install-runner] %s\n' "$*"; }
fail() { log "ERROR: $*"; exit 1; }

usage() {
  cat <<EOF
Usage: $(basename "$0") [--skip-build]

  --skip-build   Skip docker image build when drowai/runner:local already exists
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-build) SKIP_BUILD=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "Unknown argument: $1" ;;
  esac
done

detect_runtime_image() {
  case "$(uname -m)" in
    aarch64|arm64) echo "drowai/kali-pentesting:arm64-runtime" ;;
    *) echo "drowai/kali-pentesting:amd64-runtime" ;;
  esac
}

preflight() {
  [[ "$(uname -s)" == "Linux" ]] || fail "Runner install requires Linux."
  command -v docker >/dev/null 2>&1 || fail "docker is not installed."
  command -v python3 >/dev/null 2>&1 || fail "python3 is not installed."
  docker compose version >/dev/null 2>&1 || fail "docker compose plugin is not available."
  [[ -S /var/run/docker.sock ]] || fail "/var/run/docker.sock not found."
  docker info >/dev/null 2>&1 || fail "docker daemon is not reachable."
  [[ -f "${COMPOSE_FILE}" ]] || fail "Missing ${COMPOSE_FILE}"
}

ensure_runner_data_dir() {
  if [[ ! -d "${RUNNER_DATA_DIR}" ]]; then
    log "Creating ${RUNNER_DATA_DIR} (requires sudo)..."
    sudo mkdir -p "${RUNNER_DATA_DIR}"
    sudo chown "$(id -u):$(id -g)" "${RUNNER_DATA_DIR}"
  fi
  mkdir -p "${RUNNER_DATA_DIR}/tasks" "${RUNNER_DATA_DIR}/credentials"
}

build_runner_image() {
  if [[ "${SKIP_BUILD}" -eq 1 ]] && docker image inspect drowai/runner:local >/dev/null 2>&1; then
    log "Skipping image build (--skip-build, drowai/runner:local exists)."
    return 0
  fi
  log "Building runner image (may take several minutes)..."
  python3 "${PACKAGE_ROOT}/scripts/build_runner_image.py" --network "${DROWAI_DOCKER_BUILD_NETWORK:-host}"
}

pull_runtime_image() {
  local image="${DROWAI_RUNTIME_IMAGE:-$(detect_runtime_image)}"
  if docker image inspect "${image}" >/dev/null 2>&1; then
    log "Runtime image already present: ${image}"
    return 0
  fi
  log "Pulling runtime image (required for health check): ${image}"
  docker pull "${image}"
}

compose_up() {
  log "Starting runner..."
  docker compose --project-directory "${PACKAGE_ROOT}" -f "${COMPOSE_FILE}" up -d --build
}

show_runner_health() {
  docker compose --project-directory "${PACKAGE_ROOT}" -f "${COMPOSE_FILE}" exec -T runner \
    python -m drowai_runner health 2>&1 || true
}

wait_for_runner_health() {
  local attempt health_output
  for attempt in $(seq 1 30); do
    health_output="$(docker compose --project-directory "${PACKAGE_ROOT}" -f "${COMPOSE_FILE}" exec -T runner \
      python -m drowai_runner health 2>&1 || true)"
    if printf '%s' "${health_output}" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
      log "Runner is healthy."
      return 0
    fi
    if [[ "${attempt}" -eq 1 ]] || [[ $((attempt % 6)) -eq 0 ]]; then
      log "Health check pending (${attempt}/30): ${health_output}"
    else
      log "Waiting for runner (${attempt}/30)..."
    fi
    sleep 5
  done
  log "Last health report:"
  show_runner_health
  fail "Runner failed health check. Run: docker compose --project-directory . -f compose.yml logs runner"
}

main() {
  cd "${PACKAGE_ROOT}"
  log "Package root: ${PACKAGE_ROOT}"
  preflight
  ensure_runner_data_dir
  if [[ ! -s "${PACKAGE_ROOT}/config/enrollment.toml" ]]; then
    log "No packaged config/enrollment.toml found. The runner-config compose service will prompt for enrollment details if needed."
  fi
  build_runner_image
  pull_runtime_image
  compose_up
  wait_for_runner_health
  log "Done. Runner is running on this machine."
}

main "$@"
