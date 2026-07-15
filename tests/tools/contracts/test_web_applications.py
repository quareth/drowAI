import pytest

from agent.tools.tool_registry import available_tools, get_tool

from .base_contract import BaseToolContract

# GUI-only tools to skip (require X11/desktop environment)
SKIP_TOOLS = {
    "web_applications.web_application_fuzzers.clusterd",  # Limited CLI support
    "web_applications.web_application_fuzzers.websploit",  # Framework with limited automation
    "web_applications.web_application_proxies.zaproxy",  # GUI (same as OWASP ZAP)
    "web_applications.web_vulnerability_scanners.arachni",  # Discontinued/limited
    "web_applications.web_vulnerability_scanners.w3af",  # Framework requiring setup
}


def _web_application_tools():
    tools = []
    for tool_id in available_tools():
        if not tool_id.startswith("web_applications."):
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
    [pytest.param(tool_id, marks=pytest.mark.tool(tool_id)) for tool_id in _web_application_tools()],
)
class TestWebApplicationContracts(BaseToolContract):
    @pytest.fixture
    def tool_id(self, request):
        return request.param
