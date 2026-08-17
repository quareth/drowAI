"""Regression tests for graph artifact persistence helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agent.graph.subgraphs.tool_execution_runtime.artifact_and_provenance import (
    save_execution_artifact,
    should_skip_backend_execution_artifact_save,
)
from agent.tool_runtime.output_persistence_policy import resolve_output_persistence


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


@pytest.mark.parametrize(
    "tool_id",
    ["shell.utility", "shell.assessment", "shell.exec", "shell.write_stdin"],
)
def test_save_execution_artifact_never_writes_shell_output_on_backend(
    tool_id: str,
    tmp_path,
) -> None:
    calls: list[dict[str, Any]] = []
    increments: list[str] = []
    interactive = SimpleNamespace(trace=SimpleNamespace(reasoning=[]))

    artifact_path = save_execution_artifact(
        outcome=SimpleNamespace(
            tool_id=tool_id,
            result={"stdout": "transient output", "stderr": ""},
        ),
        tool_name=tool_id,
        workspace_path=str(tmp_path),
        facts=SimpleNamespace(task_id=34, metadata={}),
        interactive=interactive,
        save_tool_output_artifact_fn=lambda **kwargs: (
            calls.append(dict(kwargs)) or "artifacts/tool_output.txt"
        ),
        safe_inc_fn=increments.append,
        logger=SimpleNamespace(debug=lambda *_args, **_kwargs: None),
        persistence_decision=resolve_output_persistence(tool_id),
    )

    assert artifact_path is None
    assert calls == []
    assert increments == []
    assert interactive.trace.reasoning == []


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
