"""Tests for the local single-host managed-runtime launcher.

Responsibilities:
- Lock local single-host managed-runtime bootstrap behavior.
- Preserve safe shutdown compatibility across PID metadata schemas.
- Prevent stored runner credentials from skipping required tenant identity setup.
"""

from __future__ import annotations

from argparse import Namespace
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import signal
import subprocess
import uuid as uuid_lib

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Task, User
from backend.models.runner_control import ExecutionSite, Runner, RunnerConnection, RunnerCredential
from backend.models.tenant import Tenant
from drowai_runner.process_lock import RunnerProcessOwner
from scripts import local_dev


class _FakeProcess:
    def __init__(self, name: str) -> None:
        self.name = name
        self.pid = abs(hash(name)) % 100000 + 1000
        self.stdout = None
        self.stderr = None
        self.returncode = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


def test_single_host_env_defaults_use_managed_runner_control_channel() -> None:
    args = Namespace(
        backend_host="127.0.0.1",
        backend_port=8000,
        runner_root=Path(".drowai-runner-cloud"),
        tenant_id=None,
    )
    env = local_dev._single_host_env(args)

    assert env["DROWAI_DEPLOYMENT_PROFILE"] == "single_host"
    assert env["TASK_RUNTIME_PLACEMENT_MODE_DEFAULT"] == "runner"
    assert env["DROWAI_RUNNER_CONTROL_PLANE_URL"] == "http://127.0.0.1:8000"


def test_run_up_fresh_setup_waits_for_enrollment_before_runner(monkeypatch, tmp_path: Path) -> None:
    args = Namespace(
        no_frontend=False,
        backend_host="127.0.0.1",
        backend_port=8000,
        frontend_host="127.0.0.1",
        frontend_port=5000,
    )
    env = {
        "DROWAI_RUNNER_ROOT": str(tmp_path / "runner-root"),
        "DROWAI_RUNTIME_IMAGE": "runtime:test",
    }
    enrollment_path = tmp_path / "config" / "enrollment.toml"
    calls: list[tuple[str, object]] = []
    created: dict[str, _FakeProcess] = {}

    monkeypatch.setattr(local_dev, "_run_schema_migrations", lambda _env: calls.append(("migrate", None)))
    def _unexpected_runtime_image_preflight(_env):
        raise AssertionError("startup must defer missing-image handling to task materialization")

    monkeypatch.setattr(
        local_dev,
        "_run_runtime_image_preflight",
        _unexpected_runtime_image_preflight,
    )
    monkeypatch.setattr(local_dev, "_wait_for_backend", lambda _url: calls.append(("wait-backend", _url)))
    monkeypatch.setattr(local_dev, "_has_setup_identity", lambda _args, _env: False)
    monkeypatch.setattr(local_dev, "_local_enrollment_path", lambda: enrollment_path)
    monkeypatch.setattr(local_dev, "_write_pid_file", lambda _env, _processes: None)
    monkeypatch.setattr(local_dev, "_remove_pid_file", lambda _env: None)
    monkeypatch.setattr(local_dev, "_attach_output_threads", lambda _name, _process: [])
    monkeypatch.setattr(local_dev, "_wait_for_connected_runner", lambda *_args, **_kwargs: calls.append(("wait-runner", None)))

    def _fake_backend(_args, _env):
        calls.append(("backend", None))
        created["backend"] = _FakeProcess("backend")
        return created["backend"]

    def _fake_frontend(_args, _env):
        calls.append(("frontend", None))
        created["frontend"] = _FakeProcess("frontend")
        return created["frontend"]

    def _fake_wait_for_enrollment(*, processes, enrollment_path):
        calls.append(("wait-enrollment", [name for name, _process in processes]))
        enrollment_path.parent.mkdir(parents=True, exist_ok=True)
        enrollment_path.write_text("[runner]\n", encoding="utf-8")
        return enrollment_path

    def _fake_runner(_env, *, config_path=None):
        calls.append(("runner", config_path))
        created["runner"] = _FakeProcess("runner")
        return created["runner"]

    monkeypatch.setattr(local_dev, "_run_backend", _fake_backend)
    monkeypatch.setattr(local_dev, "_run_frontend", _fake_frontend)
    monkeypatch.setattr(local_dev, "_wait_for_local_enrollment", _fake_wait_for_enrollment)
    monkeypatch.setattr(local_dev, "_run_runner", _fake_runner)

    def _exit_main_loop(_seconds):
        created["backend"].returncode = 0

    monkeypatch.setattr(local_dev.time, "sleep", _exit_main_loop)

    code = local_dev._run_up(args, env)

    assert code == 0
    assert ("frontend", None) in calls
    assert ("wait-enrollment", ["backend", "frontend"]) in calls
    assert ("runner", enrollment_path) in calls


