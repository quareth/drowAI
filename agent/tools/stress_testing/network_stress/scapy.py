"""Provide Scapy network-testing arguments and command execution."""

import subprocess
import re
from typing import Dict, Any, Optional
from enum import Enum
from pydantic import Field

from agent.tools.base_tool import BaseTool
from agent.tools.schemas import BaseToolArgs, ToolResult

class ScapyMode(str, Enum):
    """Scapy operation modes"""
    PING = "ping"
    TRACEROUTE = "traceroute"
    SCAN = "scan"
    SNIFF = "sniff"
    SEND = "send"
    FLOOD = "flood"
    SYN_SCAN = "syn_scan"
    ACK_SCAN = "ack_scan"
    XMAS_SCAN = "xmas_scan"
    NULL_SCAN = "null_scan"
    FIN_SCAN = "fin_scan"
    UDP_SCAN = "udp_scan"
    ARP_SCAN = "arp_scan"
    DNS_QUERY = "dns_query"
    DHCP_DISCOVER = "dhcp_discover"
    CUSTOM = "custom"

class ScapyProtocol(str, Enum):
    """Protocol types for scapy"""
    TCP = "tcp"
    UDP = "udp"
    ICMP = "icmp"
    ARP = "arp"
    DNS = "dns"
    DHCP = "dhcp"
    HTTP = "http"
    HTTPS = "https"
    SMTP = "smtp"
    FTP = "ftp"

class ScapyArgs(BaseToolArgs):
    """Arguments for Scapy tool"""
    mode: ScapyMode = Field(default=ScapyMode.PING, description="Operation mode for scapy")
    target: str = Field(description="Target host or IP address")
    protocol: ScapyProtocol = Field(default=ScapyProtocol.ICMP, description="Protocol to use")
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
    filter: Optional[str] = Field(default=None, description="BPF filter for sniffing")
    duration: Optional[int] = Field(default=None, description="Duration for sniffing in seconds")
    packet_count: Optional[int] = Field(default=None, description="Number of packets to capture")
    verbose: bool = Field(default=False, description="Enable verbose output")
    quiet: bool = Field(default=False, description="Suppress output")
    numeric: bool = Field(default=False, description="Numeric output")
    output_file: Optional[str] = Field(default=None, description="Output file for results")
    script_file: Optional[str] = Field(default=None, description="Scapy script file to execute")
    commands: Optional[str] = Field(default=None, description="Scapy commands to execute")
    timeout_seconds: int = Field(default=300, description="Timeout in seconds for the operation")

