"""Own runner-root process locking and optional launcher supervision metadata."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import threading
from uuid import uuid4

import psutil

from drowai_runner.control_channel.errors import RunnerCloudClientError

RUNNER_LAUNCHER_ENV = "DROWAI_RUNNER_LAUNCHER"
RUNNER_INSTANCE_ID_ENV = "DROWAI_RUNNER_INSTANCE_ID"
RUNNER_PARENT_PID_ENV = "DROWAI_RUNNER_PARENT_PID"
RUNNER_PARENT_CREATE_TIME_ENV = "DROWAI_RUNNER_PARENT_CREATE_TIME"
LOCAL_DEV_LAUNCHER = "local-dev"
_LOCK_RELATIVE_PATH = Path("control") / "runner-process.lock"
_LOCK_SCHEMA_VERSION = 1
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunnerProcessOwner:
    """Identity published by the process holding one runner-root lock."""

    pid: int
    create_time: float
    launcher: str
    instance_id: str


def process_create_time(pid: int) -> float | None:
    """Return a process start timestamp, or ``None`` when it cannot be verified."""
    try:
        return float(psutil.Process(pid).create_time())
    except (psutil.Error, ValueError):
        return None


def process_identity_matches(pid: int, create_time: float) -> bool:
    """Return whether PID still names the exact process that published the timestamp."""
    observed = process_create_time(pid)
    return observed is not None and abs(observed - create_time) < 0.01


def _lock_path(runner_root: Path) -> Path:
    return runner_root / _LOCK_RELATIVE_PATH


def _serialize_owner(owner: RunnerProcessOwner) -> str:
    return json.dumps(
        {
            "schema_version": _LOCK_SCHEMA_VERSION,
            "pid": owner.pid,
            "create_time": owner.create_time,
            "launcher": owner.launcher,
            "instance_id": owner.instance_id,
        },
        sort_keys=True,
    )


def _parse_owner(raw_value: str) -> RunnerProcessOwner | None:
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        try:
            pid = int(payload["pid"])
            create_time = float(payload["create_time"])
        except (KeyError, TypeError, ValueError):
            return None
        if pid <= 0 or create_time <= 0:
            return None
        return RunnerProcessOwner(
            pid=pid,
            create_time=create_time,
            launcher=str(payload.get("launcher") or "standalone"),
            instance_id=str(payload.get("instance_id") or ""),
        )

    try:
        legacy_pid = int(raw_value.strip())
    except ValueError:
        return None
    create_time = process_create_time(legacy_pid)
    if legacy_pid <= 0 or create_time is None:
        return None
    return RunnerProcessOwner(
        pid=legacy_pid,
        create_time=create_time,
        launcher="legacy",
        instance_id="",
    )


@contextmanager
def runner_process_lock(
    runner_root: Path,
    *,
    launcher: str | None = None,
    instance_id: str | None = None,
) -> Iterator[None]:
    """Exclusively own a runner root and publish verifiable process metadata."""
    lock_path = _lock_path(runner_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+", encoding="utf-8")
    acquired = False
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            raise RunnerCloudClientError(
                error_code="RUNNER_ALREADY_RUNNING",
                message=f"Another runner process already owns runner_root `{runner_root}`.",
            ) from exc

        create_time = process_create_time(os.getpid())
        if create_time is None:
            raise RunnerCloudClientError(
                error_code="RUNNER_PROCESS_IDENTITY_UNAVAILABLE",
                message="Runner process start identity could not be verified.",
            )
        resolved_launcher = (
            launcher or os.getenv(RUNNER_LAUNCHER_ENV) or "standalone"
        ).strip() or "standalone"
        resolved_instance_id = (
            instance_id or os.getenv(RUNNER_INSTANCE_ID_ENV) or uuid4().hex
        ).strip() or uuid4().hex
        owner = RunnerProcessOwner(
            pid=os.getpid(),
            create_time=create_time,
            launcher=resolved_launcher,
            instance_id=resolved_instance_id,
        )
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(_serialize_owner(owner))
        lock_file.flush()
        yield
    finally:
        if acquired:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def read_active_runner_process_owner(runner_root: Path) -> RunnerProcessOwner | None:
    """Return the verified metadata for the active lock holder, if one exists."""
    lock_path = _lock_path(runner_root)
    try:
        lock_file = lock_path.open("r+", encoding="utf-8")
    except FileNotFoundError:
        return None
    with lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.seek(0)
            return _parse_owner(lock_file.read())
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            return None


def _monitor_parent_process(
    *,
    parent_pid: int,
    parent_create_time: float,
    stop_event: threading.Event,
    poll_interval_seconds: float = 0.5,
) -> None:
    while process_identity_matches(parent_pid, parent_create_time):
        if stop_event.wait(poll_interval_seconds):
            return
    if stop_event.is_set():
        return
    logger.warning("runner.local_dev_parent_exited parent_pid=%s", parent_pid)
    os.kill(os.getpid(), signal.SIGTERM)


@contextmanager
def parent_process_watchdog(env: Mapping[str, str] | None = None) -> Iterator[None]:
    """Stop a local-dev runner when its exact launcher process disappears."""
    values = os.environ if env is None else env
    if values.get(RUNNER_LAUNCHER_ENV) != LOCAL_DEV_LAUNCHER:
        yield
        return
    try:
        parent_pid = int(values[RUNNER_PARENT_PID_ENV])
        parent_create_time = float(values[RUNNER_PARENT_CREATE_TIME_ENV])
    except (KeyError, TypeError, ValueError):
        raise RunnerCloudClientError(
            error_code="RUNNER_PARENT_IDENTITY_INVALID",
            message="Local-dev runner requires a valid launcher process identity.",
        ) from None
    if parent_pid <= 0 or parent_pid == os.getpid() or parent_create_time <= 0:
        raise RunnerCloudClientError(
            error_code="RUNNER_PARENT_IDENTITY_INVALID",
            message="Local-dev runner requires a valid launcher process identity.",
        )

    stop_event = threading.Event()
    thread = threading.Thread(
        target=_monitor_parent_process,
        kwargs={
            "parent_pid": parent_pid,
            "parent_create_time": parent_create_time,
            "stop_event": stop_event,
        },
        name="runner-parent-watchdog",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=1)
