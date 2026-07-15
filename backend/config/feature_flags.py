"""
Feature flag helpers for backend runtime behavior.

This module keeps env-var parsing for feature toggles in one place so services
can gate optional behavior consistently.
"""

from __future__ import annotations

import os
from typing import Literal

from backend.config.data_plane import get_data_plane_config


ChatTranscriptReadMode = Literal["legacy", "shadow", "canonical"]
DeploymentProfileMode = Literal["dev_local", "single_host", "distributed"]


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return float(default)
    try:
        return float(raw_value.strip())
    except (TypeError, ValueError):
        return float(default)


def _env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return int(default)
    try:
        return int(raw_value.strip())
    except (TypeError, ValueError):
        return int(default)


def _env_optional_non_negative_int(name: str) -> int | None:
    """Return a non-negative integer env value or ``None`` when unset."""
    raw_value = os.getenv(name)
    if raw_value is None:
        return None
    try:
        parsed = int(raw_value.strip())
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {name} value: expected an integer or unset.")
    if parsed < 0:
        raise ValueError(f"Invalid {name} value: expected a non-negative integer or unset.")
    return parsed


def get_task_max_concurrent_per_tenant_default() -> int | None:
    """Return the global tenant quota fallback from environment."""
    return _env_optional_non_negative_int("TASK_MAX_CONCURRENT_PER_TENANT")


def get_task_max_concurrent_per_user_default() -> int | None:
    """Return the global per-user quota fallback from environment."""
    return _env_optional_non_negative_int("TASK_MAX_CONCURRENT_PER_USER")


def get_local_max_active_tasks_default() -> int | None:
    """Return the global runner capacity fallback from environment."""
    return _env_optional_non_negative_int("LOCAL_MAX_ACTIVE_TASKS")


def resolve_task_concurrency_limit(
    *,
    row_limit: int | None,
    tenant_default_limit: int | None = None,
    global_default_limit: int | None = None,
) -> int | None:
    """Resolve task concurrency limit using row, tenant, then global defaults.

    Resolution order: row-level override, tenant-level default, global config
    default, then unlimited (``None``).
    """
    for candidate in (row_limit, tenant_default_limit, global_default_limit):
        if candidate is None:
            continue
        if candidate < 0:
            raise ValueError(
                "Invalid task concurrency limit value: expected a non-negative integer or None."
            )
        return int(candidate)
    return None


def is_artifact_provenance_enabled() -> bool:
    """Return True; artifact provenance persistence is always enabled."""
    return True


def is_semantic_memory_runtime_enabled() -> bool:
    """Return True when embedding-backed semantic memory runtime paths are enabled."""
    return _env_bool("ENABLE_SEMANTIC_MEMORY_RUNTIME", default=False)


def is_knowledge_candidate_extraction_enabled() -> bool:
    """Return True when LLM candidate extraction is enabled."""
    return _env_bool("ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION", default=False)


def is_knowledge_cve_lookup_enabled() -> bool:
    """Return True when `knowledge.cve_lookup` runtime availability is enabled."""
    return _env_bool("ENABLE_KNOWLEDGE_CVE_LOOKUP", default=True)


def is_knowledge_vulnerability_candidates_enabled() -> bool:
    """Return True when vulnerability candidate extraction behavior is enabled."""
    return _env_bool("ENABLE_KNOWLEDGE_VULNERABILITY_CANDIDATES", default=True)


def get_knowledge_candidate_max_prompt_tokens() -> int:
    """Return the candidate extraction prompt-token budget."""
    default = 200_000
    value = _env_int("KNOWLEDGE_CANDIDATE_MAX_PROMPT_TOKENS", default=default)
    return value if value > 0 else default


def get_knowledge_candidate_max_cost_usd() -> float:
    """Return the candidate extraction cost budget in USD."""
    default = 10.0
    value = _env_float("KNOWLEDGE_CANDIDATE_MAX_COST_USD", default=default)
    return value if value > 0 else default


def get_knowledge_vulnerability_min_confidence() -> float:
    """Return vulnerability candidate confidence threshold within [0.0, 1.0]."""
    default = 0.80
    value = _env_float("KNOWLEDGE_VULNERABILITY_MIN_CONFIDENCE", default=default)
    if value < 0.0 or value > 1.0:
        return default
    return value


def get_chat_transcript_read_mode() -> ChatTranscriptReadMode:
    """
    Return transcript read-mode rollout flag.

    Modes:
    - legacy: pre-cutover detail mapper remains user-facing.
    - shadow: compute canonical rows for parity metrics, but keep legacy output.
    - canonical: canonical turn-event rows are authoritative.
    """
    raw = str(os.getenv("CHAT_TRANSCRIPT_READ_MODE", "canonical") or "").strip().lower()
    if raw in {"legacy", "shadow", "canonical"}:
        return raw  # type: ignore[return-value]
    return "canonical"


def get_default_task_runtime_placement_mode() -> str:
    """Return default runtime placement mode for task execution provider selection."""
    default_mode = "local"
    raw = str(os.getenv("TASK_RUNTIME_PLACEMENT_MODE_DEFAULT", default_mode) or "").strip().lower()
    if raw in {"local", "runner"}:
        return raw
    raise ValueError(
        "Invalid TASK_RUNTIME_PLACEMENT_MODE_DEFAULT value: "
        f"`{raw}`. Expected one of: `local`, `runner`."
    )


def get_deployment_profile() -> DeploymentProfileMode:
    """Return typed deployment profile mode for product runtime validation."""
    raw_profile = str(os.getenv("DROWAI_DEPLOYMENT_PROFILE", "dev_local") or "").strip().lower()
    if raw_profile in {"dev_local", "single_host", "distributed"}:
        return raw_profile  # type: ignore[return-value]
    raise ValueError(
        "Invalid DROWAI_DEPLOYMENT_PROFILE value: "
        f"`{raw_profile}`. Expected one of: `dev_local`, `single_host`, `distributed`."
    )


def is_cloud_runner_control_enabled() -> bool:
    """Return True when runner control plane readiness is required."""
    return _env_bool("ENABLE_CLOUD_RUNNER_CONTROL", default=False)


def is_runner_tool_command_enabled() -> bool:
    """Return True when tooling plane runner `tool.command` dispatch is enabled."""
    return _env_bool("RUNNER_TOOL_COMMAND_ENABLED", default=False)


def get_object_store_backend() -> str:
    """Return configured data-plane object-store backend identifier."""
    return get_data_plane_config().object_store_backend
