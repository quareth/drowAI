"""Shared tool execution utilities for validation, normalization, and aggregation."""

from __future__ import annotations

import re
import shlex
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError

from .schemas import ToolResult
from agent.models import ExecutionResult
from .exceptions import ToolValidationError
from .tool_registry import get_tool, tool_exists


_MASSCAN_EXTRA_PARAM_SUGGESTIONS: Dict[str, str] = {
    "max_retries": "Use 'retries' instead of 'max_retries'",
    "interface": "Use 'adapter' instead of 'interface'",
    "source_ip": "Use 'adapter_ip' instead of 'source_ip'",
    "banner": "Use 'banners' instead of 'banner'",
    "ping": "Use 'host_discovery' instead of 'ping' (default|ping_only|no_ping)",
}


def safe_inc_metric(name: str, value: int = 1) -> None:
    """Best-effort metrics increment helper used across executor flows."""
    try:
        from backend.services.metrics.utils import safe_inc

        safe_inc(name, value)
    except Exception:
        pass


def _safe_inc_metric(name: str, value: int = 1) -> None:
    """Backward-compatible private alias for local call sites."""
    safe_inc_metric(name, value)


def _sanitize_metric_suffix(raw: str) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in (raw or "unknown"))
    while "__" in safe:
        safe = safe.replace("__", "_")
    return safe.strip("_") or "unknown"


def generate_fix_suggestion(error: Dict[str, Any]) -> str:
    """Return a helpful fix suggestion based on a validation error."""
    loc = ".".join(str(x) for x in error.get("loc", []))
    err_type = error.get("type", "")

    if err_type.startswith("missing"):
        return f"Provide a value for '{loc}'"
    if err_type.startswith("type_error"):
        expected = err_type.split(".")[-1]
        return f"Ensure '{loc}' is of type {expected}"
    if err_type in {"value_error.enum", "enum", "literal_error"}:
        allowed = ", ".join(str(v) for v in error.get("ctx", {}).get("enum_values", []))
        return f"Use one of allowed values for '{loc}': {allowed}"
    if err_type == "extra_forbidden":
        if loc in _MASSCAN_EXTRA_PARAM_SUGGESTIONS:
            return _MASSCAN_EXTRA_PARAM_SUGGESTIONS[loc]
        return f"Remove unsupported parameter '{loc}'"
    if err_type.startswith("value_error") and "greater_than" in err_type:
        limit = error.get("ctx", {}).get("gt")
        return f"Use a value greater than {limit} for '{loc}'"
    if err_type == "greater_than_equal":
        limit = error.get("ctx", {}).get("ge")
        return f"Use a value greater than or equal to {limit} for '{loc}'"
    if err_type == "less_than_equal":
        limit = error.get("ctx", {}).get("le")
        return f"Use a value less than or equal to {limit} for '{loc}'"

    return f"Check value for '{loc}'"


def sanitize_command_text(command: str) -> str:
    """Redact credential-bearing shell arguments before logging or persistence."""
    if not command:
        return command

    try:
        tokens = shlex.split(command)
    except Exception:
        redacted = re.sub(r"(?i)(\bsshpass\s+-p\s+)([^\s]+)", r"\1<REDACTED>", command)
        command = redacted
        redacted = re.sub(r"(?i)(--user\s+)([^\s]+)", r"\1<REDACTED>", command)
        redacted = re.sub(r"(?i)(--oauth2-bearer\s+)([^\s]+)", r"\1<REDACTED>", redacted)
        redacted = re.sub(r"(?i)(--pass\s+)([^\s]+)", r"\1<REDACTED>", redacted)
        redacted = re.sub(r"(?i)(https?://)([^/\s:@]+):([^@\s]+)@", r"\1<REDACTED>@", redacted)
        return redacted

    redacted_flags = {"--user", "-u", "--oauth2-bearer", "--pass", "--proxy-user"}
    header_prefixes = {
        "authorization:",
        "proxy-authorization:",
        "cookie:",
        "set-cookie:",
        "x-api-key:",
        "x-auth-token:",
    }
    sanitized: list[str] = []
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        lower = token.lower()
        if lower == "sshpass" and idx + 2 < len(tokens) and tokens[idx + 1] == "-p":
            sanitized.extend([token, tokens[idx + 1], "<REDACTED>"])
            idx += 3
            continue
        if lower in redacted_flags:
            sanitized.append(token)
            if idx + 1 < len(tokens):
                sanitized.append("<REDACTED>")
                idx += 2
                continue
            idx += 1
            continue

        if lower in {"--header", "-h", "-H"}:
            sanitized.append(token)
            if idx + 1 < len(tokens):
                header_value = tokens[idx + 1]
                header_lower = header_value.lower().strip()
                if any(header_lower.startswith(prefix) for prefix in header_prefixes):
                    key = (
                        header_value.split(":", 1)[0].strip()
                        if ":" in header_value
                        else header_value.strip()
                    )
                    sanitized.append(f"{key}: <REDACTED>")
                else:
                    sanitized.append(header_value)
                idx += 2
                continue
            idx += 1
            continue

        token = re.sub(r"(?i)^(https?://)([^/\s:@]+):([^@\s]+)@", r"\1<REDACTED>@", token)
        sanitized.append(token)
        idx += 1

    return shlex.join(sanitized)


