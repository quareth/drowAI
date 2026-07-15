"""Execution facade for agent action and tool execution.

Purpose:
- Provide stable orchestration entrypoints used by runtime flows and adapters.

Owns:
- Facade contracts for action/tool execution and dependency injection.
- Dependency composition and collaborator wiring for scope validation, approval, and file-comm.
- High-level orchestration flow entrypoints and result aggregation boundaries.
- Compatibility wrappers that preserve existing caller/test contracts.

Does not own:
- Shell policy internals and strict validation rule definitions.
- PTY/file-comm/direct transport decision internals beyond delegated wrappers.
- Workspace/container path translation internals.
- Result normalization internals and tool-specific command synthesis internals.
- Legacy parse/scan implementation details beyond compatibility delegation.

Invariants:
- Preserve behavior lock: no routing-order, approval, or result-shape drift.
- Preserve security boundaries, including scope validation and workspace-safe path handling.
- Preserve adapter-sensitive method signatures and compatibility wrapper semantics.
"""

import asyncio
import os
import subprocess
from shutil import which
from typing import Optional, List, Dict, Any
try:
    from .logger import AgentLogger
except ImportError:  # pragma: no cover
    from logger import AgentLogger
try:
    from .models import Action, ActionType, ExecutionResult, Finding
except ImportError:  # pragma: no cover
    from models import Action, ActionType, ExecutionResult, Finding
try:
    from .scope_validator import ScopeValidator
except ImportError:  # pragma: no cover
    from scope_validator import ScopeValidator
try:
    from .communication.file_comm import FileCommAgent, execute_tool_via_file_comm
except ImportError:  # pragma: no cover
    from communication.file_comm import FileCommAgent, execute_tool_via_file_comm
try:
    from .interactive.proposal_manager import ProposalManager
except ImportError:  # pragma: no cover
    try:
        from interactive.proposal_manager import ProposalManager
    except Exception:
        ProposalManager = None  # type: ignore
try:
    from .reasoning import EnhancedActionPlanner
except ImportError:  # pragma: no cover
    from reasoning import EnhancedActionPlanner
try:
    from .execution.legacy_scan import (
        execute_nmap_scan as execute_nmap_scan_legacy,
        parse_nmap_output as parse_nmap_output_legacy,
        store_command_output as store_command_output_legacy,
    )
except ImportError:  # pragma: no cover
    from execution.legacy_scan import (
        execute_nmap_scan as execute_nmap_scan_legacy,
        parse_nmap_output as parse_nmap_output_legacy,
        store_command_output as store_command_output_legacy,
    )
try:
    from .execution.gates import evaluate_execution_gates
except ImportError:  # pragma: no cover
    from execution.gates import evaluate_execution_gates
try:
    from .tool_runtime.transport_router import (
        build_pty_transport_command,
        execute_single_tool_with_fallback as execute_single_tool_with_fallback_route,
        execute_via_pty_transport,
        resolve_pty_enabled_cached,
        should_use_pty as should_use_pty_route,
        tool_supports_pty as tool_supports_pty_route,
    )
    from .tool_runtime.timeout_policy import (
        TOOL_TIMEOUT_EXIT_CODE,
        TOOL_TIMEOUT_FAILURE_CATEGORY,
        ToolTimeoutPlan,
        resolve_tool_timeout_plan,
    )
except ImportError:  # pragma: no cover
    from tool_runtime.transport_router import (
        build_pty_transport_command,
        execute_single_tool_with_fallback as execute_single_tool_with_fallback_route,
        execute_via_pty_transport,
        resolve_pty_enabled_cached,
        should_use_pty as should_use_pty_route,
        tool_supports_pty as tool_supports_pty_route,
    )
    from tool_runtime.timeout_policy import (
        TOOL_TIMEOUT_EXIT_CODE,
        TOOL_TIMEOUT_FAILURE_CATEGORY,
        ToolTimeoutPlan,
        resolve_tool_timeout_plan,
    )



try:
    from .utils.workspace_helpers import (
        resolve_container_path,
        resolve_workspace_path_for_executor,
    )
