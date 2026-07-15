import subprocess
import re
from typing import Dict, Any, Optional
from enum import Enum
from pydantic import Field

from agent.tools.base_tool import BaseTool
from agent.tools.schemas import BaseToolArgs, ToolResult

class HTTPPrintMode(str, Enum):
    """HTTPPrint operation modes"""
    SCAN = "scan"
    FINGERPRINT = "fingerprint"
    STRESS = "stress"
    BENCHMARK = "benchmark"
    DETECT = "detect"
    COMPARE = "compare"
    HELP = "help"
    VERSION = "version"

class HTTPPrintProtocol(str, Enum):
    """HTTP protocols supported"""
    HTTP = "http"
    HTTPS = "https"

class HTTPPrintArgs(BaseToolArgs):
    """Arguments for HTTPPrint tool"""
    mode: HTTPPrintMode = Field(default=HTTPPrintMode.SCAN, description="Operation mode for httprint")
    target_host: str = Field(description="Target host or IP address")
    port: int = Field(default=80, description="Target port number")
    protocol: HTTPPrintProtocol = Field(default=HTTPPrintProtocol.HTTP, description="Protocol to use")
    timeout: int = Field(default=30, description="Connection timeout in seconds")
    concurrent_connections: int = Field(default=10, description="Number of concurrent connections")
    test_duration: Optional[int] = Field(default=None, description="Test duration in seconds")
    request_count: Optional[int] = Field(default=None, description="Number of requests to make")
    delay: Optional[float] = Field(default=None, description="Delay between requests in seconds")
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
    output_file: Optional[str] = Field(default=None, description="Output file for results")
    log_file: Optional[str] = Field(default=None, description="Log file for detailed output")
    signature_file: Optional[str] = Field(default=None, description="Signature file for fingerprinting")
    config_file: Optional[str] = Field(default=None, description="HTTPPrint configuration file")
    timeout_seconds: int = Field(default=300, description="Timeout in seconds for the operation")

