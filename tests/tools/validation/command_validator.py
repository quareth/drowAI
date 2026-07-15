"""Command correctness validator for penetration testing tools.

This module validates that generated commands are syntactically correct
and follow the expected patterns for each tool's CLI interface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from pydantic import BaseModel

from agent.tools.base_tool import BaseTool


@dataclass
class CommandPattern:
    """Defines expected command patterns for a tool."""
    
    tool_name: str
    binary_name: str
    # Required flags that must always be present
    required_flags: List[str] = field(default_factory=list)
    # Flag patterns: maps flag to expected value pattern (regex)
    flag_patterns: Dict[str, str] = field(default_factory=dict)
    # Mutually exclusive flag groups: list of sets of flags that can't coexist
    mutually_exclusive: List[Set[str]] = field(default_factory=list)
    # Flags that require other flags to be present
    dependent_flags: Dict[str, List[str]] = field(default_factory=dict)
    # Valid flag prefixes for this tool
    valid_prefixes: List[str] = field(default_factory=lambda: ["-", "--"])
    # Position-sensitive arguments (e.g., target must be last)
    positional_args: Dict[str, int] = field(default_factory=dict)  # arg_name -> position (-1 for last)
    # Flags that take no value (boolean flags)
    boolean_flags: Set[str] = field(default_factory=set)


@dataclass
class CommandValidationResult:
    """Result of command validation."""
    
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    
    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.valid = False
    
    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)


# Known command patterns for tools
COMMAND_PATTERNS: Dict[str, CommandPattern] = {
    "nmap": CommandPattern(
        tool_name="nmap",
        binary_name="nmap",
        flag_patterns={
            "-p": r"^[\d,\-]+$|^-$",  # port spec
            "-sS": r"^$",  # SYN scan (no value)
            "-sT": r"^$",  # TCP connect scan
            "-sU": r"^$",  # UDP scan
            "-sV": r"^$",  # Version detection
            "-O": r"^$",   # OS detection
            "-A": r"^$",   # Aggressive scan
            "-T": r"^[0-5]$",  # Timing template
            "-oX": r".+",  # XML output file
            "-oN": r".+",  # Normal output file
            "-oG": r".+",  # Grepable output
            "--top-ports": r"^\d+$",
            "--script": r".+",
        },
        mutually_exclusive=[
            {"-sS", "-sT"},  # Can't do SYN and TCP connect at same time
        ],
        boolean_flags={"-sS", "-sT", "-sU", "-sV", "-O", "-A", "-v", "-Pn", "--open"},
        positional_args={"target": -1},
    ),
    "hydra": CommandPattern(
        tool_name="hydra",
        binary_name="hydra",
        required_flags=["-l", "-L", "-p", "-P"],  # At least one of each pair
        flag_patterns={
            "-l": r".+",  # single login
            "-L": r".+",  # login file
            "-p": r".+",  # single password
            "-P": r".+",  # password file
            "-s": r"^\d+$",  # port
            "-t": r"^\d+$",  # tasks
            "-w": r"^\d+$",  # wait time
            "-o": r".+",  # output file
        },
        mutually_exclusive=[
            {"-l", "-L"},  # Single login vs login file
            {"-p", "-P"},  # Single password vs password file
        ],
        boolean_flags={"-f", "-F", "-v", "-V", "-d"},
    ),
    "nikto": CommandPattern(
        tool_name="nikto",
        binary_name="nikto",
        required_flags=["-h"],
        flag_patterns={
            "-h": r".+",  # host
            "-p": r"^\d+$",  # port
            "-ssl": r"^$",  # use SSL
            "-o": r".+",  # output file
            "-Format": r"^(htm|csv|txt|xml)$",
        },
        boolean_flags={"-ssl", "-nossl", "-nolookup"},
    ),
    "gobuster": CommandPattern(
        tool_name="gobuster",
        binary_name="gobuster",
        flag_patterns={
            "-u": r"^https?://.+",  # URL
            "-w": r".+",  # wordlist
            "-t": r"^\d+$",  # threads
            "-o": r".+",  # output
            "-x": r"^[\w,\.]+$",  # extensions
            "-s": r"^[\d,]+$",  # status codes
        },
        boolean_flags={"-q", "-n", "-e", "-r", "-k"},
    ),
    "sqlmap": CommandPattern(
        tool_name="sqlmap",
        binary_name="sqlmap",
        flag_patterns={
            "-u": r"^https?://.+",  # URL
            "--url": r"^https?://.+",
            "-p": r"^\w+$",  # parameter
            "--data": r".+",  # POST data
            "--cookie": r".+",
            "--level": r"^[1-5]$",
            "--risk": r"^[1-3]$",
            "--threads": r"^\d+$",
            "-o": r".+",
            "--batch": r"^$",
        },
        boolean_flags={"--batch", "--dbs", "--tables", "--dump", "-v"},
    ),
    "tnscmd10g": CommandPattern(
        tool_name="tnscmd10g",
        binary_name="tnscmd10g",
        flag_patterns={
            "-h": r".+",  # host
            "-p": r"^\d+$",  # port
        },
    ),
    "oscanner": CommandPattern(
        tool_name="oscanner",
        binary_name="oscanner",
        required_flags=["-s"],
        flag_patterns={
            "-s": r".+",  # server
            "-p": r"^\d+$",  # port
            "-r": r".+",  # report file
        },
        boolean_flags={"-v"},
    ),
    "sidguesser": CommandPattern(
        tool_name="sidguesser",
        binary_name="sidguesser",
        required_flags=["-i"],
        flag_patterns={
            "-i": r".+",  # host
            "-p": r"^\d+$",  # port
            "-d": r".+",  # dictionary
        },
    ),
    "ffuf": CommandPattern(
        tool_name="ffuf",
        binary_name="ffuf",
        required_flags=["-u", "-w"],
        flag_patterns={
            "-u": r"^https?://.+FUZZ.*",  # URL with FUZZ keyword
            "-w": r".+",  # wordlist
            "-t": r"^\d+$",  # threads
            "-mc": r"^[\d,]+$|^all$",  # match codes
            "-fc": r"^[\d,]+$",  # filter codes
            "-o": r".+",  # output
            "-of": r"^(json|csv|html|md)$",  # output format
        },
        boolean_flags={"-v", "-s", "-r", "-ac"},
    ),
    "web_applications.web_crawlers.ffuf": CommandPattern(
        tool_name="web_applications.web_crawlers.ffuf",
        binary_name="ffuf",
        required_flags=["-u", "-w"],
        flag_patterns={
            "-u": r"^https?://.+/FUZZ/?$",
            "-w": r".+",
            "-t": r"^\d+$",
            "-recursion-depth": r"^\d+$",
            "-recursion-strategy": r"^(default|greedy)$",
            "-mc": r"^[\d,]+$|^all$",
            "-fc": r"^[\d,]+$",
            "-o": r".+",
            "-of": r"^(json|csv|html|md)$",
        },
        boolean_flags={"-v", "-s", "-r", "-ac", "-ach", "-recursion", "-D", "-k"},
    ),
    "web_applications.web_application_fuzzers.ffuf": CommandPattern(
        tool_name="web_applications.web_application_fuzzers.ffuf",
        binary_name="ffuf",
        required_flags=["-u"],
        flag_patterns={
            "-u": r"^https?://.+(?:FUZZ|[A-Z][A-Z0-9_]{1,}).*",
            "-w": r".+",
            "-input-cmd": r".+",
            "-input-num": r"^\d+$",
            "-t": r"^\d+$",
            "-mc": r"^[\d,]+$|^all$",
            "-fc": r"^[\d,]+$",
            "-mode": r"^(clusterbomb|pitchfork|sniper)$",
            "-o": r".+",
            "-of": r"^(json|csv|html|md)$",
        },
        boolean_flags={"-v", "-s", "-r", "-ac", "-ach", "-k"},
    ),
    "amass": CommandPattern(
        tool_name="amass",
        binary_name="amass",
        flag_patterns={
            "-d": r"^[\w\.\-]+$",  # domain
            "-o": r".+",  # output file
            "-json": r".+",  # JSON output
            "-timeout": r"^\d+$",
            "-max-depth": r"^\d+$",
        },
        boolean_flags={"-passive", "-active", "-v", "-ip"},
    ),
    "masscan": CommandPattern(
        tool_name="masscan",
        binary_name="masscan",
        flag_patterns={
            "-p": r"^(?:[TU]:)?\d{1,5}(?:-\d{1,5})?(?:,(?:[TU]:)?\d{1,5}(?:-\d{1,5})?)*$",
            "--rate": r"^\d+$",
            "--max-rate": r"^\d+$",
            "--retries": r"^\d+$",
            "--max-retries": r"^\d+$",
            "--wait": r"^\d+$",
            "-e": r".+",
            "--adapter": r".+",
            "--adapter-ip": r".+",
            "--source-ip": r".+",
            "-iL": r".+",
            "--includefile": r".+",
            "--include-file": r".+",
            "--exclude": r".+",
            "--excludefile": r".+",
            "--exclude-file": r".+",
            "-oX": r"^$|^-$|.+",
            "-oJ": r"^$|^-$|.+",
            "-oL": r"^$|^-$|.+",
            "-oB": r"^$|^-$|.+",
            "-oG": r"^$|^-$|.+",
        },
        boolean_flags={"--ping", "--no-ping", "--banners", "--open-only"},
        positional_args={"target": -1},
    ),
    "binwalk": CommandPattern(
        tool_name="binwalk",
        binary_name="binwalk",
        flag_patterns={
            "-C": r".+",
        },
        boolean_flags={"-e", "-E", "-M", "-v", "-q"},
        positional_args={"target": -1},
    ),
    "sleuthkit": CommandPattern(
        tool_name="sleuthkit",
        binary_name="mmls",
        flag_patterns={
            "-f": r"^(fat12|fat16|fat32|ntfs|ext2|ext3|ext4|hfs|hfs\+|iso9660|ufs)$",
            "-i": r"^(raw|aff|ewf|vmdk|vhd|vhdx)$",
            "-o": r"^\d+$",
            "-m": r".+",
        },
        boolean_flags={"-r"},
    ),
    "volatility": CommandPattern(
        tool_name="volatility",
        binary_name="volatility3",
        flag_patterns={
            "-f": r".+",
            "-r": r"^(json|csv|text|table|pretty)$",
            "--profile": r".+",
            "--output": r".+",
            "--output-file": r".+",
            "-o": r".+",
        },
        boolean_flags={"-v", "-q"},
    ),
    "foremost": CommandPattern(
        tool_name="foremost",
        binary_name="foremost",
        flag_patterns={
            "-i": r".+",
            "-o": r".+",
            "-t": r".+",
            "-c": r".+",
        },
        boolean_flags={"-Q", "-T", "-v", "-q"},
    ),
    "bulk_extractor": CommandPattern(
        tool_name="bulk_extractor",
        binary_name="bulk_extractor",
        flag_patterns={
            "-o": r".+",
            "-e": r".+",
            "-x": r".+",
            "-j": r"^\d+$",
        },
        boolean_flags={"-v", "-q"},
    ),
    "hashdeep": CommandPattern(
        tool_name="hashdeep",
        binary_name="sha256deep",
        flag_patterns={
            "-r": r"^$",
        },
        boolean_flags={"-r", "-v", "-q"},
    ),
    "chkrootkit": CommandPattern(
        tool_name="chkrootkit",
        binary_name="chkrootkit",
        flag_patterns={
            "-r": r".+",
            "-p": r".+",
        },
        boolean_flags={"-x", "-q"},
    ),
    "scalpel": CommandPattern(
        tool_name="scalpel",
        binary_name="scalpel",
        flag_patterns={
            "-c": r".+",
            "-b": r"^\d+$",
            "-o": r".+",
        },
        boolean_flags={"-v", "-q"},
    ),
    "ddrescue": CommandPattern(
        tool_name="ddrescue",
        binary_name="ddrescue",
        flag_patterns={
            "-r": r"^\d+$",
        },
        boolean_flags={"-n", "-R", "-v"},
    ),
    "safecopy": CommandPattern(
        tool_name="safecopy",
        binary_name="safecopy",
        flag_patterns={
            "--log": r".+",
        },
        boolean_flags={"--stage1", "--stage2", "--stage3", "-v"},
    ),
    "objdump": CommandPattern(
        tool_name="objdump",
        binary_name="objdump",
        flag_patterns={
            "-M": r"^(intel|att)$",
            "--start-address": r"^0x[0-9a-fA-F]+$|^\d+$",
            "--stop-address": r"^0x[0-9a-fA-F]+$|^\d+$",
            "-j": r".+",
            "-m": r".+",
            "--disassemble-symbols": r".+",
        },
        boolean_flags={"-d", "-h", "-s", "-t", "-r", "-T", "-f", "-a", "-g", "-C", "-l", "-S", "--wide", "--full-contents", "--show-raw-insn", "-v"},
        positional_args={"target": -1},
    ),
    "radare2": CommandPattern(
        tool_name="radare2",
        binary_name="r2",
        flag_patterns={
            "-c": r".+",
            "-a": r".+",
            "-b": r"^\d+$",
            "-e": r".+",
            "-i": r".+",
        },
        boolean_flags={"-q", "-v"},
        positional_args={"target": -1},
    ),
    "gdb": CommandPattern(
        tool_name="gdb",
        binary_name="gdb",
        flag_patterns={
            "-ex": r".+",
            "-x": r".+",
        },
        boolean_flags={"--batch", "--quiet"},
        positional_args={"target": -1},
    ),
    "theharvester": CommandPattern(
        tool_name="theharvester",
        binary_name="theHarvester",
        required_flags=["-d"],
        flag_patterns={
            "-d": r"^[\w\.\-]+$",  # domain
            "-b": r"^\w+$",  # data source
            "-l": r"^\d+$",  # limit
            "-f": r".+",  # output file
        },
        boolean_flags={"-v", "-n", "-c"},
    ),
    "wpscan": CommandPattern(
        tool_name="wpscan",
        binary_name="wpscan",
        required_flags=["--url"],
        flag_patterns={
            "--url": r"^https?://.+",
            "-e": r"^[\w,]+$",  # enumerate
            "--enumerate": r"^[\w,]+$",
            "-o": r".+",
            "-f": r"^(cli|json|cli-no-color)$",
            "--api-token": r".+",
        },
        boolean_flags={"--stealthy", "--verbose", "--no-update"},
    ),
    "crunch": CommandPattern(
        tool_name="crunch",
        binary_name="crunch",
        flag_patterns={
            "-o": r".+",  # output file
            "-t": r".+",  # pattern
            "-s": r".+",  # start string
            "-e": r".+",  # end string
            "-b": r"^\d+[kmg]b$",  # file size
        },
        positional_args={"min_length": 0, "max_length": 1},
    ),
    "read_file": CommandPattern(
        tool_name="read_file",
        binary_name="cat",
        flag_patterns={
            "-n": r"^\d+$",
        },
        positional_args={"path": -1},
    ),
    "write_file": CommandPattern(
        tool_name="write_file",
        binary_name="bash",
    ),
    "append_file": CommandPattern(
        tool_name="append_file",
        binary_name="bash",
    ),
    "delete_path": CommandPattern(
        tool_name="delete_path",
        binary_name="rm",
        boolean_flags={"-r", "-f"},
        positional_args={"path": -1},
    ),
    "make_dir": CommandPattern(
        tool_name="make_dir",
        binary_name="mkdir",
        boolean_flags={"-p"},
        positional_args={"path": -1},
    ),
    "list_dir": CommandPattern(
        tool_name="list_dir",
        binary_name="ls",
        boolean_flags={"-l", "-a", "-la"},
        positional_args={"path": -1},
    ),
    "move_path": CommandPattern(
        tool_name="move_path",
        binary_name="mv",
        positional_args={"src": 0, "dest": 1},
    ),
    "copy_path": CommandPattern(
        tool_name="copy_path",
        binary_name="cp",
        boolean_flags={"-r"},
        positional_args={"src": 0, "dest": 1},
    ),
    "stat_path": CommandPattern(
        tool_name="stat_path",
        binary_name="stat",
        positional_args={"path": -1},
    ),
    "find_paths": CommandPattern(
        tool_name="find_paths",
        binary_name="find",
        flag_patterns={
            "-name": r".+",
            "-maxdepth": r"^\d+$",
        },
        positional_args={"path": 0},
    ),
    "search_text": CommandPattern(
        tool_name="search_text",
        binary_name="grep",
        boolean_flags={"-n", "-r", "-i", "-F"},
    ),
    "cymothoa": CommandPattern(
        tool_name="cymothoa",
        binary_name="cymothoa",
        required_flags=["-p", "-s"],
        flag_patterns={
            "-p": r"^\d+$",
            "-s": r"^\d+$",
            "-y": r"^\d+$",
        },
        boolean_flags={"-S"},
    ),
    "weevely": CommandPattern(
        tool_name="weevely",
        binary_name="weevely",
    ),
    "proxychains": CommandPattern(
        tool_name="proxychains",
        binary_name="proxychains4",
        boolean_flags={"-q"},
    ),
    "dns2tcp": CommandPattern(
        tool_name="dns2tcp",
        binary_name="dns2tcpc",
        flag_patterns={
            "-z": r"^[\w\.\-]+$",
            "-k": r".+",
            "-r": r".+",
            "-l": r"^\d+$",
        },
    ),
    "iodine": CommandPattern(
        tool_name="iodine",
        binary_name="iodine",
        flag_patterns={
            "-P": r".+",
            "-m": r"^\d+$",
            "-M": r"^\d+$",
        },
        boolean_flags={"-f"},
    ),
    "proxytunnel": CommandPattern(
        tool_name="proxytunnel",
        binary_name="proxytunnel",
        required_flags=["-p", "-d"],
        flag_patterns={
            "-p": r"^[^:]+:\d+$",
            "-d": r"^[^:]+:\d+$",
            "-a": r"^\d+$",
            "-P": r"^[^:]+:.+$",
        },
    ),
    "ptunnel": CommandPattern(
        tool_name="ptunnel",
        binary_name="ptunnel-ng",
        flag_patterns={
            "-p": r".+",
            "-l": r"^\d+$",
            "-r": r".+",
            "-R": r"^\d+$",
        },
        boolean_flags={"-s"},
    ),
    "tshark": CommandPattern(
        tool_name="tshark",
        binary_name="tshark",
        flag_patterns={
            "-i": r".+",
            "-r": r".+",
            "-T": r".+",
            "-Y": r".+",
            "-f": r".+",
            "-s": r"^\d+$",
            "-c": r"^\d+$",
            "-a": r"^duration:\d+$",
            "-w": r".+",
            "-e": r".+",
        },
        boolean_flags={"-V"},
    ),
    "tcpdump": CommandPattern(
        tool_name="tcpdump",
        binary_name="tcpdump",
        flag_patterns={
            "-i": r".+",
            "-c": r"^\d+$",
            "-G": r"^\d+$",
            "-s": r"^\d+$",
            "-w": r".+",
        },
        boolean_flags={"-q", "-v", "-vv", "-vvv", "-nn", "-tttt", "-l", "-A"},
    ),
    "netsniff_ng": CommandPattern(
        tool_name="netsniff_ng",
        binary_name="netsniff-ng",
        flag_patterns={
            "-i": r".+",
            "-r": r".+",
            "-o": r".+",
            "-c": r"^\d+$",
            "-t": r"^\d+$",
            "-f": r".+",
            "-s": r"^\d+$",
            "-B": r"^\d+$",
        },
        boolean_flags={"-v"},
    ),
    "dsniff": CommandPattern(
        tool_name="dsniff",
        binary_name="dsniff",
        flag_patterns={
            "-i": r".+",
            "-p": r".+",
            "-f": r".+",
            "-w": r".+",
        },
        boolean_flags={"-v"},
    ),
    "arpspoof": CommandPattern(
        tool_name="arpspoof",
        binary_name="arpspoof",
        flag_patterns={
            "-i": r".+",
            "-c": r"^(own|host|both)$",
            "-t": r".+",
        },
        boolean_flags={"-r", "-v"},
    ),
    "bettercap": CommandPattern(
        tool_name="bettercap",
        binary_name="bettercap",
        flag_patterns={
            "-iface": r".+",
            "-caplet": r".+",
            "-eval": r".+",
            "-gateway-override": r".+",
        },
        boolean_flags={"-silent", "-no-colors", "-debug"},
    ),
    "dnsspoof": CommandPattern(
        tool_name="dnsspoof",
        binary_name="dnsspoof",
        flag_patterns={
            "-i": r".+",
            "-f": r".+",
        },
        boolean_flags={"-v"},
    ),
    "ettercap": CommandPattern(
        tool_name="ettercap",
        binary_name="ettercap",
        flag_patterns={
            "-i": r".+",
            "-M": r".+",
            "-P": r".+",
            "-w": r".+",
            "-L": r".+",
        },
        boolean_flags={"-T", "-q", "-o"},
    ),
    "responder": CommandPattern(
        tool_name="responder",
        binary_name="responder",
        flag_patterns={
            "-I": r".+",
        },
        boolean_flags={"-A", "-w", "-r", "-d", "-f", "-v"},
    ),
    "zaproxy": CommandPattern(
        tool_name="zaproxy",
        binary_name="zap-baseline.py",
        flag_patterns={
            "-t": r"^https?://.+",
            "-r": r".+",
            "-J": r".+",
            "-x": r".+",
            "-w": r".+",
            "-g": r".+",
            "-c": r".+",
        },
        boolean_flags={"-j", "-v"},
    ),
}


class CommandValidator:
    """Validates generated commands against known patterns."""
    
    def __init__(self, patterns: Optional[Dict[str, CommandPattern]] = None):
        self.patterns = patterns or COMMAND_PATTERNS
    
    def validate_command(
        self,
        command: List[str],
        tool_id: str,
    ) -> CommandValidationResult:
        """Validate a command against known patterns.
        
        Args:
            command: The command as a list of strings
            tool_id: The tool identifier (e.g., "information_gathering.dns.nmap")
            
        Returns:
            CommandValidationResult with validation status and any errors/warnings
        """
        result = CommandValidationResult(valid=True)
        
        if not command:
            result.add_error("Command is empty")
            return result

        command_to_validate = self._unwrap_timeout_wrapper(command, result)
        if not result.valid:
            return result
        
        pattern = self.patterns.get(tool_id)
        tool_name = tool_id.split(".")[-1]
        if pattern is None:
            pattern = self.patterns.get(tool_name)
        
        if not pattern:
            result.add_warning(f"No command pattern defined for tool: {tool_name}")
            # Still do basic validation
            return self._validate_basic(command_to_validate, result)
        
        # Validate binary name
        if command_to_validate[0] != pattern.binary_name:
            result.add_error(
                f"Expected binary '{pattern.binary_name}', got '{command_to_validate[0]}'"
            )
        
        # Parse command into flags and values
        flags, positional = self._parse_command(command_to_validate[1:], pattern)
        
        # Check required flags
        self._validate_required_flags(flags, pattern, result)
        
        # Check flag patterns
        self._validate_flag_patterns(flags, pattern, result)
        
        # Check mutual exclusivity
        self._validate_mutual_exclusivity(flags, pattern, result)
        
        # Check dependent flags
        self._validate_dependent_flags(flags, pattern, result)
        
        # Validate positional arguments
        self._validate_positional_args(positional, command_to_validate, pattern, result)
        
        return result

    def _unwrap_timeout_wrapper(
        self,
        command: List[str],
        result: CommandValidationResult,
    ) -> List[str]:
        """Return the wrapped command when using `timeout <duration> <binary>`."""
        if command[0] != "timeout":
            return command
        if len(command) < 3:
            result.add_error("timeout wrapper requires duration and wrapped command")
            return command
        duration = command[1]
        if not re.match(r"^\d+(?:\.\d+)?[smhd]?$", duration):
            result.add_error(f"Invalid timeout duration: {duration}")
            return command
        return command[2:]
    
    def _parse_command(
        self,
        args: List[str],
        pattern: CommandPattern,
    ) -> Tuple[Dict[str, str], List[str]]:
        """Parse command arguments into flags and positional args."""
        flags: Dict[str, str] = {}
        positional: List[str] = []
        
        i = 0
        while i < len(args):
            arg = args[i]
            
            if arg.startswith("-"):
                # Handle flags
                if "=" in arg:
                    # Flag with value: --flag=value
                    flag, value = arg.split("=", 1)
                    flags[flag] = value
                elif arg in pattern.boolean_flags:
                    # Boolean flag (no value)
                    flags[arg] = ""
                elif i + 1 < len(args) and not args[i + 1].startswith("-"):
                    # Flag with separate value: -f value
                    flags[arg] = args[i + 1]
                    i += 1
                else:
                    # Assume boolean flag
                    flags[arg] = ""
            else:
                positional.append(arg)
            
            i += 1
        
        return flags, positional
    
    def _validate_basic(
        self,
        command: List[str],
        result: CommandValidationResult,
    ) -> CommandValidationResult:
        """Basic validation for tools without defined patterns."""
        # Check that command has at least a binary name
        if not command[0]:
            result.add_error("Binary name is empty")
        
        # Check for obvious issues
        for i, arg in enumerate(command):
            if arg is None:
                result.add_error(f"Argument at position {i} is None")
            elif not isinstance(arg, str):
                result.add_error(f"Argument at position {i} is not a string: {type(arg)}")
        
        return result
    
    def _validate_required_flags(
        self,
        flags: Dict[str, str],
        pattern: CommandPattern,
        result: CommandValidationResult,
    ) -> None:
        """Validate that required flags are present."""
        for required in pattern.required_flags:
            if required not in flags:
                # Check if it's part of a mutually exclusive group
                in_exclusive_group = False
                for group in pattern.mutually_exclusive:
                    if required in group and any(f in flags for f in group):
                        in_exclusive_group = True
                        break
                
                if not in_exclusive_group:
                    result.add_warning(f"Required flag '{required}' not found")
    
    def _validate_flag_patterns(
        self,
        flags: Dict[str, str],
        pattern: CommandPattern,
        result: CommandValidationResult,
    ) -> None:
        """Validate flag values against expected patterns."""
        for flag, value in flags.items():
            if flag in pattern.flag_patterns:
                expected_pattern = pattern.flag_patterns[flag]
                if not re.match(expected_pattern, value):
                    result.add_error(
                        f"Flag '{flag}' value '{value}' doesn't match pattern '{expected_pattern}'"
                    )
    
    def _validate_mutual_exclusivity(
        self,
        flags: Dict[str, str],
        pattern: CommandPattern,
        result: CommandValidationResult,
    ) -> None:
        """Validate that mutually exclusive flags aren't used together."""
        for group in pattern.mutually_exclusive:
            present = [f for f in group if f in flags]
            if len(present) > 1:
                result.add_error(
                    f"Mutually exclusive flags used together: {present}"
                )
    
    def _validate_dependent_flags(
        self,
        flags: Dict[str, str],
        pattern: CommandPattern,
        result: CommandValidationResult,
    ) -> None:
        """Validate that dependent flags have their dependencies."""
        for flag, dependencies in pattern.dependent_flags.items():
            if flag in flags:
                missing = [d for d in dependencies if d not in flags]
                if missing:
                    result.add_error(
                        f"Flag '{flag}' requires flags {missing}"
                    )
    
    def _validate_positional_args(
        self,
        positional: List[str],
        command: List[str],
        pattern: CommandPattern,
        result: CommandValidationResult,
    ) -> None:
        """Validate positional arguments."""
        for arg_name, expected_pos in pattern.positional_args.items():
            if expected_pos == -1:
                # Should be last
                if positional and positional[-1] != command[-1]:
                    result.add_warning(
                        f"Expected '{arg_name}' to be last argument"
                    )


def get_command_pattern(tool_id: str) -> Optional[CommandPattern]:
    """Get the command pattern for a tool."""
    return COMMAND_PATTERNS.get(tool_id) or COMMAND_PATTERNS.get(tool_id.split(".")[-1])


def validate_tool_command(
    tool: BaseTool,
    args: BaseModel,
    tool_id: str,
) -> CommandValidationResult:
    """Validate a tool's generated command.
    
    Args:
        tool: The tool instance
        args: The validated arguments
        tool_id: The tool identifier
        
    Returns:
        CommandValidationResult with validation status
    """
    validator = CommandValidator()
    
    try:
        command = tool.build_command(args)
    except NotImplementedError:
        return CommandValidationResult(
            valid=False,
            errors=["build_command() not implemented"]
        )
    except Exception as e:
        return CommandValidationResult(
            valid=False,
            errors=[f"build_command() raised exception: {e}"]
        )
    
    return validator.validate_command(command, tool_id)
