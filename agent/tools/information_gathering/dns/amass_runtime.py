"""Own task-scoped Amass v5 runtime state and collector execution."""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from importlib import resources
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


_COLLECTOR_ASSET_NAME = "amass_collector_v5.sh"
_COLLECTOR_PLACEHOLDER_PATTERN = re.compile(r"@@[A-Z0-9_]+@@")
_COLLECTOR_REPLACEMENTS = {
    "@@AMASS_BEFORE_NAMES_BEGIN@@": AMASS_BEFORE_NAMES_BEGIN,
    "@@AMASS_BEFORE_NAMES_END@@": AMASS_BEFORE_NAMES_END,
    "@@AMASS_BEFORE_RESOLVED_BEGIN@@": AMASS_BEFORE_RESOLVED_BEGIN,
    "@@AMASS_BEFORE_RESOLVED_END@@": AMASS_BEFORE_RESOLVED_END,
    "@@AMASS_NAMES_BEGIN@@": AMASS_NAMES_BEGIN,
    "@@AMASS_NAMES_END@@": AMASS_NAMES_END,
    "@@AMASS_RESOLVED_BEGIN@@": AMASS_RESOLVED_BEGIN,
    "@@AMASS_RESOLVED_END@@": AMASS_RESOLVED_END,
    "@@AMASS_STATUS_BEGIN@@": AMASS_STATUS_BEGIN,
    "@@AMASS_STATUS_END@@": AMASS_STATUS_END,
    "@@AMASS_TIMEOUT_EXIT_CODE@@": str(AMASS_TIMEOUT_EXIT_CODE),
    "@@AMASS_UNOWNED_ENGINE_EXIT_CODE@@": str(AMASS_UNOWNED_ENGINE_EXIT_CODE),
}


def _render_amass_collector_script() -> str:
    """Load and render the packaged collector with the shared marker contract."""

    template = (
        resources.files(__package__)
        .joinpath(_COLLECTOR_ASSET_NAME)
        .read_text(encoding="utf-8")
    )
    placeholders = frozenset(_COLLECTOR_PLACEHOLDER_PATTERN.findall(template))
    expected = frozenset(_COLLECTOR_REPLACEMENTS)
    if placeholders != expected:
        missing = sorted(expected - placeholders)
        unexpected = sorted(placeholders - expected)
        raise RuntimeError(
            "invalid Amass collector placeholders: "
            f"missing={missing}, unexpected={unexpected}"
        )

    for placeholder, value in _COLLECTOR_REPLACEMENTS.items():
        template = template.replace(placeholder, value)
    return template


AMASS_COLLECTOR_SCRIPT = _render_amass_collector_script()


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
