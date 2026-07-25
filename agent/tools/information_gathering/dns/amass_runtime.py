"""Own task-scoped Amass v5 runtime state and collector execution."""

from __future__ import annotations

import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List

from runtime_shared.workspace_files import RuntimeWorkspaceDirectory, RuntimeWorkspaceFile

AMASS_NAMES_BEGIN = "__DROWAI_AMASS_V5_NAMES_BEGIN__"
AMASS_NAMES_END = "__DROWAI_AMASS_V5_NAMES_END__"
AMASS_RESOLVED_BEGIN = "__DROWAI_AMASS_V5_RESOLVED_BEGIN__"
AMASS_RESOLVED_END = "__DROWAI_AMASS_V5_RESOLVED_END__"

AMASS_STATUS_BEGIN = "__DROWAI_AMASS_V5_STATUS_BEGIN__"
AMASS_STATUS_END = "__DROWAI_AMASS_V5_STATUS_END__"
AMASS_BEFORE_NAMES_BEGIN = "__DROWAI_AMASS_V5_BEFORE_NAMES_BEGIN__"
AMASS_BEFORE_NAMES_END = "__DROWAI_AMASS_V5_BEFORE_NAMES_END__"
AMASS_BEFORE_RESOLVED_BEGIN = "__DROWAI_AMASS_V5_BEFORE_RESOLVED_BEGIN__"
AMASS_BEFORE_RESOLVED_END = "__DROWAI_AMASS_V5_BEFORE_RESOLVED_END__"

AMASS_RUNTIME_RELATIVE_DIR = ".drowai/amass"
AMASS_COLLECTOR_RELATIVE_PATH = f"{AMASS_RUNTIME_RELATIVE_DIR}/collect_v5.sh"
AMASS_CONTAINER_COLLECTOR_PATH = f"/workspace/{AMASS_COLLECTOR_RELATIVE_PATH}"
AMASS_XDG_CONFIG_RELATIVE_DIR = f"{AMASS_RUNTIME_RELATIVE_DIR}/xdg-config"
AMASS_OUTPUT_RELATIVE_DIR = f"{AMASS_XDG_CONFIG_RELATIVE_DIR}/amass"
AMASS_XDG_DATA_RELATIVE_DIR = f"{AMASS_RUNTIME_RELATIVE_DIR}/xdg-data"
AMASS_XDG_CACHE_RELATIVE_DIR = f"{AMASS_RUNTIME_RELATIVE_DIR}/xdg-cache"
AMASS_RUNS_RELATIVE_DIR = f"{AMASS_RUNTIME_RELATIVE_DIR}/runs"

AMASS_QUERY_CLEANUP_GRACE_SECONDS = 15
AMASS_FORCE_KILL_GRACE_SECONDS = 5
AMASS_LOCK_WAIT_GRACE_SECONDS = 10
AMASS_MAX_RESERVED_GRACE_SECONDS = 20
AMASS_PROVIDER_DEADLINE_MARGIN_SECONDS = 1
AMASS_TIMEOUT_EXIT_CODE = 124
AMASS_UNOWNED_ENGINE_EXIT_CODE = 70


@dataclass(frozen=True, slots=True)
class AmassCollectorTimeoutBudget:
    """Internal deadlines that leave provider timeout room for result recovery."""

    lock_wait_seconds: int
    enum_deadline_seconds: int
    query_grace_seconds: int
    force_kill_grace_seconds: int


@dataclass(frozen=True, slots=True)
class AmassCollectorExecution:
    """Raw local collector execution data returned to the public tool wrapper."""

    stdout: str
    stderr: str
    exit_code: int
    execution_time: float


