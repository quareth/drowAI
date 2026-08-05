"""Adapter that bridges LangGraph nodes to the existing agent executor."""

from __future__ import annotations

import inspect
import os
from dataclasses import dataclass
from time import monotonic
from typing import Any, Dict, Optional

from pydantic import ValidationError

from agent.config import AgentConfig
from agent.executor import EnhancedCommandExecutor
from agent.logger import AgentLogger
from agent.models import Action, ActionType, ExecutionResult, ExecutionStrategy
from agent.planner import ScopeParser
from agent.scope_validator import ScopeValidator
from agent.tool_runtime.pty_identity import derive_parallel_pty_identity
from agent.tool_runtime.backend_tool_policy import (
    SHELL_EXEC_TOOL_ID,
    SHELL_WRITE_STDIN_TOOL_ID,
)
from agent.tool_runtime.timeout_policy import ToolTimeoutPlan, resolve_tool_timeout_plan
from agent.utils.output_processing import smart_truncate, classify_output_type
from agent.utils.truncation_config import (
    STDERR_SNIPPET,
    get_threshold_for_type,
    should_suggest_file_reading,
)

try:
    from agent.communication.file_comm import FileCommAgent
except ImportError:  # pragma: no cover - defensive guard for packaging
    FileCommAgent = None  # type: ignore

from ..infrastructure.state_models import GraphRuntimeContext
from ..state import InteractiveState
from ..subgraphs.tool_execution_runtime.lane_dispatch import (
    ToolCallDispatchInput,
    ToolLaneDispatchDecision,
    dispatch_tool_call_by_lane,
    missing_runtime_placement_payload,
    resolve_tool_lane_dispatch,
    runner_unsupported_tool_payload,
)
from ..subgraphs.tool_execution_runtime.runner_command_orchestration import (
    execute_runner_container_tool_via_provider,
)
from ..utils.llm_resolver import DEFAULT_MODEL
from .tool_interface import ToolInterface, normalize_tool_arguments
from runtime_shared.shell_session_contracts import (
    ShellExecRequest,
    ShellProcessStatus,
    ShellSessionErrorCode,
    ShellSessionIdentity,
    ShellSessionUpdate,
    ShellWriteRequest,
)
from runtime_shared.shell_session_port import get_shell_session_service

from backend.services.runtime_provider.contracts import (
    RuntimeActorType,
    RuntimePlacementMode,
)
from backend.services.runtime_provider.registry import RuntimeProviderRegistry


@dataclass(slots=True)
class _StaticToolInterface(ToolInterface):
    """Minimal ToolInterface implementation for compatibility."""

    parameters: Dict[str, Any]

    def get_args_for_non_tool_llm(
        self,
        query: str,
        history,
        llm,
    ) -> Dict[str, Any]:
        return dict(self.parameters)

    def run(self, **kwargs: Any):
        return iter(())

    def final_result(self, *responses: str) -> Dict[str, Any]:
        return {"observation": "\n".join(responses)}

    def build_next_prompt(self, result: Dict[str, Any]) -> Optional[str]:
        return None