def attach_execution_result_extras(
    result: ExecutionResult,
    *,
    metadata: Optional[Dict[str, Any]] = None,
    artifacts: Optional[List[Any]] = None,
    command_text: Optional[str] = None,
) -> None:
    """Attach optional execution fields without affecting control flow."""
    try:
        if metadata is not None:
            setattr(result, "metadata", metadata)
        if artifacts is not None:
            setattr(result, "artifacts", artifacts)
        if isinstance(command_text, str) and command_text.strip():
            setattr(result, "command_text", sanitize_command_text(command_text))
    except Exception:
        pass


def build_validation_error_execution_result(
    validation_errors: List[Dict[str, Any]],
) -> ExecutionResult:
    """Build canonical validation-error ExecutionResult payload."""
    details = []
    for err in validation_errors:
        suggestion = (
            f" (suggestion: {err.get('suggested_fix')})"
            if err.get("suggested_fix")
            else ""
        )
        details.append(
            f"{err.get('field', 'arguments')}: {err.get('message') or err.get('error')}{suggestion}"
        )
    stderr_message = "; ".join(details) if details else "Input validation failed"
    result = ExecutionResult(
        success=False,
        stdout="",
        stderr=f"Validation error: {stderr_message}",
        exit_code=-1,
    )
    attach_execution_result_extras(
        result,
        metadata={
            "error_type": "validation_error",
            "validation_errors": validation_errors,
        },
    )
    try:
        setattr(result, "validation_errors", validation_errors)
    except Exception:
        pass
    return result


def build_domain_validation_error_tool_result(exc: Exception) -> ToolResult:
    """Build canonical ToolResult for domain-level validation errors."""
    message = str(exc).strip() or "Invalid arguments"
    return ToolResult(
        success=False,
        exit_code=-1,
        stdout="",
        stderr=message,
        validation_errors=[],
        artifacts=[],
        metadata={"error_type": "validation_error"},
        execution_time=0.0,
    )


