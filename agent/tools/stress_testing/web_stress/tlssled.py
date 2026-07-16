"""Provide tlssled TLS testing arguments and command execution."""

import subprocess
import re
from typing import Dict, Any, Optional
from enum import Enum
from pydantic import Field

from agent.tools.base_tool import BaseTool
from agent.tools.schemas import BaseToolArgs, ToolResult

class TLSSledMode(str, Enum):
    """TLSSled operation modes"""
    SCAN = "scan"
    TEST = "test"
    BENCHMARK = "benchmark"
    STRESS = "stress"
    VULNERABILITY = "vulnerability"
    CONFIG = "config"
    HELP = "help"
    VERSION = "version"

class TLSSledProtocol(str, Enum):
    """TLS protocols supported"""
    TLS_1_0 = "tls1.0"
    TLS_1_1 = "tls1.1"
    TLS_1_2 = "tls1.2"
    TLS_1_3 = "tls1.3"
    SSL_2_0 = "ssl2.0"
    SSL_3_0 = "ssl3.0"

class TLSSledArgs(BaseToolArgs):
    """Arguments for TLSSled tool"""
    mode: TLSSledMode = Field(default=TLSSledMode.SCAN, description="Operation mode for tlssled")
    target_host: str = Field(description="Target host or IP address")
    port: int = Field(default=443, description="Target port number")
    protocol: Optional[TLSSledProtocol] = Field(default=None, description="TLS protocol to test")
    timeout: int = Field(default=30, description="Connection timeout in seconds")
    concurrent_connections: int = Field(default=10, description="Number of concurrent connections")
    test_duration: Optional[int] = Field(default=None, description="Test duration in seconds")
    request_count: Optional[int] = Field(default=None, description="Number of requests to make")
    delay: Optional[float] = Field(default=None, description="Delay between requests in seconds")
    user_agent: Optional[str] = Field(default=None, description="Custom User-Agent string")
    certificate_file: Optional[str] = Field(default=None, description="Client certificate file")
    key_file: Optional[str] = Field(default=None, description="Client private key file")
    ca_file: Optional[str] = Field(default=None, description="CA certificate file")
    verify_ssl: bool = Field(default=True, description="Verify SSL certificates")
    insecure: bool = Field(default=False, description="Allow insecure connections")
    verbose: bool = Field(default=False, description="Enable verbose output")
    quiet: bool = Field(default=False, description="Suppress output")
    output_file: Optional[str] = Field(default=None, description="Output file for results")
    log_file: Optional[str] = Field(default=None, description="Log file for detailed output")
    config_file: Optional[str] = Field(default=None, description="TLSSled configuration file")
    timeout_seconds: int = Field(default=300, description="Timeout in seconds for the operation")

