"""
Ncrack tool implementation.

This module provides an interface to Ncrack,
a high-speed network authentication cracking tool.
"""

import os
import subprocess
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class Protocol(str, Enum):
    """Supported protocols for Ncrack."""
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


class NcrackArgs(BaseToolArgs):
    """Arguments for the Ncrack tool."""
    protocol: Protocol = Field(..., description="Protocol to attack")
    service_type: Optional[ServiceType] = Field(None, description="Service type for specific protocols")
    target: str = Field(..., description="Target host or IP address")
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
    max_connections: Optional[int] = Field(None, description="Maximum concurrent connections")
    min_connections: Optional[int] = Field(None, description="Minimum concurrent connections")
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


def parse_ncrack_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """
    Parse the output from ncrack command.

    Args:
        stdout: Standard output from ncrack
        stderr: Standard error from ncrack

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
            if "Ncrack" in line and "starting" in line:
                result["attack_info"]["status"] = "started"
            elif "Ncrack" in line and "finished" in line:
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
            elif "Ncrack v" in line:
                result["general_info"]["version"] = line.split("Ncrack v")[1].split()[0]
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


class NcrackTool(BaseTool):
    """Ncrack tool implementation."""

    args_model = NcrackArgs

    def build_command(self, args: NcrackArgs) -> List[str]:
        cmd: List[str] = ["ncrack"]

        # Verbosity/debug
        if args.verbose:
            cmd.append("-v")
        if args.debug:
            cmd.append("-d")
        if args.quiet:
            cmd.append("-q")
        if args.version:
            cmd.append("-V")
        if args.help:
            cmd.append("-h")

        # Listing/info flags
        if args.list_modules:
            cmd.append("-M")
        if args.list_protocols:
            cmd.append("-L")
        if args.show:
            cmd.append("-U")

        # Concurrency and timing
        if args.max_connections:
            cmd.extend(["-t", str(args.max_connections)])
        if args.timeout:
            cmd.extend(["-W", str(args.timeout)])
        if args.retry_delay:
            cmd.extend(["-R", str(args.retry_delay)])
        if args.max_attempts:
            cmd.extend(["-M", str(args.max_attempts)])
        if args.min_connections:
            cmd.extend(["-l", str(args.min_connections)])

        # Username/password sources
        if args.username:
            cmd.extend(["-U", args.username])
        elif args.username_list:
            cmd.extend(["-L", args.username_list])

        if args.password:
            cmd.extend(["-P", args.password])
        elif args.password_list:
            cmd.extend(["-p", args.password_list])
        elif args.user_as_pass:
            cmd.append("-e")
        elif args.pass_as_user:
            cmd.append("-C")
        elif args.null_pass:
            cmd.append("-n")
        elif args.same_pass:
            cmd.append("-s")

        # Login string helpers
        if args.login:
            cmd.extend(["-x", args.login])
        if args.password_str:
            cmd.extend(["-y", args.password_str])

        # Looping
        if args.loop_users:
            cmd.append("-u")
        if args.loop_passwords:
            cmd.append("-U")

        # Output/session
        if args.output_file:
            cmd.extend(["-o", args.output_file])
        if args.log_file:
            cmd.extend(["-b", args.log_file])
        if args.restore_file:
            cmd.extend(["-R", args.restore_file])
        if args.save_file:
            cmd.extend(["-S", args.save_file])

        # Modules/config
        if args.module:
            cmd.extend(["-m", args.module])
        if args.module_path:
            cmd.extend(["-M", args.module_path])
        if args.config_file:
            cmd.extend(["-c", args.config_file])

        # Proxy/SSL
        if args.proxy:
            cmd.extend(["-x", args.proxy])
        if args.proxy_auth:
            cmd.extend(["-X", args.proxy_auth])
        if args.ssl:
            cmd.append("-s")
        if not args.ssl_verify:
            cmd.append("-k")
        if args.ssl_version:
            cmd.extend(["-V", args.ssl_version])
        if args.cipher:
            cmd.extend(["-C", args.cipher])
        if args.cert:
            cmd.extend(["-E", args.cert])
        if args.key:
            cmd.extend(["-K", args.key])
        if args.ca_cert:
            cmd.extend(["-A", args.ca_cert])

        # HTTP options
        if args.http_method:
            cmd.extend(["--http-method", args.http_method])
        if args.http_path:
            cmd.extend(["--http-path", args.http_path])
        if args.http_data:
            cmd.extend(["--http-data", args.http_data])
        if args.http_headers:
            cmd.extend(["--http-headers", args.http_headers])
        if args.http_cookie:
            cmd.extend(["--http-cookie", args.http_cookie])
        if args.http_user_agent:
            cmd.extend(["--http-user-agent", args.http_user_agent])
        if args.http_referer:
            cmd.extend(["--http-referer", args.http_referer])
        if args.http_auth_type:
            cmd.extend(["--http-auth-type", args.http_auth_type])

        # SMTP options
        if args.smtp_domain:
            cmd.extend(["--smtp-domain", args.smtp_domain])
        if args.smtp_from:
            cmd.extend(["--smtp-from", args.smtp_from])
        if args.smtp_to:
            cmd.extend(["--smtp-to", args.smtp_to])
        if args.smtp_subject:
            cmd.extend(["--smtp-subject", args.smtp_subject])
        if args.smtp_body:
            cmd.extend(["--smtp-body", args.smtp_body])

        # Target/service
        cmd.append(args.target)
        if args.port:
            cmd.append(str(args.port))
        cmd.append(args.protocol.value)
        if args.service_type:
            cmd.append(args.service_type.value)

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: NcrackArgs,
    ) -> Dict[str, Any]:
        parsed = parse_ncrack_output(stdout or "", stderr or "")
        parsed["exit_code"] = exit_code
        parsed["protocol"] = args.protocol.value
        if args.service_type:
            parsed["service_type"] = args.service_type.value
        parsed.setdefault("target_info", {})["host"] = args.target
        if args.port:
            parsed.setdefault("target_info", {})["port"] = args.port

        # Mask sensitive credentials in metadata
        for credential in parsed.get("credentials", []):
            if "password" in credential:
                credential["password"] = "***"
        return parsed

    def create_artifacts(
        self,
        stdout: str,
        args: NcrackArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        artifacts: List[str] = []
        if args.output_file and os.path.exists(args.output_file):
            artifacts.append(args.output_file)
        if args.log_file and os.path.exists(args.log_file):
            artifacts.append(args.log_file)
        if args.save_file and os.path.exists(args.save_file):
            artifacts.append(args.save_file)

        if stdout and len(stdout) > 200:
            ts = int(timestamp or time.time())
            try:
                os.makedirs("artifacts", exist_ok=True)
                artifact_path = f"artifacts/ncrack_{args.protocol.value}_{ts}.txt"
                with open(artifact_path, "w", encoding="utf-8") as handle:
                    handle.write(stdout)
                artifacts.append(artifact_path)
            except OSError:
                # Best-effort artifact creation; ignore failures to avoid breaking the tool
                pass

        return artifacts

    def run(self, args: NcrackArgs) -> ToolResult:
        start_ts = time.time()
        try:
            cmd = self.build_command(args)
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                exit_code=-2,
                stdout="",
                stderr="ncrack command timed out after 10 minutes",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start_ts,
            )
        except FileNotFoundError:
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="ncrack command not found. Please ensure Ncrack is installed.",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start_ts,
            )
        except Exception as exc:
            msg = f"Error executing ncrack: {exc}"
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr=msg,
                artifacts=[],
                metadata={},
                execution_time=time.time() - start_ts,
            )

        metadata = self.parse_output(
            stdout=process.stdout,
            stderr=process.stderr,
            exit_code=process.returncode,
            args=args,
        )
        artifacts = self.create_artifacts(
            stdout=process.stdout,
            args=args,
            timestamp=int(start_ts),
        )

        return ToolResult(
            success=process.returncode == 0,
            exit_code=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
            artifacts=artifacts,
            metadata=metadata,
            execution_time=time.time() - start_ts,
        )


# Export the tool class
__all__ = ["NcrackTool", "NcrackArgs"]


# ---------------------------------------------------------------------------
# Tool Metadata Registration
# ---------------------------------------------------------------------------
from ...enhanced_metadata_registry import (  # noqa: E402
    EnhancedToolMetadata,
    PentestPhase,
    ToolCapability,
    ToolCategory,
    register_enhanced_tool_metadata,
)

register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="password_attacks.online_attacks.ncrack",
        display_name="Ncrack",
        category=ToolCategory.PASSWORD_ATTACKS,
        applicable_phases=[PentestPhase.EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="online_password_bruteforce",
                description="Rapid network authentication cracking (SSH, RDP, FTP, HTTP, SMB, VNC) with wordlists; returns valid credential pairs; tuned for speed over stealth.",
                output_indicators=["login", "success", "credential"],
            ),
        ],
        required_services=["ssh", "rdp", "ftp", "http", "smb"],
        target_protocols=["tcp"],
        execution_priority=6,
        parallel_compatible=False,
        stealth_level=2,
        estimated_runtime_minutes=20,
    )
)
