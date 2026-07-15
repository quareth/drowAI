"""Managed runner logging configuration and redaction helpers.

This module keeps runner process logging on standard Python loggers while
reusing the shared runtime redaction vocabulary.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from runtime_shared.durable_secret_masking import mask_durable_secrets

_HANDLER_MARKER = "_drowai_runner_handler"
_DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DEFAULT_LOG_FILE = Path(__file__).resolve().parent / "log" / "runner.log"
_DEFAULT_LOG_MAX_BYTES = 50 * 1024 * 1024
_DEFAULT_LOG_BACKUP_COUNT = 5
_configured = False


class RunnerRedactingFormatter(logging.Formatter):
    """Formatter that redacts secret-like values from runner log output."""

    def format(self, record: logging.LogRecord) -> str:
        masked = mask_durable_secrets(super().format(record), source="runner_log")
        return str(masked)


def configure_runner_logging(
    level: str | int = "INFO",
    *,
    log_file: str | os.PathLike[str] | None = None,
) -> None:
    """Configure runner process logging once."""

    global _configured
    if _configured:
        return

    logging.Formatter.converter = time.gmtime
    resolved_level = _coerce_level(level)
    root = logging.getLogger()
    root.setLevel(resolved_level)

    for existing in list(root.handlers):
        root.removeHandler(existing)

    file_handler = _build_file_handler(log_file)
    stream_handler = logging.StreamHandler(sys.stdout)
    setattr(file_handler, _HANDLER_MARKER, True)
    setattr(stream_handler, _HANDLER_MARKER, True)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    formatter = RunnerRedactingFormatter(_DEFAULT_FORMAT)
    for handler in root.handlers:
        handler.setLevel(resolved_level)
        handler.setFormatter(formatter)

    _configured = True


def _coerce_level(value: str | int) -> int:
    if isinstance(value, int):
        return value
    return getattr(logging, str(value or "INFO").strip().upper(), logging.INFO)


def _build_file_handler(
    log_file: str | os.PathLike[str] | None = None,
) -> RotatingFileHandler:
    path = Path(
        log_file or os.getenv("DROWAI_RUNNER_LOG_FILE") or _DEFAULT_LOG_FILE
    ).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return RotatingFileHandler(
        path,
        maxBytes=_resolve_positive_int(
            "DROWAI_RUNNER_LOG_MAX_BYTES",
            _DEFAULT_LOG_MAX_BYTES,
        ),
        backupCount=_resolve_positive_int(
            "DROWAI_RUNNER_LOG_BACKUP_COUNT",
            _DEFAULT_LOG_BACKUP_COUNT,
        ),
        encoding="utf-8",
    )


def _resolve_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        parsed = int(str(raw or "").strip())
    except ValueError:
        parsed = default
    return parsed if parsed > 0 else default


__all__ = ["configure_runner_logging"]