def parse_tlssled_output(output_text: str) -> Dict[str, Any]:
    """Parse tlssled command output and extract structured information."""
    result = {
        "connections": 0,
        "successful_connections": 0,
        "failed_connections": 0,
        "response_time": 0.0,
        "throughput": 0.0,
        "ssl_info": {},
        "certificate_info": {},
        "vulnerabilities": [],
        "performance_metrics": {},
        "target_info": {},
        "errors": [],
        "metadata": {}
    }
    
    try:
        lines = output_text.strip().split('\n')
        
        # Extract connection statistics
        conn_match = re.search(r'(\d+)\s+connections?\s+(?:attempted|made)', output_text, re.IGNORECASE)
        if conn_match:
            result["connections"] = int(conn_match.group(1))
        
        success_match = re.search(r'(\d+)\s+successful\s+connections?', output_text, re.IGNORECASE)
        if success_match:
            result["successful_connections"] = int(success_match.group(1))
        
        failed_match = re.search(r'(\d+)\s+failed\s+connections?', output_text, re.IGNORECASE)
        if failed_match:
            result["failed_connections"] = int(failed_match.group(1))
        
        # Extract response time
        resp_match = re.search(r'response\s+time:\s*([\d.]+)\s*ms', output_text, re.IGNORECASE)
        if resp_match:
            result["response_time"] = float(resp_match.group(1))
        
        # Extract throughput
        throughput_match = re.search(r'throughput:\s*([\d.]+)\s*conn/sec', output_text, re.IGNORECASE)
        if throughput_match:
            result["throughput"] = float(throughput_match.group(1))
        
        # Extract SSL/TLS information
        ssl_pattern = r'(TLS|SSL)\s+version:\s*([^\s]+)'
        for line in lines:
            ssl_match = re.search(ssl_pattern, line, re.IGNORECASE)
            if ssl_match:
                result["ssl_info"]["version"] = ssl_match.group(2)
        
        cipher_pattern = r'cipher:\s*([^\s]+)'
        for line in lines:
            cipher_match = re.search(cipher_pattern, line, re.IGNORECASE)
            if cipher_match:
                result["ssl_info"]["cipher"] = cipher_match.group(1)
        
        # Extract certificate information
        cert_pattern = r'certificate:\s*(.+)'
        for line in lines:
            cert_match = re.search(cert_pattern, line, re.IGNORECASE)
            if cert_match:
                result["certificate_info"]["subject"] = cert_match.group(1)
        
        issuer_pattern = r'issuer:\s*(.+)'
        for line in lines:
            issuer_match = re.search(issuer_pattern, line, re.IGNORECASE)
            if issuer_match:
                result["certificate_info"]["issuer"] = issuer_match.group(1)
        
        expiry_pattern = r'expires:\s*(.+)'
        for line in lines:
            expiry_match = re.search(expiry_pattern, line, re.IGNORECASE)
            if expiry_match:
                result["certificate_info"]["expires"] = expiry_match.group(1)
        
        # Extract vulnerabilities
        vuln_pattern = r'(vulnerability|weak|insecure|deprecated):\s*(.+)'
        for line in lines:
            vuln_match = re.search(vuln_pattern, line, re.IGNORECASE)
            if vuln_match:
                result["vulnerabilities"].append({
                    "type": vuln_match.group(1),
                    "description": vuln_match.group(2)
                })
        
        # Extract performance metrics
        perf_pattern = r'(\w+):\s*([\d.]+)'
        for line in lines:
            perf_match = re.search(perf_pattern, line)
            if perf_match:
                metric = perf_match.group(1).lower()
                value = float(perf_match.group(2))
                result["performance_metrics"][metric] = value
        
        # Extract target information
        target_pattern = r'target:\s*([^\s:]+):(\d+)'
        target_match = re.search(target_pattern, output_text, re.IGNORECASE)
        if target_match:
            result["target_info"]["host"] = target_match.group(1)
            result["target_info"]["port"] = int(target_match.group(2))
        
        # Extract error messages
        error_pattern = r'(error|failed|timeout|connection refused|ssl error)'
        for line in lines:
            if re.search(error_pattern, line, re.IGNORECASE):
                result["errors"].append(line.strip())
        
        # Extract metadata
        result["metadata"] = {
            "total_lines": len(lines),
            "has_errors": len(result["errors"]) > 0,
            "has_vulnerabilities": len(result["vulnerabilities"]) > 0,
            "has_ssl_info": len(result["ssl_info"]) > 0,
            "has_certificate_info": len(result["certificate_info"]) > 0
        }
        
    except Exception as e:
        result["errors"].append(f"Error parsing output: {str(e)}")
    
    return result

