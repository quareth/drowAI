"""Command policy enforcement for shell execution tools."""

from __future__ import annotations

import fnmatch
import os
import shlex
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional
from pydantic import ValidationError


class PolicyEnforcement(str, Enum):
    """Policy enforcement levels."""

    STRICT = "strict"  # Block all non-allowlisted commands
    PERMISSIVE = "permissive"  # Warn but allow non-allowlisted commands
    DISABLED = "disabled"  # No policy checks (unsafe, testing only)


@dataclass
class PolicyResult:
    """Result of policy validation."""

    allowed: bool
    reason: Optional[str] = None
    matched_pattern: Optional[str] = None
    severity: str = "info"  # "info", "warning", "error"


class CommandPolicy:
    """Validates shell commands against allowlist and denylist rules."""

    def __init__(self, enforcement: Optional[PolicyEnforcement] = None):
        """Initialize policy with enforcement level from env or default to PERMISSIVE for pentesting."""
        if enforcement is None:
            env_enforcement = os.getenv("SHELL_POLICY_ENFORCEMENT", "permissive").lower()
            try:
                enforcement = PolicyEnforcement(env_enforcement)
            except ValueError:
                enforcement = PolicyEnforcement.PERMISSIVE

        self.enforcement = enforcement
        self._allowlist_patterns = self._build_allowlist()
        self._denylist_patterns = self._build_denylist()

    def validate(self, command: str) -> PolicyResult:
        """Validate a command against policy rules."""
        if self.enforcement == PolicyEnforcement.DISABLED:
            return PolicyResult(allowed=True, reason="Policy enforcement disabled")

        normalized = command.strip()
        if not normalized:
            return PolicyResult(
                allowed=False,
                reason="Empty command",
                severity="error",
            )

        # Check denylist first (hard block regardless of enforcement level)
        denylist_match = self._check_denylist(normalized)
        if denylist_match:
            return PolicyResult(
                allowed=False,
                reason=f"Command matches denylist pattern: {denylist_match}",
                matched_pattern=denylist_match,
                severity="error",
            )

        # Check allowlist
        allowlist_match = self._check_allowlist(normalized)
        if allowlist_match:
            return PolicyResult(
                allowed=True,
                reason="Command matches allowlist",
                matched_pattern=allowlist_match,
                severity="info",
            )

        # Not in allowlist
        if self.enforcement == PolicyEnforcement.STRICT:
            return PolicyResult(
                allowed=False,
                reason="Command not in allowlist (strict mode)",
                severity="error",
            )
        else:  # PERMISSIVE
            return PolicyResult(
                allowed=True,
                reason="Command not in allowlist but permissive mode enabled",
                severity="warning",
            )

    def _check_allowlist(self, command: str) -> Optional[str]:
        """Check if command matches any allowlist pattern."""
        for pattern in self._allowlist_patterns:
            if self._matches_pattern(command, pattern):
                return pattern
        return None

    def _check_denylist(self, command: str) -> Optional[str]:
        """Check if command matches any denylist pattern."""
        for pattern in self._denylist_patterns:
            if self._matches_pattern(command, pattern):
                return pattern
        return None

    def _matches_pattern(self, command: str, pattern: str) -> bool:
        """Check if command matches a pattern (supports wildcards and regex)."""
        # Exact match
        if command == pattern:
            return True

        # Glob-style wildcard match
        if "*" in pattern:
            return fnmatch.fnmatch(command, pattern)

        # Prefix match for simple patterns
        if pattern.endswith(" "):
            return command.startswith(pattern.rstrip())

        # Check if command starts with pattern followed by space or end
        if command == pattern or command.startswith(pattern + " "):
            return True

        return False

    def _build_allowlist(self) -> List[str]:
        """
        Build allowlist of common safe command patterns.
        
        In PERMISSIVE mode (default), this is advisory - unlisted commands will warn but still execute.
        In STRICT mode, only these commands are allowed.
        
        Note: Pentesting tools (nmap, metasploit, etc.) should use structured tool implementations
        rather than raw shell execution. This allowlist focuses on core utilities and troubleshooting.
        """
        return [
            # Package managers (for installing pentesting tools)
            "apt-get update",
            "apt-get install *",
            "apt-get upgrade *",
            "apt-cache search *",
            "apt-cache show *",
            "pip install *",
            "pip uninstall *",
            "pip list",
            "pip show *",
            "pip freeze",
            # File permissions (for making exploits/scripts executable)
            "chmod +x *",
            "chmod u+x *",
            "chmod 755 *",
            "chmod 644 *",
            "chmod 600 *",
            # Service management (for starting/stopping target services)
            "service * start",
            "service * stop",
            "service * restart",
            "service * status",
            "systemctl status *",
            "systemctl start *",
            "systemctl stop *",
            "systemctl restart *",
            # Core utilities (essential for navigation and inspection)
            "echo *",
            "cat *",
            "grep *",
            "egrep *",
            "fgrep *",
            "find *",
            "locate *",
            "ls",
            "ls *",
            "pwd",
            "which *",
            "whereis *",
            "whoami",
            "id",
            "id *",
            "date",
            "uptime",
            "uname",
            "uname *",
            "df",
            "df *",
            "du *",
            "head *",
            "tail *",
            "wc *",
            "sort *",
            "uniq *",
            "cut *",
            "awk *",
            "sed *",
            "tr *",
            "tee *",
            "xargs *",
            # Archive/compression (for handling payloads and exploits)
            "tar -xzf *",
            "tar -xvf *",
            "tar -czf *",
            "tar -xf *",
            "tar -tf *",
            "unzip *",
            "zip *",
            "gzip *",
            "gunzip *",
            "bzip2 *",
            "bunzip2 *",
            # Network reconnaissance
            "ping *",
            "ping6 *",
            "traceroute *",
            "nslookup *",
            "dig *",
            "host *",
            "whois *",
            "netstat",
            "netstat *",
            "ss",
            "ss *",
            "ip addr",
            "ip route",
            "ip link",
            "ifconfig",
            "ifconfig *",
            "arp",
            "arp *",
            "route",
            "route *",
            # Network utilities (for downloading tools/payloads)
            "curl *",
            "wget *",
            "python *",
            "python3 *",
            "python2 *",
            # Bash/shell scripting
            "bash *",
            "sh *",
            # Process management (for troubleshooting)
            "ps",
            "ps *",
            "top",
            "htop",
            "kill *",
            "killall *",
            "pkill *",
            # Environment
            "export *",
            "env",
            "printenv",
            "printenv *",
            # Troubleshooting
            "strace *",
            "ltrace *",
            "lsof *",
            "dmesg",
            "dmesg *",
        ]

    def _build_denylist(self) -> List[str]:
        """Build denylist of explicitly forbidden command patterns."""
        return [
            # Destructive filesystem operations
            "rm -rf /",
            "rm -rf /*",
            "rm -rf ~",
            "rm -rf ~/*",
            "rm -rf .",
            "rm -rf ./*",
            "rm -rf *",  # Too broad
            "dd if=*",
            "mkfs*",
            "fdisk *",
            "parted *",
            "shred *",
            # Privilege escalation
            "sudo su",
            "sudo -i",
            "sudo bash",
            "sudo sh",
            "passwd",
            "passwd *",
            "useradd *",
            "usermod *",
            "groupadd *",
            "chown root *",
            "chmod 777 *",
            "chmod -R 777 *",
            # Code execution from network
            "curl * | bash",
            "curl * | sh",
            "wget * | bash",
            "wget * | sh",
            "curl * -o - | bash",
            # Network listeners (exfiltration risk)
            "nc -l *",
            "ncat -l *",
            "netcat -l *",
            "python -m http.server",
            "python -m SimpleHTTPServer",
            "php -S *",
            # System modification
            "reboot",
            "shutdown *",
            "halt",
            "poweroff",
            "init 0",
            "init 6",
            "systemctl reboot",
            "systemctl poweroff",
            # Kernel/system
            "insmod *",
            "rmmod *",
            "modprobe *",
            "sysctl *",
            # Fork bombs and resource exhaustion
            ":(){:|:&};:",
            ":(){ :|:& };:",
            "while true; do *",
            # Sensitive file access
            "cat /etc/shadow",
            "cat /etc/passwd",
            "cat ~/.ssh/*",
            "cat /root/*",
            # Docker escape attempts
            "docker run --privileged *",
            "docker run -v /:/host *",
            # Cron/scheduled tasks
            "crontab *",
            "at *",
        ]


