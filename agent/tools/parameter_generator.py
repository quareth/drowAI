from __future__ import annotations

"""Schema-based parameter extraction for tools.

This module provides a minimal parameter generator that extracts default
values from tool Pydantic schemas without injecting any contextual overrides.
All parameter decisions are delegated to the LLM.
"""

from typing import Any, Dict  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from .tool_registry import get_tool  # noqa: E402


class ContextualParameterGenerator:
    """Generate tool parameters based on Pydantic schema defaults only.

    This generator extracts default values from the tool's argument schema
    without any contextual overrides. All parameter decisions are made by the LLM.
    
    Previously this class contained tool-specific configuration logic that would
    inject "helpful" defaults based on phase/context, but this has been removed
    to rely 100% on LLM parameter generation.
    """

    def __init__(self, config: Any | None = None) -> None:
        self.config = config or SimpleNamespace()

    # ------------------------------------------------------------------
    def generate_parameters(
        self, tool_id: str, action_type: str = None, context: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Return base parameters from the tool's schema defaults.
        
        Args:
            tool_id: Tool identifier
            action_type: (Deprecated) No longer used, kept for backward compatibility
            context: (Deprecated) No longer used, kept for backward compatibility
        
        Returns:
            Dict with only the defaults defined in the tool's Pydantic schema
        """
        tool_cls = get_tool(tool_id)
        return self._get_base_parameters_from_schema(tool_cls)

    # ------------------------------------------------------------------
    def _get_base_parameters_from_schema(self, tool_cls: type) -> Dict[str, Any]:
        """Extract default parameter values from a tool's Pydantic schema.
        
        Only returns parameters that have explicit defaults defined in the schema.
        Does not inject any contextual or phase-based overrides.
        """
        schema = tool_cls.args_model.model_json_schema()
        properties = schema.get("properties", {})
        base_params: Dict[str, Any] = {}
        for name, info in properties.items():
            default = info.get("default")
            if default is not None:
                base_params[name] = default
        return base_params