class GraphToolExecutor:
    """Helper that reuses EnhancedCommandExecutor for LangGraph tool calls.
    
    Uses centralized truncation configuration from truncation_config.py for
    type-aware output truncation with soft margins.
    """

    def __init__(self, executor: Optional[EnhancedCommandExecutor] = None) -> None:
        self._provided_executor = executor
        self._executor_cache: Dict[str, EnhancedCommandExecutor] = {}
        self._logger_cache: Dict[str, AgentLogger] = {}
        self._scope_cache: Dict[str, ScopeValidator] = {}

    def create_tool_request(self, state: InteractiveState) -> Dict[str, Any]:
        facts = state.facts
        tool_id = facts.selected_tool
        candidates = facts.tool_candidates or facts.tool_ids
        if not tool_id and candidates:
            tool_id = candidates[0]
        if not tool_id:
            raise ValueError("No candidate tool available for execution.")

        parameters = dict(facts.tool_parameters.get(tool_id, {}))
        parameters = normalize_tool_arguments(parameters)

        if "target" not in parameters:
            hints = facts.intent_hints or {}
            targets = hints.get("targets") or []
            if targets:
                parameters["target"] = targets[0]
        
        # DR.7.3 & DR.7.4: Optimize parameters before execution
        metadata = facts.ensure_metadata()
        findings = []
        observations = state.trace.observations or []
        
        # Extract findings from synthesized output and executed tools
        synthesized = metadata.get("synthesized_output") or {}
        if synthesized:
            findings.append(synthesized)
        
        # Extract from executed tools
        for tool_record in state.trace.executed_tools or []:
            if hasattr(tool_record, "observation") and tool_record.observation:
                findings.append({"type": "tool_output", "content": tool_record.observation})
        
        # Extract from observations
        for obs in observations:
            findings.append({"type": "observation", "content": obs})
        
        from ..utils.tool_optimization import optimize_tool_parameters
        
        parameters = optimize_tool_parameters(
            tool_id, parameters, findings, observations, metadata
        )
        
        # DR.7.2: Check for redundant execution
        from ..utils.tool_optimization import (
            ToolExecution,
            check_redundant_execution,
        )
        
        execution_history_data = metadata.get("tool_execution_history", [])
        execution_history = [
            ToolExecution.from_dict(exec_data) if isinstance(exec_data, dict) else exec_data
            for exec_data in execution_history_data
        ]
        
        redundancy_reason = check_redundant_execution(
            tool_id, parameters, execution_history
        )
        
        if redundancy_reason:
            # Store redundancy warning in metadata for router to see
            metadata["redundant_execution_warning"] = redundancy_reason
            facts.metadata = metadata
            # Note: We don't block execution here, just warn - router can decide

        runtime_context = facts.safe_metadata.get("graph_runtime_context") or {}
        reasoning = ""
        if state.trace.reasoning:
            reasoning = str(state.trace.reasoning[-1])
        elif isinstance(facts.metadata.get("tool_reasoning"), str):
            reasoning = str(facts.metadata["tool_reasoning"])

        request: Dict[str, Any] = {
            "tool": tool_id,
            "parameters": parameters,
            "capability": facts.capability,
            "task_id": facts.task_id,
            "conversation_id": facts.conversation_id,
            "workspace_path": runtime_context.get("workspace_path"),
            "provider": facts.safe_metadata.get("provider") or runtime_context.get("provider"),
            "model": facts.safe_metadata.get("model") or runtime_context.get("model"),
            "credential_ref": facts.safe_metadata.get("credential_ref")
            or runtime_context.get("credential_ref"),
            "llm_runtime_selection": facts.safe_metadata.get("llm_runtime_selection"),
            "reasoning_effort": facts.safe_metadata.get("reasoning_effort")
            or runtime_context.get("reasoning_effort"),
            "reasoning": reasoning,
            "expected_outcome": facts.safe_metadata.get("expected_outcome", ""),
            "targets": list((facts.intent_hints or {}).get("targets") or []),
        }
        target = request["parameters"].get("target")
        if not target and request["targets"]:
            request["target"] = request["targets"][0]
        elif target:
            request["target"] = target
        return request

    def get_tool_interface(self, state: InteractiveState) -> ToolInterface:
        facts = state.facts
        tool_id = facts.selected_tool or (facts.tool_candidates or facts.tool_ids or [None])[0]
        parameters = dict(facts.tool_parameters.get(tool_id, {})) if tool_id else {}
        return _StaticToolInterface(normalize_tool_arguments(parameters))

    def ensure_can_execute(self, state: InteractiveState) -> None:
        facts = state.facts
        cancellation = getattr(facts, "cancellation", None)
        if cancellation and getattr(cancellation, "cancelled", False):
            raise RuntimeError("Tool execution aborted: cooperative cancellation requested.")

        budgets = getattr(facts, "runtime_budgets", None)
        if budgets:
            remaining_time = getattr(budgets, "time_budget_ms", None)
            if remaining_time is not None and remaining_time <= 0:
                raise RuntimeError("Tool execution aborted: time budget exhausted.")
            remaining_calls = getattr(budgets, "remaining_tool_calls", None)
            if remaining_calls is not None and remaining_calls <= 0:
                raise RuntimeError("Tool execution aborted: tool-call budget exhausted.")

    async def execute_tool(
        self,
        request: Dict[str, Any],
        *,
        context: Optional[GraphRuntimeContext] = None,
    ) -> Dict[str, Any]:
        tool_id = request["tool"]
        parameters = dict(request.get("parameters", {}))
        workspace_path = request.get("workspace_path") or (context.workspace_path if context else None)
        task_id = request.get("task_id") or (context.task_id if context else None)
        model = request.get("model") or (context.model if context else None)
        execution_strategy = str(request.get("execution_strategy") or "").strip().lower()
        tool_call_id = request.get("tool_call_id")
        tool_batch_id = request.get("tool_batch_id")
        runtime_placement_mode_raw = request.get("runtime_placement_mode") or (
            context.runtime_placement_mode if context else None
        )
        try:
            dispatch_decision = resolve_tool_lane_dispatch(
                tool_id=str(tool_id),
                runtime_placement_mode=runtime_placement_mode_raw,
            )
        except ValueError as exc:
            return missing_runtime_placement_payload(
                tool_id=str(tool_id),
                message=str(exc),
            )
        unsupported_payload = runner_unsupported_tool_payload(decision=dispatch_decision)
        if unsupported_payload is not None:
            return unsupported_payload
        runtime_placement_mode = dispatch_decision.runtime_placement_mode
        requires_local_executor = dispatch_decision.authority not in {
            "container_runner_transport",
            "runtime_session_control",
        }
        is_parallel_call = execution_strategy == "parallel"
        parallel_pty_identity = (
            derive_parallel_pty_identity(
                tool_batch_id=tool_batch_id,
                tool_call_id=tool_call_id,
            )
            if is_parallel_call
            else None
        )
        allow_pty = True
        if is_parallel_call and parallel_pty_identity is None:
            allow_pty = False

        executor: Optional[EnhancedCommandExecutor] = None

        def _ensure_local_executor() -> EnhancedCommandExecutor:
            nonlocal executor
            if executor is None:
                executor = self._get_executor(
                    workspace_path,
                    task_id,
                    model,
                    runtime_placement_mode=runtime_placement_mode,
                )
            return executor

        timeout_config = None
        if requires_local_executor:
            timeout_config = getattr(_ensure_local_executor(), "config", None)
        elif self._provided_executor is not None:
            timeout_config = getattr(self._provided_executor, "config", None)
        timeout_plan = ToolTimeoutPlan.from_metadata(
            request.get("timeout_plan"),
            normalized_parameters=parameters,
        )
        if timeout_plan is None or timeout_plan.tool_id != str(tool_id):
            timeout_plan = resolve_tool_timeout_plan(
                tool_id=tool_id,
                parameters=parameters,
                config=timeout_config,
            )
        parameters = dict(timeout_plan.normalized_parameters)
        action = self._build_action(request, parameters)
        if requires_local_executor:
            _ensure_local_executor()._last_action = action
        elif self._provided_executor is not None:
            self._provided_executor._last_action = action

        approval_reason: Optional[str] = None
        approval_metadata: Dict[str, Any] = {}
        approved = True
        approval_executor = None
        if requires_local_executor:
            approval_executor = _ensure_local_executor()
        elif self._provided_executor is not None:
            approval_executor = self._provided_executor
        if approval_executor is not None and hasattr(approval_executor, "_maybe_request_approval"):
            approved = await approval_executor._maybe_request_approval(
                tool_id,
                parameters,
                action.reasoning,
            )
            if not approved:
                approval_reason = "user_rejected"

        if not approved:
            return {
                "tool": tool_id,
                "success": False,
                "stdout": "",
                "stderr": "Execution skipped: proposal rejected by user.",
                "stdout_excerpt": "",
                "stderr_excerpt": "Execution skipped: proposal rejected by user.",
                "exit_code": -1,
                "observation": "Tool execution skipped due to rejection.",
                "approval_granted": False,
                "approval_reason": approval_reason,
                "approval_metadata": approval_metadata,
                "duration": 0.0,
                "metadata": {},
                "status": "rejected",
            }

        dispatch_input = ToolCallDispatchInput(
            tool_id=str(tool_id),
            normalized_parameters=dict(parameters),
            timeout_plan=timeout_plan,
            tool_call_id=tool_call_id,
            tool_batch_id=tool_batch_id,
            runtime_placement_mode=runtime_placement_mode,
            tenant_id=request.get("tenant_id") or (context.tenant_id if context else None),
            task_id=request.get("task_id") or (context.task_id if context else None),
            execution_owner_id=request.get("execution_owner_id")
            or (context.execution_owner_id if context else None),
            runtime_metadata={
                "workspace_id": request.get("workspace_id")
                or (context.workspace_id if context else None),
                "runner_id": request.get("runner_id") or (context.runner_id if context else None),
                "execution_site_id": request.get("execution_site_id")
                or (context.execution_site_id if context else None),
                "workspace_path": workspace_path,
            },
        )
        return await dispatch_tool_call_by_lane(
            dispatch_input=dispatch_input,
            execute_local=lambda decision, lane_input: self._execute_local_tool_call(
                executor=_ensure_local_executor(),
                request=request,
                decision=decision,
                dispatch_input=lane_input,
                timeout_plan=timeout_plan,
                parallel_pty_identity=parallel_pty_identity,
                allow_pty=allow_pty,
                approval_metadata=approval_metadata,
            ),
            execute_runner=lambda decision, lane_input: self._execute_runner_container_tool_via_provider(
                request=request,
                parameters=dict(lane_input.normalized_parameters),
                timeout_plan=timeout_plan,
                context=context,
                decision=decision,
                workspace_path=workspace_path,
                parallel_pty_identity=parallel_pty_identity,
                allow_pty=allow_pty,
            ),
            execute_session=lambda decision, lane_input: self._execute_runtime_session_tool_call(
                request=request,
                decision=decision,
                dispatch_input=lane_input,
            ),
        )

    async def _execute_runtime_session_tool_call(
        self,
        *,
        request: Dict[str, Any],
        decision: ToolLaneDispatchDecision,
        dispatch_input: ToolCallDispatchInput,
    ) -> Dict[str, Any]:
        """Execute a shell-session tool call through the runtime-shared service port."""
        identity = self._build_shell_session_identity(dispatch_input=dispatch_input)
        if identity is None:
            missing = "tenant_id, task_id, execution_owner_id, workspace_id"
            message = (
                "tool execution runtime context is missing shell-session identity "
                f"field(s): {missing}."
            )
            return self._shell_session_error_payload(
                tool_id=str(request["tool"]),
                message=message,
                status="missing_shell_session_identity",
                error_code="missing_shell_session_identity",
                decision=decision,
                dispatch_input=dispatch_input,
            )

        try:
            session_request = self._build_shell_session_request(
                tool_id=str(request["tool"]),
                parameters=dispatch_input.normalized_parameters,
            )
        except ValidationError as exc:
            message = f"Invalid shell-session request: {exc}"
            return self._shell_session_error_payload(
                tool_id=str(request["tool"]),
                message=message,
                status="validation_error",
                error_code="validation_error",
                decision=decision,
                dispatch_input=dispatch_input,
            )
        except ValueError as exc:
            message = str(exc)
            return self._shell_session_error_payload(
                tool_id=str(request["tool"]),
                message=message,
                status="unsupported_shell_session_tool",
                error_code="unsupported_shell_session_tool",
                decision=decision,
                dispatch_input=dispatch_input,
            )

        service = get_shell_session_service()
        if isinstance(session_request, ShellExecRequest):
            update = await service.execute(identity=identity, request=session_request)
        else:
            update = await service.write_stdin(identity=identity, request=session_request)

        return self._shell_session_update_payload(
            tool_id=str(request["tool"]),
            update=update,
            decision=decision,
            dispatch_input=dispatch_input,
        )

    @staticmethod
    def _build_shell_session_identity(
        *,
        dispatch_input: ToolCallDispatchInput,
    ) -> ShellSessionIdentity | None:
        """Build service authority context from serializable graph runtime metadata."""
        runtime_metadata = dispatch_input.runtime_metadata or {}
        workspace_id = runtime_metadata.get("workspace_id")
        execution_owner_id = dispatch_input.execution_owner_id
        if (
            dispatch_input.tenant_id is None
            or dispatch_input.task_id is None
            or not isinstance(execution_owner_id, str)
            or not execution_owner_id.strip()
            or not isinstance(workspace_id, str)
            or not workspace_id.strip()
            or dispatch_input.runtime_placement_mode not in {"local", "runner"}
        ):
            return None

        workspace_path = runtime_metadata.get("workspace_path")
        if not isinstance(workspace_path, str):
            workspace_path = None
        runner_id = runtime_metadata.get("runner_id")
        if not isinstance(runner_id, str):
            runner_id = None
        execution_site_id = runtime_metadata.get("execution_site_id")
        if not isinstance(execution_site_id, str):
            execution_site_id = None

        return ShellSessionIdentity(
            tenant_id=int(dispatch_input.tenant_id),
            task_id=int(dispatch_input.task_id),
            execution_owner_id=execution_owner_id.strip(),
            runtime_placement_mode=dispatch_input.runtime_placement_mode,
            workspace_id=workspace_id.strip(),
            workspace_path=workspace_path,
            runner_id=runner_id,
            execution_site_id=execution_site_id,
        )

    @staticmethod
    def _build_shell_session_request(
        *,
        tool_id: str,
        parameters: Any,
    ) -> ShellExecRequest | ShellWriteRequest:
        """Convert normalized graph parameters into a typed shell-session request."""
        normalized = dict(parameters or {})
        if tool_id == SHELL_EXEC_TOOL_ID:
            return ShellExecRequest(**normalized)
        if tool_id == SHELL_WRITE_STDIN_TOOL_ID:
            return ShellWriteRequest(**normalized)
        raise ValueError(f"Unsupported shell-session tool `{tool_id}`.")

    @staticmethod
    def _shell_session_error_payload(
        *,
        tool_id: str,
        message: str,
        status: str,
        error_code: str,
        decision: ToolLaneDispatchDecision,
        dispatch_input: ToolCallDispatchInput,
    ) -> Dict[str, Any]:
        """Return a structured shell-session adapter failure payload."""
        update = ShellSessionUpdate(
            success=False,
            status="error",
            process_status=None,
            session_id=None,
            stdout="",
            stderr=message,
            exit_code=None,
            stdin_available=False,
            truncated=False,
            duration_ms=0,
            summary=message,
            error_code=(
                ShellSessionErrorCode(error_code)
                if error_code in {code.value for code in ShellSessionErrorCode}
                else None
            ),
        )
        payload = GraphToolExecutor._shell_session_update_payload(
            tool_id=tool_id,
            update=update,
            decision=decision,
            dispatch_input=dispatch_input,
        )
        payload["status"] = status
        payload["metadata"]["error_code"] = error_code
        return payload

    @staticmethod
    def _shell_session_update_payload(
        *,
        tool_id: str,
        update: ShellSessionUpdate,
        decision: ToolLaneDispatchDecision,
        dispatch_input: ToolCallDispatchInput,
    ) -> Dict[str, Any]:
        """Map a shell-session update to the graph tool-result payload shape."""
        process_status = (
            update.process_status.value
            if isinstance(update.process_status, ShellProcessStatus)
            else update.process_status
        )
        error_code = (
            update.error_code.value
            if isinstance(update.error_code, ShellSessionErrorCode)
            else update.error_code
        )
        stdout = update.stdout or ""
        stderr = update.stderr or ""
        stdout_excerpt = stdout
        stderr_excerpt = stderr[:STDERR_SNIPPET]
        summary = update.summary.strip() or (
            stdout_excerpt or stderr_excerpt or "Shell session update completed."
        )
        duration = max(0.0, update.duration_ms / 1000.0)
        metadata: Dict[str, Any] = {
            "route_policy": {
                "selected_lane": decision.lane,
                "selected_authority": decision.authority,
            },
            "runtime_session": {
                "tool_call_id": str(dispatch_input.tool_call_id or ""),
                "tool_batch_id": str(dispatch_input.tool_batch_id or ""),
                "authority": decision.authority,
                "process_status": process_status,
                "session_id": update.session_id,
                "stdin_available": update.stdin_available,
                "truncated": update.truncated,
                "error_code": error_code,
            },
        }
        if error_code:
            metadata["error_code"] = error_code

        status = "success" if update.success else "failed"
        if error_code == ShellSessionErrorCode.SHELL_RUNTIME_UNAVAILABLE.value:
            status = ShellSessionErrorCode.SHELL_RUNTIME_UNAVAILABLE.value
        elif update.process_status is ShellProcessStatus.TIMED_OUT:
            status = "failed"

        return {
            "tool": tool_id,
            "success": bool(update.success),
            "stdout": stdout,
            "stderr": stderr,
            "stdout_excerpt": stdout_excerpt,
            "stderr_excerpt": stderr_excerpt,
            "exit_code": update.exit_code,
            "observation": summary,
            "approval_granted": True,
            "approval_reason": None,
            "approval_metadata": {},
            "duration": duration,
            "metadata": metadata,
            "status": status,
            "process_status": process_status,
            "session_id": update.session_id,
            "stdin_available": update.stdin_available,
            "truncated": update.truncated,
            "summary": summary,
            "error_code": error_code,
        }

    async def _execute_local_tool_call(
        self,
        *,
        executor: EnhancedCommandExecutor,
        request: Dict[str, Any],
        decision: ToolLaneDispatchDecision,
        dispatch_input: ToolCallDispatchInput,
        timeout_plan: ToolTimeoutPlan,
        parallel_pty_identity: Any,
        allow_pty: bool,
        approval_metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute a non-runner authority tool call via the local executor path."""
        tool_id = dispatch_input.tool_id
        parameters = dict(dispatch_input.normalized_parameters)
        started = monotonic()
        try:
            execute_kwargs = {
                "interrupt_id": request.get("interrupt_id"),
                "tool_call_id": dispatch_input.tool_call_id,
                "tool_batch_id": dispatch_input.tool_batch_id,
                "session_name": parallel_pty_identity.session_name if parallel_pty_identity else None,
                "cleanup_session": bool(parallel_pty_identity),
                "artifact_stamp": (
                    parallel_pty_identity.artifact_stamp if parallel_pty_identity else None
                ),
                "allow_pty": allow_pty,
            }
            try:
                signature = inspect.signature(executor._execute_single_tool)
                accepts_timeout_plan = "timeout_plan" in signature.parameters
            except (TypeError, ValueError):
                accepts_timeout_plan = True
            if accepts_timeout_plan:
                execute_kwargs["timeout_plan"] = timeout_plan
            result: ExecutionResult = await executor._execute_single_tool(
                tool_id,
                parameters,
                **execute_kwargs,
            )
        except Exception as exc:  # pragma: no cover - defensive guard
            duration = monotonic() - started
            message = f"Tool execution failed: {exc}"
            return {
                "tool": tool_id,
                "success": False,
                "stdout": "",
                "stderr": message,
                "stdout_excerpt": "",
                "stderr_excerpt": message[:STDERR_SNIPPET],
                "exit_code": -1,
                "observation": message,
                "approval_granted": True,
                "approval_reason": None,
                "approval_metadata": {},
                "duration": duration,
                "metadata": {
                    "route_policy": {
                        "selected_lane": decision.lane,
                        "selected_authority": decision.authority,
                    }
                },
                "status": "error",
            }

        duration = monotonic() - started
        stdout = result.stdout or ""
        stderr = result.stderr or ""

        # Classify output type for intelligent truncation thresholds
        command = parameters.get("command", "") if parameters else ""
        output_type = classify_output_type(
            tool_name=tool_id,
            command=command,
            output=stdout,
        )
        
        # Get type-aware truncation limit
        stdout_limit = get_threshold_for_type(output_type)
        
        # Track original lengths for chars_truncated calculation
        original_stdout_len = len(stdout)
        original_stderr_len = len(stderr)

        # Use head+tail truncation with type-aware limits and soft margins
        stdout_excerpt, stdout_truncated = smart_truncate(
            stdout,
            total_limit=stdout_limit,
            output_type=output_type,
            return_was_truncated=True,
        )
        stderr_excerpt, stderr_truncated = smart_truncate(
            stderr,
            total_limit=STDERR_SNIPPET,
            return_was_truncated=True,
        )

        observation = stdout_excerpt or stderr_excerpt or "Tool completed without output."

        metadata = getattr(result, "metadata", {}) or {}
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        metadata.setdefault(
            "route_policy",
            {
                "selected_lane": decision.lane,
                "selected_authority": decision.authority,
            },
        )
        metadata.setdefault("timeout_policy", timeout_plan.to_metadata())
        validation_errors = getattr(result, "validation_errors", None)
        command_text = getattr(result, "command_text", None)
        if not isinstance(command_text, str):
            command_text = None

        failure_category = (
            str(metadata.get("failure_category"))
            if isinstance(metadata.get("failure_category"), str)
            else ""
        )
        status = "success" if result.success else (failure_category or "error")
        if validation_errors:
            status = "validation_error"
            metadata = dict(metadata)
            metadata.setdefault("validation_errors", validation_errors)

        # Combined truncation flag for prompt builders
        was_truncated = stdout_truncated or stderr_truncated
        
        # Calculate total chars truncated for informational messaging
        chars_truncated = 0
        if stdout_truncated:
            chars_truncated += original_stdout_len - len(stdout_excerpt)
        if stderr_truncated:
            chars_truncated += original_stderr_len - len(stderr_excerpt)
        
        # Determine if file reading should be suggested (only for large truncations)
        suggest_file_reading = should_suggest_file_reading(chars_truncated)

        return {
            "tool": tool_id,
            "success": bool(result.success),
            "stdout": stdout,
            "stderr": stderr,
            "stdout_excerpt": stdout_excerpt,
            "stderr_excerpt": stderr_excerpt,
            "exit_code": result.exit_code,
            "observation": observation,
            "approval_granted": True,
            "approval_reason": None,
            "approval_metadata": approval_metadata,
            "duration": duration,
            "metadata": metadata,
            "validation_errors": validation_errors,
            "status": status,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "was_truncated": was_truncated,
            "chars_truncated": chars_truncated,
            "output_type": output_type,
            "suggest_file_reading": suggest_file_reading,
            "artifacts": list(getattr(result, "artifacts", []) or []),
            "command_text": command_text,
        }

    @staticmethod
    def _resolve_runtime_actor_type(value: Any) -> RuntimeActorType:
        raw = str(value or RuntimeActorType.AGENT.value).strip().lower()
        try:
            return RuntimeActorType(raw)
        except Exception:
            return RuntimeActorType.AGENT

    async def _execute_runner_container_tool_via_provider(
        self,
        *,
        request: Dict[str, Any],
        parameters: Dict[str, Any],
        timeout_plan: ToolTimeoutPlan,
        context: Optional[GraphRuntimeContext],
        decision: ToolLaneDispatchDecision,
        workspace_path: Optional[str],
        parallel_pty_identity: Any,
        allow_pty: bool,
    ) -> Dict[str, Any]:
        return await execute_runner_container_tool_via_provider(
            request=request,
            parameters=parameters,
            timeout_plan=timeout_plan,
            context=context,
            decision=decision,
            workspace_path=workspace_path,
            parallel_pty_identity=parallel_pty_identity,
            allow_pty=allow_pty,
            get_executor=self._get_executor,
            resolve_runtime_actor_type=self._resolve_runtime_actor_type,
            get_provider=RuntimeProviderRegistry().get_provider,
        )

    def _get_executor(
        self,
        workspace_path: Optional[str],
        task_id: Optional[int],
        model: Optional[str] = None,
        *,
        runtime_placement_mode: str = "local",
        ignore_provided: bool = False,
    ) -> EnhancedCommandExecutor:
        if self._provided_executor and not ignore_provided:
            return self._provided_executor

        key = f"{workspace_path or '__default__'}::{runtime_placement_mode}"
        if key in self._executor_cache:
            return self._executor_cache[key]

        default_workspace_path = os.getenv("WORKSPACE")
        if not default_workspace_path and runtime_placement_mode != RuntimePlacementMode.RUNNER.value:
            default_workspace_path = "/workspace"

        config = AgentConfig(
            task_id=str(task_id) if task_id is not None else None,
            workspace_path=workspace_path or default_workspace_path or os.getcwd(),
            model_name=model or DEFAULT_MODEL,
        )
        config.runtime_placement_mode = runtime_placement_mode
        config.artifacts_dir = os.path.join(config.workspace_path, "artifacts")

        logger = self._get_logger(key, task_id, config.workspace_path)

        # Ensure workspace is available for logs/artifacts
        try:
            os.makedirs(config.artifacts_dir, exist_ok=True)
        except Exception:
            pass

        executor = EnhancedCommandExecutor(config, logger)
        if (
            workspace_path
            and FileCommAgent is not None
            and runtime_placement_mode != RuntimePlacementMode.RUNNER.value
        ):
            try:
                executor.set_file_comm(FileCommAgent(workspace_path))
            except Exception:
                pass

        scope_validator = self._build_scope_validator(workspace_path, logger)
        if scope_validator:
            try:
                executor.set_scope_validator(scope_validator)
            except Exception:
                pass

        self._executor_cache[key] = executor
        return executor

    def _get_logger(
        self,
        cache_key: str,
        task_id: Optional[int],
        workspace_path: str,
    ) -> AgentLogger:
        if cache_key in self._logger_cache:
            return self._logger_cache[cache_key]

        previous_workspace = os.environ.get("WORKSPACE")
        os.environ["WORKSPACE"] = workspace_path
        try:
            logger = AgentLogger(str(task_id or "langgraph"))
        finally:
            if previous_workspace is not None:
                os.environ["WORKSPACE"] = previous_workspace
            else:
                os.environ.pop("WORKSPACE", None)

        self._logger_cache[cache_key] = logger
        return logger

    def _build_scope_validator(
        self,
        workspace_path: Optional[str],
        logger: AgentLogger,
    ) -> Optional[ScopeValidator]:
        if not workspace_path:
            return None

        cache_key = workspace_path
        if cache_key in self._scope_cache:
            return self._scope_cache[cache_key]

        scope_file = os.path.join(workspace_path, "scope.md")
        if not os.path.exists(scope_file):
            return None

        try:
            parser = ScopeParser()
            scope_doc = parser.parse_scope_document(scope_file)
            validator = ScopeValidator(scope_doc, logger)
            self._scope_cache[cache_key] = validator
            return validator
        except Exception:
            return None

    @staticmethod
    def _resolve_action_type(capability: Optional[str]) -> ActionType:
        """Resolve capability to ActionType for Action object construction.
        
        ActionType is only needed for Action object construction (legacy requirement).
        It does NOT influence tool selection (that's done via CapabilityType).
        Default to neutral GATHER_INFO to avoid biasing LLM prompts.
        """
        if not capability:
            return ActionType.GATHER_INFO
        
        # Try direct ActionType enum match only (no hardcoded mappings)
        try:
            return ActionType(capability)
        except Exception:
            # Keep neutral default - tool selection happens via CapabilityType, not ActionType
            return ActionType.GATHER_INFO

    def _build_action(self, request: Dict[str, Any], parameters: Dict[str, Any]) -> Action:
        capability = request.get("capability")
        action_type = self._resolve_action_type(capability)
        target = request.get("target") or parameters.get("target") or ""
        reasoning = request.get("reasoning") or f"LangGraph execution for {request['tool']}"
        expected = request.get("expected_outcome") or ""

        return Action(
            type=action_type,
            target=str(target or "unknown"),
            parameters=dict(parameters),
            reasoning=reasoning,
            expected_outcome=expected,
            selected_tools=[request["tool"]],
            tool_parameters={request["tool"]: dict(parameters)},
            execution_strategy=ExecutionStrategy.SEQUENTIAL,
        )


__all__ = ["GraphToolExecutor"]
