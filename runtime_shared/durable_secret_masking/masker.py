"""Recursive masking for app-owned durable payloads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .detectors import detect_durable_secret_spans
from runtime_shared.shell_capabilities import SHELL_SESSION_TOOL_IDS

MASK_PREFIX = "<DURABLE_SECRET_MASK"
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "authorization",
        "cookie",
        "password",
        "passwd",
        "pwd",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "id_token",
        "auth_token",
        "token",
        "secret",
        "private_key",
        "credential",
        "session_id",
        "command_parameter",
        "auth.argument",
    }
)
_SAFE_MARKERS = frozenset({"<KEY_SET>", "<NO_KEY>", "<REDACTED>", "***"})
_SENSITIVE_PROOF_CONTEXT_TOKENS = frozenset(
    {
        "auth",
        "authorization",
        "bearer",
        "command_parameter",
        "credential",
        "password",
        "passwd",
        "secret",
        "token",
    }
)
_RUNNER_CONTROL_SOURCES_ALLOWING_TERMINAL_SESSION_IDS = frozenset(
    {
        "runner_inbound_message",
        "runner_outbound_message",
        "runtime_job_payload",
        "runtime_job_payload_transition",
        "runtime_job_result",
        "runtime_job_runner_event_result",
    }
)
_SHELL_RESULT_SOURCES_ALLOWING_PUBLIC_SESSION_IDS = frozenset(
    {
        "last_tool_result",
        "last_tool_result_compact",
        "last_tool_result_compact_batch",
    }
)
_SHELL_RESULT_TOOL_IDS = frozenset(
    {*SHELL_SESSION_TOOL_IDS, "shell_exec", "shell_write_stdin"}
)
_PUBLIC_SHELL_SESSION_ID_PREFIX = "shs_"
_TERMINAL_MESSAGE_TYPES = frozenset(
    {
        "terminal_open",
        "terminal_input",
        "terminal_resize",
        "terminal_close",
        "terminal_result",
        "terminal_frame",
    }
)
_TERMINAL_OPERATIONS = frozenset({"open", "input", "resize", "close"})
_TERMINAL_OPERATION_NAMES = frozenset(
    {
        "open_terminal_session",
        "send_terminal_input",
        "read_terminal_output",
        "resize_terminal_session",
        "close_terminal_session",
    }
)


def mask_durable_secrets(value: Any, *, source: str | None = None) -> Any:
    """Return ``value`` with reusable secrets masked for durable storage."""

    return _mask_value(
        value,
        parent_key="",
        path=(),
        source=_normalize_context(source),
        terminal_context=False,
        shell_result_context=False,
    )


def _mask_value(
    value: Any,
    *,
    parent_key: str,
    path: tuple[str, ...],
    source: str,
    terminal_context: bool,
    shell_result_context: bool,
) -> Any:
    if isinstance(value, Mapping):
        next_terminal_context = terminal_context or _is_runner_terminal_mapping(
            value=value,
            source=source,
        )
        next_shell_result_context = shell_result_context or _is_shell_session_result_mapping(
            value=value,
            source=source,
        )
        masked = {
            str(key): _mask_value(
                child,
                parent_key=str(key),
                path=(*path, str(key)),
                source=source,
                terminal_context=next_terminal_context,
                shell_result_context=next_shell_result_context,
            )
            for key, child in value.items()
        }
        return _mask_secret_exposure_proof_mapping(masked, parent_key=parent_key)
    if isinstance(value, list):
        return [
            _mask_value(
                item,
                parent_key=parent_key,
                path=path,
                source=source,
                terminal_context=terminal_context,
                shell_result_context=shell_result_context,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _mask_value(
                item,
                parent_key=parent_key,
                path=path,
                source=source,
                terminal_context=terminal_context,
                shell_result_context=shell_result_context,
            )
            for item in value
        )
    if isinstance(value, str):
        if _should_preserve_public_shell_session_id(
            value=value,
            parent_key=parent_key,
            shell_result_context=shell_result_context,
        ):
            return value
        if _should_preserve_terminal_session_id(
            parent_key=parent_key,
            source=source,
            terminal_context=terminal_context,
        ):
            return value
        if _is_positional_secret(path, value):
            return _placeholder("secret")
        if _is_sensitive_key(parent_key) and not _is_safe_marker(value):
            return _mask_text(value) if _contains_detectable_secret(value) else _placeholder("secret")
        return _mask_text(value)
    return value


def _mask_text(text: str) -> str:
    matches = detect_durable_secret_spans(text)
    if not matches:
        return text
    pieces: list[str] = []
    cursor = 0
    for match in matches:
        pieces.append(text[cursor : match.start])
        pieces.append(_placeholder(match.kind))
        cursor = match.end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _contains_detectable_secret(text: str) -> bool:
    return bool(detect_durable_secret_spans(text))


def _is_sensitive_key(key: str) -> bool:
    normalized = str(key or "").strip().lower()
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _should_preserve_terminal_session_id(
    *,
    parent_key: str,
    source: str,
    terminal_context: bool,
) -> bool:
    if not terminal_context:
        return False
    if source not in _RUNNER_CONTROL_SOURCES_ALLOWING_TERMINAL_SESSION_IDS:
        return False
    return _normalize_context(parent_key) == "session_id"


def _should_preserve_public_shell_session_id(
    *,
    value: str,
    parent_key: str,
    shell_result_context: bool,
) -> bool:
    if not shell_result_context:
        return False
    if _normalize_context(parent_key) != "session_id":
        return False
    return value.startswith(_PUBLIC_SHELL_SESSION_ID_PREFIX)


def _is_shell_session_result_mapping(*, value: Mapping[str, Any], source: str) -> bool:
    if source not in _SHELL_RESULT_SOURCES_ALLOWING_PUBLIC_SESSION_IDS:
        return False
    tool_id = _normalize_context(value.get("tool") or value.get("tool_id"))
    if tool_id not in _SHELL_RESULT_TOOL_IDS:
        return False
    return any(
        key in value
        for key in (
            "process_status",
            "stdin_available",
            "exit_code",
            "stdout",
            "stderr",
            "summary",
            "error_code",
        )
    )


def _is_positional_secret(path: tuple[str, ...], value: str) -> bool:
    normalized_path = tuple(_normalize_context(part) for part in path)
    if "env" in normalized_path:
        return True
    if normalized_path and normalized_path[-1] == "chars":
        return bool(value)
    return False


def _is_runner_terminal_mapping(*, value: Mapping[str, Any], source: str) -> bool:
    if source not in _RUNNER_CONTROL_SOURCES_ALLOWING_TERMINAL_SESSION_IDS:
        return False
    for key in ("message_type", "type", "operation"):
        if _normalize_context(value.get(key)) in _TERMINAL_MESSAGE_TYPES:
            return True
    if _normalize_context(value.get("terminal_operation")) in _TERMINAL_OPERATIONS:
        return True
    if _normalize_context(value.get("operation_name")) in _TERMINAL_OPERATION_NAMES:
        return True
    if (
        "session_id" in value
        and "sequence" in value
        and "stream" in value
        and "data" in value
    ):
        return True
    return False


def _is_safe_marker(value: str) -> bool:
    normalized = str(value or "").strip()
    lowered = normalized.lower()
    return (
        normalized in _SAFE_MARKERS
        or MASK_PREFIX in normalized
        or normalized.startswith(MASK_PREFIX)
        or lowered.startswith(("enc:", "encrypted:"))
    )


def _placeholder(kind: str) -> str:
    normalized = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in str(kind or "secret").lower())
    return f"{MASK_PREFIX}:{normalized}>"


def _mask_secret_exposure_proof_mapping(
    value: Mapping[str, Any],
    *,
    parent_key: str,
) -> dict[str, Any]:
    if not _looks_like_secret_exposure_mapping(value=value, parent_key=parent_key):
        return dict(value)

    proof = value.get("proof_excerpt")
    if not isinstance(proof, str) or not proof.strip() or _is_safe_marker(proof):
        return dict(value)

    masked = _mask_text(proof)
    if masked == proof and _secret_exposure_has_sensitive_context(value=value, parent_key=parent_key):
        masked = _placeholder("secret")
    if masked == proof:
        return dict(value)

    result = dict(value)
    result["proof_excerpt"] = masked
    return result


def _looks_like_secret_exposure_mapping(
    *,
    value: Mapping[str, Any],
    parent_key: str,
) -> bool:
    if "proof_excerpt" not in value:
        return False
    if _normalize_context(parent_key) == "secret_exposure":
        return True
    return any(key in value for key in ("field", "kind", "detector_id", "proof_mode", "fingerprint"))


def _secret_exposure_has_sensitive_context(
    *,
    value: Mapping[str, Any],
    parent_key: str,
) -> bool:
    contexts = [
        parent_key,
        value.get("field"),
        value.get("kind"),
        value.get("detector_id"),
        value.get("finding_subtype"),
        value.get("proof_mode"),
    ]
    normalized = " ".join(_normalize_context(item) for item in contexts)
    return (
        "secret_exposure" in normalized
        or "credential_exposure" in normalized
        or any(token in normalized for token in _SENSITIVE_PROOF_CONTEXT_TOKENS)
    )


def _normalize_context(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(".", "_").replace("/", "_")
