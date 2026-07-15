"""Regression tests for agent runtime configuration loading and validation."""

import os
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.config import AgentConfig, PLANNER_TOOL_CALL_TIMEOUT_SEC
from core.llm import (
    LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC,
    LLM_TIMEOUT_PLANNER_TOOL_SELECTION_SEC,
)


def test_load_from_env_requires_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError):
        AgentConfig.load_from_env()


def test_load_from_env_defaults(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    cfg = AgentConfig.load_from_env()
    assert cfg.openai_api_key == "key"
    assert cfg.model_name == "gpt-5.2"
    assert cfg.max_concurrent_scans == 3
    assert cfg.llm_tool_selection_timeout == LLM_TIMEOUT_PLANNER_TOOL_SELECTION_SEC == 120
    assert cfg.tool_call_timeout == PLANNER_TOOL_CALL_TIMEOUT_SEC == LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC == 120
    assert cfg.shell_exec_max_command_chars == 320


def test_load_from_env_shell_exec_length_override(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("SHELL_EXEC_MAX_COMMAND_CHARS", "420")
    cfg = AgentConfig.load_from_env()
    assert cfg.shell_exec_max_command_chars == 420


def test_load_from_env_planner_tool_call_timeout_override(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC", "75")
    cfg = AgentConfig.load_from_env()
    assert cfg.tool_call_timeout == 75


def test_load_from_env_planner_tool_call_legacy_fallback(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.delenv("LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC", raising=False)
    monkeypatch.setenv("PLANNER_TOOL_CALL_TIMEOUT_SEC", "76")
    cfg = AgentConfig.load_from_env()
    assert cfg.tool_call_timeout == 76


def test_load_from_env_planner_tool_selection_timeout_override(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("LLM_TIMEOUT_PLANNER_TOOL_SELECTION_SEC", "77")
    cfg = AgentConfig.load_from_env()
    assert cfg.llm_tool_selection_timeout == 77


def test_validate_limits(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    cfg = AgentConfig.load_from_env()
    cfg.max_tokens = 50
    with pytest.raises(ValueError):
        cfg.validate()
    cfg.max_tokens = 200
    cfg.tool_paths["nmap"] = "fake/tool"

    def fake_exists(path):
        return False if path == "fake/tool" else True

    monkeypatch.setattr(os.path, "exists", fake_exists)
    with pytest.raises(FileNotFoundError):
        cfg.validate()


def test_max_committed_tools_per_batch_default_is_three_after_phase_7():
    """Phase 7 Task 7.7: validator commit cap default is now 3."""
    from agent.config import AgentConfig

    cfg = AgentConfig()
    assert cfg.max_committed_tools_per_batch == 3


def test_emit_batch_events_for_single_call_default_is_true_after_phase_7():
    """Phase 7 Task 7.7: tool_batch_start/end fire even for single-call batches."""
    from agent.config import AgentConfig

    cfg = AgentConfig()
    assert cfg.emit_batch_events_for_single_call is True


def test_parallel_execution_enabled_default_is_true_after_phase_8():
    """Phase 8 Task 8.1: parallel BatchExecutor branch is enabled by default."""
    from agent.config import AgentConfig

    cfg = AgentConfig()
    assert cfg.parallel_execution_enabled is True
