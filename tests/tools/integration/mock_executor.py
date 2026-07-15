"""Mock executor for integration testing.

This module provides a mock execution framework that simulates tool
execution without actually running the tools, allowing for:
- Testing the full execution pipeline
- Simulating various output scenarios
- Testing error handling
- Validating artifact creation
"""

from __future__ import annotations

import os
import json
import time
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Type
from unittest.mock import MagicMock, patch

from pydantic import BaseModel

from agent.tools.base_tool import BaseTool
from agent.tools.schemas import ToolResult


@dataclass
class MockExecutionResult:
    """Result of a mock execution."""
    
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    duration: float = 0.1


@dataclass
class MockScenario:
    """Defines a mock execution scenario."""
    
    name: str
    description: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    duration: float = 0.1
    # Artifacts to create
    artifacts: Dict[str, str] = field(default_factory=dict)
    # Custom validator for this scenario
    validator: Optional[Callable[[ToolResult], bool]] = None


# Predefined mock scenarios for common situations
COMMON_SCENARIOS: Dict[str, MockScenario] = {
    "success": MockScenario(
        name="success",
        description="Successful execution with normal output",
        stdout="Scan completed successfully.\nResults: 5 items found.",
        stderr="",
        exit_code=0,
        duration=2.5,
    ),
    "success_with_warnings": MockScenario(
        name="success_with_warnings",
        description="Successful execution with warning messages",
        stdout="Operation completed.\n",
        stderr="Warning: Some hosts did not respond.\n",
        exit_code=0,
        duration=5.0,
    ),
    "no_results": MockScenario(
        name="no_results",
        description="Successful execution but no findings",
        stdout="Scan completed. No results found.\n",
        stderr="",
        exit_code=0,
        duration=1.0,
    ),
    "connection_error": MockScenario(
        name="connection_error",
        description="Connection refused error",
        stdout="",
        stderr="Error: Connection refused to target host.\n",
        exit_code=1,
        duration=0.5,
    ),
    "timeout": MockScenario(
        name="timeout",
        description="Operation timed out",
        stdout="Starting scan...\n",
        stderr="Error: Operation timed out after 30 seconds.\n",
        exit_code=124,  # Common timeout exit code
        duration=30.0,
    ),
    "permission_denied": MockScenario(
        name="permission_denied",
        description="Permission denied error",
        stdout="",
        stderr="Error: Permission denied. Try running with elevated privileges.\n",
        exit_code=1,
        duration=0.1,
    ),
    "invalid_target": MockScenario(
        name="invalid_target",
        description="Invalid target specified",
        stdout="",
        stderr="Error: Unable to resolve target hostname.\n",
        exit_code=1,
        duration=0.2,
    ),
    "rate_limited": MockScenario(
        name="rate_limited",
        description="Rate limited by target",
        stdout="Partial results:\n- Item 1\n",
        stderr="Warning: Rate limited. Reducing scan speed.\n",
        exit_code=0,
        duration=10.0,
    ),
    "authentication_failed": MockScenario(
        name="authentication_failed",
        description="Authentication failure",
        stdout="Attempting authentication...\n",
        stderr="Error: Authentication failed. Check credentials.\n",
        exit_code=1,
        duration=1.0,
    ),
    "binary_not_found": MockScenario(
        name="binary_not_found",
        description="Tool binary not found",
        stdout="",
        stderr="/bin/sh: 1: tool_name: not found\n",
        exit_code=127,
        duration=0.1,
    ),
    "segfault": MockScenario(
        name="segfault",
        description="Tool crashed with segfault",
        stdout="Processing...\n",
        stderr="Segmentation fault (core dumped)\n",
        exit_code=139,  # 128 + 11 (SIGSEGV)
        duration=0.5,
    ),
    "out_of_memory": MockScenario(
        name="out_of_memory",
        description="Tool ran out of memory",
        stdout="Loading data...\n",
        stderr="Error: Cannot allocate memory\n",
        exit_code=137,  # 128 + 9 (SIGKILL from OOM killer)
        duration=15.0,
    ),
}