def test_run_up_fresh_setup_discards_stale_enrollment_before_wait(monkeypatch, tmp_path: Path) -> None:
    args = Namespace(
        no_frontend=True,
        backend_host="127.0.0.1",
        backend_port=8000,
        frontend_host="127.0.0.1",
        frontend_port=5000,
    )
    env = {
        "DROWAI_RUNNER_ROOT": str(tmp_path / "runner-root"),
        "DROWAI_RUNTIME_IMAGE": "runtime:test",
    }
    enrollment_path = tmp_path / "config" / "enrollment.toml"
    enrollment_path.parent.mkdir(parents=True)
    enrollment_path.write_text("stale enrollment", encoding="utf-8")
    created: dict[str, _FakeProcess] = {}
    wait_saw_stale_file = None

    monkeypatch.setattr(local_dev, "_run_schema_migrations", lambda _env: None)
    monkeypatch.setattr(local_dev, "_wait_for_backend", lambda _url: None)
    monkeypatch.setattr(local_dev, "_has_setup_identity", lambda _args, _env: False)
    monkeypatch.setattr(local_dev, "_local_enrollment_path", lambda: enrollment_path)
    monkeypatch.setattr(local_dev, "_write_pid_file", lambda _env, _processes: None)
    monkeypatch.setattr(local_dev, "_remove_pid_file", lambda _env: None)
    monkeypatch.setattr(local_dev, "_attach_output_threads", lambda _name, _process: [])
    monkeypatch.setattr(local_dev, "_wait_for_connected_runner", lambda *_args, **_kwargs: None)

    def _fake_backend(_args, _env):
        created["backend"] = _FakeProcess("backend")
        return created["backend"]

    def _fake_wait_for_enrollment(*, processes, enrollment_path):
        nonlocal wait_saw_stale_file
        del processes
        wait_saw_stale_file = enrollment_path.exists()
        enrollment_path.write_text("fresh enrollment", encoding="utf-8")
        return enrollment_path

    def _fake_runner(_env, *, config_path=None):
        del config_path
        created["runner"] = _FakeProcess("runner")
        return created["runner"]

    monkeypatch.setattr(local_dev, "_run_backend", _fake_backend)
    monkeypatch.setattr(local_dev, "_wait_for_local_enrollment", _fake_wait_for_enrollment)
    monkeypatch.setattr(local_dev, "_run_runner", _fake_runner)

    def _exit_main_loop(_seconds):
        created["backend"].returncode = 0

    monkeypatch.setattr(local_dev.time, "sleep", _exit_main_loop)

    code = local_dev._run_up(args, env)

    assert code == 0
    assert wait_saw_stale_file is False
    assert enrollment_path.read_text(encoding="utf-8") == "fresh enrollment"


