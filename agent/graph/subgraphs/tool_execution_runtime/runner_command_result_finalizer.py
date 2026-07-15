"""Finalize runner command results into backend-owned tool artifacts.

This module owns the control-plane post-processing step for prepared command
transports. It enriches raw runner output with existing tool hooks, persists the
standard raw-output artifact and chunk index through existing helpers, then
materializes the generated workspace files into the runtime workspace.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from agent.tool_runtime.result_enrichment import (
    build_command_transport_tool_result,
    include_stderr_in_artifacts_for_tool,
)
from agent.tool_runtime.workspace_artifacts import (
    WorkspaceIndexWrite,
    save_and_index_tool_output_artifact_with_index_writes,
    should_persist_workspace_artifact,
)
from agent.tools.shell.contracts import ShellCommandResult
from backend.services.runtime_provider.contracts import RuntimeOperationRequest
from backend.services.runtime_provider.runtime_artifact_access import (
    runtime_artifact_wait_fields,
    runtime_artifact_wait_metadata,
)
from runtime_shared.tool_command_transport import TRANSPORT_FILE_COMM, TRANSPORT_PTY


@dataclass(frozen=True, slots=True)
class _MaterializationItem:
    """One command-owned workspace payload to materialize into the runtime."""

    path: str
    source_path: Optional[Path] = None
    content: Optional[bytes] = None
    append: bool = False
    remove_source: bool = False


@dataclass(frozen=True, slots=True)
class _RuntimeOutputValidation:
    """Validation metadata for tool-declared runtime output files."""

    artifacts: list[str]
    metadata: dict[str, Any]


async def finalize_runner_command_result(
    *,
    prepared: Any,
    delegate: Mapping[str, Any] | None,
    provider_ok: bool,
    command: str,
    artifact_stamp: Optional[int],
    timeout_policy: Mapping[str, Any],
    provider: Any,
    runtime_request: RuntimeOperationRequest,
    provider_metadata: Mapping[str, Any] | None = None,
) -> Mapping[str, Any] | None:
    """Return a delegate result enriched with backend-owned artifact behavior."""
    if not isinstance(delegate, Mapping):
        return None

    raw_delegate = dict(delegate)
    workspace = Path(str(prepared.host_workspace_path)).resolve()
    metadata = raw_delegate.get("metadata")
    existing_metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    existing_metadata.setdefault("timeout_policy", dict(timeout_policy))
    command_text = str(
        raw_delegate.get("command_text")
        or existing_metadata.get("command_text")
        or command
    )
    success = _delegate_success(raw_delegate, provider_ok=provider_ok)
    exit_code = _delegate_exit_code(raw_delegate, success=success)
    shell_result = ShellCommandResult(
        status=_delegate_shell_status(raw_delegate, success=success),
        exit_code=exit_code,
        stdout=str(raw_delegate.get("stdout") or ""),
        stderr=str(raw_delegate.get("stderr") or ""),
        duration_ms=_delegate_duration_ms(raw_delegate),
        transport=_prepared_transport(prepared),
        truncated=False,
    )

    runtime_output_validation = await _validate_declared_runtime_output_files(
        prepared=prepared,
        provider=provider,
        runtime_request=runtime_request,
        process_success=success,
    )
    if runtime_output_validation is not None:
        existing_metadata.update(runtime_output_validation.metadata)

    try:
        tool_id = str(getattr(prepared, "tool_id", "") or "")
        enriched = build_command_transport_tool_result(
            tool=prepared.tool,
            args=prepared.args,
            shell_result=shell_result,
            command=command_text,
            host_workspace_path=prepared.host_workspace_path,
            runtime_context=prepared.runtime_context,
            artifact_stamp=artifact_stamp,
            include_stderr_in_artifacts=include_stderr_in_artifacts_for_tool(tool_id),
            existing_metadata=existing_metadata,
        )
    except Exception:
        return raw_delegate

    process_stdout = str(
        getattr(enriched, "process_stdout", None) or shell_result.stdout or ""
    )
    process_stderr = str(
        getattr(enriched, "process_stderr", None) or shell_result.stderr or ""
    )

    artifact_save = save_and_index_tool_output_artifact_with_index_writes(
        workspace_path=str(workspace),
        stdout=process_stdout,
        stderr=process_stderr,
        selected_tool=str(getattr(prepared, "tool_id", "") or ""),
    )
    workspace_items = _collect_workspace_files_for_materialization(
        workspace=workspace,
        artifact_refs=getattr(enriched, "artifacts", None),
        raw_output_artifact=artifact_save.artifact_path,
        index_writes=artifact_save.index_writes,
    )
    materialized_paths, materialization_metadata = await _materialize_workspace_files(
        provider=provider,
        runtime_request=runtime_request,
        workspace=workspace,
        workspace_items=workspace_items,
    )

    enriched_metadata = getattr(enriched, "metadata", None)
    merged_metadata = (
        dict(enriched_metadata) if isinstance(enriched_metadata, Mapping) else {}
    )
    merged_metadata.setdefault("timeout_policy", dict(timeout_policy))
    if materialization_metadata:
        materialization_metadata["materialized_paths"] = list(materialized_paths)
        merged_metadata["artifact_materialization"] = materialization_metadata

    persist_runtime_output_artifacts = should_persist_workspace_artifact(
        str(getattr(prepared, "tool_id", "") or "")
    )
    result_artifacts = _merge_artifact_refs(
        raw_delegate.get("artifacts"),
        (
            runtime_output_validation.artifacts
            if runtime_output_validation is not None
            and persist_runtime_output_artifacts
            else None
        ),
        _result_artifact_refs(
            materialized_paths=materialized_paths,
            artifact_refs=getattr(enriched, "artifacts", None),
            raw_output_artifact=artifact_save.artifact_path,
            workspace=workspace,
        ),
    )
    raw_delegate.update(
        {
            "success": bool(enriched.success),
            "stdout": str(enriched.stdout or ""),
            "stderr": str(enriched.stderr or ""),
            "process_stdout": process_stdout,
            "process_stderr": process_stderr,
            "exit_code": int(enriched.exit_code),
            "status": (
                "success"
                if bool(enriched.success)
                else _delegate_status(raw_delegate, success=False)
            ),
            "metadata": merged_metadata,
            "artifacts": result_artifacts,
            "command_text": command_text,
        }
    )
    return raw_delegate


async def _validate_declared_runtime_output_files(
    *,
    prepared: Any,
    provider: Any,
    runtime_request: RuntimeOperationRequest,
    process_success: bool,
) -> _RuntimeOutputValidation | None:
    if not process_success:
        return None

    try:
        declarations = list(
            prepared.tool.runtime_output_files(
                prepared.args,
                metadata={},
            )
        )
    except Exception:
        return _RuntimeOutputValidation(
            artifacts=[],
            metadata={
                "runtime_output_validation": {
                    "status": "failed",
                    "error": "declaration_failed",
                },
                "runtime_output_files": [],
            },
        )
    if not declarations:
        return None

    query = getattr(provider, "query_runtime_artifacts", None)
    queried_items: dict[str, Mapping[str, Any]] = {}
    query_errors: list[dict[str, str]] = []
    for prefix in _runtime_output_query_prefixes(declarations):
        try:
            result = await query(_runtime_output_query_request(runtime_request, prefix=prefix))
            if not getattr(result, "ok", False):
                query_errors.append({"prefix": prefix, "reason": str(getattr(result, "error_code", None) or "query_failed")})
                continue
            for item in _runtime_output_items_from_result(result):
                relative_path = _normalize_runner_workspace_path(
                    item.get("path") or item.get("relative_path") or item.get("artifact_path")
                )
                if relative_path:
                    queried_items[relative_path] = item
        except Exception as exc:
            query_errors.append({"prefix": prefix, "reason": type(exc).__name__})

    output_entries: list[dict[str, Any]] = []
    artifact_refs: list[str] = []
    for declaration in declarations:
        relative_path = _normalize_runner_workspace_path(getattr(declaration, "relative_path", ""))
        item = queried_items.get(relative_path)
        entry: dict[str, Any] = {
            "relative_path": relative_path,
            "required": bool(getattr(declaration, "required", True)),
            "exists": item is not None,
        }
        if item is not None:
            size_bytes = _optional_int(item.get("size_bytes") if item.get("size_bytes") is not None else item.get("size"))
            content_sha256 = str(item.get("content_sha256") or "").strip().lower() or None
            entry.update(
                {
                    "size_bytes": size_bytes,
                    "content_sha256": content_sha256,
                }
            )
            artifact_refs.append(relative_path)
        output_entries.append(entry)

    missing_required = [
        entry["relative_path"]
        for entry in output_entries
        if entry.get("required") and not entry.get("exists")
    ]
    status = "failed" if missing_required or query_errors else "succeeded"
    return _RuntimeOutputValidation(
        artifacts=artifact_refs,
        metadata={
            "runtime_output_validation": {
                "status": status,
                "source": "runtime_workspace_query",
                "declared_count": len(output_entries),
                "found_count": len(artifact_refs),
                "missing_required": missing_required,
                "query_errors": query_errors,
            },
            "runtime_output_files": output_entries,
        },
    )


def _runtime_output_query_request(
    runtime_request: RuntimeOperationRequest,
    *,
    prefix: str,
) -> RuntimeOperationRequest:
    payload = {
        "prefix": prefix,
        "wait_for_result": True,
        **runtime_artifact_wait_fields(),
    }
    return RuntimeOperationRequest(
        tenant_id=runtime_request.tenant_id,
        task_id=runtime_request.task_id,
        user_id=runtime_request.user_id,
        actor_type=runtime_request.actor_type,
        actor_id=runtime_request.actor_id,
        runtime_placement_mode=runtime_request.runtime_placement_mode,
        workspace_id=runtime_request.workspace_id,
        runner_id=runtime_request.runner_id,
        execution_site_id=runtime_request.execution_site_id,
        operation="query_runtime_artifacts",
        payload=payload,
        metadata=runtime_artifact_wait_metadata(),
    )


def _runtime_output_items_from_result(result: Any) -> list[Mapping[str, Any]]:
    items: list[Mapping[str, Any]] = []
    for candidate in _metadata_candidates(getattr(result, "metadata", None)):
        raw_items = candidate.get("items")
        if not isinstance(raw_items, list):
            continue
        items.extend(item for item in raw_items if isinstance(item, Mapping))
    return items


def _metadata_candidates(metadata: Any) -> list[Mapping[str, Any]]:
    candidates: list[Mapping[str, Any]] = []
    if isinstance(metadata, Mapping):
        candidates.append(metadata)
        delegate = metadata.get("delegate_result")
        if isinstance(delegate, Mapping):
            candidates.append(delegate)
            delegate_metadata = delegate.get("metadata")
            if isinstance(delegate_metadata, Mapping):
                candidates.append(delegate_metadata)
    return candidates


def _runtime_output_query_prefixes(declarations: list[Any]) -> list[str]:
    prefixes: list[str] = []
    for declaration in declarations:
        relative_path = _normalize_runner_workspace_path(getattr(declaration, "relative_path", ""))
        prefix = relative_path
        if prefix not in prefixes:
            prefixes.append(prefix)
    return prefixes


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_runner_workspace_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if text.startswith("/workspace/"):
        text = text[len("/workspace/") :]
    return text.lstrip("/")


def _collect_workspace_files_for_materialization(
    *,
    workspace: Path,
    artifact_refs: Any,
    raw_output_artifact: Optional[str],
    index_writes: tuple[WorkspaceIndexWrite, ...] = (),
) -> list[_MaterializationItem]:
    ordered: list[_MaterializationItem] = []
    for artifact_ref in _as_list(artifact_refs):
        _append_existing_workspace_file(
            ordered,
            workspace=workspace,
            value=artifact_ref,
        )
    if raw_output_artifact:
        _append_existing_workspace_file(
            ordered,
            workspace=workspace,
            value=raw_output_artifact,
        )
    seen_paths = {item.path for item in ordered}
    for index_write in index_writes:
        relative_path = _normalize_workspace_ref(index_write.path, workspace=workspace)
        if not relative_path or not relative_path.startswith("index/"):
            continue
        if relative_path in seen_paths:
            continue
        seen_paths.add(relative_path)
        ordered.append(
            _MaterializationItem(
                path=relative_path,
                content=bytes(index_write.content),
                append=True,
                remove_source=False,
            )
        )
    return ordered


async def _materialize_workspace_files(
    *,
    provider: Any,
    runtime_request: RuntimeOperationRequest,
    workspace: Path,
    workspace_items: list[_MaterializationItem],
) -> tuple[list[str], dict[str, Any]]:
    if not workspace_items:
        return [], {}

    materialized: list[str] = []
    skipped: list[dict[str, str]] = []
    for item in workspace_items:
        relative_path = item.path
        try:
            data = item.content
            source_path = item.source_path
            if data is None:
                if source_path is None:
                    skipped.append({"path": relative_path, "reason": "source_missing"})
                    continue
                try:
                    source_path.relative_to(workspace)
                except ValueError:
                    skipped.append(
                        {"path": relative_path, "reason": "outside_workspace"}
                    )
                    continue
                if not source_path.exists() or not source_path.is_file():
                    skipped.append({"path": relative_path, "reason": "source_missing"})
                    continue
                data = source_path.read_bytes()
            write_request = _runtime_workspace_write_request(
                runtime_request=runtime_request,
                path=relative_path,
                data=data,
                append=item.append,
            )
            result = await provider.write_runtime_artifact_file(write_request)
            if result.ok:
                materialized.append(relative_path)
                if item.remove_source and source_path is not None:
                    _remove_staged_workspace_file(source_path, workspace=workspace)
            else:
                skipped.append(
                    {
                        "path": relative_path,
                        "reason": str(result.error_code or "runtime_write_failed"),
                    }
                )
        except Exception as exc:
            skipped.append({"path": relative_path, "reason": type(exc).__name__})

    status = (
        "succeeded"
        if materialized and not skipped
        else "partial"
        if materialized
        else "failed"
    )
    return materialized, {
        "status": status,
        "declared_count": len(workspace_items),
        "materialized_count": len(materialized),
        "skipped_count": len(skipped),
        "skipped": skipped[:10],
    }


def _runtime_workspace_write_request(
    *,
    runtime_request: RuntimeOperationRequest,
    path: str,
    data: bytes,
    append: bool,
) -> RuntimeOperationRequest:
    payload: dict[str, Any] = {
        "path": path,
        "content_base64": base64.b64encode(data).decode("ascii"),
        "encoding": "base64",
        **runtime_artifact_wait_fields(),
    }
    if append:
        payload["mode"] = "append"
    return RuntimeOperationRequest(
        tenant_id=runtime_request.tenant_id,
        task_id=runtime_request.task_id,
        user_id=runtime_request.user_id,
        actor_type=runtime_request.actor_type,
        actor_id=runtime_request.actor_id,
        runtime_placement_mode=runtime_request.runtime_placement_mode,
        workspace_id=runtime_request.workspace_id,
        runner_id=runtime_request.runner_id,
        execution_site_id=runtime_request.execution_site_id,
        operation="write_runtime_artifact_file",
        payload=payload,
        metadata=runtime_artifact_wait_metadata(),
    )


def _result_artifact_refs(
    *,
    materialized_paths: list[str],
    artifact_refs: Any,
    raw_output_artifact: Optional[str],
    workspace: Path,
) -> list[str]:
    refs: list[str] = []
    materialized = set(materialized_paths)
    for artifact_ref in _as_list(artifact_refs):
        relative_path = _normalize_workspace_ref(artifact_ref, workspace=workspace)
        if (
            relative_path
            and relative_path in materialized
            and relative_path.startswith("artifacts/")
        ):
            refs.append(relative_path)
    if raw_output_artifact:
        relative_path = _normalize_workspace_ref(
            raw_output_artifact,
            workspace=workspace,
        )
        if relative_path and relative_path in materialized:
            refs.append(relative_path)
    return refs


def _append_existing_workspace_file(
    ordered: list[_MaterializationItem],
    *,
    workspace: Path,
    value: Any,
) -> None:
    relative_path = _normalize_workspace_ref(value, workspace=workspace)
    if not relative_path:
        return
    path = (workspace / relative_path).resolve()
    try:
        path.relative_to(workspace)
    except ValueError:
        return
    if path.is_file() and relative_path not in {item.path for item in ordered}:
        ordered.append(
            _MaterializationItem(
                path=relative_path,
                source_path=path,
                remove_source=True,
            )
        )


def _normalize_workspace_ref(value: Any, *, workspace: Path) -> Optional[str]:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return None
    if text.startswith("/workspace/"):
        text = text[len("/workspace/") :]
    path = Path(text)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(workspace).as_posix()
        except ValueError:
            return None
    if any(part == ".." for part in path.parts):
        return None
    return path.as_posix()


def _remove_staged_workspace_file(path: Path, *, workspace: Path) -> None:
    try:
        path.relative_to(workspace)
        path.unlink(missing_ok=True)
    except Exception:
        return


def _delegate_success(delegate: Mapping[str, Any], *, provider_ok: bool) -> bool:
    explicit = delegate.get("success")
    if isinstance(explicit, bool):
        return explicit
    status = str(delegate.get("status") or "").strip().lower()
    if status in {"success", "succeeded", "completed", "ok"}:
        return True
    if status in {"failed", "error", "timeout", "timed_out", "cancelled", "canceled"}:
        return False
    return bool(provider_ok)


def _delegate_status(delegate: Mapping[str, Any], *, success: bool) -> str:
    status = str(delegate.get("status") or "").strip().lower()
    if status in {"timeout", "timed_out"}:
        return "timeout"
    if status in {"cancelled", "canceled"}:
        return "cancelled"
    return "success" if success else "error"


def _delegate_shell_status(delegate: Mapping[str, Any], *, success: bool) -> str:
    status = str(delegate.get("status") or "").strip().lower()
    if status in {"timeout", "timed_out"}:
        return "timeout"
    return "success" if success else "error"


def _delegate_exit_code(delegate: Mapping[str, Any], *, success: bool) -> int:
    try:
        return int(delegate.get("exit_code", 0 if success else 2))
    except (TypeError, ValueError):
        return 0 if success else 2


def _delegate_duration_ms(delegate: Mapping[str, Any]) -> int:
    for key in ("duration_ms", "execution_time_ms"):
        try:
            return max(0, int(delegate.get(key)))
        except (TypeError, ValueError):
            pass
    for key in ("duration", "execution_time"):
        try:
            return max(0, int(float(delegate.get(key)) * 1000))
        except (TypeError, ValueError):
            pass
    return 0


def _prepared_transport(prepared: Any) -> str:
    raw = (
        str(getattr(getattr(prepared, "args", None), "transport", "") or "")
        .strip()
        .lower()
    )
    if raw in {"direct", TRANSPORT_FILE_COMM, TRANSPORT_PTY}:
        return raw
    return TRANSPORT_FILE_COMM


def _merge_artifact_refs(*values: Any) -> list[str]:
    merged: list[str] = []
    for value in values:
        for item in _as_list(value):
            text = str(item or "").strip()
            if text and text not in merged:
                merged.append(text)
    return merged


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


__all__ = ["finalize_runner_command_result"]
