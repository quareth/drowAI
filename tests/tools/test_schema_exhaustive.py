"""Exhaustive schema validation tests for registered tool argument models."""

import pytest

from agent.tools.tool_registry import available_tools, get_tool

from tests.tools.validation.schema_validator import SchemaValidator


def _discover_tools():
    tools = []
    for tool_id in available_tools():
        try:
            get_tool(tool_id)
        except Exception:
            continue
        tools.append(tool_id)
    return sorted(tools)


@pytest.mark.parametrize(
    "tool_id",
    [pytest.param(tool_id, marks=pytest.mark.tool(tool_id)) for tool_id in _discover_tools()],
)
def test_schema_exhaustive(tool_id: str) -> None:
    tool_cls = get_tool(tool_id)
    validator = SchemaValidator()
    report = validator.validate_tool(tool_id, tool_cls)
    assert report.all_passed(), report.failures()


def test_schema_validator_uses_fixture_full_args_for_amass_dns_contract() -> None:
    tool_cls = get_tool("information_gathering.dns.amass")
    validator = SchemaValidator()

    report = validator.validate_tool("information_gathering.dns.amass", tool_cls)

    assert report.results["build_command_full"]["passed"] is True
