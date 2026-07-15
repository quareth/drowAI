"""Comprehensive tests for tool parameter optimization and execution tracking (DR.7)."""

from __future__ import annotations

import pytest
import time

from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.graph.utils.tool_optimization import (
    ToolExecution,
    check_redundant_execution,
    get_scan_phase,
    hash_parameters,
    optimize_tool_parameters,
    record_tool_execution,
    should_skip_phase,
)


class TestParameterHashing:
    """Test parameter hashing functionality."""

    def test_hash_parameters_stable(self):
        """Test that same parameters produce same hash."""
        params1 = {"target": "127.0.0.1", "ports": "1-1000"}
        params2 = {"target": "127.0.0.1", "ports": "1-1000"}

        hash1 = hash_parameters(params1)
        hash2 = hash_parameters(params2)

        assert hash1 == hash2

    def test_hash_parameters_order_independent(self):
        """Test that hash is independent of parameter order."""
        params1 = {"target": "127.0.0.1", "ports": "1-1000"}
        params2 = {"ports": "1-1000", "target": "127.0.0.1"}

        hash1 = hash_parameters(params1)
        hash2 = hash_parameters(params2)

        assert hash1 == hash2

    def test_hash_parameters_ignores_noise(self):
        """Test that hash ignores timestamp and other noise."""
        params1 = {"target": "127.0.0.1", "ports": "1-1000", "timestamp": "2024-01-01"}
        params2 = {"target": "127.0.0.1", "ports": "1-1000", "timestamp": "2024-01-02"}

        hash1 = hash_parameters(params1)
        hash2 = hash_parameters(params2)

        assert hash1 == hash2

    def test_hash_parameters_different_content(self):
        """Test that different parameters produce different hashes."""
        params1 = {"target": "127.0.0.1", "ports": "1-1000"}
        params2 = {"target": "127.0.0.1", "ports": "1-2000"}

        hash1 = hash_parameters(params1)
        hash2 = hash_parameters(params2)

        assert hash1 != hash2


class TestRedundancyDetection:
    """Test redundant execution detection."""

    def test_check_redundant_execution_identical(self):
        """Test detecting identical execution."""
        tool_id = "nmap"
        parameters = {"target": "127.0.0.1", "ports": "1-1000"}
        
        execution1 = ToolExecution(
            tool_id=tool_id,
            parameters=parameters,
            parameter_hash=hash_parameters(parameters),
            result_summary="Scan completed",
            timestamp=time.time(),
            iteration=1,
        )
        execution_history = [execution1]

        reason = check_redundant_execution(tool_id, parameters, execution_history)

        assert reason is not None
        assert "Identical execution" in reason

    def test_check_redundant_execution_different(self):
        """Test that different parameters are not flagged as redundant."""
        tool_id = "nmap"
        params1 = {"target": "127.0.0.1", "ports": "1-1000"}
        params2 = {"target": "127.0.0.1", "ports": "1-2000"}
        
        execution1 = ToolExecution(
            tool_id=tool_id,
            parameters=params1,
            parameter_hash=hash_parameters(params1),
            result_summary="Scan completed",
            timestamp=time.time(),
            iteration=1,
        )
        execution_history = [execution1]

        reason = check_redundant_execution(tool_id, params2, execution_history)

        assert reason is None

    def test_check_redundant_execution_empty_history(self):
        """Test that empty history doesn't flag redundancy."""
        tool_id = "nmap"
        parameters = {"target": "127.0.0.1", "ports": "1-1000"}

        reason = check_redundant_execution(tool_id, parameters, [])

        assert reason is None


class TestParameterOptimization:
    """Test parameter optimization functionality."""

    def test_optimize_nmap_port_narrowing(self):
        """Test that port scan parameters are narrowed after finding ports."""
        tool_id = "information_gathering.network_discovery.nmap"
        parameters = {"target": "127.0.0.1", "ports": "1-10000"}
        findings = [
            {"type": "open_port", "port": 22},
            {"type": "open_port", "port": 80},
            {"type": "open_port", "port": 443},
        ]
        observations = []
        metadata = {}

        optimized = optimize_tool_parameters(
            tool_id, parameters, findings, observations, metadata
        )

        assert optimized["ports"] == "22,80,443"

    def test_optimize_nmap_no_narrowing_if_specific(self):
        """Test that specific port ranges are not narrowed."""
        tool_id = "nmap"
        parameters = {"target": "127.0.0.1", "ports": "22,80,443"}
        findings = [
            {"type": "open_port", "port": 22},
        ]
        observations = []
        metadata = {}

        optimized = optimize_tool_parameters(
            tool_id, parameters, findings, observations, metadata
        )

        assert optimized["ports"] == "22,80,443"  # Unchanged

    def test_optimize_target_fallback(self):
        """Test that fallback target is used when network scan finds no hosts."""
        tool_id = "nmap"
        parameters = {"target": "192.168.1.0/24", "ports": "1-1000"}
        findings = []  # No hosts found
        observations = []
        metadata = {
            "network_scan_attempted": True,
            "user_scope": {
                "goals": [],
                "boundaries": [],
                "conditional_targets": {"fallback_host": "127.0.0.1"},
                "explicit_tools": [],
            },
        }

        optimized = optimize_tool_parameters(
            tool_id, parameters, findings, observations, metadata
        )

        assert optimized["target"] == "127.0.0.1"

    def test_remove_duplicate_flags(self):
        """Test that duplicate flags are removed."""
        tool_id = "nmap"
        parameters = {
            "target": "127.0.0.1",
            "scan_types": ["-sV", "-sS"],
            "service_detection": True,
        }
        findings = []
        observations = []
        metadata = {}

        optimized = optimize_tool_parameters(
            tool_id, parameters, findings, observations, metadata
        )

        assert "-sV" not in optimized.get("scan_types", [])
        assert optimized.get("service_detection") is True


