"""
Shared test helpers for deterministic `tool_execution` module patching.

These helpers import the real `agent.graph.subgraphs.tool_execution` module and
let tests patch module attributes directly without relying on `agent.graph`
package attribute resolution.
"""

from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Any


def load_tool_execution_module() -> ModuleType:
    """Return the imported tool_execution subgraph module."""
    return import_module("agent.graph.subgraphs.tool_execution")


def patch_tool_execution_attr(monkeypatch: Any, name: str, value: Any) -> None:
    """Patch tool execution attributes across legacy and extracted module layouts."""
    module = load_tool_execution_module()
    if hasattr(module, name):
        monkeypatch.setattr(module, name, value)
        return

    fallback_modules_by_attr = {
        "EnhancedActionPlanner": [
            "agent.graph.subgraphs.tool_execution_runtime.planner_service",
        ],
    }
    for module_path in fallback_modules_by_attr.get(name, []):
        fallback_module = import_module(module_path)
        if hasattr(fallback_module, name):
            monkeypatch.setattr(fallback_module, name, value)
            return

    monkeypatch.setattr(module, name, value, raising=False)


def build_tool_execution_metadata(
    *,
    task_id: int,
    message: str,
    conversation_id: str = "direct-tool-test-conv",
    turn_id: str = "turn-1",
    turn_sequence: int = 1,
    selected_tools: list[str] | None = None,
    tool_parameters: dict[str, Any] | None = None,
    api_key: str = "key",
    model: str = "model",
    tenant_id: int = 1,
    runtime_placement_mode: str = "local",
    workspace_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build direct-node metadata that mirrors facade context-bundle setup."""
    from agent.graph.context.builder import (
        METADATA_CONTEXT_BUNDLE_KEY,
        build_conversation_context_bundle,
    )

    selected = list(selected_tools or [])
    parameters = dict(tool_parameters or {})
    metadata = {
        "api_key": api_key,
        "model": model,
        "tenant_id": int(tenant_id),
        "runtime_placement_mode": str(runtime_placement_mode),
        "workspace_id": str(workspace_id or f"task-{task_id}"),
        "turn_id": turn_id,
        "turn_sequence": turn_sequence,
        "tool_plan_prepared": True,
        "planner_plan": {
            "selected_tools": selected,
            "tool_parameters": parameters,
            "execution_strategy": "single",
            "reasoning": "",
            "expected_outcome": "",
        },
    }
    if extra:
        metadata.update(extra)
    metadata[METADATA_CONTEXT_BUNDLE_KEY] = build_conversation_context_bundle(
        conversation_id=conversation_id,
        turn_id=turn_id,
        turn_sequence=turn_sequence,
        messages=[],
        current_message=message,
    )
    return metadata