except ImportError:  # pragma: no cover
    from utils.workspace_helpers import (
        resolve_container_path,
        resolve_workspace_path_for_executor,
    )


# Enhanced executor dependencies
try:
    from .tools.tool_registry import run_tool_by_name, get_tool
    from .tools.parameter_validation import (
        ToolParameterValidationResult,
        validate_tool_parameters,
    )
    from .tools.utils import (
        attach_execution_result_extras,
        build_validation_error_execution_result,
        safe_inc_metric,
    )
except ImportError:  # pragma: no cover
    from tools.tool_registry import run_tool_by_name, get_tool
    from tools.parameter_validation import (
        ToolParameterValidationResult,
        validate_tool_parameters,
    )
    from tools.utils import (
        attach_execution_result_extras,
        build_validation_error_execution_result,
        safe_inc_metric,
    )


class CommandExecutor:
    """Execute commands while supporting interruption and resumption."""

    # Section: Dependency setup and external injection
    def __init__(self, config, logger: Optional[AgentLogger] = None):
        self.config = config
        self.logger = logger
        self._current_process: Optional[subprocess.Popen] = None
        self._last_action: Optional[Action] = None
        self._interrupted: bool = False
        self.scope_validator: Optional[ScopeValidator] = None
        self._file_comm: Optional[FileCommAgent] = None
        self._enhanced_planner: Optional[EnhancedActionPlanner] = None


        execution_mode = os.getenv("EXECUTION_MODE")
        workspace = os.getenv("WORKSPACE", "/workspace")
        
        if self.logger:
            self.logger.log_operation("DEBUG", f"CommandExecutor init: EXECUTION_MODE={execution_mode}, WORKSPACE={workspace}")
        
        if execution_mode == "file":
            try:
                self._file_comm = FileCommAgent(workspace)
                if self.logger:
                    self.logger.log_operation("INFO", f"✓ FileCommAgent initialized for workspace: {workspace}")
            except Exception as e:
                if self.logger:
                    self.logger.log_operation("ERROR", f"✗ FileCommAgent initialization failed: {e}")
                self._file_comm = None
        else:
            if self.logger:
                self.logger.log_operation("INFO", f"Direct execution mode (EXECUTION_MODE={execution_mode})")

    @property
    def enhanced_planner(self) -> EnhancedActionPlanner:
        """Return the planner, creating it only for planner-backed paths."""
        if self._enhanced_planner is None:
            self._enhanced_planner = self._build_enhanced_planner()
        return self._enhanced_planner

    @enhanced_planner.setter
    def enhanced_planner(self, planner: EnhancedActionPlanner) -> None:
        """Allow tests and compatibility callers to inject a planner."""
        self._enhanced_planner = planner

    def _build_enhanced_planner(self) -> EnhancedActionPlanner:
        llm_client = None
        resolver = getattr(self.config, "llm_client_resolver", None)
        if callable(resolver):
            llm_client = resolver()
        return EnhancedActionPlanner(self.config, llm_client=llm_client)

    def set_scope_validator(self, validator: ScopeValidator) -> None:
        """Inject a ScopeValidator instance."""
        self.scope_validator = validator

    def set_file_comm(self, comm: FileCommAgent) -> None:
        self._file_comm = comm


    def check_tool_availability(self, tool: str) -> bool:
        return which(tool) is not None

    # Section: Approval and external contracts
    async def _maybe_request_approval(self, tool_name: str, parameters: Dict[str, Any], reasoning: str | None = None) -> bool:
        """Interactive gate: emit proposal and wait for approval when AGENT_MODE=interactive.

        Returns True if approved or not interactive; False if rejected.
        """
        _, approved = await evaluate_execution_gates(
            tool_name=tool_name,
            parameters=parameters,
            reasoning=reasoning,
            last_action=getattr(self, "_last_action", None),
            proposal_manager_cls=ProposalManager,
            workspace=os.getenv("WORKSPACE", "/workspace"),
            logger=self.logger,
            check_approval=True,
        )
        return approved

    async def execute_action(self, action: Action, context: Dict[str, Any] | None = None) -> ExecutionResult:
        """Execute the given action asynchronously with scope validation."""
        self._last_action = action
        context = context or {}

        if self.logger:
            self.logger.log_operation("DEBUG", f"execute_action called: {action.type.value} on {action.target}")
            self.logger.log_operation("DEBUG", f"File comm available: {self._file_comm is not None}")

        blocked_by_scope, _ = await evaluate_execution_gates(
            scope_validator=self.scope_validator,
            command=action.command,
            target=action.target,
            logger=self.logger,
            check_scope=True,
        )
        if blocked_by_scope is not None:
            return blocked_by_scope

        # Single source of truth for tool selection:
        # Use EnhancedActionPlanner to choose concrete tools/parameters, then execute via FileComm when available.
        if action.type in [ActionType.SCAN_PORTS, ActionType.SCAN_WEB, ActionType.ENUMERATE_SERVICES]:
            try:
                plan = await self.enhanced_planner.build_action_plan(action, context)
                selected = list(plan.selected_tools)
                if self.logger:
                    self.logger.log_operation("INFO", f"Planner selected tools: {selected}")
                if not selected:
                    if self.logger:
                        self.logger.log_operation("WARNING", "No tools selected by planner; falling back to direct mapping")
                else:
                    # Execute first selected tool for now; can extend to multiple sequential/concurrent
                    tool_id = selected[0]
                    params = dict(plan.tool_parameters.get(tool_id, {}))
                    # Prefer FileComm execution when available
                    if self._file_comm is not None:
                        res = await self._execute_tool_via_comm(
                            tool_id,
                            params,
                            log_mode="planner",
                            include_metrics=False,
                        )
                        if res is not None:
                            return res
                        # If FileComm failed, continue to the direct action mapping below.
                    else:
                        # Fall back to local registry execution path if desired in the future
                        pass
            except Exception as exc:
                if self.logger:
                    self.logger.log_operation("WARNING", f"Enhanced planner failed, falling back: {exc}")

        if action.type == ActionType.END:
            if self.logger:
                self.logger.log_operation("INFO", "Task completion requested via END action")
            return ExecutionResult(True, "Task completed", "", 0)

        if self._file_comm is not None:
            if self.logger:
                self.logger.log_operation("INFO", "Attempting file communication execution...")
            result = await self._execute_via_comm(action)
            if result is not None:
                if self.logger:
                    self.logger.log_operation("INFO", f"File communication result: success={result.success}")
                return result
            else:
                if self.logger:
                    self.logger.log_operation("WARNING", "File communication returned None, falling back to direct execution")

        if self.logger:
            self.logger.log_operation("INFO", f"Using direct execution for {action.type.value}")

        # Log the unsupported action type for debugging
        if self.logger:
            self.logger.log_operation("ERROR", f"Unsupported action type {action.type}")
        
        return ExecutionResult(False, "", f"Unsupported action type: {action.type}", -1)

    # Section: Compatibility wrappers (transport delegation)
    async def _execute_tool_via_comm(
        self,
        tool_id: str,
        args: Dict[str, Any],
        *,
        timeout_seconds: Optional[float] = None,
        log_mode: str = "enhanced",
        include_metrics: bool = True,
        timeout_plan: Optional[ToolTimeoutPlan] = None,
        interrupt_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        tool_batch_id: Optional[str] = None,
        artifact_stamp: Optional[int] = None,
    ) -> Optional[ExecutionResult]:
        """Compatibility wrapper delegating to communication-owned file-comm bridge."""
        return await execute_tool_via_file_comm(
            file_comm=self._file_comm,
            tool_id=tool_id,
            args=args,
            config=self.config,
            logger=self.logger,
            timeout_seconds=timeout_seconds,
            log_mode=log_mode,
            include_metrics=include_metrics,
            timeout_plan=timeout_plan,
            explicit_command_builder=self._tool_to_shell_command,
            interrupt_id=interrupt_id,
            tool_call_id=tool_call_id,
            tool_batch_id=tool_batch_id,
            artifact_stamp=artifact_stamp,
        )

    def _tool_to_shell_command(
        self,
        tool_id: str,
        parameters: Dict[str, Any],
    ) -> str:
        """Delegate shell/filesystem command synthesis to the transport router."""
        return build_pty_transport_command(
            tool_id,
            parameters,
            resolve_container_path_fn=self._resolve_container_path,
            logger=self.logger,
        )

    def _resolve_workspace_path(self, path: str) -> str:
        """Compatibility shim: resolve paths using shared workspace helper."""
        return resolve_workspace_path_for_executor(
            path,
            workspace_path=getattr(self.config, "workspace_path", "/workspace"),
        )

    def _resolve_container_path(self, path: str) -> str:
        """Compatibility shim: translate paths using shared container helper."""
        return resolve_container_path(
            path,
            host_workspace=getattr(self.config, "workspace_path", ""),
        )

    def interrupt(self) -> None:
        """Interrupt the currently running process, if any."""
        if self._current_process and self._current_process.poll() is None:
            try:
                self._current_process.terminate()
                self._interrupted = True
            except Exception:
                pass

    async def resume(self) -> Optional[ExecutionResult]:
        """Resume execution after an interrupt by rerunning the last action."""
        if self._interrupted and self._last_action is not None:
            return await self.execute_action(self._last_action)
        return None

    async def _execute_via_comm(self, action: Action) -> Optional[ExecutionResult]:
        if self._file_comm is None:
            if self.logger:
                self.logger.log_operation("ERROR", "_execute_via_comm called but file_comm is None")
            return None
            
        tool_map: Dict[ActionType, str] = {
            ActionType.SCAN_PORTS: "information_gathering.network_discovery.nmap",
        }
        tool = tool_map.get(action.type)
        if not tool:
            if self.logger:
                self.logger.log_operation("ERROR", f"Unsupported action type {action.type}")
            return ExecutionResult(False, "", f"Unsupported action type {action.type}", -1)

        # Interactive approval gate
        approved = await self._maybe_request_approval(tool, {"target": action.target}, getattr(action, "reasoning", ""))
        if not approved:
            return ExecutionResult(False, "", "Proposal rejected by user", -1)

        return await self._execute_tool_via_comm(
            tool,
            {"target": action.target},
            log_mode="legacy",
            include_metrics=False,
        )

    # Section: Legacy parse helpers retained for active provider flow
    def parse_results(self, result: ExecutionResult, action: Action) -> List[Finding]:
        """Parse command output into structured findings."""

        if not result.success:
            return []

        if action.type == ActionType.SCAN_PORTS:
            return self._parse_nmap_output(result.stdout, action.target)

        return []

    def _parse_nmap_output(self, output: str, target: str) -> List[Finding]:
        """Compatibility wrapper delegating legacy nmap parsing."""
        return parse_nmap_output_legacy(output, target)

    async def _execute_nmap_scan(self, target: str) -> ExecutionResult:
        """Compatibility wrapper delegating legacy nmap scan execution."""
        timeout_plan = resolve_tool_timeout_plan(
            tool_id="information_gathering.network_discovery.nmap",
            parameters={"target": target},
            config=self.config,
        )
        return await execute_nmap_scan_legacy(
            target=target,
            timeout_seconds=timeout_plan.deadline_seconds,
            logger=self.logger,
            store_output_fn=self._store_command_output,
        )

    async def _store_command_output(self, command: str, output: str) -> None:
        """Compatibility wrapper delegating legacy command-output persistence."""
        await store_command_output_legacy(
            command=command,
            output=output,
            logger=self.logger,
            workspace=os.getenv("WORKSPACE", "/workspace"),
        )


