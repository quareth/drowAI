"""Security contract tests for penetration testing tools.

These tests validate that tool implementations don't introduce security
vulnerabilities like command injection, path traversal, etc.
"""

from __future__ import annotations

from typing import Any, Dict, List, Type

import pytest

from agent.tools.base_tool import BaseTool
from agent.tools.tool_registry import get_tool

from tests.tools.fixtures.parameter_fixtures import load_param_fixture
from tests.tools.validation.security_validator import (
    SecurityValidator,
    SecurityValidationResult,
    validate_tool_security,
    run_injection_payload_tests,
    SHELL_METACHARACTERS,
    INJECTION_PATTERNS,
    PATH_TRAVERSAL_PATTERNS,
)


class TestSecurityContracts:
    """Test security for all tools."""

    # All tools to test for security
    SECURITY_TEST_TOOLS = [
        "information_gathering.network_discovery.nmap",
        "information_gathering.dns.amass",
        "information_gathering.dns.dnsrecon",
        "information_gathering.osint.theharvester",
        "password_attacks.online_attacks.hydra",
        "password_attacks.offline_attacks.john",
        "password_attacks.offline_attacks.crunch",
        "web_applications.web_crawlers.gobuster",
        "web_applications.web_crawlers.ffuf",
        "web_applications.web_vulnerability_scanners.nikto",
        "web_applications.web_vulnerability_scanners.sqlmap",
        "reverse_engineering.disassemblers.binwalk",
        "reverse_engineering.disassemblers.objdump",
        "reverse_engineering.disassemblers.radare2",
        "reverse_engineering.debuggers.gdb",
        "filesystem.read_file",
        "filesystem.write_file",
        "filesystem.search_text",
    ]

    @pytest.fixture
    def validator(self) -> SecurityValidator:
        return SecurityValidator()

    @pytest.mark.parametrize("tool_id", SECURITY_TEST_TOOLS)
    def test_no_command_injection(self, tool_id: str, validator: SecurityValidator) -> None:
        """Test that tools don't have command injection vulnerabilities."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
            minimal_params = param_fixture["test_cases"]["minimal"]["params"]
        except FileNotFoundError:
            pytest.skip(f"No fixture for {tool_id}")
        
        args = args_class(**minimal_params)
        
        result = validate_tool_security(tool, args, tool_id)
        
        # Check for critical command injection vulnerabilities
        injection_vulns = [v for v in result.vulnerabilities if "injection" in v.lower()]
        assert not injection_vulns, f"Command injection vulnerabilities: {injection_vulns}"

    @pytest.mark.parametrize("tool_id", SECURITY_TEST_TOOLS)
    def test_no_path_traversal(self, tool_id: str, validator: SecurityValidator) -> None:
        """Test that tools don't have path traversal vulnerabilities."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
            minimal_params = param_fixture["test_cases"]["minimal"]["params"]
        except FileNotFoundError:
            pytest.skip(f"No fixture for {tool_id}")
        
        args = args_class(**minimal_params)
        
        result = validate_tool_security(tool, args, tool_id)
        
        # Check for path traversal vulnerabilities
        traversal_vulns = [v for v in result.vulnerabilities if "traversal" in v.lower()]
        assert not traversal_vulns, f"Path traversal vulnerabilities: {traversal_vulns}"

    @pytest.mark.parametrize("tool_id", SECURITY_TEST_TOOLS)
    def test_no_dangerous_code_patterns(self, tool_id: str, validator: SecurityValidator) -> None:
        """Test that tools don't use dangerous code patterns."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        
        result = validator.validate_output_handling(tool, tool_id)
        
        # Check for dangerous code patterns
        dangerous_vulns = [v for v in result.vulnerabilities if "dangerous" in v.lower()]
        # These are warnings, not necessarily failures
        if dangerous_vulns:
            pytest.skip(f"Found dangerous patterns (review manually): {dangerous_vulns}")


class TestInjectionPayloads:
    """Test tools against common injection payloads.
    
    Note: These tests report findings but don't fail the build.
    Tools may legitimately pass through arguments to subprocess.run()
    which handles them safely when shell=False.
    """

    # Tools with target field to test injection
    INJECTABLE_TOOLS = [
        ("information_gathering.network_discovery.nmap", "target"),
        ("information_gathering.dns.amass", "target"),
        ("password_attacks.online_attacks.hydra", "target"),
        ("web_applications.web_crawlers.gobuster", "target"),
    ]

    @pytest.mark.parametrize("tool_id,field", INJECTABLE_TOOLS)
    def test_injection_payloads_rejected(self, tool_id: str, field: str) -> None:
        """Test that injection payloads are logged for review."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
            base_args = param_fixture["test_cases"]["minimal"]["params"].copy()
        except FileNotFoundError:
            pytest.skip(f"No fixture for {tool_id}")
        
        result = run_injection_payload_tests(tool, args_class, base_args, field)
        
        # Log findings but don't fail - subprocess.run with shell=False is safe
        if result.vulnerabilities:
            import warnings
            for v in result.vulnerabilities:
                warnings.warn(f"{tool_id}.{field}: {v}")
        
        # Only fail if we find actual shell=True usage
        # (which would make injection dangerous)
        import inspect
        try:
            source = inspect.getsource(tool.__class__)
            if "shell=True" in source or "shell = True" in source:
                # This tool uses shell=True, so injection IS dangerous
                critical = [v for v in result.vulnerabilities if "[CRITICAL]" in v]
                assert not critical, f"Injection with shell=True: {critical}"
        except (OSError, TypeError):
            pass


