from __future__ import annotations

from typing import List, Tuple

from tests.tools.fixtures.output_fixtures import load_output_fixture


class MockExecutor:
    """Return canned outputs for tool executions."""

    def run(self, tool_id: str, command: List[str]) -> Tuple[str, str, int]:
        _ = command
        try:
            stdout = load_output_fixture(tool_id)
        except Exception:
            stdout = f"[fixture] No output fixture for {tool_id}."
        return stdout, "", 0
