"""Retention policy defaults and runtime rollout configuration.

This module owns named MVP retention defaults, shared validation bounds, and
typed rollout flags so schemas, services, and executors do not embed raw
retention literals or parse retention environment values directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, Mapping, cast


RetentionRolloutStage = Literal["off", "internal", "beta", "ga"]


MIN_RETENTION_DAYS: Final[int] = 1
MAX_RETENTION_DAYS: Final[int] = 3650
MIN_RETENTION_BATCH_SIZE_PER_TENANT: Final[int] = 1
MAX_RETENTION_BATCH_SIZE_PER_TENANT: Final[int] = 10_000

DEFAULT_REPORT_RETENTION_ENABLED: Final[bool] = True
DEFAULT_OPERATIONAL_LOG_RETENTION_DAYS: Final[int] = 30
DEFAULT_RUNNER_CONTROL_RETENTION_DAYS: Final[int] = 30
DEFAULT_CHECKPOINT_RETENTION_DAYS_AFTER_TERMINAL: Final[int] = 30
DEFAULT_TASK_RETENTION_DAYS_AFTER_TERMINAL: Final[int] = 180
DEFAULT_CHAT_TRANSCRIPT_RETENTION_DAYS_AFTER_TERMINAL: Final[int] = 90
DEFAULT_ARTIFACT_PAYLOAD_RETENTION_DAYS: Final[int] = 90
DEFAULT_ARTIFACT_METADATA_RETENTION_DAYS_AFTER_TERMINAL: Final[int] = 180
DEFAULT_REPORT_HISTORY_RETENTION_DAYS: Final[int] = 180
DEFAULT_REPORT_JOB_RETENTION_DAYS: Final[int] = 90
DEFAULT_TASK_MEMO_HISTORY_RETENTION_DAYS: Final[int] = 180
DEFAULT_SEMANTIC_MEMORY_STALE_RETENTION_DAYS: Final[int] = 365
DEFAULT_USAGE_RECORD_RETENTION_DAYS: Final[int] = 365
DEFAULT_RETENTION_BATCH_SIZE_PER_TENANT: Final[int] = 100

DEFAULT_RETENTION_ORCHESTRATOR_ENABLED: Final[bool] = False
DEFAULT_RETENTION_DRY_RUN_ONLY: Final[bool] = True
DEFAULT_RETENTION_ROLLOUT_STAGE: Final[RetentionRolloutStage] = "off"

RETENTION_POLICY_DEFAULTS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "operational_log_retention_days": DEFAULT_OPERATIONAL_LOG_RETENTION_DAYS,
        "runner_control_retention_days": DEFAULT_RUNNER_CONTROL_RETENTION_DAYS,
        "checkpoint_retention_days_after_terminal": (
            DEFAULT_CHECKPOINT_RETENTION_DAYS_AFTER_TERMINAL
        ),
        "task_retention_days_after_terminal": DEFAULT_TASK_RETENTION_DAYS_AFTER_TERMINAL,
        "chat_transcript_retention_days_after_terminal": (
            DEFAULT_CHAT_TRANSCRIPT_RETENTION_DAYS_AFTER_TERMINAL
        ),
        "artifact_payload_retention_days": DEFAULT_ARTIFACT_PAYLOAD_RETENTION_DAYS,
        "artifact_metadata_retention_days_after_terminal": (
            DEFAULT_ARTIFACT_METADATA_RETENTION_DAYS_AFTER_TERMINAL
        ),
        "report_history_retention_days": DEFAULT_REPORT_HISTORY_RETENTION_DAYS,
        "report_job_retention_days": DEFAULT_REPORT_JOB_RETENTION_DAYS,
        "task_memo_history_retention_days": DEFAULT_TASK_MEMO_HISTORY_RETENTION_DAYS,
        "semantic_memory_stale_retention_days": (
            DEFAULT_SEMANTIC_MEMORY_STALE_RETENTION_DAYS
        ),
        "usage_record_retention_days": DEFAULT_USAGE_RECORD_RETENTION_DAYS,
        "retention_batch_size_per_tenant": DEFAULT_RETENTION_BATCH_SIZE_PER_TENANT,
    }
)

RETENTION_DAY_FIELD_BOUNDS: Final[tuple[int, int]] = (
    MIN_RETENTION_DAYS,
    MAX_RETENTION_DAYS,
)
RETENTION_BATCH_SIZE_PER_TENANT_BOUNDS: Final[tuple[int, int]] = (
    MIN_RETENTION_BATCH_SIZE_PER_TENANT,
    MAX_RETENTION_BATCH_SIZE_PER_TENANT,
)


@dataclass(frozen=True)
class RetentionRuntimeConfig:
    """Typed runtime flags for staged retention rollout."""

    orchestrator_enabled: bool
    dry_run_only: bool
    rollout_stage: RetentionRolloutStage


def get_retention_runtime_config(
    environ: Mapping[str, str] | None = None,
) -> RetentionRuntimeConfig:
    """Return typed retention runtime rollout flags from the environment."""

    env = environ if environ is not None else os.environ
    return RetentionRuntimeConfig(
        orchestrator_enabled=_read_bool(
            env,
            "RETENTION_ORCHESTRATOR_ENABLED",
            DEFAULT_RETENTION_ORCHESTRATOR_ENABLED,
        ),
        dry_run_only=_read_bool(
            env,
            "RETENTION_DRY_RUN_ONLY",
            DEFAULT_RETENTION_DRY_RUN_ONLY,
        ),
        rollout_stage=_read_rollout_stage(
            env,
            "RETENTION_ROLLOUT_STAGE",
            DEFAULT_RETENTION_ROLLOUT_STAGE,
        ),
    )


def _read_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _read_rollout_stage(
    env: Mapping[str, str],
    name: str,
    default: RetentionRolloutStage,
) -> RetentionRolloutStage:
    raw = env.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"off", "internal", "beta", "ga"}:
        return cast(RetentionRolloutStage, normalized)
    return default


__all__ = [
    "DEFAULT_ARTIFACT_METADATA_RETENTION_DAYS_AFTER_TERMINAL",
    "DEFAULT_ARTIFACT_PAYLOAD_RETENTION_DAYS",
    "DEFAULT_CHAT_TRANSCRIPT_RETENTION_DAYS_AFTER_TERMINAL",
    "DEFAULT_CHECKPOINT_RETENTION_DAYS_AFTER_TERMINAL",
    "DEFAULT_OPERATIONAL_LOG_RETENTION_DAYS",
    "DEFAULT_REPORT_HISTORY_RETENTION_DAYS",
    "DEFAULT_REPORT_JOB_RETENTION_DAYS",
    "DEFAULT_REPORT_RETENTION_ENABLED",
    "DEFAULT_RETENTION_BATCH_SIZE_PER_TENANT",
    "DEFAULT_RETENTION_DRY_RUN_ONLY",
    "DEFAULT_RETENTION_ORCHESTRATOR_ENABLED",
    "DEFAULT_RETENTION_ROLLOUT_STAGE",
    "DEFAULT_RUNNER_CONTROL_RETENTION_DAYS",
    "DEFAULT_SEMANTIC_MEMORY_STALE_RETENTION_DAYS",
    "DEFAULT_TASK_MEMO_HISTORY_RETENTION_DAYS",
    "DEFAULT_TASK_RETENTION_DAYS_AFTER_TERMINAL",
    "DEFAULT_USAGE_RECORD_RETENTION_DAYS",
    "MAX_RETENTION_BATCH_SIZE_PER_TENANT",
    "MAX_RETENTION_DAYS",
    "MIN_RETENTION_BATCH_SIZE_PER_TENANT",
    "MIN_RETENTION_DAYS",
    "RETENTION_BATCH_SIZE_PER_TENANT_BOUNDS",
    "RETENTION_DAY_FIELD_BOUNDS",
    "RETENTION_POLICY_DEFAULTS",
    "RetentionRolloutStage",
    "RetentionRuntimeConfig",
    "get_retention_runtime_config",
]