def extract_command_text_from_metadata(metadata: Optional[Dict[str, Any]]) -> Optional[str]:
    """Best-effort extraction of command text from result metadata."""
    if not isinstance(metadata, dict):
        return None

    candidate_maps: List[Dict[str, Any]] = [metadata]
    candidate_maps.extend(value for value in metadata.values() if isinstance(value, dict))

    for candidate in candidate_maps:
        for key in ("command_text", "command", "cmd"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def resolve_command_text_for_execution(
    tool_id: str,
    parameters: Optional[Dict[str, Any]],
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Resolve best-effort command text for artifact provenance and UI display."""
    from shlex import join as shlex_join

    command_from_metadata = extract_command_text_from_metadata(metadata)
    if command_from_metadata:
        return sanitize_command_text(command_from_metadata)

    params = parameters if isinstance(parameters, dict) else {}
    for key in ("command", "cmd", "binary"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return sanitize_command_text(value.strip())

    if not tool_id:
        return None

    try:
        tool_cls = get_tool(tool_id)
        tool = tool_cls()
        args_model = getattr(tool, "args_model", None)
        if args_model is None:
            return None
        args = args_model(**params)
        command_parts = tool.build_command(args)
        if isinstance(command_parts, (list, tuple)):
            normalized_parts = [str(part) for part in command_parts if part is not None]
            if normalized_parts:
                return sanitize_command_text(shlex_join(normalized_parts))
    except Exception:
        return None

    return None


def sanitize_for_file_comm(value: Any, *, drop_none_dict_keys: bool = True) -> Any:
    """Convert values to JSON/file-comm-safe data (enum values, recursive containers)."""
    try:
        import enum
        if isinstance(value, enum.Enum):
            return value.value
    except Exception:
        pass

    if isinstance(value, list):
        return [sanitize_for_file_comm(v, drop_none_dict_keys=drop_none_dict_keys) for v in value]
    if isinstance(value, tuple):
        return [sanitize_for_file_comm(v, drop_none_dict_keys=drop_none_dict_keys) for v in value]
    if isinstance(value, dict):
        sanitized = {
            k: sanitize_for_file_comm(v, drop_none_dict_keys=drop_none_dict_keys)
            for k, v in value.items()
        }
        if drop_none_dict_keys:
            sanitized = {k: v for k, v in sanitized.items() if v is not None}
        return sanitized
    return value


def validate_and_execute_tool(
    tool: Any,
    data: Dict[str, Any],
) -> ToolResult:
    """Validate tool input and execute, returning a structured result."""
    # Treat explicit nulls as "omitted". This reduces LLM over-specification noise
    # (e.g., {"timeout": null}) and prevents type errors in args models.
    #
    # Note: This is intentionally shallow; tool schemas are flat in practice.
    data = {k: v for k, v in (data or {}).items() if v is not None}

    module = tool.__class__.__module__
    if module.startswith("agent.tools"):
        name = module.split("agent.tools.", 1)[1]
        if not tool_exists(name):
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr=f"Tool '{name}' not available",
                artifacts=[],
                metadata={},
                execution_time=0.0,
            )

    try:
        args = tool.args_model(**data)
        return tool.run(args)
    except ValidationError as e:
        is_masscan = module.endswith("information_gathering.network_discovery.masscan")
        if is_masscan:
            _safe_inc_metric("masscan_validation_error_total")
            for raw_error in e.errors():
                reason = _sanitize_metric_suffix(str(raw_error.get("type", "unknown")))
                _safe_inc_metric(f"masscan_validation_error_total_{reason}")

        errors = [
            ToolValidationError(
                field=".".join(str(x) for x in err["loc"]),
                error=err["msg"],
                suggested_fix=generate_fix_suggestion(err),
            )
            for err in e.errors()
        ]

        # Construct a descriptive stderr message so upstream components
        # can distinguish validation issues from tool runtime stderr.
        error_details = []
        for err in errors:
            suggestion = f" (suggestion: {err.suggested_fix})" if err.suggested_fix else ""
            error_details.append(f"{err.field}: {err.error}{suggestion}")
        stderr_message = "; ".join(error_details) if error_details else "Input validation failed"

        metadata = {
            "error_type": "validation_error",
            "validation_errors": [err.model_dump() for err in errors],
        }

        return ToolResult(
            success=False,
            exit_code=-1,
            stdout="",
            stderr=f"Validation error: {stderr_message}",
            validation_errors=[err.model_dump() for err in errors],
            artifacts=[],
            metadata=metadata,
            execution_time=0.0,
        )
    except ValueError as exc:
        return build_domain_validation_error_tool_result(exc)


def aggregate_tool_results(
    results: List[Dict[str, Any]], include_findings: bool = False
) -> Tuple[ExecutionResult, List[Dict[str, Any]]]:
    """Aggregate multiple tool outputs into a single ExecutionResult.

    Parameters
    ----------
    results:
        A list of dictionaries with keys ``tool`` and ``result`` where ``result``
        is either a ToolResult-like object or an Exception.
    include_findings:
        When True, extract structured findings from tool metadata if present.

    Returns
    -------
    (ExecutionResult, List[dict])
        Combined execution result and an optional list of collected findings.
    """

    all_stdout: List[str] = []
    all_stderr: List[str] = []
    collected_findings: List[Dict[str, Any]] = []
    success = False

    for entry in results:
        tool_id = entry.get("tool")
        tool_result = entry.get("result")

        if isinstance(tool_result, Exception):
            all_stderr.append(f"Tool {tool_id} failed: {str(tool_result)}")
            continue

        if hasattr(tool_result, "success"):
            success = success or bool(getattr(tool_result, "success", False))
            stdout = getattr(tool_result, "stdout", "")
            stderr = getattr(tool_result, "stderr", "")
            if stdout:
                all_stdout.append(f"[{tool_id}] {stdout}")
            if stderr:
                all_stderr.append(f"[{tool_id}] {stderr}")

            if include_findings:
                metadata = getattr(tool_result, "metadata", None)
                if isinstance(metadata, dict):
                    # Direct findings if provided
                    findings = metadata.get("findings") or []
                    if isinstance(findings, list):
                        collected_findings.extend(findings)
                    # Convert open_ports metadata into findings
                    open_ports = metadata.get("open_ports") or []
                    for p in open_ports:
                        try:
                            port = int(p.get("port"))
                            protocol = str(p.get("protocol", "tcp"))
                        except Exception:
                            continue
                        service = str(p.get("service", ""))
                        collected_findings.append(
                            {
                                "service": service or "unknown",
                                "port": port,
                                "protocol": protocol,
                                "name": service or f"port_{port}_{protocol}",
                            }
                        )

    return (
        ExecutionResult(
            success=success,
            stdout="\n".join(all_stdout),
            stderr="\n".join(all_stderr),
            exit_code=0 if success else -1,
        ),
        collected_findings,
    )
