"""Integration tests using mock execution.

These tests validate the full tool execution pipeline using mock
subprocess calls, allowing testing without actual tool binaries.
"""

from __future__ import annotations

from typing import Dict, List

import pytest

from agent.tools.base_tool import BaseTool
from agent.tools.tool_registry import get_tool
from agent.tools.schemas import ToolResult

from tests.tools.fixtures.parameter_fixtures import load_param_fixture
from tests.tools.integration.mock_executor import (
    MockExecutor,
    MockScenario,
    COMMON_SCENARIOS,
    IntegrationTestRunner,
    create_tool_specific_scenarios,
)


class TestMockExecution:
    """Test mock execution framework."""

    @pytest.fixture
    def executor(self) -> MockExecutor:
        exec = MockExecutor()
        yield exec
        exec.cleanup()

    def test_success_scenario(self, executor: MockExecutor) -> None:
        """Test successful execution scenario."""
        tool_cls = get_tool("information_gathering.network_discovery.nmap")
        if tool_cls is None:
            pytest.skip("nmap tool not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture("information_gathering.network_discovery.nmap")
            params = param_fixture["test_cases"]["minimal"]["params"]
        except FileNotFoundError:
            params = {"target": "192.168.1.1"}
        
        args = args_class(**params)
        result = executor.execute(tool, args, "success")
        
        assert result.success
        assert result.exit_code == 0
        assert result.stdout != ""

    def test_error_scenario(self, executor: MockExecutor) -> None:
        """Test error execution scenario."""
        tool_cls = get_tool("information_gathering.network_discovery.nmap")
        if tool_cls is None:
            pytest.skip("nmap tool not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture("information_gathering.network_discovery.nmap")
            params = param_fixture["test_cases"]["minimal"]["params"]
        except FileNotFoundError:
            params = {"target": "192.168.1.1"}
        
        args = args_class(**params)
        result = executor.execute(tool, args, "connection_error")
        
        assert not result.success
        assert result.exit_code != 0
        assert result.stderr != ""

    def test_timeout_scenario(self, executor: MockExecutor) -> None:
        """Test timeout execution scenario."""
        tool_cls = get_tool("information_gathering.network_discovery.nmap")
        if tool_cls is None:
            pytest.skip("nmap tool not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture("information_gathering.network_discovery.nmap")
            params = param_fixture["test_cases"]["minimal"]["params"]
        except FileNotFoundError:
            params = {"target": "192.168.1.1"}
        
        args = args_class(**params)
        result = executor.execute(tool, args, "timeout")
        
        assert not result.success
        assert result.exit_code == 124

    def test_execution_logging(self, executor: MockExecutor) -> None:
        """Test that executions are logged."""
        tool_cls = get_tool("information_gathering.network_discovery.nmap")
        if tool_cls is None:
            pytest.skip("nmap tool not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture("information_gathering.network_discovery.nmap")
            params = param_fixture["test_cases"]["minimal"]["params"]
        except FileNotFoundError:
            params = {"target": "192.168.1.1"}
        
        args = args_class(**params)
        
        # Execute multiple times
        executor.execute(tool, args, "success")
        executor.execute(tool, args, "timeout")
        
        assert len(executor.execution_log) == 2
        assert executor.execution_log[0]["scenario"] == "success"
        assert executor.execution_log[1]["scenario"] == "timeout"


class TestToolSpecificScenarios:
    """Test tool-specific mock scenarios."""

    def test_nmap_scenarios(self) -> None:
        """Test nmap-specific scenarios."""
        scenarios = create_tool_specific_scenarios("information_gathering.network_discovery.nmap")
        
        assert "found_hosts" in scenarios
        assert "host_down" in scenarios
        
        # Verify scenario content
        found_hosts = scenarios["found_hosts"]
        assert "192.168.1.1" in found_hosts.stdout
        assert found_hosts.exit_code == 0

    def test_hydra_scenarios(self) -> None:
        """Test hydra-specific scenarios."""
        scenarios = create_tool_specific_scenarios("password_attacks.online_attacks.hydra")
        
        assert "credentials_found" in scenarios
        assert "no_credentials" in scenarios
        
        # Verify credentials found scenario
        creds = scenarios["credentials_found"]
        assert "password" in creds.stdout.lower()
        assert creds.exit_code == 0

    def test_gobuster_scenarios(self) -> None:
        """Test gobuster-specific scenarios."""
        scenarios = create_tool_specific_scenarios("web_applications.web_crawlers.gobuster")
        
        assert "directories_found" in scenarios
        
        dirs = scenarios["directories_found"]
        assert "/admin" in dirs.stdout
        assert dirs.exit_code == 0


class TestIntegrationRunner:
    """Test the integration test runner."""

    SAMPLE_TOOLS = [
        "information_gathering.network_discovery.nmap",
        "password_attacks.online_attacks.hydra",
        "web_applications.web_crawlers.gobuster",
    ]

    @pytest.fixture
    def runner(self) -> IntegrationTestRunner:
        runner = IntegrationTestRunner()
        yield runner
        runner.cleanup()

    @pytest.mark.parametrize("tool_id", SAMPLE_TOOLS)
    def test_scenario_suite(self, tool_id: str, runner: IntegrationTestRunner) -> None:
        """Test running full scenario suite for a tool."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
            params = param_fixture["test_cases"]["minimal"]["params"]
        except FileNotFoundError:
            pytest.skip(f"No fixture for {tool_id}")
        
        args = args_class(**params)
        results = runner.run_scenario_suite(tool, args, tool_id)
        
        # Should have results for common scenarios
        assert "success" in results
        assert "connection_error" in results
        
        # Success scenario should succeed
        assert results["success"].success
        
        # Error scenario should fail
        assert not results["connection_error"].success

    def test_runner_summary(self, runner: IntegrationTestRunner) -> None:
        """Test runner summary generation."""
        tool_cls = get_tool("information_gathering.network_discovery.nmap")
        if tool_cls is None:
            pytest.skip("nmap tool not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture("information_gathering.network_discovery.nmap")
            params = param_fixture["test_cases"]["minimal"]["params"]
        except FileNotFoundError:
            params = {"target": "192.168.1.1"}
        
        args = args_class(**params)
        runner.run_scenario_suite(tool, args, "information_gathering.network_discovery.nmap")
        
        summary = runner.get_summary()
        
        assert "total_scenarios" in summary
        assert "successful" in summary
        assert "pass_rate" in summary
        assert summary["total_scenarios"] > 0


class TestErrorConditionHandling:
    """Test that tools handle error conditions gracefully."""

    SAMPLE_TOOLS = [
        "information_gathering.network_discovery.nmap",
        "information_gathering.dns.amass",
        "password_attacks.online_attacks.hydra",
        "web_applications.web_crawlers.gobuster",
    ]

    ERROR_SCENARIOS = [
        "connection_error",
        "timeout",
        "permission_denied",
        "invalid_target",
        "binary_not_found",
    ]

    @pytest.fixture
    def executor(self) -> MockExecutor:
        exec = MockExecutor()
        yield exec
        exec.cleanup()

    @pytest.mark.parametrize("tool_id", SAMPLE_TOOLS)
    @pytest.mark.parametrize("scenario", ERROR_SCENARIOS)
    def test_error_handling(self, tool_id: str, scenario: str, executor: MockExecutor) -> None:
        """Test that tools handle error scenarios gracefully."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
            params = param_fixture["test_cases"]["minimal"]["params"]
        except FileNotFoundError:
            pytest.skip(f"No fixture for {tool_id}")
        
        args = args_class(**params)
        
        # Should not raise exception
        result = executor.execute(tool, args, scenario)
        
        # Result should be a ToolResult
        assert isinstance(result, ToolResult)
        
        # Error scenarios should report failure
        assert not result.success
        
        # Should have metadata (even if empty)
        assert isinstance(result.metadata, dict)


class TestArtifactCreation:
    """Test artifact creation during mock execution."""

    @pytest.fixture
    def executor(self) -> MockExecutor:
        exec = MockExecutor()
        yield exec
        exec.cleanup()

    def test_artifact_creation(self, executor: MockExecutor) -> None:
        """Test that artifacts are created during execution."""
        tool_cls = get_tool("information_gathering.network_discovery.nmap")
        if tool_cls is None:
            pytest.skip("nmap tool not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture("information_gathering.network_discovery.nmap")
            params = param_fixture["test_cases"]["minimal"]["params"]
        except FileNotFoundError:
            params = {"target": "192.168.1.1"}
        
        args = args_class(**params)
        
        # Add custom scenario with artifacts
        executor.scenarios["with_artifacts"] = MockScenario(
            name="with_artifacts",
            description="Success with artifacts",
            stdout="Scan complete",
            exit_code=0,
            artifacts={
                "scan_results.xml": "<nmaprun>...</nmaprun>",
            },
        )
        
        result = executor.execute(tool, args, "with_artifacts")
        
        # Should have artifacts
        assert len(result.artifacts) > 0


class TestFullPipelineIntegration:
    """Test the full execution pipeline."""

    def test_full_pipeline(self) -> None:
        """Test complete execution pipeline from args to result."""
        tool_cls = get_tool("information_gathering.network_discovery.nmap")
        if tool_cls is None:
            pytest.skip("nmap tool not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        # 1. Create args
        try:
            param_fixture = load_param_fixture("information_gathering.network_discovery.nmap")
            params = param_fixture["test_cases"]["full"]["params"]
        except FileNotFoundError:
            params = {"target": "192.168.1.0/24", "ports": "1-1000"}
        
        args = args_class(**params)
        
        # 2. Build command
        try:
            command = tool.build_command(args)
            assert isinstance(command, list)
            assert len(command) > 0
        except NotImplementedError:
            pytest.skip("build_command not implemented")
        
        # 3. Execute with mock
        executor = MockExecutor()
        try:
            result = executor.execute(tool, args, "success")
            
            # 4. Verify result structure
            assert isinstance(result, ToolResult)
            assert isinstance(result.success, bool)
            assert isinstance(result.exit_code, int)
            assert isinstance(result.stdout, str)
            assert isinstance(result.stderr, str)
            assert isinstance(result.artifacts, list)
            assert isinstance(result.metadata, dict)
            assert isinstance(result.execution_time, float)
            
            # 5. Verify metadata was parsed
            # (specific content depends on tool implementation)
            assert result.metadata is not None
            
        finally:
            executor.cleanup()
