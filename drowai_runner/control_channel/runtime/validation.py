"""Remote-runtime request validation and task-runtime binding lookup.

Owns inbound remote_runtime request validation, job binding lookups, and
task-runtime binding resolution. Performs no websocket I/O, operation dispatch,
result-event assembly, or terminal frame handling. Must not import
``drowai_runner.cloud_client``.
"""

from __future__ import annotations

from typing import Callable, Mapping

from drowai_runner.job_store import RunnerJobStore
from drowai_runner.protocol_handler import RunnerTaskRuntimeBinding
from runtime_shared.runner_protocol import RunnerEnvelope, RunnerMessageType

from drowai_runner.control_channel.constants import (
    _REMOTE_RUNTIME_RUNNER_IDENTITY_ERROR,
    _REMOTE_RUNTIME_CONTEXT_MISMATCH,
    _REMOTE_RUNTIME_CONTEXT_MISSING,
    _REMOTE_RUNTIME_START_CONFLICT,
    _REMOTE_RUNTIME_WORKSPACE_MISMATCH,
    _REMOTE_RUNTIME_WORKSPACE_SCOPED_REQUEST_TYPES,
)
from drowai_runner.control_channel.identity.models import CloudChannelIdentity
from drowai_runner.control_channel.runtime.models import _RemoteRuntimeRequestContext