def test_run_up_fresh_setup_discards_stale_runner_identity_before_start(monkeypatch, tmp_path: Path) -> None:
    args = Namespace(
        no_frontend=True,
        backend_host="127.0.0.1",
        backend_port=8000,
        frontend_host="127.0.0.1",
        frontend_port=5000,
    )
    runner_root = tmp_path / "runner-root"
    env = {
        "DROWAI_RUNNER_ROOT": str(runner_root),
        "DROWAI_RUNTIME_IMAGE": "runtime:test",
    }
    credential_dir = runner_root / "credentials"
    credential_dir.mkdir(parents=True)
    stale_paths = [
        credential_dir / "runner.secret",
        credential_dir / "runner.secret.runner_id",
        credential_dir / "runner.secret.tenant_id",
        credential_dir / "runner.secret.protocol_version",
    ]
    for path in stale_paths:
        path.write_text("stale", encoding="utf-8")
    enrollment_path = tmp_path / "config" / "enrollment.toml"
    created: dict[str, _FakeProcess] = {}
    runner_saw_stale_identity = None

    monkeypatch.setattr(local_dev, "_run_schema_migrations", lambda _env: None)
    monkeypatch.setattr(local_dev, "_wait_for_backend", lambda _url: None)
    monkeypatch.setattr(local_dev, "_has_setup_identity", lambda _args, _env: False)
    monkeypatch.setattr(local_dev, "_local_enrollment_path", lambda: enrollment_path)
    monkeypatch.setattr(local_dev, "_write_pid_file", lambda _env, _processes: None)
    monkeypatch.setattr(local_dev, "_remove_pid_file", lambda _env: None)
    monkeypatch.setattr(local_dev, "_attach_output_threads", lambda _name, _process: [])
    monkeypatch.setattr(local_dev, "_wait_for_connected_runner", lambda *_args, **_kwargs: None)

    def _fake_backend(_args, _env):
        created["backend"] = _FakeProcess("backend")
        return created["backend"]

    def _fake_wait_for_enrollment(*, processes, enrollment_path):
        del processes
        enrollment_path.parent.mkdir(parents=True, exist_ok=True)
        enrollment_path.write_text("fresh enrollment", encoding="utf-8")
        return enrollment_path

    def _fake_runner(_env, *, config_path=None):
        nonlocal runner_saw_stale_identity
        del config_path
        runner_saw_stale_identity = any(path.exists() for path in stale_paths)
        created["runner"] = _FakeProcess("runner")
        return created["runner"]

    monkeypatch.setattr(local_dev, "_run_backend", _fake_backend)
    monkeypatch.setattr(local_dev, "_wait_for_local_enrollment", _fake_wait_for_enrollment)
    monkeypatch.setattr(local_dev, "_run_runner", _fake_runner)

    def _exit_main_loop(_seconds):
        created["backend"].returncode = 0

    monkeypatch.setattr(local_dev.time, "sleep", _exit_main_loop)

    code = local_dev._run_up(args, env)

    assert code == 0
    assert runner_saw_stale_identity is False
    assert all(not path.exists() for path in stale_paths)


def test_runtime_image_preflight_runs_runner_runtime_info(monkeypatch, capsys) -> None:
    calls = []

    def _fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            4,
            stdout="",
            stderr="runtime-info failed",
        )

    monkeypatch.setattr(local_dev, "_project_python", lambda: "/venv/bin/python")
    monkeypatch.setattr(local_dev.subprocess, "run", _fake_run)

    code = local_dev._run_runtime_image_preflight(
        {"DROWAI_RUNTIME_IMAGE": "stale-runtime"}
    )

    assert code == 4
    assert calls[0][0] == ["/venv/bin/python", "-m", "drowai_runner", "runtime-info"]
    assert calls[0][1]["check"] is False
    output = capsys.readouterr().out
    assert "runtime-info failed" in output
    assert "runtime image preflight failed" in output
    assert "DROWAI_RUNTIME_IMAGE=stale-runtime" in output


