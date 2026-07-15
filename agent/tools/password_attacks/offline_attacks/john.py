"""
John the Ripper tool implementation.

This module provides an interface to John the Ripper,
a fast password cracker for various hash types.
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
    """Supported hash types for John the Ripper."""
    MD5 = "md5"
    SHA1 = "sha1"
    SHA256 = "sha256"
    SHA512 = "sha512"
    NTLM = "ntlm"
    LM = "lm"
    DES = "des"
    BSDI = "bsdi"
    MD5_CRYPT = "md5crypt"
    SHA256_CRYPT = "sha256crypt"
    SHA512_CRYPT = "sha512crypt"
    BLOWFISH = "blowfish"
    MYSQL = "mysql"
    MYSQL_SHA1 = "mysql-sha1"
    POSTGRESQL = "postgresql"
    ORACLE = "oracle"
    MSSQL = "mssql"
    LDAP = "ldap"
    RADIUS = "radius"
    KERBEROS = "kerberos"
    AFSCLIENT = "afsclient"
    AFSSERVER = "afsserver"
    LOKI = "loki"
    SKEY = "skey"
    SSH = "ssh"
    RAR = "rar"
    ZIP = "zip"
    PDF = "pdf"
    WORD = "word"
    EXCEL = "excel"
    BITCOIN = "bitcoin"
    ETHEREUM = "ethereum"
    WPA = "wpa"
    WPA2 = "wpa2"
    WPA3 = "wpa3"


class Mode(str, Enum):
    """John the Ripper modes."""
    WORDLIST = "wordlist"
    SINGLE = "single"
    INCREMENTAL = "incremental"
    EXTERNAL = "external"
    MASK = "mask"
    RULE = "rule"
    PRINCE = "prince"
    LOOPBACK = "loopback"


class OutputFormat(str, Enum):
    """Output format options."""
    RAW = "raw"
    JSON = "json"
    XML = "xml"
    CSV = "csv"


class JohnArgs(BaseToolArgs):
    """Arguments for the John the Ripper tool."""
    hash_file: Optional[str] = Field(None, description="File containing hashes to crack")
    hash_string: Optional[str] = Field(None, description="Single hash string to crack")
    hash_type: Optional[HashType] = Field(None, description="Type of hash to crack")
    wordlist: Optional[str] = Field(None, description="Wordlist file to use")
    wordlist_dir: Optional[str] = Field(None, description="Directory containing wordlists")
    mode: Optional[Mode] = Field(None, description="Cracking mode to use")
    output_format: OutputFormat = Field(OutputFormat.RAW, description="Output format")
    output_file: Optional[str] = Field(None, description="Output file path")
    log_file: Optional[str] = Field(None, description="Log file path")
    session: Optional[str] = Field(None, description="Session name")
    restore_file: Optional[str] = Field(None, description="Restore session from file")
    save_file: Optional[str] = Field(None, description="Save session to file")
    timeout: Optional[int] = Field(None, description="Timeout in seconds")
    max_runtime: Optional[int] = Field(None, description="Maximum runtime in seconds")
    max_memory: Optional[int] = Field(None, description="Maximum memory usage in MB")
    threads: Optional[int] = Field(None, description="Number of threads to use")
    fork: Optional[int] = Field(None, description="Number of processes to fork")
    node: Optional[str] = Field(None, description="Node specification")
    list: Optional[str] = Field(None, description="List format")
    show: Optional[str] = Field(None, description="Show cracked passwords")
    test: bool = Field(False, description="Run benchmark tests")
    benchmark: bool = Field(False, description="Run benchmark tests")
    make_charset: Optional[str] = Field(None, description="Make charset file")
    external: Optional[str] = Field(None, description="External mode name")
    mask: Optional[str] = Field(None, description="Mask for incremental mode")
    min_length: Optional[int] = Field(None, description="Minimum password length")
    max_length: Optional[int] = Field(None, description="Maximum password length")
    charset: Optional[str] = Field(None, description="Character set to use")
    rule: Optional[str] = Field(None, description="Rule to apply")
    rule_file: Optional[str] = Field(None, description="Rule file to use")
    prince_dir: Optional[str] = Field(None, description="Prince directory")
    prince_file: Optional[str] = Field(None, description="Prince file")
    loopback_file: Optional[str] = Field(None, description="Loopback file")
    pot_file: Optional[str] = Field(None, description="Pot file")
    config_file: Optional[str] = Field(None, description="Configuration file")
    verbose: bool = Field(False, description="Enable verbose output")
    debug: bool = Field(False, description="Enable debug output")
    quiet: bool = Field(False, description="Suppress output")
    help: bool = Field(False, description="Show help information")
    version: bool = Field(False, description="Show version information")
    list_formats: bool = Field(False, description="List available formats")
    list_modes: bool = Field(False, description="List available modes")
    list_external: bool = Field(False, description="List external modes")
    list_rules: bool = Field(False, description="List available rules")
    list_charsets: bool = Field(False, description="List available charsets")
    list_wordlists: bool = Field(False, description="List available wordlists")
    list_sessions: bool = Field(False, description="List available sessions")
    list_pot: bool = Field(False, description="List pot file entries")
    list_cracked: bool = Field(False, description="List cracked passwords")
    list_uncracked: bool = Field(False, description="List uncracked passwords")
    list_salts: bool = Field(False, description="List salts")
    list_users: bool = Field(False, description="List users")
    list_groups: bool = Field(False, description="List groups")
    list_hashes: bool = Field(False, description="List hashes")
    list_stats: bool = Field(False, description="List statistics")
    list_status: bool = Field(False, description="List status")
    list_progress: bool = Field(False, description="List progress")
    list_eta: bool = Field(False, description="List estimated time")
    list_speed: bool = Field(False, description="List speed")
    list_memory: bool = Field(False, description="List memory usage")
    list_cpu: bool = Field(False, description="List CPU usage")
    list_threads: bool = Field(False, description="List threads")
    list_processes: bool = Field(False, description="List processes")
    list_nodes: bool = Field(False, description="List nodes")
    list_log: bool = Field(False, description="List log file")
    list_config: bool = Field(False, description="List configuration")
    list_plugins: bool = Field(False, description="List plugins")


def parse_john_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """
    Parse the output from john command.

    Args:
        stdout: Standard output from john
        stderr: Standard error from john

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
            if "Loaded" in line and "password hashes" in line:
                result["crack_info"]["loaded_hashes"] = int(line.split()[1])
            elif "No password hashes loaded" in line:
                result["crack_info"]["loaded_hashes"] = 0
            elif "No password hashes left to crack" in line:
                result["crack_info"]["status"] = "completed"
            elif "Cracked" in line and "password hashes" in line:
                result["crack_info"]["cracked_hashes"] = int(line.split()[1])
            elif "Remaining" in line and "password hashes" in line:
                result["crack_info"]["remaining_hashes"] = int(line.split()[1])

            # Parse hash information
            elif "Hash type:" in line:
                result["hash_info"]["type"] = line.split(":", 1)[1].strip()
            elif "Hash format:" in line:
                result["hash_info"]["format"] = line.split(":", 1)[1].strip()
            elif "Hash algorithm:" in line:
                result["hash_info"]["algorithm"] = line.split(":", 1)[1].strip()

            # Parse cracked passwords
            elif ":" in line and len(line.split(":")) >= 2:
                parts = line.split(":")
                if len(parts) >= 2:
                    username = parts[0].strip()
                    password = parts[1].strip()
                    result["cracked_passwords"].append({
                        "username": username,
                        "password": password,
                        "hash_type": result["hash_info"].get("type", ""),
                        "hash_format": result["hash_info"].get("format", "")
                    })

            # Parse statistics
            elif "guesses:" in line:
                result["statistics"]["guesses"] = int(line.split()[0])
            elif "time:" in line:
                result["statistics"]["time"] = line.split(":", 1)[1].strip()
            elif "speed:" in line:
                result["statistics"]["speed"] = line.split(":", 1)[1].strip()
            elif "ETA:" in line:
                result["statistics"]["eta"] = line.split(":", 1)[1].strip()

            # Parse general information
            elif "John the Ripper" in line:
                result["general_info"]["version"] = line.split("John the Ripper")[1].split()[0]
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


