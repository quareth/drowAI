"""Backend logging configuration and redaction helpers.

This module owns process-level logging setup for the FastAPI control plane.
It keeps configuration centralized while leaving subsystem modules on standard
``logging.getLogger(__name__)`` loggers.
"""

from __future__ import annotations

import logging
import json
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from backend.config import LOG_FORMAT, LOG_LEVEL
from runtime_shared.durable_secret_masking import mask_durable_secrets
from runtime_shared.runner_protocol import sanitize_log_message

_HANDLER_MARKER = "_drowai_backend_handler"
_FILTER_MARKER = "_drowai_redaction_filter"
_DEFAULT_REDACTION_MAX_CHARS = 20_000
_DEFAULT_LOG_MAX_BYTES = 50 * 1024 * 1024
_DEFAULT_LOG_BACKUP_COUNT = 5
_DEFAULT_LOG_FILE = Path(__file__).resolve().parents[1] / "log" / "backend.log"
_configured = False


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts secret-like values after standard formatting."""

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        masked = mask_durable_secrets(formatted, source="backend_log")
        return str(masked)


class JsonRedactingFormatter(logging.Formatter):
    """Formatter that emits redacted JSON-lines records."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        masked = mask_durable_secrets(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            source="backend_log",
        )
        return str(masked)


class RedactionFilter(logging.Filter):
    """Marker filter used to identify handlers with redaction installed."""

    def filter(self, record: logging.LogRecord) -> bool:
        return True


def configure_backend_logging(
    *,
    level: str | None = None,
    fmt: str | None = None,
    log_file: str | os.PathLike[str] | None = None,
) -> None:
    """Configure root logging for backend processes once."""

    global _configured
    if _configured:
        return

    logging.Formatter.converter = time.gmtime
    resolved_level = _coerce_level(level or LOG_LEVEL)
    formatter = _build_formatter(fmt or LOG_FORMAT)
    root = logging.getLogger()
    root.setLevel(resolved_level)

    for existing in list(root.handlers):
        root.removeHandler(existing)

    handler = _build_file_handler(log_file)
    setattr(handler, _HANDLER_MARKER, True)
    root.addHandler(handler)

    for handler in root.handlers:
        handler.setLevel(resolved_level)
        handler.setFormatter(formatter)
        if not any(getattr(existing, _FILTER_MARKER, False) for existing in handler.filters):
            redaction_filter = RedactionFilter()
            setattr(redaction_filter, _FILTER_MARKER, True)
            handler.addFilter(redaction_filter)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        framework_logger = logging.getLogger(logger_name)
        framework_logger.handlers = []
        framework_logger.propagate = True
        framework_logger.setLevel(resolved_level)

    _configured = True


def _coerce_level(value: str | int) -> int:
    if isinstance(value, int):
        return value
    return getattr(logging, str(value or "INFO").strip().upper(), logging.INFO)


def _build_formatter(fmt: str) -> logging.Formatter:
    if str(fmt or "").strip().lower() == "json":
        return JsonRedactingFormatter()
    return RedactingFormatter(fmt)


def _build_file_handler(
    log_file: str | os.PathLike[str] | None = None,
) -> RotatingFileHandler:
    path = Path(log_file or os.getenv("LOG_FILE") or _DEFAULT_LOG_FILE).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return RotatingFileHandler(
        path,
        maxBytes=_resolve_positive_int("LOG_MAX_BYTES", _DEFAULT_LOG_MAX_BYTES),
        backupCount=_resolve_positive_int("LOG_BACKUP_COUNT", _DEFAULT_LOG_BACKUP_COUNT),
        encoding="utf-8",
    )


def _resolve_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        parsed = int(str(raw or "").strip())
    except ValueError:
        parsed = default
    return parsed if parsed > 0 else default


def _resolve_redaction_max_chars() -> int:
    return _resolve_positive_int("LOG_REDACTION_MAX_CHARS", _DEFAULT_REDACTION_MAX_CHARS)


def safe_log_message(value: Any, *, max_chars: int | None = None) -> str:
    """Return a sanitized single-line message for explicit log fields."""

    return sanitize_log_message(str(value), max_chars=max_chars or _resolve_redaction_max_chars())


__all__ = [
    "RedactingFormatter",
    "configure_backend_logging",
    "safe_log_message",
]
