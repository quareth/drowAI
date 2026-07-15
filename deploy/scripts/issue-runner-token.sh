#!/usr/bin/env bash
# Issue a one-time runner install token (rit_...) from the control plane.
set -euo pipefail

log() { printf '[issue-runner-token] %s\n' "$*"; }
fail() { log "ERROR: $*"; exit 1; }

prompt() {
  local message="$1"
  local default="${2:-}"
  local reply
  if [[ -n "$default" ]]; then
    read -r -p "${message} [${default}]: " reply
    reply="${reply:-$default}"
  else
    read -r -p "${message}: " reply
    while [[ -z "$reply" ]]; do
      read -r -p "${message}: " reply
    done
  fi
  printf '%s' "$reply"
}

main() {
  command -v curl >/dev/null 2>&1 || fail "curl is not installed."
  command -v jq >/dev/null 2>&1 || fail "jq is not installed."

  local control_plane_url username password site_name site_slug token site_id install_token

  control_plane_url="$(prompt "Control plane URL" "http://localhost")"
  control_plane_url="${control_plane_url%/}"
  username="$(prompt "Username")"
  password="$(prompt "Password")"
  site_name="$(prompt "Execution site name" "customer-site")"
  site_slug="$(prompt "Execution site slug" "${site_name// /-}")"

  log "Logging in..."
  token="$(curl -sf -X POST "${control_plane_url}/api/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"${username}\",\"password\":\"${password}\"}" \
    | jq -r .access_token)" || fail "Login failed."
  [[ -n "${token}" && "${token}" != "null" ]] || fail "Login failed — check username/password."

  log "Creating execution site..."
  site_id="$(curl -sf -X POST "${control_plane_url}/api/runner-control/execution-sites" \
    -H "Authorization: Bearer ${token}" \
    -H 'Content-Type: application/json' \
    -d "{\"name\":\"${site_name}\",\"slug\":\"${site_slug}\",\"network_label\":\"customer\"}" \
    | jq -r .id)" || fail "Failed to create execution site."
  [[ -n "${site_id}" && "${site_id}" != "null" ]] || fail "Failed to create execution site."

  log "Issuing install token..."
  install_token="$(curl -sf -X POST "${control_plane_url}/api/runner-control/install-tokens" \
    -H "Authorization: Bearer ${token}" \
    -H 'Content-Type: application/json' \
    -d "{\"execution_site_id\":\"${site_id}\",\"ttl_seconds\":3600}" \
    | jq -r .install_token)" || fail "Failed to issue install token."
  [[ -n "${install_token}" && "${install_token}" != "null" ]] || fail "Failed to issue install token."

  log ""
  log "Give this install token to the customer (shown once):"
  printf '%s\n' "${install_token}"
}

main "$@"