def parse_scapy_output(output_text: str) -> Dict[str, Any]:
    """Parse scapy command output and extract structured information."""
    result = {
        "packets_sent": 0,
        "packets_received": 0,
        "responses": [],
        "statistics": {},
        "target_info": {},
        "scan_results": [],
        "sniff_results": [],
        "errors": [],
        "metadata": {}
    }
    
    try:
        lines = output_text.strip().split('\n')
        
        # Extract packet statistics
        sent_match = re.search(r'(\d+)\s+packets?\s+sent', output_text, re.IGNORECASE)
        if sent_match:
            result["packets_sent"] = int(sent_match.group(1))
        
        received_match = re.search(r'(\d+)\s+packets?\s+received', output_text, re.IGNORECASE)
        if received_match:
            result["packets_received"] = int(received_match.group(1))
        
        # Extract ping results
        ping_pattern = r'(\d+)\s+bytes\s+from\s+([^\s]+):\s+icmp_seq=(\d+)\s+ttl=(\d+)\s+time=([\d.]+)\s+ms'
        for line in lines:
            ping_match = re.search(ping_pattern, line)
            if ping_match:
                result["responses"].append({
                    "type": "ping_response",
                    "bytes": int(ping_match.group(1)),
                    "source": ping_match.group(2),
                    "sequence": int(ping_match.group(3)),
                    "ttl": int(ping_match.group(4)),
                    "time_ms": float(ping_match.group(5))
                })
        
        # Extract traceroute results
        trace_pattern = r'(\d+)\s+([^\s]+)\s+([\d.]+)\s+ms'
        for line in lines:
            trace_match = re.search(trace_pattern, line)
            if trace_match:
                result["responses"].append({
                    "type": "traceroute_hop",
                    "hop": int(trace_match.group(1)),
                    "host": trace_match.group(2),
                    "time_ms": float(trace_match.group(3))
                })
        
        # Extract scan results
        scan_pattern = r'([^\s]+):(\d+)\s+([^\s]+)'
        for line in lines:
            scan_match = re.search(scan_pattern, line)
            if scan_match:
                result["scan_results"].append({
                    "host": scan_match.group(1),
                    "port": int(scan_match.group(2)),
                    "status": scan_match.group(3)
                })
        
        # Extract sniff results
        sniff_pattern = r'([^\s]+)\s+->\s+([^\s]+):\s+(.+)'
        for line in lines:
            sniff_match = re.search(sniff_pattern, line)
            if sniff_match:
                result["sniff_results"].append({
                    "source": sniff_match.group(1),
                    "destination": sniff_match.group(2),
                    "details": sniff_match.group(3)
                })
        
        # Extract error messages
        error_pattern = r'(error|failed|timeout|unreachable)'
        for line in lines:
            if re.search(error_pattern, line, re.IGNORECASE):
                result["errors"].append(line.strip())
        
        # Extract statistics
        stats_pattern = r'(\d+)\s+packets?\s+(\w+)'
        for line in lines:
            stats_match = re.search(stats_pattern, line, re.IGNORECASE)
            if stats_match:
                count = int(stats_match.group(1))
                stat_type = stats_match.group(2).lower()
                result["statistics"][stat_type] = count
        
        # Extract target information
        target_pattern = r'target:\s*([^\s]+)'
        target_match = re.search(target_pattern, output_text, re.IGNORECASE)
        if target_match:
            result["target_info"]["address"] = target_match.group(1)
        
        # Extract metadata
        result["metadata"] = {
            "total_lines": len(lines),
            "has_errors": len(result["errors"]) > 0,
            "has_responses": len(result["responses"]) > 0,
            "has_scan_results": len(result["scan_results"]) > 0,
            "has_sniff_results": len(result["sniff_results"]) > 0
        }
        
    except Exception as e:
        result["errors"].append(f"Error parsing output: {str(e)}")
    
    return result

