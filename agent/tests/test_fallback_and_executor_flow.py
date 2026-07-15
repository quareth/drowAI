"""Contract and behavior-lock tests for planner fallback and executor integration hooks."""

import asyncio
import inspect
import os
import sys
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from agent.executor import CommandExecutor, EnhancedCommandExecutor
from agent.models import Action, ActionType, ExecutionResult
from agent.reasoning.enhanced_planner import EnhancedActionPlanner
from agent.tool_runtime.timeout_policy import ToolTimeoutPlan


# ---------------------------------------------------------------------------
# Import-time callable contracts
# ---------------------------------------------------------------------------
# These tests verify that critical dependencies imported with try/except
# fallback-to-None patterns in executor.py are actually callable at import
# time.  A silent `X = None` fallback passes import but crashes at runtime
# when X is called — these tests catch that gap without mocking.
# ---------------------------------------------------------------------------

class TestExecutorImportCallableContracts:
    """Verify that executor's fallback-imported symbols are real callables, not None."""

    def test_evaluate_execution_gates_is_callable(self):
        from agent.executor import evaluate_execution_gates
        assert evaluate_execution_gates is not None, (
            "evaluate_execution_gates resolved to None — "
            "agent.execution.gates may have been deleted or failed to import"
        )
        assert callable(evaluate_execution_gates)

    def test_legacy_scan_functions_are_callable(self):
        from agent.executor import (
            execute_nmap_scan_legacy,
            parse_nmap_output_legacy,
            store_command_output_legacy,
        )
        for name, fn in [
            ("execute_nmap_scan_legacy", execute_nmap_scan_legacy),
            ("parse_nmap_output_legacy", parse_nmap_output_legacy),
            ("store_command_output_legacy", store_command_output_legacy),
        ]:
            assert fn is not None, (
                f"{name} resolved to None — "
                "agent.execution.legacy_scan may have been deleted or failed to import"
            )
            assert callable(fn)

    def test_proposal_manager_is_importable(self):
        from agent.executor import ProposalManager
        # ProposalManager is allowed to be None when interactive mode is not
        # available, but if the module exists it should be a class.
        if ProposalManager is not None:
            assert callable(ProposalManager)


class DummyConfig:
    openai_api_key = "test"
    model_name = "gpt-4"
    max_tools_per_action = 3
    default_execution_strategy = "parallel"
    enforce_llm_tool_selection = False
    llm_tool_selection_timeout = 3
    use_llm_tool_calls = True
    max_tools_exposed = 2
    tool_call_timeout = 3
    tool_timeout_default_seconds = 600
    tool_timeout_max_seconds = 600
    tool_timeout_grace_seconds = 5


class RaisingLLM:
    async def chat_with_usage(self, *a, **k):
        raise RuntimeError("simulated tool-call failure")


@pytest.fixture(autouse=True)
def mock_openai_client():
    with patch("agent.reasoning.enhanced_planner.LLMClientFactory.get_client") as mock_client:
        mock_client.return_value = MagicMock()
        yield mock_client


def _make_executor() -> EnhancedCommandExecutor:
    cfg = DummyConfig()
    cfg.workspace_path = "/workspace/task_1"
    cfg.task_id = "1"
    return EnhancedCommandExecutor(cfg, logger=MagicMock())


def test_executor_initialization_does_not_create_planner(mock_openai_client):
    executor = _make_executor()

    assert executor._enhanced_planner is None
    mock_openai_client.assert_not_called()


def test_executor_lazily_creates_legacy_openai_planner(mock_openai_client):
    executor = _make_executor()

    planner = executor.enhanced_planner

    assert planner is executor.enhanced_planner
    mock_openai_client.assert_called_once_with(api_key="test", model="gpt-4")


def test_planner_surfaces_llm_failure():
    cfg = DummyConfig()
    planner = EnhancedActionPlanner(cfg, llm_client=RaisingLLM())
    action = Action(type=ActionType.SCAN_PORTS, target="127.0.0.1", parameters={}, reasoning="", expected_outcome="")

    async def run():
        with pytest.raises(RuntimeError, match="simulated tool-call failure"):
            await planner.build_action_plan(action, {"current_phase": "enumeration"})

    asyncio.run(run())