def extract_shell_wrapper_payload(command: str) -> Optional[str]:
    """Extract wrapped payload for `bash/sh -c` command forms."""
    if not isinstance(command, str) or not command.strip():
        return None

    try:
        tokens = shlex.split(command, posix=True)
    except Exception:
        return None

    if len(tokens) < 2:
        return None

    shell_bin = os.path.basename(tokens[0]).lower()
    if shell_bin not in {"bash", "sh"}:
        return None

    for idx, token in enumerate(tokens[1:], start=1):
        if token in {"-c", "-lc"}:
            return tokens[idx + 1] if idx + 1 < len(tokens) else ""
    return None


def split_shell_chained_segments(command_line: str) -> List[str]:
    """Split a shell command line into top-level segments by `;`, `&&`, `||`."""
    line = (command_line or "").strip()
    if not line:
        return []

    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except Exception:
        return [line]

    split_tokens = {";", "&&", "||"}
    segments: List[str] = []
    current: List[str] = []

    for token in tokens:
        if token in split_tokens:
            segment = " ".join(current).strip()
            if segment:
                segments.append(segment)
            current = []
            continue
        current.append(token)

    tail = " ".join(current).strip()
    if tail:
        segments.append(tail)

    return segments or [line]


def is_removal_segment(segment: str) -> bool:
    """Detect whether a segment executes `rm` directly."""
    try:
        tokens = shlex.split(segment, posix=True)
    except Exception:
        return False
    if not tokens:
        return False
    return os.path.basename(tokens[0]).lower() == "rm"


