"""
Medusa tool implementation.

This module provides an interface to Medusa,
a fast, parallel, and modular login brute-forcer.
"""

from __future__ import annotations

import os
import subprocess
import time
from enum import Enum
from typing import Optional, Dict, Any, List

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class Protocol(str, Enum):
    """Supported protocols for Medusa."""
    SSH = "ssh"
    FTP = "ftp"
    TELNET = "telnet"
    HTTP = "http"
    HTTPS = "https"
    HTTP_PROXY = "http-proxy"
    HTTPS_PROXY = "https-proxy"
    SMTP = "smtp"
    POP3 = "pop3"
    IMAP = "imap"
    MYSQL = "mysql"
    POSTGRESQL = "postgresql"
    ORACLE = "oracle"
    MSSQL = "mssql"
    RDP = "rdp"
    VNC = "vnc"
    SNMP = "snmp"
    LDAP = "ldap"
    SMB = "smb"
    AFP = "afp"
    RSH = "rsh"
    REXEC = "rexec"
    RLOGIN = "rlogin"
    CVS = "cvs"
    SVN = "svn"
    SOCKS5 = "socks5"
    PCNFS = "pcnfs"
    NNTP = "nntp"
    XMPP = "xmpp"
    IRC = "irc"
    DNS = "dns"
    SMTP_AUTH = "smtp-auth"
    SMTP_ENUM = "smtp-enum"
    SNMP_ENUM = "snmp-enum"
    LDAP_ENUM = "ldap-enum"


class ServiceType(str, Enum):
    """Service types for specific protocols."""
    HTTP_GET = "http-get"
    HTTP_POST = "http-post"
    HTTP_HEAD = "http-head"
    HTTP_PROXY = "http-proxy"
    HTTPS_GET = "https-get"
    HTTPS_POST = "https-post"
    HTTPS_HEAD = "https-head"
    HTTPS_PROXY = "https-proxy"
    SMTP_AUTH_LOGIN = "smtp-auth-login"
    SMTP_AUTH_PLAIN = "smtp-auth-plain"
    SMTP_AUTH_CRAM_MD5 = "smtp-auth-cram-md5"
    SMTP_AUTH_DIGEST_MD5 = "smtp-auth-digest-md5"
    SMTP_AUTH_NTLM = "smtp-auth-ntlm"
    POP3_AUTH_LOGIN = "pop3-auth-login"
    POP3_AUTH_PLAIN = "pop3-auth-plain"
    POP3_AUTH_CRAM_MD5 = "pop3-auth-cram-md5"
    POP3_AUTH_DIGEST_MD5 = "pop3-auth-digest-md5"
    POP3_AUTH_NTLM = "pop3-auth-ntlm"
    IMAP_AUTH_LOGIN = "imap-auth-login"
    IMAP_AUTH_PLAIN = "imap-auth-plain"
    IMAP_AUTH_CRAM_MD5 = "imap-auth-cram-md5"
    IMAP_AUTH_DIGEST_MD5 = "imap-auth-digest-md5"
    IMAP_AUTH_NTLM = "imap-auth-ntlm"


class OutputFormat(str, Enum):
    """Output format options."""
    RAW = "raw"
    JSON = "json"
    XML = "xml"
    CSV = "csv"


