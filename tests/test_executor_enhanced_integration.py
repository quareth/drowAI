import asyncio
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent.executor import CommandExecutor
from agent.models import Action, ActionType, ExecutionResult


def test_executor_uses_enhanced_planner():
    executor = CommandExecutor(config=type("Cfg", (), {})())

    called = {}

    async def fake_plan(action, context):
        called["called"] = True
        return ExecutionResult(success=True, stdout="ok", stderr="", exit_code=0)

    executor.enhanced_planner.plan_and_execute_action = fake_plan  # type: ignore

    action = Action(
        type=ActionType.SCAN_PORTS,
        target="127.0.0.1",
        parameters={},
        reasoning="",
        expected_outcome="",
    )

    result = asyncio.run(executor.execute_action(action, {}))

    assert called.get("called")
    assert result.success