def validate_shell_exec_command(
    command: str,
    *,
    max_command_chars: int = 320,
    policy: Optional[CommandPolicy] = None,
    metric_hook: Optional[Callable[[str], None]] = None,
) -> List[Dict[str, str]]:
    """Validate shell command text and return error descriptors."""
    validation_errors: List[Dict[str, str]] = []
    command_text = str(command or "")
    max_chars = int(max_command_chars or 320)

    if len(command_text) > max_chars:
        validation_errors.append(
            {
                "field": "command",
                "error": f"Command too long ({len(command_text)} > {max_chars} characters)",
                "message": f"Command exceeds max length of {max_chars} characters (received {len(command_text)}).",
                "suggested_fix": "Use a shorter command for the immediate objective, then continue in a follow-up tool call.",
            }
        )
        if metric_hook:
            metric_hook("executor_shell_exec_length_rejected")

    if policy is None:
        policy = CommandPolicy()

    def _validate_policy_segment(segment: str, *, source: str) -> None:
        policy_result = policy.validate(segment)
        if policy_result.allowed:
            return
        validation_errors.append(
            {
                "field": "command",
                "error": f"Policy violation in {source}",
                "message": f"Shell policy violation in {source}: {policy_result.reason}",
                "suggested_fix": "Adjust the command to satisfy shell policy or split the task into safer steps.",
            }
        )

    def _validate_segments_by_line(raw_text: str, *, source_prefix: str) -> None:
        for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue

            segments = split_shell_chained_segments(line)
            multi_segment = len(segments) > 1
            for segment_idx, segment in enumerate(segments, start=1):
                source = f"{source_prefix} {line_no}"
                if multi_segment:
                    source = f"{source} segment {segment_idx}"
                _validate_policy_segment(segment, source=source)

            # Lightweight guardrail: disallow `rm` segments hidden in chained commands.
            if multi_segment:
                for segment_idx, segment in enumerate(segments, start=1):
                    if not is_removal_segment(segment):
                        continue
                    source = f"{source_prefix} {line_no} segment {segment_idx}"
                    validation_errors.append(
                        {
                            "field": "command",
                            "error": f"Chained removal blocked in {source}",
                            "message": (
                                f"Chained removal is blocked in {source}. "
                                "Run removal as a separate explicit command."
                            ),
                            "suggested_fix": (
                                "Split this into separate commands so removal is explicit and isolated."
                            ),
                        }
                    )
                    if metric_hook:
                        metric_hook("executor_shell_exec_chained_removal_rejected")

    _validate_segments_by_line(command_text, source_prefix="command line")

    wrapped_payload = extract_shell_wrapper_payload(command_text)
    if wrapped_payload is not None:
        payload = wrapped_payload.strip()
        if not payload:
            validation_errors.append(
                {
                    "field": "command",
                    "error": "Empty shell wrapper payload",
                    "message": "bash/sh -c wrapper must include a non-empty payload command.",
                    "suggested_fix": "Provide a command payload after -c/-lc or use a direct shell command.",
                }
            )
        else:
            _validate_segments_by_line(wrapped_payload, source_prefix="wrapper payload line")

    if any("Policy violation" in err.get("error", "") for err in validation_errors):
        if metric_hook:
            metric_hook("executor_shell_exec_policy_rejected")
        # Keep a stable error signal for policy violations only.
    return validation_errors