class MedusaArgs(BaseToolArgs):
    """Arguments for the Medusa tool."""
    protocol: Protocol = Field(..., description="Protocol to attack")
    service_type: Optional[ServiceType] = Field(None, description="Service type for specific protocols")
    port: Optional[int] = Field(None, description="Target port number")
    username: Optional[str] = Field(None, description="Single username to test")
    username_list: Optional[str] = Field(None, description="File containing usernames")
    password: Optional[str] = Field(None, description="Single password to test")
    password_list: Optional[str] = Field(None, description="File containing passwords")
    login: Optional[str] = Field(None, description="Login string (username:password)")
    password_str: Optional[str] = Field(None, description="Password string")
    user_as_pass: bool = Field(False, description="Use username as password")
    pass_as_user: bool = Field(False, description="Use password as username")
    null_pass: bool = Field(False, description="Try empty password")
    same_pass: bool = Field(False, description="Try same password for all users")
    loop_users: bool = Field(False, description="Loop through users")
    loop_passwords: bool = Field(False, description="Loop through passwords")
    output_format: OutputFormat = Field(OutputFormat.RAW, description="Output format")
    output_file: Optional[str] = Field(None, description="Output file path")
    log_file: Optional[str] = Field(None, description="Log file path")
    restore_file: Optional[str] = Field(None, description="Restore session from file")
    save_file: Optional[str] = Field(None, description="Save session to file")
    timeout: Optional[int] = Field(None, description="Connection timeout in seconds")
    retry_delay: Optional[int] = Field(None, description="Delay between retries in seconds")
    max_attempts: Optional[int] = Field(None, description="Maximum login attempts")
    max_threads: Optional[int] = Field(None, description="Maximum concurrent threads")
    min_threads: Optional[int] = Field(None, description="Minimum concurrent threads")
    verbose: bool = Field(False, description="Enable verbose output")
    debug: bool = Field(False, description="Enable debug output")
    quiet: bool = Field(False, description="Suppress output")
    help: bool = Field(False, description="Show help information")
    version: bool = Field(False, description="Show version information")
    list_modules: bool = Field(False, description="List available modules")
    list_protocols: bool = Field(False, description="List available protocols")
    show: bool = Field(False, description="Show module information")
    module: Optional[str] = Field(None, description="Module to use")
    module_path: Optional[str] = Field(None, description="Module path")
    config_file: Optional[str] = Field(None, description="Configuration file")
    proxy: Optional[str] = Field(None, description="Proxy server (host:port)")
    proxy_auth: Optional[str] = Field(None, description="Proxy authentication (user:pass)")
    ssl: bool = Field(False, description="Use SSL/TLS")
    ssl_verify: bool = Field(True, description="Verify SSL certificates")
    ssl_version: Optional[str] = Field(None, description="SSL version to use")
    cipher: Optional[str] = Field(None, description="SSL cipher to use")
    cert: Optional[str] = Field(None, description="Client certificate file")
    key: Optional[str] = Field(None, description="Client private key file")
    ca_cert: Optional[str] = Field(None, description="CA certificate file")
    http_method: Optional[str] = Field(None, description="HTTP method to use")
    http_path: Optional[str] = Field(None, description="HTTP path to request")
    http_data: Optional[str] = Field(None, description="HTTP POST data")
    http_headers: Optional[str] = Field(None, description="HTTP headers")
    http_cookie: Optional[str] = Field(None, description="HTTP cookie")
    http_user_agent: Optional[str] = Field(None, description="HTTP user agent")
    http_referer: Optional[str] = Field(None, description="HTTP referer")
    http_auth_type: Optional[str] = Field(None, description="HTTP authentication type")
    smtp_domain: Optional[str] = Field(None, description="SMTP domain")
    smtp_from: Optional[str] = Field(None, description="SMTP from address")
    smtp_to: Optional[str] = Field(None, description="SMTP to address")
    smtp_subject: Optional[str] = Field(None, description="SMTP subject")
    smtp_body: Optional[str] = Field(None, description="SMTP body")
    pop3_user: Optional[str] = Field(None, description="POP3 username")
    pop3_pass: Optional[str] = Field(None, description="POP3 password")
    imap_user: Optional[str] = Field(None, description="IMAP username")
    imap_pass: Optional[str] = Field(None, description="IMAP password")
    mysql_user: Optional[str] = Field(None, description="MySQL username")
    mysql_pass: Optional[str] = Field(None, description="MySQL password")
    mysql_db: Optional[str] = Field(None, description="MySQL database")
    postgres_user: Optional[str] = Field(None, description="PostgreSQL username")
    postgres_pass: Optional[str] = Field(None, description="PostgreSQL password")
    postgres_db: Optional[str] = Field(None, description="PostgreSQL database")
    oracle_user: Optional[str] = Field(None, description="Oracle username")
    oracle_pass: Optional[str] = Field(None, description="Oracle password")
    oracle_sid: Optional[str] = Field(None, description="Oracle SID")
    mssql_user: Optional[str] = Field(None, description="MSSQL username")
    mssql_pass: Optional[str] = Field(None, description="MSSQL password")
    mssql_db: Optional[str] = Field(None, description="MSSQL database")
    rdp_user: Optional[str] = Field(None, description="RDP username")
    rdp_pass: Optional[str] = Field(None, description="RDP password")
    rdp_domain: Optional[str] = Field(None, description="RDP domain")
    vnc_pass: Optional[str] = Field(None, description="VNC password")
    snmp_community: Optional[str] = Field(None, description="SNMP community string")
    snmp_version: Optional[str] = Field(None, description="SNMP version")
    ldap_user: Optional[str] = Field(None, description="LDAP username")
    ldap_pass: Optional[str] = Field(None, description="LDAP password")
    ldap_base: Optional[str] = Field(None, description="LDAP base DN")
    smb_user: Optional[str] = Field(None, description="SMB username")
    smb_pass: Optional[str] = Field(None, description="SMB password")
    smb_domain: Optional[str] = Field(None, description="SMB domain")
    smb_share: Optional[str] = Field(None, description="SMB share")
    afp_user: Optional[str] = Field(None, description="AFP username")
    afp_pass: Optional[str] = Field(None, description="AFP password")
    afp_share: Optional[str] = Field(None, description="AFP share")


