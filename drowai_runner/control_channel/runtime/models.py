"""Remote-runtime request context DTO. Data only.

Holds the immutable identity binding a validated remote_runtime request to one local
runner job scope. No logic, no I/O, no protocol behavior.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _RemoteRuntimeRequestContext:
    """Validated remote_runtime request identity bound to one local runner job scope."""

    runtime_job_id: str
    task_id: int
    workspace_id: str
