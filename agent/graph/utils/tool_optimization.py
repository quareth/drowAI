"""Tool parameter optimization and execution tracking utilities (DR.7)."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from backend.services.metrics.utils import safe_inc

logger = logging.getLogger(__name__)


@dataclass
class ToolExecution:
    """Record of tool execution for tracking and optimization."""

    tool_id: str
    parameters: Dict[str, Any]
    parameter_hash: str
    result_summary: str
    timestamp: float
    iteration: int

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage in metadata."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ToolExecution":
        """Create from dictionary stored in metadata."""
        return cls(
            tool_id=data["tool_id"],
            parameters=data["parameters"],
            parameter_hash=data["parameter_hash"],
            result_summary=data.get("result_summary", ""),
            timestamp=data.get("timestamp", time.time()),
            iteration=data.get("iteration", 0),
        )


def hash_parameters(params: Dict[str, Any]) -> str:
    """
    Create stable hash of parameters.

    Normalizes parameters (remove noise like timestamps) and hashes for comparison.

    Args:
        params: Tool parameters dict

    Returns:
        SHA256 hex digest
    """
    # Normalize parameters (remove noise like timestamps)
    normalized = {
        k: v
        for k, v in params.items()
        if k not in ["timestamp", "request_id", "transport", "workspace_path"]
    }
    content = json.dumps(normalized, sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()


def check_redundant_execution(
    tool_id: str,
    parameters: Dict[str, Any],
    execution_history: List[ToolExecution],
    check_last_n: int = 5,
) -> Optional[str]:
    """
    Check if tool execution is redundant.

    Args:
        tool_id: Tool to execute
        parameters: Tool parameters
        execution_history: List of previous tool executions
        check_last_n: Number of recent executions to check (default: 5)

    Returns:
        Redundancy reason if redundant, None otherwise
    """
    if not execution_history:
        return None

    # Hash parameters for comparison
    param_hash = hash_parameters(parameters)

    # Check last N executions
    for prev_exec in execution_history[-check_last_n:]:
        if prev_exec.tool_id == tool_id and prev_exec.parameter_hash == param_hash:
            reason = (
                f"Identical execution detected: {tool_id} was executed "
                f"with same parameters at iteration {prev_exec.iteration}"
            )
            logger.warning(f"[OPTIMIZATION] {reason}")
            safe_inc("redundant_tool_execution_prevented")
            return reason

    return None


def optimize_tool_parameters(
    tool_id: str,
    parameters: Dict[str, Any],
    findings: List[Dict[str, Any]],
    observations: List[str],
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Optimize tool parameters based on previous findings.

    Args:
        tool_id: Tool to execute
        parameters: Original parameters
        findings: List of finding dicts from previous executions
        observations: List of observation strings
        metadata: State metadata (for scope, conditional targets, etc.)

    Returns:
        Optimized parameters
    """
    optimized = parameters.copy()

    # Optimize port scanning (nmap)
    if "nmap" in tool_id.lower() or tool_id.endswith(".nmap"):
        optimized = _optimize_nmap_parameters(optimized, findings, observations)

    # Optimize target selection
    optimized = _optimize_target_selection(optimized, findings, metadata)

    # Remove duplicate flags
    optimized = _remove_duplicate_flags(optimized, tool_id)

    return optimized


def _optimize_nmap_parameters(
    parameters: Dict[str, Any],
    findings: List[Dict[str, Any]],
    observations: List[str],
) -> Dict[str, Any]:
    """Optimize Nmap parameters based on findings."""
    optimized = parameters.copy()

    # Extract open ports from findings
    open_ports = _extract_open_ports(findings, observations)

    if open_ports and "ports" in optimized:
        ports_value = str(optimized["ports"])
        # If first scan was broad, narrow to found ports
        if ports_value in ["1-10000", "1-65535", "1-1024", "1-49151"]:
            optimized["ports"] = ",".join(map(str, open_ports))
            logger.info(
                f"[OPTIMIZATION] Narrowed port scan to found ports: {optimized['ports']}"
            )
            safe_inc("parameter_optimization_port_narrow")

    return optimized


