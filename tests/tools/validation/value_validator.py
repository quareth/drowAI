"""Value validation for tool arguments.

This module validates that input values conform to expected formats
for common security tool parameters like IP addresses, ports, hostnames, etc.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Type, Union
from urllib.parse import urlparse

from pydantic import BaseModel

from agent.tools.base_tool import BaseTool


@dataclass
class ValueValidationResult:
    """Result of value validation."""
    
    valid: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    field_results: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    
    def add_error(self, field: str, msg: str) -> None:
        self.errors.append(f"{field}: {msg}")
        self.field_results[field] = {"valid": False, "error": msg}
        self.valid = False
    
    def add_warning(self, field: str, msg: str) -> None:
        self.warnings.append(f"{field}: {msg}")
        if field not in self.field_results:
            self.field_results[field] = {"valid": True}
        self.field_results[field]["warning"] = msg
    
    def add_success(self, field: str) -> None:
        self.field_results[field] = {"valid": True}


class ValueValidator:
    """Validates input values for security tools."""
    
    # Common wordlist paths
    COMMON_WORDLIST_PATHS = [
        "/usr/share/wordlists",
        "/usr/share/seclists",
        "/usr/share/dirb/wordlists",
        "/opt/wordlists",
    ]
    
    # Common file extensions for output
    VALID_OUTPUT_EXTENSIONS = {
        ".txt", ".xml", ".json", ".html", ".csv", ".log", ".md",
        ".nmap", ".gnmap", ".lst", ".out"
    }
    
    def validate_ip_address(self, value: str, field: str, result: ValueValidationResult) -> None:
        """Validate an IP address (IPv4 or IPv6)."""
        try:
            ipaddress.ip_address(value)
            result.add_success(field)
        except ValueError:
            result.add_error(field, f"Invalid IP address: {value}")
    
    def validate_ip_network(self, value: str, field: str, result: ValueValidationResult) -> None:
        """Validate a CIDR network notation."""
        try:
            ipaddress.ip_network(value, strict=False)
            result.add_success(field)
        except ValueError:
            result.add_error(field, f"Invalid CIDR notation: {value}")
    
    def validate_hostname(self, value: str, field: str, result: ValueValidationResult) -> None:
        """Validate a hostname."""
        # RFC 1123 hostname pattern
        hostname_pattern = r'^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.[A-Za-z0-9-]{1,63})*\.?$'
        
        if re.match(hostname_pattern, value):
            result.add_success(field)
        else:
            result.add_error(field, f"Invalid hostname: {value}")
    
    def validate_target(self, value: str, field: str, result: ValueValidationResult) -> None:
        """Validate a target (IP, CIDR, hostname, or URL)."""
        # Try URL first
        if value.startswith(("http://", "https://")):
            self.validate_url(value, field, result)
            return
        
        # Try IP address
        try:
            ipaddress.ip_address(value)
            result.add_success(field)
            return
        except ValueError:
            pass
        
        # Try CIDR
        try:
            ipaddress.ip_network(value, strict=False)
            result.add_success(field)
            return
        except ValueError:
            pass
        
        # Try hostname
        self.validate_hostname(value, field, result)
    
    def validate_url(self, value: str, field: str, result: ValueValidationResult) -> None:
        """Validate a URL."""
        try:
            parsed = urlparse(value)
            if parsed.scheme not in ("http", "https", "ftp", "ssh"):
                result.add_warning(field, f"Unusual URL scheme: {parsed.scheme}")
            if not parsed.netloc:
                result.add_error(field, f"URL missing host: {value}")
            else:
                result.add_success(field)
        except Exception as e:
            result.add_error(field, f"Invalid URL: {value} ({e})")
    
    def validate_port(self, value: int, field: str, result: ValueValidationResult) -> None:
        """Validate a port number."""
        if 1 <= value <= 65535:
            result.add_success(field)
        else:
            result.add_error(field, f"Port {value} out of valid range (1-65535)")
    
    def validate_port_spec(self, value: str, field: str, result: ValueValidationResult) -> None:
        """Validate a port specification (e.g., '80', '80,443', '1-1000')."""
        # Handle special cases
        if value == "-" or value == "all":
            result.add_success(field)
            return
        
        parts = value.split(",")
        for part in parts:
            part = part.strip()
            if "-" in part:
                # Range
                try:
                    start, end = map(int, part.split("-"))
                    if not (1 <= start <= 65535) or not (1 <= end <= 65535):
                        result.add_error(field, f"Port range {part} contains invalid ports")
                        return
                    if start > end:
                        result.add_error(field, f"Invalid port range: {start} > {end}")
                        return
                except ValueError:
                    result.add_error(field, f"Invalid port range: {part}")
                    return
            else:
                try:
                    port = int(part)
                    if not (1 <= port <= 65535):
                        result.add_error(field, f"Port {port} out of valid range")
                        return
                except ValueError:
                    result.add_error(field, f"Invalid port specification: {part}")
                    return
        
        result.add_success(field)
    
    def validate_file_path(
        self,
        value: str,
        field: str,
        result: ValueValidationResult,
        must_exist: bool = False,
        check_readable: bool = False,
    ) -> None:
        """Validate a file path."""
        # Check for path traversal attempts
        if ".." in value:
            result.add_warning(field, f"Path contains '..': {value}")
        
        # Check for absolute path safety
        if os.path.isabs(value):
            # Allow common safe paths
            safe_prefixes = [
                "/usr/share/",
                "/opt/",
                "/tmp/",
                "/var/log/",
                "C:\\",
            ]
            if not any(value.startswith(p) for p in safe_prefixes):
                result.add_warning(field, f"Absolute path outside safe directories: {value}")
        
        if must_exist:
            if not os.path.exists(value):
                result.add_error(field, f"File does not exist: {value}")
                return
        
        if check_readable and os.path.exists(value):
            if not os.access(value, os.R_OK):
                result.add_error(field, f"File not readable: {value}")
                return
        
        result.add_success(field)
    
    def validate_output_path(self, value: str, field: str, result: ValueValidationResult) -> None:
        """Validate an output file path."""
        # Check extension
        _, ext = os.path.splitext(value)
        if ext and ext.lower() not in self.VALID_OUTPUT_EXTENSIONS:
            result.add_warning(field, f"Unusual output file extension: {ext}")
        
        # Check for path traversal
        if ".." in value:
            result.add_error(field, f"Output path contains '..': {value}")
            return
        
        # Check directory is writable (if it exists)
        dir_path = os.path.dirname(value)
        if dir_path and os.path.exists(dir_path):
            if not os.access(dir_path, os.W_OK):
                result.add_warning(field, f"Output directory not writable: {dir_path}")
        
        result.add_success(field)
    
    def validate_wordlist_path(self, value: str, field: str, result: ValueValidationResult) -> None:
        """Validate a wordlist file path."""
        # Check if it's a common wordlist path
        is_common = any(value.startswith(p) for p in self.COMMON_WORDLIST_PATHS)
        
        if not is_common and os.path.isabs(value):
            result.add_warning(field, f"Wordlist path not in common location: {value}")
        
        # Wordlists should have reasonable extensions
        valid_extensions = {".txt", ".lst", ".dic", ".wordlist", ""}
        _, ext = os.path.splitext(value)
        if ext.lower() not in valid_extensions:
            result.add_warning(field, f"Unusual wordlist extension: {ext}")
        
        result.add_success(field)
    
    def validate_domain(self, value: str, field: str, result: ValueValidationResult) -> None:
        """Validate a domain name."""
        # Domain pattern (simplified)
        domain_pattern = r'^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$'
        
        if re.match(domain_pattern, value):
            result.add_success(field)
        else:
            result.add_error(field, f"Invalid domain: {value}")
    
    def validate_email(self, value: str, field: str, result: ValueValidationResult) -> None:
        """Validate an email address."""
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        
        if re.match(email_pattern, value):
            result.add_success(field)
        else:
            result.add_error(field, f"Invalid email: {value}")
    
    def validate_timeout(self, value: int, field: str, result: ValueValidationResult) -> None:
        """Validate a timeout value."""
        if value < 0:
            result.add_error(field, f"Timeout cannot be negative: {value}")
        elif value > 86400:  # 24 hours
            result.add_warning(field, f"Very long timeout: {value} seconds")
        else:
            result.add_success(field)
    
    def validate_threads(self, value: int, field: str, result: ValueValidationResult) -> None:
        """Validate thread count."""
        if value < 1:
            result.add_error(field, f"Thread count must be at least 1: {value}")
        elif value > 1000:
            result.add_warning(field, f"High thread count may cause issues: {value}")
        else:
            result.add_success(field)


# Field type detection and validation mapping
FIELD_VALIDATORS = {
    # Field name patterns -> validator method
    r"^target$": "validate_target",
    r"^(host|hostname|ip)$": "validate_target",
    r"^url$": "validate_url",
    r"^(port|ports)$": "validate_port_spec",
    r"^(domain|domains)$": "validate_domain",
    r"^(wordlist|dictionary)$": "validate_wordlist_path",
    r"^(output|output_file|outfile|ofile)$": "validate_output_path",
    r"^(input|input_file|infile)$": "validate_file_path",
    r"^(timeout|wait)$": "validate_timeout",
    r"^(threads|tasks|workers)$": "validate_threads",
    r"^email$": "validate_email",
}


def detect_field_validator(field_name: str) -> Optional[str]:
    """Detect which validator to use for a field based on its name."""
    field_lower = field_name.lower()
    for pattern, validator in FIELD_VALIDATORS.items():
        if re.match(pattern, field_lower):
            return validator
    return None


def validate_tool_args(
    args: BaseModel,
    tool_id: str,
) -> ValueValidationResult:
    """Validate all fields in a tool's arguments.
    
    Args:
        args: The validated Pydantic arguments
        tool_id: The tool identifier
        
    Returns:
        ValueValidationResult with validation details
    """
    result = ValueValidationResult()
    validator = ValueValidator()
    
    for field_name, value in args.model_dump().items():
        if value is None:
            continue
        
        validator_method = detect_field_validator(field_name)
        if validator_method:
            method = getattr(validator, validator_method)
            if isinstance(value, int) and validator_method in ("validate_port_spec", "validate_timeout", "validate_threads"):
                if validator_method == "validate_port_spec":
                    method(str(value), field_name, result)
                else:
                    method(value, field_name, result)
            elif isinstance(value, str):
                method(value, field_name, result)
        else:
            # No specific validator, mark as unchecked
            result.field_results[field_name] = {"valid": True, "checked": False}
    
    return result
