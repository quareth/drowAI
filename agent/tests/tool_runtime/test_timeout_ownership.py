"""Ownership tests for runtime tool execution timeout policy wiring."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]

RUNTIME_TIMEOUT_PATHS = (
    "agent/executor.py",
    "agent/communication/file_comm.py",
    "agent/graph/adapters/executor_adapter.py",
    "agent/graph/subgraphs/tool_execution_runtime",
    "agent/tool_runtime",
    "kali_executor",
)

ALLOWED_FILES = {
    "agent/config.py",
    "agent/tool_runtime/timeout_policy.py",
}

FORBIDDEN_LEGACY_TIMEOUT_FIELDS = (
    "tool_execution_timeout",
    "individual_tool_timeout",
    "concurrent_execution_timeout",
    "nmap_timeout",
    "command_timeout",
)


def _iter_runtime_python_files() -> list[Path]:
    files: list[Path] = []
    for rel_path in RUNTIME_TIMEOUT_PATHS:
        path = REPO_ROOT / rel_path
        if path.is_file() and path.suffix == ".py":
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(path.rglob("*.py")))
    return files


def test_runtime_code_does_not_read_legacy_timeout_fields_directly():
    offenders: list[str] = []
    for path in _iter_runtime_python_files():
        rel_path = path.relative_to(REPO_ROOT).as_posix()
        if rel_path in ALLOWED_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        for field_name in FORBIDDEN_LEGACY_TIMEOUT_FIELDS:
            if field_name in text:
                offenders.append(f"{rel_path}: {field_name}")

    assert offenders == []
