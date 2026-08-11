"""Tests for managed-runner process ownership and parent supervision."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import threading

import pytest

from drowai_runner import process_lock
from drowai_runner.control_channel.errors import RunnerCloudClientError


def test_runner_process_lock_publishes_active_owner(tmp_path: Path) -> None:
    runner_root = tmp_path / "runner-root"

    with process_lock.runner_process_lock(
        runner_root,
        launcher="local-dev",
        instance_id="instance-123",
    ):
        owner = process_lock.read_active_runner_process_owner(runner_root)

        assert owner is not None
        assert owner.pid == os.getpid()
        assert owner.launcher == "local-dev"
        assert owner.instance_id == "instance-123"
        assert process_lock.process_identity_matches(owner.pid, owner.create_time)

    assert process_lock.read_active_runner_process_owner(runner_root) is None


def test_parent_monitor_requests_shutdown_when_launcher_disappears(
    monkeypatch,
) -> None:
    stop_event = threading.Event()
    signals: list[tuple[int, int]] = []

    monkeypatch.setattr(process_lock, "process_identity_matches", lambda _pid, _started: False)
    monkeypatch.setattr(process_lock.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    process_lock._monitor_parent_process(
        parent_pid=123,
        parent_create_time=456.0,
        stop_event=stop_event,
        poll_interval_seconds=0,
    )

    assert signals == [(os.getpid(), signal.SIGTERM)]


def test_parent_watchdog_is_disabled_without_local_launcher_metadata(
    monkeypatch,
) -> None:
    started = False

    def _unexpected_start(self) -> None:
        nonlocal started
        started = True

    monkeypatch.setattr(threading.Thread, "start", _unexpected_start)

    with process_lock.parent_process_watchdog({}):
        pass

    assert started is False


def test_local_parent_watchdog_rejects_missing_parent_identity() -> None:
    with pytest.raises(RunnerCloudClientError) as exc_info:
        with process_lock.parent_process_watchdog(
            {process_lock.RUNNER_LAUNCHER_ENV: process_lock.LOCAL_DEV_LAUNCHER}
        ):
            pass

    assert exc_info.value.error_code == "RUNNER_PARENT_IDENTITY_INVALID"
