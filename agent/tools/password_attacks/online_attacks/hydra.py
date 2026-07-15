"""
Hydra tool implementation.

This module provides an interface to Hydra,
a fast network logon cracker for various protocols.
"""

from __future__ import annotations

import subprocess
import re
import os
from typing import Optional, Dict, Any, List
from enum import Enum
from urllib.parse import urlsplit
from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult
from runtime_shared.durable_secret_masking import mask_durable_secrets
from runtime_shared.semantic.canonical_keys import build_finding_vulnerability_key
from runtime_shared.semantic.service_identity import (
    build_service_socket_key,
    default_port_for_application_protocol,
)


HYDRA_SEMANTIC_SCHEMA_VERSION = "hydra.v1"
HYDRA_CAPABILITY_FAMILY = "credential_attack"
HYDRA_WEAK_AUTH_DETECTOR_ID = "hydra/weak-auth"
_MASKED_PASSWORD = "***"
_HYDRA_VERSION_RE = re.compile(r"\bHydra v(?P<version>[0-9A-Za-z_.-]+)")
_HYDRA_START_RE = re.compile(r"\bHydra\b.*\bstarting at\b(?P<started_at>.+)$", re.IGNORECASE)
_HYDRA_FINISH_RE = re.compile(r"\bHydra\b.*\bfinished at\b(?P<finished_at>.+)$", re.IGNORECASE)
_HYDRA_DATA_RE = re.compile(
    r"^\[DATA\]\s+max\s+(?P<max_tasks>\d+)\s+tasks\s+per\s+(?P<servers>\d+)\s+server,\s+"
    r"overall\s+(?P<overall_tasks>\d+)\s+tasks,\s+(?P<login_tries>\d+)\s+login\s+tries"
    r"\s+\(l:(?P<login_count>\d+)/p:(?P<password_count>\d+)\),\s+~(?P<tries_per_task>\d+)\s+tries\s+per\s+task",
    re.IGNORECASE,
)
_HYDRA_ATTACKING_RE = re.compile(r"^\[DATA\]\s+attacking\s+(?P<url>\S+)", re.IGNORECASE)
_HYDRA_STATUS_RE = re.compile(
    r"^\[STATUS\]\s+(?P<tries_per_minute>[0-9.]+)\s+tries/min,\s+"
    r"(?P<tries_completed>\d+)\s+tries\s+in\s+(?P<elapsed>\S+),\s+"
    r"(?P<tries_remaining>\d+)\s+to\s+do\s+in\s+(?P<eta>\S+),\s+"
    r"(?P<active_tasks>\d+)\s+active",
    re.IGNORECASE,
)
_HYDRA_SUCCESS_RE = re.compile(
    r"^\[(?P<port>\d+)\]\[(?P<service>[^\]]+)\]\s+host:\s*(?P<host>\S+)\s+"
    r"login:\s*(?P<login>\S+)\s+password:\s*(?P<password>.*)$",
    re.IGNORECASE,
)
_HYDRA_LEGACY_SUCCESS_RE = re.compile(
    r"(?:\[SUCCESS\]\s*)?(?:host:\s*(?P<host>\S+)\s+)?login:\s*(?P<login>\S+)\s+password:\s*(?P<password>\S*)",
    re.IGNORECASE,
)
_HYDRA_COMPLETION_RE = re.compile(
    r"(?P<targets_completed>\d+)\s+of\s+(?P<targets_total>\d+)\s+target.*completed,\s+"
    r"(?P<valid_passwords>\d+)\s+valid\s+passwords?\s+found",
    re.IGNORECASE,
)
class Protocol(str, Enum):
    """Supported protocols for Hydra."""
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
    ICQ = "icq"
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


class HydraArgs(BaseToolArgs):
    """Arguments for the Hydra tool."""
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
    max_tasks: Optional[int] = Field(None, description="Maximum concurrent tasks")
    min_parallel: Optional[int] = Field(None, description="Minimum parallel tasks")
    max_parallel: Optional[int] = Field(None, description="Maximum parallel tasks")
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


