"""Provide slowhttptest arguments and command execution."""

import subprocess
from typing import Dict, Any, Optional
from enum import Enum
from pydantic import Field

from agent.tools.base_tool import BaseTool
from agent.tools.schemas import BaseToolArgs, ToolResult

class SlowHTTPTestMode(str, Enum):
    """SlowHTTPTest attack modes"""
    SLOWLORIS = "slowloris"
    SLOW_READ = "slowread"
    SLOW_POST = "slowpost"
    APACHE_KILLER = "apache-killer"
    RANGE_ATTACK = "range-attack"

class SlowHTTPTestProtocol(str, Enum):
    """HTTP protocols supported"""
    HTTP = "http"
    HTTPS = "https"

class SlowHTTPTestArgs(BaseToolArgs):
    """Arguments for SlowHTTPTest tool"""
    mode: SlowHTTPTestMode = Field(default=SlowHTTPTestMode.SLOWLORIS, description="Attack mode to use")
    target_url: str = Field(description="Target URL to test")
    protocol: SlowHTTPTestProtocol = Field(default=SlowHTTPTestProtocol.HTTP, description="Protocol to use")
    port: Optional[int] = Field(default=None, description="Target port (default: 80 for HTTP, 443 for HTTPS)")
    connections: int = Field(default=150, description="Number of connections to establish")
    interval: int = Field(default=10, description="Interval between requests in seconds")
    timeout: int = Field(default=5, description="Connection timeout in seconds")
    test_duration: int = Field(default=60, description="Test duration in seconds")
    request_rate: int = Field(default=10, description="Number of requests per second")
    content_length: Optional[int] = Field(default=None, description="Content length for POST requests")
    range_start: Optional[int] = Field(default=None, description="Start of range for range attack")
    range_end: Optional[int] = Field(default=None, description="End of range for range attack")
    user_agent: Optional[str] = Field(default=None, description="Custom User-Agent string")
    referer: Optional[str] = Field(default=None, description="Custom Referer header")
    cookie: Optional[str] = Field(default=None, description="Custom Cookie header")
    follow_redirects: bool = Field(default=False, description="Follow HTTP redirects")
    verbose: bool = Field(default=False, description="Enable verbose output")
    quiet: bool = Field(default=False, description="Suppress output")
    output_file: Optional[str] = Field(default=None, description="Output file for results")
    log_file: Optional[str] = Field(default=None, description="Log file for detailed output")
    timeout_seconds: int = Field(default=300, description="Timeout in seconds for the operation")

def parse_slowhttptest_output(output_text: str) -> Dict[str, Any]:
    """Parse slowhttptest command output and extract structured information."""
    result = {
        "attack_stats": {
            "connections_established": 0,
            "requests_sent": 0,
            "responses_received": 0,
            "errors": 0,
            "timeouts": 0
        },
        "target_info": {
            "url": None,
            "protocol": None,
            "port": None,
            "status_code": None
        },
        "performance_metrics": {
            "requests_per_second": 0.0,
            "average_response_time": 0.0,
            "total_duration": 0,
            "bytes_transferred": 0
        },
        "attack_details": {
            "mode": None,
            "connections": 0,
            "interval": 0,
            "duration": 0
        },
        "logs": []
    }
    
    try:
        lines = output_text.split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
                
            # Parse target information
            if "Target:" in line:
                result["target_info"]["url"] = line.split("Target:")[1].strip()
            elif "Protocol:" in line:
                result["target_info"]["protocol"] = line.split("Protocol:")[1].strip()
            elif "Port:" in line:
                result["target_info"]["port"] = line.split("Port:")[1].strip()
            
            # Parse attack mode
            elif "Attack mode:" in line:
                result["attack_details"]["mode"] = line.split("Attack mode:")[1].strip()
            elif "Connections:" in line:
                result["attack_details"]["connections"] = int(line.split("Connections:")[1].strip())
            elif "Interval:" in line:
                result["attack_details"]["interval"] = int(line.split("Interval:")[1].strip())
            elif "Duration:" in line:
                result["attack_details"]["duration"] = int(line.split("Duration:")[1].strip())
            
            # Parse statistics
            elif "Connections established:" in line:
                result["attack_stats"]["connections_established"] = int(line.split(":")[1].strip())
            elif "Requests sent:" in line:
                result["attack_stats"]["requests_sent"] = int(line.split(":")[1].strip())
            elif "Responses received:" in line:
                result["attack_stats"]["responses_received"] = int(line.split(":")[1].strip())
            elif "Errors:" in line:
                result["attack_stats"]["errors"] = int(line.split(":")[1].strip())
            elif "Timeouts:" in line:
                result["attack_stats"]["timeouts"] = int(line.split(":")[1].strip())
            
            # Parse performance metrics
            elif "Requests per second:" in line:
                result["performance_metrics"]["requests_per_second"] = float(line.split(":")[1].strip())
            elif "Average response time:" in line:
                result["performance_metrics"]["average_response_time"] = float(line.split(":")[1].strip())
            elif "Total duration:" in line:
                result["performance_metrics"]["total_duration"] = int(line.split(":")[1].strip())
            elif "Bytes transferred:" in line:
                result["performance_metrics"]["bytes_transferred"] = int(line.split(":")[1].strip())
            
            # Parse status codes
            elif "Status code:" in line:
                result["target_info"]["status_code"] = line.split("Status code:")[1].strip()
            
            # Parse log entries
            elif line.startswith("[") and "]" in line:
                # Log entry: [2024-01-01 12:00:00] INFO: Connection established
                result["logs"].append(line)
    
    except Exception as e:
        result["error"] = f"Error parsing slowhttptest output: {str(e)}"
    
    return result

