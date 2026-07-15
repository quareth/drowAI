import subprocess
import re
from typing import Dict, Any, Optional
from enum import Enum
from pydantic import Field

from agent.tools.base_tool import BaseTool
from agent.tools.schemas import BaseToolArgs, ToolResult

class Hping3Mode(str, Enum):
    """Hping3 operation modes"""
    ICMP = "icmp"
    TCP = "tcp"
    UDP = "udp"
    RAW = "raw"
    SYN = "syn"
    ACK = "ack"
    FIN = "fin"
    RST = "rst"
    PSH = "psh"
    URG = "urg"
    XMAS = "xmas"
    NULL = "null"
    MAIMON = "maimon"

class Hping3Protocol(str, Enum):
    """Protocol types for hping3"""
    ICMP = "icmp"
    TCP = "tcp"
    UDP = "udp"

class Hping3Args(BaseToolArgs):
    """Arguments for Hping3 tool"""
    mode: Hping3Mode = Field(default=Hping3Mode.TCP, description="Operation mode for hping3")
    target: str = Field(description="Target host or IP address")
    protocol: Hping3Protocol = Field(default=Hping3Protocol.TCP, description="Protocol to use")
    port: Optional[int] = Field(default=None, description="Target port number")
    source_port: Optional[int] = Field(default=None, description="Source port number")
    count: Optional[int] = Field(default=None, description="Number of packets to send")
    interval: Optional[float] = Field(default=None, description="Interval between packets in seconds")
    timeout: Optional[int] = Field(default=None, description="Timeout in seconds")
    ttl: Optional[int] = Field(default=None, description="Time to live value")
    tos: Optional[int] = Field(default=None, description="Type of service value")
    id: Optional[int] = Field(default=None, description="IP ID value")
    window: Optional[int] = Field(default=None, description="TCP window size")
    flags: Optional[str] = Field(default=None, description="TCP flags (S, A, F, R, P, U)")
    data_size: Optional[int] = Field(default=None, description="Data size in bytes")
    data: Optional[str] = Field(default=None, description="Data to send")
    source_ip: Optional[str] = Field(default=None, description="Source IP address")
    interface: Optional[str] = Field(default=None, description="Network interface to use")
    fast: bool = Field(default=False, description="Fast mode")
    flood: bool = Field(default=False, description="Flood mode (send as fast as possible)")
    verbose: bool = Field(default=False, description="Enable verbose output")
    quiet: bool = Field(default=False, description="Suppress output")
    numeric: bool = Field(default=False, description="Numeric output")
    output_file: Optional[str] = Field(default=None, description="Output file for results")
    timeout_seconds: int = Field(default=300, description="Timeout in seconds for the operation")

def parse_hping3_output(output_text: str) -> Dict[str, Any]:
    """Parse hping3 command output and extract structured information."""
    result = {
        "packets": {
            "sent": 0,
            "received": 0,
            "lost": 0,
            "loss_percentage": 0.0
        },
        "timing": {
            "min_rtt": 0.0,
            "avg_rtt": 0.0,
            "max_rtt": 0.0,
            "mdev": 0.0
        },
        "target_info": {
            "host": None,
            "port": None,
            "protocol": None
        },
        "responses": [],
        "statistics": {
            "total_time": 0.0,
            "packets_per_second": 0.0
        }
    }
    
    try:
        lines = output_text.split('\n')
        rtt_values = []
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
                
            # Parse target information
            if "HPING" in line and "(" in line and ")" in line:
                # HPING example.com (eth0 192.168.1.1): S set, 40 headers + 0 data bytes
                parts = line.split("(")
                if len(parts) > 1:
                    target_part = parts[0].split("HPING")[1].strip()
                    result["target_info"]["host"] = target_part
                    
                    # Extract protocol and port from the rest of the line
                    if "S set" in line:
                        result["target_info"]["protocol"] = "TCP"
                    elif "UDP" in line:
                        result["target_info"]["protocol"] = "UDP"
                    elif "ICMP" in line:
                        result["target_info"]["protocol"] = "ICMP"
            
            # Parse response lines
            elif "len=" in line and "ttl=" in line and "time=" in line:
                # Response line: 64 bytes from 192.168.1.1: icmp_seq=1 ttl=64 time=0.123 ms
                response = {}
                
                # Extract length
                len_match = re.search(r"len=(\d+)", line)
                if len_match:
                    response["length"] = int(len_match.group(1))
                
                # Extract TTL
                ttl_match = re.search(r"ttl=(\d+)", line)
                if ttl_match:
                    response["ttl"] = int(ttl_match.group(1))
                
                # Extract time
                time_match = re.search(r"time=([\d.]+)", line)
                if time_match:
                    response["time"] = float(time_match.group(1))
                    rtt_values.append(response["time"])
                
                # Extract sequence number
                seq_match = re.search(r"icmp_seq=(\d+)", line)
                if seq_match:
                    response["sequence"] = int(seq_match.group(1))
                
                # Extract port if present
                port_match = re.search(r"(\d+\.\d+\.\d+\.\d+):(\d+)", line)
                if port_match:
                    response["source_ip"] = port_match.group(1)
                    response["source_port"] = int(port_match.group(2))
                
                result["responses"].append(response)
                result["packets"]["received"] += 1
            
            # Parse statistics
            elif "packets transmitted" in line and "packets received" in line:
                # Statistics line: 10 packets transmitted, 10 packets received, 0% packet loss
                stats_match = re.search(r"(\d+) packets transmitted, (\d+) packets received, ([\d.]+)% packet loss", line)
                if stats_match:
                    result["packets"]["sent"] = int(stats_match.group(1))
                    result["packets"]["received"] = int(stats_match.group(2))
                    result["packets"]["loss_percentage"] = float(stats_match.group(3))
                    result["packets"]["lost"] = result["packets"]["sent"] - result["packets"]["received"]
            
            # Parse timing statistics
            elif "round-trip min/avg/max/mdev" in line:
                # Timing line: round-trip min/avg/max/mdev = 0.123/0.456/0.789/0.123 ms
                timing_match = re.search(r"round-trip min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)", line)
                if timing_match:
                    result["timing"]["min_rtt"] = float(timing_match.group(1))
                    result["timing"]["avg_rtt"] = float(timing_match.group(2))
                    result["timing"]["max_rtt"] = float(timing_match.group(3))
                    result["timing"]["mdev"] = float(timing_match.group(4))
            
            # Parse total time
            elif "time" in line and "ms" in line and "packets" in line:
                # Time line: 9.123 ms total, 1.234 packets per second
                time_match = re.search(r"([\d.]+) ms total, ([\d.]+) packets per second", line)
                if time_match:
                    result["statistics"]["total_time"] = float(time_match.group(1))
                    result["statistics"]["packets_per_second"] = float(time_match.group(2))
    
    except Exception as e:
        result["error"] = f"Error parsing hping3 output: {str(e)}"
    
    return result

