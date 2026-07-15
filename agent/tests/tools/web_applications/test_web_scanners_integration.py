import pytest

from agent.tools.web_applications.web_vulnerability_scanners.sqlmap import (
    SqlmapArgs,
    SqlmapTool,
)
from agent.tools.web_applications.web_vulnerability_scanners.nikto import (
    NiktoArgs,
    NiktoTool,
)
from agent.tools.web_applications.web_vulnerability_scanners.wapiti import (
    WapitiArgs,
    WapitiTool,
)
from agent.tools.web_applications.web_vulnerability_scanners.skipfish import (
    SkipfishArgs,
    SkipfishTool,
)
from agent.tools.web_applications.web_vulnerability_scanners.commix import (
    CommixArgs,
    CommixTool,
)
from agent.tools.web_applications.web_vulnerability_scanners.xsser import (
    XsserArgs,
    XsserTool,
)


@pytest.mark.parametrize(
    "tool_class,args_class,target",
    [
        (SqlmapTool, SqlmapArgs, "http://example.com"),
        (NiktoTool, NiktoArgs, "http://example.com"),
        (WapitiTool, WapitiArgs, "http://example.com"),
        (SkipfishTool, SkipfishArgs, "http://example.com"),
        (CommixTool, CommixArgs, "http://example.com"),
        (XsserTool, XsserArgs, "http://example.com"),
    ],
)
def test_execution_model_compliance(tool_class, args_class, target):
    """Verify all scanner tools implement execution model methods."""
    tool = tool_class()
    args = args_class(target=target)

    assert hasattr(tool, "build_command")
    command = tool.build_command(args)
    assert isinstance(command, list)
    assert command

    assert hasattr(tool, "parse_output")
    metadata = tool.parse_output("", "", 0, args)
    assert isinstance(metadata, dict)

    assert hasattr(tool, "create_artifacts")
    artifacts = tool.create_artifacts("", args)
    assert isinstance(artifacts, list)


