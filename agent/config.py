"""
Minimal agent runtime configuration.

This module provides environment-backed defaults for subprocess agent runtime
configuration. The default model fallback is GPT-5 (`gpt-5.2`) to align with
the active GPT-5-only execution policy.
"""
import os
from typing import Optional, Dict
from dataclasses import dataclass, field
from agent.tool_runtime.timeout_policy import (
    DEFAULT_TOOL_TIMEOUT_GRACE_SECONDS,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    ToolTimeoutConfig,
)
from core.llm.timeouts import (
    LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC,
    LLM_TIMEOUT_PLANNER_TOOL_SELECTION_SEC,
    read_llm_timeout_planner_parameter_resolution_sec,
    read_llm_timeout_planner_tool_selection_sec,
)


PLANNER_TOOL_CALL_TIMEOUT_SEC = LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC


def _read_first_positive_float_env(names: tuple[str, ...], default: float) -> float:
    """Read the first positive float env value from ``names``."""
    for name in names:
        raw = os.getenv(name)
        if raw is None:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if value > 0:
            return value
    return default


@dataclass 
class AgentConfig:
    """Configuration for the agent process"""
    
    task_id: Optional[str] = None
    tenant_id: Optional[int] = None
    runtime_placement_mode: str = "local"
    workspace_path: str = "/workspace"
    openai_api_key: Optional[str] = None
    model_name: str = "gpt-5.2"
    command_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    artifacts_dir: str = "/workspace/artifacts"
    nmap_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    
    # LLM configuration attributes
    temperature: float = 0.1
    max_tokens: int = 4000
    max_concurrent_scans: int = 3
    tool_paths: Dict[str, str] = field(default_factory=dict)
    # Enhanced planning/execution knobs
    max_tools_per_action: int = 3
    default_execution_strategy: str = "parallel"  # sequential|parallel
    tool_timeout_default_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    tool_timeout_max_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    tool_timeout_grace_seconds: float = DEFAULT_TOOL_TIMEOUT_GRACE_SECONDS
    # Deprecated compatibility aliases. Tool execution code must read these
    # through ToolTimeoutPolicy only.
    tool_execution_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    individual_tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    concurrent_execution_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    # Tool-batch execution knobs (see docs/architecture/tool-batch-execution.md).
    # max_tools_per_action above is the *selector candidate cap*; the field below
    # is the *validator commit cap*. Defaults below keep batch wiring shaped but
    # user-visible behavior single-tool until later phases flip them.
    max_committed_tools_per_batch: int = 3  # validator cap on committed calls (Phase 7 Task 7.7 flipped 1→3)
    parallel_execution_enabled: bool = True  # Phase 8 Task 8.1 flipped False→True; gates BatchExecutor parallel branch
    emit_batch_events_for_single_call: bool = True  # Phase 7 Task 7.7 flipped False→True; frontend now groups via tool_batch_id
    batch_default_execution_strategy: str = "sequential"  # validator default when builder omits a strategy
    # LLM planning knobs
    enforce_llm_tool_selection: bool = False
    llm_tool_selection_timeout: int = LLM_TIMEOUT_PLANNER_TOOL_SELECTION_SEC
    # Tool-calling knobs
    use_llm_tool_calls: bool = True
    max_tools_exposed: int = 3
    tool_call_timeout: int = PLANNER_TOOL_CALL_TIMEOUT_SEC
    shell_exec_max_command_chars: int = 320
    tool_choice_mode: str = "auto"  # auto|required
    
    # Todo completion guardrails (Phase 1.1)
    max_todo_attempts: int = 5  # Maximum attempts before marking todo as exhausted
    max_todo_actions: int = 10  # Maximum actions/tools executed for single todo
    max_todo_time_seconds: int = 300  # Maximum time (5 minutes) before marking exhausted
    
    # Agent pause feature flags (Phase 3.1)
    enable_agent_pause: bool = False  # Enable agent-initiated pause for user confirmation
    pause_response_timeout: int = 300  # Timeout in seconds to wait for user response (5 minutes)
    pause_min_remaining_todos: int = 5  # Minimum remaining todos to trigger pause
    pause_context_length_threshold: int = 10  # Minimum observations to trigger pause
    pause_budget_concern_tools: int = 10  # Tools used before budget concern pause
    
    @classmethod
    def load_from_env(cls) -> 'AgentConfig':
        """Load configuration from environment variables"""
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required")

        cfg = cls(
            task_id=os.getenv("TASK_ID"),
            runtime_placement_mode=os.getenv("RUNTIME_PLACEMENT_MODE", "local"),
            workspace_path=os.getenv("WORKSPACE", "/workspace"),
            openai_api_key=api_key,
            model_name=os.getenv("MODEL_NAME", "gpt-5.2"),
            command_timeout=_read_first_positive_float_env(
                ("COMMAND_TIMEOUT",),
                DEFAULT_TOOL_TIMEOUT_SECONDS,
            ),
            artifacts_dir=os.getenv("ARTIFACTS_DIR", "/workspace/artifacts"),
            nmap_timeout=_read_first_positive_float_env(
                ("NMAP_TIMEOUT",),
                DEFAULT_TOOL_TIMEOUT_SECONDS,
            ),
            temperature=float(os.getenv("LLM_TEMPERATURE", "0.1")),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "4000")),
            max_concurrent_scans=int(os.getenv("MAX_CONCURRENT_SCANS", "3"))
        )
        # Enhanced planning/execution knobs from env
        cfg.max_tools_per_action = int(os.getenv("MAX_TOOLS_PER_ACTION", str(cfg.max_tools_per_action)))
        cfg.default_execution_strategy = os.getenv(
            "DEFAULT_EXECUTION_STRATEGY", cfg.default_execution_strategy
        )
        cfg.tool_timeout_default_seconds = _read_first_positive_float_env(
            (
                "TOOL_TIMEOUT_DEFAULT_SECONDS",
                "TOOL_EXECUTION_TIMEOUT",
                "NMAP_TIMEOUT",
                "COMMAND_TIMEOUT",
            ),
            cfg.tool_timeout_default_seconds,
        )
        cfg.tool_timeout_max_seconds = _read_first_positive_float_env(
            (
                "TOOL_TIMEOUT_MAX_SECONDS",
                "CONCURRENT_EXECUTION_TIMEOUT",
            ),
            cfg.tool_timeout_default_seconds,
        )
        cfg.tool_timeout_grace_seconds = _read_first_positive_float_env(
            ("TOOL_TIMEOUT_GRACE_SECONDS",),
            cfg.tool_timeout_grace_seconds,
        )
        timeout_config = ToolTimeoutConfig.from_runtime_config(cfg)
        cfg.tool_timeout_default_seconds = timeout_config.default_seconds
        cfg.tool_timeout_max_seconds = timeout_config.max_seconds
        cfg.tool_timeout_grace_seconds = timeout_config.grace_seconds
        # Keep deprecated aliases coherent for code/tests that still inspect
        # AgentConfig, but keep timeout ownership in ToolTimeoutPolicy.
        cfg.tool_execution_timeout = timeout_config.default_seconds
        cfg.individual_tool_timeout = timeout_config.default_seconds
        cfg.concurrent_execution_timeout = timeout_config.max_seconds
        cfg.command_timeout = timeout_config.default_seconds
        cfg.nmap_timeout = timeout_config.default_seconds
        # Tool-batch execution knobs from env
        cfg.max_committed_tools_per_batch = int(
            os.getenv("MAX_COMMITTED_TOOLS_PER_BATCH", str(cfg.max_committed_tools_per_batch))
        )
        cfg.parallel_execution_enabled = os.getenv(
            "PARALLEL_EXECUTION_ENABLED", str(cfg.parallel_execution_enabled)
        ).lower() == "true"
        cfg.emit_batch_events_for_single_call = os.getenv(
            "EMIT_BATCH_EVENTS_FOR_SINGLE_CALL", str(cfg.emit_batch_events_for_single_call)
        ).lower() == "true"
        cfg.batch_default_execution_strategy = os.getenv(
            "BATCH_DEFAULT_EXECUTION_STRATEGY", cfg.batch_default_execution_strategy
        )
        # LLM planning knobs from env
        cfg.llm_tool_selection_timeout = read_llm_timeout_planner_tool_selection_sec(
            cfg.llm_tool_selection_timeout,
        )
        # Tool-calling knobs from env
        cfg.use_llm_tool_calls = os.getenv("USE_LLM_TOOL_CALLS", str(cfg.use_llm_tool_calls)).lower() == "true"
        cfg.max_tools_exposed = int(os.getenv("MAX_TOOLS_EXPOSED", str(cfg.max_tools_exposed)))
        cfg.tool_call_timeout = read_llm_timeout_planner_parameter_resolution_sec(
            cfg.tool_call_timeout,
        )
        cfg.shell_exec_max_command_chars = int(
            os.getenv("SHELL_EXEC_MAX_COMMAND_CHARS", str(cfg.shell_exec_max_command_chars))
        )
        # Todo completion guardrails from env
        cfg.max_todo_attempts = int(os.getenv("MAX_TODO_ATTEMPTS", str(cfg.max_todo_attempts)))
        cfg.max_todo_actions = int(os.getenv("MAX_TODO_ACTIONS", str(cfg.max_todo_actions)))
        cfg.max_todo_time_seconds = int(os.getenv("MAX_TODO_TIME_SECONDS", str(cfg.max_todo_time_seconds)))
        # Agent pause feature flags from env
        cfg.enable_agent_pause = os.getenv("ENABLE_AGENT_PAUSE", str(cfg.enable_agent_pause)).lower() == "true"
        cfg.pause_response_timeout = int(os.getenv("PAUSE_RESPONSE_TIMEOUT", str(cfg.pause_response_timeout)))
        cfg.pause_min_remaining_todos = int(os.getenv("PAUSE_MIN_REMAINING_TODOS", str(cfg.pause_min_remaining_todos)))
        cfg.pause_context_length_threshold = int(os.getenv("PAUSE_CONTEXT_LENGTH_THRESHOLD", str(cfg.pause_context_length_threshold)))
        cfg.pause_budget_concern_tools = int(os.getenv("PAUSE_BUDGET_CONCERN_TOOLS", str(cfg.pause_budget_concern_tools)))
        return cfg

    def validate(self) -> None:
        """Validate configuration values."""
        if self.max_tokens < 100:
            raise ValueError("max_tokens must be at least 100")
        for name, path in self.tool_paths.items():
            if not os.path.exists(path):
                raise FileNotFoundError(f"Tool path not found: {path}")