def parse_medusa_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """
    Parse the output from medusa command.

    Args:
        stdout: Standard output from medusa
        stderr: Standard error from medusa

    Returns:
        Dictionary containing parsed output information
    """
    result = {
        "attack_info": {},
        "target_info": {},
        "credentials": [],
        "statistics": {},
        "errors": [],
        "warnings": [],
        "general_info": {}
    }

    # Parse stdout
    if stdout:
        lines = stdout.strip().split('\n')

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Parse attack information
            if "Medusa" in line and "starting" in line:
                result["attack_info"]["status"] = "started"
            elif "Medusa" in line and "finished" in line:
                result["attack_info"]["status"] = "finished"
            elif "Target:" in line:
                result["target_info"]["host"] = line.split(":", 1)[1].strip()
            elif "Port:" in line:
                result["target_info"]["port"] = line.split(":", 1)[1].strip()
            elif "Protocol:" in line:
                result["attack_info"]["protocol"] = line.split(":", 1)[1].strip()
            elif "Service:" in line:
                result["attack_info"]["service"] = line.split(":", 1)[1].strip()

            # Parse credentials
            elif "[SUCCESS]" in line:
                success_info = line.split("[SUCCESS]")[1].strip()
                # Extract username and password from success line
                if "login:" in success_info and "password:" in success_info:
                    parts = success_info.split()
                    for i, part in enumerate(parts):
                        if part == "login:" and i + 1 < len(parts):
                            username = parts[i + 1]
                        elif part == "password:" and i + 1 < len(parts):
                            password = parts[i + 1]
                            result["credentials"].append({
                                "username": username,
                                "password": password,
                                "protocol": result["attack_info"].get("protocol", ""),
                                "service": result["attack_info"].get("service", "")
                            })
                            break

            # Parse statistics
            elif "valid passwords found" in line:
                result["statistics"]["valid_passwords"] = int(line.split()[0])
            elif "valid pairs found" in line:
                result["statistics"]["valid_pairs"] = int(line.split()[0])
            elif "completed" in line and "tasks" in line:
                result["statistics"]["completed_tasks"] = int(line.split()[0])
            elif "remaining" in line and "tasks" in line:
                result["statistics"]["remaining_tasks"] = int(line.split()[0])
            elif "time elapsed" in line:
                result["statistics"]["time_elapsed"] = line.split("time elapsed:")[1].strip()

            # Parse general information
            elif "Medusa v" in line:
                result["general_info"]["version"] = line.split("Medusa v")[1].split()[0]
            elif "Modules:" in line:
                result["general_info"]["modules"] = line.split(":", 1)[1].strip()
            elif "WARNING:" in line:
                result["warnings"].append(line.split("WARNING:", 1)[1].strip())
            elif "ERROR:" in line:
                result["errors"].append(line.split("ERROR:", 1)[1].strip())
            elif "FATAL:" in line:
                result["errors"].append(line.split("FATAL:", 1)[1].strip())

    # Parse stderr
    if stderr:
        stderr_lines = stderr.strip().split('\n')
        for line in stderr_lines:
            line = line.strip()
            if line and ("ERROR:" in line or "error:" in line):
                result["errors"].append(line)
            elif line and ("WARNING:" in line or "warning:" in line):
                result["warnings"].append(line)
            elif line and ("FATAL:" in line or "fatal:" in line):
                result["errors"].append(line)

    return result


