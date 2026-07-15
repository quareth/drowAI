import pytest

from agent.models import ActionType

from tests.tools.simulation.pipeline_simulator import PipelineSimulator


@pytest.mark.asyncio
async def test_agent_simulation_scan_ports() -> None:
    simulator = PipelineSimulator()
    result = await simulator.simulate_action(
        action_type=ActionType.SCAN_PORTS,
        target="127.0.0.1",
        context={"max_tools_per_action": 1},
    )
    assert result.tool_id
    assert result.command
    assert isinstance(result.metadata, dict)