class TLSSledTool(BaseTool):
    """TLSSled Tool for TLS/SSL stress testing and analysis."""

    args_model = TLSSledArgs

    name: str = "tlssled"
    description: str = "TLS/SSL stress testing and vulnerability assessment tool"
    version: str = "1.0.0"
    def run(self, args: TLSSledArgs) -> ToolResult:
        """Execute tlssled with the specified arguments."""
        try:
            # Construct the tlssled command
            cmd = ["tlssled"]
            
            # Add mode-specific options
            if args.mode == TLSSledMode.SCAN:
                cmd.extend(["--scan"])
            elif args.mode == TLSSledMode.TEST:
                cmd.extend(["--test"])
            elif args.mode == TLSSledMode.BENCHMARK:
                cmd.extend(["--benchmark"])
            elif args.mode == TLSSledMode.STRESS:
                cmd.extend(["--stress"])
            elif args.mode == TLSSledMode.VULNERABILITY:
                cmd.extend(["--vulnerability"])
            elif args.mode == TLSSledMode.CONFIG:
                if args.config_file:
                    cmd.extend(["--config", args.config_file])
            elif args.mode == TLSSledMode.HELP:
                cmd.extend(["--help"])
            elif args.mode == TLSSledMode.VERSION:
                cmd.extend(["--version"])
            
            # Add target host and port
            cmd.extend([args.target_host, str(args.port)])
            
            # Add protocol if specified
            if args.protocol:
                cmd.extend(["--protocol", args.protocol.value])
            
            # Add timeout
            cmd.extend(["--timeout", str(args.timeout)])
            
            # Add concurrent connections
            cmd.extend(["--concurrent", str(args.concurrent_connections)])
            
            # Add test duration if specified
            if args.test_duration:
                cmd.extend(["--duration", str(args.test_duration)])
            
            # Add request count if specified
            if args.request_count:
                cmd.extend(["--requests", str(args.request_count)])
            
            # Add delay if specified
            if args.delay:
                cmd.extend(["--delay", str(args.delay)])
            
            # Add User-Agent if specified
            if args.user_agent:
                cmd.extend(["--user-agent", args.user_agent])
            
            # Add certificate file if specified
            if args.certificate_file:
                cmd.extend(["--cert", args.certificate_file])
            
            # Add key file if specified
            if args.key_file:
                cmd.extend(["--key", args.key_file])
            
            # Add CA file if specified
            if args.ca_file:
                cmd.extend(["--ca", args.ca_file])
            
            # Add SSL verification options
            if not args.verify_ssl:
                cmd.extend(["--no-verify"])
            
            if args.insecure:
                cmd.extend(["--insecure"])
            
            # Add verbose output
            if args.verbose:
                cmd.extend(["--verbose"])
            
            # Add quiet output
            if args.quiet:
                cmd.extend(["--quiet"])
            
            # Add output file if specified
            if args.output_file:
                cmd.extend(["--output", args.output_file])
            
            # Add log file if specified
            if args.log_file:
                cmd.extend(["--log", args.log_file])
            
            # Execute the command
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=args.timeout_seconds
            )
            
            # Parse the output
            parsed_output = parse_tlssled_output(process.stdout)
            
            # Handle errors
            if process.returncode != 0:
                parsed_output["errors"].append(f"Command failed with return code {process.returncode}")
                if process.stderr:
                    parsed_output["errors"].append(f"Stderr: {process.stderr}")
            
            # Prepare artifacts
            artifacts = []
            if args.output_file:
                artifacts.append({
                    "type": "output_file",
                    "path": args.output_file,
                    "description": "TLSSled output results"
                })
            if args.log_file:
                artifacts.append({
                    "type": "log_file",
                    "path": args.log_file,
                    "description": "TLSSled detailed log"
                })
            
            return ToolResult(
                success=process.returncode == 0,
                output=parsed_output,
                artifacts=artifacts,
                metadata={
                    "command": " ".join(cmd),
                    "return_code": process.returncode,
                    "execution_time": None,  # Could be calculated if needed
                    "mode": args.mode.value,
                    "target_host": args.target_host,
                    "port": args.port,
                    "concurrent_connections": args.concurrent_connections
                }
            )
            
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                output={"errors": [f"Command timed out after {args.timeout_seconds} seconds"]},
                artifacts=[],
                metadata={
                    "command": " ".join(cmd) if 'cmd' in locals() else "Unknown",
                    "return_code": -1,
                    "execution_time": args.timeout_seconds,
                    "mode": args.mode.value,
                    "target_host": args.target_host,
                    "port": args.port,
                    "concurrent_connections": args.concurrent_connections
                }
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output={"errors": [f"Error executing tlssled: {str(e)}"]},
                artifacts=[],
                metadata={
                    "command": " ".join(cmd) if 'cmd' in locals() else "Unknown",
                    "return_code": -1,
                    "execution_time": None,
                    "mode": args.mode.value,
                    "target_host": args.target_host,
                    "port": args.port,
                    "concurrent_connections": args.concurrent_connections
                }
            )
