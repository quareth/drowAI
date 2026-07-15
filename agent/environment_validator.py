"""Environment validation utilities for the penetration testing agent."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import List
import requests


@dataclass
class ValidationResult:
    """Results from environment validation."""
    
    is_valid: bool
    errors: List[str]
    warnings: List[str]
    info: List[str] = None
    
    def __post_init__(self):
        if self.info is None:
            self.info = []
    
    def add_error(self, message: str) -> None:
        """Add an error message."""
        self.errors.append(message)
        self.is_valid = False
    
    def add_warning(self, message: str) -> None:
        """Add a warning message."""
        self.warnings.append(message)
    
    def add_info(self, message: str) -> None:
        """Add an informational message."""
        self.info.append(message)


class EnvironmentValidator:
    """Validates environment - uses existing utilities"""
    
    def __init__(self):
        self.workspace_path = os.getenv("WORKSPACE", "/workspace")
        self.scope_file_path = os.path.join(self.workspace_path, "scope.md")
        self.required_tools = ["nmap", "gobuster", "nikto", "sqlmap"]
    
    def validate_all(self) -> ValidationResult:
        """Validate complete environment setup."""
        result = ValidationResult(is_valid=True, errors=[], warnings=[])
        
        # Validate workspace
        self._validate_workspace(result)
        
        # Validate scope document
        self._validate_scope_document(result)
        
        # Validate OpenAI API key
        self._validate_openai_key(result)
        
        # Validate required tools
        self._validate_tools(result)
        
        # Validate network connectivity
        self._validate_network(result)
        
        return result
    
    def _validate_workspace(self, result: ValidationResult) -> None:
        """Validate workspace directory and permissions."""
        if not os.path.exists(self.workspace_path):
            result.add_error(f"Workspace directory does not exist: {self.workspace_path}")
            return
        
        if not os.path.isdir(self.workspace_path):
            result.add_error(f"Workspace path is not a directory: {self.workspace_path}")
            return
        
        # Check write permissions
        test_file = os.path.join(self.workspace_path, ".write_test")
        try:
            with open(test_file, 'w') as f:
                f.write("test")
            os.remove(test_file)
        except (OSError, IOError, PermissionError) as e:
            result.add_error(f"Workspace is not writable: {str(e)}")
    
    def _validate_scope_document(self, result: ValidationResult) -> None:
        """Validate scope document existence and readability."""
        if not os.path.exists(self.scope_file_path):
            result.add_error(f"Scope document not found: {self.scope_file_path}")
            return
        
        if not os.path.isfile(self.scope_file_path):
            result.add_error(f"Scope path is not a file: {self.scope_file_path}")
            return
        
        try:
            with open(self.scope_file_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if not content:
                    result.add_error("Scope document is empty")
                elif len(content) < 50:
                    result.add_warning("Scope document appears very short")
        except (OSError, IOError, UnicodeDecodeError) as e:
            result.add_error(f"Cannot read scope document: {str(e)}")
    
    def _validate_openai_key(self, result: ValidationResult) -> None:
        """Validate OpenAI API key format and presence."""
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            result.add_error("OPENAI_API_KEY environment variable not set")
            return
        
        # Basic format validation
        if not api_key.startswith("sk-"):
            result.add_warning("OpenAI API key format appears incorrect (should start with 'sk-')")
        
        if len(api_key) < 40:
            result.add_warning("OpenAI API key appears too short")
    
    def _validate_tools(self, result: ValidationResult) -> None:
        """Validate required tools availability."""
        # Skip tool validation if running on host for testing
        if os.getenv("SKIP_TOOL_VALIDATION") == "true":
            result.add_warning("Tool validation skipped (SKIP_TOOL_VALIDATION=true)")
            return
            
        for tool in self.required_tools:
            tool_path = shutil.which(tool)
            if not tool_path:
                result.add_error(f"Required tool not found: {tool}")
            else:
                # Check if tool is executable
                if not os.access(tool_path, os.X_OK):
                    result.add_error(f"Tool not executable: {tool} at {tool_path}")
    
    def _validate_network(self, result: ValidationResult) -> None:
        """Validate network connectivity and OpenAI API access."""
        # Test OpenAI API connectivity with actual API key
        api_key = os.getenv("OPENAI_API_KEY")
        if api_key and api_key.startswith("sk-"):
            try:
                # Test with a real API endpoint that validates the key
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                }
                response = requests.get(
                    "https://api.openai.com/v1/models", 
                    headers=headers, 
                    timeout=10
                )
                if response.status_code == 200:
                    result.add_info("OpenAI API connection successful")
                elif response.status_code == 401:
                    result.add_error("OpenAI API key is invalid or expired")
                elif response.status_code == 403:
                    result.add_warning("OpenAI API access forbidden - check account status")
                else:
                    result.add_warning(f"OpenAI API returned status: {response.status_code}")
            except requests.RequestException as e:
                result.add_warning(f"OpenAI API connectivity test failed: {str(e)}")
        else:
            # Basic internet connectivity test without API key
            try:
                response = requests.get("https://httpbin.org/status/200", timeout=10)
                if response.status_code == 200:
                    result.add_info("Basic internet connectivity confirmed")
                else:
                    result.add_warning(f"Connectivity test returned: {response.status_code}")
            except requests.RequestException as e:
                result.add_warning(f"Network connectivity test failed: {str(e)}")
        
        # Test DNS resolution
        try:
            import socket
            socket.gethostbyname("google.com")
        except socket.gaierror as e:
            result.add_error(f"DNS resolution test failed: {str(e)}")
        except Exception as e:
            result.add_warning(f"DNS test could not be performed: {str(e)}")