class MockExecutor:
    """Mock executor for testing tool execution."""
    
    def __init__(
        self,
        default_scenario: str = "success",
        custom_scenarios: Optional[Dict[str, MockScenario]] = None,
    ):
        """Initialize mock executor.
        
        Args:
            default_scenario: Default scenario to use
            custom_scenarios: Additional custom scenarios
        """
        self.scenarios = {**COMMON_SCENARIOS}
        if custom_scenarios:
            self.scenarios.update(custom_scenarios)
        self.default_scenario = default_scenario
        self.execution_log: List[Dict[str, Any]] = []
        self.temp_dir = tempfile.mkdtemp(prefix="mock_executor_")
    
    def get_scenario(self, name: str) -> MockScenario:
        """Get a scenario by name."""
        return self.scenarios.get(name, self.scenarios[self.default_scenario])
    
    def execute(
        self,
        tool: BaseTool,
        args: BaseModel,
        scenario: Optional[str] = None,
    ) -> ToolResult:
        """Execute a tool with mock subprocess.
        
        Args:
            tool: The tool to execute
            args: Tool arguments
            scenario: Scenario to simulate (or use default)
            
        Returns:
            ToolResult from the mock execution
        """
        scenario_obj = self.get_scenario(scenario or self.default_scenario)
        
        # Log the execution
        try:
            command = tool.build_command(args)
        except NotImplementedError:
            command = ["<not implemented>"]
        
        self.execution_log.append({
            "tool": tool.__class__.__name__,
            "args": args.model_dump(),
            "command": command,
            "scenario": scenario_obj.name,
            "timestamp": time.time(),
        })
        
        # Create mock artifacts if specified
        artifacts = []
        for artifact_name, content in scenario_obj.artifacts.items():
            artifact_path = os.path.join(self.temp_dir, artifact_name)
            os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
            with open(artifact_path, "w") as f:
                f.write(content)
            artifacts.append(artifact_path)
        
        # Parse output using tool's parser
        metadata = tool.parse_output(
            scenario_obj.stdout,
            scenario_obj.stderr,
            scenario_obj.exit_code,
            args,
        )
        
        # Create tool artifacts
        tool_artifacts = tool.create_artifacts(scenario_obj.stdout, args)
        artifacts.extend(tool_artifacts)
        
        return ToolResult(
            success=scenario_obj.exit_code == 0,
            exit_code=scenario_obj.exit_code,
            stdout=scenario_obj.stdout,
            stderr=scenario_obj.stderr,
            artifacts=artifacts,
            metadata=metadata,
            execution_time=scenario_obj.duration,
        )
    
    def execute_with_patch(
        self,
        tool: BaseTool,
        args: BaseModel,
        scenario: Optional[str] = None,
    ) -> ToolResult:
        """Execute tool's run() method with patched subprocess.
        
        This patches subprocess.run to return mock results, allowing
        testing of the full run() method implementation.
        
        Args:
            tool: The tool to execute
            args: Tool arguments
            scenario: Scenario to simulate
            
        Returns:
            ToolResult from the tool's run() method
        """
        scenario_obj = self.get_scenario(scenario or self.default_scenario)
        
        # Create mock process result
        mock_proc = MagicMock()
        mock_proc.returncode = scenario_obj.exit_code
        mock_proc.stdout = scenario_obj.stdout
        mock_proc.stderr = scenario_obj.stderr
        
        with patch("subprocess.run", return_value=mock_proc):
            return tool.run(args)
    
    def cleanup(self) -> None:
        """Clean up temporary files."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)


def create_tool_specific_scenarios(tool_id: str) -> Dict[str, MockScenario]:
    """Create tool-specific mock scenarios.
    
    Args:
        tool_id: The tool identifier
        
    Returns:
        Dict of scenario name -> MockScenario
    """
    tool_name = tool_id.split(".")[-1]
    scenarios = {}
    
    if tool_name == "nmap":
        scenarios["found_hosts"] = MockScenario(
            name="found_hosts",
            description="Found hosts with open ports",
            stdout="""Starting Nmap 7.93 ( https://nmap.org )
Nmap scan report for 192.168.1.1
Host is up (0.001s latency).
PORT     STATE SERVICE
22/tcp   open  ssh
80/tcp   open  http
443/tcp  open  https

Nmap scan report for 192.168.1.2
Host is up (0.002s latency).
PORT     STATE SERVICE
22/tcp   open  ssh

Nmap done: 256 IP addresses (2 hosts up) scanned in 5.23 seconds
""",
            exit_code=0,
            duration=5.23,
        )
        scenarios["host_down"] = MockScenario(
            name="host_down",
            description="Target host is down",
            stdout="""Starting Nmap 7.93 ( https://nmap.org )
Note: Host seems down. If it is really up, but blocking our ping probes, try -Pn
Nmap done: 1 IP address (0 hosts up) scanned in 3.05 seconds
""",
            exit_code=0,
            duration=3.05,
        )
    
    elif tool_name == "hydra":
        scenarios["credentials_found"] = MockScenario(
            name="credentials_found",
            description="Found valid credentials",
            stdout="""Hydra v9.4 (c) 2022 by van Hauser/THC
[DATA] max 16 tasks per 1 server, overall 16 tasks
[DATA] attacking ssh://192.168.1.1:22/
[22][ssh] host: 192.168.1.1   login: admin   password: admin123
[22][ssh] host: 192.168.1.1   login: root    password: toor
2 of 2 target(s) successfully completed, 2 valid password(s) found
""",
            exit_code=0,
            duration=120.5,
        )
        scenarios["no_credentials"] = MockScenario(
            name="no_credentials",
            description="No valid credentials found",
            stdout="""Hydra v9.4 (c) 2022 by van Hauser/THC
[DATA] max 16 tasks per 1 server, overall 16 tasks
[DATA] attacking ssh://192.168.1.1:22/
0 of 1 target(s) successfully completed, 0 valid password(s) found
""",
            exit_code=0,
            duration=300.0,
        )
    
    elif tool_name == "gobuster":
        scenarios["directories_found"] = MockScenario(
            name="directories_found",
            description="Found directories",
            stdout="""===============================================================
Gobuster v3.5
===============================================================
[+] Url:                     http://example.com
[+] Method:                  GET
[+] Threads:                 10
[+] Wordlist:                /usr/share/wordlists/common.txt
===============================================================
/admin                (Status: 301) [Size: 0]
/api                  (Status: 200) [Size: 2048]
/backup               (Status: 403) [Size: 162]
/config               (Status: 403) [Size: 162]
===============================================================
Finished
===============================================================
""",
            exit_code=0,
            duration=45.0,
        )
    
    return scenarios


class IntegrationTestRunner:
    """Runner for integration tests with mock execution."""
    
    def __init__(self):
        self.executor = MockExecutor()
        self.results: List[Dict[str, Any]] = []
    
    def run_scenario_suite(
        self,
        tool: BaseTool,
        args: BaseModel,
        tool_id: str,
    ) -> Dict[str, ToolResult]:
        """Run all applicable scenarios for a tool.
        
        Args:
            tool: The tool to test
            args: Base arguments to use
            tool_id: The tool identifier
            
        Returns:
            Dict of scenario name -> ToolResult
        """
        results = {}
        
        # Add tool-specific scenarios
        tool_scenarios = create_tool_specific_scenarios(tool_id)
        self.executor.scenarios.update(tool_scenarios)
        
        # Run common scenarios
        for scenario_name in ["success", "connection_error", "timeout", "no_results"]:
            try:
                result = self.executor.execute(tool, args, scenario_name)
                results[scenario_name] = result
                self.results.append({
                    "tool_id": tool_id,
                    "scenario": scenario_name,
                    "success": result.success,
                    "exit_code": result.exit_code,
                })
            except Exception as e:
                self.results.append({
                    "tool_id": tool_id,
                    "scenario": scenario_name,
                    "error": str(e),
                })
        
        # Run tool-specific scenarios
        for scenario_name in tool_scenarios.keys():
            try:
                result = self.executor.execute(tool, args, scenario_name)
                results[scenario_name] = result
                self.results.append({
                    "tool_id": tool_id,
                    "scenario": scenario_name,
                    "success": result.success,
                    "exit_code": result.exit_code,
                })
            except Exception as e:
                self.results.append({
                    "tool_id": tool_id,
                    "scenario": scenario_name,
                    "error": str(e),
                })
        
        return results
    
    def cleanup(self) -> None:
        """Clean up resources."""
        self.executor.cleanup()
    
    def get_summary(self) -> Dict[str, Any]:
        """Get summary of test results."""
        total = len(self.results)
        successful = sum(1 for r in self.results if r.get("success", False))
        errors = sum(1 for r in self.results if "error" in r)
        
        return {
            "total_scenarios": total,
            "successful": successful,
            "failed": total - successful - errors,
            "errors": errors,
            "pass_rate": successful / total if total > 0 else 0,
        }
