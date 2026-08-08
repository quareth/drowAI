"""PTY Executor Core
Executes shell commands in persistent PTY sessions for agent self-recovery and troubleshooting."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING, Optional

from agent.tools.shell.contracts import ShellCommandResult
from runtime_shared.shell_session_framing import (
    EXIT_CODE_PATTERN,
    PTY_EXIT_CODE_MARKER,
    ShellSessionFramingError,
    create_pty_command_frame,
    parse_exit_code_from_combined_output,
    parse_legacy_exit_code,
    parse_marked_command_output,
    strip_exit_code_marker,
    strip_pty_artifacts,
)
from runtime_shared.metrics import safe_gauge
from runtime_shared.terminal_contracts import (
    AGENT_PROMPT_ENV as PTY_PROMPT_ENV,
    AGENT_PROMPT_MARKER as PTY_PROMPT_MARKER,
    build_agent_session_id,
    build_named_agent_session_id,
)
from runtime_shared.terminal_manager_port import get_terminal_session_manager

if TYPE_CHECKING:
    from backend.services.terminal.models import TerminalSession

logger = logging.getLogger(__name__)


def _get_terminal_session_manager():
    """Resolve terminal manager through runtime-shared adapter boundary."""
    return get_terminal_session_manager()

class PTYSessionNotAvailable(Exception):
    """Raised when PTY session cannot be created or accessed."""
    pass


class PTYTimeoutError(Exception):
    """Raised when command execution exceeds timeout."""
    pass


class PTYCommandError(Exception):
    """Raised when command execution fails in PTY."""
    pass


class PTYReadTimeoutError(Exception):
    """Raised when PTY read times out, but carries partial output for recovery."""
    def __init__(self, message: str, partial_output: str = ""):
        super().__init__(message)
        self.partial_output = partial_output


class PTYOutputParseError(Exception):
    """Raised when marker-bounded PTY output cannot be parsed safely."""

    def __init__(self, message: str, raw_output: str = ""):
        super().__init__(message)
        self.raw_output = raw_output


async def _write_session_input(session: "TerminalSession", data: bytes | str) -> bool:
    """Write to a PTY session through the backend terminal manager."""
    terminal_session_manager = _get_terminal_session_manager()
    return await terminal_session_manager.send_input(session.session_id, data)


async def _read_session_output(
    session: "TerminalSession",
    size: int = 4096,
    *,
    timeout: float | None = None,
) -> bytes:
    """Read from a PTY session through the backend terminal manager."""
    terminal_session_manager = _get_terminal_session_manager()
    return await terminal_session_manager.read_output(session.session_id, size, timeout=timeout)


def _emit_latency_metric(metric_name: str, value_ms: float) -> None:
    """Emit PTY latency gauge while tolerating missing metrics backend."""
    try:
        safe_gauge(metric_name, max(0.0, float(value_ms)))
    except Exception:
        return


def _emit_hitl_stage_timing(
    *,
    stage: str,
    timestamp: float,
    task_id: int,
    interrupt_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
) -> None:
    """Emit standardized PTY stage timestamps for correlation and latency analysis."""
    logger.info(
        "[HITL_TIMING] stage=%s task_id=%s interrupt_id=%s tool_call_id=%s ts=%.9f",
        stage,
        task_id,
        interrupt_id or "unknown",
        tool_call_id or "unknown",
        float(timestamp),
    )


def _agent_session_id(task_id: int, session_name: Optional[str] = None) -> str:
    """Return the canonical or named agent PTY session id for a task."""
    if session_name:
        return build_named_agent_session_id(task_id, session_name)
    return build_agent_session_id(task_id)


async def _reset_agent_session(task_id: int, session_name: Optional[str] = None) -> None:
    """Force-close the existing agent PTY session so the next prepare call is fresh."""
    try:
        terminal_session_manager = _get_terminal_session_manager()
        await terminal_session_manager.close_session(_agent_session_id(task_id, session_name))
    except Exception as exc:
        logger.warning(
            "[PTY] Failed to reset agent session for task %s session_name=%s: %s",
            task_id,
            session_name or "<canonical>",
            exc,
        )

async def execute_via_pty(
    command: str,
    task_id: int,
    timeout_sec: int = 60,
    workspace_path: Optional[str] = None,
    interrupt_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
    session_name: Optional[str] = None,
    cleanup_session: bool = False,
) -> ShellCommandResult:
    """
    Execute shell command in agent's persistent PTY session.
    
    This function provides PTY-based execution for shell and filesystem tools,
    enabling visible troubleshooting and diagnostic commands during agent
    recovery workflows.
    
    Args:
        command: Shell command to execute
        task_id: Task ID for session isolation
        timeout_sec: Command timeout in seconds
        workspace_path: Optional workspace path for cwd
        session_name: Optional named PTY session for isolated parallel execution
        cleanup_session: Close named session after the call finishes
    
    Returns:
        ShellCommandResult with stdout, stderr, exit_code, duration_ms
    
    Raises:
        PTYSessionNotAvailable: If PTY session cannot be created
        PTYTimeoutError: If command exceeds timeout
        PTYCommandError: If command execution fails
    """
    logger.info(
        "[PTY] Executing command for task %s session_name=%s: %s",
        task_id,
        session_name or "<canonical>",
        command[:100],
    )
    
    try:
        # Get or create agent PTY session
        logger.debug(f"[PTY] Getting/creating session for task {task_id}")
        session_prepare_started_at = time.perf_counter()
        session = await _get_or_setup_pty_session(
            task_id=task_id,
            workspace_path=workspace_path,
            session_name=session_name,
            reset=bool(session_name),
        )
        _emit_latency_metric(
            "pty_session_prepare_ms",
            (time.perf_counter() - session_prepare_started_at) * 1000.0,
        )
        _emit_hitl_stage_timing(
            stage="pty_session_ready_at",
            timestamp=time.perf_counter(),
            task_id=task_id,
            interrupt_id=interrupt_id,
            tool_call_id=tool_call_id,
        )
        logger.debug("[PTY] Session obtained, executing command...")
        
        # Execute command with timeout (timeout is handled inside _execute_command_in_pty
        # to preserve partial output on timeout)
        start_time = asyncio.get_event_loop().time()
        try:
            parse_attempts = 0
            while True:
                _emit_hitl_stage_timing(
                    stage="tool_process_start_at",
                    timestamp=time.perf_counter(),
                    task_id=task_id,
                    interrupt_id=interrupt_id,
                    tool_call_id=tool_call_id,
                )
                try:
                    raw_output, stdout_for_tools, exit_code = await _execute_command_in_pty(
                        session, command, timeout_sec=timeout_sec
                    )
                    break
                except PTYOutputParseError as parse_exc:
                    parse_attempts += 1
                    if parse_attempts >= 2:
                        raise
                    logger.warning(
                        "[PTY] Marker parse failed for task %s session_name=%s; resetting session and retrying once: %s",
                        task_id,
                        session_name or "<canonical>",
                        parse_exc,
                    )
                    await _reset_agent_session(task_id, session_name=session_name)
                    session_prepare_started_at = time.perf_counter()
                    session = await _get_or_setup_pty_session(
                        task_id=task_id,
                        workspace_path=workspace_path,
                        session_name=session_name,
                        reset=False,
                    )
                    _emit_latency_metric(
                        "pty_session_prepare_ms",
                        (time.perf_counter() - session_prepare_started_at) * 1000.0,
                    )
            # Log output details to diagnose empty output issues
            logger.info(
                f"[PTY] Execution complete - raw_len={len(raw_output)}, "
                f"stdout_len={len(stdout_for_tools)}, exit_code={exit_code}"
            )
            if len(raw_output) < 500:
                logger.debug(f"[PTY] Raw output: {raw_output!r}")
        except PTYReadTimeoutError as timeout_exc:
            # Timeout occurred but we have partial output - RETURN IT to LLM for recovery!
            msg = f"PTY command timed out after {timeout_sec}s: {command[:100]}"
            logger.warning(f"[PTY] {msg}")
            # Try to interrupt the command
            try:
                await _write_session_input(session, b'\x03')  # Send Ctrl+C
            except Exception:
                pass
            # Get the ACTUAL partial output (not just the timeout message)
            partial_output = timeout_exc.partial_output or ""
            duration_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)
            
            # Minimal cleanup: only strip PTY artifacts (ANSI codes, markers), preserve actual output
            # This matches file-comm behavior where stdout is the raw command output
            stdout_output = _strip_pty_artifacts(partial_output, command)
            
            return ShellCommandResult(
                status="timeout",
                exit_code=-9,  # Conventional timeout exit code
                stdout=stdout_output,  # LLM sees actual output (same as file-comm)
                stderr=f"Command timed out after {timeout_sec}s",  # Actual error, not file hint
                duration_ms=duration_ms,
                transport="pty",
            )
        
        duration_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)
        
        # Determine status
        status = "success" if exit_code == 0 else "error"
        
        logger.info(
            f"[PTY] Command completed for task {task_id}: "
            f"exit_code={exit_code}, duration={duration_ms}ms"
        )
        
        # Return output EXACTLY like file-comm does:
        # - stdout: actual command output (only PTY artifacts stripped)
        # - stderr: empty (PTY combines streams, same as file-comm subprocess behavior)
        # This ensures tools parse output identically regardless of execution method.
        
        # Log if output is unexpectedly empty
        if not stdout_for_tools:
            logger.warning(f"[PTY] Empty stdout_for_tools for command: {command[:100]}")
        
        return ShellCommandResult(
            status=status,
            exit_code=exit_code,
            stdout=stdout_for_tools,  # Raw output from command (already marker-bounded & ANSI-stripped)
            stderr="",  # PTY combines streams; keep empty like file-comm
            duration_ms=duration_ms,
            transport="pty",
        )
    
    except PTYSessionNotAvailable as exc:
        logger.warning(
            "[PTY] Session not available for task %s session_name=%s: %s",
            task_id,
            session_name or "<canonical>",
            exc,
        )
        raise
    except PTYReadTimeoutError:
        # Already handled above, re-raise if somehow escaped
        raise
    except Exception as exc:
        import traceback
        msg = f"PTY execution failed for task {task_id}: {exc}"
        logger.error(f"[PTY] {msg}")
        logger.error(f"[PTY] Traceback: {traceback.format_exc()}")
        raise PTYCommandError(msg) from exc
    finally:
        if cleanup_session and session_name:
            try:
                terminal_session_manager = _get_terminal_session_manager()
                await terminal_session_manager.close_session(
                    _agent_session_id(task_id, session_name=session_name)
                )
            except Exception as exc:
                logger.warning(
                    "[PTY] Failed to clean up named session for task %s session_name=%s: %s",
                    task_id,
                    session_name,
                    exc,
                )


async def _get_or_setup_pty_session(
    task_id: int,
    workspace_path: Optional[str],
    session_name: Optional[str] = None,
    reset: bool = False,
) -> "TerminalSession":
    """
    Get existing agent PTY session or create and set up a new one.
    
    Sets up the session with:
    - Unique prompt marker for command completion detection
    - Workspace directory as cwd
    - Disabled command history for cleaner output
    
    Args:
        task_id: Task ID for session isolation
        workspace_path: Optional workspace path for cwd
        session_name: Optional named PTY session for isolated parallel execution
        reset: Close a stale named session before preparing it
    
    Returns:
        TerminalSession ready for command execution
    
    Raises:
        PTYSessionNotAvailable: If session cannot be created
    """
    try:
        terminal_session_manager = _get_terminal_session_manager()
        return await terminal_session_manager.prepare_agent_session(
            task_id=task_id,
            workspace_path=workspace_path,
            session_name=session_name,
            reset=reset,
        )
    
    except Exception as exc:
        msg = f"Failed to get/setup PTY session for task {task_id}: {exc}"
        logger.error(f"[PTY] {msg}")
        raise PTYSessionNotAvailable(msg) from exc


async def _execute_command_in_pty(
    session: "TerminalSession",
    command: str,
    timeout_sec: float = 60.0,
) -> tuple[str, str, int]:
    """
    Execute command in PTY session and capture output.
    
    Sends command, waits for prompt once, extracts stdout, and gets exit code.
    Exit code is emitted via a unique marker in the SAME read to avoid
    buffering/interleaving issues with a separate `echo $?` command.
    
    Uses a unique command ID per invocation to avoid reading stale output from
    previous commands in case of PTY buffer/state issues.
    
    Args:
        session: TerminalSession to execute command in
        command: Shell command to execute
        timeout_sec: Timeout in seconds for reading output
    
    Returns:
        Tuple of (raw_combined_output, stdout_for_tools, exit_code)
    
    Raises:
        PTYReadTimeoutError: If command output isn't received within timeout (carries partial output)
    """
    # Generate unique command ID to ensure we're reading THIS command's output
    frame = create_pty_command_frame(command, command_id=uuid.uuid4().hex[:8])
    
    # Drain any stale data from PTY buffer before sending new command.
    # This prevents reading leftover output from previous commands.
    await _drain_pty_buffer(session)
    
    # Verify session is still active
    if not session.is_active or not session.socket:
        logger.error("[PTY] Session is not active or has no socket")
        raise PTYSessionNotAvailable("PTY session is not active")
    
    logger.debug(f"[PTY] Sending wrapped command ({len(frame.wrapped_command)} bytes)")
    if not await _write_session_input(session, frame.wrapped_command.encode()):
        raise PTYSessionNotAvailable("PTY session input provider rejected command write")

    # Read output until we see BOTH the end marker AND the prompt
    try:
        raw_combined = await _read_until_marker_and_prompt(
            session,
            end_marker=frame.end_marker,
            timeout_sec=timeout_sec,
        )
        logger.debug(f"[PTY] Successfully read {len(raw_combined)} bytes until markers")
    except PTYReadTimeoutError as exc:
        logger.error(f"[PTY] Timeout waiting for markers: {exc}")
        logger.error(f"[PTY] Partial output ({len(exc.partial_output)} bytes): {exc.partial_output[:500]!r}")
        raise
    except Exception as exc:
        logger.error(f"[PTY] Unexpected error reading output: {exc}")
        raise

    # Extract output between start and end markers
    stdout_for_tools, exit_code = _parse_marked_output(
        raw_combined,
        frame.start_marker,
        frame.end_marker,
    )
    
    # Emit a warning when marker parsing yields unexpectedly empty output.
    if not stdout_for_tools and len(raw_combined) > 100:
        logger.warning(
            f"[PTY] Output extraction returned empty but raw_combined has "
            f"{len(raw_combined)} bytes. start_marker={frame.start_marker[:20]}, "
            f"raw_preview={raw_combined[:200]!r}"
        )

    return raw_combined, stdout_for_tools, exit_code


async def _drain_pty_buffer(session: "TerminalSession", drain_timeout: float = 0.1) -> None:
    """
    Drain any stale data from PTY buffer.
    
    This prevents reading leftover output from previous commands that might
    have left data in the buffer due to interrupts, timeouts, etc.
    """
    try:
        while True:
            chunk = await _read_session_output(session, 4096, timeout=drain_timeout)
            if not chunk:
                break
            # Discard the stale data
            logger.debug(f"[PTY] Drained {len(chunk)} stale bytes from buffer")
    except asyncio.TimeoutError:
        # No more data available - buffer is drained
        pass
    except Exception as exc:
        logger.debug(f"[PTY] Buffer drain error (non-fatal): {exc}")


async def _read_until_marker_and_prompt(
    session: "TerminalSession",
    end_marker: str,
    timeout_sec: float = 60.0,
) -> str:
    """
    Read from PTY session until end marker + exit code appear (prompt optional).
    
    This function waits for the end marker and a valid exit code, which proves
    the command completed. The prompt is given 500ms to arrive after the exit
    code is detected, but is not required (to handle buffering edge cases where
    the prompt doesn't flush immediately on failed commands).
    
    Args:
        session: TerminalSession to read from
        end_marker: Unique end marker for this command
        timeout_sec: Read timeout in seconds
    
    Returns:
        Accumulated output as string
    
    Raises:
        PTYReadTimeoutError: If markers not detected within timeout (carries partial output)
    """
    output = b""
    start_time = asyncio.get_event_loop().time()
    prompt_wait_started = None  # Track when we start waiting for prompt
    chunk_count = 0
    
    while True:
        # Check overall timeout
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed > timeout_sec:
            partial = output.decode('utf-8', errors='replace')
            raise PTYReadTimeoutError(
                f"Command markers not detected within {timeout_sec}s",
                partial_output=partial,
            )
        
        # Read chunk
        try:
            chunk = await _read_session_output(session, 4096, timeout=0.5)
            chunk_count += 1
            if chunk:
                output += chunk
                
                decoded = output.decode('utf-8', errors='replace')
                
                # Check markers
                has_end_marker = end_marker in decoded
                has_exit_code = bool(PTY_EXIT_CODE_MARKER in decoded and EXIT_CODE_PATTERN.search(decoded))
                has_prompt = PTY_PROMPT_MARKER in decoded or PTY_PROMPT_ENV in decoded
                
                # If we have end marker + exit code + prompt: perfect, return immediately
                if has_end_marker and has_exit_code and has_prompt:
                    logger.debug("[PTY] Command complete with all markers (exit_code + prompt)")
                    return decoded
                
                # If we have end marker + exit code: command is done, give prompt 500ms to arrive
                if has_end_marker and has_exit_code and prompt_wait_started is None:
                    prompt_wait_started = asyncio.get_event_loop().time()
                    logger.debug("[PTY] Command complete (has exit code), waiting briefly for prompt")
                
                # If we've been waiting for prompt, check if grace period elapsed
                if prompt_wait_started is not None:
                    prompt_wait_elapsed = asyncio.get_event_loop().time() - prompt_wait_started
                    if prompt_wait_elapsed > 0.5:  # 500ms grace period
                        logger.info(
                            "[PTY] Prompt not received within 500ms, proceeding with exit code "
                            "(command is complete)"
                        )
                        return decoded
                    
        except asyncio.TimeoutError:
            # No data available yet, check if we already have what we need
            decoded = output.decode('utf-8', errors='replace')
            has_end_marker = end_marker in decoded
            has_exit_code = bool(PTY_EXIT_CODE_MARKER in decoded and EXIT_CODE_PATTERN.search(decoded))
            has_prompt = PTY_PROMPT_MARKER in decoded or PTY_PROMPT_ENV in decoded
            
            # Perfect case: all markers present
            if has_end_marker and has_exit_code and has_prompt:
                logger.debug("[PTY] Command complete with all markers")
                return decoded
            
            # Start prompt wait if we have completion markers
            if has_end_marker and has_exit_code and prompt_wait_started is None:
                prompt_wait_started = asyncio.get_event_loop().time()
                logger.debug("[PTY] Command complete (has exit code), waiting for prompt")
            
            # Check prompt wait timeout
            if prompt_wait_started is not None:
                prompt_wait_elapsed = asyncio.get_event_loop().time() - prompt_wait_started
                if prompt_wait_elapsed > 0.5:
                    logger.info(
                        "[PTY] Prompt not received after 500ms, proceeding anyway "
                        "(exit code confirms completion)"
                    )
                    return decoded
            
            continue
        except Exception as exc:
            logger.warning(f"[PTY] Error reading from session: {exc}")
            return output.decode('utf-8', errors='replace')


def _parse_marked_output(
    raw_output: str,
    start_marker: str,
    end_marker: str,
) -> tuple[str, int]:
    """
    Parse output bounded by unique start/end markers.
    
    Returns:
        Tuple of (command_output, exit_code)
    """
    try:
        command_output, exit_code = parse_marked_command_output(
            raw_output,
            start_marker,
            end_marker,
        )
    except ShellSessionFramingError as exc:
        raise PTYOutputParseError(str(exc), raw_output=exc.raw_output) from exc

    logger.debug(
        "[PTY] Extracted marker-bounded output: output_len=%s",
        len(command_output),
    )
    return command_output, exit_code


def _parse_exit_code_from_combined_output(output: str) -> int:
    """Parse exit code from combined output containing PTY_EXIT_CODE_MARKER."""
    try:
        return parse_exit_code_from_combined_output(output)
    except Exception:
        return 1


def _strip_exit_code_marker(output: str) -> str:
    """Remove the exit-code marker line from combined output."""
    try:
        return strip_exit_code_marker(output)
    except Exception:
        return output


async def _read_until_prompt(
    session: "TerminalSession",
    timeout_sec: float = 30.0,
) -> str:
    """
    Read from PTY session until prompt marker appears.
    
    Args:
        session: TerminalSession to read from
        timeout_sec: Read timeout in seconds
    
    Returns:
        Accumulated output as string
    
    Raises:
        PTYReadTimeoutError: If prompt not detected within timeout (carries partial output)
    """
    output = b""
    start_time = asyncio.get_event_loop().time()
    
    while True:
        # Check timeout
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed > timeout_sec:
            # Carry partial output in the exception so caller can recover/save it
            partial = output.decode('utf-8', errors='replace')
            raise PTYReadTimeoutError(
                f"Prompt not detected within {timeout_sec}s",
                partial_output=partial,
            )
        
        # Read chunk
        try:
            chunk = await asyncio.wait_for(
                _read_session_output(session, 1024, timeout=0.5),
                timeout=1.0,
            )
            if chunk:
                output += chunk
                
                # Check if prompt appears in output
                decoded = output.decode('utf-8', errors='replace')
                if PTY_PROMPT_MARKER in decoded or PTY_PROMPT_ENV in decoded:
                    return decoded
        except asyncio.TimeoutError:
            # No data available, check if we already have prompt
            decoded = output.decode('utf-8', errors='replace')
            if PTY_PROMPT_MARKER in decoded or PTY_PROMPT_ENV in decoded:
                return decoded
            continue
        except Exception as exc:
            logger.warning(f"[PTY] Error reading from session: {exc}")
            # Return what we have
            return output.decode('utf-8', errors='replace')


def _parse_exit_code(output: str) -> int:
    """
    Extract exit code from echo $? output.
    
    Args:
        output: PTY output containing exit code
    
    Returns:
        Exit code as integer (defaults to 1 if parsing fails)
    """
    try:
        return parse_legacy_exit_code(output)
    except Exception as exc:
        logger.warning(f"[PTY] Error parsing exit code: {exc}")
        return 1


def _strip_pty_artifacts(output: str, command: str) -> str:
    """
    Minimal cleanup of PTY output - only strip PTY-specific artifacts.
    
    This function does minimal processing to match file-comm behavior:
    - Strip ANSI escape codes (PTY terminal codes)
    - Normalize line endings (CRLF → LF)
    - Remove command echo if present (PTY echoes input)
    - Remove prompt markers (our PTY markers)
    
    Does NOT remove:
    - Empty lines (preserves output formatting)
    - Numeric-only lines (could be valid output)
    - Any actual command output
    
    Args:
        output: Raw PTY output
        command: Original command (for echo removal)
    
    Returns:
        Output with only PTY artifacts removed
    """
    return strip_pty_artifacts(output, command)


def _cleanup_pty_output(output: str, command: str) -> str:
    """
    DEPRECATED: Use _strip_pty_artifacts() instead.
    
    This function over-cleans output and is kept only for backward compatibility.
    The new _strip_pty_artifacts() does minimal cleanup matching file-comm behavior.
    """
    # Delegate to the minimal version
    return _strip_pty_artifacts(output, command)


__all__ = [
    'execute_via_pty',
    'PTYSessionNotAvailable',
    'PTYTimeoutError',
    'PTYCommandError',
]
