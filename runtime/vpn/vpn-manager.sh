#!/usr/bin/env bash
set -euo pipefail

# Runtime-owned VPN manager for packaged execution-plane images.

LOG_DIR="${VPN_STATE_DIR:-/vpn}"
LOG_FILE="$LOG_DIR/connection.log"
STATE_FILE="$LOG_DIR/state"
LOCK_FILE="$LOG_DIR/manager.lock"
ATTEMPT_DIR="$LOG_DIR/attempts"
OVPN_CONFIG="${VPN_CONFIG:-/vpn/task.ovpn}"
CRED_FILE="${VPN_CREDENTIALS_FILE:-/workspace/vpn/credentials.txt}"
TUN_DEVICE="${VPN_TUN_DEVICE:-/dev/net/tun}"
TUN_INTERFACE="${VPN_TUN_INTERFACE:-tun0}"
OPENVPN_BIN="${VPN_OPENVPN_BIN:-openvpn}"
CLASSIFIER_PYTHONPATH="${VPN_CLASSIFIER_PYTHONPATH:-${DROWAI_RUNTIME_PYTHON_ROOT:-/opt/drowai/runtime/python}}"
ATTEMPT_DEADLINE_SECONDS="${VPN_ATTEMPT_DEADLINE_SECONDS:-90}"
WATCH_POLL_SECONDS="${VPN_WATCH_POLL_SECONDS:-1}"
STOP_TIMEOUT_SECONDS="${VPN_STOP_TIMEOUT_SECONDS:-5}"
MAX_LOG_BYTES="${VPN_MAX_LOG_BYTES:-1048576}"
ATTEMPT_STARTED_RC=10

