import asyncio
import os
import sys

# Ensure project root on path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent.executor import EnhancedCommandExecutor, CommandExecutor
from agent.models import Action, ActionType, ExecutionResult


def test_aggregates_multiple_tool_results():
    executor = EnhancedCommandExecutor(config=type("Cfg", (), {})())

    # Mock selector to return two tools
    executor.tool_selector.select_tools_for_action = lambda action, ctx: [
        "tool_a",
        "tool_b",
    ]

    # Mock parameter generator to supply unique params
    def fake_params(tool_id, action_type, context):
        return {"param": tool_id}

    executor.parameter_generator.generate_parameters = fake_params  # type: ignore

    async def fake_execute_tools(executions):
        results = []
        for exec in executions:
            res = type(
                "Res",
                (),
                {"success": True, "stdout": exec["tool"], "stderr": ""},
            )()
            results.append({"tool": exec["tool"], "result": res})
        return results

    executor.concurrent_executor.execute_tools = fake_execute_tools  # type: ignore

    action = Action(
        type=ActionType.SCAN_PORTS,
        target="127.0.0.1",
        parameters={},
        reasoning="",
        expected_outcome="",
    )

    result = asyncio.run(executor.execute_action(action))
    assert result.success
    assert "tool_a" in result.stdout and "tool_b" in result.stdout


def test_falls_back_to_super(monkeypatch):
    executor = EnhancedCommandExecutor(config=type("Cfg", (), {})())

    # Selector returns no tools triggering fallback
    executor.tool_selector.select_tools_for_action = lambda action, ctx: []

    called = {}

    async def fake_super_execute(self, action, context=None):
        called["called"] = True
        return ExecutionResult(success=True, stdout="fallback", stderr="", exit_code=0)

    monkeypatch.setattr(CommandExecutor, "execute_action", fake_super_execute)

    action = Action(
        type=ActionType.SCAN_PORTS,
        target="127.0.0.1",
        parameters={},
        reasoning="",
        expected_outcome="",
    )

    result = asyncio.run(executor.execute_action(action))
    assert called.get("called")
    assert result.stdout == "fallback"
