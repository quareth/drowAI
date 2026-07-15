"""Tests for backend logging setup and redaction behavior."""

from __future__ import annotations

import logging
from pathlib import Path

from backend.core import logging as backend_logging
from backend.core.logging import RedactingFormatter, safe_log_message


def test_redacting_formatter_masks_bearer_tokens() -> None:
    formatter = RedactingFormatter("%(levelname)s:%(message)s")
    record = logging.LogRecord(
        name="test.backend.logging",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="authorization: Bearer TOP_SECRET_TOKEN",
        args=(),
        exc_info=None,
    )

    formatted = formatter.format(record)

    assert "TOP_SECRET_TOKEN" not in formatted
    assert "<DURABLE_SECRET_MASK:token>" in formatted


def test_safe_log_message_normalizes_and_redacts() -> None:
    message = safe_log_message("token=abc12345\nnext line", max_chars=200)

    assert "abc12345" not in message
    assert "\n" not in message
    assert "<redacted>" in message


def test_configure_backend_logging_writes_to_file(tmp_path: Path, monkeypatch) -> None:
    log_file = tmp_path / "backend.log"
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    monkeypatch.setattr(backend_logging, "_configured", False)

    try:
        backend_logging.configure_backend_logging(level="INFO", log_file=log_file)
        logging.getLogger("test.backend.file").info("backend file event")
        for handler in root.handlers:
            handler.flush()

        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        assert "test.backend.file" in content
        assert "backend file event" in content
        assert not any(
            isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, logging.FileHandler)
            for handler in root.handlers
        )
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in original_handlers:
            root.addHandler(handler)
        root.setLevel(original_level)
        monkeypatch.setattr(backend_logging, "_configured", False)
