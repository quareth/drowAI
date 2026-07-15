from __future__ import annotations

from typing import Dict, Type

import pytest

from agent.tools.base_tool import BaseTool
from agent.tools.tool_registry import get_tool

from tests.tools.fixtures.parameter_fixtures import load_param_fixture
from tests.tools.fixtures.output_fixtures import load_output_fixture
from tests.tools.validation.schema_validator import SchemaValidator


class BaseToolContract:
    """Base contract test for penetration testing tools."""

    @pytest.fixture
    def tool_id(self) -> str:
        raise NotImplementedError

    @pytest.fixture
    def tool_cls(self, tool_id: str) -> Type[BaseTool]:
        return get_tool(tool_id)

    @pytest.fixture
    def param_fixture(self, tool_id: str) -> Dict:
        return load_param_fixture(tool_id)

    @pytest.fixture
    def output_fixture(self, tool_id: str) -> str:
        return load_output_fixture(tool_id)

    def test_schema_contract(self, tool_id: str, tool_cls: Type[BaseTool]) -> None:
        validator = SchemaValidator()
        report = validator.validate_tool(tool_id, tool_cls)
        assert report.all_passed(), report.failures()

    def test_command_contract(self, tool_cls: Type[BaseTool], param_fixture: Dict) -> None:
        tool = tool_cls()
        args_class = tool_cls.args_model
        cases = param_fixture["test_cases"]

        for case_key, case in cases.items():
            if case_key == "invalid":
                continue
            if isinstance(case, list):
                for item in case:
                    if not item.get("expected_valid", True):
                        continue
                    args_instance = args_class(**item["params"])
                    command = tool.build_command(args_instance)
                    assert isinstance(command, list)
                    assert command
                    assert all(isinstance(arg, str) for arg in command)
                continue
            if not case.get("expected_valid", True):
                continue
            args_instance = args_class(**case["params"])
            command = tool.build_command(args_instance)
            assert isinstance(command, list)
            assert command
            assert all(isinstance(arg, str) for arg in command)

    def test_parse_output_contract(
        self,
        tool_cls: Type[BaseTool],
        output_fixture: str,
        param_fixture: Dict,
    ) -> None:
        tool = tool_cls()
        args_class = tool_cls.args_model
        minimal_params = param_fixture["test_cases"]["minimal"]["params"]
        args_instance = args_class(**minimal_params)

        metadata = tool.parse_output(
            stdout=output_fixture,
            stderr="",
            exit_code=0,
            args=args_instance,
        )
        assert isinstance(metadata, dict)
