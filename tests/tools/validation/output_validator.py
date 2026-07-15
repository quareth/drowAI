"""Output parsing accuracy validator for penetration testing tools.

This module validates that parse_output() correctly extracts expected
fields and data structures from tool output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Type

from pydantic import BaseModel

from agent.tools.base_tool import BaseTool


@dataclass
class ExpectedOutput:
    """Defines expected output fields for a tool."""
    
    tool_name: str
    # Required fields that should always be present in metadata
    required_fields: Set[str] = field(default_factory=set)
    # Optional fields that may be present
    optional_fields: Set[str] = field(default_factory=set)
    # Field type expectations
    field_types: Dict[str, type] = field(default_factory=dict)
    # Fields that should be lists
    list_fields: Set[str] = field(default_factory=set)
    # Fields that should contain specific patterns
    field_patterns: Dict[str, str] = field(default_factory=dict)
    # Minimum expected items for list fields
    min_items: Dict[str, int] = field(default_factory=dict)


@dataclass
class OutputValidationResult:
    """Result of output validation."""
    
    valid: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    missing_fields: List[str] = field(default_factory=list)
    extra_fields: List[str] = field(default_factory=list)
    field_results: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    coverage_score: float = 0.0
    
    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.valid = False
    
    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)
    
    def add_field_error(self, field: str, msg: str) -> None:
        self.field_results[field] = {"valid": False, "error": msg}
        self.add_error(f"{field}: {msg}")
    
    def add_field_success(self, field: str, value_summary: Optional[str] = None) -> None:
        self.field_results[field] = {"valid": True, "summary": value_summary}


# Expected output schemas for tools
EXPECTED_OUTPUTS: Dict[str, ExpectedOutput] = {
    "nmap": ExpectedOutput(
        tool_name="nmap",
        required_fields={"hosts"},
        optional_fields={"ports", "services", "os_matches", "scripts", "scan_info"},
        field_types={
            "hosts": list,
            "ports": list,
            "services": list,
        },
        list_fields={"hosts", "ports", "services", "os_matches", "scripts"},
        min_items={"hosts": 0},  # May be 0 if no hosts found
    ),
    "hydra": ExpectedOutput(
        tool_name="hydra",
        required_fields=set(),
        optional_fields={"credentials", "attempts", "success_count", "target"},
        field_types={
            "credentials": list,
            "attempts": int,
            "success_count": int,
        },
        list_fields={"credentials"},
    ),
    "nikto": ExpectedOutput(
        tool_name="nikto",
        required_fields=set(),
        optional_fields={"vulnerabilities", "findings", "target", "server"},
        field_types={
            "vulnerabilities": list,
            "findings": list,
        },
        list_fields={"vulnerabilities", "findings"},
    ),
    "gobuster": ExpectedOutput(
        tool_name="gobuster",
        required_fields=set(),
        optional_fields={"found_paths", "directories", "files", "status_codes"},
        field_types={
            "found_paths": list,
            "directories": list,
            "files": list,
        },
        list_fields={"found_paths", "directories", "files"},
    ),
    "sqlmap": ExpectedOutput(
        tool_name="sqlmap",
        required_fields=set(),
        optional_fields={"databases", "tables", "columns", "data", "vulnerabilities", "injection_points"},
        field_types={
            "databases": list,
            "tables": list,
            "columns": list,
            "vulnerabilities": list,
        },
        list_fields={"databases", "tables", "columns", "data", "vulnerabilities", "injection_points"},
    ),
    "tnscmd10g": ExpectedOutput(
        tool_name="tnscmd10g",
        required_fields={"command", "exit_code", "host", "port"},
        optional_fields={"version_obtained", "status_obtained", "services_found", "errors"},
        field_types={
            "services_found": list,
            "errors": list,
        },
        list_fields={"services_found", "errors"},
    ),
    "oscanner": ExpectedOutput(
        tool_name="oscanner",
        required_fields={"exit_code", "server", "port"},
        optional_fields={"sids_found", "services_found", "accounts_found", "errors"},
        field_types={
            "sids_found": list,
            "services_found": list,
            "accounts_found": list,
            "errors": list,
        },
        list_fields={"sids_found", "services_found", "accounts_found", "errors"},
    ),
    "sidguesser": ExpectedOutput(
        tool_name="sidguesser",
        required_fields={"exit_code", "host", "port"},
        optional_fields={"sids_found", "errors"},
        field_types={
            "sids_found": list,
            "errors": list,
        },
        list_fields={"sids_found", "errors"},
    ),
    "ffuf": ExpectedOutput(
        tool_name="ffuf",
        required_fields=set(),
        optional_fields={"results", "found_paths", "status_codes"},
        field_types={
            "results": list,
            "found_paths": list,
        },
        list_fields={"results", "found_paths"},
    ),
    "amass": ExpectedOutput(
        tool_name="amass",
        required_fields=set(),
        optional_fields={"subdomains", "hosts", "ips", "sources"},
        field_types={
            "subdomains": list,
            "hosts": list,
            "ips": list,
        },
        list_fields={"subdomains", "hosts", "ips"},
    ),
    "masscan": ExpectedOutput(
        tool_name="masscan",
        required_fields=set(),
        optional_fields={"hosts", "open_ports", "ports", "services", "deprecations"},
        field_types={
            "hosts": list,
            "open_ports": list,
            "ports": list,
            "deprecations": list,
        },
        list_fields={"hosts", "open_ports", "ports", "deprecations"},
    ),
    "theharvester": ExpectedOutput(
        tool_name="theharvester",
        required_fields=set(),
        optional_fields={"emails", "hosts", "subdomains", "ips"},
        field_types={
            "emails": list,
            "hosts": list,
            "subdomains": list,
            "ips": list,
        },
        list_fields={"emails", "hosts", "subdomains", "ips"},
    ),
    "wpscan": ExpectedOutput(
        tool_name="wpscan",
        required_fields=set(),
        optional_fields={"vulnerabilities", "plugins", "themes", "users", "version"},
        field_types={
            "vulnerabilities": list,
            "plugins": list,
            "themes": list,
            "users": list,
        },
        list_fields={"vulnerabilities", "plugins", "themes", "users"},
    ),
    "john": ExpectedOutput(
        tool_name="john",
        required_fields=set(),
        optional_fields={"cracked", "passwords", "hashes_loaded", "time_elapsed"},
        field_types={
            "cracked": list,
            "passwords": list,
        },
        list_fields={"cracked", "passwords"},
    ),
    "hashcat": ExpectedOutput(
        tool_name="hashcat",
        required_fields=set(),
        optional_fields={"cracked", "passwords", "recovered", "progress"},
        field_types={
            "cracked": list,
            "passwords": list,
        },
        list_fields={"cracked", "passwords"},
    ),
    "sleuthkit": ExpectedOutput(
        tool_name="sleuthkit",
        required_fields={"command_executed", "exit_code"},
        optional_fields={"partition_table", "entries", "entries_found", "partitions_found", "output_lines"},
        field_types={
            "partition_table": list,
            "entries": list,
            "entries_found": int,
            "partitions_found": int,
        },
        list_fields={"partition_table", "entries"},
    ),
    "volatility": ExpectedOutput(
        tool_name="volatility",
        required_fields={"exit_code"},
        optional_fields={
            "processes_found",
            "connections_found",
            "files_found",
            "modules_found",
            "handles_found",
            "registry_keys",
        },
        field_types={
            "processes_found": int,
            "connections_found": int,
            "files_found": int,
            "modules_found": int,
            "handles_found": int,
            "registry_keys": int,
        },
    ),
    "binwalk": ExpectedOutput(
        tool_name="binwalk",
        required_fields={"exit_code"},
        optional_fields={"signatures_found", "signature_list", "entropy_points"},
        field_types={
            "signatures_found": int,
            "signature_list": list,
            "entropy_points": int,
        },
        list_fields={"signature_list"},
    ),
    "foremost": ExpectedOutput(
        tool_name="foremost",
        required_fields={"exit_code"},
        optional_fields={"files_carved", "summary"},
        field_types={
            "files_carved": dict,
            "summary": dict,
        },
    ),
    "bulk_extractor": ExpectedOutput(
        tool_name="bulk_extractor",
        required_fields={"exit_code"},
        optional_fields={"features", "summary"},
        field_types={
            "features": dict,
            "summary": dict,
        },
    ),
    "hashdeep": ExpectedOutput(
        tool_name="hashdeep",
        required_fields={"exit_code"},
        optional_fields={"hashes", "summary"},
        field_types={
            "hashes": list,
            "summary": dict,
        },
        list_fields={"hashes"},
    ),
    "chkrootkit": ExpectedOutput(
        tool_name="chkrootkit",
        required_fields={"exit_code"},
        optional_fields={"alerts", "summary"},
        field_types={
            "alerts": list,
            "summary": dict,
        },
        list_fields={"alerts"},
    ),
    "scalpel": ExpectedOutput(
        tool_name="scalpel",
        required_fields={"exit_code"},
        optional_fields={"files_carved", "bytes_processed"},
        field_types={
            "files_carved": int,
            "bytes_processed": int,
        },
    ),
    "ddrescue": ExpectedOutput(
        tool_name="ddrescue",
        required_fields={"exit_code"},
        optional_fields={"bytes_copied", "bytes_failed", "recovery_rate"},
        field_types={
            "bytes_copied": int,
            "bytes_failed": int,
            "recovery_rate": float,
        },
    ),
    "safecopy": ExpectedOutput(
        tool_name="safecopy",
        required_fields={"exit_code"},
        optional_fields={"bytes_recovered", "blocks_processed"},
        field_types={
            "bytes_recovered": int,
            "blocks_processed": int,
        },
    ),
}


class OutputValidator:
    """Validates parsed output against expected schemas."""
    
    def __init__(self, expected_outputs: Optional[Dict[str, ExpectedOutput]] = None):
        self.expected_outputs = expected_outputs or EXPECTED_OUTPUTS
    
    def validate_output(
        self,
        metadata: Dict[str, Any],
        tool_id: str,
        stdout: str = "",
    ) -> OutputValidationResult:
        """Validate parsed output metadata.
        
        Args:
            metadata: The metadata dict from parse_output()
            tool_id: The tool identifier
            stdout: Original stdout for reference
            
        Returns:
            OutputValidationResult with validation details
        """
        result = OutputValidationResult()
        
        if not isinstance(metadata, dict):
            result.add_error(f"Metadata is not a dict: {type(metadata)}")
            return result
        
        # Extract tool name
        tool_name = tool_id.split(".")[-1]
        expected = self.expected_outputs.get(tool_name)
        
        if not expected:
            result.add_warning(f"No expected output schema for tool: {tool_name}")
            return self._validate_generic(metadata, result)
        
        # Check required fields
        self._validate_required_fields(metadata, expected, result)
        
        # Check field types
        self._validate_field_types(metadata, expected, result)
        
        # Check list fields
        self._validate_list_fields(metadata, expected, result)
        
        # Check field patterns
        self._validate_field_patterns(metadata, expected, result)
        
        # Check for extra (unexpected) fields
        all_expected = expected.required_fields | expected.optional_fields
        for field in metadata.keys():
            if field not in all_expected and not field.startswith("_"):
                result.extra_fields.append(field)
        
        # Calculate coverage score
        if all_expected:
            found = len([f for f in all_expected if f in metadata])
            result.coverage_score = found / len(all_expected)
        
        return result
    
    def _validate_generic(
        self,
        metadata: Dict[str, Any],
        result: OutputValidationResult,
    ) -> OutputValidationResult:
        """Generic validation for tools without defined schemas."""
        # Check for common field patterns
        for field, value in metadata.items():
            if value is None:
                result.add_warning(f"Field '{field}' is None")
            elif isinstance(value, list) and len(value) == 0:
                result.add_warning(f"Field '{field}' is empty list")
            else:
                result.add_field_success(field, self._summarize_value(value))
        
        return result
    
    def _validate_required_fields(
        self,
        metadata: Dict[str, Any],
        expected: ExpectedOutput,
        result: OutputValidationResult,
    ) -> None:
        """Validate that required fields are present."""
        for field in expected.required_fields:
            if field not in metadata:
                result.missing_fields.append(field)
                result.add_field_error(field, "Required field missing")
            elif metadata[field] is None:
                result.add_field_error(field, "Required field is None")
            else:
                result.add_field_success(field, self._summarize_value(metadata[field]))
    
    def _validate_field_types(
        self,
        metadata: Dict[str, Any],
        expected: ExpectedOutput,
        result: OutputValidationResult,
    ) -> None:
        """Validate field types match expectations."""
        for field, expected_type in expected.field_types.items():
            if field not in metadata:
                continue
            
            value = metadata[field]
            if value is not None and not isinstance(value, expected_type):
                result.add_field_error(
                    field,
                    f"Expected type {expected_type.__name__}, got {type(value).__name__}"
                )
    
    def _validate_list_fields(
        self,
        metadata: Dict[str, Any],
        expected: ExpectedOutput,
        result: OutputValidationResult,
    ) -> None:
        """Validate list fields have minimum items."""
        for field in expected.list_fields:
            if field not in metadata:
                continue
            
            value = metadata[field]
            if not isinstance(value, list):
                continue
            
            min_items = expected.min_items.get(field, 0)
            if len(value) < min_items:
                result.add_warning(
                    f"Field '{field}' has {len(value)} items, expected at least {min_items}"
                )
    
    def _validate_field_patterns(
        self,
        metadata: Dict[str, Any],
        expected: ExpectedOutput,
        result: OutputValidationResult,
    ) -> None:
        """Validate field values match expected patterns."""
        for field, pattern in expected.field_patterns.items():
            if field not in metadata:
                continue
            
            value = metadata[field]
            if isinstance(value, str) and not re.match(pattern, value):
                result.add_warning(
                    f"Field '{field}' value doesn't match pattern '{pattern}'"
                )
    
    def _summarize_value(self, value: Any) -> str:
        """Create a brief summary of a value."""
        if isinstance(value, list):
            return f"list({len(value)} items)"
        elif isinstance(value, dict):
            return f"dict({len(value)} keys)"
        elif isinstance(value, str):
            if len(value) > 50:
                return f"str({len(value)} chars)"
            return f"'{value}'"
        else:
            return str(value)


def validate_parse_output(
    tool: BaseTool,
    stdout: str,
    stderr: str,
    exit_code: int,
    args: BaseModel,
    tool_id: str,
) -> OutputValidationResult:
    """Validate a tool's parsed output.
    
    Args:
        tool: The tool instance
        stdout: Standard output from tool
        stderr: Standard error from tool
        exit_code: Exit code from tool
        args: The arguments used
        tool_id: The tool identifier
        
    Returns:
        OutputValidationResult with validation details
    """
    validator = OutputValidator()
    
    try:
        metadata = tool.parse_output(stdout, stderr, exit_code, args)
    except Exception as e:
        return OutputValidationResult(
            valid=False,
            errors=[f"parse_output() raised exception: {e}"]
        )
    
    return validator.validate_output(metadata, tool_id, stdout)


def validate_output_extracts_data(
    tool: BaseTool,
    stdout: str,
    stderr: str,
    exit_code: int,
    args: BaseModel,
    expected_extractions: Dict[str, Any],
) -> OutputValidationResult:
    """Validate that specific data is extracted from output.
    
    Args:
        tool: The tool instance
        stdout: Standard output containing known data
        stderr: Standard error
        exit_code: Exit code
        args: Arguments used
        expected_extractions: Dict of field -> expected value/pattern
        
    Returns:
        OutputValidationResult with extraction validation
    """
    result = OutputValidationResult()
    
    try:
        metadata = tool.parse_output(stdout, stderr, exit_code, args)
    except Exception as e:
        result.add_error(f"parse_output() raised exception: {e}")
        return result
    
    for field, expected in expected_extractions.items():
        if field not in metadata:
            result.add_field_error(field, "Expected field not found in parsed output")
            continue
        
        actual = metadata[field]
        
        if isinstance(expected, str) and expected.startswith("regex:"):
            # Regex pattern match
            pattern = expected[6:]
            if isinstance(actual, str) and not re.search(pattern, actual):
                result.add_field_error(
                    field,
                    f"Value doesn't match pattern: expected '{pattern}', got '{actual}'"
                )
            else:
                result.add_field_success(field)
        elif isinstance(expected, type):
            # Type check
            if not isinstance(actual, expected):
                result.add_field_error(
                    field,
                    f"Wrong type: expected {expected.__name__}, got {type(actual).__name__}"
                )
            else:
                result.add_field_success(field)
        elif callable(expected):
            # Custom validator function
            try:
                if not expected(actual):
                    result.add_field_error(field, "Custom validator returned False")
                else:
                    result.add_field_success(field)
            except Exception as e:
                result.add_field_error(field, f"Custom validator raised: {e}")
        else:
            # Direct value comparison
            if actual != expected:
                result.add_field_error(
                    field,
                    f"Value mismatch: expected {expected}, got {actual}"
                )
            else:
                result.add_field_success(field)
    
    return result
