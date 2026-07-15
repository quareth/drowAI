"""Regression tests for graph artifact persistence helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from agent.graph.subgraphs.tool_execution_runtime.artifact_and_provenance import (
    save_execution_artifact,
    should_skip_backend_execution_artifact_save,
)


def test_save_execution_artifact_resolves_runner_task_workspace(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[dict[str, Any]] = []

    def _fake_save_tool_output_artifact(**kwargs: Any) -> str:
        calls.append(dict(kwargs))
        return "artifacts/tool_output.txt"

    monkeypatch.setattr(
        "backend.config.workspace_config.WorkspaceConfig.ensure_workspace_structure",
        lambda task_id: tmp_path / f"task-{task_id}",
    )
    facts = SimpleNamespace(task_id=34, metadata={})
    interactive = SimpleNamespace(trace=SimpleNamespace(reasoning=[]))
    outcome = SimpleNamespace(
        tool_id="information_gathering.network_discovery.nmap",
        result={"stdout": "nmap output", "stderr": ""},
    )

    artifact_path = save_execution_artifact(
        outcome=outcome,
        tool_name="information_gathering.network_discovery.nmap",
        workspace_path=None,
        facts=facts,
        interactive=interactive,
        save_tool_output_artifact_fn=_fake_save_tool_output_artifact,
        safe_inc_fn=lambda _name: None,
        logger=SimpleNamespace(debug=lambda *_args, **_kwargs: None),
    )

    assert artifact_path == "artifacts/tool_output.txt"
    assert calls == [
        {
            "workspace_path": str(tmp_path / "task-34"),
            "stdout": "nmap output",
            "stderr": "",
            "logger": None,
        }
    ]
    assert facts.metadata["workspace_path"] == str(tmp_path / "task-34")


def test_should_skip_backend_execution_artifact_save_when_runner_materialized() -> None:
    outcome = SimpleNamespace(
        result={
            "metadata": {
                "artifact_materialization": {
                    "status": "succeeded",
                    "materialized_count": 2,
                }
            }
        }
    )
    assert should_skip_backend_execution_artifact_save(outcome=outcome) is True


def test_save_execution_artifact_skips_backend_mirror_when_runner_materialized(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[dict[str, Any]] = []

    def _fake_save_tool_output_artifact(**kwargs: Any) -> str:
        calls.append(dict(kwargs))
        return "artifacts/tool_output.txt"

    monkeypatch.setattr(
        "backend.config.workspace_config.WorkspaceConfig.ensure_workspace_structure",
        lambda task_id: tmp_path / f"task-{task_id}",
    )
    facts = SimpleNamespace(task_id=48, metadata={})
    interactive = SimpleNamespace(trace=SimpleNamespace(reasoning=[]))
    outcome = SimpleNamespace(
        tool_id="information_gathering.network_discovery.nmap",
        result={
            "stdout": "nmap output",
            "stderr": "",
            "metadata": {
                "artifact_materialization": {
                    "status": "succeeded",
                    "materialized_count": 1,
                }
            },
        },
    )

    artifact_path = save_execution_artifact(
        outcome=outcome,
        tool_name="information_gathering.network_discovery.nmap",
        workspace_path=None,
        facts=facts,
        interactive=interactive,
        save_tool_output_artifact_fn=_fake_save_tool_output_artifact,
        safe_inc_fn=lambda _name: None,
        logger=SimpleNamespace(debug=lambda *_args, **_kwargs: None),
    )

    assert artifact_path is None
    assert calls == []