def _optimize_target_selection(
    parameters: Dict[str, Any],
    findings: List[Dict[str, Any]],
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """Optimize target selection based on findings and scope."""
    optimized = parameters.copy()

    if "target" not in optimized:
        return optimized

    current_target = str(optimized["target"])
    
    # Check if this is a network scan (CIDR notation)
    is_network_scan = "/" in current_target or "-" in current_target
    
    # Mark network scan attempt if CIDR/range
    if is_network_scan:
        metadata["network_scan_attempted"] = True
    
    # Check if network scan was attempted and found no hosts
    network_scan_attempted = metadata.get("network_scan_attempted", False)
    has_hosts = any(
        finding.get("type") == "host_discovered"
        or "host" in str(finding).lower()
        or finding.get("type") == "tool_output"
        and "host" in str(finding.get("content", "")).lower()
        for finding in findings
    )

    if network_scan_attempted and not has_hosts and is_network_scan:
        # Check for conditional targets from scope
        user_scope = metadata.get("user_scope")
        if user_scope:
            if isinstance(user_scope, dict):
                from .scope_parser import UserScope

                user_scope = UserScope.from_dict(user_scope)
            fallback = user_scope.conditional_targets.get("fallback_host")
            if fallback:
                optimized["target"] = fallback
                logger.info(f"[OPTIMIZATION] Using fallback target: {fallback}")
                safe_inc("parameter_optimization_fallback_target")

    return optimized


def _remove_duplicate_flags(
    parameters: Dict[str, Any],
    tool_id: str,
) -> Dict[str, Any]:
    """Remove duplicate flags from tool parameters."""
    optimized = parameters.copy()

    # Nmap-specific flag deduplication
    if "nmap" in tool_id.lower() or tool_id.endswith(".nmap"):
        scan_types = list(optimized.get("scan_types", []))

        # Check for duplicate -sV flag
        if "-sV" in scan_types and optimized.get("service_detection"):
            scan_types.remove("-sV")  # service_detection implies -sV
            optimized["scan_types"] = scan_types
            logger.info("[OPTIMIZATION] Removed duplicate -sV flag")
            safe_inc("parameter_optimization_flag_deduplication")

        # Check for duplicate -sS flag
        if "-sS" in scan_types and optimized.get("syn_scan"):
            scan_types.remove("-sS")  # syn_scan implies -sS
            optimized["scan_types"] = scan_types
            logger.info("[OPTIMIZATION] Removed duplicate -sS flag")

    return optimized


def _extract_open_ports(
    findings: List[Dict[str, Any]],
    observations: List[str],
) -> List[int]:
    """
    Extract list of open ports from findings and observations.

    Args:
        findings: List of finding dicts
        observations: List of observation strings

    Returns:
        Sorted list of open port numbers
    """
    ports = set()

    # Extract from findings
    for finding in findings:
        if finding.get("type") == "open_port":
            port = finding.get("port")
            if port:
                try:
                    ports.add(int(port))
                except (ValueError, TypeError):
                    pass

        # Also check content for port patterns
        content = str(finding).lower()
        import re

        port_matches = re.findall(r"port\s+(\d+)[/\s]*(?:tcp|udp)?", content)
        for match in port_matches:
            try:
                ports.add(int(match))
            except ValueError:
                pass

    # Extract from observations
    for obs in observations:
        import re

        port_matches = re.findall(r"port\s+(\d+)[/\s]*(?:tcp|udp)?", obs.lower())
        for match in port_matches:
            try:
                ports.add(int(match))
            except ValueError:
                pass

    return sorted(ports)


def record_tool_execution(
    tool_id: str,
    parameters: Dict[str, Any],
    result_summary: str,
    iteration: int,
    execution_history: List[ToolExecution],
    max_history: int = 10,
) -> List[ToolExecution]:
    """
    Record tool execution in history.

    Args:
        tool_id: Tool identifier
        parameters: Tool parameters used
        result_summary: Summary of execution result
        iteration: Current iteration number
        execution_history: Current execution history
        max_history: Maximum number of executions to keep

    Returns:
        Updated execution history
    """
    param_hash = hash_parameters(parameters)

    execution = ToolExecution(
        tool_id=tool_id,
        parameters=parameters,
        parameter_hash=param_hash,
        result_summary=result_summary,
        timestamp=time.time(),
        iteration=iteration,
    )

    history = list(execution_history) + [execution]

    # Limit history size
    if len(history) > max_history:
        history = history[-max_history:]

    return history


def get_scan_phase(metadata: Dict[str, Any]) -> str:
    """
    Get current scan phase based on findings and execution history.

    Phases: discovery → enumeration → deep_scan

    Args:
        metadata: State metadata (may contain findings, synthesized_output, etc.)

    Returns:
        Current phase: "discovery", "enumeration", or "deep_scan"
    """
    execution_history = metadata.get("tool_execution_history", [])
    findings = metadata.get("findings", [])
    
    # Also check synthesized output for findings
    synthesized = metadata.get("synthesized_output") or {}
    if synthesized:
        findings.append(synthesized)
    
    # Extract from key_findings and vulnerabilities in synthesized output
    if synthesized:
        key_findings = synthesized.get("key_findings", [])
        vulnerabilities = synthesized.get("vulnerabilities", [])
        for finding in key_findings:
            findings.append({"type": "finding", "content": str(finding)})
        for vuln in vulnerabilities:
            findings.append({"type": "vulnerability", "content": str(vuln)})

    # Check if we've discovered hosts
    has_hosts = any(
        finding.get("type") == "host_discovered"
        or "host" in str(finding).lower()
        or finding.get("type") == "tool_output"
        and "host" in str(finding.get("content", "")).lower()
        for finding in findings
    )

    # Check if we've enumerated services
    has_services = any(
        "service" in str(finding).lower()
        or finding.get("type") == "service"
        or finding.get("type") == "finding"
        and "service" in str(finding.get("content", "")).lower()
        for finding in findings
    )

    # Check if we've done vulnerability scanning
    has_vulns = any(
        "vulnerabilit" in str(finding).lower()
        or finding.get("type") == "vulnerability"
        for finding in findings
    )

    # Determine phase
    if not has_hosts and len(execution_history) == 0:
        return "discovery"
    elif has_hosts and not has_services:
        return "enumeration"
    elif has_services and not has_vulns:
        return "enumeration"
    else:
        return "deep_scan"


def should_skip_phase(
    target_phase: str,
    current_phase: str,
    execution_history: List[ToolExecution],
) -> bool:
    """
    Determine if a scan phase should be skipped.

    Args:
        target_phase: Phase to check (discovery, enumeration, deep_scan)
        current_phase: Current phase
        execution_history: Execution history

    Returns:
        True if phase should be skipped, False otherwise
    """
    # Don't skip if no history
    if not execution_history:
        return False

    # Skip discovery if we already have hosts
    if target_phase == "discovery" and current_phase in ["enumeration", "deep_scan"]:
        return True

    # Skip enumeration if we're in deep_scan and have services
    if target_phase == "enumeration" and current_phase == "deep_scan":
        return True

    return False


__all__ = [
    "ToolExecution",
    "hash_parameters",
    "check_redundant_execution",
    "optimize_tool_parameters",
    "record_tool_execution",
    "get_scan_phase",
    "should_skip_phase",
    "_extract_open_ports",
]

