"""Shared runtime workspace artifact read access for internal backend callers.

Runner-placement artifacts live in the task runtime workspace. Internal services
(provenance, memory, archive) must read them through the runtime-provider boundary
with the same wait semantics used by workspace file APIs—not fire-and-forget
dispatches that return before runner results arrive.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.services.runtime_provider.contracts import (
    RuntimeActorType,
    RuntimeOperationResult,
)
from backend.services.runtime_provider.operations import RuntimeOperationService
from backend.services.runtime_provider.sync_bridge import run_provider_operation_sync

RUNTIME_ARTIFACT_IO_WAIT_TIMEOUT_SECONDS = 30.0


def normalize_runtime_artifact_relative_path(path: str) -> str:
    """Normalize runtime artifact path without resolving a host workspace."""
    normalized = str(path or "").replace("\\", "/").strip()
    if normalized.startswith("/workspace/"):
        normalized = normalized[len("/workspace/") :]
    return normalized.lstrip("/")


def runtime_artifact_wait_fields(
    *,
    wait_timeout_seconds: float = RUNTIME_ARTIFACT_IO_WAIT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Return wait policy fields for runtime artifact provider operations."""
    return {
        "wait_for_result": True,
        "wait_timeout_seconds": float(wait_timeout_seconds),
    }


def runtime_artifact_wait_metadata(
    *,
    wait_timeout_seconds: float = RUNTIME_ARTIFACT_IO_WAIT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Return metadata that blocks until a runner workspace operation completes."""
    return runtime_artifact_wait_fields(wait_timeout_seconds=wait_timeout_seconds)


def build_runtime_artifact_read_payload(
    *,
    path: str,
    binary: bool,
    encoding: str = "utf-8",
    max_chars: int | None = None,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """Build a provider read payload with normalized workspace-relative paths."""
    normalized_path = normalize_runtime_artifact_relative_path(path)
    payload: dict[str, Any] = {
        "path": normalized_path,
        "artifact_path": normalized_path,
        "binary": binary,
        "encoding": encoding,
    }
    if max_chars is not None:
        payload["max_chars"] = max_chars
    if max_bytes is not None:
        payload["max_bytes"] = max_bytes
    return payload


async def execute_runtime_artifact_read(
    db: Session,
    *,
    task_id: int,
    path: str,
    actor_type: RuntimeActorType,
    actor_id: str | int | None,
    user_id: int | None = None,
    binary: bool = True,
    encoding: str = "utf-8",
    max_chars: int | None = None,
    wait_timeout_seconds: float = RUNTIME_ARTIFACT_IO_WAIT_TIMEOUT_SECONDS,
) -> RuntimeOperationResult | None:
    """Read one runtime workspace artifact through the provider boundary."""
    runtime_operations = RuntimeOperationService(db)
    context = runtime_operations.context_for_internal_task(
        task_id=int(task_id),
        actor_type=actor_type,
        actor_id=actor_id,
        user_id=user_id,
    )
    return await runtime_operations.run_for_context(
        context=context,
        operation="read_runtime_artifact_file",
        call=lambda provider, request: provider.read_runtime_artifact_file(request),
        payload=build_runtime_artifact_read_payload(
            path=path,
            binary=binary,
            encoding=encoding,
            max_chars=max_chars,
        ),
        metadata=runtime_artifact_wait_metadata(wait_timeout_seconds=wait_timeout_seconds),
    )


def execute_runtime_artifact_read_sync(
    db: Session | None,
    *,
    task_id: int,
    path: str,
    actor_type: RuntimeActorType,
    actor_id: str | int | None,
    user_id: int | None = None,
    binary: bool = True,
    encoding: str = "utf-8",
    max_chars: int | None = None,
    wait_timeout_seconds: float = RUNTIME_ARTIFACT_IO_WAIT_TIMEOUT_SECONDS,
    log_context: str = "runtime artifact read",
) -> RuntimeOperationResult | None:
    """Run a runtime artifact read from synchronous code, including inside async graphs."""
    try:
        asyncio.get_running_loop()
        in_async_context = True
    except RuntimeError:
        in_async_context = False

    if in_async_context or db is None:
        def _coro_factory() -> Any:
            async def _run() -> RuntimeOperationResult | None:
                session = SessionLocal()
                try:
                    return await execute_runtime_artifact_read(
                        session,
                        task_id=task_id,
                        path=path,
                        actor_type=actor_type,
                        actor_id=actor_id,
                        user_id=user_id,
                        binary=binary,
                        encoding=encoding,
                        max_chars=max_chars,
                        wait_timeout_seconds=wait_timeout_seconds,
                    )
                finally:
                    session.close()

            return _run()

        return run_provider_operation_sync(_coro_factory, log_context=log_context)

    return asyncio.run(
        execute_runtime_artifact_read(
            db,
            task_id=task_id,
            path=path,
            actor_type=actor_type,
            actor_id=actor_id,
            user_id=user_id,
            binary=binary,
            encoding=encoding,
            max_chars=max_chars,
            wait_timeout_seconds=wait_timeout_seconds,
        )
    )


def decode_runtime_artifact_binary_delegate(
    result: RuntimeOperationResult | None,
    *,
    fallback_path: str,
) -> tuple[bytes | None, str | None]:
    """Extract binary artifact bytes from a completed provider read result."""
    if result is None or not result.ok:
        return None, None
    delegate = result.metadata.get("delegate_result")
    if not isinstance(delegate, Mapping):
        return None, None
    encoded = delegate.get("content_base64")
    if not isinstance(encoded, str):
        return None, None
    try:
        data = base64.b64decode(encoded)
    except Exception:
        return None, None
    resolved_path = str(delegate.get("path") or delegate.get("artifact_path") or fallback_path)
    return data, normalize_runtime_artifact_relative_path(resolved_path)


def decode_runtime_artifact_text_delegate(
    result: RuntimeOperationResult | None,
) -> tuple[str | None, bool]:
    """Extract text artifact content from a completed provider read result."""
    if result is None or not result.ok:
        return None, False
    delegate = result.metadata.get("delegate_result")
    if not isinstance(delegate, Mapping):
        return None, False
    content = delegate.get("content")
    if not isinstance(content, str):
        return None, False
    return content, bool(delegate.get("omitted_by_policy"))