[[ "$ATTEMPT_DEADLINE_SECONDS" =~ ^[0-9]+$ ]] || ATTEMPT_DEADLINE_SECONDS=90
[[ "$WATCH_POLL_SECONDS" =~ ^[0-9]+([.][0-9]+)?$ ]] || WATCH_POLL_SECONDS=1
[[ "$WATCH_POLL_SECONDS" != "0" && "$WATCH_POLL_SECONDS" != "0.0" ]] || WATCH_POLL_SECONDS=1
[[ "$STOP_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] || STOP_TIMEOUT_SECONDS=5
[[ "$MAX_LOG_BYTES" =~ ^[0-9]+$ ]] || MAX_LOG_BYTES=1048576

mkdir -p "$LOG_DIR" "$ATTEMPT_DIR"
chmod 700 "$LOG_DIR" "$ATTEMPT_DIR" || true
touch "$LOG_FILE" "$LOCK_FILE"
chmod 600 "$LOG_FILE" "$LOCK_FILE" || true

timestamp() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

log() { echo "[$(timestamp)] $*" | tee -a "$LOG_FILE"; }

get_tun_ip() {
  ip -4 addr show dev "$TUN_INTERFACE" 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 || true
}

attempt_pid_path() {
  local attempt_id="$1"
  [[ "$attempt_id" =~ ^[A-Za-z0-9._-]+$ ]] || return 1
  printf '%s/%s.pid\n' "$ATTEMPT_DIR" "$attempt_id"
}

write_state() {
  local status="$1"
  local error_category="${2:-}"
  local error_message="${3:-}"
  local attempt_id="${4:-}"
  local pid="${5:-}"
  local process_start="${6:-}"
  local started_at="${7:-}"
  local tmp_file="${STATE_FILE}.tmp.$$.$RANDOM"
  {
    printf 'status=%s\n' "$status"
    printf 'error_category=%s\n' "$error_category"
    printf 'error=%s\n' "$error_message"
    printf 'attempt_id=%s\n' "$attempt_id"
    printf 'pid=%s\n' "$pid"
    printf 'process_start=%s\n' "$process_start"
    printf 'started_at=%s\n' "$started_at"
    printf 'updated_at=%s\n' "$(date +%s)"
  } >"$tmp_file"
  chmod 600 "$tmp_file" || true
  mv -f "$tmp_file" "$STATE_FILE"
}

read_state_value() {
  local key="$1"
  [[ -f "$STATE_FILE" ]] || return 0
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$STATE_FILE"
}

json_escape() {
  local value="$1"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\n'/ }
  value=${value//$'\r'/ }
  printf '%s' "$value"
}

emit_status() {
  local status="$1"
  local ip_address="${2:-}"
  local error_message="${3:-}"
  printf '{"status":"%s","ip_address":"%s","error_message":"%s"}\n' \
    "$(json_escape "$status")" "$(json_escape "$ip_address")" "$(json_escape "$error_message")"
}

rotate_log_if_needed() {
  local size=0
  if [[ -f "$LOG_FILE" ]]; then
    size=$(wc -c <"$LOG_FILE" 2>/dev/null || echo 0)
  fi
  if (( size >= MAX_LOG_BYTES )); then
    mv -f "$LOG_FILE" "${LOG_FILE}.1"
    touch "$LOG_FILE"
    chmod 600 "$LOG_FILE" || true
  fi
}

process_start_identity() {
  local pid="$1"
  if [[ -r "/proc/$pid/stat" ]]; then
    awk '{print $22}' "/proc/$pid/stat" 2>/dev/null || true
  else
    ps -o lstart= -p "$pid" 2>/dev/null | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' || true
  fi
}

is_owned_process() {
  local pid="$1"
  local expected_start="$2"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  [[ -n "$expected_start" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  [[ "$(process_start_identity "$pid")" == "$expected_start" ]]
}

stop_owned_process() {
  local pid="$1"
  local expected_start="$2"
  if ! is_owned_process "$pid" "$expected_start"; then
    return 0
  fi
  kill -TERM "$pid" 2>/dev/null || return 1
  local iterations=$(( STOP_TIMEOUT_SECONDS * 10 ))
  local iteration
  for ((iteration = 0; iteration < iterations; iteration++)); do
    is_owned_process "$pid" "$expected_start" || return 0
    sleep 0.1
  done
  is_owned_process "$pid" "$expected_start" || return 0
  kill -KILL "$pid" 2>/dev/null || return 1
  for ((iteration = 0; iteration < 10; iteration++)); do
    is_owned_process "$pid" "$expected_start" || return 0
    sleep 0.1
  done
  ! is_owned_process "$pid" "$expected_start"
}

classify_failure() {
  local fallback_category="${1:-process_exit}"
  local result=()
  if mapfile -t result < <(
    PYTHONPATH="$CLASSIFIER_PYTHONPATH${PYTHONPATH:+:$PYTHONPATH}" \
      python3 -m runtime_shared.vpn_observability classify \
      --log-file "$LOG_FILE" --fallback "$fallback_category" 2>/dev/null
  ) && [[ ${#result[@]} -ge 2 ]]; then
    printf '%s\n%s\n' "${result[0]}" "${result[1]}"
    return 0
  fi
  printf 'process_exit\nOpenVPN process exited before the tunnel was ready\n'
}

persist_classified_failure() {
  local fallback_category="$1"
  local attempt_id="${2:-}"
  local pid="${3:-}"
  local process_start="${4:-}"
  local started_at="${5:-}"
  local classification=()
  mapfile -t classification < <(classify_failure "$fallback_category")
  local category="${classification[0]:-process_exit}"
  local message="${classification[1]:-OpenVPN process exited before the tunnel was ready}"
  write_state "failed" "$category" "$message" "$attempt_id" "$pid" "$process_start" "$started_at"
  printf '%s\n' "$message"
}

ensure_tun_device() {
  if [[ ! -e "$TUN_DEVICE" ]]; then
    log "Creating VPN tunnel device node"
    mkdir -p "$(dirname "$TUN_DEVICE")" || true
    if mknod "$TUN_DEVICE" c 10 200 2>/dev/null; then
      chmod 666 "$TUN_DEVICE" || true
    else
      log "Tunnel device creation failed; the runtime must provide NET_ADMIN and /dev/net/tun"
    fi
  fi
}

snapshot_legacy_processes() {
  local pid start
  while IFS= read -r pid; do
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    start=$(process_start_identity "$pid")
    [[ -n "$start" ]] && printf '%s\t%s\n' "$pid" "$start"
  done < <(pgrep -x openvpn 2>/dev/null || true)
}

stop_legacy_processes() {
  local snapshot=()
  mapfile -t snapshot < <(snapshot_legacy_processes)
  local record pid start
  for record in "${snapshot[@]}"; do
    pid=${record%%$'\t'*}
    start=${record#*$'\t'}
    if ! stop_owned_process "$pid" "$start"; then
      return 1
    fi
    log "Stopped legacy OpenVPN process (pid=$pid)"
  done
}

warn_route_overlap() {
  local bridge_cidr overlap
  bridge_cidr=$(ip -o -4 route show dev eth0 scope link 2>/dev/null | awk 'NR == 1 {print $1}')
  [[ -n "$bridge_cidr" ]] || return 0
  if overlap=$(
    ip -o -4 route show dev "$TUN_INTERFACE" 2>/dev/null \
      | PYTHONPATH="$CLASSIFIER_PYTHONPATH${PYTHONPATH:+:$PYTHONPATH}" \
        python3 -m runtime_shared.vpn_observability route-overlap --bridge-cidr "$bridge_cidr" 2>/dev/null
  ); then
    log "WARNING: task bridge subnet $bridge_cidr overlaps VPN route $overlap; set DROWAI_RUNTIME_NETWORK_POOL to a non-overlapping pool"
  fi
}

start_vpn_attempt() {
  local requested_status="$1"
  local attempt_id="$(date +%s)-$$-$RANDOM"
  local pid_file
  pid_file=$(attempt_pid_path "$attempt_id")
  local started_at
  started_at=$(date +%s)

  rotate_log_if_needed
  write_state "$requested_status" "" "" "$attempt_id" "" "" "$started_at"
  log "Starting OpenVPN attempt $attempt_id"

  local openvpn_args=(--verb 3 --config "$OVPN_CONFIG")
  if grep -q '^[[:space:]]*auth-user-pass' "$OVPN_CONFIG" 2>/dev/null && [[ -f "$CRED_FILE" ]]; then
    openvpn_args+=(--auth-user-pass "$CRED_FILE")
    log "Using task-local VPN credentials file"
  fi
  openvpn_args+=(--daemon --writepid "$pid_file" --log-append "$LOG_FILE")

  local command_rc=0
  if "$OPENVPN_BIN" "${openvpn_args[@]}" 9>&-; then
    command_rc=0
  else
    command_rc=$?
    local message
    message=$(persist_classified_failure "process_start" "$attempt_id" "" "" "$started_at")
    emit_status "failed" "" "$message"
    return "$command_rc"
  fi

  local pid=""
  local process_start=""
  local iteration
  for ((iteration = 0; iteration < 20; iteration++)); do
    pid=$(cat "$pid_file" 2>/dev/null || true)
    if [[ "$pid" =~ ^[0-9]+$ ]]; then
      process_start=$(process_start_identity "$pid")
      [[ -n "$process_start" ]] && kill -0 "$pid" 2>/dev/null && break
    fi
    sleep 0.1
  done
  if [[ ! "$pid" =~ ^[0-9]+$ ]] || [[ -z "$process_start" ]] || ! kill -0 "$pid" 2>/dev/null; then
    local message
    message=$(persist_classified_failure "process_start" "$attempt_id" "$pid" "$process_start" "$started_at")
    rm -f "$pid_file"
    emit_status "failed" "" "$message"
    return 1
  fi

  write_state "$requested_status" "" "" "$attempt_id" "$pid" "$process_start" "$started_at"
  emit_status "$requested_status"
  return "$ATTEMPT_STARTED_RC"
}

connect_vpn() {
  local requested_status="${1:-connecting}"
  ensure_tun_device
  local current_ip
  current_ip=$(get_tun_ip)
  if [[ -n "$current_ip" ]]; then
    emit_status "connected" "$current_ip"
    return 0
  fi

  local attempt_id pid process_start started_at persisted_status
  attempt_id=$(read_state_value attempt_id)
  pid=$(read_state_value pid)
  process_start=$(read_state_value process_start)
  started_at=$(read_state_value started_at)
  persisted_status=$(read_state_value status)
  if is_owned_process "$pid" "$process_start"; then
    if [[ "$requested_status" != "reconnecting" ]]; then
      emit_status "${persisted_status:-connecting}"
      return 0
    fi
    log "Stopping unhealthy OpenVPN attempt $attempt_id (pid=$pid)"
    if ! stop_owned_process "$pid" "$process_start"; then
      local message
      message=$(persist_classified_failure "process_stop" "$attempt_id" "$pid" "$process_start" "$started_at")
      emit_status "failed" "" "$message"
      return 1
    fi
    local old_pid_file
    old_pid_file=$(attempt_pid_path "$attempt_id" 2>/dev/null || true)
    [[ -n "$old_pid_file" ]] && rm -f "$old_pid_file"
  elif [[ "$requested_status" != "reconnecting" ]] && pgrep -x openvpn >/dev/null 2>&1; then
    emit_status "connecting"
    return 0
  fi

  if [[ "$requested_status" == "reconnecting" ]] && ! stop_legacy_processes; then
    local message
    message=$(persist_classified_failure "process_stop" "$attempt_id" "$pid" "$process_start" "$started_at")
    emit_status "failed" "" "$message"
    return 1
  fi
  if [[ ! -c "$TUN_DEVICE" ]]; then
    write_state "failed" "device" "VPN tunnel device setup failed"
    emit_status "failed" "" "VPN tunnel device setup failed"
    return 1
  fi
  if [[ ! -f "$OVPN_CONFIG" ]]; then
    write_state "failed" "config" "VPN configuration is unavailable"
    emit_status "failed" "" "VPN configuration is unavailable"
    return 1
  fi
  start_vpn_attempt "$requested_status"
}

watch_attempt_step() {
  local watched_attempt="$1"
  local current_attempt
  current_attempt=$(read_state_value attempt_id)
  [[ "$current_attempt" == "$watched_attempt" ]] || return 0

  local status pid process_start started_at ip_address
  status=$(read_state_value status)
  [[ "$status" == "connecting" || "$status" == "reconnecting" ]] || return 0
  pid=$(read_state_value pid)
  process_start=$(read_state_value process_start)
  started_at=$(read_state_value started_at)
  ip_address=$(get_tun_ip)

  if [[ -n "$ip_address" ]]; then
    write_state "connected" "" "" "$watched_attempt" "$pid" "$process_start" "$started_at"
    log "VPN attempt $watched_attempt connected with tunnel address $ip_address"
    warn_route_overlap
    return 0
  fi

  if ! is_owned_process "$pid" "$process_start"; then
    local message pid_file
    message=$(persist_classified_failure "process_exit" "$watched_attempt" "$pid" "$process_start" "$started_at")
    pid_file=$(attempt_pid_path "$watched_attempt" 2>/dev/null || true)
    [[ -n "$pid_file" ]] && rm -f "$pid_file"
    log "$message"
    return 0
  fi

  local now
  now=$(date +%s)
  [[ "$started_at" =~ ^[0-9]+$ ]] || started_at="$now"
  if (( now - started_at >= ATTEMPT_DEADLINE_SECONDS )); then
    log "VPN attempt $watched_attempt exceeded ${ATTEMPT_DEADLINE_SECONDS}s without a usable tunnel"
    if ! stop_owned_process "$pid" "$process_start"; then
      local message
      message=$(persist_classified_failure "process_stop" "$watched_attempt" "$pid" "$process_start" "$started_at")
      log "$message"
      return 1
    fi
    local pid_file
    pid_file=$(attempt_pid_path "$watched_attempt" 2>/dev/null || true)
    [[ -n "$pid_file" ]] && rm -f "$pid_file"
    write_state "failed" "deadline" "VPN connection deadline exceeded" "$watched_attempt" "$pid" "$process_start" "$started_at"
    return 0
  fi
  return 75
}

with_manager_lock() (
  exec 9>"$LOCK_FILE"
  flock -x 9
  "$@"
)

watch_attempt() {
  local attempt_id="$1"
  while true; do
    local step_rc=0
    if with_manager_lock watch_attempt_step "$attempt_id"; then
      step_rc=0
    else
      step_rc=$?
    fi
    if (( step_rc == 0 )); then
      return 0
    fi
    if (( step_rc != 75 )); then
      return "$step_rc"
    fi
    sleep "$WATCH_POLL_SECONDS"
  done
}

current_watchdog_target() {
  local status attempt_id
  status=$(read_state_value status)
  attempt_id=$(read_state_value attempt_id)
  if [[ "$status" == "connecting" || "$status" == "reconnecting" ]] && [[ -n "$attempt_id" ]]; then
    printf '%s\n' "$attempt_id"
  fi
}

launch_current_watchdog() {
  local attempt_id
  attempt_id=$(with_manager_lock current_watchdog_target)
  [[ -n "$attempt_id" ]] || return 0
  nohup bash "$0" _watch "$attempt_id" </dev/null >/dev/null 2>&1 &
}

run_connect_action() {
  local requested_status="$1"
  local command_rc=0
  if with_manager_lock connect_vpn "$requested_status"; then
    command_rc=0
  else
    command_rc=$?
  fi
  if (( command_rc == ATTEMPT_STARTED_RC )); then
    launch_current_watchdog
    command_rc=0
  fi
  return "$command_rc"
}

disconnect_vpn() {
  local attempt_id pid process_start started_at
  attempt_id=$(read_state_value attempt_id)
  pid=$(read_state_value pid)
  process_start=$(read_state_value process_start)
  started_at=$(read_state_value started_at)
  if is_owned_process "$pid" "$process_start"; then
    if ! stop_owned_process "$pid" "$process_start"; then
      local message
      message=$(persist_classified_failure "process_stop" "$attempt_id" "$pid" "$process_start" "$started_at")
      emit_status "failed" "" "$message"
      return 1
    fi
  fi
  if ! stop_legacy_processes; then
    local message
    message=$(persist_classified_failure "process_stop" "$attempt_id" "$pid" "$process_start" "$started_at")
    emit_status "failed" "" "$message"
    return 1
  fi
  local pid_file
  pid_file=$(attempt_pid_path "$attempt_id" 2>/dev/null || true)
  [[ -n "$pid_file" ]] && rm -f "$pid_file"
  write_state "disconnected"
  log "OpenVPN stopped"
  emit_status "disconnected"
}

check_vpn_status() {
  local ip_address status error_message attempt_id pid process_start started_at
  ip_address=$(get_tun_ip)
  status=$(read_state_value status)
  error_message=$(read_state_value error)
  attempt_id=$(read_state_value attempt_id)
  pid=$(read_state_value pid)
  process_start=$(read_state_value process_start)
  started_at=$(read_state_value started_at)

  if [[ -n "$ip_address" ]]; then
    write_state "connected" "" "" "$attempt_id" "$pid" "$process_start" "$started_at"
    emit_status "connected" "$ip_address"
    return 0
  fi
  if [[ "$status" == "connecting" || "$status" == "reconnecting" || "$status" == "connected" ]]; then
    if is_owned_process "$pid" "$process_start"; then
      if [[ "$status" == "connected" ]]; then
        write_state "failed" "device" "VPN tunnel device setup failed" "$attempt_id" "$pid" "$process_start" "$started_at"
        log "VPN tunnel address disappeared while OpenVPN remained active"
        emit_status "failed" "" "VPN tunnel device setup failed"
        return 0
      fi
      emit_status "$status" "" "$error_message"
      return 0
    fi
    local message pid_file
    message=$(persist_classified_failure "process_exit" "$attempt_id" "$pid" "$process_start" "$started_at")
    pid_file=$(attempt_pid_path "$attempt_id" 2>/dev/null || true)
    [[ -n "$pid_file" ]] && rm -f "$pid_file"
    emit_status "failed" "" "$message"
    return 0
  fi
  if [[ "$status" == "failed" ]]; then
    emit_status "failed" "" "$error_message"
  elif pgrep -x openvpn >/dev/null 2>&1; then
    emit_status "connecting"
  else
    emit_status "disconnected"
  fi
}

if ! command -v flock >/dev/null 2>&1; then
  emit_status "failed" "" "VPN manager locking is unavailable"
  exit 127
fi

case "${1:-}" in
  connect)
    run_connect_action connecting
    ;;
  reconnect)
    run_connect_action reconnecting
    ;;
  disconnect)
    with_manager_lock disconnect_vpn
    ;;
  status)
    with_manager_lock check_vpn_status
    ;;
  _watch)
    watch_attempt "${2:?missing attempt id}"
    ;;
  *)
    echo "Usage: $0 {connect|reconnect|disconnect|status}"
    exit 2
    ;;
esac
