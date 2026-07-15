"""Production-grade diagnostic logging for LangGraph execution.

This module provides structured, file-based logging for LangGraph streaming,
checkpointing, and graph execution diagnostics without polluting console output.
"""

from __future__ import annotations

import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Optional

from core.llm.timeout_runtime import log_timeout_event as _shared_log_timeout_event

# Module-level logger instance
_diagnostic_logger: Optional[logging.Logger] = None
_logger_configured = False


def get_diagnostic_logger() -> logging.Logger:
    """Get or create the LangGraph diagnostic logger.
    
    Logs are written to: backend/log/langgraph_diagnostics.log
    - Max file size: 50 MB (with 5 backup files)
    - Format: timestamp | level | component | message
    - Levels: DEBUG for detailed traces, INFO for key events
    """
    global _diagnostic_logger, _logger_configured
    
    if _diagnostic_logger is not None and _logger_configured:
        return _diagnostic_logger
    
    # Create logger
    _diagnostic_logger = logging.getLogger("langgraph.diagnostics")
    _diagnostic_logger.setLevel(logging.DEBUG)
    _diagnostic_logger.propagate = False  # Don't propagate to root logger
    
    # Determine log directory
    log_dir = Path(__file__).resolve().parent.parent.parent / "log"
    log_file = log_dir / "langgraph_diagnostics.log"
    
    # Create directory if needed
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Fallback to current directory
        log_dir = Path(".")
        log_file = log_dir / "langgraph_diagnostics.log"
    
    # Create rotating file handler
    try:
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=50 * 1024 * 1024,  # 50 MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        
        # Format: 2025-11-12 20:15:30,123 | INFO | EXECUTOR | Streaming graph for task 1461
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        file_handler.setFormatter(formatter)
        
        _diagnostic_logger.addHandler(file_handler)
        _logger_configured = True
        
    except Exception:
        _diagnostic_logger.addHandler(logging.NullHandler())
        _logger_configured = True
    
    return _diagnostic_logger


# Convenience functions for common diagnostic scenarios

def log_graph_execution(task_id: int, graph_type: str, details: Dict[str, Any]) -> None:
    """Log graph execution start."""
    logger = get_diagnostic_logger()
    details_str = ", ".join(f"{k}={v}" for k, v in details.items())
    logger.info(f"EXECUTOR | Task {task_id} | Starting {graph_type} execution | {details_str}")


def log_streaming_event(task_id: int, event_num: int, mode: str, event_type: str) -> None:
    """Log a streaming event."""
    logger = get_diagnostic_logger()
    logger.debug(f"EXECUTOR | Task {task_id} | Event #{event_num} | mode={mode}, type={event_type}")


def log_state_capture(task_id: int, captured: bool, state_keys: Optional[list] = None) -> None:
    """Log state capture from values mode."""
    logger = get_diagnostic_logger()
    if captured:
        keys_str = f", keys={state_keys}" if state_keys else ""
        logger.info(f"EXECUTOR | Task {task_id} | State captured successfully{keys_str}")
    else:
        logger.error(f"EXECUTOR | Task {task_id} | Failed to capture state from values events")


def log_checkpointer_operation(task_id: int, operation: str, success: bool = True, details: str = "") -> None:
    """Log checkpointer operations (setup, aput, aget)."""
    logger = get_diagnostic_logger()
    status = "SUCCESS" if success else "FAILED"
    detail_str = f" | {details}" if details else ""
    logger.info(f"CHECKPOINT | Task {task_id} | {operation} | {status}{detail_str}")


def log_wrapper_context(node_name: str, has_writer: bool, has_config: bool) -> None:
    """Log node wrapper parameter injection."""
    logger = get_diagnostic_logger()
    logger.debug(f"WRAPPER | Node {node_name} | writer={has_writer}, config={has_config}")


def log_graph_build(graph_type: str, checkpointer_type: str, node_count: int) -> None:
    """Log graph compilation."""
    logger = get_diagnostic_logger()
    logger.info(f"BUILDER | Building {graph_type} | checkpointer={checkpointer_type}, nodes={node_count}")


def log_handler_flow(task_id: int, handler: str, stage: str, success: bool = True, error: str = "") -> None:
    """Log handler execution stages."""
    logger = get_diagnostic_logger()
    status = "SUCCESS" if success else "FAILED"
    error_str = f" | error={error}" if error else ""
    logger.info(f"HANDLER | Task {task_id} | {handler} | {stage} | {status}{error_str}")


def log_timeout_event(
    task_id: int,
    component: str,
    operation: str,
    timeout_sec: float,
    outcome: str,
    details: str = "",
) -> None:
    """Log a LangGraph timeout with consistent task-scoped formatting."""
    _shared_log_timeout_event(
        get_diagnostic_logger(),
        task_id=task_id,
        component=component,
        operation=operation,
        timeout_sec=timeout_sec,
        outcome=outcome,
        details=details,
    )


__all__ = [
    "get_diagnostic_logger",
    "log_graph_execution",
    "log_streaming_event",
    "log_state_capture",
    "log_checkpointer_operation",
    "log_wrapper_context",
    "log_graph_build",
    "log_handler_flow",
    "log_timeout_event",
]