class ScapyTool(BaseTool):
    """Scapy Tool for network packet manipulation and analysis."""

    args_model = ScapyArgs

    name: str = "scapy"
    description: str = "Interactive packet manipulation program for network analysis and stress testing"
    version: str = "1.0.0"
    def run(self, args: ScapyArgs) -> ToolResult:
        """Execute scapy with the specified arguments."""
        try:
            # Construct the scapy command
            cmd = ["python3", "-c"]
            
            # Build the scapy script based on mode
            script_lines = [
                "from scapy.all import *",
                "import sys",
                "import time",
                "",
                "# Configure verbosity",
                f"conf.verb = {'2' if args.verbose else '0'}",
                "",
                "# Set interface if specified",
                f"if '{args.interface}':",
                f"    conf.iface = '{args.interface}'",
                "",
                "# Set timeout",
                f"conf.timeout = {args.timeout or 5}",
                ""
            ]
            
            # Add mode-specific commands
            if args.mode == ScapyMode.PING:
                script_lines.extend([
                    f"# Ping {args.target}",
                    f"ans, unans = sr(IP(dst='{args.target}')/ICMP(), timeout={args.timeout or 5})",
                    "print('Ping Results:')",
                    "for snd, rcv in ans:",
                    "    print(f'{rcv[IP].src} -> {snd[IP].dst}: icmp_seq={rcv[ICMP].seq} ttl={rcv[IP].ttl} time={rcv.time - snd.time:.3f}s')",
                    "print(f'Packets sent: {len(ans)}')",
                    "print(f'Packets received: {len(ans)}')"
                ])
            
            elif args.mode == ScapyMode.TRACEROUTE:
                script_lines.extend([
                    f"# Traceroute to {args.target}",
                    f"ans, unans = sr(IP(dst='{args.target}', ttl=(1,30))/ICMP(), timeout={args.timeout or 5})",
                    "print('Traceroute Results:')",
                    "for snd, rcv in ans:",
                    "    print(f'{snd[IP].ttl} {rcv[IP].src} {rcv.time - snd.time:.3f}s')",
                    "    if rcv[IP].src == args.target:",
                    "        break"
                ])
            
            elif args.mode == ScapyMode.SYN_SCAN:
                script_lines.extend([
                    f"# SYN scan of {args.target}",
                    f"ports = range(1, 1025) if {args.port} is None else [{args.port}]",
                    f"ans, unans = sr(IP(dst='{args.target}')/TCP(dport=ports, flags='S'), timeout={args.timeout or 5})",
                    "print('SYN Scan Results:')",
                    "for snd, rcv in ans:",
                    "    if rcv[TCP].flags == 0x12:  # SYN-ACK",
                    "        print(f'{args.target}:{snd[TCP].dport} open')",
                    "    elif rcv[TCP].flags == 0x14:  # RST-ACK",
                    "        print(f'{args.target}:{snd[TCP].dport} closed')"
                ])
            
            elif args.mode == ScapyMode.UDP_SCAN:
                script_lines.extend([
                    f"# UDP scan of {args.target}",
                    f"ports = range(1, 1025) if {args.port} is None else [{args.port}]",
                    f"ans, unans = sr(IP(dst='{args.target}')/UDP(dport=ports), timeout={args.timeout or 5})",
                    "print('UDP Scan Results:')",
                    "for snd, rcv in ans:",
                    "    if rcv.haslayer(ICMP) and rcv[ICMP].type == 3:",
                    "        print(f'{args.target}:{snd[UDP].dport} closed')",
                    "    else:",
                    "        print(f'{args.target}:{snd[UDP].dport} open|filtered')"
                ])
            
            elif args.mode == ScapyMode.ARP_SCAN:
                script_lines.extend([
                    f"# ARP scan of {args.target}",
                    f"ans, unans = srp(Ether(dst='ff:ff:ff:ff:ff:ff')/ARP(pdst='{args.target}'), timeout={args.timeout or 5})",
                    "print('ARP Scan Results:')",
                    "for snd, rcv in ans:",
                    "    print(f'{rcv[ARP].psrc} -> {rcv[ARP].hwsrc}')"
                ])
            
            elif args.mode == ScapyMode.SNIFF:
                script_lines.extend([
                    "# Sniff packets",
                    f"filter_str = '{args.filter}' if '{args.filter}' else None",
                    f"packets = sniff(count={args.packet_count or 10}, filter=filter_str, timeout={args.duration or 10})",
                    "print('Sniff Results:')",
                    "for pkt in packets:",
                    "    if pkt.haslayer(IP):",
                    "        print(f'{pkt[IP].src} -> {pkt[IP].dst}: {pkt.summary()}')"
                ])
            
            elif args.mode == ScapyMode.FLOOD:
                script_lines.extend([
                    f"# Flood {args.target}",
                    f"packet = IP(dst='{args.target}')/ICMP()",
                    f"send(packet, count={args.count or 100}, inter={args.interval or 0.1})",
                    "print(f'Flooded {args.target} with {args.count or 100} packets')"
                ])
            
            elif args.mode == ScapyMode.CUSTOM and args.commands:
                script_lines.extend([
                    "# Custom commands",
                    args.commands
                ])
            
            else:
                # Default ping mode
                script_lines.extend([
                    f"# Default ping to {args.target}",
                    f"ans, unans = sr(IP(dst='{args.target}')/ICMP(), timeout={args.timeout or 5})",
                    "print('Ping Results:')",
                    "for snd, rcv in ans:",
                    "    print(f'{rcv[IP].src} -> {snd[IP].dst}: icmp_seq={rcv[ICMP].seq} ttl={rcv[IP].ttl} time={rcv.time - snd.time:.3f}s')"
                ])
            
            # Join script lines
            script = "\n".join(script_lines)
            cmd.append(script)
            
            # Execute the command
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=args.timeout_seconds
            )
            
            # Parse the output
            parsed_output = parse_scapy_output(process.stdout)
            
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
                    "description": "Scapy output results"
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
                    "target": args.target,
                    "protocol": args.protocol.value
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
                    "target": args.target,
                    "protocol": args.protocol.value
                }
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output={"errors": [f"Error executing scapy: {str(e)}"]},
                artifacts=[],
                metadata={
                    "command": " ".join(cmd) if 'cmd' in locals() else "Unknown",
                    "return_code": -1,
                    "execution_time": None,
                    "mode": args.mode.value,
                    "target": args.target,
                    "protocol": args.protocol.value
                }
            )