class JohnTool(BaseTool):
    """John the Ripper tool implementation.
    
    Supports PTY execution via build_command(), parse_output(), and create_artifacts().
    """

    args_model = JohnArgs

    def build_command(self, args: JohnArgs) -> List[str]:
        """Build john command arguments.
        
        This method is used by both run() and PTY execution,
        ensuring consistent command construction.
        
        Args:
            args: Validated JohnArgs
            
        Returns:
            List of command arguments for john
        """
        cmd: List[str] = ["john"]

        # Verbosity/debug
        if args.verbose:
            cmd.append("--verbosity=5")
        if args.debug:
            cmd.append("--verb=debug")
        if args.quiet:
            cmd.append("--quiet")

        # Version/help (early return for info commands)
        if args.version:
            cmd.append("--version")
            return cmd
        if args.help:
            cmd.append("--help")
            return cmd

        # List commands (early return)
        if args.list_formats:
            cmd.append("--list=formats")
            return cmd
        if args.list:
            cmd.extend(["--list", args.list])
            return cmd

        # Hash format (IMPORTANT: Specify format to avoid prompts)
        if args.hash_type:
            cmd.extend(["--format", args.hash_type.value])

        # Mode selection
        if args.mode:
            if args.mode == Mode.WORDLIST:
                cmd.append("--wordlist=" + (args.wordlist or ""))
            elif args.mode == Mode.SINGLE:
                cmd.append("--single")
            elif args.mode == Mode.INCREMENTAL:
                cmd.append("--incremental")
            elif args.mode == Mode.EXTERNAL:
                if args.external:
                    cmd.extend(["--external", args.external])

        # Wordlist (dictionary attack)
        if args.wordlist and not args.mode:
            cmd.extend(["--wordlist", args.wordlist])

        # Rules
        if args.rule_file:
            cmd.extend(["--rules", args.rule_file])
        elif args.rule:
            cmd.extend(["--rules=" + args.rule])

        # Mask (for mask attack)
        if args.mask:
            cmd.extend(["--mask", args.mask])

        # Session management
        if args.session:
            cmd.extend(["--session", args.session])
        if args.restore_file:
            cmd.append("--restore=" + args.restore_file)

        # Performance options
        if args.fork:
            cmd.extend(["--fork", str(args.fork)])

        # Output options
        if args.pot_file:
            cmd.extend(["--pot", args.pot_file])
        if args.show:
            cmd.append("--show")

        # Test/benchmark
        if args.test:
            cmd.append("--test")
        if args.benchmark:
            cmd.append("--benchmark")

        # Hash file
        if args.hash_file:
            cmd.append(args.hash_file)

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: JohnArgs,
    ) -> Dict[str, Any]:
        """Parse john output into structured metadata.
        
        Reuses the standalone parse_john_output function for consistency.
        
        Args:
            stdout: Command stdout
            stderr: Command stderr
            exit_code: Command exit code
            args: Original JohnArgs
            
        Returns:
            Metadata dict with crack_info, hash_info, cracked_passwords, etc.
        """
        parsed = parse_john_output(stdout or "", stderr or "")
        parsed["exit_code"] = exit_code
        if args.hash_type:
            parsed.setdefault("hash_info", {})["format"] = args.hash_type.value
        if args.mode:
            parsed["mode"] = args.mode.value
        return parsed

    def create_artifacts(
        self,
        stdout: str,
        args: JohnArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create artifact files from john output.
        
        Args:
            stdout: Command stdout
            args: Original JohnArgs
            timestamp: Optional timestamp for artifact naming
            
        Returns:
            List of artifact file paths created
        """
        artifacts: List[str] = []

        # Respect explicit john pot file if present and created
        if args.pot_file and os.path.exists(args.pot_file):
            artifacts.append(args.pot_file)

        if args.output_file and os.path.exists(args.output_file):
            artifacts.append(args.output_file)

        if args.log_file and os.path.exists(args.log_file):
            artifacts.append(args.log_file)

        # Save stdout for auditing when meaningful
        if stdout and len(stdout) > 200:
            ts = int(timestamp or 0) or int(time.time())
            os.makedirs("artifacts", exist_ok=True)
            hash_type = args.hash_type.value if args.hash_type else "unknown"
            path = f"artifacts/john_{hash_type}_{ts}.txt"
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(path)
            except OSError:
                pass

        return artifacts

    def run(self, args: JohnArgs) -> ToolResult:
        """Execute john password cracking.
        
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
                stderr="john command not found. Ensure John the Ripper is installed in the execution environment.",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr=f"Error executing john: {str(e)}",
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
__all__ = ["JohnTool", "JohnArgs"]


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
        tool_id="password_attacks.offline_attacks.john",
        display_name="John the Ripper",
        category=ToolCategory.PASSWORD_ATTACKS,
        applicable_phases=[PentestPhase.EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="cpu_password_cracking",
                description="Crack password hashes (MD5, SHA, NTLM, DES, bcrypt, WPA) offline with wordlist, incremental, or rule modes; returns plaintext passwords; not for online services.",
                output_indicators=["Loaded", "Cracked", "guesses"],
            ),
            ToolCapability(
                name="multi_format_support",
                description="Support for 100+ hash formats including Unix, Windows, databases, archives",
                output_indicators=["Hash type", "Hash format"],
            ),
            ToolCapability(
                name="flexible_attack_modes",
                description="Multiple attack modes: wordlist, incremental, single-crack, rule-based, external",
                output_indicators=["mode", "speed"],
            ),
        ],
        required_services=[],
        target_protocols=[],
        execution_priority=8,
        parallel_compatible=True,
        stealth_level=1,
        estimated_runtime_minutes=30,
        best_combined_with=["password_attacks.offline_attacks.hashcat"],
    )
)
