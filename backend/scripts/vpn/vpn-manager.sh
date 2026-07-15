#!/usr/bin/env bash
set -euo pipefail

# Simple VPN manager to run inside container via bind-mounted scripts

LOG_DIR="/vpn"
LOG_FILE="$LOG_DIR/connection.log"
PID_FILE="$LOG_DIR/openvpn.pid"
OVPN_CONFIG="${VPN_CONFIG:-/vpn/task.ovpn}"
# Optional credentials file (two lines: username, password)
CRED_FILE="/workspace/vpn/credentials.txt"
# How long to wait for tun0 to appear
WAIT_SECONDS="${VPN_WAIT_SECONDS:-20}"

mkdir -p "$LOG_DIR"

timestamp() { date +"%Y-%m-%dT%H:%M:%S%z"; }

log() { echo "[$(timestamp)] $*" | tee -a "$LOG_FILE"; }

ensure_tun_device() {
  if [[ ! -e /dev/net/tun ]]; then
    log "Creating /dev/net/tun device node"
    mkdir -p /dev/net || true
    # Create char device 10:200 if possible
    if mknod /dev/net/tun c 10 200 2>/dev/null; then
      chmod 666 /dev/net/tun || true
    else
      log "mknod failed (insufficient privileges). Device may be provided via Docker --device or --privileged."
    fi
  fi
}

connect_vpn() {
  ensure_tun_device
  if pgrep -x openvpn >/dev/null 2>&1; then
    log "OpenVPN already running"
    exit 0
  fi
  if [[ ! -f "$OVPN_CONFIG" ]]; then
    log "OVPN config not found: $OVPN_CONFIG"
    exit 1
  fi
  # If config expects auth-user-pass and credentials file exists, use it
  EXTRA_ARGS=""
  if grep -q '^auth-user-pass' "$OVPN_CONFIG" 2>/dev/null && [[ -f "$CRED_FILE" ]]; then
    EXTRA_ARGS="--auth-user-pass $CRED_FILE"
    log "Using credentials file at $CRED_FILE"
  fi

  log "Starting OpenVPN with config: $OVPN_CONFIG"
  # Run with moderate verbosity for troubleshooting
  openvpn --verb 3 --config "$OVPN_CONFIG" $EXTRA_ARGS --daemon --writepid "$PID_FILE" --log "$LOG_FILE"

  # Wait briefly for the process to start
  sleep 2
  if ! pgrep -x openvpn >/dev/null 2>&1; then
    log "Failed to start OpenVPN process"
    exit 1
  fi

  # Wait for tun0 to get an IPv4 address
  for i in $(seq 1 "$WAIT_SECONDS"); do
    IP=$(ip -4 addr show dev tun0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)
    if [[ -n "${IP:-}" ]]; then
      log "tun0 ready with IP $IP"
      exit 0
    fi
    sleep 1
  done
  log "tun0 did not come up within ${WAIT_SECONDS}s (TLS/route/UDP may be blocked). See $LOG_FILE for details"
  exit 1
}

disconnect_vpn() {
  if [[ -f "$PID_FILE" ]]; then
    PID=$(cat "$PID_FILE" || true)
    if [[ -n "${PID:-}" ]] && kill "$PID" 2>/dev/null; then
      log "Sent SIGTERM to OpenVPN (pid=$PID)"
      sleep 2
    fi
    rm -f "$PID_FILE"
  fi
  pkill -x openvpn 2>/dev/null || true
  log "OpenVPN stopped"
}

check_vpn_status() {
  if pgrep -x openvpn >/dev/null 2>&1; then
    IP=$(ip -4 addr show dev tun0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)
    if [[ -n "${IP:-}" ]]; then
      echo "{\"status\":\"connected\",\"ip\":\"${IP}\"}"
      exit 0
    else
      echo '{"status":"connecting"}'
      exit 2
    fi
  else
    echo '{"status":"disconnected"}'
    exit 1
  fi
}

handle_vpn_failure() {
  log "Handling VPN failure"
  disconnect_vpn || true
}

case "${1:-}" in
  connect)
    connect_vpn
    ;;
  disconnect)
    disconnect_vpn
    ;;
  status)
    check_vpn_status
    ;;
  *)
    echo "Usage: $0 {connect|disconnect|status}"
    exit 2
    ;;
esac