class Hping3Tool(BaseTool):
    """Hping3 Tool for network testing and stress testing."""

    args_model = Hping3Args

    name: str = "hping3"
    description: str = "Network testing tool for TCP/IP stack analysis and stress testing"
    version: str = "1.0.0"
    author: str = "AI Assistant"

    def run(self, args: Hping3Args) -> ToolResult:
        """Execute hping3 with the specified arguments."""
        try:
            # Build the command
            cmd = ["hping3"]
            
            # Add mode-specific options
            if args.mode == Hping3Mode.ICMP:
                cmd.append("-1")
            elif args.mode == Hping3Mode.UDP:
                cmd.append("-2")
            elif args.mode == Hping3Mode.RAW:
                cmd.append("-0")
            elif args.mode == Hping3Mode.SYN:
                cmd.append("-S")
            elif args.mode == Hping3Mode.ACK:
                cmd.append("-A")
            elif args.mode == Hping3Mode.FIN:
                cmd.append("-F")
            elif args.mode == Hping3Mode.RST:
                cmd.append("-R")
            elif args.mode == Hping3Mode.PSH:
                cmd.append("-P")
            elif args.mode == Hping3Mode.URG:
                cmd.append("-U")
            elif args.mode == Hping3Mode.XMAS:
                cmd.append("-X")
            elif args.mode == Hping3Mode.NULL:
                cmd.append("-Y")
            elif args.mode == Hping3Mode.MAIMON:
                cmd.append("-M")
            
            # Add general options
            if args.count:
                cmd.extend(["-c", str(args.count)])
            if args.interval:
                cmd.extend(["-i", str(args.interval)])
            if args.timeout:
                cmd.extend(["-t", str(args.timeout)])
            if args.ttl:
                cmd.extend(["--ttl", str(args.ttl)])
            if args.tos:
                cmd.extend(["--tos", str(args.tos)])
            if args.id:
                cmd.extend(["--id", str(args.id)])
            if args.window:
                cmd.extend(["--win", str(args.window)])
            if args.flags:
                cmd.extend(["--flags", args.flags])
            if args.data_size:
                cmd.extend(["-d", str(args.data_size)])
            if args.data:
                cmd.extend(["--data", args.data])
            if args.source_ip:
                cmd.extend(["-a", args.source_ip])
            if args.source_port:
                cmd.extend(["-p", str(args.source_port)])
            if args.interface:
                cmd.extend(["-I", args.interface])
            
            # Add target port
            if args.port:
                cmd.extend(["-p", str(args.port)])
            
            # Add other options
            if args.fast:
                cmd.append("--fast")
            if args.flood:
                cmd.append("--flood")
            if args.verbose:
                cmd.append("-V")
            if args.quiet:
                cmd.append("-q")
            if args.numeric:
                cmd.append("-n")
            
            # Add target
            cmd.append(args.target)
            
            # Execute the command
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=args.timeout_seconds
            )
            
            # Parse the output
            parsed_output = parse_hping3_output(process.stdout)
            
            # Handle errors
            if process.returncode != 0:
                error_msg = process.stderr.strip() if process.stderr else "Unknown error"
                return ToolResult(
                    success=False,
                    output=f"Hping3 failed with return code {process.returncode}: {error_msg}",
                    metadata={
                        "command": " ".join(cmd),
                        "return_code": process.returncode,
                        "error": error_msg
                    }
                )
            
            # Prepare artifacts
            artifacts = []
            if args.output_file:
                try:
                    with open(args.output_file, 'w') as f:
                        f.write(process.stdout)
                    artifacts.append(f"Output saved to: {args.output_file}")
                except Exception as e:
                    artifacts.append(f"Failed to save output file: {str(e)}")
            
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
                output=f"Hping3 command timed out after {args.timeout_seconds} seconds",
                metadata={
                    "command": " ".join(cmd) if 'cmd' in locals() else "Unknown",
                    "timeout": args.timeout_seconds,
                    "error": "Timeout expired"
                }
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output=f"Error executing hping3: {str(e)}",
                metadata={
                    "command": " ".join(cmd) if 'cmd' in locals() else "Unknown",
                    "error": str(e)
                }
            )
