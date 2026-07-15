#!/usr/bin/env bash
# Install DrowAI Standalone (postgres + backend + frontend + runner).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/deploy/compose/standalone.yml"
ENV_FILE="${REPO_ROOT}/.env"
RUNNER_DATA_DIR="/var/lib/drowai"
RUNNER_ENROLLMENT_PATH="/var/lib/drowai/config/enrollment.toml"

log() { printf '[install-standalone] %s\n' "$*"; }
fail() { log "ERROR: $*"; exit 1; }

frontend_url() {
  local port="${FRONTEND_PORT:-80}"
  if [[ "${port}" == "80" ]]; then
    printf 'http://localhost'
  else
    printf 'http://localhost:%s' "${port}"
  fi
}

preflight() {
  [[ "$(uname -s)" == "Linux" ]] || fail "Standalone install requires Linux."
  command -v docker >/dev/null 2>&1 || fail "docker is not installed."
  command -v python3 >/dev/null 2>&1 || fail "python3 is not installed."
  docker compose version >/dev/null 2>&1 || fail "docker compose plugin is not available."
  [[ -S /var/run/docker.sock ]] || fail "/var/run/docker.sock not found."
  docker info >/dev/null 2>&1 || fail "docker daemon is not reachable."
}

detect_runtime_image() {
  case "$(uname -m)" in
    aarch64|arm64) echo "drowai/kali-pentesting:arm64-runtime" ;;
    *) echo "drowai/kali-pentesting:amd64-runtime" ;;
  esac
}

generate_encryption_key() {
  python3 - <<'PY'
import base64
import os
print(base64.urlsafe_b64encode(os.urandom(32)).decode(), end="")
PY
}

set_env_value() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" "${ENV_FILE}"; then
    sed -i.bak "s|^${key}=.*|${key}=${value}|" "${ENV_FILE}"
    rm -f "${ENV_FILE}.bak"
  else
    printf '\n%s=%s\n' "${key}" "${value}" >>"${ENV_FILE}"
  fi
  export "${key}=${value}"
}

ensure_runner_data_dir() {
  if [[ ! -d "${RUNNER_DATA_DIR}" ]]; then
    log "Creating ${RUNNER_DATA_DIR} (requires sudo)..."
    sudo mkdir -p "${RUNNER_DATA_DIR}"
    sudo chown "$(id -u):$(id -g)" "${RUNNER_DATA_DIR}"
  fi
  mkdir -p "${RUNNER_DATA_DIR}/tasks" "${RUNNER_DATA_DIR}/credentials"
}

ensure_env_file() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    log "Creating minimal ${ENV_FILE} for compose-first bootstrap..."
    cat >"${ENV_FILE}" <<EOF
# Minimal standalone bootstrap — complete remaining config at /setup
POSTGRES_USER=drowai_user
POSTGRES_PASSWORD=
POSTGRES_DB=drowai
JWT_SECRET=
ENCRYPTION_KEY=
DROWAI_DEPLOYMENT_PROFILE=single_host
EOF
  fi
  # shellcheck disable=SC1090
  set -a
  source "${ENV_FILE}"
  set +a
  if [[ -z "${POSTGRES_PASSWORD:-}" ]]; then
    fail "Set POSTGRES_PASSWORD in ${ENV_FILE}, then re-run."
  fi
  if [[ -z "${JWT_SECRET:-}" ]]; then
    if command -v openssl >/dev/null 2>&1; then
      JWT_SECRET="$(openssl rand -base64 48 | tr -d '\n')"
    else
      JWT_SECRET="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48), end="")
PY
)"
    fi
    log "Generated JWT_SECRET for backend boot (wizard may rewrite .env on completion)."
    set_env_value "JWT_SECRET" "${JWT_SECRET}"
  fi
  if [[ -z "${ENCRYPTION_KEY:-}" ]]; then
    ENCRYPTION_KEY="$(generate_encryption_key)"
    log "Generated ENCRYPTION_KEY for provider credential storage. Keep this value stable across rebuilds."
    set_env_value "ENCRYPTION_KEY" "${ENCRYPTION_KEY}"
  fi
}

build_runner_image() {
  log "Building runner image for post-setup startup..."
  python3 "${REPO_ROOT}/scripts/build_runner_image.py"
}

compose_up_control_plane() {
  log "Starting standalone control plane..."
  docker compose --project-directory "${REPO_ROOT}" -f "${COMPOSE_FILE}" up -d --build postgres backend frontend
}