class SlowHTTPTestTool(BaseTool):
    """SlowHTTPTest Tool for testing slow HTTP attack vulnerabilities."""

    args_model = SlowHTTPTestArgs

    name: str = "slowhttptest"
    description: str = "Application layer DoS attack simulator for testing slow HTTP attack vulnerabilities"
    version: str = "1.0.0"
    def run(self, args: SlowHTTPTestArgs) -> ToolResult:
        """Execute slowhttptest with the specified arguments."""
        try:
            # Build the command
            cmd = ["slowhttptest"]
            
            # Add mode-specific options
            if args.mode == SlowHTTPTestMode.SLOWLORIS:
                cmd.append("-H")
            elif args.mode == SlowHTTPTestMode.SLOW_READ:
                cmd.append("-R")
            elif args.mode == SlowHTTPTestMode.SLOW_POST:
                cmd.append("-B")
            elif args.mode == SlowHTTPTestMode.APACHE_KILLER:
                cmd.append("-A")
            elif args.mode == SlowHTTPTestMode.RANGE_ATTACK:
                cmd.append("-X")
            
            # Add general options
            cmd.extend(["-c", str(args.connections)])
            cmd.extend(["-i", str(args.interval)])
            cmd.extend(["-t", str(args.timeout)])
            cmd.extend(["-l", str(args.test_duration)])
            cmd.extend(["-r", str(args.request_rate)])
            
            # Add protocol and port
            if args.protocol == SlowHTTPTestProtocol.HTTPS:
                cmd.append("-g")
                if not args.port:
                    args.port = 443
            else:
                if not args.port:
                    args.port = 80
            
            if args.port:
                cmd.extend(["-p", str(args.port)])
            
            # Add content length for POST mode
            if args.mode == SlowHTTPTestMode.SLOW_POST and args.content_length:
                cmd.extend(["-k", str(args.content_length)])
            
            # Add range for range attack
            if args.mode == SlowHTTPTestMode.RANGE_ATTACK:
                if args.range_start:
                    cmd.extend(["-m", str(args.range_start)])
                if args.range_end:
                    cmd.extend(["-n", str(args.range_end)])
            
            # Add custom headers
            if args.user_agent:
                cmd.extend(["-u", args.user_agent])
            if args.referer:
                cmd.extend(["-e", args.referer])
            if args.cookie:
                cmd.extend(["-C", args.cookie])
            
            # Add other options
            if args.follow_redirects:
                cmd.append("-f")
            if args.verbose:
                cmd.append("-v")
            if args.quiet:
                cmd.append("-q")
            
            # Add output options
            if args.output_file:
                cmd.extend(["-o", args.output_file])
            if args.log_file:
                cmd.extend(["-l", args.log_file])
            
            # Add target URL
            cmd.append(args.target_url)
            
            # Execute the command
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=args.timeout_seconds
            )
            
            # Parse the output
            parsed_output = parse_slowhttptest_output(process.stdout)
            
            # Handle errors
            if process.returncode != 0:
                error_msg = process.stderr.strip() if process.stderr else "Unknown error"
                return ToolResult(
                    success=False,
                    output=f"SlowHTTPTest failed with return code {process.returncode}: {error_msg}",
                    metadata={
                        "command": " ".join(cmd),
                        "return_code": process.returncode,
                        "error": error_msg
                    }
                )
            
            # Prepare artifacts
            artifacts = []
            if args.output_file:
                artifacts.append(f"Results saved to: {args.output_file}")
            if args.log_file:
                artifacts.append(f"Log saved to: {args.log_file}")
            
            return ToolResult(
                success=True,
                output=process.stdout,
                metadata={
                    "command": " ".join(cmd),
                    "return_code": process.returncode,
                    "parsed_data": parsed_output,
                    "artifacts": artifacts
                }
            )
            
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                output=f"SlowHTTPTest command timed out after {args.timeout_seconds} seconds",
                metadata={
                    "command": " ".join(cmd) if 'cmd' in locals() else "Unknown",
                    "timeout": args.timeout_seconds,
                    "error": "Timeout expired"
                }
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output=f"Error executing slowhttptest: {str(e)}",
                metadata={
                    "command": " ".join(cmd) if 'cmd' in locals() else "Unknown",
                    "error": str(e)
                }
            )