def parse_hydra_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """
    Parse the output from hydra command.
    
    Args:
        stdout: Standard output from hydra
        stderr: Standard error from hydra
        
    Returns:
        Dictionary containing parsed output information
    """
    result: Dict[str, Any] = {
        "semantic_schema_version": HYDRA_SEMANTIC_SCHEMA_VERSION,
        "capability_family": HYDRA_CAPABILITY_FAMILY,
        "attack_info": {},
        "target_info": {},
        "credentials": [],
        "successful_logins": [],
        "statistics": {},
        "errors": [],
        "warnings": [],
        "general_info": {},
    }
    
    # Parse stdout
    if stdout:
        lines = stdout.strip().split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue

            version_match = _HYDRA_VERSION_RE.search(line)
            if version_match:
                result["general_info"]["version"] = version_match.group("version")

            start_match = _HYDRA_START_RE.search(line)
            if start_match:
                result["attack_info"]["status"] = "started"
                result["attack_info"]["started_at"] = start_match.group("started_at").strip()
                continue

            finish_match = _HYDRA_FINISH_RE.search(line)
            if finish_match:
                result["attack_info"]["status"] = "finished"
                result["attack_info"]["finished_at"] = finish_match.group("finished_at").strip()
                continue

            data_match = _HYDRA_DATA_RE.match(line)
            if data_match:
                result["statistics"].update({
                    "max_tasks_per_server": _safe_int(data_match.group("max_tasks")),
                    "server_count": _safe_int(data_match.group("servers")),
                    "overall_tasks": _safe_int(data_match.group("overall_tasks")),
                    "login_tries_total": _safe_int(data_match.group("login_tries")),
                    "login_count": _safe_int(data_match.group("login_count")),
                    "password_count": _safe_int(data_match.group("password_count")),
                    "tries_per_task": _safe_int(data_match.group("tries_per_task")),
                })
                continue

            attacking_match = _HYDRA_ATTACKING_RE.match(line)
            if attacking_match:
                _apply_attacking_target(result, attacking_match.group("url"))
                continue

            status_match = _HYDRA_STATUS_RE.match(line)
            if status_match:
                status = {
                    "tries_per_minute": _safe_float(status_match.group("tries_per_minute")),
                    "tries_completed": _safe_int(status_match.group("tries_completed")),
                    "elapsed": status_match.group("elapsed"),
                    "tries_remaining": _safe_int(status_match.group("tries_remaining")),
                    "eta": status_match.group("eta"),
                    "active_tasks": _safe_int(status_match.group("active_tasks")),
                }
                result["statistics"]["last_status"] = status
                result["statistics"]["tries_per_minute"] = status["tries_per_minute"]
                result["statistics"]["tries_completed"] = status["tries_completed"]
                result["statistics"]["tries_remaining"] = status["tries_remaining"]
                result["statistics"]["active_tasks"] = status["active_tasks"]
                continue

            success_match = _HYDRA_SUCCESS_RE.match(line)
            if success_match:
                _append_success(
                    result,
                    host=success_match.group("host"),
                    port=success_match.group("port"),
                    service=success_match.group("service"),
                    username=success_match.group("login"),
                    password=success_match.group("password"),
                    source_format="standard",
                )
                continue

            completion_match = _HYDRA_COMPLETION_RE.search(line)
            if completion_match:
                result["statistics"].update({
                    "targets_completed": _safe_int(completion_match.group("targets_completed")),
                    "targets_total": _safe_int(completion_match.group("targets_total")),
                    "valid_passwords": _safe_int(completion_match.group("valid_passwords")),
                })
                continue

            # Parse attack information
            if "Target:" in line:
                result["target_info"]["host"] = line.split(":", 1)[1].strip()
            elif "Port:" in line:
                result["target_info"]["port"] = _safe_int(line.split(":", 1)[1].strip())
            elif "Protocol:" in line:
                result["attack_info"]["protocol"] = line.split(":", 1)[1].strip()
            elif "Service:" in line:
                result["attack_info"]["service"] = line.split(":", 1)[1].strip()
            
            # Parse credentials
            elif "[ATTEMPT]" in line:
                result["statistics"]["attempts"] = result["statistics"].get("attempts", 0) + 1
            elif "[SUCCESS]" in line:
                success_info = line.split("[SUCCESS]", 1)[1].strip()
                legacy_match = _HYDRA_LEGACY_SUCCESS_RE.search(success_info)
                if legacy_match:
                    _append_success(
                        result,
                        host=legacy_match.group("host") or result["target_info"].get("host"),
                        port=result["target_info"].get("port"),
                        service=result["attack_info"].get("service") or result["attack_info"].get("protocol"),
                        username=legacy_match.group("login"),
                        password=legacy_match.group("password"),
                        source_format="success_tag",
                    )
                    continue
            
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

    result["statistics"]["successful_login_count"] = len(result["credentials"])
    result["success_count"] = len(result["credentials"])
    if result["credentials"]:
        first = result["credentials"][0]
        result["target_info"].setdefault("host", first.get("host"))
        result["target_info"].setdefault("port", first.get("port"))
        result["attack_info"].setdefault("protocol", first.get("protocol"))
        result["attack_info"].setdefault("service", first.get("service"))

    return result


