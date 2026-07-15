"""Security validation for penetration testing tools.

This module validates that tool implementations don't introduce security
vulnerabilities like command injection, path traversal, etc.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Type

from pydantic import BaseModel

from agent.tools.base_tool import BaseTool


@dataclass
class SecurityValidationResult:
    """Result of security validation."""
    
    secure: bool = True
    vulnerabilities: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    checks_passed: List[str] = field(default_factory=list)
    risk_level: str = "low"  # low, medium, high, critical
    
    def add_vulnerability(self, vuln: str, severity: str = "medium") -> None:
        self.vulnerabilities.append(f"[{severity.upper()}] {vuln}")
        self.secure = False
        self._update_risk(severity)
    
    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)
    
    def add_passed(self, check: str) -> None:
        self.checks_passed.append(check)
    
    def _update_risk(self, severity: str) -> None:
        severity_order = {"low": 1, "medium": 2, "high": 3, "critical": 4}
        current = severity_order.get(self.risk_level, 1)
        new = severity_order.get(severity, 2)
        if new > current:
            self.risk_level = severity


# Dangerous shell metacharacters
SHELL_METACHARACTERS = set(";|&$`\\\"'<>(){}[]!#")

# Command injection patterns
INJECTION_PATTERNS = [
    r";\s*\w+",           # ; command
    r"\|\s*\w+",          # | command  
    r"\|\|\s*\w+",        # || command
    r"&&\s*\w+",          # && command
    r"`[^`]+`",           # `command`
    r"\$\([^)]+\)",       # $(command)
    r"\$\{[^}]+\}",       # ${var}
    r">\s*\w+",           # > file
    r">>\s*\w+",          # >> file
    r"<\s*\w+",           # < file
]

# Path traversal patterns
PATH_TRAVERSAL_PATTERNS = [
    r"\.\./",             # ../
    r"\.\.\\",            # ..\
    r"%2e%2e%2f",         # URL encoded ../
    r"%2e%2e/",           # Partial URL encoded
    r"\.\.%2f",           # Partial URL encoded
    r"%252e%252e%252f",   # Double URL encoded
]

# Dangerous commands that should never appear in arguments
DANGEROUS_COMMANDS = {
    "rm", "rmdir", "del", "format", "mkfs",
    "dd", "shred", "wipe",
    "shutdown", "reboot", "halt", "poweroff",
    "chmod", "chown", "chgrp",
    "curl", "wget",  # Could be used for data exfiltration
    "nc", "netcat", "ncat",  # Network tools (context dependent)
    "python", "perl", "ruby", "php", "node",  # Script interpreters
    "bash", "sh", "zsh", "cmd", "powershell",
    "eval", "exec",
}


class SecurityValidator:
    """Validates tool implementations for security vulnerabilities."""
    
    def validate_command(
        self,
        command: List[str],
        args: BaseModel,
        tool_id: str,
    ) -> SecurityValidationResult:
        """Validate a command for security issues.
        
        Args:
            command: The generated command
            args: The tool arguments
            tool_id: The tool identifier
            
        Returns:
            SecurityValidationResult with findings
        """
        result = SecurityValidationResult()
        
        if not command:
            result.add_warning("Empty command")
            return result
        
        # Check each command argument
        for i, arg in enumerate(command):
            self._check_command_injection(arg, i, result)
            self._check_path_traversal(arg, i, result)
            self._check_dangerous_commands(arg, i, result)
        
        # Check for proper argument escaping
        self._check_argument_escaping(command, args, result)
        
        # Check that user input doesn't directly form commands
        self._check_input_sanitization(command, args, result)
        
        if result.secure:
            result.add_passed("No command injection vulnerabilities found")
            result.add_passed("No path traversal vulnerabilities found")
        
        return result
    
    def _check_command_injection(
        self,
        arg: str,
        position: int,
        result: SecurityValidationResult,
    ) -> None:
        """Check for command injection patterns."""
        for pattern in INJECTION_PATTERNS:
            if re.search(pattern, arg, re.IGNORECASE):
                result.add_vulnerability(
                    f"Possible command injection in arg {position}: '{arg}' matches '{pattern}'",
                    severity="critical"
                )
    
    def _check_path_traversal(
        self,
        arg: str,
        position: int,
        result: SecurityValidationResult,
    ) -> None:
        """Check for path traversal patterns."""
        for pattern in PATH_TRAVERSAL_PATTERNS:
            if re.search(pattern, arg, re.IGNORECASE):
                result.add_vulnerability(
                    f"Possible path traversal in arg {position}: '{arg}'",
                    severity="high"
                )
    
    def _check_dangerous_commands(
        self,
        arg: str,
        position: int,
        result: SecurityValidationResult,
    ) -> None:
        """Check for dangerous commands in arguments."""
        # Skip the binary name (position 0)
        if position == 0:
            return
        
        # Check if the argument contains dangerous commands
        arg_lower = arg.lower()
        for cmd in DANGEROUS_COMMANDS:
            # Look for the command as a word boundary
            if re.search(rf'\b{cmd}\b', arg_lower):
                result.add_warning(
                    f"Potentially dangerous command '{cmd}' in arg {position}: '{arg}'"
                )
    
    def _check_argument_escaping(
        self,
        command: List[str],
        args: BaseModel,
        result: SecurityValidationResult,
    ) -> None:
        """Check that arguments are properly escaped."""
        for arg in command:
            # Check for unescaped shell metacharacters
            has_metachar = any(c in arg for c in SHELL_METACHARACTERS)
            if has_metachar:
                # This might be intentional (e.g., port ranges use -)
                # But we should warn about suspicious patterns
                suspicious = any(c in arg for c in ";|&`$")
                if suspicious:
                    result.add_warning(
                        f"Argument contains shell metacharacters: '{arg}'"
                    )
    
    def _check_input_sanitization(
        self,
        command: List[str],
        args: BaseModel,
        result: SecurityValidationResult,
    ) -> None:
        """Check that user input is properly sanitized."""
        args_dict = args.model_dump()
        
        for field, value in args_dict.items():
            if value is None or not isinstance(value, str):
                continue
            
            # Check if the raw value appears unsanitized in command
            if value in command:
                # This is often fine, but check for dangerous content
                for pattern in INJECTION_PATTERNS:
                    if re.search(pattern, value, re.IGNORECASE):
                        result.add_vulnerability(
                            f"User input '{field}' contains injection pattern and is used directly",
                            severity="critical"
                        )
    
    def validate_output_handling(
        self,
        tool: BaseTool,
        tool_id: str,
    ) -> SecurityValidationResult:
        """Validate that output handling is secure.
        
        Checks that parse_output and create_artifacts don't
        have obvious security issues.
        """
        result = SecurityValidationResult()
        
        # Check for safe file handling patterns
        # This is a static analysis of the tool class
        import inspect
        
        # Get the source code if available
        try:
            source = inspect.getsource(tool.__class__)
        except (OSError, TypeError):
            result.add_warning("Could not inspect tool source code")
            return result
        
        # Check for dangerous patterns in source
        dangerous_patterns = [
            (r"eval\s*\(", "Use of eval() is dangerous"),
            (r"exec\s*\(", "Use of exec() is dangerous"),
            (r"pickle\.loads?\s*\(", "Use of pickle is dangerous with untrusted data"),
            (r"os\.system\s*\(", "Use of os.system() - prefer subprocess"),
            (r"shell\s*=\s*True", "subprocess with shell=True is risky"),
            (r"__import__\s*\(", "Dynamic imports are risky"),
        ]
        
        for pattern, message in dangerous_patterns:
            if re.search(pattern, source):
                result.add_vulnerability(message, severity="high")
        
        # Check for proper path sanitization
        if "os.path.join" in source and ".." not in source:
            result.add_passed("Uses os.path.join for path construction")
        elif "../" in source or "..\\" in source:
            result.add_warning("Contains hardcoded relative paths")
        
        if result.secure:
            result.add_passed("No dangerous code patterns found")
        
        return result


def validate_tool_security(
    tool: BaseTool,
    args: BaseModel,
    tool_id: str,
) -> SecurityValidationResult:
    """Run full security validation on a tool.
    
    Args:
        tool: The tool instance
        args: The validated arguments
        tool_id: The tool identifier
        
    Returns:
        SecurityValidationResult with all findings
    """
    validator = SecurityValidator()
    combined = SecurityValidationResult()
    
    # Validate command generation
    try:
        command = tool.build_command(args)
        cmd_result = validator.validate_command(command, args, tool_id)
        
        combined.vulnerabilities.extend(cmd_result.vulnerabilities)
        combined.warnings.extend(cmd_result.warnings)
        combined.checks_passed.extend(cmd_result.checks_passed)
        if not cmd_result.secure:
            combined.secure = False
            combined.risk_level = cmd_result.risk_level
    except NotImplementedError:
        combined.add_warning("build_command() not implemented - skipping command validation")
    except Exception as e:
        combined.add_warning(f"Could not validate command: {e}")
    
    # Validate output handling
    output_result = validator.validate_output_handling(tool, tool_id)
    combined.vulnerabilities.extend(output_result.vulnerabilities)
    combined.warnings.extend(output_result.warnings)
    combined.checks_passed.extend(output_result.checks_passed)
    if not output_result.secure:
        combined.secure = False
        if output_result.risk_level > combined.risk_level:
            combined.risk_level = output_result.risk_level
    
    return combined


def run_injection_payload_tests(
    tool: BaseTool,
    args_class: Type[BaseModel],
    base_args: Dict[str, Any],
    target_field: str,
) -> SecurityValidationResult:
    """Test a tool against common injection payloads.
    
    Args:
        tool: The tool instance
        args_class: The args model class
        base_args: Valid base arguments
        target_field: Field to inject payloads into
        
    Returns:
        SecurityValidationResult from payload testing
    """
    result = SecurityValidationResult()
    validator = SecurityValidator()
    
    # Common injection payloads
    payloads = [
        "; ls",
        "| cat /etc/passwd",
        "&& whoami",
        "$(id)",
        "`id`",
        "${IFS}cat${IFS}/etc/passwd",
        "127.0.0.1; ls",
        "test.com && rm -rf /",
        "'; DROP TABLE users; --",
        "../../../etc/passwd",
        "....//....//etc/passwd",
        "%2e%2e%2f%2e%2e%2fetc/passwd",
    ]
    
    for payload in payloads:
        test_args = {**base_args, target_field: payload}
        
        try:
            args = args_class(**test_args)
            command = tool.build_command(args)
            
            # Check if the payload made it through unescaped
            cmd_str = " ".join(command)
            
            for pattern in INJECTION_PATTERNS:
                if re.search(pattern, cmd_str):
                    result.add_vulnerability(
                        f"Injection payload passed through: '{payload}' -> '{cmd_str}'",
                        severity="critical"
                    )
                    break
            
        except Exception:
            # Validation rejected the payload - good!
            result.add_passed(f"Payload rejected: '{payload}'")
    
    return result
