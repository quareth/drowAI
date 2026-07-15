import pytest

from agent.tools.tool_registry import available_tools, get_tool

from .base_contract import BaseToolContract


SKIP_TOOLS = {
    "reverse_engineering.debuggers.edb",
    "reverse_engineering.debuggers.ollydbg",
    "reverse_engineering.debuggers.immunity_debugger",
    "reverse_engineering.hex_editors.bless",
    "reverse_engineering.hex_editors.hexedit",
}


def _reverse_engineering_tools():
    tools = []
    for tool_id in available_tools():
        if not tool_id.startswith("reverse_engineering."):
            continue
        if tool_id in SKIP_TOOLS:
            continue
        try:
            get_tool(tool_id)
        except Exception:
            continue
        tools.append(tool_id)
    return sorted(tools)


@pytest.mark.parametrize(
    "tool_id",
    [pytest.param(tool_id, marks=pytest.mark.tool(tool_id)) for tool_id in _reverse_engineering_tools()],
)
class TestReverseEngineeringContracts(BaseToolContract):
    @pytest.fixture
    def tool_id(self, request):
        return request.param
