"""Normalize runtime-provider snapshot payloads for compatibility routes/streams.

Responsibilities:
- Project runner snapshot wrapper payloads to legacy client-facing shapes.
- Preserve local-provider payloads while providing deterministic fallback defaults.
"""

from __future__ import annotations

import math
import re
from typing import Any, Mapping

from backend.core.time_utils import format_iso, utc_now


_MISSING_STATUS_VALUES = frozenset({"", "missing", "not_found", "none"})
_PATH_KEY_SUFFIX = "_path"
_WINDOWS_DRIVE_PATH_RE = re.compile(r"^[a-zA-Z]:[\\/]")
_BYTES_PER_MIB = 1024 * 1024


def normalize_runtime_logs_snapshot(delegate_result: object) -> list[Any]:
    """Return a list-shaped log snapshot from local or runner delegate results."""
    if isinstance(delegate_result, list):
        return list(delegate_result)
    if not isinstance(delegate_result, Mapping):
        return []
    logs = delegate_result.get("logs")
    if isinstance(logs, list):
        return list(logs)
    if isinstance(logs, str):
        return [{"message": line} for line in logs.splitlines() if line.strip()]
    return []


def normalize_runtime_metrics_snapshot(delegate_result: object) -> dict[str, Any] | None:
    """Return flat metrics mapping from local or runner delegate results."""
    if not isinstance(delegate_result, Mapping):
        return None
    nested_metrics = delegate_result.get("metrics")
    if isinstance(nested_metrics, Mapping):
        return _normalize_metrics_mapping(nested_metrics)
    if not delegate_result:
        return None
    return _normalize_metrics_mapping(delegate_result)


def normalize_runtime_startup_progress_snapshot(delegate_result: object) -> dict[str, Any] | None:
    """Return startup progress mapping with a stable `container_exists` projection."""
    if not isinstance(delegate_result, Mapping):
        return None
    progress = _sanitize_metadata_mapping(delegate_result)
    if "container_exists" in progress:
        return progress

    raw_container_status = str(progress.get("container_status") or "").strip().lower()
    raw_job_status = str(progress.get("job_status") or "").strip().lower()
    progress["container_exists"] = raw_container_status not in _MISSING_STATUS_VALUES
    if raw_job_status == "running" or raw_container_status == "running":
        progress.setdefault("status", "running")
        progress.setdefault("message", "Runtime is now running. Streaming logs...")
    elif progress.get("startup_phase") == "container_starting":
        progress.setdefault("status", "starting")
        progress.setdefault("message", "Runtime is starting...")
    else:
        progress.setdefault("status", "starting")
        progress.setdefault("message", "Runtime startup pending")
    return progress


def normalize_runtime_status_snapshot(delegate_result: object) -> tuple[bool, str, dict[str, Any]] | None:
    """Return `(container_exists, status, details)` from local or runner status snapshots."""
    if isinstance(delegate_result, tuple) and len(delegate_result) == 3:
        exists, status, details = delegate_result
        sanitized_details = (
            _sanitize_metadata_mapping(details) if isinstance(details, Mapping) else {}
        )
        return bool(exists), str(status), sanitized_details
    if isinstance(delegate_result, str):
        lowered = delegate_result.strip().lower()
        return lowered not in _MISSING_STATUS_VALUES, delegate_result, {}
    if not isinstance(delegate_result, Mapping):
        return None

    raw_container_status = str(delegate_result.get("container_status") or "").strip()
    if raw_container_status:
        status_text = raw_container_status
    else:
        status_text = str(
            delegate_result.get("status")
            or delegate_result.get("job_status")
            or "unknown"
        ).strip()
    exists = raw_container_status.lower() not in _MISSING_STATUS_VALUES if raw_container_status else False
    return exists, status_text or "unknown", _sanitize_metadata_mapping(delegate_result)


