"""Run the curated public-release test gate.

This module owns the first official release-blocking test entrypoint. It keeps
the gate explicit and small so historical tests can be triaged separately
without making stale coverage authoritative.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_DATABASE_URL = "sqlite:///./.release-gate.sqlite3"
MIN_PYTHON_VERSION = (3, 11)


@dataclass(frozen=True)
class GateCommand:
    """A single release-gate command with a stable label."""

    name: str
    command: Sequence[str]
    tiers: frozenset[str]


def _tool(name: str) -> str:
    if os.name != "nt":
        return name
    return f"{name}.cmd"


def _version_for_python(executable: str) -> tuple[int, int] | None:
    result = subprocess.run(
        [
            executable,
            "-c",
            "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        major, minor = result.stdout.strip().split(".", 1)
        return int(major), int(minor)
    except ValueError:
        return None


def _python_executable() -> str:
    candidates: list[str] = []
    explicit = os.environ.get("DROWAI_TEST_PYTHON")
    if explicit:
        candidates.append(explicit)
    candidates.extend(
        [
            sys.executable,
            str(REPO_ROOT / ".venv" / "bin" / "python"),
            str(REPO_ROOT / ".venv313" / "bin" / "python"),
            "python3.13",
            "python3.12",
            "python3.11",
            "python3",
        ]
    )

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        resolved = shutil.which(candidate) if os.sep not in candidate else candidate
        if not resolved or not Path(resolved).exists():
            continue
        version = _version_for_python(resolved)
        if version is not None and version >= MIN_PYTHON_VERSION:
            return resolved

    raise RuntimeError(
        "Release gate requires Python 3.11+. Set DROWAI_TEST_PYTHON to a valid interpreter."
    )


def _commands(*, include_e2e: bool) -> list[GateCommand]:
    python = _python_executable()
    npx = _tool("npx")
    npm = _tool("npm")

    quick_backend_paths = (
        "backend/tests/services/test_tenant_context_service.py",
        "backend/tests/services/test_runtime_provider_contracts.py",
        "backend/tests/services/runtime_provider/test_registry.py",
        "backend/tests/services/test_ws_gateway_authorize.py",
        "backend/tests/test_auth_config_expiry.py",
        "backend/tests/test_main_websocket_task_ownership.py",
        "backend/tests/routers/test_tasks_tenant_authz.py",
        "backend/tests/routers/test_chat_tenant_authz.py",
        "backend/tests/streaming/test_stream_event_schema.py",
    )
    main_backend_paths = (
        "backend/tests/security/test_runner_control_security.py",
        "backend/tests/services/runtime_provider/test_context_resolver.py",
        "backend/tests/services/runner_control/test_protocol.py",
        "tests/runtime_shared/test_runner_protocol.py",
        "tests/runner/test_runner_status_contract.py",
    )
    frontend_contract_paths = (
        "client/src/lib/__tests__/api-config.tenant-context.test.ts",
        "client/src/lib/__tests__/auth-session.test.ts",
        "client/src/services/runtime_stream/__tests__/RuntimeStreamClient.test.ts",
        "client/src/services/runtime_stream/__tests__/TaskSubscriptionPlanner.test.ts",
        "client/src/services/runtime_stream/__tests__/StreamPacketIngestor.test.ts",
        "client/src/state/__tests__/chat-stream-store.test.ts",
        "client/src/utils/__tests__/stepToChatMessage.test.ts",
        "client/src/hooks/__tests__/useMessageGrouping.test.ts",
    )
    fixture_contract_paths = (
        "e2e/fixtures/actors-domain.test.ts",
        "e2e/fixtures/artifact-policy.test.ts",
        "e2e/fixtures/runtime-local-contract.test.ts",
        "e2e/fixtures/sanitized-logs.test.ts",
    )

    commands = [
        GateCommand(
            name="Product version consistency",
            command=(python, "scripts/check_version_consistency.py"),
            tiers=frozenset({"quick", "main"}),
        ),
        GateCommand(
            name="Publication safety",
            command=(python, "scripts/check_publication_safety.py"),
            tiers=frozenset({"quick", "main"}),
        ),
        GateCommand(
            name="Backend auth, tenant, runtime, and stream contracts",
            command=(python, "-m", "pytest", *quick_backend_paths, "-q"),
            tiers=frozenset({"quick", "main"}),
        ),
        GateCommand(
            name="LangGraph quick regression gate",
            command=(python, "scripts/run_langgraph_regression_suite.py", "--tier", "quick"),
            tiers=frozenset({"quick", "main"}),
        ),
        GateCommand(
            name="Frontend auth and streaming contracts",
            command=(npx, "vitest", "run", *frontend_contract_paths),
            tiers=frozenset({"quick", "main"}),
        ),
        GateCommand(
            name="E2E fixture and artifact-security contracts",
            command=(npm, "run", "test:e2e:fixture-contracts:quick"),
            tiers=frozenset({"quick", "main"}),
        ),
        GateCommand(
            name="Frontend TypeScript check",
            command=(npm, "run", "check"),
            tiers=frozenset({"quick", "main"}),
        ),
        GateCommand(
            name="Frontend production build",
            command=(npm, "run", "build"),
            tiers=frozenset({"quick", "main"}),
        ),
        GateCommand(
            name="Runner/runtime provider main contracts",
            command=(python, "-m", "pytest", *main_backend_paths, "-q"),
            tiers=frozenset({"main"}),
        ),
    ]
    if include_e2e:
        commands.append(
            GateCommand(
                name="Deterministic Playwright PR core",
                command=(npm, "run", "test:e2e:pr"),
                tiers=frozenset({"quick", "main"}),
            )
        )
    return commands


def _missing_paths(commands: Sequence[GateCommand]) -> list[str]:
    missing: list[str] = []
    for entry in commands:
        for token in entry.command:
            if not token.startswith(("backend/", "client/", "tests/", "scripts/", "e2e/")):
                continue
            path = REPO_ROOT / token
            if not path.exists():
                missing.append(token)
    return sorted(set(missing))


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    env.setdefault("E2E_DETERMINISTIC_MODE", "true")
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("TASK_RUNTIME_PLACEMENT_MODE_DEFAULT", "local")
    return env


def _run(entry: GateCommand, *, env: dict[str, str], verbose: bool) -> int:
    print(f"[release-gate] {entry.name}")
    if verbose:
        print("[release-gate] $ " + " ".join(entry.command))
    result = subprocess.run(entry.command, cwd=REPO_ROOT, env=env, check=False)
    if result.returncode != 0:
        print(f"[release-gate] FAIL: {entry.name} (exit {result.returncode})")
        return result.returncode
    print(f"[release-gate] PASS: {entry.name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tier",
        choices=("quick", "main"),
        default="quick",
        help="Release gate tier to run. `quick` is the PR/default gate; `main` is stronger.",
    )
    parser.add_argument(
        "--include-e2e",
        action="store_true",
        help="Also run the isolated deterministic Playwright PR core.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print the selected commands without running them.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print command lines before execution.",
    )
    args = parser.parse_args()

    selected = [
        entry for entry in _commands(include_e2e=args.include_e2e) if args.tier in entry.tiers
    ]
    missing = _missing_paths(selected)
    if missing:
        print("[release-gate] Missing release-gate targets:")
        for path in missing:
            print(f"[release-gate] - {path}")
        return 2

    print(f"[release-gate] Tier={args.tier} checks={len(selected)} include_e2e={args.include_e2e}")
    if args.list:
        for entry in selected:
            print(f"[release-gate] {entry.name}: {' '.join(entry.command)}")
        return 0

    env = _environment()
    for entry in selected:
        code = _run(entry, env=env, verbose=args.verbose)
        if code != 0:
            return code
    print("[release-gate] All release-gate checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