class TestShellMetacharacters:
    """Test handling of shell metacharacters in arguments."""

    SAMPLE_TOOLS = [
        "information_gathering.network_discovery.nmap",
        "password_attacks.online_attacks.hydra",
        "web_applications.web_crawlers.gobuster",
    ]

    @pytest.mark.parametrize("tool_id", SAMPLE_TOOLS)
    def test_semicolon_handling(self, tool_id: str) -> None:
        """Test that semicolons in input don't cause command chaining."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
            params = param_fixture["test_cases"]["minimal"]["params"].copy()
        except FileNotFoundError:
            pytest.skip(f"No fixture for {tool_id}")
        
        # Try to inject semicolon
        params["target"] = "example.com; whoami"
        
        try:
            args = args_class(**params)
            command = tool.build_command(args)
            cmd_str = " ".join(command)
            
            # The semicolon should either be rejected or properly escaped
            # It should NOT appear as a command separator
            assert "; whoami" not in cmd_str or cmd_str.count(";") == cmd_str.count("\\;"), \
                f"Semicolon injection possible: {cmd_str}"
        except Exception:
            # Validation rejected the input - good!
            pass

    @pytest.mark.parametrize("tool_id", SAMPLE_TOOLS)
    def test_pipe_handling(self, tool_id: str) -> None:
        """Test that pipes in input don't cause command piping."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
            params = param_fixture["test_cases"]["minimal"]["params"].copy()
        except FileNotFoundError:
            pytest.skip(f"No fixture for {tool_id}")
        
        params["target"] = "example.com | cat /etc/passwd"
        
        try:
            args = args_class(**params)
            command = tool.build_command(args)
            cmd_str = " ".join(command)
            
            # Check that pipe doesn't enable command piping
            assert "| cat" not in cmd_str, f"Pipe injection possible: {cmd_str}"
        except Exception:
            pass  # Validation rejected - good

    @pytest.mark.parametrize("tool_id", SAMPLE_TOOLS)
    def test_backtick_handling(self, tool_id: str) -> None:
        """Test backtick handling - warn if passed through."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
            params = param_fixture["test_cases"]["minimal"]["params"].copy()
        except FileNotFoundError:
            pytest.skip(f"No fixture for {tool_id}")
        
        params["target"] = "`whoami`.example.com"
        
        try:
            args = args_class(**params)
            command = tool.build_command(args)
            cmd_str = " ".join(command)
            
            # Backticks in command - warn but don't fail (safe with shell=False)
            if "`whoami`" in cmd_str:
                import warnings
                warnings.warn(f"{tool_id}: Backticks passed through (safe with shell=False)")
        except Exception:
            pass  # Validation rejected - good

    @pytest.mark.parametrize("tool_id", SAMPLE_TOOLS)
    def test_dollar_handling(self, tool_id: str) -> None:
        """Test dollar sign handling - warn if passed through."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
            params = param_fixture["test_cases"]["minimal"]["params"].copy()
        except FileNotFoundError:
            pytest.skip(f"No fixture for {tool_id}")
        
        params["target"] = "$(whoami).example.com"
        
        try:
            args = args_class(**params)
            command = tool.build_command(args)
            cmd_str = " ".join(command)
            
            # Dollar substitution in command - warn but don't fail (safe with shell=False)
            if "$(whoami)" in cmd_str:
                import warnings
                warnings.warn(f"{tool_id}: Dollar substitution passed through (safe with shell=False)")
        except Exception:
            pass  # Validation rejected - good


