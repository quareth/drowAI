"""Runtime result enrichment across PTY and direct transports.

This module assembles normalized ``ExecutionResult`` values for PTY and direct
tool execution paths. Shared semantic envelope policy is delegated to
``agent.semantic.enrichment`` to avoid split semantic authorities.
"""

from __future__ import annotations

import inspect
import json
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional, Sequence

from agent.semantic.enrichment import (
    build_runtime_semantic_metadata,
    validate_semantic_evidence_entries,
)

if TYPE_CHECKING:
    from agent.models import ExecutionResult

# Tools whose stderr carries audit evidence that must be preserved in artifacts.
_STDERR_ARTIFACT_TOOL_IDS = frozenset(
    {
        "shell.exec",
        "shell.script",
        "information_gathering.network_discovery.fping",
    }
)


def include_stderr_in_artifacts_for_tool(tool_id: str) -> bool:
    """Return whether artifact creation should receive raw stderr for ``tool_id``."""
    return str(tool_id or "").strip() in _STDERR_ARTIFACT_TOOL_IDS


def merge_semantic_emitter_metadata(
    *,
    tool: Any,
    args: Any,
    stdout: str,
    stderr: str,
    exit_code: int,
    existing_metadata: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build runtime metadata via shared semantic transport helpers."""
    parsed_metadata: Dict[str, Any] = {}
    try:
        parsed = tool.parse_output(stdout=stdout, stderr=stderr, exit_code=exit_code, args=args)
        if isinstance(parsed, dict):
            parsed_metadata = parsed
    except Exception:
        parsed_metadata = {}
    legacy_semantic_evidence_raw = parsed_metadata.pop("semantic_evidence", None)

    metadata_for_emitter = build_runtime_semantic_metadata(
        parsed_metadata=parsed_metadata,
        semantic_observations=None,
        existing_metadata=existing_metadata,
    )

    semantic_observations: list[Any] | None = None
    try:
        emitted_observations = tool.emit_semantic_observations(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            args=args,
            metadata=dict(metadata_for_emitter),
        )
        if isinstance(emitted_observations, list) and emitted_observations:
            semantic_observations = emitted_observations
    except Exception:
        pass

    emitted_evidence: Any = None
    try:
        emitted_evidence = tool.emit_semantic_evidence(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            args=args,
            metadata=build_runtime_semantic_metadata(
                parsed_metadata=parsed_metadata,
                semantic_observations=semantic_observations,
                existing_metadata=existing_metadata,
            ),
        )
    except Exception:
        emitted_evidence = None

    emitted_valid, _ = validate_semantic_evidence_entries(
        _coerce_mapping_sequence(emitted_evidence)
    )
    parsed_valid, _ = validate_semantic_evidence_entries(
        _coerce_mapping_sequence(legacy_semantic_evidence_raw)
    )
    semantic_evidence: list[dict[str, Any]] | None = _merge_semantic_evidence_with_precedence(
        emitted_valid=emitted_valid,
        parsed_valid=parsed_valid,
    ) or None

    return build_runtime_semantic_metadata(
        parsed_metadata=parsed_metadata,
        semantic_observations=semantic_observations,
        semantic_evidence=semantic_evidence,
        existing_metadata=existing_metadata,
    )


def _coerce_mapping_sequence(value: Any) -> Sequence[Mapping[str, Any]] | None:
    """Return mapping sequence for semantic validators, else None."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    return value


def _merge_semantic_evidence_with_precedence(
    *,
    emitted_valid: Sequence[Mapping[str, Any]],
    parsed_valid: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Merge validated evidence with emitter precedence and validator-owned limits."""
    merged: list[dict[str, Any]] = [dict(entry) for entry in emitted_valid]
    seen = {_semantic_evidence_identity(entry) for entry in merged}
    for entry in parsed_valid:
        identity = _semantic_evidence_identity(entry)
        if identity in seen:
            continue
        seen.add(identity)
        merged.append(dict(entry))
    revalidated, _ = validate_semantic_evidence_entries(merged)
    return revalidated


def _semantic_evidence_identity(entry: Mapping[str, Any]) -> str:
    """Build stable identity used for cross-source semantic evidence dedupe."""
    return "|".join(
        (
            str(entry.get("type") or ""),
            str(entry.get("name") or ""),
            json.dumps(entry.get("value"), sort_keys=True),
            json.dumps(entry.get("detail"), sort_keys=True),
        )
    )


def build_command_transport_tool_result(
    *,
    tool: Any,
    args: Any,
    shell_result: Any,
    command: str,
    host_workspace_path: str,
    runtime_context: Optional[Any] = None,
    include_stderr_in_artifacts: bool = False,
    artifact_stamp: Optional[int] = None,
    existing_metadata: Optional[Dict[str, Any]] = None,
) -> "ExecutionResult":
    """Build the canonical command-transport ``ExecutionResult`` for a tool invocation."""
    try:
        from ..models import ExecutionResult
        from .runtime_context import bind_tool_runtime_context
        from ..tools.execution_outcome import detect_hard_cli_failure, is_hard_cli_exit_code
        from ..tools.utils import attach_execution_result_extras
        from ..utils.workspace_helpers import temporary_cwd
    except Exception:  # pragma: no cover
        from agent.models import ExecutionResult
        from agent.tool_runtime.runtime_context import bind_tool_runtime_context
        from agent.tools.execution_outcome import detect_hard_cli_failure, is_hard_cli_exit_code
        from agent.tools.utils import attach_execution_result_extras
        from agent.utils.workspace_helpers import temporary_cwd

    raw_stdout = str(getattr(shell_result, "stdout", "") or "")
    raw_stderr = str(getattr(shell_result, "stderr", "") or "")
    raw_exit_code = int(getattr(shell_result, "exit_code", 0) or 0)
    hard_cli_failure = is_hard_cli_exit_code(raw_exit_code) or detect_hard_cli_failure(
        stdout=raw_stdout,
        stderr=raw_stderr,
    )

    if hard_cli_failure:
        metadata = dict(existing_metadata or {})
        metadata.setdefault("execution_outcome", "failed")
    else:
        with bind_tool_runtime_context(runtime_context), temporary_cwd(host_workspace_path):
            metadata = merge_semantic_emitter_metadata(
                tool=tool,
                args=args,
                stdout=raw_stdout,
                stderr=raw_stderr,
                exit_code=raw_exit_code,
                existing_metadata=existing_metadata,
            )

    stdout_for_result = raw_stdout
    stderr_for_result = raw_stderr
    process_stdout_for_result = raw_stdout
    process_stderr_for_result = raw_stderr
    if hard_cli_failure:
        stdout_for_result = "" if not raw_stderr else raw_stdout
        stderr_for_result = raw_stderr or raw_stdout
    else:
        try:
            render_process_output_fn = getattr(tool, "render_process_output", None)
            if callable(render_process_output_fn):
                rendered_process = render_process_output_fn(
                    args=args,
                    stdout=raw_stdout,
                    stderr=raw_stderr,
                )
                if isinstance(rendered_process, tuple) and len(rendered_process) == 2:
                    process_stdout_for_result, process_stderr_for_result = rendered_process
        except Exception:
            pass
        try:
            render_output_fn = getattr(tool, "render_result_output", None)
            if callable(render_output_fn):
                rendered = render_output_fn(
                    args=args,
                    stdout=raw_stdout,
                    stderr=raw_stderr,
                )
                if isinstance(rendered, tuple) and len(rendered) == 2:
                    stdout_for_result, stderr_for_result = rendered
        except Exception:
            pass

    success = tool.is_success_exit_code(
        raw_exit_code,
        args,
        stdout=raw_stdout,
        stderr=raw_stderr,
        parsed_metadata=metadata,
    )
    exit_code = raw_exit_code
    postprocessed_metadata = metadata
    postprocessed_artifacts: list[Any] = []
    try:
        with bind_tool_runtime_context(runtime_context):
            post = tool.postprocess_execution(
                args=args,
                stdout=stdout_for_result,
                stderr=stderr_for_result,
                exit_code=exit_code,
                success=success,
                metadata=dict(metadata or {}),
                artifacts=[],
                runtime_context=runtime_context,
            )
        success = bool(post.success)
        exit_code = int(post.exit_code)
        stdout_for_result = post.stdout
        stderr_for_result = post.stderr
        postprocessed_metadata = dict(post.metadata or {})
        postprocessed_artifacts = list(post.artifacts or [])
    except Exception:
        pass

    artifact_kwargs: Dict[str, Any] = {
        "stdout": process_stdout_for_result,
        "args": args,
    }
    if include_stderr_in_artifacts:
        artifact_kwargs["stderr"] = process_stderr_for_result
    if artifact_stamp is not None and _call_accepts_keyword(tool.create_artifacts, "timestamp"):
        artifact_kwargs["timestamp"] = artifact_stamp

    created_artifacts: list[Any] = []
    with bind_tool_runtime_context(runtime_context), temporary_cwd(host_workspace_path):
        try:
            created_artifacts = list(tool.create_artifacts(**artifact_kwargs) or [])
        except Exception:
            created_artifacts = []

    if postprocessed_artifacts or created_artifacts:
        merged_artifacts: list[Any] = []
        seen: set[str] = set()
        for artifact in [*postprocessed_artifacts, *created_artifacts]:
            key = repr(artifact)
            if key in seen:
                continue
            seen.add(key)
            merged_artifacts.append(artifact)
        postprocessed_artifacts = merged_artifacts

    result = ExecutionResult(
        success=success,
        stdout=stdout_for_result,
        stderr=stderr_for_result,
        exit_code=exit_code,
    )
    attach_execution_result_extras(
        result,
        metadata=postprocessed_metadata,
        artifacts=postprocessed_artifacts,
        command_text=command,
    )
    # Process output is usually the raw shell stream; tools that may emit reusable
    # secrets can opt in to a sanitized process stream before artifact persistence.
    try:
        setattr(result, "process_stdout", str(process_stdout_for_result or ""))
        setattr(result, "process_stderr", str(process_stderr_for_result or ""))
    except Exception:
        pass
    return result


def build_pty_tool_result(
    *,
    tool: Any,
    args: Any,
    shell_result: Any,
    command: str,
    host_workspace_path: str,
    runtime_context: Optional[Any] = None,
    include_stderr_in_artifacts: bool = False,
    artifact_stamp: Optional[int] = None,
    existing_metadata: Optional[Dict[str, Any]] = None,
) -> "ExecutionResult":
    """Compatibility wrapper for PTY callers using command-transport enrichment."""
    return build_command_transport_tool_result(
        tool=tool,
        args=args,
        shell_result=shell_result,
        command=command,
        host_workspace_path=host_workspace_path,
        runtime_context=runtime_context,
        include_stderr_in_artifacts=include_stderr_in_artifacts,
        artifact_stamp=artifact_stamp,
        existing_metadata=existing_metadata,
    )


def _call_accepts_keyword(callable_obj: Any, keyword: str) -> bool:
    """Return whether ``callable_obj`` can accept ``keyword`` as a kwarg."""
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return keyword in signature.parameters


def enrich_direct_execution_result(
    *,
    tool_id: str,
    parameters: Dict[str, Any],
    result: Any,
) -> "ExecutionResult":
    """Normalize a direct ``ToolResult`` into ``ExecutionResult`` with metadata passthrough."""
    try:
        from ..models import ExecutionResult
    except Exception:  # pragma: no cover
        from agent.models import ExecutionResult

    execution_result = ExecutionResult(
        success=result.success,
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
    )

    direct_metadata = result.metadata or {}
    try:
        from agent.tools.tool_registry import get_tool

        tool_cls = get_tool(tool_id)
        tool = tool_cls()
        args = tool.args_model(**parameters)
        direct_metadata = merge_semantic_emitter_metadata(
            tool=tool,
            args=args,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            existing_metadata=direct_metadata,
        )
    except Exception:
        if not isinstance(direct_metadata, dict):
            direct_metadata = {}

    try:
        setattr(execution_result, "metadata", direct_metadata)
    except Exception:
        pass

    try:
        from agent.tools.utils import resolve_command_text_for_execution

        command_text = resolve_command_text_for_execution(
            tool_id,
            parameters,
            direct_metadata if isinstance(direct_metadata, dict) else None,
        )
        if isinstance(command_text, str) and command_text.strip():
            setattr(execution_result, "command_text", command_text)
    except Exception:
        pass

    if getattr(result, "validation_errors", None):
        try:
            setattr(execution_result, "validation_errors", result.validation_errors)
        except Exception:
            pass
    if getattr(result, "artifacts", None):
        try:
            setattr(execution_result, "artifacts", list(result.artifacts))
        except Exception:
            pass

    return execution_result


__all__ = [
    "build_command_transport_tool_result",
    "build_pty_tool_result",
    "enrich_direct_execution_result",
    "include_stderr_in_artifacts_for_tool",
    "merge_semantic_emitter_metadata",
]
