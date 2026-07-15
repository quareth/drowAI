"""Tests for runner heartbeat payload composition helpers."""

from __future__ import annotations

from pathlib import Path

from drowai_runner.config import RunnerConfig
from drowai_runner.heartbeat import build_runner_heartbeat_payload
from runtime_shared.runtime_image_contract import default_runtime_image_for_machine


def _cloud_config(tmp_path: Path) -> RunnerConfig:
    return RunnerConfig.from_env(
        {
            "DROWAI_RUNNER_ROOT": str(tmp_path / "runner-root"),
            "DROWAI_RUNNER_CLOUD_BASE_URL": "http://cloud.example.test",
            "DROWAI_RUNNER_ALLOW_INSECURE_CLOUD_ENDPOINT": "true",
            "DROWAI_RUNTIME_IMAGE": default_runtime_image_for_machine(),
            "DROWAI_RUNNER_MAX_ACTIVE_TASKS": "3",
            "DROWAI_RUNNER_MAX_PARALLEL_COMMANDS_PER_TASK": "5",
            "DROWAI_RUNNER_LABELS": '{"site":"hq"}',
            "DROWAI_RUNNER_CAPABILITIES": '["docker","file_comm"]',
        }
    )


def test_build_runner_heartbeat_payload_includes_expected_capacity_fields(tmp_path: Path) -> None:
    config = _cloud_config(tmp_path)

    payload = build_runner_heartbeat_payload(
        config=config,
        runner_version="1.2.3",
        active_tasks=1,
    )

    assert payload.capacity.active_tasks == 1
    assert payload.capacity.max_active_tasks == 3
    assert payload.capacity.available_tasks == 2
    assert payload.capacity.max_parallel_commands_per_task == 5
    assert payload.capacity.runtime_image == default_runtime_image_for_machine()
    assert payload.capacity.version == "1.2.3"
    assert payload.capacity.capabilities == ("docker", "file_comm")
    assert dict(payload.capacity.labels) == {"site": "hq"}


def test_build_runner_heartbeat_payload_limits_metadata_sizes(tmp_path: Path) -> None:
    config = RunnerConfig.from_env(
        {
            "DROWAI_RUNNER_ROOT": str(tmp_path / "runner-root"),
            "DROWAI_RUNNER_CLOUD_BASE_URL": "http://cloud.example.test",
            "DROWAI_RUNNER_ALLOW_INSECURE_CLOUD_ENDPOINT": "true",
            "DROWAI_RUNNER_MAX_ACTIVE_TASKS": "1",
            "DROWAI_RUNNER_CAPABILITIES": "[" + ",".join('"c"' for _ in range(40)) + "]",
            "DROWAI_RUNNER_LABELS": "{" + ",".join(f'\"k{i}\":\"v\"' for i in range(40)) + "}",
        }
    )

    payload = build_runner_heartbeat_payload(
        config=config,
        runner_version="1.2.3",
        active_tasks=0,
    )

    assert len(payload.capacity.capabilities) <= 32
    assert len(payload.capacity.labels) <= 32