def validate_shell_tool_parameters(
    tool_id: str,
    parameters: Dict[str, object],
    *,
    get_tool_fn: Callable[[str], object],
    generate_fix_suggestion_fn: Callable[[dict], str],
    max_command_chars: int = 320,
    metric_hook: Optional[Callable[[str], None]] = None,
    logger: object = None,
) -> List[Dict[str, str]]:
    """Validate strict shell tool parameters for ``shell.exec`` and ``shell.script``."""
    if tool_id not in {"shell.exec", "shell.script"}:
        return []

    try:
        tool_cls = get_tool_fn(tool_id)
        args_model = getattr(tool_cls, "args_model", None)
        if args_model is None:
            return [
                {
                    "field": "arguments",
                    "error": "Tool schema unavailable",
                    "message": "Tool schema unavailable",
                    "suggested_fix": f"Ensure {tool_id} declares a valid args_model",
                }
            ]

        args = args_model(**dict(parameters or {}))

        # shell.script keeps existing behavior (line-by-line policy validation in tool.run).
        if tool_id != "shell.exec":
            return []

        command = str(getattr(args, "command", "") or "")
        max_chars = int(max_command_chars or 320)
        validation_errors = validate_shell_exec_command(
            command,
            max_command_chars=max_chars,
            metric_hook=metric_hook,
        )
        if validation_errors and logger and hasattr(logger, "log_operation"):
            logger.log_operation(
                "WARNING",
                "EnhancedExecutor: shell.exec quality gate rejected command",
                metadata={
                    "tool_id": tool_id,
                    "error_count": len(validation_errors),
                    "command_length": len(command),
                },
            )
        return validation_errors
    except ValidationError as exc:
        return [
            {
                "field": ".".join(str(x) for x in err.get("loc", [])) or "arguments",
                "error": str(err.get("msg", "Invalid value")),
                "message": str(err.get("msg", "Invalid value")),
                "suggested_fix": generate_fix_suggestion_fn(err),
            }
            for err in exc.errors()
        ]
    except Exception as exc:
        return [
            {
                "field": "arguments",
                "error": str(exc),
                "message": str(exc),
                "suggested_fix": f"Provide valid arguments for {tool_id}",
            }
        ]


__all__ = [
    "CommandPolicy",
    "PolicyResult",
    "PolicyEnforcement",
    "extract_shell_wrapper_payload",
    "split_shell_chained_segments",
    "is_removal_segment",
    "validate_shell_exec_command",
    "validate_shell_tool_parameters",
]