wait_for_backend_health() {
  local attempt max_attempts=60
  for attempt in $(seq 1 "${max_attempts}"); do
    if curl -sf "$(frontend_url)/api/health" >/dev/null 2>&1; then
      log "Backend health check passed."
      return 0
    fi
    log "Waiting for backend health (${attempt}/${max_attempts})..."
    sleep 5
  done
  fail "Backend failed health check. Inspect: docker compose --project-directory . -f deploy/compose/standalone.yml logs backend"
}

wait_for_setup_wizard_hint() {
  local attempt max_attempts=12
  local status_url="$(frontend_url)/api/setup/status"
  for attempt in $(seq 1 "${max_attempts}"); do
    local payload
    payload="$(curl -sf "${status_url}" 2>/dev/null || true)"
    if [[ -n "${payload}" ]] && printf '%s' "${payload}" | grep -q '"setup_required"[[:space:]]*:[[:space:]]*true'; then
      log "First-run setup required. Open $(frontend_url)/setup to finish configuration."
      return 0
    fi
    if [[ -n "${payload}" ]] && printf '%s' "${payload}" | grep -q '"installation_complete"[[:space:]]*:[[:space:]]*true'; then
      log "Installation already complete."
      return 0
    fi
    sleep 2
  done
  log "Could not read setup status; open $(frontend_url)/setup if this is a fresh install."
}

setup_installation_complete() {
  local status_url="$(frontend_url)/api/setup/status"
  local payload
  payload="$(curl -sf "${status_url}" 2>/dev/null || true)"
  [[ -n "${payload}" ]] && printf '%s' "${payload}" | grep -q '"installation_complete"[[:space:]]*:[[:space:]]*true'
}

wait_for_runner_enrollment_artifact() {
  local attempt
  local interval_seconds="${RUNNER_ENROLLMENT_WAIT_INTERVAL_SECONDS:-5}"
  local max_attempts="${RUNNER_ENROLLMENT_WAIT_ATTEMPTS:-360}"
  for attempt in $(seq 1 "${max_attempts}"); do
    if docker compose --project-directory "${REPO_ROOT}" -f "${COMPOSE_FILE}" exec -T backend \
      test -s "${RUNNER_ENROLLMENT_PATH}" >/dev/null 2>&1; then
      log "Generated runner enrollment detected at ${RUNNER_ENROLLMENT_PATH}."
      return 0
    fi
    if [[ "${attempt}" == "1" ]]; then
      log "Waiting for setup wizard to publish generated runner enrollment..."
      log "Open $(frontend_url)/setup if setup has not been completed."
    else
      if setup_installation_complete; then
        log "Setup is complete; waiting for generated runner enrollment (${attempt}/${max_attempts})..."
      else
        log "Waiting for generated runner enrollment (${attempt}/${max_attempts})..."
      fi
    fi
    sleep "${interval_seconds}"
  done
  log "Timed out waiting for generated runner enrollment at ${RUNNER_ENROLLMENT_PATH}."
  return 1
}

start_runner() {
  log "Starting standalone runner with generated enrollment artifact..."
  docker compose --project-directory "${REPO_ROOT}" -f "${COMPOSE_FILE}" up -d --build --force-recreate runner
}

wait_for_runner_health() {
  local attempt max_attempts=30
  for attempt in $(seq 1 "${max_attempts}"); do
    if docker compose --project-directory "${REPO_ROOT}" -f "${COMPOSE_FILE}" exec -T runner \
      python -m drowai_runner health >/dev/null 2>&1; then
      log "Runner health check passed."
      return 0
    fi
    log "Waiting for runner health (${attempt}/${max_attempts})..."
    sleep 5
  done
  fail "Runner failed health check. Inspect: docker compose --project-directory . -f deploy/compose/standalone.yml logs runner"
}

main() {
  cd "${REPO_ROOT}"
  preflight
  ensure_runner_data_dir
  ensure_env_file
  build_runner_image
  compose_up_control_plane
  wait_for_backend_health
  wait_for_setup_wizard_hint
  if wait_for_runner_enrollment_artifact; then
    start_runner
    wait_for_runner_health
  else
    log "Runner was not started. After setup publishes ${RUNNER_ENROLLMENT_PATH}, run:"
    log "docker compose --project-directory . -f deploy/compose/standalone.yml up -d --build --force-recreate runner"
  fi
  log "Standalone install complete. UI: $(frontend_url)"
}

main "$@"
