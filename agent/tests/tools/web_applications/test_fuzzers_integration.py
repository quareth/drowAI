import pytest

from agent.tools.enhanced_metadata_registry import get_enhanced_tool_metadata
from agent.tools.web_applications.web_application_fuzzers.ffuf import (
    FfufArgs,
    FfufTool,
)
from agent.tools.web_applications.web_application_fuzzers.wfuzz import (
    WfuzzArgs,
    WfuzzTool,
)
from agent.tools.categories import ToolCategory


def test_ffuf_supports_pty():
    assert FfufTool().supports_pty() is True


def test_ffuf_execution_model_methods():
    tool = FfufTool()
    args = FfufArgs(target="http://example.com/FUZZ", wordlist="list.txt")
    command = tool.build_command(args)
    assert isinstance(command, list) and command
    metadata = tool.parse_output("", "", 0, args)
    assert isinstance(metadata, dict)
    artifacts = tool.create_artifacts("", args)
    assert isinstance(artifacts, list)


def test_wfuzz_supports_pty():
    assert WfuzzTool().supports_pty() is True


def test_wfuzz_execution_model_methods():
    tool = WfuzzTool()
    args = WfuzzArgs(target="http://example.com", wordlist="list.txt")
    command = tool.build_command(args)
    assert isinstance(command, list) and command
    metadata = tool.parse_output("", "", 0, args)
    assert isinstance(metadata, dict)
    artifacts = tool.create_artifacts("", args)
    assert isinstance(artifacts, list)


def test_ffuf_metadata_registered():
    metadata = get_enhanced_tool_metadata("web_applications.web_application_fuzzers.ffuf")
    assert metadata is not None
    assert metadata.category == ToolCategory.WEB_FUZZING
    capability_names = {cap.name for cap in metadata.capabilities}
    assert {"parameter_fuzzing", "response_calibration"} <= capability_names


def test_wfuzz_metadata_registered():
    metadata = get_enhanced_tool_metadata("web_applications.web_application_fuzzers.wfuzz")
    assert metadata is not None
    assert metadata.category == ToolCategory.WEB_FUZZING
    capability_names = {cap.name for cap in metadata.capabilities}
    assert {"parameter_fuzzing", "response_filtering"} <= capability_names