class RemoteRuntimeRequestValidator:
    """Validates remote_runtime requests and resolves task-runtime bindings."""

    def __init__(
        self,
        *,
        job_store_provider: Callable[[], RunnerJobStore],
    ) -> None:
        self._job_store_provider = job_store_provider

    def validate(
        self,
        *,
        identity: CloudChannelIdentity,
        inbound: RunnerEnvelope,
    ) -> tuple[str, str | None, _RemoteRuntimeRequestContext | None]:
        if inbound.tenant_id != str(identity.tenant_id) or inbound.runner_id != identity.runner_id:
            return ("rejected", _REMOTE_RUNTIME_RUNNER_IDENTITY_ERROR, None)
        control_runtime_job_id = str(inbound.runtime_job_id or "").strip()
        if not control_runtime_job_id or inbound.task_id is None:
            return ("rejected", _REMOTE_RUNTIME_CONTEXT_MISSING, None)
        runtime_job_id = self.resolve_runner_runtime_job_id(
            inbound=inbound,
            fallback_runtime_job_id=control_runtime_job_id,
        )
        if not runtime_job_id:
            return ("rejected", _REMOTE_RUNTIME_CONTEXT_MISSING, None)

        try:
            task_id = int(inbound.task_id)
        except (TypeError, ValueError):
            return ("rejected", _REMOTE_RUNTIME_CONTEXT_MISSING, None)

        job_store = self._job_store_provider()
        local_job = job_store.find_job(runtime_job_id)
        payload_workspace_id = str(getattr(inbound.payload, "workspace_id", "") or "").strip()
        expected_workspace_id = f"task-{task_id}"

        if inbound.message_type is RunnerMessageType.TASK_START:
            if payload_workspace_id and payload_workspace_id != expected_workspace_id:
                return ("rejected", _REMOTE_RUNTIME_WORKSPACE_MISMATCH, None)

            if local_job is None:
                existing_task_job = self._find_job_for_task(
                    tenant_id=inbound.tenant_id,
                    task_id=str(task_id),
                )
                if (
                    existing_task_job is not None
                    and existing_task_job.runtime_job_id != runtime_job_id
                ):
                    return ("rejected", _REMOTE_RUNTIME_START_CONFLICT, None)
                existing_workspace_job = self._find_job_for_workspace(
                    workspace_id=expected_workspace_id
                )
                if (
                    existing_workspace_job is not None
                    and existing_workspace_job.runtime_job_id != runtime_job_id
                ):
                    return ("rejected", _REMOTE_RUNTIME_START_CONFLICT, None)
            else:
                if (
                    local_job.tenant_id != inbound.tenant_id
                    or local_job.task_id != str(task_id)
                    or local_job.workspace_id != expected_workspace_id
                ):
                    return ("rejected", _REMOTE_RUNTIME_CONTEXT_MISMATCH, None)

            return (
                "accepted",
                None,
                _RemoteRuntimeRequestContext(
                    runtime_job_id=runtime_job_id,
                    task_id=task_id,
                    workspace_id=expected_workspace_id,
                ),
            )

        if local_job is None:
            if inbound.message_type is RunnerMessageType.TASK_RETIRE:
                existing_task_job = self._find_job_for_task(
                    tenant_id=inbound.tenant_id,
                    task_id=str(task_id),
                )
                if existing_task_job is not None:
                    return (
                        "accepted",
                        None,
                        _RemoteRuntimeRequestContext(
                            runtime_job_id=existing_task_job.runtime_job_id,
                            task_id=task_id,
                            workspace_id=existing_task_job.workspace_id,
                        ),
                    )
                existing_workspace_job = self._find_job_for_workspace(
                    workspace_id=expected_workspace_id
                )
                if existing_workspace_job is not None:
                    return (
                        "accepted",
                        None,
                        _RemoteRuntimeRequestContext(
                            runtime_job_id=existing_workspace_job.runtime_job_id,
                            task_id=task_id,
                            workspace_id=existing_workspace_job.workspace_id,
                        ),
                    )
                if payload_workspace_id and payload_workspace_id != expected_workspace_id:
                    return ("rejected", _REMOTE_RUNTIME_WORKSPACE_MISMATCH, None)
                return (
                    "accepted",
                    None,
                    _RemoteRuntimeRequestContext(
                        runtime_job_id=runtime_job_id,
                        task_id=task_id,
                        workspace_id=expected_workspace_id,
                    ),
                )
            if inbound.message_type is RunnerMessageType.RUNTIME_ARTIFACT_PROMOTE:
                existing_task_job = self._find_job_for_task(
                    tenant_id=inbound.tenant_id,
                    task_id=str(task_id),
                )
                if existing_task_job is not None:
                    return (
                        "accepted",
                        None,
                        _RemoteRuntimeRequestContext(
                            runtime_job_id=existing_task_job.runtime_job_id,
                            task_id=task_id,
                            workspace_id=existing_task_job.workspace_id,
                        ),
                    )
                existing_workspace_job = self._find_job_for_workspace(
                    workspace_id=expected_workspace_id
                )
                if existing_workspace_job is not None:
                    return (
                        "accepted",
                        None,
                        _RemoteRuntimeRequestContext(
                            runtime_job_id=existing_workspace_job.runtime_job_id,
                            task_id=task_id,
                            workspace_id=existing_workspace_job.workspace_id,
                        ),
                    )
            return ("rejected", _REMOTE_RUNTIME_CONTEXT_MISSING, None)
        if local_job.tenant_id != inbound.tenant_id or local_job.task_id != str(task_id):
            return ("rejected", _REMOTE_RUNTIME_CONTEXT_MISMATCH, None)
        if (
            inbound.message_type in _REMOTE_RUNTIME_WORKSPACE_SCOPED_REQUEST_TYPES
            and payload_workspace_id
            and payload_workspace_id != local_job.workspace_id
        ):
            return ("rejected", _REMOTE_RUNTIME_WORKSPACE_MISMATCH, None)

        return (
            "accepted",
            None,
            _RemoteRuntimeRequestContext(
                runtime_job_id=runtime_job_id,
                task_id=task_id,
                workspace_id=local_job.workspace_id,
            ),
        )

    def _find_job_for_task(self, *, tenant_id: str, task_id: str):
        for job in self._job_store_provider().recover_active_jobs():
            if job.tenant_id == tenant_id and job.task_id == task_id:
                return job
        return None

    def _find_job_for_workspace(self, *, workspace_id: str):
        for job in self._job_store_provider().recover_active_jobs():
            if job.workspace_id == workspace_id:
                return job
        return None

    @staticmethod
    def resolve_runner_runtime_job_id(
        *,
        inbound: RunnerEnvelope,
        fallback_runtime_job_id: str,
    ) -> str:
        payload_params = getattr(inbound.payload, "params", {})
        if isinstance(payload_params, Mapping):
            candidate = str(payload_params.get("runtime_job_id") or "").strip()
            if candidate:
                return candidate
            candidate = str(payload_params.get("runner_runtime_job_id") or "").strip()
            if candidate:
                return candidate
            candidate = str(payload_params.get("task_runtime_job_id") or "").strip()
            if candidate:
                return candidate
        return str(fallback_runtime_job_id or "").strip()

    def lookup_task_runtime_binding(self, runtime_job_id: str) -> RunnerTaskRuntimeBinding | None:
        normalized_runtime_job_id = str(runtime_job_id).strip()
        if not normalized_runtime_job_id:
            return None
        local_job = self._job_store_provider().find_job(normalized_runtime_job_id)
        if local_job is None:
            return None
        workspace_id = str(local_job.workspace_id).strip()
        if not workspace_id:
            return None
        return RunnerTaskRuntimeBinding(
            runtime_job_id=str(local_job.runtime_job_id).strip(),
            tenant_id=str(local_job.tenant_id).strip(),
            task_id=str(local_job.task_id).strip(),
            workspace_id=workspace_id,
        )
