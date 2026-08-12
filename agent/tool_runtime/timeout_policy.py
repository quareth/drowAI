"""Central timeout policy for runtime tool execution.

This module owns the hard execution deadline used by graph, executor,
transport, and Kali file-comm paths. Tool parameters may request a deadline
through approved whole-operation fields, but the policy always clamps the
effective value to deployment configuration before a tool is dispatched.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from agent.tool_runtime.backend_tool_policy import is_runtime_session_scoped_tool
from runtime_shared.file_comm_contracts import (
    TOOL_TIMEOUT_EXIT_CODE,
    TOOL_TIMEOUT_FAILURE_CATEGORY,
)
from runtime_shared.shell_timeouts import (
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    SHELL_SESSION_CLEANUP_TIMEOUT_SEC,
    SHELL_SESSION_MAX_RUNTIME_SEC,
    SHELL_SESSION_MAX_YIELD_TIME_MS,
    SHELL_SESSION_PREPARATION_TIMEOUT_SEC,
    SHELL_SESSION_DEFAULT_TERMINAL_IO_GRACE_SEC,
    clamp_shell_runtime_sec,
    clamp_shell_yield_time_ms,
    shell_control_timeout_sec,
    shell_preparation_timeout_sec,
)
from runtime_shared.shell_capabilities import (
    SHELL_SESSION_START_TOOL_IDS,
    SHELL_WRITE_STDIN_TOOL_ID,
)

DEFAULT_TOOL_TIMEOUT_GRACE_SECONDS = 5.0
DEFAULT_SHELL_SESSION_TERMINAL_IO_GRACE_SECONDS = float(
    SHELL_SESSION_DEFAULT_TERMINAL_IO_GRACE_SEC
)

WHOLE_OPERATION_TIMEOUT_FIELDS: tuple[str, ...] = (
    "execution_timeout",
    "common_timeout",
    "max_timeout",
    "timeout_seconds",
    "timeout_sec",
    "job_max_time",
)

_CANONICAL_DEFAULT_ENV = "TOOL_TIMEOUT_DEFAULT_SECONDS"
_CANONICAL_MAX_ENV = "TOOL_TIMEOUT_MAX_SECONDS"
_CANONICAL_GRACE_ENV = "TOOL_TIMEOUT_GRACE_SECONDS"
_SHELL_SESSION_TERMINAL_IO_GRACE_ENV = "SHELL_SESSION_TERMINAL_IO_GRACE_SEC"
_LEGACY_DEFAULT_ENVS = (
    "TOOL_EXECUTION_TIMEOUT",
    "NMAP_TIMEOUT",
    "COMMAND_TIMEOUT",
)
_LEGACY_MAX_ENVS = ("CONCURRENT_EXECUTION_TIMEOUT",)


def _coerce_positive_seconds(value: Any) -> Optional[float]:
    """Return a positive seconds value or ``None`` when invalid."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def _coerce_non_negative_seconds(value: Any) -> Optional[float]:
    """Return a non-negative seconds value or ``None`` when invalid."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


def _read_first_positive_env(names: tuple[str, ...]) -> tuple[Optional[float], Optional[str]]:
    """Return the first positive env value with the env name that supplied it."""
    for name in names:
        raw = os.getenv(name)
        if raw is None:
            continue
        parsed = _coerce_positive_seconds(raw)
        if parsed is not None:
            return parsed, name
    return None, None


def _read_first_non_negative_env(
    names: tuple[str, ...],
) -> tuple[Optional[float], Optional[str]]:
    """Return the first non-negative env value with the env name that supplied it."""
    for name in names:
        raw = os.getenv(name)
        if raw is None:
            continue
        parsed = _coerce_non_negative_seconds(raw)
        if parsed is not None:
            return parsed, name
    return None, None


def _config_value(config: Any, *names: str) -> tuple[Optional[float], Optional[str]]:
    """Return the first positive config attribute with its attribute name."""
    for name in names:
        if config is None or not hasattr(config, name):
            continue
        parsed = _coerce_positive_seconds(getattr(config, name))
        if parsed is not None:
            return parsed, name
    return None, None


def _non_negative_config_value(
    config: Any, *names: str
) -> tuple[Optional[float], Optional[str]]:
    """Return the first non-negative config attribute with its attribute name."""
    for name in names:
        if config is None or not hasattr(config, name):
            continue
        parsed = _coerce_non_negative_seconds(getattr(config, name))
        if parsed is not None:
            return parsed, name
    return None, None


def _schema_fields_for_tool(tool_id: str) -> set[str]:
    """Return execution argument fields for ``tool_id`` if the tool is importable."""
    try:
        from agent.tools.tool_registry import get_tool

        tool_cls = get_tool(tool_id)
        args_model = getattr(tool_cls, "args_model", None)
        if args_model is None:
            return set()
        fields = getattr(args_model, "model_fields", None)
        if isinstance(fields, Mapping):
            return {str(name) for name in fields}
        fields = getattr(args_model, "__fields__", None)
        if isinstance(fields, Mapping):
            return {str(name) for name in fields}
    except Exception:
        return set()
    return set()


def _supports_inherited_base_timeout(tool_id: str) -> bool:
    """Return True when a tool inherits the shared execution ``timeout`` field."""
    try:
        from agent.tools.schemas import BaseToolArgs
        from agent.tools.tool_registry import get_tool

        tool_cls = get_tool(tool_id)
        args_model = getattr(tool_cls, "args_model", None)
        if args_model is None or not issubclass(args_model, BaseToolArgs):
            return False
        annotations = getattr(args_model, "__annotations__", {}) or {}
        return "timeout" not in annotations and "timeout" in _schema_fields_for_tool(tool_id)
    except Exception:
        return False


@dataclass(frozen=True, slots=True)
class ToolTimeoutConfig:
    """Configurable global tool timeout bounds."""

    default_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    max_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    grace_seconds: float = DEFAULT_TOOL_TIMEOUT_GRACE_SECONDS
    shell_session_terminal_io_grace_seconds: float = (
        DEFAULT_SHELL_SESSION_TERMINAL_IO_GRACE_SECONDS
    )
    default_source: str = "default"
    max_source: str = "default"
    grace_source: str = "default"
    shell_session_terminal_io_grace_source: str = "default"

    @classmethod
    def from_runtime_config(cls, config: Any = None) -> "ToolTimeoutConfig":
        """Build timeout config from canonical attrs/env with legacy env aliases."""
        default_seconds, default_source = _read_first_positive_env((_CANONICAL_DEFAULT_ENV,))
        if default_seconds is not None:
            default_source = str(default_source or _CANONICAL_DEFAULT_ENV)
        if default_seconds is None:
            default_seconds, default_source = _config_value(config, "tool_timeout_default_seconds")
        if default_seconds is None:
            default_seconds, default_source = _read_first_positive_env(_LEGACY_DEFAULT_ENVS)
        if default_seconds is None:
            default_seconds, default_source = _config_value(config, "tool_execution_timeout")
        if default_seconds is None:
            default_seconds, default_source = _config_value(
                config,
                "individual_tool_timeout",
                "nmap_timeout",
                "command_timeout",
            )
        if default_seconds is None:
            default_seconds = DEFAULT_TOOL_TIMEOUT_SECONDS
            default_source = "default"

        default_from_canonical_env = default_source == _CANONICAL_DEFAULT_ENV
        max_seconds, max_source = _read_first_positive_env((_CANONICAL_MAX_ENV,))
        if max_seconds is not None:
            max_source = str(max_source or _CANONICAL_MAX_ENV)
        if max_seconds is None:
            max_seconds, max_source = _config_value(config, "tool_timeout_max_seconds")
            if (
                default_from_canonical_env
                and max_seconds == DEFAULT_TOOL_TIMEOUT_SECONDS
            ):
                max_seconds = default_seconds
                max_source = "default_seconds"
        if max_seconds is None:
            max_seconds, max_source = _read_first_positive_env(_LEGACY_MAX_ENVS)
        if max_seconds is None:
            max_seconds, max_source = _config_value(config, "concurrent_execution_timeout")
        if max_seconds is None:
            max_seconds = default_seconds
            max_source = "default_seconds"
        max_seconds = max(1.0, max_seconds)

        grace_seconds, grace_source = _read_first_positive_env((_CANONICAL_GRACE_ENV,))
        if grace_seconds is not None:
            grace_source = str(grace_source or _CANONICAL_GRACE_ENV)
        if grace_seconds is None:
            grace_seconds, grace_source = _config_value(config, "tool_timeout_grace_seconds")
        if grace_seconds is None:
            grace_seconds = DEFAULT_TOOL_TIMEOUT_GRACE_SECONDS
            grace_source = "default"

        shell_session_terminal_io_grace_seconds, shell_grace_source = (
            _read_first_non_negative_env((_SHELL_SESSION_TERMINAL_IO_GRACE_ENV,))
        )
        if shell_session_terminal_io_grace_seconds is not None:
            shell_grace_source = str(
                shell_grace_source or _SHELL_SESSION_TERMINAL_IO_GRACE_ENV
            )
        if shell_session_terminal_io_grace_seconds is None:
            shell_session_terminal_io_grace_seconds, shell_grace_source = (
                _non_negative_config_value(
                    config,
                    "shell_session_terminal_io_grace_seconds",
                    "shell_session_terminal_io_grace_sec",
                    "terminal_io_grace_seconds",
                    "terminal_io_grace_sec",
                )
            )
        if shell_session_terminal_io_grace_seconds is None:
            shell_session_terminal_io_grace_seconds = (
                DEFAULT_SHELL_SESSION_TERMINAL_IO_GRACE_SECONDS
            )
            shell_grace_source = "default"

        return cls(
            default_seconds=max(1.0, min(default_seconds, max_seconds)),
            max_seconds=max_seconds,
            grace_seconds=max(0.0, grace_seconds),
            shell_session_terminal_io_grace_seconds=max(
                0.0,
                shell_session_terminal_io_grace_seconds,
            ),
            default_source=str(default_source or "default"),
            max_source=str(max_source or "default"),
            grace_source=str(grace_source or "default"),
            shell_session_terminal_io_grace_source=str(shell_grace_source or "default"),
        )


@dataclass(frozen=True, slots=True)
class ToolTimeoutPlan:
    """Resolved timeout for one concrete tool invocation."""

    tool_id: str
    deadline_seconds: float
    native_timeout_seconds: int
    normalized_parameters: dict[str, Any]
    source: str
    requested_timeout_seconds: Optional[float] = None
    requested_timeout_field: Optional[str] = None
    native_timeout_field: Optional[str] = None
    max_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    default_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    grace_seconds: float = DEFAULT_TOOL_TIMEOUT_GRACE_SECONDS
    stripped_timeout_fields: tuple[str, ...] = field(default_factory=tuple)

    def to_metadata(self) -> dict[str, Any]:
        """Return JSON-safe timeout metadata for result envelopes."""
        return {
            "tool_id": self.tool_id,
            "deadline_seconds": self.deadline_seconds,
            "native_timeout_seconds": self.native_timeout_seconds,
            "source": self.source,
            "requested_timeout_seconds": self.requested_timeout_seconds,
            "requested_timeout_field": self.requested_timeout_field,
            "native_timeout_field": self.native_timeout_field,
            "max_timeout_seconds": self.max_timeout_seconds,
            "default_timeout_seconds": self.default_timeout_seconds,
            "grace_seconds": self.grace_seconds,
            "stripped_timeout_fields": list(self.stripped_timeout_fields),
        }

    @classmethod
    def from_metadata(
        cls,
        metadata: Mapping[str, Any] | None,
        *,
        normalized_parameters: Optional[Mapping[str, Any]] = None,
    ) -> Optional["ToolTimeoutPlan"]:
        """Rehydrate a plan previously serialized with :meth:`to_metadata`."""
        if not isinstance(metadata, Mapping):
            return None
        try:
            return cls(
                tool_id=str(metadata["tool_id"]),
                deadline_seconds=float(metadata["deadline_seconds"]),
                native_timeout_seconds=int(metadata["native_timeout_seconds"]),
                normalized_parameters=dict(normalized_parameters or {}),
                source=str(metadata.get("source") or "metadata"),
                requested_timeout_seconds=(
                    float(metadata["requested_timeout_seconds"])
                    if metadata.get("requested_timeout_seconds") is not None
                    else None
                ),
                requested_timeout_field=(
                    str(metadata["requested_timeout_field"])
                    if metadata.get("requested_timeout_field")
                    else None
                ),
                native_timeout_field=(
                    str(metadata["native_timeout_field"])
                    if metadata.get("native_timeout_field")
                    else None
                ),
                max_timeout_seconds=float(
                    metadata.get("max_timeout_seconds", DEFAULT_TOOL_TIMEOUT_SECONDS)
                ),
                default_timeout_seconds=float(
                    metadata.get("default_timeout_seconds", DEFAULT_TOOL_TIMEOUT_SECONDS)
                ),
                grace_seconds=float(
                    metadata.get("grace_seconds", DEFAULT_TOOL_TIMEOUT_GRACE_SECONDS)
                ),
                stripped_timeout_fields=tuple(
                    str(field_name)
                    for field_name in metadata.get("stripped_timeout_fields", ())
                ),
            )
        except Exception:
            return None


class ToolTimeoutPolicy:
    """Resolve authoritative timeout plans for tool execution."""

    def __init__(self, config: ToolTimeoutConfig) -> None:
        self._config = config

    @classmethod
    def from_runtime_config(cls, config: Any = None) -> "ToolTimeoutPolicy":
        """Build a policy from runtime config/env."""
        return cls(ToolTimeoutConfig.from_runtime_config(config))

    def resolve(
        self,
        *,
        tool_id: str,
        parameters: Mapping[str, Any] | None,
        override_deadline_seconds: Any = None,
    ) -> ToolTimeoutPlan:
        """Compute the timeout plan for one tool invocation."""
        raw_parameters = dict(parameters or {})
        if is_runtime_session_scoped_tool(tool_id):
            return self._resolve_shell_session_timeout(
                tool_id=tool_id,
                raw_parameters=raw_parameters,
                override_deadline_seconds=override_deadline_seconds,
            )

        supported_fields = _schema_fields_for_tool(tool_id)

        requested_value = _coerce_positive_seconds(override_deadline_seconds)
        requested_field: Optional[str] = None
        source = "caller_override" if requested_value is not None else "default"

        if requested_value is None:
            for field_name in WHOLE_OPERATION_TIMEOUT_FIELDS:
                if field_name not in raw_parameters:
                    continue
                requested_value = _coerce_positive_seconds(raw_parameters.get(field_name))
                if requested_value is None:
                    continue
                requested_field = field_name
                source = f"parameter:{field_name}"
                break

        effective = requested_value if requested_value is not None else self._config.default_seconds
        effective = max(1.0, min(float(effective), self._config.max_seconds))
        native_timeout_seconds = max(1, int(math.ceil(effective)))

        native_field = self._select_native_timeout_field(
            supported_fields,
            requested_field=requested_field,
            tool_id=tool_id,
        )
        normalized = dict(raw_parameters)
        stripped_fields: list[str] = []
        for field_name in WHOLE_OPERATION_TIMEOUT_FIELDS:
            if field_name in normalized and field_name not in supported_fields:
                normalized.pop(field_name, None)
                stripped_fields.append(field_name)

        if native_field:
            normalized[native_field] = native_timeout_seconds

        return ToolTimeoutPlan(
            tool_id=str(tool_id),
            deadline_seconds=effective,
            native_timeout_seconds=native_timeout_seconds,
            normalized_parameters=normalized,
            source=source,
            requested_timeout_seconds=requested_value,
            requested_timeout_field=requested_field,
            native_timeout_field=native_field,
            max_timeout_seconds=self._config.max_seconds,
            default_timeout_seconds=self._config.default_seconds,
            grace_seconds=self._config.grace_seconds,
            stripped_timeout_fields=tuple(stripped_fields),
        )

    def _resolve_shell_session_timeout(
        self,
        *,
        tool_id: str,
        raw_parameters: Mapping[str, Any],
        override_deadline_seconds: Any = None,
    ) -> ToolTimeoutPlan:
        """Resolve bounded wait policy for one shell-session invocation."""
        caller_override = _coerce_positive_seconds(override_deadline_seconds)
        operation_max_seconds = min(
            self._config.max_seconds,
            caller_override or self._config.max_seconds,
        )
        raw_yield_ms = raw_parameters.get("yield_time_ms")
        configured_preparation_seconds = (
            shell_preparation_timeout_sec(
                tool_timeout_max_seconds=self._config.max_seconds,
            )
            if tool_id in SHELL_SESSION_START_TOOL_IDS
            else 0.0
        )
        preparation_seconds = (
            shell_preparation_timeout_sec(
                tool_timeout_max_seconds=operation_max_seconds,
            )
            if tool_id in SHELL_SESSION_START_TOOL_IDS
            else 0.0
        )
        if tool_id in SHELL_SESSION_START_TOOL_IDS and raw_yield_ms is None:
            requested_runtime_seconds = _coerce_positive_seconds(
                raw_parameters.get("max_runtime_sec")
            )
            runtime_seconds = clamp_shell_runtime_sec(
                requested_runtime_seconds,
                tool_timeout_max_seconds=operation_max_seconds,
                preparation_seconds=preparation_seconds,
            )
            deadline_seconds = preparation_seconds + runtime_seconds
            normalized = dict(raw_parameters)
            if (
                "max_runtime_sec" not in raw_parameters
                or requested_runtime_seconds is not None
            ):
                normalized["max_runtime_sec"] = runtime_seconds
            return ToolTimeoutPlan(
                tool_id=str(tool_id),
                deadline_seconds=deadline_seconds,
                native_timeout_seconds=max(1, int(math.ceil(deadline_seconds))),
                normalized_parameters=normalized,
                source=(
                    "caller_override"
                    if caller_override is not None
                    else "parameter:max_runtime_sec"
                    if requested_runtime_seconds is not None
                    else "default:max_runtime_sec"
                ),
                requested_timeout_seconds=(
                    caller_override
                    if caller_override is not None
                    else requested_runtime_seconds
                ),
                requested_timeout_field=(
                    None
                    if caller_override is not None
                    else "max_runtime_sec"
                    if requested_runtime_seconds is not None
                    else None
                ),
                native_timeout_field=None,
                max_timeout_seconds=min(
                    self._config.max_seconds,
                    configured_preparation_seconds + SHELL_SESSION_MAX_RUNTIME_SEC,
                ),
                default_timeout_seconds=(
                    configured_preparation_seconds
                    + clamp_shell_runtime_sec(
                        None,
                        tool_timeout_max_seconds=self._config.max_seconds,
                        preparation_seconds=configured_preparation_seconds,
                    )
                ),
                grace_seconds=max(
                    self._config.shell_session_terminal_io_grace_seconds,
                    SHELL_SESSION_CLEANUP_TIMEOUT_SEC,
                ),
                stripped_timeout_fields=(),
            )

        requested_yield_ms = _coerce_non_negative_seconds(raw_yield_ms)
        requested_seconds: Optional[float] = None
        requested_field: Optional[str] = None
        source = "default:yield_time_ms"
        if requested_yield_ms is not None:
            requested_seconds = requested_yield_ms / 1000.0
            requested_field = "yield_time_ms"
            source = "parameter:yield_time_ms"

        control_seconds = (
            shell_control_timeout_sec(
                tool_timeout_max_seconds=operation_max_seconds,
            )
            if tool_id == SHELL_WRITE_STDIN_TOOL_ID and bool(raw_parameters.get("chars"))
            else 0.0
        )
        reserved_seconds = preparation_seconds + control_seconds
        configured_control_seconds = (
            shell_control_timeout_sec(
                tool_timeout_max_seconds=self._config.max_seconds,
            )
            if tool_id == SHELL_WRITE_STDIN_TOOL_ID
            and bool(raw_parameters.get("chars"))
            else 0.0
        )
        configured_reserved_seconds = (
            configured_preparation_seconds + configured_control_seconds
        )
        yield_ms = clamp_shell_yield_time_ms(
            requested_yield_ms,
            reserved_seconds=reserved_seconds,
            tool_timeout_max_seconds=operation_max_seconds,
        )
        yield_seconds = yield_ms / 1000.0
        deadline_seconds = preparation_seconds + control_seconds + yield_seconds
        native_timeout_seconds = max(1, int(math.ceil(deadline_seconds)))
        normalized = dict(raw_parameters)
        if "yield_time_ms" not in raw_parameters or requested_yield_ms is not None:
            normalized["yield_time_ms"] = yield_ms
        if tool_id in SHELL_SESSION_START_TOOL_IDS:
            requested_runtime_seconds = _coerce_positive_seconds(
                raw_parameters.get("max_runtime_sec")
            )
            runtime_seconds = clamp_shell_runtime_sec(
                requested_runtime_seconds,
                tool_timeout_max_seconds=operation_max_seconds,
                preparation_seconds=preparation_seconds,
            )
            if (
                "max_runtime_sec" not in raw_parameters
                or requested_runtime_seconds is not None
            ):
                normalized["max_runtime_sec"] = runtime_seconds

        return ToolTimeoutPlan(
            tool_id=str(tool_id),
            deadline_seconds=deadline_seconds,
            native_timeout_seconds=native_timeout_seconds,
            normalized_parameters=normalized,
            source="caller_override" if caller_override is not None else source,
            requested_timeout_seconds=(
                caller_override if caller_override is not None else requested_seconds
            ),
            requested_timeout_field=(
                None if caller_override is not None else requested_field
            ),
            native_timeout_field=None,
            max_timeout_seconds=min(
                self._config.max_seconds,
                configured_reserved_seconds
                + SHELL_SESSION_MAX_YIELD_TIME_MS / 1000.0,
            ),
            default_timeout_seconds=(
                configured_reserved_seconds
                + clamp_shell_yield_time_ms(
                    None,
                    reserved_seconds=configured_reserved_seconds,
                    tool_timeout_max_seconds=self._config.max_seconds,
                )
                / 1000.0
            ),
            grace_seconds=max(
                self._config.shell_session_terminal_io_grace_seconds,
                SHELL_SESSION_CLEANUP_TIMEOUT_SEC,
            ),
            stripped_timeout_fields=(),
        )

    @staticmethod
    def _select_native_timeout_field(
        supported_fields: set[str],
        *,
        requested_field: Optional[str],
        tool_id: str,
    ) -> Optional[str]:
        if requested_field and requested_field in supported_fields:
            return requested_field
        for field_name in WHOLE_OPERATION_TIMEOUT_FIELDS:
            if field_name in supported_fields:
                return field_name
        if (
            tool_id == "information_gathering.web_enumeration.http_download"
            and "timeout" in supported_fields
        ):
            return "timeout"
        if _supports_inherited_base_timeout(tool_id):
            return "timeout"
        return None


def resolve_tool_timeout_plan(
    *,
    tool_id: str,
    parameters: Mapping[str, Any] | None,
    config: Any = None,
    override_deadline_seconds: Any = None,
) -> ToolTimeoutPlan:
    """Convenience wrapper for resolving one tool timeout plan."""
    return ToolTimeoutPolicy.from_runtime_config(config).resolve(
        tool_id=tool_id,
        parameters=parameters,
        override_deadline_seconds=override_deadline_seconds,
    )


def ensure_timeout_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a mutable metadata dict with a timeout-policy payload slot."""
    if isinstance(metadata, Mapping):
        return dict(metadata)
    return {}


__all__ = [
    "DEFAULT_SHELL_SESSION_TERMINAL_IO_GRACE_SECONDS",
    "DEFAULT_TOOL_TIMEOUT_GRACE_SECONDS",
    "DEFAULT_TOOL_TIMEOUT_SECONDS",
    "TOOL_TIMEOUT_EXIT_CODE",
    "TOOL_TIMEOUT_FAILURE_CATEGORY",
    "ToolTimeoutConfig",
    "ToolTimeoutPlan",
    "ToolTimeoutPolicy",
    "WHOLE_OPERATION_TIMEOUT_FIELDS",
    "ensure_timeout_metadata",
    "resolve_tool_timeout_plan",
]