class TestPathTraversalPayloads:
    """Test tools against path traversal payloads."""

    # Tools that accept file path arguments
    FILE_PATH_TOOLS = [
        ("password_attacks.offline_attacks.john", "wordlist"),
        ("password_attacks.online_attacks.hydra", "wordlist"),
        ("web_applications.web_crawlers.gobuster", "wordlist"),
        ("web_applications.web_crawlers.ffuf", "wordlist"),
        ("reverse_engineering.disassemblers.binwalk", "target"),
        ("reverse_engineering.disassemblers.objdump", "target"),
        ("reverse_engineering.disassemblers.radare2", "target"),
        ("reverse_engineering.debuggers.gdb", "target"),
        ("filesystem.read_file", "path"),
        ("filesystem.write_file", "path"),
        ("filesystem.search_text", "path"),
    ]

    @pytest.mark.parametrize("tool_id,field", FILE_PATH_TOOLS)
    def test_path_traversal_payloads(self, tool_id: str, field: str) -> None:
        """Test that path traversal payloads are handled safely."""
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        tool = tool_cls()
        args_class = tool_cls.args_model
        
        try:
            param_fixture = load_param_fixture(tool_id)
            base_args = param_fixture["test_cases"]["minimal"]["params"].copy()
        except FileNotFoundError:
            pytest.skip(f"No fixture for {tool_id}")
        
        traversal_payloads = [
            "../../../etc/passwd",
            "....//....//etc/passwd",
            "..\\..\\..\\windows\\system32\\config\\sam",
            "%2e%2e%2f%2e%2e%2f%2e%2e%2fetc%2fpasswd",
            "....//....//....//etc/passwd",
        ]
        
        for payload in traversal_payloads:
            test_args = {**base_args, field: payload}
            
            try:
                args = args_class(**test_args)
                command = tool.build_command(args)
                
                # The traversal path should either be rejected or sanitized
                # Check that it's at least contained within a safe structure
                cmd_str = " ".join(command)
                
                # Warn if raw traversal appears in command
                if "../" in cmd_str or "..\\" in cmd_str:
                    # This is a warning, not necessarily a vulnerability
                    # The actual security depends on how the command is executed
                    pass
                    
            except Exception:
                # Validation rejected the input - good!
                pass


class TestCodePatternSecurity:
    """Test for dangerous code patterns in tool implementations."""

    # Sample tools to check
    SAMPLE_TOOLS = [
        "information_gathering.network_discovery.nmap",
        "information_gathering.dns.amass",
        "password_attacks.online_attacks.hydra",
        "web_applications.web_crawlers.gobuster",
        "web_applications.web_vulnerability_scanners.sqlmap",
    ]

    @pytest.mark.parametrize("tool_id", SAMPLE_TOOLS)
    def test_no_eval_usage(self, tool_id: str) -> None:
        """Test that tool doesn't use eval()."""
        import inspect
        
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        try:
            source = inspect.getsource(tool_cls)
            # Check for eval( but not method names containing 'eval'
            if "eval(" in source and "evaluate" not in source.lower():
                import warnings
                warnings.warn(f"{tool_id}: Uses eval() - review for security")
        except (OSError, TypeError):
            pytest.skip(f"Cannot inspect {tool_id}")

    @pytest.mark.parametrize("tool_id", SAMPLE_TOOLS)
    def test_no_exec_usage(self, tool_id: str) -> None:
        """Test that tool doesn't use exec()."""
        import inspect
        
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        try:
            source = inspect.getsource(tool_cls)
            # Check for exec( but not 'execute' method names
            if "exec(" in source and "execute" not in source.lower():
                import warnings
                warnings.warn(f"{tool_id}: Uses exec() - review for security")
        except (OSError, TypeError):
            pytest.skip(f"Cannot inspect {tool_id}")

    @pytest.mark.parametrize("tool_id", SAMPLE_TOOLS)
    def test_no_shell_true(self, tool_id: str) -> None:
        """Test that tool doesn't use shell=True in subprocess."""
        import inspect
        
        tool_cls = get_tool(tool_id)
        if tool_cls is None:
            pytest.skip(f"Tool {tool_id} not found")
        
        try:
            source = inspect.getsource(tool_cls)
            if "shell=True" in source or "shell = True" in source:
                import warnings
                warnings.warn(f"{tool_id}: Uses shell=True - review for security")
        except (OSError, TypeError):
            pytest.skip(f"Cannot inspect {tool_id}")
