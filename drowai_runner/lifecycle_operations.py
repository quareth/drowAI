"""Runner-local runtime lifecycle operation bodies.

This module owns runner-side runtime materialization, lifecycle transitions,
retirement recovery, cleanup hand-off, and runtime input persistence/signalling.
It does not perform protocol mapping or websocket I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import threading
from typing import Any, Mapping
import uuid

from drowai_runner.cleanup import RunnerCleanupService
from drowai_runner.config import RunnerConfig
from drowai_runner.docker_runtime import (
    RunnerDockerRuntime,
    build_runner_container_config,
    build_runner_container_name,
)
from drowai_runner.environment import (
    collect_and_save_runner_environment_info,
    load_runner_environment_info,
)
from drowai_runner.job_store import RunnerJobStore
from drowai_runner.workspace import RunnerWorkspaceManager
from runtime_shared.workspace_filesystem import WorkspaceEntryUnsafeError

_RETIRE_GRACEFUL_STOP_TIMEOUT_SECONDS = 1


class RunnerLifecycleOperations:
    """Execute runner-local runtime lifecycle operations."""

    def __init__(
        self,
        *,
        config: RunnerConfig,
        workspace: RunnerWorkspaceManager,
        job_store: RunnerJobStore,
        docker_runtime: RunnerDockerRuntime,
        cleanup: RunnerCleanupService,
    ) -> None:
        self._config = config
        self._workspace = workspace
        self._job_store = job_store
        self._docker_runtime = docker_runtime
        self._cleanup = cleanup
        self._materialize_locks: dict[str, threading.Lock] = {}
        self._materialize_locks_guard = threading.Lock()

    def materialize_runtime(self, params: dict[str, object]) -> dict[str, object]:
        try:
            task_id = int(str(params.get("task_id") or "").strip())
        except ValueError:
            return {"status": "failed", "error_code": "INVALID_TASK_ID"}
        tenant_id = str(params.get("tenant_id") or "tenant").strip() or "tenant"
        runtime_job_id = str(params.get("runtime_job_id") or str(uuid.uuid4())).strip()
        workspace_id = str(params.get("workspace_id") or f"task-{task_id}").strip()
        image_name = str(params.get("image") or self._config.runtime_image_tag).strip()
        vpn_enabled = bool(params.get("vpn_enabled") or False)
        if not runtime_job_id:
            return {"status": "failed", "error_code": "MISSING_RUNTIME_JOB_ID"}
        materialize_lock = self._materialize_lock_for(runtime_job_id)
        with materialize_lock:
            container_id: str | None = None
            network_created = False
            network_name: str | None = None
            try:
                workspace_path = self._workspace.initialize_task_workspace(workspace_id)
                job = self._job_store.start_job(
                    runtime_job_id=runtime_job_id,
                    tenant_id=tenant_id,
                    task_id=str(task_id),
                    workspace_id=workspace_id,
                    image=image_name,
                )
                if job.container_id:
                    if self._existing_runtime_is_reusable(job.container_id):
                        reuse_metadata: dict[str, object] = {
                            "runtime_job_id": runtime_job_id,
                            "task_id": str(task_id),
                            "workspace_id": workspace_id,
                            "container_id": job.container_id,
                            "image": job.image or image_name,
                            "reused_existing_runtime": True,
                        }
                        # Re-report the environment captured at first start so the
                        # control plane can (re)persist it for prompt/context reads
                        # even when the container is reused across runner restarts.
                        reused_environment_info = load_runner_environment_info(
                            workspace_manager=self._workspace,
                            workspace_id=workspace_id,
                        )
                        if isinstance(reused_environment_info, dict):
                            reuse_metadata["environment_info"] = reused_environment_info
                        return {
                            "accepted": True,
                            "status": "succeeded",
                            "metadata": reuse_metadata,
                        }
                    self._remove_runtime_container_best_effort(job.container_id)
                self._docker_runtime.ensure_runtime_image(
                    image_name,
                    pull_if_missing=True,
                    refresh_if_tagged=True,
                )
                container_name = build_runner_container_name(
                    tenant_id=tenant_id,
                    task_id=task_id,
                )
                network_result = self._docker_runtime.ensure_task_network(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    container_name=container_name,
                    runtime_identity=container_name,
                    pool_cidr=self._config.runtime_network_pool,
                )
                network_created = network_result.created
                network_name = network_result.name
                self._workspace.migrate_legacy_runtime_input(workspace_id)
                container_config = build_runner_container_config(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    image_name=image_name,
                    workspace_path=workspace_path,
                    control_path=self._workspace.initialize_task_control(workspace_id),
                    vpn_enabled=vpn_enabled,
                    runner_root=self._config.runner_root,
                    host_bind_root=self._config.host_bind_root,
                    network_name=network_name,
                )
                container_id = self._docker_runtime.create_container(container_config)
                self._docker_runtime.start_container(container_id)
                if self._docker_runtime.container_status(container_id) != "running":
                    raise RuntimeError("Runtime container exited before startup completed.")
                verification = self._docker_runtime.verify_runtime_manifest(container_id)
                if not verification.ok:
                    mismatch = ", ".join(verification.mismatch_keys)
                    raise RuntimeError(f"Runtime manifest contract mismatch: {mismatch}")
                self._workspace.finalize_legacy_control_cutover(workspace_id)
                environment_info = None
                try:
                    environment_info = collect_and_save_runner_environment_info(
                        docker_runtime=self._docker_runtime,
                        workspace_manager=self._workspace,
                        container_id=container_id,
                        workspace_id=workspace_id,
                    )
                except Exception:
                    environment_info = None
                self._job_store.mark_running(runtime_job_id, container_id=container_id)
                start_metadata: dict[str, Any] = {
                    "runtime_job_id": runtime_job_id,
                    "task_id": str(task_id),
                    "workspace_id": workspace_id,
                    "container_id": container_id,
                    "image": image_name,
                    "environment_info_collected": environment_info is not None,
                    "environment_collection_errors": (
                        list(environment_info.get("collection_errors") or [])
                        if isinstance(environment_info, dict)
                        else []
                    ),
                    "runtime_network": {
                        "name": network_result.name,
                        "subnet": network_result.subnet,
                        "action": "created" if network_result.created else "reused",
                    },
                }
                # Carry the full environment payload on the start result so the
                # control plane persists it once (RuntimeJob.result_json) and
                # prompt/context assembly can read it locally without a per-turn
                # remote runner round-trip.
                if isinstance(environment_info, dict):
                    start_metadata["environment_info"] = environment_info
                return {
                    "accepted": True,
                    "status": "succeeded",
                    "metadata": start_metadata,
                }
            except Exception as exc:
                if container_id:
                    self._remove_runtime_container_best_effort(container_id)
                if network_created:
                    self._remove_runtime_network_best_effort(
                        tenant_id=tenant_id,
                        task_id=task_id,
                    )
                try:
                    self._job_store.mark_stopped(runtime_job_id, status="failed")
                except Exception:
                    pass
                return {
                    "accepted": False,
                    "status": "failed",
                    "error_code": "RUNNER_MATERIALIZE_FAILED",
                    "error_message": str(exc),
                    "metadata": {"runtime_job_id": runtime_job_id},
                }

    def _existing_runtime_is_reusable(self, container_id: str) -> bool:
        """Return whether an existing runtime container matches the current contract."""
        try:
            status = self._docker_runtime.container_status(container_id)
        except Exception:
            return False
        if status != "running":
            return False
        try:
            verification = self._docker_runtime.verify_runtime_manifest(container_id)
        except Exception:
            return False
        if not verification.ok:
            return False
        return True

    def _remove_runtime_container_best_effort(self, container_id: str) -> None:
        """Stop and remove a runtime container without masking the caller's outcome."""
        try:
            self._docker_runtime.stop_container(container_id, timeout_seconds=1)
        except Exception:
            pass
        try:
            self._docker_runtime.remove_container(container_id, force=True)
        except Exception:
            pass

    def _remove_runtime_network_best_effort(
        self,
        *,
        tenant_id: str | int,
        task_id: str | int,
    ) -> None:
        """Remove only an empty runner-owned bridge without masking the caller."""
        try:
            self._remove_runtime_network(tenant_id=tenant_id, task_id=task_id)
        except Exception:
            pass

    def _remove_runtime_network(
        self,
        *,
        tenant_id: str | int,
        task_id: str | int,
    ) -> bool:
        """Remove the empty owned task bridge, surfacing ownership or Docker errors."""
        container_name = build_runner_container_name(
            tenant_id=tenant_id,
            task_id=int(task_id),
        )
        return self._docker_runtime.remove_task_network(
            tenant_id=tenant_id,
            task_id=task_id,
            container_name=container_name,
            runtime_identity=container_name,
            pool_cidr=self._config.runtime_network_pool,
        )

    def pause_or_resume_runtime(
        self,
        params: dict[str, object],
        *,
        pause: bool,
    ) -> dict[str, object]:
        runtime_job_id = str(params.get("runtime_job_id") or "").strip()
        if not runtime_job_id:
            return {"status": "failed", "error_code": "MISSING_RUNTIME_JOB_ID"}
        job = self._job_store.find_job(runtime_job_id)
        if job is None or not job.container_id:
            return {"status": "failed", "error_code": "RUNNER_JOB_NOT_FOUND"}
        try:
            if pause:
                self._docker_runtime.pause_container(job.container_id)
                self._job_store.mark_status(runtime_job_id, status="paused")
            else:
                self._docker_runtime.resume_container(job.container_id)
                self._job_store.mark_status(runtime_job_id, status="running")
        except Exception as exc:
            return {
                "accepted": False,
                "status": "failed",
                "error_code": "RUNNER_RUNTIME_TRANSITION_FAILED",
                "error_message": str(exc),
            }
        return {
            "accepted": True,
            "status": "succeeded",
            "metadata": {
                "runtime_job_id": runtime_job_id,
                "container_id": job.container_id,
                "operation": "pause_runtime" if pause else "resume_runtime",
            },
        }

    def retire_runtime(self, params: dict[str, object]) -> dict[str, object]:
        runtime_job_id = str(params.get("runtime_job_id") or "").strip()
        if not runtime_job_id:
            return {"status": "failed", "error_code": "MISSING_RUNTIME_JOB_ID"}
        job = self._job_store.find_job(runtime_job_id)
        if job is None:
            recovered = self._recover_missing_retire_job(runtime_job_id, params)
            if not recovered["accepted"]:
                return recovered
            job = self._job_store.get_job(runtime_job_id)
        if job is None:
            return {"status": "failed", "error_code": "RUNNER_JOB_NOT_FOUND"}
        if job.status == "cleaned_up":
            try:
                network_removed = self._remove_runtime_network(
                    tenant_id=job.tenant_id,
                    task_id=job.task_id,
                )
            except Exception as exc:
                return {
                    "accepted": False,
                    "status": "failed",
                    "error_code": "RUNNER_NETWORK_REMOVE_FAILED",
                    "error_message": str(exc),
                    "metadata": {
                        "runtime_job_id": runtime_job_id,
                        "container_removed": False,
                        "workspace_removed": False,
                        "retained_paths": [],
                        "network_removed": False,
                        "idempotent": True,
                    },
                }
            return {
                "accepted": True,
                "status": "succeeded",
                "metadata": {
                    "runtime_job_id": runtime_job_id,
                    "container_removed": False,
                    "workspace_removed": False,
                    "retained_paths": [],
                    "network_removed": network_removed,
                    "idempotent": True,
                },
            }
        if job.container_id:
            try:
                self._docker_runtime.stop_container(
                    job.container_id,
                    timeout_seconds=_RETIRE_GRACEFUL_STOP_TIMEOUT_SECONDS,
                )
            except Exception:
                pass
        self._job_store.mark_stopped(runtime_job_id, status="stopped")
        cleanup = self._cleanup.cleanup_task(runtime_job_id)
        network_removed = False
        network_error: Exception | None = None
        if cleanup.status == "ok":
            try:
                network_removed = self._remove_runtime_network(
                    tenant_id=job.tenant_id,
                    task_id=job.task_id,
                )
            except Exception as exc:
                network_error = exc
        succeeded = cleanup.status == "ok" and network_error is None
        return {
            "accepted": succeeded,
            "status": "succeeded" if succeeded else "failed",
            "error_code": (
                None
                if succeeded
                else "RUNNER_NETWORK_REMOVE_FAILED"
                if network_error is not None
                else "RUNNER_RETIRE_FAILED"
            ),
            "error_message": str(network_error) if network_error is not None else None,
            "metadata": {
                "runtime_job_id": runtime_job_id,
                "container_removed": cleanup.container_removed,
                "workspace_removed": cleanup.workspace_removed,
                "retained_paths": list(cleanup.retained_paths),
                "network_removed": network_removed,
            },
        }

    def _recover_missing_retire_job(
        self,
        runtime_job_id: str,
        params: Mapping[str, object],
    ) -> dict[str, object]:
        tenant_id = str(params.get("tenant_id") or "").strip()
        task_id = str(params.get("task_id") or "").strip()
        workspace_id = str(params.get("workspace_id") or "").strip()
        if not tenant_id or not task_id or not workspace_id:
            return {
                "accepted": False,
                "status": "failed",
                "error_code": "RUNNER_JOB_NOT_FOUND",
                "error_message": f"Unknown runtime job: {runtime_job_id}",
            }

        container_id = self._find_container_id_for_retire(tenant_id=tenant_id, task_id=task_id)
        try:
            self._job_store.start_job(
                runtime_job_id=runtime_job_id,
                tenant_id=tenant_id,
                task_id=task_id,
                workspace_id=workspace_id,
                image=None,
                container_id=container_id,
            )
        except ValueError as exc:
            return {
                "accepted": False,
                "status": "failed",
                "error_code": "RUNNER_JOB_RECOVERY_CONFLICT",
                "error_message": str(exc),
            }
        return {
            "accepted": True,
            "status": "succeeded",
            "metadata": {
                "runtime_job_id": runtime_job_id,
                "task_id": task_id,
                "workspace_id": workspace_id,
                "container_id": container_id,
                "recovered": True,
            },
        }

    def _find_container_id_for_retire(self, *, tenant_id: str, task_id: str) -> str | None:
        try:
            task_id_int = int(task_id)
        except ValueError:
            return None
        container_name = build_runner_container_name(tenant_id=tenant_id, task_id=task_id_int)
        finder = getattr(self._docker_runtime, "find_container_id_by_name", None)
        if not callable(finder):
            return None
        try:
            return finder(container_name)
        except Exception:
            return None

    def stop_runtime(self, params: dict[str, object]) -> dict[str, object]:
        runtime_job_id = str(params.get("runtime_job_id") or "").strip()
        if not runtime_job_id:
            return {"status": "failed", "error_code": "MISSING_RUNTIME_JOB_ID"}
        job = self._job_store.find_job(runtime_job_id)
        if job is None:
            return {"status": "failed", "error_code": "RUNNER_JOB_NOT_FOUND"}
        lifecycle_intent = str(params.get("lifecycle_intent") or "").strip().lower()
        job_status = "cancelled" if lifecycle_intent == "cancel" else "stopped"
        if job.container_id:
            try:
                self._docker_runtime.stop_container(job.container_id)
            except Exception as exc:
                return {
                    "accepted": False,
                    "status": "failed",
                    "error_code": "RUNNER_RUNTIME_TRANSITION_FAILED",
                    "error_message": str(exc),
                }
        self._job_store.mark_stopped(runtime_job_id, status=job_status)
        return {
            "accepted": True,
            "status": "succeeded",
            "metadata": {
                "runtime_job_id": runtime_job_id,
                "container_id": job.container_id,
                "operation": "stop_runtime",
                "lifecycle_outcome": job_status,
            },
        }

    def append_runtime_input(self, params: dict[str, object]) -> dict[str, object]:
        runtime_job_id = str(params.get("runtime_job_id") or "").strip()
        if not runtime_job_id:
            return {"accepted": False, "status": "failed", "error_code": "MISSING_RUNTIME_JOB_ID"}

        strict_persistence = bool(params.get("strict_persistence") or False)
        message = str(params.get("message") or params.get("text") or "")
        metadata = params.get("metadata")
        payload_metadata = dict(metadata) if isinstance(metadata, Mapping) else {}

        job = self._job_store.find_job(runtime_job_id)
        if job is None:
            return {
                "accepted": False,
                "status": "failed",
                "error_code": "RUNNER_JOB_NOT_FOUND",
                "metadata": {
                    "success": False,
                    "runtime_job_id": runtime_job_id,
                    "persisted": False,
                    "signal_attempted": False,
                    "signal_sent": False,
                    "detail": f"Unknown runtime job: {runtime_job_id}",
                },
            }

        try:
            self._workspace.resolve_task_workspace(job.workspace_id)
        except ValueError as exc:
            return {
                "accepted": False,
                "status": "rejected",
                "error_code": "RUNNER_WORKSPACE_PATH_OUTSIDE_SCOPE",
                "error_message": str(exc),
                "metadata": {
                    "success": False,
                    "runtime_job_id": runtime_job_id,
                    "workspace_id": job.workspace_id,
                    "persisted": False,
                    "signal_attempted": False,
                    "signal_sent": False,
                    "detail": str(exc),
                },
            }

        persisted = False
        append_error: str | None = None
        entry: dict[str, object] = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "message": message,
        }
        if payload_metadata:
            entry["metadata"] = payload_metadata
        try:
            encoded_entry = (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")
            self._workspace.control_filesystem(job.workspace_id).append_bytes(
                "runtime-input/user_input.jsonl",
                encoded_entry,
                mode=0o600,
            )
            persisted = True
        except (OSError, WorkspaceEntryUnsafeError) as exc:
            unsafe_entry = isinstance(exc, WorkspaceEntryUnsafeError)
            append_error = (
                "Runtime input path is unsafe."
                if unsafe_entry
                else "Runtime input could not be persisted."
            )
            if strict_persistence:
                return {
                    "accepted": False,
                    "status": "failed",
                    "error_code": (
                        "RUNNER_WORKSPACE_ENTRY_UNSAFE"
                        if unsafe_entry
                        else "RUNNER_RUNTIME_INPUT_PERSIST_FAILED"
                    ),
                    "error_message": append_error,
                    "metadata": {
                        "success": False,
                        "runtime_job_id": runtime_job_id,
                        "workspace_id": job.workspace_id,
                        "persisted": False,
                        "signal_attempted": False,
                        "signal_sent": False,
                        "detail": append_error,
                    },
                }

        signal_attempted = False
        signal_sent = False
        signal_detail: str | None = None
        if job.container_id:
            signal_attempted = True
            signal_sent, signal_detail = self._docker_runtime.send_signal(job.container_id, "SIGUSR1")
        elif persisted or not strict_persistence:
            signal_detail = "Runtime container is not assigned."

        success = persisted or not strict_persistence
        detail = append_error or signal_detail
        return {
            "accepted": success,
            "status": "succeeded" if success else "failed",
            "error_code": None if success else "RUNNER_RUNTIME_INPUT_PERSIST_FAILED",
            "error_message": None if success else detail,
            "metadata": {
                "success": success,
                "runtime_job_id": runtime_job_id,
                "workspace_id": job.workspace_id,
                "persisted": persisted,
                "signal_attempted": signal_attempted,
                "signal_sent": signal_sent,
                "detail": detail,
            },
        }

    def _materialize_lock_for(self, runtime_job_id: str) -> threading.Lock:
        with self._materialize_locks_guard:
            existing = self._materialize_locks.get(runtime_job_id)
            if existing is not None:
                return existing
            created = threading.Lock()
            self._materialize_locks[runtime_job_id] = created
            return created


__all__ = ["RunnerLifecycleOperations"]
