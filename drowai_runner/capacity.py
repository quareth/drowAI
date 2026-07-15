"""Runner-side capacity snapshot construction for cloud heartbeat messages.

This module builds a bounded, schema-compatible capacity payload without
importing backend modules so the runner can report self-observed metadata to
the cloud control plane.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from drowai_runner.config import RunnerConfig
from runtime_shared.runner_protocol import (
    RUNNER_CAPABILITIES_MAX_ITEMS,
    RUNNER_CAPABILITY_MAX_LENGTH,
    RUNNER_LABELS_MAX_ITEMS,
    RUNNER_LABEL_KEY_MAX_LENGTH,
    RUNNER_LABEL_VALUE_MAX_LENGTH,
    RunnerActiveRuntimeJobPayload,
    RunnerCapacityPayload,
)


def build_runner_capacity_payload(
    *,
    config: RunnerConfig,
    runner_version: str,
    active_tasks: int,
    docker_available: bool,
    runtime_image_available: bool,
    active_runtime_jobs: Sequence[RunnerActiveRuntimeJobPayload] = (),
) -> RunnerCapacityPayload:
    """Build a bounded runner capacity payload for heartbeat and capacity events."""
    # Advisory only: control-plane admission/capacity gating is Postgres-sourced.
    # Heartbeat capacity remains useful for observability and runner diagnostics.
    max_active_tasks = max(0, int(config.max_active_tasks))
    bounded_active_tasks = max(0, min(int(active_tasks), max_active_tasks))
    available_tasks = max(0, max_active_tasks - bounded_active_tasks)
    max_parallel_commands = max(1, int(config.max_parallel_commands_per_task))

    return RunnerCapacityPayload(
        active_tasks=bounded_active_tasks,
        max_active_tasks=max_active_tasks,
        available_tasks=available_tasks,
        max_parallel_commands_per_task=max_parallel_commands,
        docker_available=bool(docker_available),
        runtime_image=(config.runtime_image_tag or "unknown").strip() or "unknown",
        runtime_image_available=bool(runtime_image_available),
        version=(runner_version or "unknown").strip() or "unknown",
        capabilities=_normalize_capabilities(config.capabilities),
        labels=_normalize_labels(config.labels),
        active_runtime_jobs=tuple(active_runtime_jobs),
    )


def _normalize_capabilities(values: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw in values:
        value = str(raw).strip()
        if not value:
            continue
        normalized.append(value[:RUNNER_CAPABILITY_MAX_LENGTH])
        if len(normalized) >= RUNNER_CAPABILITIES_MAX_ITEMS:
            break
    return tuple(normalized)


def _normalize_labels(labels: Mapping[str, str] | None) -> dict[str, str]:
    normalized: dict[str, str] = {}
    if labels is None:
        return normalized
    for raw_key, raw_value in labels.items():
        key = str(raw_key).strip()[:RUNNER_LABEL_KEY_MAX_LENGTH]
        if not key:
            continue
        normalized[key] = str(raw_value).strip()[:RUNNER_LABEL_VALUE_MAX_LENGTH]
        if len(normalized) >= RUNNER_LABELS_MAX_ITEMS:
            break
    return normalized