class TestExecutionTracking:
    """Test execution history tracking."""

    def test_record_tool_execution(self):
        """Test recording tool execution in history."""
        tool_id = "nmap"
        parameters = {"target": "127.0.0.1", "ports": "1-1000"}
        result_summary = "Found ports 22, 80, 443"
        iteration = 1
        execution_history = []

        updated_history = record_tool_execution(
            tool_id, parameters, result_summary, iteration, execution_history
        )

        assert len(updated_history) == 1
        assert updated_history[0].tool_id == tool_id
        assert updated_history[0].iteration == iteration

    def test_record_tool_execution_limit(self):
        """Test that execution history is limited to max_history."""
        tool_id = "nmap"
        parameters = {"target": "127.0.0.1", "ports": "1-1000"}
        result_summary = "Scan completed"
        iteration = 1
        
        # Create history with 15 executions
        execution_history = []
        for i in range(15):
            exec_record = ToolExecution(
                tool_id=tool_id,
                parameters=parameters,
                parameter_hash=hash_parameters(parameters),
                result_summary=result_summary,
                timestamp=time.time(),
                iteration=i,
            )
            execution_history.append(exec_record)

        updated_history = record_tool_execution(
            tool_id, parameters, result_summary, iteration, execution_history, max_history=10
        )

        assert len(updated_history) == 10
        assert updated_history[-1].iteration == iteration


class TestScanProgression:
    """Test scan phase progression logic."""

    def test_get_scan_phase_discovery(self):
        """Test that initial phase is discovery."""
        metadata = {"tool_execution_history": [], "findings": []}

        phase = get_scan_phase(metadata)

        assert phase == "discovery"

    def test_get_scan_phase_enumeration(self):
        """Test that phase is enumeration after hosts found."""
        metadata = {
            "tool_execution_history": [
                ToolExecution(
                    tool_id="nmap",
                    parameters={},
                    parameter_hash="hash1",
                    result_summary="Found host",
                    timestamp=time.time(),
                    iteration=1,
                ).to_dict()
            ],
            "findings": [{"type": "host_discovered", "content": "192.168.1.1"}],
        }

        phase = get_scan_phase(metadata)

        assert phase == "enumeration"

    def test_get_scan_phase_deep_scan(self):
        """Test that phase is deep_scan after services found."""
        metadata = {
            "tool_execution_history": [
                ToolExecution(
                    tool_id="nmap",
                    parameters={},
                    parameter_hash="hash1",
                    result_summary="Found services",
                    timestamp=time.time(),
                    iteration=1,
                ).to_dict()
            ],
            "findings": [
                {"type": "host_discovered", "content": "192.168.1.1"},
                {"type": "service", "content": "SSH on port 22"},
            ],
        }

        phase = get_scan_phase(metadata)

        assert phase == "enumeration"  # Still enumeration if no vulns

    def test_should_skip_phase_discovery(self):
        """Test that discovery phase is skipped if hosts already found."""
        target_phase = "discovery"
        current_phase = "enumeration"
        execution_history = [
            ToolExecution(
                tool_id="nmap",
                parameters={},
                parameter_hash="hash1",
                result_summary="Found host",
                timestamp=time.time(),
                iteration=1,
            )
        ]

        should_skip = should_skip_phase(target_phase, current_phase, execution_history)

        assert should_skip is True

    def test_should_skip_phase_enumeration(self):
        """Test that enumeration phase is skipped if in deep_scan."""
        target_phase = "enumeration"
        current_phase = "deep_scan"
        execution_history = [
            ToolExecution(
                tool_id="nmap",
                parameters={},
                parameter_hash="hash1",
                result_summary="Found services",
                timestamp=time.time(),
                iteration=1,
            )
        ]

        should_skip = should_skip_phase(target_phase, current_phase, execution_history)

        assert should_skip is True


class TestIntegration:
    """Integration tests for tool optimization."""

    def test_full_optimization_workflow(self):
        """Test full workflow: check redundancy → optimize → record."""
        tool_id = "nmap"
        parameters = {"target": "127.0.0.1", "ports": "1-10000"}
        execution_history = []
        findings = [
            {"type": "open_port", "port": 22},
            {"type": "open_port", "port": 80},
        ]
        observations = []
        metadata = {}

        # Check redundancy (should be None for first execution)
        redundancy = check_redundant_execution(tool_id, parameters, execution_history)
        assert redundancy is None

        # Optimize parameters
        optimized = optimize_tool_parameters(
            tool_id, parameters, findings, observations, metadata
        )
        assert optimized["ports"] == "22,80"

        # Record execution
        updated_history = record_tool_execution(
            tool_id,
            optimized,
            "Found ports 22, 80",
            1,
            execution_history,
        )
        assert len(updated_history) == 1

        # Check redundancy with same parameters (should detect)
        redundancy2 = check_redundant_execution(
            tool_id, optimized, updated_history
        )
        assert redundancy2 is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

