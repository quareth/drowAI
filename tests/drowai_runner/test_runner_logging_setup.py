"""Tests for managed runner logging redaction."""

from __future__ import annotations

import logging
from pathlib import Path

from drowai_runner import logging as runner_logging
from drowai_runner.logging import RunnerRedactingFormatter


def test_runner_formatter_redacts_secret_like_values() -> None:
    formatter = RunnerRedactingFormatter("%(message)s")
    record = logging.LogRecord(
        name="test.runner.logging",
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


def test_configure_runner_logging_writes_to_file(tmp_path: Path, monkeypatch) -> None:
    log_file = tmp_path / "runner.log"
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    monkeypatch.setattr(runner_logging, "_configured", False)

    try:
        runner_logging.configure_runner_logging("INFO", log_file=log_file)
        logging.getLogger("test.runner.file").info("runner file event")
        for handler in root.handlers:
            handler.flush()

        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        assert "test.runner.file" in content
        assert "runner file event" in content
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
        monkeypatch.setattr(runner_logging, "_configured", False)