def parse_httprint_output(output_text: str) -> Dict[str, Any]:
    """Parse httprint command output and extract structured information."""
    result = {
        "connections": 0,
        "successful_connections": 0,
        "failed_connections": 0,
        "response_time": 0.0,
        "throughput": 0.0,
        "server_signature": "",
        "detected_servers": [],
        "fingerprint_results": {},
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
        throughput_match = re.search(r'throughput:\s*([\d.]+)\s*req/sec', output_text, re.IGNORECASE)
        if throughput_match:
            result["throughput"] = float(throughput_match.group(1))
        
        # Extract server signature
        sig_pattern = r'server\s+signature:\s*(.+)'
        for line in lines:
            sig_match = re.search(sig_pattern, line, re.IGNORECASE)
            if sig_match:
                result["server_signature"] = sig_match.group(1).strip()
        
        # Extract detected servers
        server_pattern = r'detected\s+server:\s*(.+)'
        for line in lines:
            server_match = re.search(server_pattern, line, re.IGNORECASE)
            if server_match:
                result["detected_servers"].append(server_match.group(1).strip())
        
        # Extract fingerprint results
        fp_pattern = r'fingerprint:\s*(.+)'
        for line in lines:
            fp_match = re.search(fp_pattern, line, re.IGNORECASE)
            if fp_match:
                parts = fp_match.group(1).split(':')
                if len(parts) >= 2:
                    key = parts[0].strip()
                    value = ':'.join(parts[1:]).strip()
                    result["fingerprint_results"][key] = value
        
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
        error_pattern = r'(error|failed|timeout|connection refused|http error)'
        for line in lines:
            if re.search(error_pattern, line, re.IGNORECASE):
                result["errors"].append(line.strip())
        
        # Extract metadata
        result["metadata"] = {
            "total_lines": len(lines),
            "has_errors": len(result["errors"]) > 0,
            "has_detected_servers": len(result["detected_servers"]) > 0,
            "has_fingerprint_results": len(result["fingerprint_results"]) > 0,
            "has_performance_data": len(result["performance_metrics"]) > 0
        }
        
    except Exception as e:
        result["errors"].append(f"Error parsing output: {str(e)}")
    
    return result

class HTTPPrintTool(BaseTool):
    """HTTPPrint Tool for web server fingerprinting and stress testing."""

    args_model = HTTPPrintArgs

    name: str = "httprint"
    description: str = "Web server fingerprinting and stress testing tool"
    version: str = "1.0.0"
    author: str = "AI Assistant"

    def run(self, args: HTTPPrintArgs) -> ToolResult:
        """Execute httprint with the specified arguments."""
        try:
            # Construct the httprint command
            cmd = ["httprint"]
            
            # Add mode-specific options
            if args.mode == HTTPPrintMode.SCAN:
                cmd.extend(["--scan"])
            elif args.mode == HTTPPrintMode.FINGERPRINT:
                cmd.extend(["--fingerprint"])
            elif args.mode == HTTPPrintMode.STRESS:
                cmd.extend(["--stress"])
            elif args.mode == HTTPPrintMode.BENCHMARK:
                cmd.extend(["--benchmark"])
            elif args.mode == HTTPPrintMode.DETECT:
                cmd.extend(["--detect"])
            elif args.mode == HTTPPrintMode.COMPARE:
                cmd.extend(["--compare"])
            elif args.mode == HTTPPrintMode.HELP:
                cmd.extend(["--help"])
            elif args.mode == HTTPPrintMode.VERSION:
                cmd.extend(["--version"])
            
            # Add target host and port
            cmd.extend(["-h", args.target_host, "-p", str(args.port)])
            
            # Add protocol
            if args.protocol == HTTPPrintProtocol.HTTPS:
                cmd.extend(["--ssl"])
            
            # Add timeout
            cmd.extend(["-t", str(args.timeout)])
            
            # Add concurrent connections
            cmd.extend(["-c", str(args.concurrent_connections)])
            
            # Add test duration if specified
            if args.test_duration:
                cmd.extend(["-d", str(args.test_duration)])
            
            # Add request count if specified
            if args.request_count:
                cmd.extend(["-r", str(args.request_count)])
            
            # Add delay if specified
            if args.delay:
                cmd.extend(["--delay", str(args.delay)])
            
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
                cmd.extend(["-P", args.post_data])
            
            # Add HTTP method if specified
            if args.method:
                cmd.extend(["-m", args.method.upper()])
            
            # Add additional headers if specified
            if args.headers:
                for header in args.headers.split(','):
                    cmd.extend(["-H", header.strip()])
            
            # Add follow redirects option
            if not args.follow_redirects:
                cmd.extend(["--no-redirect"])
            
            # Add keep-alive option
            if not args.keep_alive:
                cmd.extend(["--no-keepalive"])
            
            # Add verbose output
            if args.verbose:
                cmd.extend(["-v"])
            
            # Add quiet output
            if args.quiet:
                cmd.extend(["-q"])
            
            # Add output file if specified
            if args.output_file:
                cmd.extend(["-o", args.output_file])
            
            # Add log file if specified
            if args.log_file:
                cmd.extend(["-l", args.log_file])
            
            # Add signature file if specified
            if args.signature_file:
                cmd.extend(["-s", args.signature_file])
            
            # Add config file if specified
            if args.config_file:
                cmd.extend(["-C", args.config_file])
            
            # Execute the command
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=args.timeout_seconds
            )
            
            # Parse the output
            parsed_output = parse_httprint_output(process.stdout)
            
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
                    "description": "HTTPPrint output results"
                })
            if args.log_file:
                artifacts.append({
                    "type": "log_file",
                    "path": args.log_file,
                    "description": "HTTPPrint detailed log"
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
                    "protocol": args.protocol.value,
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
                    "protocol": args.protocol.value,
                    "concurrent_connections": args.concurrent_connections
                }
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output={"errors": [f"Error executing httprint: {str(e)}"]},
                artifacts=[],
                metadata={
                    "command": " ".join(cmd) if 'cmd' in locals() else "Unknown",
                    "return_code": -1,
                    "execution_time": None,
                    "mode": args.mode.value,
                    "target_host": args.target_host,
                    "port": args.port,
                    "protocol": args.protocol.value,
                    "concurrent_connections": args.concurrent_connections
                }
            )