class EnhancedCommandExecutor(CommandExecutor):
    """Command executor with contextual tool selection and aggregation."""

    # Section: Dependency setup and external injection
    def __init__(self, config, logger: Optional[AgentLogger] = None):
        super().__init__(config, logger)
        # Phase 9 Task 9.1: legacy enhanced-selection wrapper retired.
        # Runtime tool dispatch now flows exclusively through the
        # BatchExecutor / orchestrator path; ``execute_action`` is inherited
        # from the parent ``CommandExecutor`` for actions that previously
        # took the enhanced branch.
        # Phase 2: Feature flag caching
        self._pty_enabled_cached = None

    def _build_context(self) -> Dict[str, Any]:
        """Build a minimal execution context."""

        return {
            "current_phase": "enumeration",
            "discovered_services": {},
            "target_responsive": True,
        }

    async def _execute_single_tool(
        self,
        tool_id: str,
        parameters: Dict[str, Any],
        *,
        interrupt_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        allow_pty: bool = True,
        tool_batch_id: Optional[str] = None,
        session_name: Optional[str] = None,
        cleanup_session: bool = False,
        artifact_stamp: Optional[int] = None,
        timeout_plan: Optional[ToolTimeoutPlan] = None,
    ) -> ExecutionResult:
        """Execute a single tool via the registry with timeout protection."""

        # Resolve deprecated fs.* namespace aliases to filesystem.*
        try:
            from .tools.filesystem.aliases import resolve_tool_alias
            tool_id = resolve_tool_alias(tool_id)
        except ImportError:
            pass  # Aliases module not available

        if timeout_plan is None or timeout_plan.tool_id != str(tool_id):
            timeout_plan = resolve_tool_timeout_plan(
                tool_id=tool_id,
                parameters=parameters,
                config=self.config,
            )
        parameters = dict(timeout_plan.normalized_parameters)
        
        # PTY execution has its own internal timeout that preserves partial output.
        # Skip the outer timeout for PTY to avoid canceling and losing partial results.
        use_pty = allow_pty and self._should_use_pty(tool_id, parameters)
        
        try:
            if use_pty:
                # PTY has internal timeout - don't wrap in another timeout
                # This ensures partial output is preserved on timeout
                return await self._execute_single_tool_internal(
                    tool_id,
                    parameters,
                    interrupt_id=interrupt_id,
                    tool_call_id=tool_call_id,
                    allow_pty=allow_pty,
                    tool_batch_id=tool_batch_id,
                    session_name=session_name,
                    cleanup_session=cleanup_session,
                    artifact_stamp=artifact_stamp,
                    timeout_plan=timeout_plan,
                )
            else:
                # Non-PTY routes: wrap in timeout
                return await asyncio.wait_for(
                    self._execute_single_tool_internal(
                        tool_id,
                        parameters,
                        interrupt_id=interrupt_id,
                        tool_call_id=tool_call_id,
                        allow_pty=allow_pty,
                        tool_batch_id=tool_batch_id,
                        session_name=session_name,
                        cleanup_session=cleanup_session,
                        artifact_stamp=artifact_stamp,
                        timeout_plan=timeout_plan,
                    ),
                    timeout=timeout_plan.deadline_seconds,
                )
        except asyncio.TimeoutError:
            if self.logger:
                self.logger.log_operation(
                    "WARNING",
                    f"EnhancedExecutor: Tool {tool_id} timed out after {timeout_plan.deadline_seconds} seconds",
                )
            result = ExecutionResult(
                success=False,
                stdout="",
                stderr=f"Tool {tool_id} timed out after {timeout_plan.deadline_seconds} seconds",
                exit_code=TOOL_TIMEOUT_EXIT_CODE,
            )
            attach_execution_result_extras(
                result,
                metadata={
                    "failure_category": TOOL_TIMEOUT_FAILURE_CATEGORY,
                    "timeout_policy": timeout_plan.to_metadata(),
                    "timed_out": True,
                    "killed": False,
                },
            )
            return result
        except Exception as exc:
            if self.logger:
                self.logger.log_operation(
                    "ERROR", f"EnhancedExecutor: Tool {tool_id} failed with exception: {exc}"
                )
            return ExecutionResult(False, "", str(exc), -1)

    def _validate_tool_parameters(
        self,
        tool_id: str,
        parameters: Dict[str, Any],
    ) -> ToolParameterValidationResult:
        return validate_tool_parameters(
            tool_id,
            parameters,
            max_shell_command_chars=int(
                getattr(self.config, "shell_exec_max_command_chars", 320) or 320
            ),
            metric_hook=safe_inc_metric,
            logger=self.logger,
        )

    def _build_validation_error_result(
        self,
        validation_errors: List[Dict[str, str]],
    ) -> ExecutionResult:
        return build_validation_error_execution_result(validation_errors)

    async def _execute_single_tool_internal(
        self,
        tool_id: str,
        parameters: Dict[str, Any],
        *,
        interrupt_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        allow_pty: bool = True,
        tool_batch_id: Optional[str] = None,
        session_name: Optional[str] = None,
        cleanup_session: bool = False,
        artifact_stamp: Optional[int] = None,
        timeout_plan: Optional[ToolTimeoutPlan] = None,
    ) -> ExecutionResult:
        """Compatibility wrapper delegating single-tool routing to transport router."""
        pty_session_not_available_exc_type = None
        try:
            from agent.tools.shell._pty_executor import PTYSessionNotAvailable

            pty_session_not_available_exc_type = PTYSessionNotAvailable
        except Exception:
            pty_session_not_available_exc_type = None

        return await execute_single_tool_with_fallback_route(
            tool_id=tool_id,
            parameters=parameters,
            config=self.config,
            logger=self.logger,
            file_comm=self._file_comm,
            validate_tool_parameters_fn=self._validate_tool_parameters,
            build_validation_error_result_fn=self._build_validation_error_result,
            should_use_pty_fn=self._should_use_pty,
            execute_via_pty_fn=self._execute_via_pty,
            execute_tool_via_comm_fn=self._execute_tool_via_comm,
            run_tool_by_name_fn=run_tool_by_name,
            safe_inc_metric_fn=safe_inc_metric,
            pty_session_not_available_exc_type=pty_session_not_available_exc_type,
            interrupt_id=interrupt_id,
            tool_call_id=tool_call_id,
            allow_pty=allow_pty,
            tool_batch_id=tool_batch_id,
            session_name=session_name,
            cleanup_session=cleanup_session,
            artifact_stamp=artifact_stamp,
            timeout_plan=timeout_plan,
        )

    async def _execute_via_pty(
        self,
        tool_id: str,
        parameters: Dict[str, Any],
        *,
        interrupt_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        tool_batch_id: Optional[str] = None,
        session_name: Optional[str] = None,
        cleanup_session: bool = False,
        artifact_stamp: Optional[int] = None,
        timeout_plan: Optional[ToolTimeoutPlan] = None,
    ) -> ExecutionResult:
        """Compatibility wrapper delegating PTY orchestration to transport router."""
        return await execute_via_pty_transport(
            tool_id=tool_id,
            parameters=parameters,
            config=self.config,
            logger=self.logger,
            tool_to_shell_command_fn=self._tool_to_shell_command,
            interrupt_id=interrupt_id,
            tool_call_id=tool_call_id,
            tool_batch_id=tool_batch_id,
            session_name=session_name,
            cleanup_session=cleanup_session,
            artifact_stamp=artifact_stamp,
            timeout_plan=timeout_plan,
        )

    def _should_use_pty(self, tool_id: str, parameters: Dict[str, Any]) -> bool:
        """Compatibility wrapper delegating PTY routing decision to transport router."""
        return should_use_pty_route(
            tool_id,
            parameters,
            is_pty_enabled_fn=self._is_pty_enabled,
            tool_supports_pty_fn=self._tool_supports_pty,
            logger=self.logger,
        )
    
    def _is_pty_enabled(self) -> bool:
        """Compatibility wrapper for cached PTY feature flag lookup."""
        self._pty_enabled_cached = resolve_pty_enabled_cached(
            self._pty_enabled_cached,
            logger=self.logger,
        )
        return self._pty_enabled_cached
    
    def _tool_supports_pty(self, tool_id: str) -> bool:
        """Compatibility wrapper delegating PTY capability checks to transport router."""
        return tool_supports_pty_route(tool_id, get_tool_fn=get_tool)

    def _aggregate_results(
        self, results: List[Dict[str, Any]], action: Action
    ) -> ExecutionResult:
        """Combine multiple tool outputs into a single result using shared helper."""

        from .tools.utils import aggregate_tool_results

        aggregated, _ = aggregate_tool_results(results, include_findings=False)
        return aggregated
