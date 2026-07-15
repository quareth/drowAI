from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from agent.tools.tool_call_specs import make_function_name_for_tool

from tests.tools.fixtures.parameter_fixtures import load_param_fixture


class MockLLMClient:
    """Mock LLMClient returning fixture-driven tool calls."""

    def __init__(self, fn_to_tool_id: Optional[Dict[str, str]] = None) -> None:
        self.fn_to_tool_id = fn_to_tool_id or {}

    async def chat_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> Dict[str, Any]:
        tool_name = self._select_tool_name(tools)
        tool_id = self.fn_to_tool_id.get(tool_name)

        params = {"target": "127.0.0.1"}
        if tool_id:
            try:
                fixture = load_param_fixture(tool_id)
                params = fixture["test_cases"]["minimal"]["params"]
            except Exception:
                params = {"target": "127.0.0.1"}

        return {
            "tool_calls": [
                {
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(params),
                    }
                }
            ]
        }

    def _select_tool_name(self, tools: List[Dict[str, Any]]) -> str:
        for tool in tools:
            fn = tool.get("function", {})
            name = fn.get("name")
            if name:
                return name
        return make_function_name_for_tool("unknown.tool")
