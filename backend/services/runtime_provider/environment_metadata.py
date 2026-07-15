"""Runtime-provider backed environment metadata helpers.

Responsibilities:
- Read task runtime environment metadata through the provider boundary.
- Resolve canonical runtime environment info from local management-plane state
  (TASK_START runtime-job result + local workspace file) without a per-turn
  remote runner round-trip, so prompt/context assembly stays synchronous and
  never blocks the serving event loop.
- Keep prompt/context consumers from reconstructing local workspace paths.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Mapping

from sqlalchemy import select

from backend.database import SessionLocal
from backend.models.runner_control import RuntimeJob
from backend.services.runtime_provider.contracts import RuntimeActorType
from backend.services.runtime_provider.operations import RuntimeOperationService
from runtime_shared.runner_protocol import RunnerMessageType

logger = logging.getLogger(__name__)

_CANONICAL_ENVIRONMENT_KEYS = ("hostname", "os", "network", "routes")
_TASK_START_JOB_TYPE = RunnerMessageType.TASK_START.value


def _run_sync(coro):
    """Run a provider coroutine from sync prompt-building code."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    if not loop.is_running():
        return loop.run_until_complete(coro)

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - defensive bridge
            error["value"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "value" in error:
        raise error["value"]
    return result.get("value")


def load_runtime_environment_metadata(
    *,
    task_id: int,
    actor_id: str,
    user_id: int | None = None,
) -> dict[str, Any] | None:
    """Load environment metadata through the task runtime provider."""
    db = SessionLocal()
    try:
        runtime_operations = RuntimeOperationService(db)
        context = runtime_operations.context_for_internal_task(
            task_id=int(task_id),
            actor_type=RuntimeActorType.USER if user_id is not None else RuntimeActorType.SYSTEM,
            actor_id=user_id if user_id is not None else actor_id,
            user_id=user_id,
        )
        result = _run_sync(
            runtime_operations.run_for_context(
                context=context,
                operation="query_runtime_environment_metadata",
                call=lambda provider, request: provider.query_runtime_environment_metadata(request),
                metadata={
                    "wait_for_result": True,
                    "wait_timeout_seconds": 5.0,
                },
            )
        )
    finally:
        db.close()

    if result is None or not result.ok:
        return None
    delegate = result.metadata.get("delegate_result")
    if not isinstance(delegate, Mapping):
        return None
    environment = delegate.get("environment")
    if isinstance(environment, Mapping):
        return dict(environment)
    items = delegate.get("items")
    if isinstance(items, Mapping):
        return dict(items)
    return None


def _is_canonical_environment_info(value: Any) -> bool:
    """Return true for structured runtime environment info, not flat metadata."""
    if not isinstance(value, Mapping):
        return False
    return any(key in value for key in _CANONICAL_ENVIRONMENT_KEYS)


def _extract_environment_from_result_json(result_json: Any) -> dict[str, Any] | None:
    """Pull the persisted environment payload out of a runtime-job result_json."""
    if not isinstance(result_json, Mapping):
        return None
    result = result_json.get("result")
    if not isinstance(result, Mapping):
        return None
    environment = result.get("environment_info")
    if _is_canonical_environment_info(environment):
        return dict(environment)
    return None


def _load_task_start_environment_metadata(*, task_id: int) -> dict[str, Any] | None:
    """Read environment info persisted on the task's TASK_START runtime jobs.

    Cloud-runner placement collects environment info once at container start and
    reports it on the ``runtime.started`` result, which the runtime-event ingest
    persists into ``RuntimeJob.result_json``. This is a fast, local, synchronous
    read scoped to the task; it performs no remote runner operation.
    """
    db = SessionLocal()
    try:
        result_jsons = (
            db.execute(
                select(RuntimeJob.result_json)
                .where(
                    RuntimeJob.task_id == int(task_id),
                    RuntimeJob.job_type == _TASK_START_JOB_TYPE,
                )
                .order_by(RuntimeJob.created_at.desc())
            )
            .scalars()
            .all()
        )
    finally:
        db.close()
    for result_json in result_jsons:
        environment = _extract_environment_from_result_json(result_json)
        if environment is not None:
            return environment
    return None


def _load_workspace_environment_info(*, task_id: int) -> dict[str, Any] | None:
    """Read environment info from the local workspace env file (local placement)."""
    from backend.services.workspace.environment_collector import load_environment_info

    environment = load_environment_info(int(task_id))
    if _is_canonical_environment_info(environment):
        return dict(environment)
    return None


def resolve_local_runtime_environment_info(*, task_id: int) -> dict[str, Any] | None:
    """Resolve canonical runtime environment info from local management-plane state.

    Resolution order:
    1. The latest TASK_START runtime job result (cloud-runner placement persists
       the collected environment there at container start).
    2. The local workspace ``env_info.json`` (local docker placement collects the
       environment directly on the backend host).

    Fully synchronous and side-effect free; performs no remote runner round-trip,
    so it is safe to call inline during prompt/context assembly. Returns ``None``
    when no canonical environment is available.
    """
    try:
        environment = _load_task_start_environment_metadata(task_id=int(task_id))
        if environment is not None:
            return environment
    except Exception:
        logger.warning(
            "Failed to read TASK_START environment metadata for task %s",
            task_id,
            exc_info=True,
        )
    try:
        return _load_workspace_environment_info(task_id=int(task_id))
    except Exception:
        logger.warning(
            "Failed to read workspace environment info for task %s",
            task_id,
            exc_info=True,
        )
        return None


__all__ = [
    "load_runtime_environment_metadata",
    "resolve_local_runtime_environment_info",
]
