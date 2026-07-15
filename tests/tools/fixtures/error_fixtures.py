"""Error condition fixtures for tool testing.

This module provides standardized error output fixtures for testing
how tools handle various error conditions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class ErrorFixture:
    """Fixture for an error condition."""
    
    name: str
    description: str
    stdout: str
    stderr: str
    exit_code: int
    # Expected behavior
    should_parse_successfully: bool = True
    expected_metadata_keys: List[str] = None
    
    def __post_init__(self):
        if self.expected_metadata_keys is None:
            self.expected_metadata_keys = []


# Common error fixtures applicable to most tools
COMMON_ERROR_FIXTURES: Dict[str, ErrorFixture] = {
    "connection_refused": ErrorFixture(
        name="connection_refused",
        description="Target actively refused connection",
        stdout="",
        stderr="Error: Connection refused\nCould not connect to target host on port 22",
        exit_code=1,
        should_parse_successfully=True,
    ),
    "connection_timeout": ErrorFixture(
        name="connection_timeout",
        description="Connection attempt timed out",
        stdout="Attempting connection...\n",
        stderr="Error: Connection timed out after 30 seconds\nNo route to host",
        exit_code=1,
        should_parse_successfully=True,
    ),
    "dns_resolution_failed": ErrorFixture(
        name="dns_resolution_failed",
        description="DNS lookup failed",
        stdout="",
        stderr="Error: Could not resolve hostname 'nonexistent.invalid'\nName or service not known",
        exit_code=1,
        should_parse_successfully=True,
    ),
    "permission_denied": ErrorFixture(
        name="permission_denied",
        description="Insufficient permissions",
        stdout="",
        stderr="Error: Permission denied\nThis operation requires root privileges",
        exit_code=1,
        should_parse_successfully=True,
    ),
    "authentication_failed": ErrorFixture(
        name="authentication_failed",
        description="Authentication credentials rejected",
        stdout="Attempting authentication...\n",
        stderr="Error: Authentication failed\nInvalid username or password",
        exit_code=1,
        should_parse_successfully=True,
    ),
    "rate_limited": ErrorFixture(
        name="rate_limited",
        description="Rate limited by target",
        stdout="Partial scan results...\n",
        stderr="Warning: Too many requests. Rate limiting in effect.\nRetrying with reduced speed.",
        exit_code=0,
        should_parse_successfully=True,
    ),
    "empty_results": ErrorFixture(
        name="empty_results",
        description="No results found",
        stdout="Scan completed.\nNo results found.",
        stderr="",
        exit_code=0,
        should_parse_successfully=True,
    ),
    "partial_results": ErrorFixture(
        name="partial_results",
        description="Only partial results obtained",
        stdout="Partial results:\n- Host 1: 192.168.1.1\n",
        stderr="Warning: Scan interrupted. Results may be incomplete.",
        exit_code=0,
        should_parse_successfully=True,
    ),
    "binary_not_found": ErrorFixture(
        name="binary_not_found",
        description="Tool binary not installed",
        stdout="",
        stderr="/bin/sh: 1: toolname: not found",
        exit_code=127,
        should_parse_successfully=True,
    ),
    "invalid_arguments": ErrorFixture(
        name="invalid_arguments",
        description="Invalid command line arguments",
        stdout="",
        stderr="Error: Invalid option '--invalid'\nUsage: tool [options] target",
        exit_code=1,
        should_parse_successfully=True,
    ),
    "out_of_memory": ErrorFixture(
        name="out_of_memory",
        description="Process ran out of memory",
        stdout="Processing large dataset...\n",
        stderr="Error: Cannot allocate memory\nKilled",
        exit_code=137,
        should_parse_successfully=True,
    ),
    "disk_full": ErrorFixture(
        name="disk_full",
        description="No space left on device",
        stdout="Writing output...\n",
        stderr="Error: No space left on device\nCould not write to output file",
        exit_code=1,
        should_parse_successfully=True,
    ),
    "segmentation_fault": ErrorFixture(
        name="segmentation_fault",
        description="Tool crashed with segfault",
        stdout="",
        stderr="Segmentation fault (core dumped)",
        exit_code=139,
        should_parse_successfully=True,
    ),
    "keyboard_interrupt": ErrorFixture(
        name="keyboard_interrupt",
        description="User interrupted execution",
        stdout="Scanning...\n",
        stderr="^C\nInterrupted",
        exit_code=130,
        should_parse_successfully=True,
    ),
    "ssl_error": ErrorFixture(
        name="ssl_error",
        description="SSL/TLS handshake failed",
        stdout="",
        stderr="Error: SSL handshake failed\nCertificate verification failed",
        exit_code=1,
        should_parse_successfully=True,
    ),
    "malformed_response": ErrorFixture(
        name="malformed_response",
        description="Server returned malformed response",
        stdout="Received response:\n\x00\xff\xfe\x00garbage\n",
        stderr="Warning: Received malformed response from server",
        exit_code=0,
        should_parse_successfully=True,
    ),
}


# Tool-specific error fixtures
TOOL_ERROR_FIXTURES: Dict[str, Dict[str, ErrorFixture]] = {
    "nmap": {
        "no_hosts_up": ErrorFixture(
            name="no_hosts_up",
            description="No hosts responded to scan",
            stdout="""Starting Nmap 7.93 ( https://nmap.org )
Note: Host seems down. If it is really up, but blocking our ping probes, try -Pn
Nmap done: 1 IP address (0 hosts up) scanned in 3.05 seconds
""",
            stderr="",
            exit_code=0,
            should_parse_successfully=True,
        ),
        "firewall_filtered": ErrorFixture(
            name="firewall_filtered",
            description="All ports filtered by firewall",
            stdout="""Starting Nmap 7.93 ( https://nmap.org )
Nmap scan report for 192.168.1.1
Host is up.
All 1000 scanned ports on 192.168.1.1 are filtered

Nmap done: 1 IP address (1 host up) scanned in 21.42 seconds
""",
            stderr="",
            exit_code=0,
            should_parse_successfully=True,
        ),
    },
    "hydra": {
        "no_valid_passwords": ErrorFixture(
            name="no_valid_passwords",
            description="No valid credentials found",
            stdout="""Hydra v9.4 (c) 2022 by van Hauser/THC
[DATA] max 16 tasks per 1 server, overall 16 tasks
[DATA] attacking ssh://192.168.1.1:22/
0 of 1 target(s) successfully completed, 0 valid password(s) found
""",
            stderr="",
            exit_code=0,
            should_parse_successfully=True,
        ),
        "service_not_responding": ErrorFixture(
            name="service_not_responding",
            description="Target service not responding",
            stdout="",
            stderr="""[ERROR] target ssh://192.168.1.1:22/ does not support password authentication
""",
            exit_code=255,
            should_parse_successfully=True,
        ),
    },
    "gobuster": {
        "no_directories_found": ErrorFixture(
            name="no_directories_found",
            description="No directories discovered",
            stdout="""===============================================================
Gobuster v3.5
===============================================================
[+] Url:                     http://example.com
[+] Method:                  GET
[+] Threads:                 10
===============================================================
===============================================================
Finished
===============================================================
""",
            stderr="",
            exit_code=0,
            should_parse_successfully=True,
        ),
        "all_forbidden": ErrorFixture(
            name="all_forbidden",
            description="All paths returned 403",
            stdout="""===============================================================
Gobuster v3.5
===============================================================
Error: the server returns a status code that matches the provided options for non existing urls. http://example.com/test => 403. To force processing of Wildcard responses, specify the '-fw' switch
""",
            stderr="",
            exit_code=1,
            should_parse_successfully=True,
        ),
    },
    "sqlmap": {
        "not_injectable": ErrorFixture(
            name="not_injectable",
            description="No injection points found",
            stdout="""[*] starting @ 12:00:00 /2024-01-01/

[12:00:01] [INFO] testing connection to the target URL
[12:00:02] [INFO] testing if the target URL content is stable
[12:00:03] [INFO] target URL content is stable
[12:00:04] [WARNING] GET parameter 'id' does not seem to be injectable

[*] ending @ 12:00:05 /2024-01-01/
""",
            stderr="",
            exit_code=0,
            should_parse_successfully=True,
        ),
        "waf_detected": ErrorFixture(
            name="waf_detected",
            description="WAF/IPS detected",
            stdout="""[*] starting @ 12:00:00 /2024-01-01/

[12:00:01] [INFO] testing connection to the target URL
[12:00:02] [WARNING] heuristic (basic) test shows that GET parameter 'id' might be injectable
[12:00:03] [CRITICAL] WAF/IPS identified - consider using tamper scripts

[*] ending @ 12:00:04 /2024-01-01/
""",
            stderr="",
            exit_code=0,
            should_parse_successfully=True,
        ),
    },
    "nikto": {
        "no_vulnerabilities": ErrorFixture(
            name="no_vulnerabilities",
            description="No vulnerabilities found",
            stdout="""- Nikto v2.5.0
---------------------------------------------------------------------------
+ Target IP:          192.168.1.1
+ Target Hostname:    example.com
+ Target Port:        80
+ Start Time:         2024-01-01 12:00:00 (GMT0)
---------------------------------------------------------------------------
+ Server: nginx/1.18.0
+ No CGI Directories found
+ 0 host(s) tested
""",
            stderr="",
            exit_code=0,
            should_parse_successfully=True,
        ),
    },
}


def get_error_fixture(tool_name: str, error_type: str) -> Optional[ErrorFixture]:
    """Get an error fixture for a tool.
    
    Args:
        tool_name: The tool name (e.g., "nmap")
        error_type: The error type (e.g., "connection_refused")
        
    Returns:
        ErrorFixture if found, None otherwise
    """
    # Check tool-specific fixtures first
    if tool_name in TOOL_ERROR_FIXTURES:
        if error_type in TOOL_ERROR_FIXTURES[tool_name]:
            return TOOL_ERROR_FIXTURES[tool_name][error_type]
    
    # Fall back to common fixtures
    return COMMON_ERROR_FIXTURES.get(error_type)


def get_all_error_fixtures(tool_name: str) -> Dict[str, ErrorFixture]:
    """Get all error fixtures applicable to a tool.
    
    Args:
        tool_name: The tool name
        
    Returns:
        Dict of error_type -> ErrorFixture
    """
    fixtures = dict(COMMON_ERROR_FIXTURES)
    
    if tool_name in TOOL_ERROR_FIXTURES:
        fixtures.update(TOOL_ERROR_FIXTURES[tool_name])
    
    return fixtures


def list_available_errors() -> List[str]:
    """List all available error types."""
    errors = set(COMMON_ERROR_FIXTURES.keys())
    for tool_errors in TOOL_ERROR_FIXTURES.values():
        errors.update(tool_errors.keys())
    return sorted(errors)
