"""Regression tests for centralized runtime tool timeout planning."""

from __future__ import annotations

from types import SimpleNamespace

from pydantic import BaseModel, Field

from agent.tool_runtime.timeout_policy import (
    TOOL_TIMEOUT_EXIT_CODE,
    TOOL_TIMEOUT_FAILURE_CATEGORY,
    ToolTimeoutPolicy,
)
from agent.tools import BaseTool, BaseToolArgs, ToolResult, register_tool


class _NoTimeoutArgs(BaseModel):
    target: str


class _ExecutionTimeoutArgs(BaseModel):
    target: str
    execution_timeout: int = Field(10, ge=1)


class _BaseTimeoutArgs(BaseToolArgs):
    ports: str | None = None


class _NoTimeoutTool(BaseTool):
    args_model = _NoTimeoutArgs

    def run(self, args: _NoTimeoutArgs) -> ToolResult:
        return ToolResult(
            success=True,
            exit_code=0,
            stdout=args.target,
            stderr="",
            artifacts=[],
            metadata={},
            execution_time=0.0,
        )


class _ExecutionTimeoutTool(BaseTool):
    args_model = _ExecutionTimeoutArgs

    def run(self, args: _ExecutionTimeoutArgs) -> ToolResult:
        return ToolResult(
            success=True,
            exit_code=0,
            stdout=str(args.execution_timeout),
            stderr="",
            artifacts=[],
            metadata={},
            execution_time=0.0,
        )


class _BaseTimeoutTool(BaseTool):
    args_model = _BaseTimeoutArgs

    def run(self, args: _BaseTimeoutArgs) -> ToolResult:
        return ToolResult(
            success=True,
            exit_code=0,
            stdout=str(args.timeout),
            stderr="",
            artifacts=[],
            metadata={},
            execution_time=0.0,
        )


def _policy(default: float = 30, max_seconds: float = 60) -> ToolTimeoutPolicy:
    config = SimpleNamespace(
        tool_timeout_default_seconds=default,
        tool_timeout_max_seconds=max_seconds,
        tool_timeout_grace_seconds=2,
    )
    return ToolTimeoutPolicy.from_runtime_config(config)


def test_default_deadline_uses_global_policy_without_native_injection():
    register_tool("test.no_timeout_policy", _NoTimeoutTool)

    plan = _policy(default=25, max_seconds=60).resolve(
        tool_id="test.no_timeout_policy",
        parameters={"target": "example.org"},
    )

    assert plan.deadline_seconds == 25
    assert plan.native_timeout_field is None
    assert plan.normalized_parameters == {"target": "example.org"}


def test_requested_whole_operation_timeout_is_clamped_and_injected():
    register_tool("test.execution_timeout_policy", _ExecutionTimeoutTool)

    plan = _policy(default=25, max_seconds=40).resolve(
        tool_id="test.execution_timeout_policy",
        parameters={"target": "example.org", "execution_timeout": 90},
    )

    assert plan.deadline_seconds == 40
    assert plan.requested_timeout_field == "execution_timeout"
    assert plan.native_timeout_field == "execution_timeout"
    assert plan.normalized_parameters["execution_timeout"] == 40


def test_unsupported_whole_operation_timeout_field_is_stripped():
    register_tool("test.strip_timeout_policy", _NoTimeoutTool)

    plan = _policy(default=20, max_seconds=60).resolve(
        tool_id="test.strip_timeout_policy",
        parameters={
            "target": "example.org",
            "timeout_seconds": 10,
            "timeout": 3,
        },
    )

    assert plan.deadline_seconds == 10
    assert "timeout_seconds" not in plan.normalized_parameters
    assert plan.normalized_parameters["timeout"] == 3
    assert plan.stripped_timeout_fields == ("timeout_seconds",)


def test_plain_timeout_is_not_read_as_requested_deadline():
    register_tool("test.plain_timeout_policy", _NoTimeoutTool)

    plan = _policy(default=30, max_seconds=60).resolve(
        tool_id="test.plain_timeout_policy",
        parameters={"target": "example.org", "timeout": 2},
    )

    assert plan.deadline_seconds == 30
    assert plan.requested_timeout_field is None
    assert plan.normalized_parameters["timeout"] == 2


def test_inherited_base_timeout_is_aligned_with_policy_but_not_used_as_request():
    register_tool("test.base_timeout_policy", _BaseTimeoutTool)

    plan = _policy(default=45, max_seconds=60).resolve(
        tool_id="test.base_timeout_policy",
        parameters={"target": "example.org", "timeout": 2},
    )

    assert plan.deadline_seconds == 45
    assert plan.requested_timeout_field is None
    assert plan.native_timeout_field == "timeout"
    assert plan.normalized_parameters["timeout"] == 45


def test_timeout_metadata_shape_for_callers():
    register_tool("test.metadata_timeout_policy", _ExecutionTimeoutTool)

    plan = _policy(default=30, max_seconds=60).resolve(
        tool_id="test.metadata_timeout_policy",
        parameters={"target": "example.org", "execution_timeout": 5},
    )
    metadata = plan.to_metadata()

    assert metadata["deadline_seconds"] == 5
    assert TOOL_TIMEOUT_EXIT_CODE == -2
    assert TOOL_TIMEOUT_FAILURE_CATEGORY == "tool_timeout"


def test_canonical_env_overrides_default_agent_config_values(monkeypatch):
    register_tool("test.env_timeout_policy", _NoTimeoutTool)
    monkeypatch.setenv("TOOL_TIMEOUT_DEFAULT_SECONDS", "12")
    monkeypatch.delenv("TOOL_TIMEOUT_MAX_SECONDS", raising=False)

    plan = _policy(default=30, max_seconds=60).resolve(
        tool_id="test.env_timeout_policy",
        parameters={"target": "example.org"},
    )

    assert plan.deadline_seconds == 12
    assert plan.default_timeout_seconds == 12
    assert plan.max_timeout_seconds == 60