def test_runner_command_starts_without_runtime_image_preflight(monkeypatch) -> None:
    """Standalone runner startup also defers image pulls to materialization."""

    def _unexpected_runtime_image_preflight(_env):
        raise AssertionError("runner startup must not require a local runtime image")

    process = _FakeProcess("runner")
    monkeypatch.setattr(local_dev, "_ensure_project_python", lambda: None)
    monkeypatch.setattr(local_dev, "_load_dotenv", lambda: None)
    monkeypatch.setattr(
        local_dev,
        "_parse_args",
        lambda _argv: Namespace(command="runner"),
    )
    monkeypatch.setattr(
        local_dev,
        "_single_host_env",
        lambda _args: {"DROWAI_RUNTIME_IMAGE": "runtime:missing"},
    )
    monkeypatch.setattr(
        local_dev,
        "_run_runtime_image_preflight",
        _unexpected_runtime_image_preflight,
    )
    monkeypatch.setattr(
        local_dev,
        "_bootstrap_install_token",
        lambda _args, _env: "",
    )
    monkeypatch.setattr(
        local_dev,
        "_run_runner",
        lambda _env: process,
    )
    monkeypatch.setattr(
        local_dev,
        "_attach_output_threads",
        lambda _name, _process: [],
    )

    assert local_dev.main(["runner"]) == 0


def test_main_up_checks_postgres_before_starting_stack(monkeypatch) -> None:
    calls: list[str] = []
    env = {
        "DATABASE_URL": "postgresql://drowai_user:secret@localhost:5432/drowai",
        "DROWAI_POSTGRES_ADMIN_URL": "postgresql://admin:secret@localhost/postgres",
    }

    monkeypatch.setattr(local_dev, "_ensure_project_python", lambda: None)
    monkeypatch.setattr(local_dev, "_load_dotenv", lambda: None)
    monkeypatch.setenv(
        "DROWAI_POSTGRES_ADMIN_URL",
        "postgresql://admin:secret@localhost/postgres",
    )
    monkeypatch.setattr(
        local_dev,
        "_parse_args",
        lambda _argv: Namespace(command="up"),
    )
    monkeypatch.setattr(local_dev, "_single_host_env", lambda _args: env)
    monkeypatch.setattr(
        local_dev,
        "ensure_local_postgres_ready",
        lambda actual_env, *, interactive: calls.append(
            f"postgres:{actual_env is env}:{interactive}"
        ),
    )
    monkeypatch.setattr(
        local_dev,
        "_run_up",
        lambda _args, _env: calls.append("up") or 0,
    )
    monkeypatch.setattr(local_dev.sys.stdin, "isatty", lambda: True)

    assert local_dev.main(["up"]) == 0
    assert calls == ["postgres:True:True", "up"]
    assert "DROWAI_POSTGRES_ADMIN_URL" not in env
    assert "DROWAI_POSTGRES_ADMIN_URL" not in local_dev.os.environ


