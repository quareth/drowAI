"""Pure tooling_plane tool-command operation mapping.

Converts validated tooling_plane tool-command protocol payloads into runner operation
params only. No websocket, queue, job-store, dispatch, or cloud client I/O; no
connection-session state. Never imports ``drowai_runner.cloud_client``.
"""

from __future__ import annotations

from typing import Mapping

from drowai_runner.protocol_handler import RunnerTaskRuntimeBinding
from runtime_shared.runner_protocol import RunnerEnvelope


def _map_tooling_plane_tool_command_operation(
    *,
    inbound: RunnerEnvelope,
    binding: RunnerTaskRuntimeBinding,
) -> dict[str, object]:
    payload = inbound.payload
    params = dict(payload.params) if isinstance(payload.params, Mapping) else {}
    params.update(
        {
            "runtime_job_id": str(payload.task_runtime_job_id).strip(),
            "tool_command_runtime_job_id": str(inbound.runtime_job_id or "").strip(),
            "operation_id": str(payload.operation_id).strip(),
            "command_id": str(payload.command_id).strip(),
            "tool": str(payload.tool).strip(),
            "command": str(payload.command).strip(),
            "cwd": str(payload.cwd).strip(),
            "env": dict(payload.env),
            "timeout_policy": dict(payload.timeout_policy),
            "timeout_seconds": float(payload.timeout_seconds),
            "workspace_files": [item.to_payload() for item in payload.workspace_files],
            "workspace_directories": [
                item.to_payload() for item in payload.workspace_directories
            ],
        }
    )
    if payload.tool_call_id is not None:
        params["tool_call_id"] = str(payload.tool_call_id).strip()
    if payload.tool_batch_id is not None:
        params["tool_batch_id"] = str(payload.tool_batch_id).strip()
    if payload.execution_strategy is not None:
        params["execution_strategy"] = str(payload.execution_strategy).strip()
    params["workspace_id"] = binding.workspace_id
    return params
