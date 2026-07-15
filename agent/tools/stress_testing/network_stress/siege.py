import subprocess
import re
from typing import Dict, Any, Optional
from enum import Enum
from pydantic import Field

from agent.tools.base_tool import BaseTool
from agent.tools.schemas import BaseToolArgs, ToolResult

class SiegeMode(str, Enum):
    """Siege operation modes"""
    BENCHMARK = "benchmark"
    STRESS = "stress"
    CONCURRENT = "concurrent"
    TIME_BASED = "time_based"
    REQUEST_BASED = "request_based"
    CONFIG = "config"
    HELP = "help"
    VERSION = "version"

class SiegeProtocol(str, Enum):
    """HTTP protocols supported by siege"""
    HTTP = "http"
    HTTPS = "https"

class SiegeArgs(BaseToolArgs):
    """Arguments for Siege tool"""
    mode: SiegeMode = Field(default=SiegeMode.BENCHMARK, description="Operation mode for siege")
    target_url: str = Field(description="Target URL to test")
    protocol: SiegeProtocol = Field(default=SiegeProtocol.HTTP, description="Protocol to use")
    port: Optional[int] = Field(default=None, description="Target port (default: 80 for HTTP, 443 for HTTPS)")
    concurrent_users: int = Field(default=10, description="Number of concurrent users")
    time_duration: Optional[int] = Field(default=None, description="Test duration in seconds")
    request_count: Optional[int] = Field(default=None, description="Number of requests to make")
    delay: Optional[float] = Field(default=None, description="Delay between requests in seconds")
    timeout: int = Field(default=30, description="Connection timeout in seconds")
    user_agent: Optional[str] = Field(default=None, description="Custom User-Agent string")
    referer: Optional[str] = Field(default=None, description="Custom Referer header")
    cookie: Optional[str] = Field(default=None, description="Custom Cookie header")
    content_type: Optional[str] = Field(default=None, description="Content-Type header")
    post_data: Optional[str] = Field(default=None, description="POST data to send")
    method: Optional[str] = Field(default=None, description="HTTP method to use (GET, POST, PUT, DELETE)")
    headers: Optional[str] = Field(default=None, description="Additional HTTP headers")
    follow_redirects: bool = Field(default=True, description="Follow HTTP redirects")
    keep_alive: bool = Field(default=True, description="Use HTTP keep-alive")
    verbose: bool = Field(default=False, description="Enable verbose output")
    quiet: bool = Field(default=False, description="Suppress output")
    log_file: Optional[str] = Field(default=None, description="Log file for detailed output")
    output_file: Optional[str] = Field(default=None, description="Output file for results")
    config_file: Optional[str] = Field(default=None, description="Siege configuration file")
    timeout_seconds: int = Field(default=300, description="Timeout in seconds for the operation")

def parse_siege_output(output_text: str) -> Dict[str, Any]:
    """Parse siege command output and extract structured information."""
    result = {
        "transactions": 0,
        "availability": 0.0,
        "response_time": 0.0,
        "throughput": 0.0,
        "concurrency": 0.0,
        "successful_transactions": 0,
        "failed_transactions": 0,
        "response_codes": {},
        "performance_metrics": {},
        "target_info": {},
        "errors": [],
        "metadata": {}
    }
    
    try:
        lines = output_text.strip().split('\n')
        
        # Extract transaction count
        trans_match = re.search(r'Transactions:\s*(\d+)', output_text, re.IGNORECASE)
        if trans_match:
            result["transactions"] = int(trans_match.group(1))
        
        # Extract availability percentage
        avail_match = re.search(r'Availability:\s*([\d.]+)%', output_text, re.IGNORECASE)
        if avail_match:
            result["availability"] = float(avail_match.group(1))
        
        # Extract response time
        resp_match = re.search(r'Response time:\s*([\d.]+)\s*secs', output_text, re.IGNORECASE)
        if resp_match:
            result["response_time"] = float(resp_match.group(1))
        
        # Extract throughput
        throughput_match = re.search(r'Throughput:\s*([\d.]+)\s*trans/sec', output_text, re.IGNORECASE)
        if throughput_match:
            result["throughput"] = float(throughput_match.group(1))
        
        # Extract concurrency
        conc_match = re.search(r'Concurrency:\s*([\d.]+)', output_text, re.IGNORECASE)
        if conc_match:
            result["concurrency"] = float(conc_match.group(1))
        
        # Extract successful transactions
        success_match = re.search(r'Successful transactions:\s*(\d+)', output_text, re.IGNORECASE)
        if success_match:
            result["successful_transactions"] = int(success_match.group(1))
        
        # Extract failed transactions
        failed_match = re.search(r'Failed transactions:\s*(\d+)', output_text, re.IGNORECASE)
        if failed_match:
            result["failed_transactions"] = int(failed_match.group(1))
        
        # Extract response codes
        code_pattern = r'(\d{3}):\s*(\d+)'
        for line in lines:
            code_match = re.search(code_pattern, line)
            if code_match:
                status_code = code_match.group(1)
                count = int(code_match.group(2))
                result["response_codes"][status_code] = count
        
        # Extract performance metrics
        perf_pattern = r'(\w+):\s*([\d.]+)'
        for line in lines:
            perf_match = re.search(perf_pattern, line)
            if perf_match:
                metric = perf_match.group(1).lower()
                value = float(perf_match.group(2))
                result["performance_metrics"][metric] = value
        
        # Extract target information
        target_pattern = r'target:\s*([^\s]+)'
        target_match = re.search(target_pattern, output_text, re.IGNORECASE)
        if target_match:
            result["target_info"]["url"] = target_match.group(1)
        
        # Extract error messages
        error_pattern = r'(error|failed|timeout|connection refused)'
        for line in lines:
            if re.search(error_pattern, line, re.IGNORECASE):
                result["errors"].append(line.strip())
        
        # Extract metadata
        result["metadata"] = {
            "total_lines": len(lines),
            "has_errors": len(result["errors"]) > 0,
            "has_performance_data": len(result["performance_metrics"]) > 0,
            "has_response_codes": len(result["response_codes"]) > 0
        }
        
    except Exception as e:
        result["errors"].append(f"Error parsing output: {str(e)}")
    
    return result