def test_planner_surfaces_llm_failure_when_tool_calls_flag_disabled():
    cfg = DummyConfig()
    cfg.use_llm_tool_calls = False
    planner = EnhancedActionPlanner(cfg, llm_client=RaisingLLM())
    action = Action(type=ActionType.SCAN_PORTS, target="127.0.0.1", parameters={}, reasoning="", expected_outcome="")

    async def run():
        with pytest.raises(RuntimeError, match="simulated tool-call failure"):
            await planner.build_action_plan(action, {"current_phase": "enumeration"})

    asyncio.run(run())


def test_executor_contract_signatures_are_stable():
    execute_sig = inspect.signature(EnhancedCommandExecutor._execute_single_tool)
    assert list(execute_sig.parameters.keys()) == [
        "self",
        "tool_id",
        "parameters",
        "interrupt_id",
        "tool_call_id",
        "allow_pty",
        "tool_batch_id",
        "session_name",
        "cleanup_session",
        "artifact_stamp",
        "timeout_plan",
    ]
    assert execute_sig.parameters["interrupt_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert execute_sig.parameters["tool_call_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert execute_sig.parameters["allow_pty"].kind is inspect.Parameter.KEYWORD_ONLY
    assert execute_sig.parameters["tool_batch_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert execute_sig.parameters["session_name"].kind is inspect.Parameter.KEYWORD_ONLY
    assert execute_sig.parameters["cleanup_session"].kind is inspect.Parameter.KEYWORD_ONLY
    assert execute_sig.parameters["artifact_stamp"].kind is inspect.Parameter.KEYWORD_ONLY
    assert execute_sig.parameters["timeout_plan"].kind is inspect.Parameter.KEYWORD_ONLY

    approval_sig = inspect.signature(EnhancedCommandExecutor._maybe_request_approval)
    assert list(approval_sig.parameters.keys()) == [
        "self",
        "tool_name",
        "parameters",
        "reasoning",
    ]
    assert approval_sig.parameters["reasoning"].default is None

    scope_sig = inspect.signature(EnhancedCommandExecutor.set_scope_validator)
    assert list(scope_sig.parameters.keys()) == ["self", "validator"]

    file_comm_sig = inspect.signature(EnhancedCommandExecutor.set_file_comm)
    assert list(file_comm_sig.parameters.keys()) == ["self", "comm"]

    via_comm_sig = inspect.signature(EnhancedCommandExecutor._execute_tool_via_comm)
    assert list(via_comm_sig.parameters.keys()) == [
        "self",
        "tool_id",
        "args",
        "timeout_seconds",
        "log_mode",
        "include_metrics",
        "timeout_plan",
    ]
    assert via_comm_sig.parameters["timeout_seconds"].kind is inspect.Parameter.KEYWORD_ONLY
    assert via_comm_sig.parameters["log_mode"].default == "enhanced"
    assert via_comm_sig.parameters["include_metrics"].default is True
    assert via_comm_sig.parameters["timeout_plan"].kind is inspect.Parameter.KEYWORD_ONLY

    resolve_workspace_sig = inspect.signature(EnhancedCommandExecutor._resolve_workspace_path)
    assert list(resolve_workspace_sig.parameters.keys()) == ["self", "path"]

    resolve_container_sig = inspect.signature(EnhancedCommandExecutor._resolve_container_path)
    assert list(resolve_container_sig.parameters.keys()) == ["self", "path"]

    should_use_pty_sig = inspect.signature(EnhancedCommandExecutor._should_use_pty)
    assert list(should_use_pty_sig.parameters.keys()) == ["self", "tool_id", "parameters"]

    is_pty_enabled_sig = inspect.signature(EnhancedCommandExecutor._is_pty_enabled)
    assert list(is_pty_enabled_sig.parameters.keys()) == ["self"]

    tool_supports_sig = inspect.signature(EnhancedCommandExecutor._tool_supports_pty)
    assert list(tool_supports_sig.parameters.keys()) == ["self", "tool_id"]


def test_set_scope_validator_sets_executor_field():
    executor = _make_executor()
    marker = MagicMock(name="scope-validator")
    executor.set_scope_validator(marker)
    assert executor.scope_validator is marker


def test_set_file_comm_sets_executor_field():
    executor = _make_executor()
    marker = MagicMock(name="file-comm")
    executor.set_file_comm(marker)
    assert executor._file_comm is marker


@pytest.mark.asyncio
async def test_maybe_request_approval_delegates_to_centralized_gate_api():
    executor = _make_executor()
    with patch("agent.executor.evaluate_execution_gates", new_callable=AsyncMock) as mock_gates:
        mock_gates.return_value = (None, False)

        approved = await executor._maybe_request_approval(
            "shell.exec",
            {"command": "echo hi"},
            "reason",
        )

    assert approved is False
    mock_gates.assert_awaited_once()
    kwargs = mock_gates.await_args.kwargs
    assert kwargs["tool_name"] == "shell.exec"
    assert kwargs["parameters"] == {"command": "echo hi"}
    assert kwargs["reasoning"] == "reason"
    assert kwargs["check_approval"] is True


@pytest.mark.asyncio
async def test_execute_action_delegates_scope_check_to_centralized_gate_api():
    executor = _make_executor()
    blocked = ExecutionResult(False, "", "Blocked by scope validator: target denied", -1)
    action = Action(
        type=ActionType.SCAN_PORTS,
        target="10.0.0.1",
        parameters={},
        reasoning="",
        expected_outcome="",
    )

    with patch("agent.executor.evaluate_execution_gates", new_callable=AsyncMock) as mock_gates:
        mock_gates.return_value = (blocked, False)

        result = await CommandExecutor.execute_action(executor, action, context={})

    assert result is blocked
    mock_gates.assert_awaited_once()
    kwargs = mock_gates.await_args.kwargs
    assert kwargs["scope_validator"] is executor.scope_validator
    assert kwargs["command"] == action.command
    assert kwargs["target"] == action.target
    assert kwargs["check_scope"] is True


@pytest.mark.asyncio
async def test_execute_single_tool_forwards_interrupt_and_tool_call_ids():
    executor = _make_executor()
    expected = ExecutionResult(success=True, stdout="ok", stderr="", exit_code=0)
    executor._should_use_pty = MagicMock(return_value=True)
    executor._execute_single_tool_internal = AsyncMock(return_value=expected)

    result = await executor._execute_single_tool(
        "shell.exec",
        {"command": "echo hi"},
        interrupt_id="interrupt-1",
        tool_call_id="tool-call-1",
    )

    assert result is expected
    executor._execute_single_tool_internal.assert_awaited_once()
    args, kwargs = executor._execute_single_tool_internal.await_args
    assert args == ("shell.exec", {"command": "echo hi", "timeout_sec": 600})
    assert kwargs == {
        "interrupt_id": "interrupt-1",
        "tool_call_id": "tool-call-1",
        "allow_pty": True,
        "tool_batch_id": None,
        "session_name": None,
        "cleanup_session": False,
        "artifact_stamp": None,
        "timeout_plan": ANY,
    }
    assert kwargs["timeout_plan"].deadline_seconds == 600


@pytest.mark.asyncio
async def test_execute_single_tool_timeout_contract_for_non_pty():
    executor = _make_executor()
    executor._should_use_pty = MagicMock(return_value=False)
    timeout_plan = ToolTimeoutPlan(
        tool_id="shell.exec",
        deadline_seconds=0.01,
        native_timeout_seconds=1,
        normalized_parameters={"command": "sleep 1", "timeout_sec": 1},
        source="test",
    )

    async def _slow_internal(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        return ExecutionResult(success=True, stdout="late", stderr="", exit_code=0)

    executor._execute_single_tool_internal = _slow_internal

    result = await executor._execute_single_tool(
        "shell.exec",
        {"command": "sleep 1"},
        timeout_plan=timeout_plan,
    )

    assert result.success is False
    assert result.exit_code == -2
    assert "timed out" in result.stderr
