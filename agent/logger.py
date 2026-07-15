"""Unified structured logging utilities for agent processes."""

from __future__ import annotations

import json
import logging
import os
import time
import threading
from typing import Any, Dict, Optional
from enum import Enum
from pathlib import Path

from agent.core.time_utils import format_iso, utc_now

# Force all Python logging formatter timestamps to UTC.
logging.Formatter.converter = time.gmtime


class LogLevel(Enum):
    """Available log levels."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class UnifiedAgentLogger:
    """Unified logging system for agent activities across all interfaces."""

    def __init__(self, task_id: str, log_level: str = "INFO"):
        self.task_id = task_id
        self.log_level = getattr(logging, log_level.upper(), logging.INFO)
        self._file_lock = threading.Lock()
        
        # Use environment variable for workspace or default to /workspace (container path)
        workspace = os.getenv("WORKSPACE", "/workspace")
        
        # Ensure workspace directory exists
        os.makedirs(workspace, exist_ok=True)
        
        self.log_file = os.path.join(workspace, "log.txt")
        self.error_file = os.path.join(workspace, "error.log")

        # Setup Python logging for errors only
        self.logger = logging.getLogger(f"agent-{task_id}")
        self.logger.setLevel(logging.ERROR)
        
        # Create file handlers with proper error handling
        try:
            file_handler = logging.FileHandler(self.error_file)
        except (OSError, IOError):
            file_handler = logging.NullHandler()
        file_handler.setLevel(logging.ERROR)
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        file_handler.setFormatter(formatter)
        if not self.logger.handlers:
            self.logger.addHandler(file_handler)

        # Initialize conversational log
        self._init_conversational_log()

    def _init_conversational_log(self) -> None:
        """Initialize the conversational log file."""
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
        with self._file_lock:
            with open(self.log_file, "w", encoding="utf-8") as f:
                f.write("# Penetration Test Agent Log\n")
                f.write(f"**Task ID:** {self.task_id}\n")
                f.write(
                    f"**Started:** {utc_now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                )
                f.write("---\n\n")

    def log(self, level: str, message: str, 
             react_step: bool = False, 
             console: bool = False,
             database: bool = False,
             metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        Unified logging method for all output channels.
        
        Args:
            level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            message: Log message
            react_step: Whether to emit as react_step for reasoning UI
            console: Whether to output to console (Docker logs)
            database: Whether to write to database (status page)
            metadata: Additional structured data
        """
        timestamp = format_iso(utc_now())
        
        # Create structured log entry for console/database logs
        log_entry = {
            "timestamp": timestamp,
            "task_id": self.task_id,
            "level": level,
            "message": message,
        }
        if metadata:
            log_entry["metadata"] = metadata

        # Write to appropriate outputs
        if react_step:
            # React steps go to frontend only, not to file log
            self._write_react_step(level, message, metadata)
        
        if console:
            # Operation logs are stored in the task log file.
            self._write_console(level, message)
            self._write_file_log(log_entry)
        
        if database:
            self._write_database(log_entry)
        
        # Write errors to Python logger
        if level in ["ERROR", "CRITICAL"]:
            self.logger.error(json.dumps(log_entry))

    def _write_react_step(self, step_type: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Write structured ReAct step for reasoning UI."""
        entry = {
            "timestamp": format_iso(utc_now()),
            "type": "react_step",
            "step_type": step_type,
            "content": content,
            "metadata": metadata or {},
        }
        with self._file_lock:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
                f.flush()  # Ensure immediate write for real-time streaming

        # Also persist to DB for real agents so SSE DB streaming works without file tail
        try:
            from backend.config import AGENT_REASONING_MOCK_MODE, REASONING_DB_PERSIST
            if not AGENT_REASONING_MOCK_MODE or REASONING_DB_PERSIST:
                from backend.database import SessionLocal
                from backend.services.streaming.reasoning_store import AgentReasoningStore
                db = SessionLocal()
                try:
                    # Prefer the logger's task_id if numeric; fallback to path inference
                    task_id: Optional[int] = None
                    try:
                        task_id = int(self.task_id)
                    except Exception:
                        try:
                            parent = Path(self.log_file).parent.name
                            if parent.startswith("task_"):
                                task_id = int(parent.split("_", 1)[1])
                        except Exception:
                            task_id = None
                    if task_id is not None:
                        AgentReasoningStore(db).append_step(task_id, entry)
                finally:
                    try:
                        db.close()
                    except Exception:
                        pass
        except Exception:
            # Swallow DB issues to never impact logging path
            pass

    def _write_console(self, level: str, message: str) -> None:
        """Compatibility hook for operation logs now persisted through ``log.txt``."""
        return None

    def _write_database(self, log_entry: Dict[str, Any]) -> None:
        """Write to database for status page (placeholder for now)."""
        # TODO: Implement database logging when needed
        pass

    def _write_file_log(self, log_entry: Dict[str, Any]) -> None:
        """Write structured log to file."""
        with self._file_lock:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry) + "\n")
                f.flush()

    # Convenience methods for common logging patterns
    def info(self, message: str, **kwargs) -> None:
        """Log info level message."""
        self.log("INFO", message, console=True, **kwargs)

    def warning(self, message: str, **kwargs) -> None:
        """Log warning level message."""
        self.log("WARNING", message, console=True, **kwargs)

    def error(self, message: str, **kwargs) -> None:
        """Log error level message."""
        self.log("ERROR", message, console=True, **kwargs)

    def critical(self, message: str, **kwargs) -> None:
        """Log critical level message."""
        self.log("CRITICAL", message, console=True, **kwargs)

    def debug(self, message: str, **kwargs) -> None:
        """Log debug level message (only if debug enabled)."""
        if self.log_level <= logging.DEBUG:
            self.log("DEBUG", message, **kwargs)

    # Reasoning UI specific methods - these should ONLY use react_step, not console
    def log_reasoning_step(self, step_type: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Single method for all reasoning steps - frontend only."""
        self.log(step_type.lower(), content, react_step=True, metadata=metadata)

    def log_operation(self, level: str, message: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Single method for operational logs - task log file only."""
        self.log(level, message, console=True, metadata=metadata)



    # Legacy compatibility methods
    def conversation(self, message: str) -> None:
        """Legacy method - now uses unified logging."""
        self.log("INFO", message, console=True)

    def log_command(self, command: list, stdout: str, stderr: str, returncode: int) -> None:
        """Log command execution details."""
        command_str = " ".join(command)
        
        # Log command execution
        self.log("INFO", f"🔧 Executed: {command_str}", console=True)
        
        if returncode == 0:
            if stdout.strip():
                display_output = stdout[:500] + "..." if len(stdout) > 500 else stdout
                self.log("INFO", f"✅ Output:\n{display_output}", console=True)
            else:
                self.log("INFO", "✅ Command completed successfully (no output)", console=True)
        else:
            self.log("ERROR", f"❌ Command failed with exit code {returncode}", console=True)
            if stderr.strip():
                self.log("ERROR", f"Error output:\n{stderr}", console=True)

        # Structured log with metadata
        self.log("INFO", f"Command executed: {command_str}", 
                metadata={
                    "command": command,
                    "stdout_length": len(stdout),
                    "stderr_length": len(stderr),
                    "returncode": returncode,
                    "success": returncode == 0,
                })

# -----------------------------------------------------------------------------
# Module-wide logging configuration for library/module logs
# -----------------------------------------------------------------------------
_module_logging_configured = False


def configure_module_logging(level: Optional[str] = None, logfile: Optional[str] = None) -> None:
    """Configure Python's root logger to capture module logs to a file.

    - Creates `agent/log/` directory if it doesn't exist
    - Writes module logs (INFO and above by default) to `agent/log/module.log`
    - Log level is configurable via argument or env `MODULE_LOG_LEVEL`
    - Optional custom file path via argument or env `MODULE_LOG_FILE`
    """
    global _module_logging_configured
    if _module_logging_configured:
        return

    # Resolve level
    lvl_name = (level or os.getenv("MODULE_LOG_LEVEL", "INFO")).upper()
    lvl = getattr(logging, lvl_name, logging.INFO)

    # Resolve file path (default: agent/log/module.log relative to this file)
    default_dir = Path(__file__).resolve().parent / "log"
    file_override = logfile or os.getenv("MODULE_LOG_FILE")
    log_path = Path(file_override) if file_override else default_dir / "module.log"

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Best-effort; fallback to current working directory
        log_path = Path("module.log")

    root = logging.getLogger()
    root.setLevel(lvl)

    # File handler
    try:
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(lvl)
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        fh.setFormatter(formatter)
        # Avoid duplicate handler on reloads
        if not any(isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == str(log_path) for h in root.handlers):
            root.addHandler(fh)
    except Exception:
        root.addHandler(logging.NullHandler())

    _module_logging_configured = True


# Backward compatibility - alias the new logger
AgentLogger = UnifiedAgentLogger
