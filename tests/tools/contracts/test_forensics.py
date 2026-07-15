import pytest

from agent.tools.tool_registry import available_tools, get_tool

from .base_contract import BaseToolContract


SKIP_TOOLS = {
    "forensics.forensics_carving_tools.photorec",
    "forensics.forensics_carving_tools.testdisk",
}


def _forensics_tools():
    tools = []
    for tool_id in available_tools():
        if not tool_id.startswith("forensics."):
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
    [pytest.param(tool_id, marks=pytest.mark.tool(tool_id)) for tool_id in _forensics_tools()],
)
class TestForensicsContracts(BaseToolContract):
    @pytest.fixture
    def tool_id(self, request):
        return request.param
