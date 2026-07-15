from agent.tools import (
    BaseTool,
    BaseToolArgs,
    ToolResult,
    validate_and_execute_tool,
)


class EchoTool(BaseTool):
    args_model = BaseToolArgs

    def run(self, args: BaseToolArgs) -> ToolResult:
        return ToolResult(
            success=True,
            exit_code=0,
            stdout=args.target,
            stderr="",
            artifacts=[],
            metadata={},
            execution_time=0.0,
        )


def test_successful_validation():
    tool = EchoTool()
    result = validate_and_execute_tool(tool, {"target": "example.com"})
    assert result.success
    assert result.stdout == "example.com"
    assert result.exit_code == 0


def test_failed_validation():
    tool = EchoTool()
    result = validate_and_execute_tool(tool, {})
    assert not result.success
    assert result.exit_code == -1
    assert result.validation_errors


def test_fix_suggestion_message():
    tool = EchoTool()
    result = validate_and_execute_tool(tool, {})
    msg = result.validation_errors[0]["suggested_fix"]
    assert "Provide a value" in msg
