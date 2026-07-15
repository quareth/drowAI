"""Adapter that bridges LangGraph nodes to the existing agent executor."""

from __future__ import annotations

import inspect
import os
from dataclasses import dataclass
from time import monotonic
from typing import Any, Dict, Optional

from agent.config import AgentConfig
from agent.executor import EnhancedCommandExecutor
from agent.logger import AgentLogger
from agent.models import Action, ActionType, ExecutionResult, ExecutionStrategy
from agent.planner import ScopeParser
from agent.scope_validator import ScopeValidator
from agent.tool_runtime.pty_identity import derive_parallel_pty_identity
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
        requires_local_executor = (
            dispatch_decision.authority != "container_runner_transport"
        )
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
            runtime_metadata={
                "workspace_id": request.get("workspace_id")
                or (context.workspace_id if context else None),
                "runner_id": request.get("runner_id") or (context.runner_id if context else None),
                "execution_site_id": request.get("execution_site_id")
                or (context.execution_site_id if context else None),
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
        )

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
