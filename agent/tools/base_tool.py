"""Base interface and validation helpers for penetration testing tools."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Type

from runtime_shared.workspace_files import (
    RuntimeWorkspaceDirectory,
    RuntimeWorkspaceFile,
    RuntimeWorkspaceFileError,
    normalize_workspace_relative_path,
)

from pydantic import BaseModel, ValidationError

from .canonical_capture import ToolCaptureContract
from .schemas import ToolResult
from .exceptions import ToolValidationError
from .utils import build_domain_validation_error_tool_result, generate_fix_suggestion


@dataclass(slots=True)
class ToolPostprocessResult:
    """Canonical postprocess payload used by direct/file-comm/PTY paths."""

    success: bool
    exit_code: int
    stdout: str
    stderr: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ToolRuntimeOutputFile:
    """One workspace file a tool expects to produce during command execution."""

    relative_path: str
    description: str | None = None
    required: bool = True
    min_bytes: int | None = None
    max_bytes: int | None = None
    expected_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "relative_path",
            normalize_workspace_relative_path(self.relative_path),
        )
        for field_name in ("min_bytes", "max_bytes"):
            value = getattr(self, field_name)
            if value is not None and int(value) < 0:
                raise RuntimeWorkspaceFileError(f"{field_name} must be non-negative")


class BaseTool(ABC):
    """Abstract base class for all tools.
    
    Tools can optionally implement PTY execution support by overriding:
    - build_command(): Build shell command arguments
    - parse_output(): Parse command output into structured metadata
    - create_artifacts(): Create artifact files from output
    
    The supports_pty() method auto-detects PTY support based on build_command() override.
    """

    #: Pydantic model used to validate input arguments
    args_model: Type[BaseModel] = BaseModel

    #: Optional planner-facing Pydantic model. When unset, planners use ``args_model``.
    planner_args_model: Optional[Type[BaseModel]] = None

    #: Optional compact planner guidance appended to tool-call descriptions.
    planner_guidance: str = ""

    #: Optional capture contract declaring this tool's internal capture strategy.
    #: Subclasses may set this class attribute to declare structured-native or
    #: text-native capture.  When None (the default), the tool has not yet been
    #: classified under the canonical capture model and retains its existing behavior.
    _capture_contract: Optional[ToolCaptureContract] = None

    #: Non-zero exit codes that still represent a completed, useful run
    #: (e.g. partial discovery). Hard CLI failure text in stdout/stderr always
    #: fails the run regardless of this set.
    informational_exit_codes: frozenset[int] = frozenset()

    @classmethod
    def get_planner_args_model(cls) -> Type[BaseModel]:
        """Return the planner-facing argument schema for this tool."""

        return cls.planner_args_model or cls.args_model

    @classmethod
    def get_planner_guidance(cls) -> str:
        """Return compact planner guidance for this tool."""

        return str(getattr(cls, "planner_guidance", "") or "").strip()

    @classmethod
    def compile_planner_parameters(
        cls,
        planner_args: BaseModel | Dict[str, Any],
        *,
        action_target: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compile planner-facing arguments into execution-facing arguments.

        Tools without a planner/execution schema split use identity compilation.
        """

        _ = action_target
        if isinstance(planner_args, BaseModel):
            return dict(planner_args.model_dump(exclude_none=True))
        return dict(planner_args or {})

    def capture_contract(self) -> Optional[ToolCaptureContract]:
        """Return this tool's canonical capture contract, if declared.

        Tools that have been classified under the canonical capture model
        set ``_capture_contract`` at the class level.  Unclassified tools
        return ``None``, preserving backward-compatible behavior.
        """
        return self._capture_contract

    def build_command(self, args: BaseModel) -> List[str]:
        """Build shell command arguments for this tool.
        
        Override in subclasses that support PTY execution.
        Tools that don't override this will fall back to direct execution.
        
        Args:
            args: Validated tool arguments
            
        Returns:
            List of command arguments (e.g., ["nmap", "-sS", "-p", "80", "target"])
            
        Raises:
            NotImplementedError: If tool doesn't support PTY execution
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support PTY execution. "
            f"Override build_command() to enable PTY support."
        )

    def prepare_workspace_files(self, args: BaseModel) -> List[RuntimeWorkspaceFile]:
        """Declare runtime workspace files required before command execution."""
        _ = args
        return []

    def prepare_workspace_directories(
        self,
        args: BaseModel,
    ) -> List[RuntimeWorkspaceDirectory]:
        """Declare runtime workspace directories required before command execution."""
        _ = args
        return []

    def runtime_output_files(
        self,
        args: BaseModel,
        *,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> List[ToolRuntimeOutputFile]:
        """Declare runtime workspace files expected after successful execution."""
        _ = args, metadata
        return []

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: BaseModel,
    ) -> Dict[str, Any]:
        """Parse command output into structured metadata.
        
        Override in subclasses that produce structured output (XML, JSON, etc.).
        Default implementation returns empty dict.
        
        Args:
            stdout: Command stdout
            stderr: Command stderr
            exit_code: Command exit code
            args: Original tool arguments
            
        Returns:
            Metadata dictionary (e.g., {"open_ports": [...], "hosts": [...]})
        """
        return {}

    def emit_semantic_observations(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: BaseModel,
        metadata: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Optionally emit canonical semantic observations for tool transport.

        The default implementation is a no-op to preserve backward compatibility.
        """
        _ = stdout, stderr, exit_code, args, metadata
        return []

    def emit_semantic_evidence(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: BaseModel,
        metadata: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Optionally emit bounded semantic evidence entries.

        Tools that return entries MUST use values from
        `agent.semantic.evidence_vocabulary.SemanticEvidenceType`.
        Unknown types are dropped by the shared validator without raising.
        Default implementation is a no-op to preserve backward compatibility.
        """
        _ = stdout, stderr, exit_code, args, metadata
        return []

    def create_artifacts(
        self,
        stdout: str,
        args: BaseModel,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create artifact files from command output.
        
        Override in subclasses that save output files.
        Default implementation returns empty list.
        
        Args:
            stdout: Command stdout
            args: Original tool arguments
            timestamp: Optional timestamp for artifact naming
            
        Returns:
            List of artifact file paths created
        """
        return []

    def postprocess_execution(
        self,
        *,
        args: BaseModel,
        stdout: str,
        stderr: str,
        exit_code: int,
        success: bool,
        metadata: Dict[str, Any],
        artifacts: List[str],
        runtime_context: Optional[Any] = None,
    ) -> ToolPostprocessResult:
        """Optional transport-agnostic semantic postprocessing hook.

        Tools may override this when execution success requires semantic checks
        beyond raw process exit codes (for example file integrity verification).
        """
        _ = args, runtime_context
        return ToolPostprocessResult(
            success=success,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            metadata=dict(metadata or {}),
            artifacts=list(artifacts or []),
        )

    def apply_postprocess_to_tool_result(
        self,
        *,
        args: BaseModel,
        result: ToolResult,
        runtime_context: Optional[Any] = None,
    ) -> ToolResult:
        """Apply ``postprocess_execution`` to a ``ToolResult`` safely."""
        try:
            post = self.postprocess_execution(
                args=args,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                success=result.success,
                metadata=dict(result.metadata or {}),
                artifacts=list(result.artifacts or []),
                runtime_context=runtime_context,
            )
            return ToolResult(
                success=post.success,
                exit_code=post.exit_code,
                stdout=post.stdout,
                stderr=post.stderr,
                artifacts=post.artifacts,
                metadata=post.metadata,
                execution_time=result.execution_time,
                validation_errors=result.validation_errors,
            )
        except Exception:
            return result

    def is_success_exit_code(
        self,
        exit_code: int,
        args: BaseModel,
        *,
        stdout: str = "",
        stderr: str = "",
        parsed_metadata: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        """Determine whether a completed invocation succeeded at the tool layer.

        Uses declarative ``informational_exit_codes`` plus generic hard-failure
        detection over stdout/stderr. Tools may set ``execution_outcome`` in
        ``parse_output`` metadata to override when necessary.
        """
        _ = args
        from .execution_outcome import resolve_execution_success

        return resolve_execution_success(
            exit_code=exit_code,
            informational_exit_codes=getattr(self, "informational_exit_codes", frozenset()),
            stdout=stdout,
            stderr=stderr,
            parsed_metadata=parsed_metadata,
        )

    def supports_pty(self) -> bool:
        """Check if this tool supports PTY execution.
        
        Returns True if build_command() is implemented (overridden from base).
        Tools can override this for custom PTY support detection.
        
        Returns:
            True if tool supports PTY execution, False otherwise
        """
        try:
            # Check if build_command is overridden (not the base NotImplementedError version)
            return type(self).build_command is not BaseTool.build_command
        except Exception:
            return False

    @abstractmethod
    def run(self, args: BaseModel) -> ToolResult:
        """Execute the tool using validated arguments."""

    def validate_and_run(self, data: Dict[str, Any]) -> ToolResult:
        """Validate input data and execute the tool.

        Parameters
        ----------
        data:
            Raw input dictionary from the agent.
        """

        try:
            args = self.args_model(**data)
        except ValidationError as e:
            errors = [
                ToolValidationError(
                    field=".".join(str(x) for x in err["loc"]),
                    error=err["msg"],
                    suggested_fix=generate_fix_suggestion(err),
                )
                for err in e.errors()
            ]

            # Build a descriptive stderr with suggestions for quick remediation
            parts = []
            for err in errors:
                suggestion = f" (suggestion: {err.suggested_fix})" if err.suggested_fix else ""
                parts.append(f"{err.field}: {err.error}{suggestion}")
            stderr_msg = "; ".join(parts) if parts else "Input validation failed"

            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr=f"Validation error: {stderr_msg}",
                validation_errors=[err.model_dump() for err in errors],
                artifacts=[],
                metadata={"error_type": "validation_error", "validation_errors": [err.model_dump() for err in errors]},
                execution_time=0.0,
            )
        try:
            return self.run(args)
        except ValueError as exc:
            return build_domain_validation_error_tool_result(exc)