def build_amass_timeout_budget(execution_timeout: Any) -> AmassCollectorTimeoutBudget:
    """Reserve bounded post-enumeration time inside the whole-tool deadline."""

    try:
        total_seconds = int(float(execution_timeout))
    except (TypeError, ValueError):
        total_seconds = 1
    total_seconds = max(1, total_seconds)
    budget_seconds = total_seconds
    if total_seconds > AMASS_PROVIDER_DEADLINE_MARGIN_SECONDS + 2:
        budget_seconds = total_seconds - AMASS_PROVIDER_DEADLINE_MARGIN_SECONDS

    lock_wait = 0
    if budget_seconds >= 4:
        lock_wait = min(AMASS_LOCK_WAIT_GRACE_SECONDS, max(1, budget_seconds // 3))

    force_kill_grace = 0
    if budget_seconds >= 8:
        force_kill_grace = min(
            AMASS_FORCE_KILL_GRACE_SECONDS,
            max(1, budget_seconds // 20),
        )

    remaining_after_lock_and_force = max(0, budget_seconds - lock_wait - force_kill_grace)
    minimum_query_grace = 2 if remaining_after_lock_and_force >= 2 else 1
    query_grace = min(
        AMASS_QUERY_CLEANUP_GRACE_SECONDS,
        max(minimum_query_grace, remaining_after_lock_and_force // 4),
        remaining_after_lock_and_force,
    )
    enum_deadline = max(0, budget_seconds - lock_wait - query_grace - force_kill_grace)
    return AmassCollectorTimeoutBudget(
        lock_wait_seconds=lock_wait,
        enum_deadline_seconds=enum_deadline,
        query_grace_seconds=query_grace,
        force_kill_grace_seconds=force_kill_grace,
    )


def build_amass_collector_command(
    args: Any,
    *,
    workspace_root: str,
    script_path: str,
) -> List[str]:
    """Return collector argv with native Amass enum options."""

    budget = build_amass_timeout_budget(getattr(args, "execution_timeout", 1))
    command = [
        "bash",
        script_path,
        workspace_root,
        args.target,
        str(budget.lock_wait_seconds),
        str(budget.enum_deadline_seconds),
        str(budget.query_grace_seconds),
        str(budget.force_kill_grace_seconds),
    ]
    enum_options: List[str] = []
    mode_value = getattr(getattr(args, "mode", ""), "value", getattr(args, "mode", ""))

    if mode_value == "active":
        enum_options.append("-active")
    elif mode_value == "brute":
        enum_options.append("-brute")

    if getattr(args, "wordlist", None):
        if "-brute" not in enum_options:
            enum_options.append("-brute")
        enum_options.extend(["-w", args.wordlist])

    enum_options.extend(["-timeout", str(args.inactivity_timeout_minutes)])

    if getattr(args, "verbose", False):
        enum_options.append("-v")
    if getattr(args, "quiet", False):
        enum_options.append("-silent")
    if getattr(args, "dns_server", None):
        enum_options.extend(["-r", args.dns_server])
    if getattr(args, "source", None):
        enum_options.extend(["-include", ",".join(args.source)])
    if getattr(args, "exclude_source", None):
        enum_options.extend(["-exclude", ",".join(args.exclude_source)])

    enum_options.append("-nocolor")
    command.extend(enum_options)
    return command


def prepare_amass_workspace_files(args: Any) -> List[RuntimeWorkspaceFile]:
    """Materialize the fixed task-scoped Amass v5 collector."""

    _ = args
    return [
        RuntimeWorkspaceFile.from_text(
            relative_path=AMASS_COLLECTOR_RELATIVE_PATH,
            content=AMASS_COLLECTOR_SCRIPT,
            description="task-scoped Amass v5 runtime collector",
        )
    ]


def prepare_amass_workspace_directories(args: Any) -> List[RuntimeWorkspaceDirectory]:
    """Create task-scoped Amass state directories before execution."""

    _ = args
    return [
        RuntimeWorkspaceDirectory(
            relative_path=AMASS_RUNTIME_RELATIVE_DIR,
            description="task-scoped Amass runtime state",
        ),
        RuntimeWorkspaceDirectory(
            relative_path=AMASS_XDG_CONFIG_RELATIVE_DIR,
            description="task-scoped Amass XDG configuration root",
        ),
        RuntimeWorkspaceDirectory(
            relative_path=AMASS_OUTPUT_RELATIVE_DIR,
            description="task-scoped Amass engine output and asset.db",
        ),
        RuntimeWorkspaceDirectory(
            relative_path=AMASS_XDG_DATA_RELATIVE_DIR,
            description="task-scoped Amass XDG data root",
        ),
        RuntimeWorkspaceDirectory(
            relative_path=AMASS_XDG_CACHE_RELATIVE_DIR,
            description="task-scoped Amass XDG cache root",
        ),
        RuntimeWorkspaceDirectory(
            relative_path=AMASS_RUNS_RELATIVE_DIR,
            description="task-scoped Amass collector run snapshots",
        ),
    ]


def execute_amass_collector_locally(args: Any) -> AmassCollectorExecution:
    """Run the same task-scoped collector contract outside container transports."""

    temporary_workspace = tempfile.TemporaryDirectory(prefix="drowai-amass-")
    workspace_root = Path(temporary_workspace.name)
    script_path = workspace_root / AMASS_COLLECTOR_RELATIVE_PATH
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(AMASS_COLLECTOR_SCRIPT, encoding="utf-8")
    command = build_amass_collector_command(
        args,
        workspace_root=str(workspace_root),
        script_path=str(script_path),
    )

    start = time.time()
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=args.execution_timeout,
        )
        return AmassCollectorExecution(
            stdout=proc.stdout,
            stderr=proc.stderr,
            exit_code=proc.returncode,
            execution_time=time.time() - start,
        )
    except subprocess.TimeoutExpired as exc:
        return AmassCollectorExecution(
            stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "Command timed out",
            exit_code=-2,
            execution_time=time.time() - start,
        )
    finally:
        temporary_workspace.cleanup()


AMASS_COLLECTOR_SCRIPT = f"""#!/usr/bin/env bash
set -u -o pipefail

if [ "$#" -lt 6 ]; then
    echo "usage: collect_v5.sh WORKSPACE_ROOT ROOT_DOMAIN LOCK_WAIT_SECONDS ENUM_DEADLINE_SECONDS QUERY_GRACE_SECONDS FORCE_KILL_GRACE_SECONDS [AMASS_ENUM_OPTIONS...]" >&2
    exit 2
fi

workspace_root=$1
root_domain=$2
lock_wait_seconds=$3
enum_deadline_seconds=$4
query_grace_seconds=$5
force_kill_grace_seconds=$6
shift 6

runtime_dir="${{workspace_root%/}}/.drowai/amass"
xdg_config_home="$runtime_dir/xdg-config"
output_dir="$xdg_config_home/amass"
asset_db="$output_dir/asset.db"
xdg_data_home="$runtime_dir/xdg-data"
xdg_cache_home="$runtime_dir/xdg-cache"
runs_dir="$runtime_dir/runs"
lock_path="$runtime_dir/workflow.lock"
owner_path="$runtime_dir/engine-owner.env"
engine_port="${{DROWAI_AMASS_ENGINE_PORT:-4000}}"

mkdir -p "$runtime_dir" "$output_dir" "$xdg_config_home" "$xdg_data_home" "$xdg_cache_home" "$runs_dir"
export XDG_CONFIG_HOME="$xdg_config_home"
export XDG_DATA_HOME="$xdg_data_home"
export XDG_CACHE_HOME="$xdg_cache_home"

release_lock() {{
    {{ exec 9>&-; }} 2>/dev/null || true
}}

acquire_lock() {{
    exec 9>"$lock_path"
    python3 - "$lock_wait_seconds" 9 <<'PY'
import errno
import fcntl
import sys
import time

deadline_seconds = max(0.0, float(sys.argv[1]))
lock_fd = int(sys.argv[2])
deadline = time.monotonic() + deadline_seconds

while True:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        raise SystemExit(0)
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EAGAIN):
            raise
    if deadline_seconds <= 0 or time.monotonic() >= deadline:
        raise SystemExit({AMASS_TIMEOUT_EXIT_CODE})
    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
PY
    lock_status=$?
    if [ "$lock_status" -ne 0 ]; then
        {{ exec 9>&-; }} 2>/dev/null || true
        return {AMASS_TIMEOUT_EXIT_CODE}
    fi
    trap release_lock EXIT
    return 0
}}

port_is_open() {{
    ( : < "/dev/tcp/127.0.0.1/${{engine_port}}" ) >/dev/null 2>&1
}}

port_owner_pid() {{
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -tiTCP:"$engine_port" -sTCP:LISTEN 2>/dev/null | head -n 1
        return 0
    fi
    if command -v ss >/dev/null 2>&1; then
        ss -ltnp "sport = :$engine_port" 2>/dev/null | sed -n 's/.*pid=\\([0-9][0-9]*\\).*/\\1/p' | head -n 1
        return 0
    fi
    return 0
}}

write_owner_marker() {{
    marker_pid="${{1:-}}"
    tmp_owner="$owner_path.$$"
    {{
        printf 'drowai_amass_output_dir=%s\\n' "$output_dir"
        printf 'drowai_amass_engine_port=%s\\n' "$engine_port"
        printf 'drowai_amass_engine_pid=%s\\n' "$marker_pid"
    }} > "$tmp_owner"
    mv "$tmp_owner" "$owner_path"
}}

owner_marker_matches_storage() {{
    [ -f "$owner_path" ] \
        && grep -Fxq "drowai_amass_output_dir=$output_dir" "$owner_path" \
        && grep -Fxq "drowai_amass_engine_port=$engine_port" "$owner_path"
}}

owner_marker_matches_process() {{
    current_owner_pid=$(port_owner_pid)
    recorded_owner_pid=$(sed -n 's/^drowai_amass_engine_pid=//p' "$owner_path" 2>/dev/null | head -n 1)
    [ -n "$current_owner_pid" ] \
        && [ -n "$recorded_owner_pid" ] \
        && [ "$current_owner_pid" = "$recorded_owner_pid" ] \
        && kill -0 "$recorded_owner_pid" 2>/dev/null
}}

emit_status() {{
    printf '%s\\n' '{AMASS_STATUS_BEGIN}'
    printf 'asset_db_readable_before=%s\\n' "$asset_db_readable_before"
    printf 'asset_db_readable_after=%s\\n' "$asset_db_readable_after"
    printf 'enum_status=%s\\n' "$enum_status"
    printf 'engine_owned=%s\\n' "$engine_owned"
    printf 'error_code=%s\\n' "$error_code"
    printf 'final_status=%s\\n' "$final_status"
    printf 'post_query_status=%s\\n' "$post_query_status"
    printf 'pre_query_status=%s\\n' "$pre_query_status"
    printf 'timed_out=%s\\n' "$timed_out"
    printf '%s\\n' '{AMASS_STATUS_END}'
}}

run_with_deadline() {{
    command_deadline=$1
    use_force_grace=$2
    shift 2
    if [ "$command_deadline" -le 0 ]; then
        return {AMASS_TIMEOUT_EXIT_CODE}
    fi
    "$@" &
    child_pid=$!
    deadline_ticks=$((command_deadline * 10))
    elapsed_ticks=0
    command_timed_out=false
    while kill -0 "$child_pid" 2>/dev/null; do
        if [ "$elapsed_ticks" -ge "$deadline_ticks" ]; then
            command_timed_out=true
            kill -INT "$child_pid" 2>/dev/null || true
            if [ "$use_force_grace" = true ]; then
                sleep "$force_kill_grace_seconds"
            else
                sleep 0.1
            fi
            if kill -0 "$child_pid" 2>/dev/null; then
                kill -TERM "$child_pid" 2>/dev/null || true
                if [ "$use_force_grace" = true ]; then
                    sleep 1
                else
                    sleep 0.1
                fi
            fi
            if kill -0 "$child_pid" 2>/dev/null; then
                kill -KILL "$child_pid" 2>/dev/null || true
            fi
            wait "$child_pid" 2>/dev/null || true
            return {AMASS_TIMEOUT_EXIT_CODE}
        fi
        sleep 0.1
        elapsed_ticks=$((elapsed_ticks + 1))
    done
    wait "$child_pid"
    return $?
}}

query_names() {{
    begin_marker=$1
    end_marker=$2
    budget_name=$3
    shift 3
    printf '%s\\n' "$begin_marker"
    eval "remaining_budget=\\$$budget_name"
    if [ -r "$asset_db" ]; then
        if [ "$remaining_budget" -le 0 ]; then
            query_status={AMASS_TIMEOUT_EXIT_CODE}
        else
            query_started_epoch=$(date +%s)
            run_with_deadline "$remaining_budget" false amass subs -dir "$output_dir" -d "$root_domain" "$@" -nocolor
            query_status=$?
            query_finished_epoch=$(date +%s)
            query_elapsed=$((query_finished_epoch - query_started_epoch))
            if [ "$query_elapsed" -lt 1 ]; then
                query_elapsed=1
            fi
            remaining_budget=$((remaining_budget - query_elapsed))
            if [ "$remaining_budget" -lt 0 ]; then
                remaining_budget=0
            fi
            eval "$budget_name=$remaining_budget"
        fi
    else
        query_status=0
    fi
    printf '%s\\n' "$end_marker"
    return "$query_status"
}}

engine_owned=false
error_code=""
final_status="failed"
timed_out=false
enum_status=0
pre_query_status=0
post_query_status=0
asset_db_readable_before=false
asset_db_readable_after=false
pre_query_budget_seconds=$((query_grace_seconds / 4))
post_query_budget_seconds=$((query_grace_seconds - pre_query_budget_seconds))

if [ -r "$asset_db" ]; then
    asset_db_readable_before=true
fi

if ! acquire_lock; then
    timed_out=true
    enum_status={AMASS_TIMEOUT_EXIT_CODE}
    error_code="lock_timeout"
    final_status="timed_out"
    if [ -r "$asset_db" ]; then
        asset_db_readable_after=true
    fi
    printf '%s\\n' "DROWAI_AMASS_LOCK_TIMEOUT: could not acquire workflow lock within $lock_wait_seconds seconds" >&2
    emit_status
    exit {AMASS_TIMEOUT_EXIT_CODE}
fi

if port_is_open; then
    if ! owner_marker_matches_storage || ! owner_marker_matches_process; then
        error_code="unowned_engine_port_occupied"
        echo "DROWAI_AMASS_ENGINE_UNOWNED: port $engine_port is occupied by an engine whose task workspace ownership cannot be proven" >&2
        emit_status
        exit {AMASS_UNOWNED_ENGINE_EXIT_CODE}
    fi
    engine_owned=true
else
    write_owner_marker ""
    engine_owned=true
fi

query_names '{AMASS_BEFORE_NAMES_BEGIN}' '{AMASS_BEFORE_NAMES_END}' pre_query_budget_seconds -names
pre_names_status=$?
query_names '{AMASS_BEFORE_RESOLVED_BEGIN}' '{AMASS_BEFORE_RESOLVED_END}' pre_query_budget_seconds -names -ip
pre_resolved_status=$?
if [ "$pre_names_status" -ne 0 ]; then
    pre_query_status=$pre_names_status
elif [ "$pre_resolved_status" -ne 0 ]; then
    pre_query_status=$pre_resolved_status
fi

amass enum -dir "$output_dir" -d "$root_domain" "$@" 1>&2 &
enum_pid=$!
enum_deadline_ticks=$((enum_deadline_seconds * 10))
enum_elapsed_ticks=0
while kill -0 "$enum_pid" 2>/dev/null; do
    if [ "$enum_elapsed_ticks" -ge "$enum_deadline_ticks" ]; then
        timed_out=true
        kill -INT "$enum_pid" 2>/dev/null || true
        sleep "$force_kill_grace_seconds"
        if kill -0 "$enum_pid" 2>/dev/null; then
            kill -TERM "$enum_pid" 2>/dev/null || true
            sleep 1
        fi
        if kill -0 "$enum_pid" 2>/dev/null; then
            kill -KILL "$enum_pid" 2>/dev/null || true
        fi
        wait "$enum_pid" 2>/dev/null || true
        enum_status={AMASS_TIMEOUT_EXIT_CODE}
        break
    fi
    sleep 0.1
    enum_elapsed_ticks=$((enum_elapsed_ticks + 1))
done
if [ "$timed_out" = false ]; then
    wait "$enum_pid"
    enum_status=$?
fi
if port_is_open; then
    write_owner_marker "$(port_owner_pid)"
fi

if [ -r "$asset_db" ]; then
    asset_db_readable_after=true
fi

query_names '{AMASS_NAMES_BEGIN}' '{AMASS_NAMES_END}' post_query_budget_seconds -names
post_names_status=$?
query_names '{AMASS_RESOLVED_BEGIN}' '{AMASS_RESOLVED_END}' post_query_budget_seconds -names -ip
post_resolved_status=$?
if [ "$post_names_status" -ne 0 ]; then
    post_query_status=$post_names_status
elif [ "$post_resolved_status" -ne 0 ]; then
    post_query_status=$post_resolved_status
fi

if [ "$timed_out" = true ]; then
    final_status="timed_out"
    error_code="enum_timeout"
    emit_status
    exit {AMASS_TIMEOUT_EXIT_CODE}
fi
if [ "$enum_status" -ne 0 ]; then
    final_status="enum_failed"
    error_code="enum_failed"
    emit_status
    exit "$enum_status"
fi
if [ "$post_query_status" -ne 0 ]; then
    final_status="query_failed"
    error_code="query_failed"
    emit_status
    exit "$post_query_status"
fi

final_status="complete"
emit_status
exit 0
"""


__all__ = [
    "AMASS_BEFORE_NAMES_BEGIN",
    "AMASS_BEFORE_NAMES_END",
    "AMASS_BEFORE_RESOLVED_BEGIN",
    "AMASS_BEFORE_RESOLVED_END",
    "AMASS_COLLECTOR_RELATIVE_PATH",
    "AMASS_COLLECTOR_SCRIPT",
    "AMASS_CONTAINER_COLLECTOR_PATH",
    "AMASS_NAMES_BEGIN",
    "AMASS_NAMES_END",
    "AMASS_OUTPUT_RELATIVE_DIR",
    "AMASS_PROVIDER_DEADLINE_MARGIN_SECONDS",
    "AMASS_RESOLVED_BEGIN",
    "AMASS_RESOLVED_END",
    "AMASS_RUNTIME_RELATIVE_DIR",
    "AMASS_STATUS_BEGIN",
    "AMASS_STATUS_END",
    "AmassCollectorExecution",
    "AmassCollectorTimeoutBudget",
    "build_amass_collector_command",
    "build_amass_timeout_budget",
    "execute_amass_collector_locally",
    "prepare_amass_workspace_directories",
    "prepare_amass_workspace_files",
]