class SiegeTool(BaseTool):
    """Siege Tool for web server stress testing and benchmarking."""

    args_model = SiegeArgs

    name: str = "siege"
    description: str = "HTTP/HTTPS load testing and benchmarking tool"
    version: str = "1.0.0"
    author: str = "AI Assistant"

    def run(self, args: SiegeArgs) -> ToolResult:
        """Execute siege with the specified arguments."""
        try:
            # Construct the siege command
            cmd = ["siege"]
            
            # Add mode-specific options
            if args.mode == SiegeMode.BENCHMARK:
                cmd.extend(["-b"])  # Benchmark mode
            elif args.mode == SiegeMode.STRESS:
                cmd.extend(["-t", str(args.time_duration or 60)])  # Time-based stress
            elif args.mode == SiegeMode.CONCURRENT:
                cmd.extend(["-c", str(args.concurrent_users)])  # Concurrent users
            elif args.mode == SiegeMode.TIME_BASED:
                cmd.extend(["-t", str(args.time_duration or 60)])  # Time-based
            elif args.mode == SiegeMode.REQUEST_BASED:
                cmd.extend(["-r", str(args.request_count or 100)])  # Request-based
            elif args.mode == SiegeMode.CONFIG:
                if args.config_file:
                    cmd.extend(["-C", args.config_file])
            elif args.mode == SiegeMode.HELP:
                cmd.extend(["--help"])
            elif args.mode == SiegeMode.VERSION:
                cmd.extend(["--version"])
            
            # Add concurrent users if not already specified
            if args.mode not in [SiegeMode.CONCURRENT, SiegeMode.HELP, SiegeMode.VERSION]:
                cmd.extend(["-c", str(args.concurrent_users)])
            
            # Add time duration if specified
            if args.time_duration and args.mode not in [SiegeMode.TIME_BASED, SiegeMode.HELP, SiegeMode.VERSION]:
                cmd.extend(["-t", str(args.time_duration)])
            
            # Add request count if specified
            if args.request_count and args.mode not in [SiegeMode.REQUEST_BASED, SiegeMode.HELP, SiegeMode.VERSION]:
                cmd.extend(["-r", str(args.request_count)])
            
            # Add delay if specified
            if args.delay:
                cmd.extend(["-d", str(args.delay)])
            
            # Add timeout
            cmd.extend(["-T", str(args.timeout)])
            
            # Add User-Agent if specified
            if args.user_agent:
                cmd.extend(["-A", args.user_agent])
            
            # Add Referer if specified
            if args.referer:
                cmd.extend(["-H", f"Referer: {args.referer}"])
            
            # Add Cookie if specified
            if args.cookie:
                cmd.extend(["-H", f"Cookie: {args.cookie}"])
            
            # Add Content-Type if specified
            if args.content_type:
                cmd.extend(["-H", f"Content-Type: {args.content_type}"])
            
            # Add POST data if specified
            if args.post_data:
                cmd.extend(["-p", args.post_data])
            
            # Add HTTP method if specified
            if args.method:
                cmd.extend(["-m", args.method.upper()])
            
            # Add additional headers if specified
            if args.headers:
                for header in args.headers.split(','):
                    cmd.extend(["-H", header.strip()])
            
            # Add follow redirects option
            if not args.follow_redirects:
                cmd.extend(["-R"])
            
            # Add keep-alive option
            if not args.keep_alive:
                cmd.extend(["-K"])
            
            # Add verbose output
            if args.verbose:
                cmd.extend(["-v"])
            
            # Add quiet output
            if args.quiet:
                cmd.extend(["-q"])
            
            # Add log file if specified
            if args.log_file:
                cmd.extend(["-l", args.log_file])
            
            # Add output file if specified
            if args.output_file:
                cmd.extend(["-o", args.output_file])
            
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
            parsed_output = parse_siege_output(process.stdout)
            
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
                    "description": "Siege output results"
                })
            if args.log_file:
                artifacts.append({
                    "type": "log_file",
                    "path": args.log_file,
                    "description": "Siege detailed log"
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
                    "target_url": args.target_url,
                    "protocol": args.protocol.value,
                    "concurrent_users": args.concurrent_users
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
                    "target_url": args.target_url,
                    "protocol": args.protocol.value,
                    "concurrent_users": args.concurrent_users
                }
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output={"errors": [f"Error executing siege: {str(e)}"]},
                artifacts=[],
                metadata={
                    "command": " ".join(cmd) if 'cmd' in locals() else "Unknown",
                    "return_code": -1,
                    "execution_time": None,
                    "mode": args.mode.value,
                    "target_url": args.target_url,
                    "protocol": args.protocol.value,
                    "concurrent_users": args.concurrent_users
                }
            )
