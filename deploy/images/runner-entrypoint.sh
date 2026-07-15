#!/usr/bin/env bash
set -euo pipefail

mkdir -p /var/lib/drowai/tasks /var/lib/drowai/credentials /var/lib/drowai/config

CONFIG_PATH="${DROWAI_RUNNER_CONFIG:-/var/lib/drowai/config/enrollment.toml}"
FALLBACK_CONFIG_PATH="/var/lib/drowai/config/runner.toml"
CREDENTIAL_PATH="/var/lib/drowai/credentials/runner.secret"

if [ "$#" -gt 0 ]; then
  exec python -m drowai_runner "$@"
fi

if [ -n "${DROWAI_RUNNER_CONTROL_PLANE_URL:-}" ]; then
  exec python -m drowai_runner run
fi

echo "Waiting for runner config at ${CONFIG_PATH}..."
while [ ! -s "${CONFIG_PATH}" ] && [ ! -s "${FALLBACK_CONFIG_PATH}" ]; do
  sleep 5
done

if [ ! -s "${CONFIG_PATH}" ] && [ -s "${FALLBACK_CONFIG_PATH}" ]; then
  CONFIG_PATH="${FALLBACK_CONFIG_PATH}"
fi

if [ -s "${CREDENTIAL_PATH}" ] && [ -s "${CREDENTIAL_PATH}.runner_id" ] && [ -s "${CREDENTIAL_PATH}.tenant_id" ]; then
  echo "Runner stored credentials found; starting managed runner with existing registration."
fi
echo "Runner config found at ${CONFIG_PATH}; starting managed runner..."
exec python -m drowai_runner --config "${CONFIG_PATH}" run
