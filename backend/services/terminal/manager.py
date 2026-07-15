"""Terminal session orchestration service.

Responsibilities:
- Own user and agent PTY session orchestration.
- Delegate session storage and stale-session lifecycle to the registry.
- Preserve the existing terminal manager public API and runtime behavior.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
from typing import Any, Dict, Optional, Tuple

from ...config import E2E_RUNTIME_LOCAL_MODE
from ...core.time_utils import format_iso, utc_now
from ...database import SessionLocal
from ...models import Task
from ..runtime_provider import (
    RuntimeActorType,
    RuntimeCallScope,
    RuntimeOperationService,
    RuntimeProviderContextResolver,
)
from ..runtime_provider.terminal_stream_contract import is_push_terminal_stream
from .contracts import (
    AGENT_PROMPT_ENV,
    AGENT_PROMPT_MARKER,
    build_agent_session_id,
    build_named_agent_session_id,
)
from .models import TerminalSession
from .registry import TerminalSessionRegistry

logger = logging.getLogger(__name__)


class TerminalSessionManager:
    """Manage terminal sessions for user terminals and agent PTYs."""

    def __init__(self, registry: TerminalSessionRegistry | None = None) -> None:
        self._registry = registry or TerminalSessionRegistry()
        self.max_sessions_per_user = 10
        self.ws_disconnect_grace_seconds = 30
        self._grace_close_tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def sessions(self) -> dict[str, TerminalSession]:
        """Compatibility view over the in-memory session map."""
        return self._registry.sessions

    @sessions.setter
    def sessions(self, value: dict[str, TerminalSession]) -> None:
        self._registry.sessions = value

    @property
    def session_timeout(self) -> int:
        return self._registry.session_timeout

    @session_timeout.setter
    def session_timeout(self, value: int) -> None:
        self._registry.session_timeout = value

    @property
    def agent_session_timeout(self) -> int:
        return self._registry.agent_session_timeout

    @agent_session_timeout.setter
    def agent_session_timeout(self, value: int) -> None:
        self._registry.agent_session_timeout = value

    @property
    def cleanup_interval(self) -> int:
        return self._registry.cleanup_interval

    @cleanup_interval.setter
    def cleanup_interval(self, value: int) -> None:
        self._registry.cleanup_interval = value

    @property
    def _cleanup_task(self) -> asyncio.Task | None:
        return self._registry.cleanup_task

    @_cleanup_task.setter
    def _cleanup_task(self, value: asyncio.Task | None) -> None:
        self._registry.cleanup_task = value

    def _start_cleanup_task(self) -> None:
        """Start the background cleanup task."""
        self._registry.start_cleanup_loop(self.close_session)

    def start(self) -> None:
        """Public entry to start background maintenance tasks."""
        try:
            self._start_cleanup_task()
        except RuntimeError:
            pass

    async def _cleanup_stale_sessions(self) -> None:
        """Close all sessions whose activity exceeded the configured timeout."""
        for session_id in list(self._registry.iter_stale_session_ids()):
            session = self._registry.get(session_id)
            if session is None:
                continue
            logger.info("Cleaning up stale %s session: %s", session.session_type, session_id)
            await self.close_session(session_id)

    def _generate_session_id(self, user_id: int, task_id: int) -> str:
        """Generate a unique user terminal session id."""
        timestamp = int(time.time())
        return f"term_{user_id}_{task_id}_{timestamp}"

    async def _validate_container_access(
        self,
        task_id: int,
        user_id: int,
        *,
        authorized_task: Task | None = None,
        tenant_id: int | None = None,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
    ) -> bool:
        """Validate user has access to the task container."""
        try:
            db = SessionLocal()
            try:
                runtime_operations = RuntimeOperationService(db)
                if authorized_task is not None:
                    result = await runtime_operations.run_authorized_task_operation(
                        task=authorized_task,
                        user_id=user_id,
                        operation="get_runtime_status",
                        call=lambda provider, request: provider.get_runtime_status(request),
                        metadata={"wait_for_result": True, "wait_timeout_seconds": 5.0},
                        runtime_call_scope=runtime_call_scope,
                    )
                else:
                    if tenant_id is None:
                        logger.warning("Terminal runtime access denied without tenant context: task_id=%s", task_id)
                        return False
                    result = await runtime_operations.run_user_task_operation(
                        task_id=task_id,
                        user_id=user_id,
                        tenant_id=int(tenant_id),
                        operation="get_runtime_status",
                        call=lambda provider, request: provider.get_runtime_status(request),
                        metadata={"wait_for_result": True, "wait_timeout_seconds": 5.0},
                        runtime_call_scope=runtime_call_scope,
                    )
            finally:
                db.close()
            container_status = result.metadata.get("delegate_result") if result.ok else "unknown"
            if self._is_runtime_accessible_status(container_status):
                return True

            logger.warning(
                "Runtime for task %s not accessible, status: %s",
                task_id,
                container_status,
            )
            return False
        except Exception as exc:
            logger.error("Error validating container access: %s", exc)
            return False

    def _validate_task_ownership(self, task_id: int, user_id: int, *, tenant_id: int | None) -> bool:
        """Ensure the task belongs to the requesting user in an explicit tenant."""
        db = None
        try:
            from ..task.access_service import get_owned_task

            if tenant_id is None:
                logger.warning("Terminal task access denied without tenant context: task_id=%s requester=%s", task_id, user_id)
                return False
            db = SessionLocal()
            task = get_owned_task(db=db, task_id=task_id, user_id=user_id, tenant_id=int(tenant_id))
            if not task:
                logger.warning(
                    "Unauthorized or missing terminal task access: task_id=%s requester=%s",
                    task_id,
                    user_id,
                )
                return False
            return True
        except Exception as exc:
            logger.error("Error validating task ownership for task_id=%s: %s", task_id, exc)
            return False
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    async def create_session(
        self,
        task_id: int,
        user_id: int,
        cols: int = 80,
        rows: int = 24,
        authorized_task: Task | None = None,
        tenant_id: int | None = None,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
    ) -> Optional[TerminalSession]:
        """Create a PTY-backed user terminal session."""
        try:
            if E2E_RUNTIME_LOCAL_MODE:
                runtime_call_scope = RuntimeCallScope.TEST
            if authorized_task is None and not self._validate_task_ownership(task_id, user_id, tenant_id=tenant_id):
                logger.error(
                    "Terminal session denied: user_id=%s not authorized for task_id=%s",
                    user_id,
                    task_id,
                )
                return None

            user_sessions = self.get_user_sessions(user_id)
            if len(user_sessions) >= self.max_sessions_per_user:
                logger.warning("User %s has reached maximum sessions limit", user_id)
                return None

            validation_kwargs = {
                "authorized_task": authorized_task,
                "runtime_call_scope": runtime_call_scope,
            }
            if authorized_task is None:
                validation_kwargs["tenant_id"] = tenant_id
            if not await self._validate_container_access(task_id, user_id, **validation_kwargs):
                logger.error("User %s cannot access runtime for task %s", user_id, task_id)
                return None

            session_id = self._generate_session_id(user_id, task_id)
            db = SessionLocal()
            try:
                runtime_operations = RuntimeOperationService(db)
                if authorized_task is not None:
                    result = await runtime_operations.run_authorized_task_operation(
                        task=authorized_task,
                        user_id=user_id,
                        operation="open_terminal_session",
                        call=lambda provider, request: provider.open_terminal_session(request),
                        payload={"shell": "/bin/bash", "cols": cols, "rows": rows},
                        metadata={"wait_for_result": True, "wait_timeout_seconds": 5.0},
                        runtime_call_scope=runtime_call_scope,
                    )
                else:
                    if tenant_id is None:
                        logger.error("Terminal session denied without tenant context for task %s", task_id)
                        return None
                    result = await runtime_operations.run_user_task_operation(
                        task_id=task_id,
                        user_id=user_id,
                        tenant_id=int(tenant_id),
                        operation="open_terminal_session",
                        call=lambda provider, request: provider.open_terminal_session(request),
                        payload={"shell": "/bin/bash", "cols": cols, "rows": rows},
                        metadata={"wait_for_result": True, "wait_timeout_seconds": 5.0},
                        runtime_call_scope=runtime_call_scope,
                    )
            finally:
                db.close()
            if not result.ok:
                logger.error("Terminal provider open failed for task %s: %s", task_id, result.error_message)
                return None
            delegate = result.metadata.get("delegate_result")
            if not isinstance(delegate, dict):
                return None
            provider_session_id = delegate.get("session_id")
            exec_id = delegate.get("exec_id") or provider_session_id
            runtime_job_id = delegate.get("runtime_job_id")
            sock = delegate.get("socket")
            container_name = str(delegate.get("container_name") or f"drowai-task-{task_id}")
            logger.debug(
                "[PTY] Created PTY socket for session_id=%s, type=%s",
                session_id,
                type(sock),
            )
            session = TerminalSession(
                session_id=session_id,
                task_id=task_id,
                user_id=user_id,
                container_name=container_name,
                connection_type="docker_exec",
                exec_id=exec_id,
                runtime_job_id=str(runtime_job_id) if runtime_job_id else None,
                runtime_call_scope=getattr(runtime_call_scope, "value", str(runtime_call_scope)),
                stream_mode=is_push_terminal_stream(sock),
                socket=sock,
            )
            self._registry.set(session)
            if session.stream_mode:
                session.reader_task = asyncio.create_task(self._drain_initial_stream_buffer(session))
            else:
                session.reader_task = asyncio.create_task(self._pty_reader(session))
            logger.info(
                "Created terminal session %s for user %s, task %s (PTY)",
                session_id,
                user_id,
                task_id,
            )
            return session
        except Exception as exc:
            logger.error("Error creating terminal session: %s", exc)
            return None

    async def execute_command(self, session_id: str, command: str) -> Tuple[bool, str]:
        """Execute a command in the terminal session."""
        try:
            session = self._registry.get(session_id)
            if not session or not session.is_active:
                return False, "Session not found or inactive"

            session.update_activity()

            db = SessionLocal()
            try:
                runtime_operations = RuntimeOperationService(db)
                result = await runtime_operations.run_for_context(
                    context=runtime_operations.context_for_internal_task(
                        task_id=session.task_id,
                        actor_type=RuntimeActorType.USER,
                        actor_id=session.user_id,
                        user_id=session.user_id,
                        runtime_call_scope=session.runtime_call_scope,
                    ),
                    operation="execute_runtime_command",
                    call=lambda provider, request: provider.execute_runtime_command(request),
                    payload={"command": command},
                )
            finally:
                db.close()
            if not result.ok:
                return False, result.error_message or "Command failed"
            command_result = result.metadata.get("delegate_result")

            if not command_result:
                return True, ""

            output_lines = []
            for log_entry in command_result:
                if isinstance(log_entry, dict) and "message" in log_entry:
                    output_lines.append(log_entry["message"])
                else:
                    output_lines.append(str(log_entry))

            return True, "\n".join(output_lines)
        except Exception as exc:
            logger.error("Error executing command in session %s: %s", session_id, exc)
            return False, f"Error executing command: {str(exc)}"

    async def _pty_reader(self, session: TerminalSession) -> None:
        """Continuously read PTY output, buffer it, and fan it out to listeners."""
        try:
            while session.is_active and self._session_provider_ref(session) is not None:
                try:
                    chunk = await self.read_output(session.session_id, 4096, timeout=0.5)
                except Exception:
                    await asyncio.sleep(0.01)
                    continue
                if not chunk:
                    await asyncio.sleep(0.01)
                    continue
                await self._handle_output_chunk(session, chunk)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("PTY reader task error for %s: %s", session.session_id, exc)

    async def _drain_initial_stream_buffer(self, session: TerminalSession) -> None:
        """Drain frames that arrived between provider open and session registration."""
        try:
            timeout = 1.0
            while session.is_active:
                chunk = await self.read_output(session.session_id, 4096, timeout=timeout)
                if not chunk:
                    return
                await self._handle_output_chunk(session, chunk)
                timeout = 0.0
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("Initial stream drain failed for %s: %s", session.session_id, exc)

    async def ingest_provider_stream_frame(
        self,
        *,
        tenant_id: int,
        runner_id: object,
        task_id: int,
        provider_session_id: str,
        data: bytes,
    ) -> bool:
        """Append and fan out a pushed provider frame for an active terminal session."""
        del tenant_id, runner_id
        normalized_provider_session_id = str(provider_session_id or "").strip()
        if not normalized_provider_session_id:
            return False
        for session in list(self._registry.sessions.values()):
            if not session.is_active or int(session.task_id) != int(task_id):
                continue
            if session.exec_id != normalized_provider_session_id and session.session_id != normalized_provider_session_id:
                continue
            await self._handle_output_chunk(session, bytes(data))
            return True
        return False

    async def _handle_output_chunk(self, session: TerminalSession, chunk: bytes) -> None:
        """Update replay state and fan terminal output to active websocket listeners."""
        if not chunk:
            return
        session.update_activity()
        self._append_to_buffer(session, chunk)
        await self._fanout_chunk(session, chunk)

    async def _fanout_chunk(self, session: TerminalSession, chunk: bytes) -> None:
        if not session.listeners:
            return
        recipients = list(session.listeners)
        coros = []
        for websocket in recipients:
            try:
                coros.append(websocket.send_bytes(chunk))
            except Exception:
                try:
                    session.listeners.discard(websocket)
                except Exception:
                    pass
        if not coros:
            return
        results = await asyncio.gather(*coros, return_exceptions=True)
        for websocket, result in zip(recipients, results):
            if isinstance(result, Exception):
                try:
                    session.listeners.discard(websocket)
                except Exception:
                    pass

    def _append_to_buffer(self, session: TerminalSession, chunk: bytes) -> None:
        """Append PTY output to the replay buffer with size enforcement."""
        session.output_buffer.append(chunk)
        session.buffer_bytes += len(chunk)
        while session.buffer_bytes > session.max_buffer_bytes and session.output_buffer:
            removed = session.output_buffer.popleft()
            session.buffer_bytes -= len(removed)

    async def attach_websocket(self, session_id: str, websocket: Any) -> bool:
        """Attach a websocket to an existing user session and replay buffered output."""
        session = self._registry.get(session_id)
        if not session or not session.is_active:
            return False
        await self.cancel_disconnect_grace(session_id)
        session.listeners.add(websocket)
        try:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "session_created",
                        "session_id": session.session_id,
                        "session": session.to_dict(),
                    }
                )
            )
            try:
                max_frame = 16 * 1024
                buffer = bytearray()
                for chunk in session.output_buffer:
                    if not isinstance(chunk, (bytes, bytearray)):
                        continue
                    if len(buffer) + len(chunk) > max_frame and buffer:
                        try:
                            await websocket.send_bytes(bytes(buffer))
                        except Exception:
                            break
                        buffer.clear()
                    if len(chunk) > max_frame:
                        start = 0
                        while start < len(chunk):
                            end = min(start + max_frame, len(chunk))
                            try:
                                await websocket.send_bytes(chunk[start:end])
                            except Exception:
                                buffer.clear()
                                break
                            start = end
                        continue
                    buffer.extend(chunk)
                if buffer:
                    try:
                        await websocket.send_bytes(bytes(buffer))
                    except Exception:
                        pass
            except Exception:
                pass
            return True
        except Exception:
            try:
                session.listeners.discard(websocket)
            except Exception:
                pass
            return False

    async def detach_websocket(self, session_id: str, websocket: Any) -> None:
        """Detach a websocket listener from a session."""
        session = self._registry.get(session_id)
        if not session:
            return
        try:
            session.listeners.discard(websocket)
        except Exception:
            pass

    async def schedule_disconnect_grace(
        self,
        session_id: str,
        *,
        grace_seconds: float | None = None,
    ) -> None:
        """Schedule a delayed terminal close after the last websocket detaches."""
        session = self._registry.get(session_id)
        if not session or not session.is_active or session.listeners:
            return
        await self.cancel_disconnect_grace(session_id)
        delay = self.ws_disconnect_grace_seconds if grace_seconds is None else max(0.0, float(grace_seconds))

        async def _grace_close() -> None:
            try:
                await asyncio.sleep(delay)
                current = self._registry.get(session_id)
                if current and current.is_active and not current.listeners:
                    await self.close_session(session_id)
            except asyncio.CancelledError:
                pass

        self._grace_close_tasks[session_id] = asyncio.create_task(_grace_close())

    async def cancel_disconnect_grace(self, session_id: str) -> None:
        """Cancel a pending websocket disconnect grace close."""
        task = self._grace_close_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def resize_session(self, session_id: str, cols: int, rows: int) -> bool:
        """Resize an active PTY-backed terminal session."""
        try:
            session = self._registry.get(session_id)
            if not session or not session.is_active:
                return False

            session.update_activity()
            provider_ref = self._session_provider_ref(session)
            if provider_ref is None:
                return False
            payload: dict[str, Any] = {"cols": cols, "rows": rows}
            if session.socket is not None:
                payload["socket"] = session.socket
                payload["exec_id"] = session.exec_id
            else:
                payload["session_id"] = provider_ref
                if session.runtime_job_id:
                    payload["runtime_job_id"] = session.runtime_job_id
            try:
                await self._run_session_provider_operation(
                    session=session,
                    operation="resize_terminal_session",
                    call=lambda provider, request: provider.resize_terminal_session(request),
                    payload=payload,
                    metadata={"wait_for_result": True, "wait_timeout_seconds": 5.0},
                )
            except Exception as exc:
                logger.debug("PTY resize failed for %s: %s", session_id, exc)
            logger.debug("Terminal resize for session %s: %sx%s", session_id, cols, rows)
            return True
        except Exception as exc:
            logger.error("Error resizing session %s: %s", session_id, exc)
            return False

    async def close_session(self, session_id: str) -> bool:
        """Close and clean up a terminal session."""
        try:
            await self.cancel_disconnect_grace(session_id)
            session = self._registry.get(session_id)
            if not session:
                return False

            session.is_active = False

            if session.reader_task:
                try:
                    session.reader_task.cancel()
                except Exception:
                    pass
                session.reader_task = None

            if session.process:
                try:
                    session.process.terminate()
                    await asyncio.sleep(0.1)
                    if session.process.poll() is None:
                        session.process.kill()
                except Exception:
                    pass
                session.process = None

            try:
                provider_ref = self._session_provider_ref(session)
                payload: dict[str, Any]
                if session.socket is not None:
                    payload = {"socket": session.socket}
                    if session.exec_id:
                        payload["session_id"] = session.exec_id
                    if session.runtime_job_id:
                        payload["runtime_job_id"] = session.runtime_job_id
                elif provider_ref is not None:
                    payload = {"session_id": provider_ref}
                    if session.runtime_job_id:
                        payload["runtime_job_id"] = session.runtime_job_id
                else:
                    payload = {}
                await self._run_session_provider_operation(
                    session=session,
                    operation="close_terminal_session",
                    call=lambda provider, request: provider.close_terminal_session(request),
                    payload=payload,
                    metadata={"wait_for_result": True, "wait_timeout_seconds": 5.0},
                )
            except Exception:
                pass
            session.socket = None

            self._registry.remove(session_id)

            logger.info("Closed terminal session %s", session_id)
            return True
        except Exception as exc:
            logger.error("Error closing session %s: %s", session_id, exc)
            return False

    def get_session(self, session_id: str) -> Optional[TerminalSession]:
        """Get session by id."""
        return self._registry.get(session_id)

    def get_user_sessions(self, user_id: int) -> list[TerminalSession]:
        """Get all active sessions for a user."""
        return self._registry.get_user_sessions(user_id)

    def get_task_sessions(self, task_id: int) -> list[TerminalSession]:
        """Get all active sessions for a task."""
        return self._registry.get_task_sessions(task_id)

    async def close_task_sessions(self, task_id: int) -> int:
        """Close all active terminal sessions bound to one task id."""
        closed = 0
        session_ids = [session.session_id for session in self._registry.get_task_sessions(task_id)]
        for session_id in session_ids:
            if await self.close_session(session_id):
                closed += 1
        return closed

    async def close_sessions_for_tasks(self, task_ids: list[int] | tuple[int, ...] | set[int]) -> int:
        """Close all active terminal sessions for the provided task ids."""
        closed = 0
        for task_id in sorted({int(task_id) for task_id in task_ids}):
            closed += await self.close_task_sessions(task_id)
        return closed

    def get_session_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session information as a dictionary."""
        session = self._registry.get(session_id)
        return session.to_dict() if session else None

    def get_all_sessions_info(self) -> Dict[str, Any]:
        """Get information about all sessions."""
        return {
            "total_sessions": len(self.sessions),
            "active_sessions": len([session for session in self.sessions.values() if session.is_active]),
            "sessions_by_user": {},
        }

    async def get_or_create_agent_session(
        self,
        task_id: int,
        cols: int = 120,
        rows: int = 30,
        session_name: Optional[str] = None,
        reset: bool = False,
    ) -> TerminalSession:
        """Get an existing agent PTY session or create a new one."""
        session_id = self._build_agent_session_id(task_id, session_name=session_name)

        if reset and session_id in self.sessions:
            await self.close_session(session_id)

        if session_id in self.sessions:
            session = self.sessions[session_id]
            if session.is_active:
                session.update_activity()
                logger.debug(
                    "[PTY] Reusing existing agent session for task %s session_name=%s",
                    task_id,
                    session_name or "<canonical>",
                )
                return session
            await self.close_session(session_id)

        logger.info(
            "[PTY] Creating new agent session for task %s session_name=%s",
            task_id,
            session_name or "<canonical>",
        )
        return await self._create_agent_session(
            task_id,
            cols,
            rows,
            session_name=session_name,
        )

    async def prepare_agent_session(
        self,
        task_id: int,
        workspace_path: Optional[str] = None,
        cols: int = 120,
        rows: int = 30,
        session_name: Optional[str] = None,
        reset: bool = False,
    ) -> TerminalSession:
        """Get/create the agent session and apply one-time shell setup."""
        session = await self.get_or_create_agent_session(
            task_id=task_id,
            cols=cols,
            rows=rows,
            session_name=session_name,
            reset=reset,
        )
        if getattr(session, "_drowai_initialized", False):
            session.update_activity()
            return session

        await self._initialize_agent_session(
            session=session,
            workspace_path=workspace_path,
        )
        session._drowai_initialized = True
        session.update_activity()
        return session

    async def _initialize_agent_session(
        self,
        session: TerminalSession,
        workspace_path: Optional[str],
    ) -> None:
        """Apply prompt, cwd, and history settings for agent PTY sessions."""
        if not await self.send_input(session.session_id, f"export PS1='{AGENT_PROMPT_ENV} '\n".encode()):
            raise RuntimeError("Failed to initialize agent PTY prompt through provider I/O.")
        await asyncio.sleep(0.1)

        resolved_workspace = workspace_path or "/workspace"
        quoted_path = shlex.quote(resolved_workspace)
        if not await self.send_input(session.session_id, f"cd {quoted_path} 2>/dev/null || true\n".encode()):
            raise RuntimeError("Failed to initialize agent PTY workspace through provider I/O.")
        await asyncio.sleep(0.1)

        if not await self.send_input(session.session_id, b"unset HISTFILE\n"):
            raise RuntimeError("Failed to initialize agent PTY history through provider I/O.")
        await asyncio.sleep(0.1)

        try:
            await asyncio.wait_for(self._read_until_agent_prompt(session), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    async def _read_until_agent_prompt(
        self,
        session: TerminalSession,
        timeout_sec: float = 30.0,
    ) -> str:
        """Read session output until the configured agent prompt appears."""
        output = b""
        start_time = asyncio.get_event_loop().time()

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout_sec:
                return output.decode("utf-8", errors="replace")

            try:
                chunk = await self.read_output(session.session_id, 1024, timeout=0.5)
                if chunk:
                    output += chunk
                    decoded = output.decode("utf-8", errors="replace")
                    if AGENT_PROMPT_MARKER in decoded or AGENT_PROMPT_ENV in decoded:
                        return decoded
            except asyncio.TimeoutError:
                decoded = output.decode("utf-8", errors="replace")
                if AGENT_PROMPT_MARKER in decoded or AGENT_PROMPT_ENV in decoded:
                    return decoded
                continue
            except Exception:
                return output.decode("utf-8", errors="replace")

    async def _create_agent_session(
        self,
        task_id: int,
        cols: int,
        rows: int,
        session_name: Optional[str] = None,
    ) -> TerminalSession:
        """Create a new agent PTY session."""
        try:
            runtime_context = self._resolve_internal_runtime_context(
                task_id=task_id,
                session_name=session_name,
            )
            session_id = self._build_agent_session_id(task_id, session_name=session_name)
            db = SessionLocal()
            try:
                runtime_operations = RuntimeOperationService(db)
                status_result = await runtime_operations.run_for_context(
                    context=runtime_context,
                    operation="get_runtime_status",
                    call=lambda provider, request: provider.get_runtime_status(request),
                    metadata={"wait_for_result": True, "wait_timeout_seconds": 5.0},
                )
                container_status = status_result.metadata.get("delegate_result") if status_result.ok else "unknown"
                open_result = await runtime_operations.run_for_context(
                    context=runtime_context,
                    operation="open_terminal_session",
                    call=lambda provider, request: provider.open_terminal_session(request),
                    payload={"shell": "/bin/bash", "cols": cols, "rows": rows},
                    metadata={"wait_for_result": True, "wait_timeout_seconds": 5.0},
                )
            finally:
                db.close()
            if not self._is_runtime_accessible_status(container_status):
                raise Exception(
                    f"Runtime for task {task_id} not running (status: {container_status})"
                )
            if not open_result.ok:
                raise RuntimeError(open_result.error_message or "Failed to open terminal session")
            delegate = open_result.metadata.get("delegate_result")
            if not isinstance(delegate, dict):
                raise RuntimeError("Terminal provider returned invalid session metadata")
            provider_session_id = delegate.get("session_id")
            exec_id = delegate.get("exec_id") or provider_session_id
            runtime_job_id = delegate.get("runtime_job_id")
            sock = delegate.get("socket")
            container_name = str(delegate.get("container_name") or f"drowai-task-{task_id}")
            logger.debug("[PTY] Created PTY socket for agent session, type=%s", type(sock))
            if runtime_context.user_id is None:
                raise RuntimeError(
                    f"Internal runtime context for task {task_id} is missing user_id."
                )

            context_scope = getattr(
                runtime_context,
                "runtime_call_scope",
                RuntimeCallScope.PRODUCT_TASK,
            )
            session = TerminalSession(
                session_id=session_id,
                task_id=task_id,
                user_id=runtime_context.user_id,
                container_name=container_name,
                connection_type="docker_exec",
                exec_id=exec_id,
                runtime_job_id=str(runtime_job_id) if runtime_job_id else None,
                runtime_call_scope=getattr(context_scope, "value", str(context_scope)),
                socket=sock,
                session_type="agent",
            )

            self._registry.set(session)
            session.reader_task = None

            logger.info(
                "[PTY] Created agent session %s for task %s (no background reader)",
                session_id,
                task_id,
            )
            return session
        except Exception as exc:
            logger.error("[PTY] Failed to create agent session for task %s: %s", task_id, exc)
            raise

    def _resolve_internal_runtime_context(
        self,
        *,
        task_id: int,
        session_name: Optional[str],
    ):
        """Resolve internal runtime context for agent PTY/named session flows."""
        db = None
        try:
            db = SessionLocal()
            resolver = RuntimeProviderContextResolver(db)
            actor_suffix = session_name.strip() if isinstance(session_name, str) and session_name.strip() else "canonical"
            return resolver.resolve_internal_task_context(
                task_id=task_id,
                actor_type=RuntimeActorType.AGENT,
                actor_id=f"agent_session:{actor_suffix}",
            )
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    async def send_input(self, session_id: str, data: bytes | str) -> bool:
        """Send bytes or text to an active terminal session through provider I/O."""
        session = self._registry.get(session_id)
        provider_ref = self._session_provider_ref(session) if session else None
        if not session or not session.is_active or provider_ref is None:
            return False
        payload: dict[str, Any] = {"data": data}
        if session.socket is not None:
            payload["socket"] = session.socket
        else:
            payload["session_id"] = provider_ref
            if session.runtime_job_id:
                payload["runtime_job_id"] = session.runtime_job_id
        result = await self._run_session_provider_operation(
            session=session,
            operation="send_terminal_input",
            call=lambda provider, request: provider.send_terminal_input(request),
            payload=payload,
            metadata={"wait_for_result": True, "wait_timeout_seconds": 5.0},
        )
        if result.ok:
            session.update_activity()
        return result.ok

    async def read_output(
        self,
        session_id: str,
        size: int = 4096,
        *,
        timeout: float | None = None,
    ) -> bytes:
        """Read bytes from an active terminal session through provider I/O."""
        session = self._registry.get(session_id)
        provider_ref = self._session_provider_ref(session) if session else None
        if not session or not session.is_active or provider_ref is None:
            return b""
        payload: dict[str, Any] = {"size": size, "timeout": timeout}
        if session.socket is not None:
            payload["socket"] = session.socket
        else:
            payload["session_id"] = provider_ref
            payload["cursor"] = session.output_cursor
            if session.runtime_job_id:
                payload["runtime_job_id"] = session.runtime_job_id
        result = await self._run_session_provider_operation(
            session=session,
            operation="read_terminal_output",
            call=lambda provider, request: provider.read_terminal_output(request),
            payload=payload,
        )
        if not result.ok:
            return b""
        delegate = result.metadata.get("delegate_result")
        if isinstance(delegate, dict):
            next_cursor = delegate.get("next_cursor", delegate.get("cursor"))
            if next_cursor is not None:
                try:
                    session.output_cursor = int(next_cursor)
                except (TypeError, ValueError):
                    pass
            data = delegate.get("data", b"")
            if isinstance(data, bytes):
                session.update_activity()
                return data
            if isinstance(data, str):
                session.update_activity()
                return data.encode()
        return b""

    async def _run_session_provider_operation(
        self,
        *,
        session: TerminalSession,
        operation: str,
        call,
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        """Resolve provider context from a live session and dispatch operation."""
        db = SessionLocal()
        try:
            runtime_operations = RuntimeOperationService(db)
            context = runtime_operations.context_for_internal_task(
                task_id=session.task_id,
                actor_type=RuntimeActorType.AGENT
                if session.session_type == "agent"
                else RuntimeActorType.USER,
                actor_id=f"{session.session_type}_terminal:{session.session_id}",
                user_id=session.user_id,
                runtime_call_scope=session.runtime_call_scope,
            )
            return await runtime_operations.run_for_context(
                context=context,
                operation=operation,
                call=call,
                payload=payload,
                metadata=metadata,
            )
        finally:
            db.close()

    @staticmethod
    def _session_provider_ref(session: TerminalSession | None) -> Any | None:
        if session is None:
            return None
        if session.socket is not None:
            return session.socket
        return session.exec_id

    @staticmethod
    def _is_runtime_accessible_status(status_payload: Any) -> bool:
        if isinstance(status_payload, str):
            return status_payload in {"running", "paused"}
        if isinstance(status_payload, dict):
            container_status = str(status_payload.get("container_status") or "").lower()
            job_status = str(status_payload.get("job_status") or "").lower()
            if container_status in {"running", "paused"}:
                return True
            if job_status in {"running", "paused"}:
                return True
        return False

    async def attach_agent_listener(
        self,
        task_id: int,
        websocket: Any,
    ) -> bool:
        """Attach a websocket listener to an agent PTY session."""
        session_id = build_agent_session_id(task_id)
        session = self._registry.get(session_id)

        if not session or not session.is_active:
            logger.warning(
                "[PTY] Cannot attach listener: agent session %s not found or inactive",
                session_id,
            )
            return False

        if session.session_type != "agent":
            logger.warning(
                "[PTY] Cannot attach agent listener: session %s is not an agent session",
                session_id,
            )
            return False

        if session.reader_task is None or session.reader_task.done():
            logger.info(
                "[PTY] Starting background reader for agent session %s (WebSocket attached)",
                session_id,
            )
            session.reader_task = asyncio.create_task(self._pty_reader(session))

        session.listeners.add(websocket)
        logger.info("[PTY] Attached listener to agent session %s", session_id)

        try:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "agent_session_attached",
                        "session_id": session.session_id,
                        "task_id": task_id,
                    }
                )
            )

            if session.output_buffer:
                max_frame = 16 * 1024
                buffer = bytearray()
                for chunk in session.output_buffer:
                    if not isinstance(chunk, (bytes, bytearray)):
                        continue
                    if len(buffer) + len(chunk) > max_frame and buffer:
                        try:
                            await websocket.send_bytes(bytes(buffer))
                        except Exception:
                            break
                        buffer.clear()
                    buffer.extend(chunk)

                if buffer:
                    try:
                        await websocket.send_bytes(bytes(buffer))
                    except Exception:
                        pass
        except Exception as exc:
            logger.error("[PTY] Error sending agent session info: %s", exc)
            session.listeners.discard(websocket)
            return False

        return True

    def record_agent_command(
        self,
        task_id: int,
        command: str,
        session_name: Optional[str] = None,
    ) -> None:
        """Record a command in the agent session audit trail."""
        session_id = self._build_agent_session_id(task_id, session_name=session_name)
        session = self._registry.get(session_id)

        if session and session.session_type == "agent":
            session.command_history.append(
                {
                    "command": command,
                    "timestamp": format_iso(utc_now()),
                }
            )
            session.last_command_timestamp = utc_now()
            logger.debug(
                "[PTY] Recorded command in agent session %s: %s",
                session_id,
                command[:100],
            )

    @staticmethod
    def _build_agent_session_id(task_id: int, *, session_name: Optional[str] = None) -> str:
        """Return canonical or named agent PTY session id for ``task_id``."""
        if session_name:
            return build_named_agent_session_id(task_id, session_name)
        return build_agent_session_id(task_id)

    async def cleanup_all_sessions(self) -> None:
        """Cleanup all sessions and stop background maintenance."""
        session_ids = list(self.sessions.keys())
        for session_id in session_ids:
            await self.close_session(session_id)

        await self._registry.stop_cleanup_loop()


terminal_session_manager = TerminalSessionManager()

try:
    from backend.services.runner_control.terminal_stream_registry import get_runner_terminal_stream_registry

    get_runner_terminal_stream_registry().register_frame_sink(
        terminal_session_manager.ingest_provider_stream_frame
    )
except Exception:
    logger.debug("Cloud terminal stream frame sink registration skipped.", exc_info=True)
