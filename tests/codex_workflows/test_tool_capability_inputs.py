"""Tests for the read-only DrowAI tool capability-analysis contracts."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    REPO_ROOT
    / ".codex/skills/drowai-tool-capability-analysis/scripts"
    / "collect_tool_capability_inputs.py"
)
STATE_PATH = (
    REPO_ROOT
    / ".codex/agents/drowai-tool-capability-analysis-state.example.md"
)
SKILL_ROOT = REPO_ROOT / ".codex/skills/drowai-tool-capability-analysis"


def _load_collector():
    spec = importlib.util.spec_from_file_location("tool_capability_collector", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Registry:
    @staticmethod
    def available_tools() -> list[str]:
        return ["category.example.tool"]

    @staticmethod
    def get_tool_metadata(tool_id: str) -> dict[str, object]:
        assert tool_id == "category.example.tool"
        return {
            "args_schema": {
                "type": "object",
                "required": ["target"],
                "properties": {
                    "target": {"type": "string"},
                    "timeout": {"type": "integer", "default": 30},
                },
            }
        }


class _Visibility:
    @staticmethod
    def is_tool_visible_in_catalog(tool_id: str) -> bool:
        return tool_id == "category.example.tool"


class _Specs:
    @staticmethod
    def build_function_tool_spec_for(tool_id: str) -> SimpleNamespace:
        assert tool_id == "category.example.tool"
        return SimpleNamespace(
            name="tool__category_example_tool",
            parameters_schema={
                "type": "object",
                "required": ["target"],
                "properties": {
                    "target": {"type": "string"},
                    "_builder_intent": {"type": "string"},
                },
            },
        )


def test_collector_reports_execution_and_planner_schema_without_writes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    collector = _load_collector()
    monkeypatch.chdir(tmp_path)

    payload = collector.collect_tool_capability_inputs(
        "category.example.tool",
        registry=_Registry,
        visibility=_Visibility,
        tool_specs=_Specs,
    )

    assert payload["registered"] is True
    assert payload["llm_visible"] is True
    assert payload["execution_schema"]["required"] == ["target"]
    assert payload["planner_schema"]["required"] == ["target"]
    assert list(tmp_path.iterdir()) == []


def test_unknown_tool_fails_closed_with_bounded_diagnostic() -> None:
    collector = _load_collector()

    payload = collector.collect_tool_capability_inputs(
        "category.missing.tool",
        registry=_Registry,
        visibility=_Visibility,
        tool_specs=_Specs,
    )

    assert payload["registered"] is False
    assert payload["execution_schema"] is None
    assert payload["diagnostics"] == ["tool_not_registered"]


def test_cli_requires_exact_tool_id() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "--tool-id" in result.stderr
    assert "Traceback" not in result.stderr


def test_state_contract_contains_all_analysis_dimensions() -> None:
    content = STATE_PATH.read_text(encoding="utf-8")

    for field in (
        "registry:",
        "execution_schema:",
        "planner_function_spec:",
        "executor_runtime:",
        "result_semantics_artifacts:",
        "semantic_observations:",
        "deterministic_compression:",
        "ptr_projection:",
        "knowledge:",
        "tests_docs:",
        "llm_visible:",
    ):
        assert field in content


def test_capability_skill_is_codex_canonical_and_has_no_todos() -> None:
    content = "\n".join(
        path.read_text(encoding="utf-8")
        for path in SKILL_ROOT.rglob("*")
        if path.is_file() and path.suffix in {".md", ".py", ".toml", ".yaml"}
    )

    assert ".cursor" not in content
    assert "[TODO" not in content
    assert "budget_rendered_items" in content
    assert "Amass" in content
