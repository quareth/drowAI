"""Validate structural pre-gate checks for memory extraction service."""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[4]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if "core" not in sys.modules:
    core_pkg = types.ModuleType("core")
    core_pkg.__path__ = [str((ROOT_DIR / "core").resolve())]
    sys.modules["core"] = core_pkg

from backend.services.memory.memory_extraction import MemoryExtractionService


def _service() -> MemoryExtractionService:
    return MemoryExtractionService(
        memory_store=object(),  # not used by _structural_check
        gate_client=object(),  # not used by _structural_check
        extraction_client=object(),  # not used by _structural_check
    )


def test_skip_empty_user_message() -> None:
    assert _service()._structural_check("", "assistant response") is False


def test_skip_short_user_message() -> None:
    assert _service()._structural_check("ok", "assistant response") is False


def test_skip_empty_assistant_response() -> None:
    assert _service()._structural_check("this is substantial", "") is False


def test_pass_substantive_exchange() -> None:
    assert _service()._structural_check("this is substantial", "assistant response") is True


def test_skip_whitespace_only() -> None:
    assert _service()._structural_check("   ", "assistant response") is False


def test_skip_pure_tool_output_block() -> None:
    assistant_response = """```text
PORT     STATE SERVICE
22/tcp   open  ssh
443/tcp  open  https
```"""
    assert _service()._structural_check("please continue enumeration", assistant_response) is False


def test_pass_when_tool_output_has_explanatory_prose() -> None:
    assistant_response = """I found two open services worth focusing on.

PORT     STATE SERVICE
22/tcp   open  ssh
443/tcp  open  https
"""
    assert _service()._structural_check("please continue enumeration", assistant_response) is True
