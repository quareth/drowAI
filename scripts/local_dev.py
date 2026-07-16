#!/usr/bin/env python3
"""Local development launcher for the single-host managed-runtime stack.

This script does not introduce a standalone runtime architecture. It runs the
same managed-runner control-channel path locally by starting backend,
runner cloud client, and frontend as separate processes on one machine.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import IO
from urllib import request as urllib_request

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from backend.config.generated_config import default_generated_paths, resolved_backend_env  # noqa: E402
from backend.migrations.runtime import upgrade_database_to_head  # noqa: E402
from deploy.env_contract import DEFAULT_RUNNER_CAPABILITIES, single_host_management_env  # noqa: E402
from runtime_shared.runtime_image_contract import default_runtime_image_for_machine  # noqa: E402
from scripts.local_postgres_bootstrap import (  # noqa: E402
    ADMIN_DATABASE_URL_ENV,
    LocalPostgresBootstrapError,
    ensure_local_postgres_ready,
)

DOTENV_PATH = REPO_ROOT / ".env"
DEFAULT_BACKEND_HOST = "127.0.0.1"
DEFAULT_BACKEND_PORT = 8000
DEFAULT_FRONTEND_HOST = "0.0.0.0"
DEFAULT_FRONTEND_PORT = 5000
DEFAULT_RUNNER_ROOT = REPO_ROOT / ".drowai-runner-cloud"
PROJECT_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"


def _project_python() -> str:
    configured = str(os.getenv("DROWAI_PYTHON") or "").strip()
    if configured:
        return configured
    if PROJECT_PYTHON.exists():
        return str(PROJECT_PYTHON)
    return sys.executable


def _ensure_project_python() -> None:
    if not PROJECT_PYTHON.exists() or os.getenv("DROWAI_LOCAL_CLOUD_NO_REEXEC"):
        return
    current = Path(sys.executable).resolve()
    target = PROJECT_PYTHON.resolve()
    if current == target:
        return
    os.execv(str(target), [str(target), *sys.argv])


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    load_dotenv(dotenv_path=REPO_ROOT / ".env")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run DrowAI single-host managed-runtime architecture locally.",
    )
    parser.add_argument(
        "command",
        choices=(
            "up",
            "down",
            "backend",
            "runner",
            "frontend",
            "check",
            "print-env",
            "bootstrap-db",
        ),
        nargs="?",
        default="up",
        help=(
            "Component command to run. `up` starts backend, runner, and frontend; "
            "`bootstrap-db` provisions the local PostgreSQL role, database, and pgvector."
        ),
    )
    parser.add_argument("--no-frontend", action="store_true", help="Do not start Vite during `up`.")
    parser.add_argument("--backend-host", default=DEFAULT_BACKEND_HOST)
    parser.add_argument("--backend-port", type=int, default=DEFAULT_BACKEND_PORT)
    parser.add_argument("--frontend-host", default=DEFAULT_FRONTEND_HOST)
    parser.add_argument("--frontend-port", type=int, default=DEFAULT_FRONTEND_PORT)
    parser.add_argument("--runner-root", type=Path, default=DEFAULT_RUNNER_ROOT)
    parser.add_argument("--runner-install-token", default=None)
    parser.add_argument("--tenant-id", type=int, default=None)
    parser.add_argument("--user-id", type=int, default=None)
    parser.add_argument("--execution-site-slug", default="local-single-host")
    parser.add_argument("--execution-site-name", default="Local Single Host")
    parser.add_argument("--skip-token-bootstrap", action="store_true")
    return parser.parse_args(argv)


def _management_plane_url(args: argparse.Namespace) -> str:
    return f"http://{args.backend_host}:{args.backend_port}"


def _load_dotenv_env() -> dict[str, str]:
    if not DOTENV_PATH.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in DOTENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key or key.startswith("export "):
            key = key.removeprefix("export ").strip()
        if not key:
            continue
        value_text = raw_value.strip()
        try:
            parsed = shlex.split(value_text, comments=False, posix=True)
            value = parsed[0] if len(parsed) == 1 else value_text
        except ValueError:
            value = value_text.strip("\"'")
        values[key] = value
    return values


def _single_host_env(args: argparse.Namespace) -> dict[str, str]:
    generated_env = resolved_backend_env(profile="single_host", docker=False)
    env = {**generated_env, **_load_dotenv_env(), **os.environ}
    runner_root = args.runner_root.expanduser()
    if not runner_root.is_absolute():
        runner_root = REPO_ROOT / runner_root
    runtime_image = str(
        env.get("DROWAI_RUNTIME_IMAGE") or default_runtime_image_for_machine()
    ).strip()
    management_plane_url = _management_plane_url(args)

    defaults = single_host_management_env(
        control_plane_url=management_plane_url,
        runtime_image=runtime_image or default_runtime_image_for_machine(),
        runner_root=str(runner_root),
        host_bind_root=str(runner_root),
    )
    defaults.update(
        {
            "DROWAI_RUNNER_DEV_MODE": "true",
            "DROWAI_RUNNER_LABELS": json.dumps(
                {
                    "deployment": "local-single-host",
                    "site": "local-dev",
                    "host": "localhost",
                },
                sort_keys=True,
            ),
            "DROWAI_RUNNER_CAPABILITIES": ",".join(DEFAULT_RUNNER_CAPABILITIES),
        }
    )
    env.update(defaults)
    tenant_id = args.tenant_id or _coerce_positive_int(env.get("DROWAI_LOCAL_CLOUD_TENANT_ID"))
    if tenant_id is not None:
        env["DROWAI_RUNNER_TENANT_ID"] = str(tenant_id)
    return env


def _coerce_positive_int(value: object) -> int | None:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _credential_secret_path(env: dict[str, str]) -> Path:
    configured = str(env.get("DROWAI_RUNNER_CREDENTIAL_SECRET_PATH") or "").strip()
    runner_root = Path(env["DROWAI_RUNNER_ROOT"]).expanduser()
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else runner_root / path
    return runner_root / "credentials" / "runner.secret"


def _has_stored_runner_identity(env: dict[str, str]) -> bool:
    secret_path = _credential_secret_path(env)
    runner_id_path = secret_path.with_name(f"{secret_path.name}.runner_id")
    return secret_path.exists() and runner_id_path.exists()


def _stored_runner_identity_paths(env: dict[str, str]) -> list[Path]:
    """Return local runner credential files that bind a runner to Management DB state."""
    secret_path = _credential_secret_path(env)
    return [
        secret_path,
        secret_path.with_name(f"{secret_path.name}.runner_id"),
        secret_path.with_name(f"{secret_path.name}.tenant_id"),
        secret_path.with_name(f"{secret_path.name}.protocol_version"),
    ]


def _discard_stale_local_runner_identity(env: dict[str, str]) -> None:
    """Remove stored local runner credentials before first-run enrollment."""
    removed = False
    for path in _stored_runner_identity_paths(env):
        try:
            path.unlink()
            removed = True
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(f"Could not remove stale local runner identity file: {path}") from exc
    if removed:
        print("[local-cloud] removed stale local runner identity")


def _local_enrollment_path() -> Path:
    return default_generated_paths(docker=False).config_dir / "enrollment.toml"


def _discard_stale_local_enrollment(enrollment_path: Path) -> None:
    """Remove local runner enrollment before a fresh setup publishes the current one."""
    try:
        enrollment_path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError(f"Could not remove stale local runner enrollment: {enrollment_path}") from exc
    print(f"[local-cloud] removed stale local runner enrollment: {enrollment_path}")


def _pid_file_path(env: dict[str, str]) -> Path:
    return Path(env["DROWAI_RUNNER_ROOT"]).expanduser() / "local-cloud-pids.json"


def _write_pid_file(env: dict[str, str], processes: Sequence[tuple[str, subprocess.Popen[str]]]) -> None:
    path = _pid_file_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "processes": [
            {"name": name, "pid": int(process.pid)}
            for name, process in processes
            if process.pid
        ]
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _remove_pid_file(env: dict[str, str]) -> None:
    try:
        _pid_file_path(env).unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def _read_recorded_pids(env: dict[str, str]) -> list[tuple[str, int]]:
    path = _pid_file_path(env)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    rows = payload.get("processes") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    recorded: list[tuple[str, int]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "process")
        try:
            pid = int(row.get("pid"))
        except (TypeError, ValueError):
            continue
        if pid > 0:
            recorded.append((name, pid))
    return recorded


def _resolve_tenant_and_user(args: argparse.Namespace, env: dict[str, str]) -> tuple[int, int]:
    """Resolve local tenant/user identity required by the cloud runner path."""
    from sqlalchemy import text

    from backend.database import SessionLocal

    db = SessionLocal()
    try:
        tenant_id = args.tenant_id or _coerce_positive_int(env.get("DROWAI_RUNNER_TENANT_ID"))
        if tenant_id is None:
            tenant_id = db.execute(text("SELECT id FROM tenants ORDER BY id LIMIT 1")).scalar()
        if tenant_id is None:
            raise RuntimeError("No tenant exists; create/login a tenant before local cloud runner startup.")
        tenant_id = int(tenant_id)

        user_id = args.user_id or _coerce_positive_int(env.get("DROWAI_LOCAL_CLOUD_USER_ID"))
        if user_id is None:
            try:
                user_id = db.execute(
                    text("SELECT id FROM users WHERE tenant_id = :tenant_id ORDER BY id LIMIT 1"),
                    {"tenant_id": tenant_id},
                ).scalar()
            except Exception:
                db.rollback()
                user_id = db.execute(text("SELECT id FROM users ORDER BY id LIMIT 1")).scalar()
        if user_id is None:
            raise RuntimeError("No user exists; create/login a user before local cloud runner startup.")
        return tenant_id, int(user_id)
    finally:
        db.close()


def _ensure_runner_tenant_env(args: argparse.Namespace, env: dict[str, str]) -> tuple[int, int]:
    tenant_id, user_id = _resolve_tenant_and_user(args, env)
    env["DROWAI_RUNNER_TENANT_ID"] = str(tenant_id)
    return tenant_id, user_id


def _has_setup_identity(args: argparse.Namespace, env: dict[str, str]) -> bool:
    try:
        _resolve_tenant_and_user(args, env)
        return True
    except RuntimeError as exc:
        message = str(exc)
        if "No tenant exists" in message or "No user exists" in message:
            return False
        raise


def _wait_for_backend(base_url: str, *, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    health_url = f"{base_url.rstrip('/')}/api/health"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib_request.urlopen(health_url, timeout=1.5) as response:  # noqa: S310
                if response.status == 200:
                    return
        except Exception as exc:
            last_error = exc
        time.sleep(0.5)
    raise RuntimeError(f"Backend did not become healthy at {health_url}: {last_error}")


def _wait_for_connected_runner(
    args: argparse.Namespace,
    env: dict[str, str],
    *,
    runner_process: subprocess.Popen[str] | None = None,
    timeout_seconds: float = 30.0,
) -> None:
    tenant_id, _user_id = _ensure_runner_tenant_env(args, env)
    last_detail = "runner did not connect"

    from backend.database import SessionLocal
    from backend.models.runner_control import Runner, RunnerConnection
    from backend.services.runner_control.assignment_service import RunnerAssignmentRequest, RunnerAssignmentService
    from sqlalchemy import select

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if runner_process is not None and runner_process.poll() is not None:
            raise RuntimeError(
                "Cloud runner process exited before opening a control-channel connection "
                f"(exit_code={runner_process.returncode})."
            )
        db = SessionLocal()
        try:
            now = datetime.now(tz=timezone.utc)
            assignment = RunnerAssignmentService(db).select_runner(
                RunnerAssignmentRequest(tenant_id=tenant_id),
                now=now,
            )
            if assignment.selection is not None:
                selection = assignment.selection
                connection = db.execute(
                    select(RunnerConnection)
                    .where(
                        RunnerConnection.tenant_id == tenant_id,
                        RunnerConnection.runner_id == selection.runner_id,
                        RunnerConnection.status == "active",
                        RunnerConnection.lease_expires_at > now,
                    )
                    .order_by(RunnerConnection.last_seen_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                print(
                    "[local-cloud] cloud runner connected "
                    f"tenant_id={tenant_id} "
                    f"runner_id={selection.runner_id} "
                    f"execution_site_id={selection.execution_site_id} "
                    f"connection_id={connection.connection_id if connection is not None else '<NO_ACTIVE_CONNECTION>'} "
                    f"available_tasks={selection.available_tasks}"
                )
                return

            runner = db.execute(
                select(Runner)
                .where(Runner.tenant_id == tenant_id)
                .order_by(Runner.last_seen_at.desc().nullslast())
                .limit(1)
            ).scalar_one_or_none()
            if runner is None:
                last_detail = "NO_RUNNERS_REGISTERED"
            else:
                reasons = ",".join(assignment.reason_codes) if assignment.reason_codes else "NO_ELIGIBLE_RUNNER"
                last_detail = (
                    f"runner_id={runner.id} status={runner.status} "
                    f"last_seen_at={runner.last_seen_at} reasons={reasons}"
                )
        except Exception as exc:
            db.rollback()
            last_detail = str(exc)
        finally:
            db.close()
        time.sleep(0.5)

    raise RuntimeError(
        "No connected cloud runner became available for local single-host testing: "
        f"{last_detail}"
    )


def _wait_for_local_enrollment(
    *,
    processes: Sequence[tuple[str, subprocess.Popen[str]]],
    enrollment_path: Path,
) -> Path:
    print(
        "[local-cloud] waiting for setup wizard to publish local runner enrollment: "
        f"{enrollment_path}"
    )
    print("[local-cloud] open the frontend and complete setup; runner starts automatically afterwards")
    while True:
        if enrollment_path.is_file() and enrollment_path.stat().st_size > 0:
            return enrollment_path
        for name, process in processes:
            return_code = process.poll()
            if return_code is not None:
                raise RuntimeError(
                    f"{name} exited before setup published local runner enrollment "
                    f"(exit_code={return_code})."
                )
        time.sleep(1.0)


def _bootstrap_install_token(args: argparse.Namespace, env: dict[str, str]) -> str:
    if args.runner_install_token:
        _ensure_runner_tenant_env(args, env)
        return str(args.runner_install_token).strip()
    tenant_id, user_id = _ensure_runner_tenant_env(args, env)
    if _has_stored_runner_identity(env):
        return ""
    if args.skip_token_bootstrap:
        return ""

    from backend.database import SessionLocal
    from backend.models.runner_control import ExecutionSite
    from backend.services.runner_control.registry_service import RunnerRegistryService

    db = SessionLocal()
    try:
        service = RunnerRegistryService(db)
        site = (
            db.query(ExecutionSite)
            .filter(ExecutionSite.tenant_id == tenant_id, ExecutionSite.slug == args.execution_site_slug)
            .one_or_none()
        )
        if site is None:
            site = service.create_execution_site(
                tenant_id=tenant_id,
                name=args.execution_site_name,
                slug=args.execution_site_slug,
                network_label="local-dev",
                labels={"deployment": "local-single-host"},
            )
            db.commit()
            db.refresh(site)

        issued = service.issue_install_token(
            tenant_id=tenant_id,
            execution_site_id=site.id,
            created_by_user_id=int(user_id),
            ttl_seconds=86400,
        )
        db.commit()
        return issued.plaintext_token
    finally:
        db.close()


def _start_process(
    name: str,
    command: Sequence[str],
    *,
    env: dict[str, str],
    cwd: Path = REPO_ROOT,
) -> subprocess.Popen[str]:
    print(f"[local-cloud] starting {name}: {' '.join(command)}")
    return subprocess.Popen(  # noqa: S603
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )


def _stream_output(name: str, pipe: IO[str] | None) -> None:
    if pipe is None:
        return
    for line in iter(pipe.readline, ""):
        print(f"[{name}] {line.rstrip()}")


def _attach_output_threads(name: str, process: subprocess.Popen[str]) -> list[threading.Thread]:
    threads = [
        threading.Thread(target=_stream_output, args=(f"{name}:stdout", process.stdout), daemon=True),
        threading.Thread(target=_stream_output, args=(f"{name}:stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    return threads


def _stop_process(name: str, process: subprocess.Popen[str]) -> None:
    print(f"[local-cloud] stopping {name}")
    _terminate_process_group(pid=process.pid, signal_number=signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _terminate_process_group(pid=process.pid, signal_number=signal.SIGKILL)
        process.wait(timeout=2)


def _terminate_process_group(*, pid: int, signal_number: int) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    try:
        os.killpg(pid, signal_number)
        return
    except ProcessLookupError:
        pass
    except OSError:
        pass

    try:
        os.kill(pid, signal_number)
    except ProcessLookupError:
        return
    except OSError:
        return


def _stop_recorded_processes(env: dict[str, str]) -> None:
    for name, pid in reversed(_read_recorded_pids(env)):
        print(f"[local-cloud] stopping recorded {name} pid={pid}")
        _terminate_process_group(pid=pid, signal_number=signal.SIGTERM)
    time.sleep(0.5)
    for name, pid in reversed(_read_recorded_pids(env)):
        if _pid_exists(pid):
            print(f"[local-cloud] force stopping recorded {name} pid={pid}")
            _terminate_process_group(pid=pid, signal_number=signal.SIGKILL)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False


def _pids_listening_on_port(port: int) -> list[int]:
    result = subprocess.run(  # noqa: S603
        ["lsof", "-tiTCP:" + str(port), "-sTCP:LISTEN"],
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode not in {0, 1}:
        return []
    pids: list[int] = []
    for line in result.stdout.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid > 0:
            pids.append(pid)
    return pids


def _stop_port_listeners(*, ports: Sequence[int]) -> None:
    seen: set[int] = set()
    for port in ports:
        for pid in _pids_listening_on_port(port):
            if pid in seen:
                continue
            seen.add(pid)
            print(f"[local-cloud] stopping process listening on port {port} pid={pid}")
            _terminate_process_group(pid=pid, signal_number=signal.SIGTERM)
    time.sleep(0.5)
    for pid in seen:
        if _pid_exists(pid):
            print(f"[local-cloud] force stopping port listener pid={pid}")
            _terminate_process_group(pid=pid, signal_number=signal.SIGKILL)


def _run_backend(args: argparse.Namespace, env: dict[str, str]) -> subprocess.Popen[str]:
    return _start_process(
        "backend",
        [
            _project_python(),
            "-m",
            "uvicorn",
            "backend.main:app",
            "--host",
            args.backend_host,
            "--port",
            str(args.backend_port),
            "--reload",
        ],
        env=env,
    )


def _run_runner(env: dict[str, str], *, config_path: Path | None = None) -> subprocess.Popen[str]:
    command = [_project_python(), "-m", "drowai_runner"]
    if config_path is not None:
        command.extend(["--config", str(config_path)])
    command.append("run")
    return _start_process(
        "runner",
        command,
        env=env,
    )


def _run_frontend(args: argparse.Namespace, env: dict[str, str]) -> subprocess.Popen[str]:
    return _start_process(
        "frontend",
        ["npx", "vite", "--host", args.frontend_host, "--port", str(args.frontend_port)],
        env=env,
    )


def _run_runtime_image_preflight(env: dict[str, str]) -> int:
    """Verify the configured task runtime image before accepting task traffic."""
    result = subprocess.run(  # noqa: S603
        [_project_python(), "-m", "drowai_runner", "runtime-info"],
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip())
    if result.returncode != 0:
        runtime_image = env.get("DROWAI_RUNTIME_IMAGE", "")
        print(
            "[local-cloud] runtime image preflight failed; "
            f"DROWAI_RUNTIME_IMAGE={runtime_image}"
        )
    return int(result.returncode)


def _run_schema_migrations(env: dict[str, str]) -> None:
    print("[local-cloud] applying database migrations")
    upgrade_database_to_head(
        env=env,
        repo_root=REPO_ROOT,
        python_executable=_project_python(),
    )


def _run_check(args: argparse.Namespace, env: dict[str, str]) -> int:
    required = [
        "DATABASE_URL",
        "DROWAI_DEPLOYMENT_PROFILE",
        "TASK_RUNTIME_PLACEMENT_MODE_DEFAULT",
        "ENABLE_CLOUD_RUNNER_CONTROL",
        "RUNNER_TOOL_COMMAND_ENABLED",
        "DROWAI_RUNNER_CONTROL_PLANE_URL",
        "DROWAI_RUNNER_TENANT_ID",
        "DROWAI_RUNNER_ROOT",
        "DROWAI_RUNTIME_IMAGE",
    ]
    try:
        _ensure_runner_tenant_env(args, env)
    except Exception as exc:
        print(f"[local-cloud] tenant/user readiness failed: {exc}")
        return 1

    missing = [key for key in required if not str(env.get(key) or "").strip()]
    if missing:
        print("[local-cloud] missing required env: " + ", ".join(missing))
        return 1

    try:
        _run_schema_migrations(env)
    except Exception as exc:
        print(f"[local-cloud] database migration failed: {exc}")
        return 1

    preflight_code = _run_runtime_image_preflight(env)
    if preflight_code != 0:
        return preflight_code

    try:
        from backend.config.deployment_topology import get_deployment_profile_state
        from backend.database import (
            ensure_tenant_baseline_schema_ready,
            ensure_runner_control_schema_ready,
        )

        get_deployment_profile_state()
        ensure_runner_control_schema_ready()
        ensure_tenant_baseline_schema_ready()
    except Exception as exc:
        print(f"[local-cloud] backend single-host readiness failed: {exc}")
        return 1

    result = subprocess.run(  # noqa: S603
        [_project_python(), "-m", "drowai_runner", "health"],
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip())
    return int(result.returncode)


def _print_env(env: dict[str, str]) -> None:
    keys = [
        "DROWAI_DEPLOYMENT_PROFILE",
        "TASK_RUNTIME_PLACEMENT_MODE_DEFAULT",
        "ENABLE_CLOUD_RUNNER_CONTROL",
        "RUNNER_TOOL_COMMAND_ENABLED",
        "DROWAI_RUNNER_CONTROL_PLANE_URL",
        "DROWAI_RUNNER_TENANT_ID",
        "DROWAI_RUNNER_ROOT",
        "DROWAI_RUNTIME_IMAGE",
    ]
    for key in keys:
        print(f"{key}={env.get(key, '')}")


def _run_up(args: argparse.Namespace, env: dict[str, str]) -> int:
    runner_env = dict(env)

    processes: list[tuple[str, subprocess.Popen[str]]] = []
    output_threads: list[threading.Thread] = []

    def cleanup() -> None:
        for name, process in reversed(processes):
            _stop_process(name, process)
        _remove_pid_file(env)

    def signal_handler(_sig: int, _frame: object) -> None:
        cleanup()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        _run_schema_migrations(env)

        backend = _run_backend(args, env)
        processes.append(("backend", backend))
        _write_pid_file(env, processes)
        output_threads.extend(_attach_output_threads("backend", backend))
        _wait_for_backend(_management_plane_url(args))

        setup_identity_exists = _has_setup_identity(args, runner_env)
        frontend_started = False
        if not setup_identity_exists:
            if not args.no_frontend:
                frontend = _run_frontend(args, env)
                processes.append(("frontend", frontend))
                frontend_started = True
                _write_pid_file(env, processes)
                output_threads.extend(_attach_output_threads("frontend", frontend))
            enrollment_path = _local_enrollment_path()
            _discard_stale_local_enrollment(enrollment_path)
            _discard_stale_local_runner_identity(runner_env)
            enrollment_path = _wait_for_local_enrollment(
                processes=processes,
                enrollment_path=enrollment_path,
            )
            runner = _run_runner(runner_env, config_path=enrollment_path)
        else:
            install_token = _bootstrap_install_token(args, runner_env)
            if install_token:
                runner_env["DROWAI_RUNNER_REGISTRATION_TOKEN"] = install_token
            runner = _run_runner(runner_env)

        processes.append(("runner", runner))
        _write_pid_file(env, processes)
        output_threads.extend(_attach_output_threads("runner", runner))
        _wait_for_connected_runner(args, runner_env, runner_process=runner)

        if not args.no_frontend and not frontend_started:
            frontend = _run_frontend(args, env)
            processes.append(("frontend", frontend))
            _write_pid_file(env, processes)
            output_threads.extend(_attach_output_threads("frontend", frontend))

        print("[local-cloud] single-host managed runtime is running")
        while True:
            for name, process in processes:
                return_code = process.poll()
                if return_code is not None:
                    print(f"[local-cloud] {name} exited with code {return_code}")
                    return int(return_code)
            time.sleep(0.3)
    finally:
        cleanup()


def _run_down(args: argparse.Namespace, env: dict[str, str]) -> int:
    _stop_recorded_processes(env)
    _stop_port_listeners(ports=(args.backend_port, args.frontend_port))
    _remove_pid_file(env)
    print("[local-cloud] local single-host managed-runtime processes stopped")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    _ensure_project_python()
    _load_dotenv()
    args = _parse_args(argv)
    env = _single_host_env(args)

    if args.command == "print-env":
        _print_env(env)
        return 0
    if args.command in {"up", "backend", "check", "bootstrap-db"}:
        try:
            ensure_local_postgres_ready(
                env,
                interactive=sys.stdin.isatty(),
            )
        except LocalPostgresBootstrapError as exc:
            print(f"[local-cloud] PostgreSQL readiness failed: {exc}")
            return 1
        env.pop(ADMIN_DATABASE_URL_ENV, None)
        os.environ.pop(ADMIN_DATABASE_URL_ENV, None)
    if args.command == "bootstrap-db":
        return 0
    if args.command == "check":
        return _run_check(args, env)
    if args.command == "down":
        return _run_down(args, env)
    if args.command == "backend":
        _run_schema_migrations(env)
        process = _run_backend(args, env)
    elif args.command == "frontend":
        process = _run_frontend(args, env)
    elif args.command == "runner":
        runner_env = dict(env)
        install_token = _bootstrap_install_token(args, runner_env)
        if install_token:
            runner_env["DROWAI_RUNNER_REGISTRATION_TOKEN"] = install_token
        process = _run_runner(runner_env)
    else:
        return _run_up(args, env)

    _attach_output_threads(args.command, process)
    try:
        return int(process.wait())
    except KeyboardInterrupt:
        _stop_process(args.command, process)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
