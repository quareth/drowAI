"""Configuration modules for agent components.

This package contains centralized configuration for various agent subsystems,
ensuring consistent behavior across automatic mode and LangGraph.

Note: This package also re-exports AgentConfig from the parent config.py module
to maintain backward compatibility with existing imports.
"""

# Import chunking configuration
from agent.config.chunking_config import (
    CHUNKING_PROFILES_DIR,
    DEFAULT_MAX_CHUNK_TOKENS,
    INGEST_SIBLING_ARTIFACTS,
    SIBLING_EXTENSIONS,
    MAX_SIBLINGS_PER_ARTIFACT,
)

# Re-export AgentConfig from parent config.py module for backward compatibility
# This allows `from agent.config import AgentConfig` to continue working
import importlib.util
from pathlib import Path

_config_module_path = Path(__file__).parent.parent / "config.py"
_spec = importlib.util.spec_from_file_location("agent_config_module", _config_module_path)
_config_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_config_module)
AgentConfig = _config_module.AgentConfig
PLANNER_TOOL_CALL_TIMEOUT_SEC = _config_module.PLANNER_TOOL_CALL_TIMEOUT_SEC

__all__ = [
    "AgentConfig",
    "PLANNER_TOOL_CALL_TIMEOUT_SEC",
    "CHUNKING_PROFILES_DIR",
    "DEFAULT_MAX_CHUNK_TOKENS",
    "INGEST_SIBLING_ARTIFACTS",
    "SIBLING_EXTENSIONS",
    "MAX_SIBLINGS_PER_ARTIFACT",
]