class MedusaTool(BaseTool):
    """Medusa tool implementation.
    
    Supports PTY execution via build_command(), parse_output(), and create_artifacts().
    """

    args_model = MedusaArgs

    def build_command(self, args: MedusaArgs) -> List[str]:
        """Build medusa command arguments.
        
        This method is used by both run() and PTY execution,
        ensuring consistent command construction.
        
        Args:
            args: Validated MedusaArgs
            
        Returns:
            List of command arguments for medusa
        """
        cmd: List[str] = ["medusa"]

        # Verbosity/debug
        if args.verbose:
            cmd.append("-v")
        if args.debug:
            cmd.append("-d")
        if args.quiet:
            cmd.append("-q")

        # Version/help/list flags
        if args.version:
            cmd.append("-V")
        if args.help:
            cmd.append("-h")
        if args.list_modules:
            cmd.append("-M")
        if args.list_protocols:
            cmd.append("-d")  # Note: -d for list in medusa

        # Target host
        cmd.extend(["-h", args.target])

        # Username/password inputs
        if args.username:
            cmd.extend(["-u", args.username])
        elif args.username_list:
            cmd.extend(["-U", args.username_list])

        if args.password:
            cmd.extend(["-p", args.password])
        elif args.password_list:
            cmd.extend(["-P", args.password_list])

        # Extra password options
        if args.null_pass:
            cmd.append("-e")
            cmd.append("n")
        if args.user_as_pass:
            cmd.append("-e")
            cmd.append("s")

        # Port
        if args.port:
            cmd.extend(["-n", str(args.port)])

        # Protocol/Module (-M flag for module)
        service = args.service_type.value if args.service_type else args.protocol.value
        cmd.extend(["-M", service])

        # Thread control
        if args.max_threads:
            cmd.extend(["-t", str(args.max_threads)])

        # Timeout
        if args.timeout:
            cmd.extend(["-T", str(args.timeout)])

        # Output file
        if args.output_file:
            cmd.extend(["-O", args.output_file])

        # Additional options
        if args.http_path:
            cmd.extend(["-m", f"PATH:{args.http_path}"])
        if args.http_method:
            cmd.extend(["-m", f"METHOD:{args.http_method}"])

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: MedusaArgs,
    ) -> Dict[str, Any]:
        """Parse medusa output into structured metadata.
        
        Reuses the standalone parse_medusa_output function for consistency.
        
        Args:
            stdout: Command stdout
            stderr: Command stderr
            exit_code: Command exit code
            args: Original MedusaArgs
            
        Returns:
            Metadata dict with credentials, attack_info, statistics, etc.
        """
        parsed = parse_medusa_output(stdout or "", stderr or "")
        parsed["exit_code"] = exit_code
        parsed["protocol"] = args.protocol.value
        if args.service_type:
            parsed["service_type"] = args.service_type.value
        if args.port:
            parsed.setdefault("target_info", {})["port"] = args.port
        return parsed

    def create_artifacts(
        self,
        stdout: str,
        args: MedusaArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create artifact files from medusa output.
        
        Args:
            stdout: Command stdout
            args: Original MedusaArgs
            timestamp: Optional timestamp for artifact naming
            
        Returns:
            List of artifact file paths created
        """
        artifacts: List[str] = []

        # Respect explicit medusa output file if present and created
        if args.output_file and os.path.exists(args.output_file):
            artifacts.append(args.output_file)

        if args.log_file and os.path.exists(args.log_file):
            artifacts.append(args.log_file)

        if args.save_file and os.path.exists(args.save_file):
            artifacts.append(args.save_file)

        # Save stdout for auditing when meaningful
        if stdout and len(stdout) > 200:
            ts = int(timestamp or 0) or int(time.time())
            os.makedirs("artifacts", exist_ok=True)
            path = f"artifacts/medusa_{args.protocol.value}_{ts}.txt"
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(path)
            except OSError:
                pass

        return artifacts

    def run(self, args: MedusaArgs) -> ToolResult:
        """Execute medusa scan.
        
        Uses build_command(), parse_output(), and create_artifacts() for
        consistent behavior with PTY execution path.
        """
        start = time.time()
        try:
            cmd = self.build_command(args)
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,  # password attacks can be long-running
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                exit_code=-2,
                stdout="",
                stderr="Command timed out after 10 minutes",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )
        except FileNotFoundError:
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="medusa command not found. Ensure Medusa is installed in the execution environment.",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr=f"Error executing medusa: {str(e)}",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )

        metadata = self.parse_output(
            stdout=process.stdout,
            stderr=process.stderr,
            exit_code=process.returncode,
            args=args,
        )
        artifacts = self.create_artifacts(process.stdout, args=args, timestamp=int(start))

        return ToolResult(
            success=process.returncode == 0,
            exit_code=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
            artifacts=artifacts,
            metadata=metadata,
            execution_time=time.time() - start,
        )


# Export the tool class
__all__ = ["MedusaTool", "MedusaArgs"]


# ---------------------------------------------------------------------------
# Tool Metadata Registration
# ---------------------------------------------------------------------------
from ...enhanced_metadata_registry import (  # noqa: E402
    register_enhanced_tool_metadata,
    EnhancedToolMetadata,
    ToolCapability,
    ToolCategory,
    PentestPhase,
)

register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="password_attacks.online_attacks.medusa",
        display_name="Medusa",
        category=ToolCategory.PASSWORD_ATTACKS,
        applicable_phases=[PentestPhase.EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="parallel_password_bruteforce",
                description="Parallel brute-force logins (SSH, FTP, SMTP, MySQL, RDP, VNC, SMB) with wordlists; returns valid credential pairs; use for high-throughput multi-protocol attacks.",
                output_indicators=["SUCCESS", "login", "password"],
            ),
            ToolCapability(
                name="multi_protocol_support",
                description="Support for multiple authentication protocols (SSH, FTP, HTTP, SMB, etc.)",
                output_indicators=["protocol", "service"],
            ),
        ],
        required_services=["ssh", "ftp", "telnet", "http", "https", "smtp", "pop3", "imap", "mysql", "postgresql", "smb", "rdp", "vnc"],
        target_protocols=["tcp"],
        execution_priority=6,
        parallel_compatible=False,
        stealth_level=2,
        estimated_runtime_minutes=20,
        best_combined_with=["password_attacks.online_attacks.hydra"],
    )
)
