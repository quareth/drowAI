"""
Hashcat tool implementation.

This module provides an interface to Hashcat,
a fast password recovery tool for various hash types.
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


class HashType(str, Enum):
    """Supported hash types for Hashcat."""
    MD5 = "0"
    SHA1 = "100"
    SHA256 = "1400"
    SHA512 = "1700"
    NTLM = "1000"
    LM = "3000"
    DES = "1400"
    BSDI = "12400"
    MD5_CRYPT = "500"
    SHA256_CRYPT = "7400"
    SHA512_CRYPT = "1800"
    BLOWFISH = "3200"
    MYSQL = "200"
    MYSQL_SHA1 = "300"
    POSTGRESQL = "111"
    ORACLE = "3100"
    MSSQL = "131"
    LDAP = "1500"
    RADIUS = "1200"
    KERBEROS = "7500"
    AFSCLIENT = "7000"
    AFSSERVER = "7001"
    LOKI = "8000"
    SKEY = "6000"
    SSH = "22"
    RAR = "12500"
    ZIP = "13600"
    PDF = "10500"
    WORD = "9700"
    EXCEL = "9400"
    BITCOIN = "11300"
    ETHEREUM = "15700"
    WPA = "2500"
    WPA2 = "2501"
    WPA3 = "2502"


class AttackMode(str, Enum):
    """Hashcat attack modes."""
    STRAIGHT = "0"
    COMBINATION = "1"
    BRUTE_FORCE = "3"
    HYBRID = "6"
    ASSOCIATION = "9"


class OutputFormat(str, Enum):
    """Output format options."""
    RAW = "raw"
    JSON = "json"
    XML = "xml"
    CSV = "csv"


class HashcatArgs(BaseToolArgs):
    """Arguments for the Hashcat tool."""
    hash_file: Optional[str] = Field(None, description="File containing hashes to crack")
    hash_string: Optional[str] = Field(None, description="Single hash string to crack")
    hash_type: Optional[HashType] = Field(None, description="Type of hash to crack")
    wordlist: Optional[str] = Field(None, description="Wordlist file to use")
    attack_mode: Optional[AttackMode] = Field(None, description="Attack mode to use")
    output_format: OutputFormat = Field(OutputFormat.RAW, description="Output format")
    output_file: Optional[str] = Field(None, description="Output file path")
    pot_file: Optional[str] = Field(None, description="Pot file path")
    session: Optional[str] = Field(None, description="Session name")
    restore_file: Optional[str] = Field(None, description="Restore session from file")
    save_file: Optional[str] = Field(None, description="Save session to file")
    timeout: Optional[int] = Field(None, description="Timeout in seconds")
    max_runtime: Optional[int] = Field(None, description="Maximum runtime in seconds")
    threads: Optional[int] = Field(None, description="Number of threads to use")
    devices: Optional[str] = Field(None, description="Device specification")
    workload_profile: Optional[str] = Field(None, description="Workload profile")
    kernel_accel: Optional[int] = Field(None, description="Kernel acceleration")
    kernel_loops: Optional[int] = Field(None, description="Kernel loops")
    kernel_threads: Optional[int] = Field(None, description="Kernel threads")
    rule_file: Optional[str] = Field(None, description="Rule file to use")
    rule: Optional[str] = Field(None, description="Rule to apply")
    mask: Optional[str] = Field(None, description="Mask for brute force")
    charset: Optional[str] = Field(None, description="Character set")
    min_length: Optional[int] = Field(None, description="Minimum password length")
    max_length: Optional[int] = Field(None, description="Maximum password length")
    increment: bool = Field(False, description="Enable increment mode")
    increment_min: Optional[int] = Field(None, description="Minimum increment length")
    increment_max: Optional[int] = Field(None, description="Maximum increment length")
    verbose: bool = Field(False, description="Enable verbose output")
    debug: bool = Field(False, description="Enable debug output")
    quiet: bool = Field(False, description="Suppress output")
    help: bool = Field(False, description="Show help information")
    version: bool = Field(False, description="Show version information")
    benchmark: bool = Field(False, description="Run benchmark tests")
    speed_only: bool = Field(False, description="Show speed only")
    progress_only: bool = Field(False, description="Show progress only")
    status: bool = Field(False, description="Show status")
    status_timer: Optional[int] = Field(None, description="Status timer")
    machine_readable: bool = Field(False, description="Machine readable output")
    show: bool = Field(False, description="Show cracked passwords")
    left: bool = Field(False, description="Show left hashes")
    username: bool = Field(False, description="Show usernames")
    remove: bool = Field(False, description="Remove cracked hashes")
    remove_timer: Optional[int] = Field(None, description="Remove timer")
    outfile_format: Optional[str] = Field(None, description="Output file format")
    outfile_autohex: bool = Field(False, description="Auto hex output file")
    outfile_check_timer: Optional[int] = Field(None, description="Output file check timer")
    outfile_check_dir: Optional[str] = Field(None, description="Output file check directory")
    outfile_check_file: Optional[str] = Field(None, description="Output file check file")
    outfile_check_disable: bool = Field(False, description="Disable output file check")
    outfile_check_eof: bool = Field(False, description="Check EOF")
    outfile_check_force: bool = Field(False, description="Force output file check")
    outfile_check_hex: bool = Field(False, description="Hex output file check")
    outfile_check_plain: bool = Field(False, description="Plain output file check")
    outfile_check_salt: bool = Field(False, description="Salt output file check")


def parse_hashcat_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """
    Parse the output from hashcat command.

    Args:
        stdout: Standard output from hashcat
        stderr: Standard error from hashcat

    Returns:
        Dictionary containing parsed output information
    """
    result = {
        "crack_info": {},
        "hash_info": {},
        "cracked_passwords": [],
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

            # Parse crack information
            if "Recovered" in line and "password hashes" in line:
                result["crack_info"]["recovered_hashes"] = int(line.split()[1])
            elif "Recovered" in line and "password hashes" in line:
                result["crack_info"]["recovered_hashes"] = int(line.split()[1])
            elif "Recovered" in line and "password hashes" in line:
                result["crack_info"]["recovered_hashes"] = int(line.split()[1])

            # Parse hash information
            elif "Hash type:" in line:
                result["hash_info"]["type"] = line.split(":", 1)[1].strip()
            elif "Hash mode:" in line:
                result["hash_info"]["mode"] = line.split(":", 1)[1].strip()

            # Parse cracked passwords
            elif ":" in line and len(line.split(":")) >= 2:
                parts = line.split(":")
                if len(parts) >= 2:
                    hash_value = parts[0].strip()
                    password = parts[1].strip()
                    result["cracked_passwords"].append({
                        "hash": hash_value,
                        "password": password,
                        "hash_type": result["hash_info"].get("type", ""),
                        "hash_mode": result["hash_info"].get("mode", "")
                    })

            # Parse statistics
            elif "Speed" in line:
                result["statistics"]["speed"] = line.split("Speed")[1].strip()
            elif "Time" in line:
                result["statistics"]["time"] = line.split("Time")[1].strip()
            elif "ETA:" in line:
                result["statistics"]["eta"] = line.split("ETA:")[1].strip()

            # Parse general information
            elif "hashcat" in line and "v" in line:
                result["general_info"]["version"] = line.split("v")[1].split()[0]
            elif "WARNING:" in line:
                result["warnings"].append(line.split("WARNING:", 1)[1].strip())
            elif "ERROR:" in line:
                result["errors"].append(line.split("ERROR:", 1)[1].strip())

    # Parse stderr
    if stderr:
        stderr_lines = stderr.strip().split('\n')
        for line in stderr_lines:
            line = line.strip()
            if line and ("ERROR:" in line or "error:" in line):
                result["errors"].append(line)
            elif line and ("WARNING:" in line or "warning:" in line):
                result["warnings"].append(line)

    return result


class HashcatTool(BaseTool):
    """Hashcat tool implementation.
    
    Supports PTY execution via build_command(), parse_output(), and create_artifacts().
    """

    args_model = HashcatArgs

    def build_command(self, args: HashcatArgs) -> List[str]:
        """Build hashcat command arguments.
        
        This method is used by both run() and PTY execution,
        ensuring consistent command construction.
        
        Args:
            args: Validated HashcatArgs
            
        Returns:
            List of command arguments for hashcat
        """
        cmd: List[str] = ["hashcat"]

        # IMPORTANT: Add --force and --quiet for non-interactive execution
        cmd.append("--force")  # Skip warnings/prompts
        if not args.verbose and not args.debug:
            cmd.append("--quiet")  # Reduce verbosity unless explicitly requested

        # Verbosity/debug
        if args.verbose:
            cmd.append("--status")  # Show status updates
        if args.debug:
            cmd.append("--debug-mode=4")  # Debug output

        # Version/help/benchmark
        if args.version:
            cmd.append("--version")
            return cmd  # Early return for info commands
        if args.help:
            cmd.append("--help")
            return cmd
        if args.benchmark:
            cmd.append("--benchmark")
            return cmd

        # Hash type (required for most operations)
        if args.hash_type:
            cmd.extend(["-m", args.hash_type.value])

        # Attack mode
        if args.attack_mode:
            cmd.extend(["-a", args.attack_mode.value])

        # Session and restore
        if args.session:
            cmd.extend(["--session", args.session])
        if args.restore_file:
            cmd.append("--restore")

        # Performance options
        if args.workload_profile:
            cmd.extend(["-w", args.workload_profile])
        if args.devices:
            cmd.extend(["-d", args.devices])

        # Output options
        if args.output_file:
            cmd.extend(["-o", args.output_file])
        if args.outfile_format:
            cmd.extend(["--outfile-format", args.outfile_format])
        if args.pot_file:
            cmd.extend(["--potfile-path", args.pot_file])

        # Rule options
        if args.rule_file:
            cmd.extend(["-r", args.rule_file])

        # Mask and charset for brute-force
        if args.charset:
            cmd.extend(["-1", args.charset])
        if args.increment:
            cmd.append("--increment")
        if args.increment_min:
            cmd.extend(["--increment-min", str(args.increment_min)])
        if args.increment_max:
            cmd.extend(["--increment-max", str(args.increment_max)])

        # Hash file or stdin
        if args.hash_file:
            cmd.append(args.hash_file)
        elif args.hash_string:
            cmd.append(args.hash_string)  # Direct hash on command line

        # Wordlist (for dictionary attacks)
        if args.wordlist:
            cmd.append(args.wordlist)

        # Mask (for brute-force)
        if args.mask:
            cmd.append(args.mask)

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: HashcatArgs,
    ) -> Dict[str, Any]:
        """Parse hashcat output into structured metadata.
        
        Reuses the standalone parse_hashcat_output function for consistency.
        
        Args:
            stdout: Command stdout
            stderr: Command stderr
            exit_code: Command exit code
            args: Original HashcatArgs
            
        Returns:
            Metadata dict with crack_info, hash_info, cracked_passwords, etc.
        """
        parsed = parse_hashcat_output(stdout or "", stderr or "")
        parsed["exit_code"] = exit_code
        if args.hash_type:
            parsed.setdefault("hash_info", {})["type"] = args.hash_type.value
        if args.attack_mode:
            parsed["attack_mode"] = args.attack_mode.value
        return parsed

    def create_artifacts(
        self,
        stdout: str,
        args: HashcatArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create artifact files from hashcat output.
        
        Args:
            stdout: Command stdout
            args: Original HashcatArgs
            timestamp: Optional timestamp for artifact naming
            
        Returns:
            List of artifact file paths created
        """
        artifacts: List[str] = []

        # Respect explicit hashcat output file if present and created
        if args.output_file and os.path.exists(args.output_file):
            artifacts.append(args.output_file)

        if args.pot_file and os.path.exists(args.pot_file):
            artifacts.append(args.pot_file)

        # Save stdout for auditing when meaningful
        if stdout and len(stdout) > 200:
            ts = int(timestamp or 0) or int(time.time())
            os.makedirs("artifacts", exist_ok=True)
            hash_type = args.hash_type.value if args.hash_type else "unknown"
            path = f"artifacts/hashcat_{hash_type}_{ts}.txt"
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(path)
            except OSError:
                pass

        return artifacts

    def run(self, args: HashcatArgs) -> ToolResult:
        """Execute hashcat password cracking.
        
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
                timeout=3600,  # password cracking can be very long-running
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                exit_code=-2,
                stdout="",
                stderr="Command timed out after 1 hour",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )
        except FileNotFoundError:
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="hashcat command not found. Ensure Hashcat is installed in the execution environment.",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr=f"Error executing hashcat: {str(e)}",
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
__all__ = ["HashcatTool", "HashcatArgs"]


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
        tool_id="password_attacks.offline_attacks.hashcat",
        display_name="Hashcat",
        category=ToolCategory.PASSWORD_ATTACKS,
        applicable_phases=[PentestPhase.EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="gpu_accelerated_cracking",
                description="GPU-accelerated offline hash cracking across 300+ algorithms (MD5, SHA, NTLM, WPA2, bcrypt) with dictionary, brute-force, hybrid, and rule attacks; returns plaintext passwords.",
                output_indicators=["Recovered", "Cracked", "Status"],
            ),
            ToolCapability(
                name="multi_hash_support",
                description="Support for 300+ hash algorithms (MD5, SHA, NTLM, WPA, etc.)",
                output_indicators=["Hash type", "Hash mode"],
            ),
            ToolCapability(
                name="attack_modes",
                description="Multiple attack modes: dictionary, brute-force, hybrid, rule-based",
                output_indicators=["Attack mode", "Speed"],
            ),
        ],
        required_services=[],
        target_protocols=[],
        execution_priority=9,
        parallel_compatible=True,
        stealth_level=1,
        estimated_runtime_minutes=30,
        best_combined_with=["password_attacks.offline_attacks.john"],
    )
)
