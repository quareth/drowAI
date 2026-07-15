"""Deterministic object-key builders for Data Plane artifact and evidence storage.

This module centralizes tenant-scoped key construction and path sanitization so
runner-provided names cannot escape logical object namespaces.
"""

from __future__ import annotations


def build_artifact_object_key(
    *,
    tenant_id: int | str,
    task_id: int | str,
    execution_id: int | str,
    artifact_id: int | str,
    filename: str,
) -> str:
    """Build a tenant/task-scoped object key for one execution artifact."""
    tenant_token = _require_identifier("tenant_id", tenant_id)
    task_token = _require_identifier("task_id", task_id)
    execution_token = _require_identifier("execution_id", execution_id)
    artifact_token = _require_identifier("artifact_id", artifact_id)
    safe_filename = sanitize_object_filename(filename)
    return (
        f"tenants/{tenant_token}/tasks/{task_token}/executions/"
        f"{execution_token}/artifacts/{artifact_token}/{safe_filename}"
    )


def build_evidence_object_key(
    *,
    tenant_id: int | str,
    engagement_id: int | str,
    evidence_id: int | str,
    filename: str,
) -> str:
    """Build a tenant/engagement-scoped object key for durable evidence."""
    tenant_token = _require_identifier("tenant_id", tenant_id)
    engagement_token = _require_identifier("engagement_id", engagement_id)
    evidence_token = _require_identifier("evidence_id", evidence_id)
    safe_filename = sanitize_object_filename(filename)
    return f"tenants/{tenant_token}/engagements/{engagement_token}/evidence/{evidence_token}/{safe_filename}"


def sanitize_object_filename(candidate: str) -> str:
    """Return a safe filename from a runner-provided path-like value."""
    if not isinstance(candidate, str):
        raise ValueError("filename must be a string")

    normalized = candidate.replace("\\", "/").strip()
    if not normalized:
        raise ValueError("filename must not be empty")
    if normalized.startswith("/workspace/"):
        normalized = normalized[len("/workspace/") :]
    normalized = normalized.lstrip("/")

    cleaned_parts: list[str] = []
    for part in normalized.split("/"):
        token = part.strip()
        if token in {"", ".", ".."}:
            continue
        token = "".join(char for char in token if ord(char) >= 32)
        if token:
            cleaned_parts.append(token)

    if not cleaned_parts:
        raise ValueError("filename must include at least one non-empty path part")
    return cleaned_parts[-1]


def _require_identifier(name: str, value: int | str) -> str:
    token = str(value).strip()
    if not token:
        raise ValueError(f"{name} is required")
    return token