def test_bootstrap_db_command_stops_after_database_readiness(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(local_dev, "_ensure_project_python", lambda: None)
    monkeypatch.setattr(local_dev, "_load_dotenv", lambda: None)
    monkeypatch.setattr(
        local_dev,
        "_parse_args",
        lambda _argv: Namespace(command="bootstrap-db"),
    )
    monkeypatch.setattr(
        local_dev,
        "_single_host_env",
        lambda _args: {"DATABASE_URL": "postgresql://drowai_user:secret@localhost:5432/drowai"},
    )
    monkeypatch.setattr(
        local_dev,
        "ensure_local_postgres_ready",
        lambda _env, *, interactive: calls.append(f"bootstrap:{interactive}"),
    )
    monkeypatch.setattr(local_dev.sys.stdin, "isatty", lambda: True)

    assert local_dev.main(["bootstrap-db"]) == 0
    assert calls == ["bootstrap:True"]


def test_stored_runner_identity_still_sets_runner_tenant_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner_root = tmp_path / "runner"
    credentials_root = runner_root / "credentials"
    credentials_root.mkdir(parents=True)
    secret_path = credentials_root / "runner.secret"
    secret_path.write_text("stored-secret", encoding="utf-8")
    secret_path.with_name("runner.secret.runner_id").write_text("runner-id", encoding="utf-8")

    args = Namespace(runner_install_token=None, skip_token_bootstrap=False)
    env = {"DROWAI_RUNNER_ROOT": str(runner_root)}

    def _fake_ensure(_args, env_payload):
        env_payload["DROWAI_RUNNER_TENANT_ID"] = "123"
        return (123, 456)

    monkeypatch.setattr(local_dev, "_ensure_runner_tenant_env", _fake_ensure)

    token = local_dev._bootstrap_install_token(args, env)

    assert token == ""
    assert env["DROWAI_RUNNER_TENANT_ID"] == "123"


def test_wait_for_connected_runner_uses_postgres_selection_with_stale_capacity_json(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            Task.__table__,
            ExecutionSite.__table__,
            Runner.__table__,
            RunnerCredential.__table__,
            RunnerConnection.__table__,
        ],
    )
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_local()
    now = datetime.now(tz=UTC)

    tenant = Tenant(slug="tenant-startup-gate", name="Tenant Startup Gate")
    user = User(username="owner-startup-gate", password="hashed")
    db.add_all([tenant, user])
    db.flush()

    site = ExecutionSite(tenant_id=tenant.id, name="Site", slug="site", status="active")
    db.add(site)
    db.flush()

    runner = Runner(
        tenant_id=tenant.id,
        execution_site_id=site.id,
        name=f"runner-{uuid_lib.uuid4().hex[:8]}",
        status="active",
        max_active_tasks=1,
        version="1.2.0",
        labels_json={},
        capabilities_json=["docker"],
        capacity_json={"available_tasks": 0, "active_tasks": 999, "max_active_tasks": 999},
        last_seen_at=now,
    )
    db.add(runner)
    db.flush()

    db.add(
        RunnerCredential(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_fingerprint=f"fp-{uuid_lib.uuid4().hex[:8]}",
            secret_hash="sha256$deadbeef",
            status="active",
            revoked_at=None,
            expires_at=now + timedelta(days=1),
        )
    )
    db.add(
        RunnerConnection(
            tenant_id=tenant.id,
            runner_id=runner.id,
            pod_id="pod-a",
            connection_id=f"conn-{uuid_lib.uuid4().hex[:8]}",
            status="active",
            lease_expires_at=now + timedelta(minutes=3),
            last_seen_at=now,
        )
    )
    tenant_id = int(tenant.id)
    user_id = int(user.id)
    db.commit()
    db.close()

    monkeypatch.setattr(local_dev, "_ensure_runner_tenant_env", lambda _args, _env: (tenant_id, user_id))
    monkeypatch.setattr("backend.database.SessionLocal", session_local)
    monkeypatch.setattr(local_dev.time, "sleep", lambda _seconds: None)

    local_dev._wait_for_connected_runner(
        Namespace(tenant_id=tenant_id, user_id=user_id),
        {"DROWAI_RUNNER_ROOT": str(Path.cwd())},
        timeout_seconds=0.5,
    )