def _sanitize_metadata_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Return metadata mapping with absolute host path fields removed."""
    sanitized: dict[str, Any] = {}
    for raw_key, value in mapping.items():
        key = str(raw_key)
        if isinstance(value, Mapping):
            sanitized[key] = _sanitize_metadata_mapping(value)
            continue
        if isinstance(value, list):
            sanitized[key] = [_sanitize_list_item(item) for item in value]
            continue
        if isinstance(value, str) and _is_sensitive_absolute_path(key=key, value=value):
            continue
        sanitized[key] = value
    return sanitized


def _normalize_metrics_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Return a client-facing metrics mapping from provider-native payloads."""
    metrics = _sanitize_metadata_mapping(mapping)

    memory_usage_mb = _resolve_mb_metric(
        metrics,
        mb_key="memory_usage_mb",
        bytes_key="memory_usage",
    )
    memory_limit_mb = _resolve_mb_metric(
        metrics,
        mb_key="memory_limit_mb",
        bytes_key="memory_limit",
    )
    metrics["memory_usage_mb"] = memory_usage_mb
    metrics["memory_limit_mb"] = memory_limit_mb

    memory_percent = _coerce_nonnegative_float(metrics.get("memory_percent"))
    if memory_percent is None:
        memory_percent = (
            round((memory_usage_mb / memory_limit_mb) * 100.0, 2)
            if memory_limit_mb > 0
            else 0.0
        )
    metrics["memory_percent"] = memory_percent

    cpu_percent = _coerce_nonnegative_float(metrics.get("cpu_percent"))
    metrics["cpu_percent"] = cpu_percent if cpu_percent is not None else 0.0
    metrics["storage"] = _normalize_storage_stats(metrics.get("storage"))
    metrics["network"] = _normalize_network_stats(metrics.get("network"))
    metrics.setdefault("timestamp", format_iso(utc_now()))
    return metrics


def _resolve_mb_metric(
    metrics: Mapping[str, Any],
    *,
    mb_key: str,
    bytes_key: str,
) -> float:
    mb_value = _coerce_nonnegative_float(metrics.get(mb_key))
    if mb_value is not None:
        return mb_value
    byte_value = _coerce_nonnegative_float(metrics.get(bytes_key))
    if byte_value is not None:
        return round(byte_value / _BYTES_PER_MIB, 2)
    return 0.0


def _normalize_storage_stats(value: Any) -> dict[str, Any]:
    storage = _sanitize_metadata_mapping(value) if isinstance(value, Mapping) else {}
    used_bytes = _coerce_nonnegative_float(storage.get("used_bytes"))
    size_root_fs = _coerce_nonnegative_float(storage.get("size_root_fs"))
    used_mb = _coerce_nonnegative_float(storage.get("used_mb"))
    used_gb = _coerce_nonnegative_float(storage.get("used_gb"))

    if used_mb is None:
        used_mb = used_bytes / _BYTES_PER_MIB if used_bytes is not None else 0.0
    if used_gb is None:
        used_gb = used_mb / 1024

    storage["used_bytes"] = int(used_bytes or 0)
    storage["size_root_fs"] = int(size_root_fs or 0)
    storage["used_mb"] = used_mb
    storage["used_gb"] = used_gb
    return storage


def _normalize_network_stats(value: Any) -> dict[str, Any]:
    network = _sanitize_metadata_mapping(value) if isinstance(value, Mapping) else {}
    rx_bytes = _coerce_nonnegative_float(network.get("rx_bytes"))
    tx_bytes = _coerce_nonnegative_float(network.get("tx_bytes"))
    network["rx_bytes"] = int(rx_bytes or 0)
    network["tx_bytes"] = int(tx_bytes or 0)
    return network


def _coerce_nonnegative_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _sanitize_list_item(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _sanitize_metadata_mapping(value)
    if isinstance(value, list):
        return [_sanitize_list_item(item) for item in value]
    return value


def _is_sensitive_absolute_path(*, key: str, value: str) -> bool:
    lowered_key = key.strip().lower()
    if not lowered_key.endswith(_PATH_KEY_SUFFIX):
        return False
    normalized = value.strip()
    if not normalized:
        return False
    return (
        normalized.startswith("/")
        or normalized.startswith("\\\\")
        or bool(_WINDOWS_DRIVE_PATH_RE.match(normalized))
    )


__all__ = [
    "normalize_runtime_logs_snapshot",
    "normalize_runtime_metrics_snapshot",
    "normalize_runtime_startup_progress_snapshot",
    "normalize_runtime_status_snapshot",
]
