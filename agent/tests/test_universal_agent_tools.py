"""Tests for the universal agent tool identifier authority."""

from __future__ import annotations

import ast
from pathlib import Path

from agent.tools.universal_agent_tools import UNIVERSAL_AGENT_TOOL_IDS


def test_universal_agent_tool_ids_are_stable_and_immutable() -> None:
    assert UNIVERSAL_AGENT_TOOL_IDS == (
        "shell.utility",
        "shell.assessment",
        "shell.write_stdin",
    )
    assert isinstance(UNIVERSAL_AGENT_TOOL_IDS, tuple)
    assert len(UNIVERSAL_AGENT_TOOL_IDS) == len(set(UNIVERSAL_AGENT_TOOL_IDS))


def test_universal_agent_tool_authority_stays_data_only() -> None:
    module_path = (
        Path(__file__).resolve().parents[1]
        / "tools"
        / "universal_agent_tools.py"
    )
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_modules = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_names = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module != "__future__"
        for alias in node.names
    }

    assert imported_modules == set()
    assert imported_names == {
        "SHELL_ASSESSMENT_TOOL_ID",
        "SHELL_UTILITY_TOOL_ID",
        "SHELL_WRITE_STDIN_TOOL_ID",
    }
    assert {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module != "__future__"
    } == {"runtime_shared.shell_capabilities"}
    assert not any(
        isinstance(node, (ast.ClassDef, ast.FunctionDef)) for node in tree.body
    )