def test_run_up_refuses_active_runner_before_starting_services(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    owner = RunnerProcessOwner(
        pid=123,
        create_time=456.0,
        launcher="standalone",
        instance_id="standalone-1",
    )
    env = {"DROWAI_RUNNER_ROOT": str(tmp_path / "runner-root")}

    monkeypatch.setattr(local_dev, "read_active_runner_process_owner", lambda _root: owner)
    monkeypatch.setattr(
        local_dev,
        "_run_schema_migrations",
        lambda _env: (_ for _ in ()).throw(AssertionError("services must not start")),
    )

    assert local_dev._run_up(Namespace(), env) == 1
    assert "standalone runner pid=123" in capsys.readouterr().out


def test_down_stops_orphaned_local_dev_runner_from_lock_owner(
    monkeypatch,
    tmp_path: Path,
) -> None:
    owner = RunnerProcessOwner(
        pid=123,
        create_time=456.0,
        launcher="local-dev",
        instance_id="local-1",
    )
    owners = iter([owner, None])
    stopped: list[tuple[int, int]] = []

    monkeypatch.setattr(local_dev, "read_active_runner_process_owner", lambda _root: next(owners))
    monkeypatch.setattr(local_dev, "process_identity_matches", lambda _pid, _started: True)
    monkeypatch.setattr(
        local_dev,
        "_terminate_process_group",
        lambda *, pid, signal_number: stopped.append((pid, signal_number)),
    )
    monkeypatch.setattr(local_dev.time, "sleep", lambda _seconds: None)

    local_dev._stop_owned_local_runner(
        {"DROWAI_RUNNER_ROOT": str(tmp_path / "runner-root")}
    )

    assert stopped == [(123, signal.SIGTERM)]


def test_down_does_not_stop_standalone_runner_sharing_configured_root(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    owner = RunnerProcessOwner(
        pid=123,
        create_time=456.0,
        launcher="standalone",
        instance_id="standalone-1",
    )
    stopped: list[int] = []

    monkeypatch.setattr(local_dev, "read_active_runner_process_owner", lambda _root: owner)
    monkeypatch.setattr(
        local_dev,
        "_terminate_process_group",
        lambda *, pid, signal_number: stopped.append(pid),
    )

    local_dev._stop_owned_local_runner(
        {"DROWAI_RUNNER_ROOT": str(tmp_path / "runner-root")}
    )

    assert stopped == []
    assert "leaving standalone runner pid=123 running" in capsys.readouterr().out


def test_run_up_marks_runner_with_local_launcher_identity(monkeypatch, tmp_path: Path) -> None:
    args = Namespace(
        no_frontend=True,
        backend_host="127.0.0.1",
        backend_port=8000,
        frontend_host="127.0.0.1",
        frontend_port=5000,
    )
    env = {
        "DROWAI_RUNNER_ROOT": str(tmp_path / "runner-root"),
        "DROWAI_RUNTIME_IMAGE": "runtime:test",
    }
    created: dict[str, _FakeProcess] = {}
    runner_env: dict[str, str] = {}

    monkeypatch.setattr(local_dev, "read_active_runner_process_owner", lambda _root: None)
    monkeypatch.setattr(local_dev, "_run_schema_migrations", lambda _env: None)
    monkeypatch.setattr(local_dev, "_wait_for_backend", lambda _url: None)
    monkeypatch.setattr(local_dev, "_has_setup_identity", lambda _args, _env: True)
    monkeypatch.setattr(local_dev, "_bootstrap_install_token", lambda _args, _env: "")
    monkeypatch.setattr(local_dev, "_write_pid_file", lambda _env, _processes: None)
    monkeypatch.setattr(local_dev, "_remove_pid_file", lambda _env: None)
    monkeypatch.setattr(local_dev, "_attach_output_threads", lambda _name, _process: [])
    monkeypatch.setattr(local_dev, "_wait_for_connected_runner", lambda *_args, **_kwargs: None)

    def _fake_backend(_args, _env):
        created["backend"] = _FakeProcess("backend")
        return created["backend"]

    def _fake_runner(actual_env, *, config_path=None):
        del config_path
        runner_env.update(actual_env)
        created["runner"] = _FakeProcess("runner")
        return created["runner"]

    monkeypatch.setattr(local_dev, "_run_backend", _fake_backend)
    monkeypatch.setattr(local_dev, "_run_runner", _fake_runner)

    def _exit_main_loop(_seconds):
        created["backend"].returncode = 0

    monkeypatch.setattr(local_dev.time, "sleep", _exit_main_loop)

    assert local_dev._run_up(args, env) == 0
    assert runner_env["DROWAI_RUNNER_LAUNCHER"] == "local-dev"
    assert runner_env["DROWAI_RUNNER_INSTANCE_ID"]
    assert runner_env["DROWAI_RUNNER_PARENT_PID"] == str(os.getpid())
    assert float(runner_env["DROWAI_RUNNER_PARENT_CREATE_TIME"]) > 0


def test_recorded_pid_reuse_does_not_stop_unrelated_process(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env = {"DROWAI_RUNNER_ROOT": str(tmp_path / "runner-root")}
    pid_file = tmp_path / "runner-root" / "local-cloud-pids.json"
    pid_file.parent.mkdir(parents=True)
    pid_file.write_text(
        '{"processes":[{"name":"runner","pid":123,"create_time":456.0}]}',
        encoding="utf-8",
    )
    stopped: list[int] = []

    monkeypatch.setattr(local_dev, "process_identity_matches", lambda _pid, _started: False)
    monkeypatch.setattr(local_dev, "_pid_exists", lambda _pid: True)
    monkeypatch.setattr(local_dev.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        local_dev,
        "_terminate_process_group",
        lambda *, pid, signal_number: stopped.append(pid),
    )

    local_dev._stop_recorded_processes(env)

    assert stopped == []


def test_legacy_recorded_runner_is_stopped_after_command_and_cwd_verification(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The pre-create-time PID format remains safe and usable during upgrades."""
    env = {"DROWAI_RUNNER_ROOT": str(tmp_path / "runner-root")}
    pid_file = tmp_path / "runner-root" / "local-cloud-pids.json"
    pid_file.parent.mkdir(parents=True)
    pid_file.write_text(
        '{"processes":[{"name":"runner","pid":123}]}',
        encoding="utf-8",
    )
    process_alive = True
    stopped: list[tuple[int, int]] = []

    class _LegacyRunnerProcess:
        def cmdline(self) -> list[str]:
            return ["/venv/bin/python", "-m", "drowai_runner", "run"]

        def cwd(self) -> str:
            return str(local_dev.REPO_ROOT)

    monkeypatch.setattr(local_dev, "process_create_time", lambda _pid: 456.0)
    monkeypatch.setattr(
        local_dev,
        "process_identity_matches",
        lambda _pid, _started: process_alive,
    )
    monkeypatch.setattr(local_dev.psutil, "Process", lambda _pid: _LegacyRunnerProcess())
    monkeypatch.setattr(local_dev.time, "sleep", lambda _seconds: None)

    def _stop(*, pid: int, signal_number: int) -> None:
        nonlocal process_alive
        stopped.append((pid, signal_number))
        process_alive = False

    monkeypatch.setattr(local_dev, "_terminate_process_group", _stop)

    local_dev._stop_recorded_processes(env)

    assert stopped == [(123, signal.SIGTERM)]


def test_legacy_recorded_pid_does_not_stop_unrelated_process(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Legacy compatibility still requires the recorded launcher command."""
    env = {"DROWAI_RUNNER_ROOT": str(tmp_path / "runner-root")}
    pid_file = tmp_path / "runner-root" / "local-cloud-pids.json"
    pid_file.parent.mkdir(parents=True)
    pid_file.write_text(
        '{"processes":[{"name":"runner","pid":123}]}',
        encoding="utf-8",
    )
    stopped: list[int] = []

    class _UnrelatedProcess:
        def cmdline(self) -> list[str]:
            return ["python", "unrelated_service.py"]

        def cwd(self) -> str:
            return str(local_dev.REPO_ROOT)

    monkeypatch.setattr(local_dev, "process_create_time", lambda _pid: 456.0)
    monkeypatch.setattr(local_dev, "process_identity_matches", lambda _pid, _started: True)
    monkeypatch.setattr(local_dev.psutil, "Process", lambda _pid: _UnrelatedProcess())
    monkeypatch.setattr(local_dev, "_pid_exists", lambda _pid: True)
    monkeypatch.setattr(local_dev.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        local_dev,
        "_terminate_process_group",
        lambda *, pid, signal_number: stopped.append(pid),
    )

    local_dev._stop_recorded_processes(env)

    assert stopped == []


def test_port_fallback_does_not_stop_unrelated_listener(monkeypatch) -> None:
    stopped: list[int] = []

    monkeypatch.setattr(local_dev, "_pids_listening_on_port", lambda _port: [123])
    monkeypatch.setattr(local_dev, "process_create_time", lambda _pid: 456.0)
    monkeypatch.setattr(local_dev, "_is_expected_local_process", lambda _record: False)
    monkeypatch.setattr(local_dev.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        local_dev,
        "_terminate_process_group",
        lambda *, pid, signal_number: stopped.append(pid),
    )

    local_dev._stop_port_listeners(port_owners=((8000, "backend"),))

    assert stopped == []
