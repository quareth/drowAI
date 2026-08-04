"""Boundary tests for subagent dispatch support modules.

These tests lock the typed dispatch vocabulary and dependency direction used
by the dispatch-service decomposition before orchestration branches move.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from typing import Any, cast

import pytest

from backend.services.agent_runs.completion import AgentRunCompletion
from backend.services.agent_runs.dispatch_contracts import (
    AgentRunDispatchResult,
    AgentRunDispatchStop,
    AgentRunLaunchService,
    DispatchBatchChild,
    DispatchBatchLaunch,
    DispatchBatchLaunchFailure,
    DispatchChildSettlement,
)
from backend.services.agent_runs.dispatch_plan import PlannedAgentInvocation
from backend.services.agent_runs.parent_handoff_coordinator import ParentHandoffOutcome
from backend.tests.services.agent_runs.test_dispatch_service import _service


_FORBIDDEN_SUPPORT_IMPORTS = (
    "backend.services.agent_runs.dispatch_service",
    "backend.services.langgraph_chat.handlers",
    "backend.services.langgraph_chat.facade",
    "backend.routers",
    "backend.database",
    "backend.models",
    "agent.graph",
)


class _TerminalAwaitable:
    def __await__(self) -> Iterator[Any]:
        if False:
            yield None
        return None


def _invocation() -> PlannedAgentInvocation:
    return cast(PlannedAgentInvocation, object())


def _completion() -> AgentRunCompletion:
    return cast(AgentRunCompletion, object())


def _stop() -> AgentRunDispatchStop:
    return AgentRunDispatchStop(invocation=_invocation(), status="failed")


def _imports_for(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.append(node.module)
    return imports


def test_existing_dispatch_contract_shapes_are_preserved() -> None:
    assert [field.name for field in fields(AgentRunDispatchStop)] == [
        "invocation",
        "status",
        "usage",
    ]
    assert AgentRunDispatchStop(_invocation(), "failed").usage == ()

    assert [field.name for field in fields(AgentRunDispatchResult)] == [
        "child_completions",
        "parent_handoff_outcome",
        "stop",
    ]
    assert AgentRunDispatchResult() == AgentRunDispatchResult(
        child_completions=(),
        parent_handoff_outcome=cast(ParentHandoffOutcome | None, None),
        stop=None,
    )

    stop = _stop()
    with pytest.raises(FrozenInstanceError):
        stop.status = "cancelled"  # type: ignore[misc]
    assert not hasattr(stop, "__dict__")
    assert not hasattr(AgentRunDispatchResult(), "__dict__")


def test_launch_service_protocol_signature_is_preserved() -> None:
    signature = inspect.signature(AgentRunLaunchService.launch)
    assert list(signature.parameters) == [
        "self",
        "assignment",
        "runtime_config",
        "graph_thread_id",
        "parent_run_id",
    ]
    assert signature.parameters["parent_run_id"].default is None


def test_batch_contracts_preserve_ordered_invocations() -> None:
    first = _invocation()
    second = _invocation()
    first_child = DispatchBatchChild(first, _TerminalAwaitable())
    second_child = DispatchBatchChild(second, _TerminalAwaitable())

    launch = DispatchBatchLaunch(children=(first_child, second_child))

    assert tuple(child.invocation for child in launch.children) == (first, second)
    with pytest.raises(FrozenInstanceError):
        first_child.invocation = second  # type: ignore[misc]
    assert not hasattr(first_child, "__dict__")
    assert not hasattr(launch, "__dict__")


def test_launch_failure_contract_is_typed_not_mapping_shaped() -> None:
    failure = DispatchBatchLaunchFailure(child_completions=(_completion(),))
    stopped = DispatchBatchLaunchFailure(stop=_stop())

    assert failure.child_completions
    assert stopped.stop is not None
    assert not isinstance(failure, dict)
    assert not hasattr(failure, "__dict__")


def test_child_settlement_requires_exactly_one_outcome() -> None:
    invocation = _invocation()
    completion = _completion()
    stop = _stop()

    assert (
        DispatchChildSettlement(invocation, completion=completion).completion
        is completion
    )
    assert DispatchChildSettlement(invocation, stop=stop).stop is stop
    assert DispatchChildSettlement(invocation, paused=True).paused is True

    with pytest.raises(ValueError, match="exactly one"):
        DispatchChildSettlement(invocation)
    with pytest.raises(ValueError, match="exactly one"):
        DispatchChildSettlement(invocation, completion=completion, paused=True)
    with pytest.raises(ValueError, match="exactly one"):
        DispatchChildSettlement(invocation, completion=completion, stop=stop)


def test_dispatch_support_modules_do_not_import_facade() -> None:
    package_root = Path("backend/services/agent_runs")
    support_modules = sorted(
        path
        for path in package_root.glob("dispatch_*.py")
        if path.name != "dispatch_service.py"
    )
    assert support_modules

    for path in support_modules:
        imports = _imports_for(path)
        assert "backend.services.agent_runs.dispatch_service" not in imports
        assert "dispatch_service" not in imports


def test_dispatch_support_modules_stay_out_of_application_edges() -> None:
    package_root = Path("backend/services/agent_runs")
    support_modules = sorted(
        path
        for path in package_root.glob("dispatch_*.py")
        if path.name != "dispatch_service.py"
    )
    assert support_modules

    for path in support_modules:
        imports = _imports_for(path)
        for imported in imports:
            assert not imported.startswith(_FORBIDDEN_SUPPORT_IMPORTS), (
                f"{path} imports application edge {imported}"
            )


def test_moved_dispatch_contracts_have_single_canonical_definitions() -> None:
    package_root = Path("backend/services/agent_runs")
    expected_owners = {
        "AgentRunLaunchService": package_root / "dispatch_contracts.py",
        "ReadyHandoffProcessor": package_root / "dispatch_contracts.py",
        "DispatchStopStatus": package_root / "dispatch_contracts.py",
        "AgentRunDispatchStop": package_root / "dispatch_contracts.py",
        "AgentRunDispatchResult": package_root / "dispatch_contracts.py",
        "LifecyclePublisher": package_root / "launcher.py",
    }

    definitions: dict[str, list[Path]] = {name: [] for name in expected_owners}
    for path in sorted(package_root.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in definitions:
                    definitions[node.name].append(path)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in definitions:
                        definitions[target.id].append(path)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id in definitions:
                    definitions[node.target.id].append(path)

    for name, owner in expected_owners.items():
        assert definitions[name] == [owner]


def test_dispatch_facade_exports_only_the_retained_service() -> None:
    tree = ast.parse(Path("backend/services/agent_runs/dispatch_service.py").read_text())

    defined_names: set[str] = set()
    exported_names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            defined_names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined_names.add(target.id)
                    if target.id == "__all__" and isinstance(node.value, ast.List):
                        exported_names = [
                            item.value
                            for item in node.value.elts
                            if isinstance(item, ast.Constant)
                            and isinstance(item.value, str)
                        ]

    assert "_LaunchBatchFailure" not in defined_names
    assert exported_names == ["SubagentDispatchService"]


def test_followup_dispatcher_shares_initial_dispatch_batch_executor() -> None:
    service, _registry, _launcher = _service()
    followup_dispatcher = service._followup_dispatcher  # noqa: SLF001

    assert followup_dispatcher._batch_executor is service._batch_executor  # noqa: SLF001


def test_dispatch_facade_private_helpers_are_only_admission_policy() -> None:
    tree = ast.parse(Path("backend/services/agent_runs/dispatch_service.py").read_text())

    module_private_helpers = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("_")
        and node.name != "__all__"
    }
    service = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SubagentDispatchService"
    )
    service_private_helpers = {
        node.name
        for node in service.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("_")
    }

    assert module_private_helpers == set()
    assert service_private_helpers == {"__init__", "_active_counts_for_plan"}


def test_dispatch_facade_no_longer_owns_extracted_helper_bodies() -> None:
    tree = ast.parse(Path("backend/services/agent_runs/dispatch_service.py").read_text())
    defined_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            defined_names.add(node.name)

    assert not {
        "_completion_for_terminal_exception",
        "_existing_replayable_followup",
        "_launch_batch",
        "_publish_entry_lifecycle",
        "_safe_launch_error",
        "_settle_launched_batch_on_failure",
    } & defined_names