def _append_success(
    result: Dict[str, Any],
    *,
    host: Any,
    port: Any,
    service: Any,
    username: Any,
    password: Any,
    source_format: str,
) -> None:
    normalized_service = str(service or result["attack_info"].get("protocol") or "").strip().lower()
    normalized_port = _safe_int(port) or _default_port_for_service(normalized_service)
    credential = {
        "host": str(host or "").strip(),
        "port": normalized_port,
        "protocol": normalized_service,
        "service": normalized_service,
        "username": str(username or "").strip(),
        "account_identifier": str(username or "").strip(),
        "password": _MASKED_PASSWORD,
        "password_present": password is not None,
        "source_format": source_format,
    }
    result["credentials"].append(credential)
    result["successful_logins"].append({
        key: value
        for key, value in credential.items()
        if key not in {"password", "password_present"}
    })
    if credential["host"]:
        result["target_info"].setdefault("host", credential["host"])
    if credential["port"]:
        result["target_info"].setdefault("port", credential["port"])
    if normalized_service:
        result["attack_info"].setdefault("protocol", normalized_service)
        result["attack_info"].setdefault("service", normalized_service)


def _apply_attacking_target(result: Dict[str, Any], url_value: str) -> None:
    result["attack_info"]["target_uri"] = url_value
    parsed = urlsplit(url_value)
    protocol = parsed.scheme.strip().lower()
    host = parsed.hostname or ""
    port = parsed.port or _default_port_for_service(protocol)
    path = parsed.path or "/"
    if protocol:
        result["attack_info"]["protocol"] = protocol
        result["attack_info"]["service"] = protocol
    if host:
        result["target_info"]["host"] = host
    if port:
        result["target_info"]["port"] = port
    result["target_info"]["path"] = path


def _safe_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _default_port_for_service(service: str) -> int | None:
    return default_port_for_application_protocol(service)


def _service_socket_key(host: Any, port: Any) -> str:
    host_text = str(host or "").strip()
    port_value = _safe_int(port)
    if not host_text or not port_value:
        return ""
    try:
        return build_service_socket_key(ip=host_text, protocol="tcp", port=port_value)
    except ValueError:
        return ""


def _semantic_hydra_payload(
    *,
    service_key: str,
    service_name: str,
    account_identifiers: list[str],
    successful_login_count: int,
) -> dict[str, Any]:
    title_service = service_name.upper() if service_name else "service"
    payload: dict[str, Any] = {
        "source": "hydra",
        "detector_id": HYDRA_WEAK_AUTH_DETECTOR_ID,
        "title": f"Weak authentication confirmed on {title_service}",
        "severity": "high",
        "confidence": "confirmed",
        "subject_type": "service.socket",
        "subject_key": service_key,
        "service": service_name,
        "auth_protocol": service_name,
        "successful_login_count": successful_login_count,
        "durable_masking_applied": True,
    }
    if account_identifiers:
        payload["account_identifier"] = account_identifiers[0]
        payload["account_identifiers"] = account_identifiers[:20]
    return payload


