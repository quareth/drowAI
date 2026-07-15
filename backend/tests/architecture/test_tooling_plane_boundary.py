"""Tooling-plane architecture boundary tests.

Responsibilities:
- Guard against backend-local runner file-comm fallback in runner placement.
- Keep runner-placement lane authority fail-closed for container-scoped tools.
- Keep lane policy ownership centralized in backend_tool_policy.
"""

from __future__ import annotations

import pathlib
import re


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_EXECUTOR_ADAPTER_PATH = _REPO_ROOT / "agent/graph/adapters/executor_adapter.py"
_TRANSPORT_ROUTER_PATH = _REPO_ROOT / "agent/tool_runtime/transport_router.py"
_LANE_DISPATCH_PATH = (
    _REPO_ROOT / "agent/graph/subgraphs/tool_execution_runtime/lane_dispatch.py"
)


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def test_runner_placement_path_blocks_backend_local_file_comm_fallback() -> None:
    """Tooling plane lock: runner placement does not attach backend-local FileCommAgent."""
    text = _read(_EXECUTOR_ADAPTER_PATH)
    execute_tool_start = text.index("async def execute_tool(")
    get_executor_start = text.index("def _get_executor(")
    execute_tool_flow = text[execute_tool_start:get_executor_start]

    assert 'dispatch_decision.authority != "container_runner_transport"' in execute_tool_flow
    assert "if requires_local_executor:" in execute_tool_flow
    assert "_ensure_local_executor()._last_action = action" in execute_tool_flow

    get_executor_end = text.index("def _get_logger(")
    get_executor_flow = text[get_executor_start:get_executor_end]
    assert "executor.set_file_comm(FileCommAgent(workspace_path))" in get_executor_flow
    assert re.search(
        r"if\s*\(\s*workspace_path\s*and\s*FileCommAgent is not None\s*and\s*"
        r"runtime_placement_mode != RuntimePlacementMode\.RUNNER\.value\s*\)\s*:\s*"
        r"try:\s*executor\.set_file_comm\(FileCommAgent\(workspace_path\)\)",
        get_executor_flow,
        flags=re.DOTALL,
    ), (
        "FileCommAgent attachment must stay disabled for runner placement to prevent "
        "backend-local fallback."
    )


def test_runner_container_lane_rejects_direct_execution_fallback() -> None:
    """Tooling plane lock: container-scoped runner lane remains fail-closed for direct fallback."""
    text = _read(_TRANSPORT_ROUTER_PATH)
    assert "if not allows_direct_execution:" in text
    assert (
        "container-scoped tools cannot execute via direct runtime fallback" in text
    )
    assert 'selected_authority == "container_runner_transport"' in text


def test_lane_policy_remains_centralized_in_backend_tool_policy() -> None:
    """Tooling plane lock: dispatch modules consume lane policy helpers instead of redefining them."""
    lane_dispatch_text = _read(_LANE_DISPATCH_PATH)
    transport_router_text = _read(_TRANSPORT_ROUTER_PATH)

    assert (
        "from agent.tool_runtime.backend_tool_policy import ("
        in lane_dispatch_text
    )
    assert "resolve_execution_lane," in lane_dispatch_text
    assert "resolve_selected_authority," in lane_dispatch_text
    assert "def resolve_execution_lane(" not in lane_dispatch_text
    assert "_BACKEND_SCOPED_TOOL_IDS" not in lane_dispatch_text
    assert "resolve_selected_authority(" in lane_dispatch_text

    resolve_dispatch_start = lane_dispatch_text.index("def resolve_tool_lane_dispatch(")
    dispatch_call_start = lane_dispatch_text.index(
        "async def dispatch_tool_call_by_lane(",
        resolve_dispatch_start,
    )
    resolve_dispatch_body = lane_dispatch_text[resolve_dispatch_start:dispatch_call_start]
    assert re.search(
        r"authority\s*=\s*resolve_selected_authority\s*\(",
        resolve_dispatch_body,
    ), "resolve_tool_lane_dispatch must delegate authority selection to policy helper."
    assert 'if lane == "backend_scoped"' not in resolve_dispatch_body
    assert 'elif lane == "artifact_scoped"' not in resolve_dispatch_body
    assert 'placement == "runner"' not in resolve_dispatch_body

    assert "resolve_execution_lane," in transport_router_text
    assert "lane_allows_direct_execution," in transport_router_text
    assert "resolve_selected_authority," in transport_router_text