class HydraTool(BaseTool):
    """Hydra tool implementation."""
    
    args_model = HydraArgs

    def build_command(self, args: HydraArgs) -> List[str]:
        cmd: List[str] = ["hydra"]

        # Verbosity/debug
        if args.verbose:
            cmd.append("-V")
        if args.debug:
            cmd.append("-d")
        if args.quiet:
            cmd.append("-q")

        # Concurrency (Hydra calls these "tasks")
        if args.max_tasks:
            cmd.extend(["-t", str(args.max_tasks)])

        # Network timeout (seconds per connection attempt)
        if args.timeout:
            cmd.extend(["-W", str(args.timeout)])

        # Username/password inputs
        if args.username:
            cmd.extend(["-l", args.username])
        elif args.username_list:
            cmd.extend(["-L", args.username_list])

        if args.password:
            cmd.extend(["-p", args.password])
        elif args.password_list:
            cmd.extend(["-P", args.password_list])

        # Extra password options (-e n/s/r)
        extras = ""
        if args.null_pass:
            extras += "n"
        if args.user_as_pass:
            extras += "s"
        if args.pass_as_user:
            extras += "r"
        if extras:
            cmd.extend(["-e", extras])

        # Output file (Hydra writes successful creds there as well)
        if args.output_file:
            cmd.extend(["-o", args.output_file])

        # Port selection
        if args.port:
            cmd.extend(["-s", str(args.port)])

        # Target and service/module
        service = args.service_type.value if args.service_type else args.protocol.value
        cmd.append(args.target)
        cmd.append(service)

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: HydraArgs,
    ) -> Dict[str, Any]:
        parsed = parse_hydra_output(stdout or "", stderr or "")
        parsed["exit_code"] = exit_code
        parsed["protocol"] = args.protocol.value
        parsed.setdefault("attack_info", {}).setdefault("protocol", args.protocol.value)
        parsed.setdefault("attack_info", {}).setdefault("service", args.protocol.value)
        parsed.setdefault("target_info", {}).setdefault("host", args.target)
        if args.service_type:
            parsed["service_type"] = args.service_type.value
            parsed.setdefault("attack_info", {})["service"] = args.service_type.value
        if args.port:
            parsed.setdefault("target_info", {})["port"] = args.port
        elif not parsed.get("target_info", {}).get("port"):
            parsed.setdefault("target_info", {})["port"] = _default_port_for_service(args.protocol.value)
        return parsed

    def emit_semantic_observations(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: HydraArgs,
        metadata: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Emit durable-safe weak-auth findings for confirmed Hydra logins."""
        _ = stdout, stderr, exit_code

        credentials = metadata.get("credentials") or []
        if not isinstance(credentials, list) or not credentials:
            return []

        grouped: dict[str, dict[str, Any]] = {}
        for row in credentials:
            if not isinstance(row, dict):
                continue
            service_name = str(row.get("service") or row.get("protocol") or args.protocol.value).strip().lower()
            host = row.get("host") or metadata.get("target_info", {}).get("host") or args.target
            port = row.get("port") or metadata.get("target_info", {}).get("port") or args.port
            if not port:
                port = _default_port_for_service(service_name or args.protocol.value)
            service_key = _service_socket_key(host, port)
            if not service_key:
                continue

            group = grouped.setdefault(
                service_key,
                {"service": service_name or args.protocol.value, "accounts": [], "count": 0},
            )
            group["count"] += 1
            account = str(row.get("account_identifier") or row.get("username") or "").strip()
            if account and account not in group["accounts"]:
                group["accounts"].append(account)

        observations: List[Dict[str, Any]] = []
        for service_key, group in grouped.items():
            finding_key = build_finding_vulnerability_key(
                subject_key=service_key,
                detector_id=HYDRA_WEAK_AUTH_DETECTOR_ID,
            )
            payload = _semantic_hydra_payload(
                service_key=service_key,
                service_name=str(group.get("service") or args.protocol.value),
                account_identifiers=list(group.get("accounts") or []),
                successful_login_count=int(group.get("count") or 0),
            )
            observations.append({
                "observation_type": "finding.vulnerability_confirmed",
                "subject_type": "finding.vulnerability",
                "subject_key": finding_key,
                "payload": mask_durable_secrets(payload, source="hydra_semantic_observations"),
            })
        return observations

    def create_artifacts(
        self,
        stdout: str,
        args: HydraArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        artifacts: List[str] = []

        # Respect explicit hydra output file if present and created
        if args.output_file and os.path.exists(args.output_file):
            artifacts.append(args.output_file)

        # Save stdout for auditing when meaningful
        if stdout and len(stdout) > 200:
            ts = int(timestamp or 0) or int(__import__("time").time())
            os.makedirs("artifacts", exist_ok=True)
            path = f"artifacts/hydra_{args.protocol.value}_{ts}.txt"
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(path)
            except OSError:
                pass

        return artifacts
    
    def run(self, args: HydraArgs) -> ToolResult:
        start = __import__("time").time()
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
                execution_time=__import__("time").time() - start,
            )
        except FileNotFoundError:
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="hydra command not found. Ensure Hydra is installed in the execution environment.",
                artifacts=[],
                metadata={},
                execution_time=__import__("time").time() - start,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr=f"Error executing hydra: {str(e)}",
                artifacts=[],
                metadata={},
                execution_time=__import__("time").time() - start,
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
            execution_time=__import__("time").time() - start,
        )


# Export the tool class
__all__ = ["HydraTool", "HydraArgs"]


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
        tool_id="password_attacks.online_attacks.hydra",
        display_name="Hydra",
        category=ToolCategory.PASSWORD_ATTACKS,
        applicable_phases=[PentestPhase.EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="online_password_bruteforce",
                description="Brute-force or credential-spray network logins (SSH, FTP, HTTP, RDP, SMTP, IMAP, POP3) with username and password lists; returns valid credential pairs; not for offline hash cracking.",
                output_indicators=["login", "password", "success"],
            ),
        ],
        required_services=["http", "https", "ssh", "ftp", "smtp"],
        target_protocols=["tcp"],
        execution_priority=5,
        parallel_compatible=False,
        stealth_level=1,
        estimated_runtime_minutes=20,
    )
)
